#!/usr/bin/env python3
import os
import gc
import json
import datetime
import threading
import subprocess
import struct
import io
import fcntl
import time
import socket
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from loguru import logger

try:
    from fm_radio_station.radio_core.receivers import FMReceiver, AMReceiver, StreamingFMReceiver, StreamingAMReceiver
except ImportError as e:
    logger.error(f"GNU Radio or osmosdr library is not installed: {e}")
    exit(1)

from fm_radio_station.radio_core.stations import STATIONS, get_station
from fm_radio_station.radio_core.radiko import RadikoClient
from fm_radio_station.radio_core.utils import sanitize_filename, JST, convert_datetime
from fm_radio_station.radio_core.transcoder import stream_fd_as_mp3, stream_growing_wav_as_mp3
from fm_radio_station import paths

try:
    from fm_radio_station.asr_core import (
        ThreadedASRSession, ASRConfig,
        transcribe_wav_to_vtt, asr_batch_available,
    )
    _ASR_AVAILABLE = True
except Exception as e:  # numpy 等の依存が無い環境でも WebUI は動かす
    ThreadedASRSession = None
    ASRConfig = None
    transcribe_wav_to_vtt = None
    asr_batch_available = None
    _ASR_AVAILABLE = False
    logger.warning(f"asr_core unavailable, transcription disabled: {e}")

# テンプレート/静的アセットはパッケージ同梱（cwd 非依存）。状態・録音先は
# 実行ディレクトリ基準＋env 上書きで解決する（fm_radio_station.paths に集約）。
_WEB_DIR = paths.web_dir()

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

_LOG_DIR = paths.log_dir()
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logger.add(
    _LOG_DIR / "webui.log",
    rotation="00:00",
    retention="3 days",
    encoding="utf-8",
    level="INFO",
)
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
templates.env.filters['basename'] = os.path.basename


def _static_version(filename: str) -> str:
    """静的アセットのキャッシュバスティング用バージョン（ファイル mtime）。

    `<link>/<script>` の URL に `?v=` として付け、ファイル更新時に必ず再取得させる
    （ブラウザ/Turbo のキャッシュで古い CSS/JS が残るのを防ぐ）。
    """
    try:
        return str(int((_WEB_DIR / "static" / filename).stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["static_v"] = _static_version

# ------------------------------
# ＜状態管理＞
STATE_FILE = str(paths.state_file())
RECORDINGS_DIR = str(paths.recordings_dir())
if not os.path.exists(RECORDINGS_DIR):
    os.makedirs(RECORDINGS_DIR)
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

_state_lock = threading.Lock()
_radiko_client = RadikoClient()


def load_state():
    """Load recording state from the JSON state file, parsing datetime strings back to objects."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_state = json.load(f)
        for category in ["scheduled", "in_progress", "completed"]:
            for item in raw_state.get(category, []):
                item["start_time"] = datetime.datetime.strptime(
                    item["start_time"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=JST)
        return raw_state
    return {"scheduled": [], "in_progress": [], "completed": []}


def save_state(state):
    """Persist the current recording state dict to the JSON state file."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4, default=convert_datetime)


state_data = load_state()
SCHEDULED_RECORDINGS = state_data.get("scheduled", [])
IN_PROGRESS_RECORDINGS = state_data.get("in_progress", [])
COMPLETED_RECORDINGS = state_data.get("completed", [])


def update_global_state():
    """Rebuild state_data from the global lists and write it to disk."""
    global state_data
    state_data = {
        "scheduled": SCHEDULED_RECORDINGS,
        "in_progress": IN_PROGRESS_RECORDINGS,
        "completed": COMPLETED_RECORDINGS,
    }
    save_state(state_data)


# ------------------------------
# ＜ストリーマー管理＞
_active_streamers: dict = {}  # station_id → (StreamingReceiver, fifo_path)
_stream_lock = threading.Lock()
_sdr_lock = threading.Lock()   # SDRハードウェア排他ロック（USB占有を保証）
_recording_events: dict = {}   # output_file → threading.Event（録音中止シグナル）


# ------------------------------
# ＜音声認識（自動字幕）＞
_ASR_ENABLED = os.environ.get("ASR_ENABLED", "1") != "0"
_asr_session = None              # ThreadedASRSession | None
_asr_station = None              # 認識中の station_id
_asr_transcript: list = []       # [{segment_id, text, t_start, t_end}]
_asr_lock = threading.Lock()


def _start_asr(station_id: str):
    """指定局のストリームに対して音声認識セッションを開始する。

    モデル/バイナリ未設置や依存欠如で起動に失敗しても例外を投げず、
    配信はそのまま継続する（字幕が出ないだけ）。
    """
    if not (_ASR_ENABLED and _ASR_AVAILABLE):
        return
    _stop_asr()
    with _asr_lock:
        _asr_transcript.clear()
        global _asr_station
        _asr_station = station_id
    try:
        session = ThreadedASRSession(ASRConfig())
        session.start()
    except Exception as e:
        logger.error(f"[asr] start failed ({station_id}): {e}")
        return
    global _asr_session
    with _asr_lock:
        _asr_session = session


def _stop_asr():
    """音声認識セッションを停止する（transcript は表示継続のため消さない）。"""
    global _asr_session
    with _asr_lock:
        session = _asr_session
        _asr_session = None
    if session is not None:
        try:
            session.stop()
        except Exception:
            pass


def _on_pcm(chunk: bytes):
    """transcoder の PCM tap。ASR に給餌し、得られた結果を transcript へ追記する。"""
    session = _asr_session
    if session is None:
        return
    session.feed(chunk)
    results = session.poll()
    if results:
        with _asr_lock:
            for r in results:
                _asr_transcript.append({
                    "segment_id": r.segment_id,
                    "text": r.text,
                    "t_start": round(r.t_start, 2),
                    "t_end": round(r.t_end, 2),
                })


# ------------------------------
# ＜録音ファイルの字幕生成（WebVTT・非同期バッチ）＞
# 録音済み WAV を parakeet-cli でファイル全体一括認識し、<base>.vtt（WebVTT）を生成する。
# CPU 重いので単一ワーカーで直列化し、ファイルごとに状態を持つ。
_ASR_BATCH_AVAILABLE = bool(_ASR_AVAILABLE and asr_batch_available and asr_batch_available())
_transcribe_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-vtt")
_transcribe_status: dict = {}   # wav basename → "running" | "done" | "error"
_transcribe_lock = threading.Lock()


def _vtt_path_for(wav_basename: str) -> str:
    """WAV ファイル名（basename）に対応する WebVTT サイドカーの絶対パス。"""
    base = os.path.basename(wav_basename)
    if base.endswith(".wav"):
        base = base[:-4]
    return os.path.join(RECORDINGS_DIR, base + ".vtt")


def _has_vtt(wav_basename: str) -> bool:
    """Return True if a WebVTT subtitle sidecar already exists for *wav_basename*."""
    return os.path.exists(_vtt_path_for(wav_basename))


def _transcribe_state(wav_basename: str) -> str:
    """字幕生成の状態。VTT が既に在れば done、実行中なら running、無ければ idle。"""
    base = os.path.basename(wav_basename)
    if _has_vtt(base):
        return "done"
    with _transcribe_lock:
        return _transcribe_status.get(base, "idle")


def _run_transcribe_job(wav_basename: str):
    """ワーカースレッド本体: <base>.wav を文字起こしして <base>.vtt を書く。"""
    base = os.path.basename(wav_basename)
    wav_path = os.path.join(RECORDINGS_DIR, base)
    vtt_path = _vtt_path_for(base)
    try:
        transcribe_wav_to_vtt(wav_path, vtt_path, ASRConfig())
        with _transcribe_lock:
            _transcribe_status[base] = "done"
        logger.info(f"[asr] 字幕生成完了: {base}")
    except Exception as e:
        with _transcribe_lock:
            _transcribe_status[base] = "error"
        logger.error(f"[asr] 字幕生成失敗 ({base}): {e}")


# ------------------------------
# ＜WAVストリーミングヘッダー＞
def make_infinite_wav_header(sample_rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    """ストリーミング用WAVヘッダー（データサイズ=0xFFFFFFF0で事実上無限）"""
    data_size = 0xFFFFFFF0
    buf = io.BytesIO()
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', 0xFFFFFFFF))
    buf.write(b'WAVE')
    buf.write(b'fmt ')
    buf.write(struct.pack('<I', 16))
    buf.write(struct.pack('<H', 1))           # PCM
    buf.write(struct.pack('<H', channels))
    buf.write(struct.pack('<I', sample_rate))
    buf.write(struct.pack('<I', sample_rate * channels * bits // 8))
    buf.write(struct.pack('<H', channels * bits // 8))
    buf.write(struct.pack('<H', bits))
    buf.write(b'data')
    buf.write(struct.pack('<I', data_size))
    return buf.getvalue()


def _needs_mp3(request: Request) -> bool:
    """SafariはWAVストリーミング非対応のためMP3トランスコードを使用"""
    ua = request.headers.get("user-agent", "").lower()
    return "safari" in ua and "chrome" not in ua and "chromium" not in ua and "firefox" not in ua


def _teardown_stream(station_id: str, recv_ref: list, fifo_path: str):
    """ストリーム固有リソースを解放する（identity ベースの単一クレーム）。

    recv_ref[0] を None に差し替えられた“最初の 1 人”だけが受信機停止と
    _sdr_lock 解放を担う。読み出しと差し替えを同じ _stream_lock 下で行うため、
    非 None の受信機を観測できるのは厳密に 1 スレッドのみ（=クレーマー）。
    レジストリ削除は recv_ref の identity 一致時のみ行うので、同一局の新しい
    ストリームを古い後始末が誤って破棄することはない。
    """
    with _stream_lock:
        entry = _active_streamers.get(station_id)
        # entry は (recv_ref, fifo_path) のタプル。recv_ref の identity が一致する
        # ときのみ取り除く（同一局の新しいストリームを古い後始末で壊さない）。
        if entry is not None and entry[0] is recv_ref:
            _active_streamers.pop(station_id, None)
        r = recv_ref[0]
        recv_ref[0] = None  # クレーム
    if r is not None:
        try:
            r.stop()
            r.wait()
        except Exception:
            pass
        del r
        gc.collect()  # 参照カウント → 0、USB 解放
        try:
            _sdr_lock.release()
        except RuntimeError:
            pass
    try:
        os.unlink(fifo_path)
    except Exception:
        pass


def _cleanup_stream(station_id: str, recv_ref: list, fifo_path: str):
    """ストリーム終了時の共通リソース解放（generate() の finally から呼ばれる）。"""
    _stop_asr()
    _teardown_stream(station_id, recv_ref, fifo_path)


# ------------------------------
# ＜録音処理＞
def record_radio(station_id, output_file, duration, title, start_time, program_detail):
    """Record a radio broadcast to *output_file* for *duration* seconds, updating global state."""
    station = get_station(station_id)
    if station is None:
        logger.warning(f"不明な局: {station_id}")
        return

    # ストリーミング中なら停止してから録音
    with _stream_lock:
        streamers = list(_active_streamers.items())
    _stop_asr()
    for sid, (recv_ref, fifo_path) in streamers:
        _teardown_stream(sid, recv_ref, fifo_path)
    del streamers

    with _state_lock:
        for scheduled_item in SCHEDULED_RECORDINGS[:]:
            if scheduled_item.get("output") == output_file:
                SCHEDULED_RECORDINGS.remove(scheduled_item)
                break
    update_global_state()

    in_progress_item = {
        "title": title,
        "station_id": station_id,
        "station_name": station["name"],
        "start_time": start_time,
        "duration": duration,
        "output": output_file,
        "program_detail": program_detail,
    }
    stop_event = threading.Event()
    _recording_events[output_file] = stop_event  # IN_PROGRESS 登録前に仕込む（キャンセル競合防止）
    with _state_lock:
        IN_PROGRESS_RECORDINGS.append(in_progress_item)
    update_global_state()

    logger.info(f"録音開始: {station['name']} - {output_file}")
    receiver = None
    recording_ok = False
    try:
        if station.get("type") == "am":
            receiver = AMReceiver(station["freq"], 2.4e6, output_file, 16000, 16, 40)
        else:
            receiver = FMReceiver(station["freq"], 2.4e6, output_file, 16000, 16, 40)
        receiver.start()
        cancelled = stop_event.wait(timeout=duration)  # True=中止ボタン, False=正常終了
        receiver.stop()
        receiver.wait()
        if cancelled:
            logger.info(f"録音中止: {output_file}")
        else:
            logger.info(f"録音終了: {output_file}")
            recording_ok = True
    except Exception as e:
        logger.error(f"録音エラー ({station['name']}): {e}")
        if receiver is not None:
            try:
                receiver.stop()
                receiver.wait()
            except Exception:
                pass
    finally:
        _recording_events.pop(output_file, None)
        with _state_lock:
            try:
                IN_PROGRESS_RECORDINGS.remove(in_progress_item)
            except ValueError:
                pass
            if recording_ok:
                COMPLETED_RECORDINGS.append(in_progress_item.copy())
        update_global_state()

    if not recording_ok:
        return

    json_file = output_file.rsplit('.', 1)[0] + ".json"
    recording_info = {
        "station_id": station_id,
        "station_name": station["name"],
        "title": title,
        "start_time": start_time,
        "duration": duration,
        "output_file": output_file,
        "program_details": program_detail,
    }
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(recording_info, f, ensure_ascii=False, indent=4, default=convert_datetime)
    logger.info(f"録音完了: {output_file}")


# ------------------------------
# ＜ページルート＞
@app.get("/")
def index(request: Request):
    """Render the dashboard index page with scheduled, in-progress, and completed recording counts."""
    return templates.TemplateResponse(request, "index.html", {
        "scheduled": SCHEDULED_RECORDINGS,
        "in_progress": IN_PROGRESS_RECORDINGS,
        "completed": COMPLETED_RECORDINGS,
    })


@app.get("/on-air")
def on_air(request: Request):
    """Render the on-air live-listening page, passing ASR availability to the template."""
    # 自動字幕トグルの初期状態をサーバレンダリング（チラつき防止／asr_core 不在時は非表示）。
    return templates.TemplateResponse(
        request,
        "on_air.html",
        {"asr_enabled": _ASR_ENABLED, "asr_available": _ASR_AVAILABLE},
    )


@app.get("/programs")
def programs(request: Request):
    """Render the programme guide page with yesterday/today/tomorrow schedules for all stations."""
    today = datetime.datetime.now(JST).date()
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)
    # 深夜0〜5時台は前日の放送日データに含まれるため yesterday も取得し、
    # 今日0時より前の番組（前日の昼間分）は除外する
    today_midnight = datetime.datetime.combine(today, datetime.time.min).replace(tzinfo=JST)
    program_table: dict = {}
    for date in [yesterday, today, tomorrow]:
        for station in STATIONS:
            for prog in _radiko_client.fetch_programs_cached(station["id"], date):
                if prog["start_time"] < today_midnight:
                    continue
                slot = prog["start_time"].strftime("%Y-%m-%d %H:%M")
                if slot not in program_table:
                    program_table[slot] = {}
                program_table[slot][station["id"]] = prog
    reserved_map = {}
    for r in SCHEDULED_RECORDINGS:
        start_str = (
            r["start_time"]
            if isinstance(r["start_time"], str)
            else r["start_time"].strftime("%Y-%m-%d %H:%M:%S")
        )
        reserved_map[(r["station_id"], start_str)] = True
    WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]
    date_labels = {
        today.strftime("%Y-%m-%d"): f"今日 ({today.month}/{today.day}・{WEEKDAYS_JA[today.weekday()]})",
        tomorrow.strftime("%Y-%m-%d"): f"明日 ({tomorrow.month}/{tomorrow.day}・{WEEKDAYS_JA[tomorrow.weekday()]})",
    }
    return templates.TemplateResponse(request, "programs.html", {
        "stations": STATIONS,
        "program_table": program_table,
        "reservations": reserved_map,
        "date_labels": date_labels,
    })


@app.get("/recording")
def recording(request: Request):
    """Render the currently-in-progress recordings page."""
    return templates.TemplateResponse(request, "recording.html", {
        "in_progress": IN_PROGRESS_RECORDINGS,
    })


@app.get("/recorded")
@app.get("/recordings")
def recorded(request: Request):
    """Render the completed recordings page, including subtitle availability for each file."""
    files = [f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".wav")]
    # 完了録音／ファイルのみ、両方のカードで字幕の有無を判定できるようにする
    vtt_map = {f: _has_vtt(f) for f in files}
    for item in COMPLETED_RECORDINGS:
        fname = os.path.basename(item.get("output", ""))
        if fname:
            vtt_map[fname] = _has_vtt(fname)
    return templates.TemplateResponse(request, "recorded.html", {
        "files": files,
        "completed": COMPLETED_RECORDINGS,
        "vtt_map": vtt_map,
        "asr_batch_available": _ASR_BATCH_AVAILABLE,
    })


@app.get("/reservations")
def reservations(request: Request):
    """Render the scheduled reservations page."""
    return templates.TemplateResponse(request, "reservations.html", {
        "scheduled": SCHEDULED_RECORDINGS,
    })


@app.get('/recordings/{filename:path}')
def serve_recordings(filename: str):
    """Serve a recording file (WAV, JSON, or VTT) from the recordings directory."""
    return FileResponse(os.path.join(RECORDINGS_DIR, filename))


# ------------------------------
# ＜予約処理＞
@app.post("/schedule")
def schedule_recording(
    station_id: str = Form(...),
    program_id: str = Form(...),
    start_time: str = Form(...),
    duration: int = Form(...),
    title: str = Form(None),
):
    """Accept a recording reservation form submission, persist it, and schedule a timer thread."""
    programs_list = _radiko_client.fetch_programs_as_dicts(station_id)
    program_detail = next((p for p in programs_list if p.get("id") == program_id), None)
    resolved_title = (program_detail["title"] if program_detail else None) or title or program_id
    safe_title = sanitize_filename(resolved_title)
    safe_start = start_time.replace(" ", "_").replace(":", "-")
    output_file = os.path.join(RECORDINGS_DIR, f"{safe_start}_{station_id}_{safe_title}.wav")

    station = get_station(station_id)
    new_item = {
        "title": resolved_title,
        "station_id": station_id,
        "station": station["name"] if station else "不明な局",
        "start_time": start_time,
        "duration": duration,
        "output": output_file,
        "program_id": program_id,
    }
    with _state_lock:
        SCHEDULED_RECORDINGS.append(new_item)
    update_global_state()

    start_dt = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    delay = max(0.0, (start_dt - datetime.datetime.now(JST)).total_seconds())
    logger.info(f"予約確定: {resolved_title} ({start_dt})")
    threading.Timer(
        delay, record_radio,
        args=(station_id, output_file, duration, resolved_title, start_time, program_detail)
    ).start()
    return RedirectResponse(url="/programs", status_code=302)


# ------------------------------
# ＜JSON API＞
@app.get("/api/programs")
def api_programs(station_id: str | None = None, date: str | None = None):
    """Return cached programme list for the given station(s) and date.

    date accepts YYYY-MM-DD or YYYYMMDD; defaults to today.
    station_id filters to a single station; omit to return all stations.
    """
    if date:
        date_str = date.replace("-", "")
        try:
            target_date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            return JSONResponse(content={"error": "date は YYYY-MM-DD または YYYYMMDD 形式で指定してください"}, status_code=400)
    else:
        target_date = datetime.datetime.now(JST).date()

    stations = [s for s in STATIONS if station_id is None or s["id"] == station_id]
    result = []
    for station in stations:
        for prog in _radiko_client.fetch_programs_cached(station["id"], target_date):
            result.append({
                "id": prog["id"],
                "station_id": station["id"],
                "title": prog["title"],
                "start_time": prog["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": prog["end_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "duration": prog["duration"],
                "ft": prog.get("ft"),
                "to": prog.get("to"),
                "info": prog.get("info"),
                "pfm": prog.get("pfm"),
            })
    return result


@app.get("/api/dashboard")
def api_dashboard():
    """Return a summary count of scheduled, in-progress, and completed recordings."""
    return {
        "scheduled": len(SCHEDULED_RECORDINGS),
        "in_progress": len(IN_PROGRESS_RECORDINGS),
        "completed": len(COMPLETED_RECORDINGS),
    }


@app.get("/api/on-air")
def api_on_air(refresh: int = 0):
    """Return current on-air programme info for every station, fetching in parallel threads."""
    now = datetime.datetime.now(JST)
    # refresh=1（「更新」ボタン）時は番組表キャッシュを無視して強制再取得し、
    # オフライン中にキャッシュされた空結果を最新の番組情報で置き換える。
    force = bool(refresh)
    results = []
    lock = threading.Lock()

    def fetch_station(station):
        """Fetch the currently airing programme for one station and append it to results."""
        # STATIONS を基準に必ず 1 局 1 エントリを返す。Radiko 番組表が取得できた局には
        # 現在放送中の番組を載せ、取れない局（オフライン／番組の谷間）は placeholder を
        # 返す。これにより Radiko API 不通でも全局の局名＋再生ボタンが表示される。
        entry = None
        try:
            # 深夜0〜4時台は前日の放送日データに番組が含まれるため両日検索する
            dates_to_check = [now.date()]
            if now.hour < 5:
                dates_to_check.append(now.date() - datetime.timedelta(days=1))
            for date in dates_to_check:
                for prog in _radiko_client.fetch_programs_cached(station["id"], date, force=force):
                    if prog["start_time"] <= now < prog["end_time"]:
                        elapsed = (now - prog["start_time"]).total_seconds()
                        progress = int(elapsed / prog["duration"] * 100) if prog["duration"] else 0
                        entry = {
                            "station_id": station["id"],
                            "station_name": station["name"],
                            "title": prog["title"],
                            "start_time": prog["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
                            "end_time": prog["end_time"].strftime("%Y-%m-%d %H:%M:%S"),
                            "duration": prog["duration"],
                            "elapsed": int(elapsed),
                            "progress": progress,
                            "info": prog.get("info"),
                            "pfm": prog.get("pfm"),
                            "has_program": True,
                        }
                        break
                if entry is not None:
                    break
        except Exception:
            entry = None

        if entry is None:
            entry = {
                "station_id": station["id"],
                "station_name": station["name"],
                "title": "番組情報なし",
                "start_time": None,
                "end_time": None,
                "duration": 0,
                "elapsed": 0,
                "progress": 0,
                "info": None,
                "pfm": None,
                "has_program": False,
            }

        entry["is_recording"] = any(
            r["station_id"] == station["id"] for r in IN_PROGRESS_RECORDINGS
        )
        with lock:
            results.append(entry)

    threads = [threading.Thread(target=fetch_station, args=(s,)) for s in STATIONS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    station_order = {s["id"]: i for i, s in enumerate(STATIONS)}
    results.sort(key=lambda x: station_order.get(x["station_id"], 999))
    return results


@app.get("/api/recording")
def api_recording():
    """Return elapsed/remaining time details for all currently in-progress recordings."""
    now = datetime.datetime.now(JST)
    items = []
    for item in IN_PROGRESS_RECORDINGS:
        start = item["start_time"]
        if isinstance(start, str):
            start = datetime.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        end = start + datetime.timedelta(seconds=item["duration"])
        elapsed = max(0, (now - start).total_seconds())
        remaining = max(0, item["duration"] - elapsed)
        items.append({
            "id": item.get("program_id", ""),
            "title": item["title"],
            "station_id": item["station_id"],
            "station_name": item.get("station_name", ""),
            "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": item["duration"],
            "remaining": int(remaining),
        })
    return items


@app.delete("/api/recording/{idx}")
def api_cancel_recording(idx: int):
    """Signal the recording at index *idx* to stop early by setting its stop event."""
    with _state_lock:
        if idx < 0 or idx >= len(IN_PROGRESS_RECORDINGS):
            return JSONResponse(content={"error": "不正なインデックス"}, status_code=400)
        item = IN_PROGRESS_RECORDINGS[idx]
    event = _recording_events.get(item.get("output", ""))
    if event is None:
        return JSONResponse(content={"error": "録音が停止できませんでした"}, status_code=409)
    event.set()
    return {"cancelled": True, "title": item["title"]}


@app.get("/api/recorded")
def api_recorded():
    """Return metadata for all completed recordings, including file existence on disk."""
    items = []
    for item in COMPLETED_RECORDINGS:
        start = item["start_time"]
        if isinstance(start, datetime.datetime):
            start = start.strftime("%Y-%m-%d %H:%M:%S")
        output = item.get("output", "")
        items.append({
            "id": item.get("program_id", ""),
            "title": item["title"],
            "station_id": item["station_id"],
            "station_name": item.get("station_name", ""),
            "start_time": start,
            "duration": item["duration"],
            "filename": os.path.basename(output) if output else "",
            "file_exists": os.path.exists(output) if output else False,
        })
    return items


@app.get("/api/reservations")
def api_reservations():
    """Return all scheduled recording reservations as a JSON list."""
    items = []
    for item in SCHEDULED_RECORDINGS:
        start = item["start_time"]
        if isinstance(start, datetime.datetime):
            start = start.strftime("%Y-%m-%d %H:%M:%S")
        items.append({
            "id": item.get("program_id", ""),
            "title": item["title"],
            "station_id": item["station_id"],
            "station": item.get("station", ""),
            "start_time": start,
            "duration": item["duration"],
        })
    return items


def _delete_recording_files(output: str):
    """Delete the WAV file at *output* and its companion JSON sidecar, silently ignoring errors."""
    if output and os.path.exists(output):
        try:
            os.remove(output)
        except Exception:
            pass
    json_file = output.rsplit('.', 1)[0] + ".json" if output else ""
    if json_file and os.path.exists(json_file):
        try:
            os.remove(json_file)
        except Exception:
            pass


@app.delete("/api/recorded/{idx}")
def api_delete_recorded(idx: int):
    """Remove the completed recording at *idx* from state and delete its files from disk."""
    with _state_lock:
        if idx < 0 or idx >= len(COMPLETED_RECORDINGS):
            return JSONResponse(content={"error": "不正なインデックス"}, status_code=400)
        removed = COMPLETED_RECORDINGS.pop(idx)
    update_global_state()
    _delete_recording_files(removed.get("output", ""))
    return {"deleted": str(removed["title"])}


@app.delete("/api/recorded/files/{filename:path}")
def api_delete_recorded_file(filename: str):
    """Delete a recording file by filename (WAV + JSON + VTT sidecars) from the recordings dir."""
    filepath = os.path.join(RECORDINGS_DIR, os.path.basename(filename))
    _delete_recording_files(filepath)
    # 字幕サイドカーも一緒に削除し、状態もクリアする
    base = os.path.basename(filename)
    vtt = _vtt_path_for(base)
    if os.path.exists(vtt):
        try:
            os.remove(vtt)
        except Exception:
            pass
    with _transcribe_lock:
        _transcribe_status.pop(base, None)
    return {"deleted": filename}


@app.post("/api/transcribe/{filename:path}")
def api_transcribe_start(filename: str):
    """録音ファイルの字幕生成（WebVTT）を非同期で開始する。"""
    base = os.path.basename(filename)
    wav_path = os.path.join(RECORDINGS_DIR, base)
    if not base.endswith(".wav") or not os.path.exists(wav_path):
        return JSONResponse(content={"error": "ファイルが見つかりません"}, status_code=404)
    if not _ASR_BATCH_AVAILABLE:
        return JSONResponse(
            content={"error": "字幕生成は利用できません（parakeet-cli / モデル未設定）"},
            status_code=503,
        )
    if _has_vtt(base):
        return {"status": "done"}
    with _transcribe_lock:
        if _transcribe_status.get(base) == "running":
            return {"status": "running"}
        _transcribe_status[base] = "running"
    _transcribe_executor.submit(_run_transcribe_job, base)
    return {"status": "running"}


@app.get("/api/transcribe/{filename:path}")
def api_transcribe_status(filename: str):
    """字幕生成の状態を返す（idle / running / done / error）。"""
    base = os.path.basename(filename)
    return {
        "filename": base,
        "status": _transcribe_state(base),
        "has_vtt": _has_vtt(base),
        "available": _ASR_BATCH_AVAILABLE,
    }


@app.delete("/api/reservations/{idx}")
def api_delete_reservation(idx: int):
    """Remove the scheduled reservation at *idx* from the list and persist state."""
    with _state_lock:
        if idx < 0 or idx >= len(SCHEDULED_RECORDINGS):
            return JSONResponse(content={"error": "不正なインデックス"}, status_code=400)
        removed = SCHEDULED_RECORDINGS.pop(idx)
    update_global_state()
    title = removed["title"] if isinstance(removed["title"], str) else str(removed["title"])
    return {"deleted": title}


# ------------------------------
# ＜ストリーミング＞
@app.get("/api/stream-status/{station_id}")
def api_stream_status(station_id: str):
    """Return whether live or timeshift streaming is available for *station_id*."""
    station = get_station(station_id)
    if station is None:
        return JSONResponse(content={"available": False, "reason": "局が見つかりません"}, status_code=404)

    in_progress = next((r for r in IN_PROGRESS_RECORDINGS if r["station_id"] == station_id), None)
    if in_progress:
        return {"available": True, "mode": "timeshift"}

    if IN_PROGRESS_RECORDINGS:
        rec = IN_PROGRESS_RECORDINGS[0]
        station_name = rec.get("station_name", rec["station_id"])
        return {"available": False, "reason": f"「{station_name}」を録音中のため配信できません"}

    with _stream_lock:
        active_sids = list(_active_streamers.keys())
    if active_sids and station_id not in active_sids:
        active_sid = active_sids[0]
        st = get_station(active_sid)
        name = st["name"] if st else active_sid
        return {"available": False, "reason": f"「{name}」をストリーミング中のため配信できません"}

    return {"available": True, "mode": "live"}


@app.get("/api/transcript/{station_id}")
def api_transcript(station_id: str, since: int = 0):
    """指定局の自動字幕（認識済みセグメント）を since 以降だけ返す。"""
    with _asr_lock:
        if _asr_station != station_id:
            return {
                "station_id": station_id,
                "available": _ASR_ENABLED and _ASR_AVAILABLE,
                "enabled": _ASR_ENABLED,
                "active": False,
                "segments": [],
                "cursor": 0,
            }
        segments = _asr_transcript[since:]
        return {
            "station_id": station_id,
            "available": _asr_session is not None,
            "enabled": _ASR_ENABLED,
            "active": True,
            "segments": segments,
            "cursor": len(_asr_transcript),
        }


@app.get("/api/asr")
def api_asr_state():
    """自動字幕（音声認識）の有効状態を返す。"""
    return {"enabled": _ASR_ENABLED, "available": _ASR_AVAILABLE}


@app.post("/api/asr")
async def api_asr_set(request: Request):
    """自動字幕（音声認識）の ON/OFF を実行時に切り替える。

    OFF にしたら再生中セッションを即停止。ON にしたら、再生中なら現用局で即開始する
    （次の再生時は既存フローで自動起動するため、再生していなければ状態だけ更新）。
    """
    global _ASR_ENABLED
    try:
        body = await request.json()
    except Exception:
        body = {}
    _ASR_ENABLED = bool(body.get("enabled"))
    if not _ASR_ENABLED:
        _stop_asr()
    elif _ASR_AVAILABLE:
        # 現用局を優先（単一 SDR なので _active_streamers の先頭）。無ければ要求の station_id。
        with _stream_lock:
            active = next(iter(_active_streamers), None)
        target = active or body.get("station_id")
        if target:
            _start_asr(target)
    return {"enabled": _ASR_ENABLED, "available": _ASR_AVAILABLE}


@app.post("/api/stop-stream")
def api_stop_stream():
    """Stop all active SDR streams and wait for the SDR lock to be released before responding."""
    # スナップショットを取り（pre-clear しない）、各エントリを identity ベースの
    # _teardown_stream に委ねる。受信機の停止と _sdr_lock 解放はクレームした
    # 1 人だけが行うため、接続クローズ起因の _cleanup_stream と競合しても
    # 二重解放や別ストリームのロック誤解放が起きない。停止は受信機を確実に
    # 止め、戻る前にロックを解放するので、直後の再生がクリーンに取得できる。
    with _stream_lock:
        streamers = list(_active_streamers.items())
    _stop_asr()
    for sid, (recv_ref, fifo_path) in streamers:
        _teardown_stream(sid, recv_ref, fifo_path)
    del streamers
    # 受信機停止（r.stop()/r.wait() は時間がかかる）は、本ハンドラと接続クローズ
    # 起因の _cleanup_stream のうち“クレームした側”が担う。本ハンドラがクレームを
    # 取れなかった場合、停止は別スレッドでまだ進行中で _sdr_lock も未解放のことが
    # ある。その状態で 200 を返すと、クライアントが直後に投げる別局の
    # GET /stream が 3 秒以内にロックを取れず 409 になる。SDR が実際に解放される
    # （= _sdr_lock を取得できる）まで待ってから応答することでこれを防ぐ。
    if _sdr_lock.acquire(timeout=10.0):
        _sdr_lock.release()
    return {"stopped": True}


def stream_from_recording(
    wav_path: str,
    station_id: str,
    use_mp3: bool = False,
    enable_asr: bool = True,
):
    """録音中のWAVファイルを先頭から読んでタイムシフトストリーミング配信する。"""
    WAV_HEADER_SIZE = 44
    if enable_asr:
        _start_asr(station_id)

    if use_mp3:
        def generate():
            """Run ffmpeg to transcode timeshift WAV PCM to MP3 and yield output chunks."""
            ffmpeg_proc = None
            try:
                ffmpeg_proc = subprocess.Popen(
                    [
                        "ffmpeg", "-y", "-loglevel", "quiet",
                        "-f", "s16le", "-ar", "16000", "-ac", "1",
                        "-i", "pipe:0", "-f", "mp3", "-q:a", "5", "pipe:1",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                def forward_pcm():
                    """Tail the growing WAV file and pipe PCM into ffmpeg's stdin."""
                    try:
                        with open(wav_path, "rb") as f:
                            f.seek(WAV_HEADER_SIZE)
                            while True:
                                chunk = f.read(4096)
                                if chunk:
                                    if enable_asr:
                                        _on_pcm(chunk)
                                    ffmpeg_proc.stdin.write(chunk)
                                elif any(r["output"] == wav_path for r in IN_PROGRESS_RECORDINGS):
                                    time.sleep(0.05)
                                else:
                                    break
                    except Exception:
                        pass
                    finally:
                        try:
                            ffmpeg_proc.stdin.close()
                        except Exception:
                            pass

                threading.Thread(target=forward_pcm, daemon=True).start()

                while True:
                    chunk = ffmpeg_proc.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if ffmpeg_proc:
                    try:
                        ffmpeg_proc.terminate()
                        ffmpeg_proc.wait(timeout=2)
                    except Exception:
                        pass
                if enable_asr:
                    _stop_asr()

        return StreamingResponse(
            generate(),
            media_type="audio/mpeg",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    def generate():
        """Stream the timeshift WAV as MP3 via stream_growing_wav_as_mp3."""
        try:
            yield from stream_growing_wav_as_mp3(
                wav_path,
                is_still_recording=lambda: any(
                    r["output"] == wav_path for r in IN_PROGRESS_RECORDINGS
                ),
                on_pcm=_on_pcm if enable_asr else None,
            )
        finally:
            if enable_asr:
                _stop_asr()

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/stream/{station_id}")
def stream_audio(station_id: str, request: Request, asr: int = 1):
    """Start an SDR live stream for *station_id*, or timeshift if a recording is in progress."""
    station = get_station(station_id)
    if station is None:
        return JSONResponse(content={"error": "局が見つかりません"}, status_code=404)
    use_mp3 = _needs_mp3(request)
    enable_asr = asr != 0

    in_progress = next((r for r in IN_PROGRESS_RECORDINGS if r["station_id"] == station_id), None)
    if in_progress:
        return stream_from_recording(
            in_progress["output"], station_id, use_mp3=use_mp3, enable_asr=enable_asr
        )

    if IN_PROGRESS_RECORDINGS:
        return JSONResponse(content={"error": "他の局を録音中のため配信できません"}, status_code=409)

    # 前のストリームが完全停止するまで最大3秒待機してSDRデバイスを排他取得
    if not _sdr_lock.acquire(blocking=True, timeout=3.0):
        return JSONResponse(content={"error": "SDRデバイスは使用中です。しばらく待ってから再試行してください"}, status_code=409)

    try:
        fifo_path = f"/tmp/radio_stream_{station_id}.pcm"
        if os.path.exists(fifo_path):
            os.unlink(fifo_path)
        os.mkfifo(fifo_path)

        rfd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        flags = fcntl.fcntl(rfd, fcntl.F_GETFL)
        fcntl.fcntl(rfd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

        # recv_ref = [receiver] とすることで、api_stop_stream() と generate() の
        # 両方から参照を明示的に None に落とせる。CPython の参照カウントにより、
        # 最後の参照が None になった瞬間にデストラクタが実行され USB が解放される。
        recv_ref = [None]
        try:
            if station.get("type") == "am":
                recv_ref[0] = StreamingAMReceiver(station["freq"], 2.4e6, fifo_path, desired_audio_rate=16000, gain=40)
            else:
                recv_ref[0] = StreamingFMReceiver(station["freq"], 2.4e6, fifo_path, desired_audio_rate=16000, gain=40)
        except Exception as e:
            os.close(rfd)
            os.unlink(fifo_path)
            _sdr_lock.release()
            return JSONResponse(content={"error": f"SDR初期化エラー: {e}"}, status_code=500)

        with _stream_lock:
            _active_streamers[station_id] = (recv_ref, fifo_path)

        recv_ref[0].start()
        if enable_asr:
            _start_asr(station_id)

    except Exception:
        _sdr_lock.release()
        raise

    if use_mp3:
        def generate():
            """Run ffmpeg to transcode live FIFO PCM to MP3 and yield output chunks."""
            ffmpeg_proc = None
            try:
                ffmpeg_proc = subprocess.Popen(
                    [
                        "ffmpeg", "-y", "-loglevel", "quiet",
                        "-f", "s16le", "-ar", "16000", "-ac", "1",
                        "-i", "pipe:0", "-f", "mp3", "-q:a", "5", "pipe:1",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                def forward_pcm():
                    """Read PCM from the SDR FIFO and pipe it into ffmpeg's stdin."""
                    try:
                        # GNU Radio 起動待ちの間、2秒分の無音PCMを先行送信
                        ffmpeg_proc.stdin.write(bytes(16000 * 2 * 2))
                        with os.fdopen(rfd, "rb") as f:
                            while True:
                                chunk = f.read(4096)
                                if not chunk:
                                    break
                                if enable_asr:
                                    _on_pcm(chunk)
                                ffmpeg_proc.stdin.write(chunk)
                    except Exception:
                        pass
                    finally:
                        try:
                            ffmpeg_proc.stdin.close()
                        except Exception:
                            pass

                threading.Thread(target=forward_pcm, daemon=True).start()

                while True:
                    chunk = ffmpeg_proc.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if ffmpeg_proc:
                    try:
                        ffmpeg_proc.terminate()
                        ffmpeg_proc.wait(timeout=2)
                    except Exception:
                        pass
                _cleanup_stream(station_id, recv_ref, fifo_path)

        return StreamingResponse(
            generate(),
            media_type="audio/mpeg",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    def generate():
        """Stream live SDR audio as MP3 via stream_fd_as_mp3 and clean up on exit."""
        try:
            yield from stream_fd_as_mp3(rfd, on_pcm=_on_pcm if enable_asr else None)
        finally:
            _cleanup_stream(station_id, recv_ref, fifo_path)

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ------------------------------
# ＜起動時の再スケジュール＞
def reschedule_pending():
    """Re-arm timer threads for future reservations surviving a server restart; drop stale entries."""
    # 前プロセスで中途終了した録音はイベント/スレッドが存在しないため削除する
    if IN_PROGRESS_RECORDINGS:
        with _state_lock:
            IN_PROGRESS_RECORDINGS.clear()
        update_global_state()

    now = datetime.datetime.now(JST)
    for rec in SCHEDULED_RECORDINGS[:]:
        start_dt = (
            rec["start_time"]
            if isinstance(rec["start_time"], datetime.datetime)
            else datetime.datetime.strptime(rec["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        )
        if start_dt > now:
            start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            programs_list = _radiko_client.fetch_programs_as_dicts(rec["station_id"])
            program_detail = next(
                (p for p in programs_list if p.get("id") == rec.get("program_id")), None
            )
            delay = (start_dt - now).total_seconds()
            threading.Timer(
                delay, record_radio,
                args=(rec["station_id"], rec["output"], rec["duration"], rec["title"], start_str, program_detail)
            ).start()
        else:
            with _state_lock:
                SCHEDULED_RECORDINGS.remove(rec)


# ------------------------------
def _select_webui_port(default_port: int = 5000) -> int:
    """Return a free port starting at *default_port*, honouring WEBUI_PORT / PORT env vars."""
    env_port = os.environ.get("WEBUI_PORT") or os.environ.get("PORT")
    if env_port:
        return int(env_port)
    for port in range(default_port, default_port + 11):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue
            if port != default_port:
                logger.info(f"port {default_port} is in use; using {port} instead")
            return port
    return default_port


# ------------------------------
def main() -> None:
    """console script エントリ（radio-webui）。"""
    reschedule_pending()
    uvicorn.run(app, host="0.0.0.0", port=_select_webui_port())


if __name__ == "__main__":
    main()
