#!/usr/bin/env python3
import os
import gc
import json
import datetime
import threading
import struct
import io
import fcntl
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

try:
    from radio_core.receivers import FMReceiver, AMReceiver, StreamingFMReceiver, StreamingAMReceiver
except ImportError as e:
    print(f"Error: GNU Radio or osmosdr library is not installed: {e}")
    exit(1)

from radio_core.stations import STATIONS, get_station
from radio_core.radiko import RadikoClient
from radio_core.utils import sanitize_filename, JST, convert_datetime
from radio_core.transcoder import stream_fd_as_mp3, stream_growing_wav_as_mp3

try:
    from asr_core import (
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
    print(f"asr_core unavailable, transcription disabled: {e}")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters['basename'] = os.path.basename

# ------------------------------
# ＜状態管理＞
STATE_FILE = "/app/data/state.json"
RECORDINGS_DIR = "./recordings"
if not os.path.exists(RECORDINGS_DIR):
    os.makedirs(RECORDINGS_DIR)

_state_lock = threading.Lock()
_radiko_client = RadikoClient()


def load_state():
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4, default=convert_datetime)


state_data = load_state()
SCHEDULED_RECORDINGS = state_data.get("scheduled", [])
IN_PROGRESS_RECORDINGS = state_data.get("in_progress", [])
COMPLETED_RECORDINGS = state_data.get("completed", [])


def update_global_state():
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
        print(f"[asr] start failed ({station_id}): {e}")
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
        print(f"[asr] 字幕生成完了: {base}")
    except Exception as e:
        with _transcribe_lock:
            _transcribe_status[base] = "error"
        print(f"[asr] 字幕生成失敗 ({base}): {e}")


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


# ------------------------------
# ＜録音処理＞
def record_radio(station_id, output_file, duration, title, start_time, program_detail):
    station = get_station(station_id)
    if station is None:
        print("不明な局:", station_id)
        return

    # ストリーミング中なら停止してから録音
    with _stream_lock:
        for sid, (recv_ref, fifo_path) in list(_active_streamers.items()):
            try:
                r = recv_ref[0]
                recv_ref[0] = None
                if r:
                    r.stop()
                    r.wait()
                    del r
            except Exception:
                pass
            try:
                os.unlink(fifo_path)
            except Exception:
                pass
        _active_streamers.clear()
    _stop_asr()
    gc.collect()

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
    with _state_lock:
        IN_PROGRESS_RECORDINGS.append(in_progress_item)
    update_global_state()

    print(f"[{datetime.datetime.now(JST)}] 録音開始: {station['name']} - {output_file}")
    if station.get("type") == "am":
        receiver = AMReceiver(station["freq"], 2.4e6, output_file, 16000, 16, 40)
    else:
        receiver = FMReceiver(station["freq"], 2.4e6, output_file, 16000, 16, 40)
    receiver.start()
    time.sleep(duration)
    receiver.stop()
    receiver.wait()
    print(f"[{datetime.datetime.now(JST)}] 録音終了: {output_file}")

    with _state_lock:
        try:
            IN_PROGRESS_RECORDINGS.remove(in_progress_item)
        except ValueError:
            pass
        completed_item = in_progress_item.copy()
        COMPLETED_RECORDINGS.append(completed_item)
    update_global_state()

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
    print(f"録音完了: {output_file}")


# ------------------------------
# ＜ページルート＞
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "scheduled": SCHEDULED_RECORDINGS,
        "in_progress": IN_PROGRESS_RECORDINGS,
        "completed": COMPLETED_RECORDINGS,
    })


@app.get("/on-air")
def on_air(request: Request):
    return templates.TemplateResponse(request, "on_air.html")


@app.get("/programs")
def programs(request: Request):
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
    return templates.TemplateResponse(request, "recording.html", {
        "in_progress": IN_PROGRESS_RECORDINGS,
    })


@app.get("/recorded")
@app.get("/recordings")
def recorded(request: Request):
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
    return templates.TemplateResponse(request, "reservations.html", {
        "scheduled": SCHEDULED_RECORDINGS,
    })


@app.get('/recordings/{filename:path}')
def serve_recordings(filename: str):
    return FileResponse(os.path.join(RECORDINGS_DIR, filename))


# ------------------------------
# ＜予約処理＞
@app.post("/schedule")
def schedule_recording(
    station_id: str = Form(...),
    title: str = Form(...),
    start_time: str = Form(...),
    duration: int = Form(...),
    program_id: str = Form(None),
):
    safe_title = sanitize_filename(title)
    safe_start = start_time.replace(" ", "_").replace(":", "-")
    output_file = os.path.join(RECORDINGS_DIR, f"{safe_start}_{station_id}_{safe_title}.wav")

    station = get_station(station_id)
    new_item = {
        "title": title,
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

    programs_list = _radiko_client.fetch_programs_as_dicts(station_id)
    program_detail = next((p for p in programs_list if p.get("id") == program_id), None)
    start_dt = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    delay = max(0.0, (start_dt - datetime.datetime.now(JST)).total_seconds())
    print(f"[{start_dt}] book: {title}")
    threading.Timer(
        delay, record_radio,
        args=(station_id, output_file, duration, title, start_time, program_detail)
    ).start()
    return RedirectResponse(url="/programs", status_code=302)


# ------------------------------
# ＜JSON API＞
@app.get("/api/dashboard")
def api_dashboard():
    return {
        "scheduled": len(SCHEDULED_RECORDINGS),
        "in_progress": len(IN_PROGRESS_RECORDINGS),
        "completed": len(COMPLETED_RECORDINGS),
    }


@app.get("/api/on-air")
def api_on_air():
    now = datetime.datetime.now(JST)
    results = []
    lock = threading.Lock()

    def fetch_station(station):
        try:
            # 深夜0〜4時台は前日の放送日データに番組が含まれるため両日検索する
            dates_to_check = [now.date()]
            if now.hour < 5:
                dates_to_check.append(now.date() - datetime.timedelta(days=1))
            for date in dates_to_check:
                for prog in _radiko_client.fetch_programs_cached(station["id"], date):
                    if prog["start_time"] <= now < prog["end_time"]:
                        elapsed = (now - prog["start_time"]).total_seconds()
                        progress = int(elapsed / prog["duration"] * 100) if prog["duration"] else 0
                        with lock:
                            results.append({
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
                                "is_recording": any(
                                    r["station_id"] == station["id"] for r in IN_PROGRESS_RECORDINGS
                                ),
                            })
                        return
        except Exception:
            pass

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
            "title": item["title"],
            "station_id": item["station_id"],
            "station_name": item.get("station_name", ""),
            "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": item["duration"],
            "remaining": int(remaining),
        })
    return items


@app.get("/api/recorded")
def api_recorded():
    items = []
    for item in COMPLETED_RECORDINGS:
        start = item["start_time"]
        if isinstance(start, datetime.datetime):
            start = start.strftime("%Y-%m-%d %H:%M:%S")
        output = item.get("output", "")
        items.append({
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
    items = []
    for item in SCHEDULED_RECORDINGS:
        start = item["start_time"]
        if isinstance(start, datetime.datetime):
            start = start.strftime("%Y-%m-%d %H:%M:%S")
        items.append({
            "title": item["title"],
            "station_id": item["station_id"],
            "station": item.get("station", ""),
            "start_time": start,
            "duration": item["duration"],
            "program_id": item.get("program_id", ""),
        })
    return items


def _delete_recording_files(output: str):
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
    with _state_lock:
        if idx < 0 or idx >= len(COMPLETED_RECORDINGS):
            return JSONResponse(content={"error": "不正なインデックス"}, status_code=400)
        removed = COMPLETED_RECORDINGS.pop(idx)
    update_global_state()
    _delete_recording_files(removed.get("output", ""))
    return {"deleted": str(removed["title"])}


@app.delete("/api/recorded/files/{filename:path}")
def api_delete_recorded_file(filename: str):
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
                "active": False,
                "segments": [],
                "cursor": 0,
            }
        segments = _asr_transcript[since:]
        return {
            "station_id": station_id,
            "available": _asr_session is not None,
            "active": True,
            "segments": segments,
            "cursor": len(_asr_transcript),
        }


@app.post("/api/stop-stream")
def api_stop_stream():
    with _stream_lock:
        streamers = list(_active_streamers.items())
        _active_streamers.clear()
    for _sid, (recv_ref, fifo_path) in streamers:
        # recv_ref[0] を None にしてから del することで、generate() クロージャ側の
        # 参照と合わせてレシーバオブジェクトの参照カウントを 0 に落とす。
        # CPython では参照カウントが 0 になった時点でデストラクタが即実行され、
        # rtlsdr_close() が呼ばれて USB デバイスが解放される。
        r = recv_ref[0]
        recv_ref[0] = None
        if r:
            try:
                r.stop()
                r.wait()
            except Exception:
                pass
            del r  # 参照カウント → 0、USB 解放
        try:
            os.unlink(fifo_path)
        except Exception:
            pass
    del streamers  # recv_ref コンテナへの参照を解放
    gc.collect()   # 念のため強制 GC（PyPy 等の非参照カウント実装向け）
    _stop_asr()
    try:
        _sdr_lock.release()
    except RuntimeError:
        pass  # generate() の finally が先に解放した場合
    return {"stopped": True}


def stream_from_recording(wav_path: str, station_id: str):
    """録音中のWAVファイルを先頭から読んでタイムシフトストリーミング配信（MP3）"""
    _start_asr(station_id)

    def generate():
        try:
            yield from stream_growing_wav_as_mp3(
                wav_path,
                is_still_recording=lambda: any(
                    r["output"] == wav_path for r in IN_PROGRESS_RECORDINGS
                ),
                on_pcm=_on_pcm,
            )
        finally:
            _stop_asr()

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/stream/{station_id}")
def stream_audio(station_id: str):
    station = get_station(station_id)
    if station is None:
        return JSONResponse(content={"error": "局が見つかりません"}, status_code=404)

    in_progress = next((r for r in IN_PROGRESS_RECORDINGS if r["station_id"] == station_id), None)
    if in_progress:
        return stream_from_recording(in_progress["output"], station_id)

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
        _start_asr(station_id)

    except Exception:
        _sdr_lock.release()
        raise

    def generate():
        try:
            yield from stream_fd_as_mp3(rfd, on_pcm=_on_pcm)
        finally:
            _stop_asr()
            with _stream_lock:
                entry = _active_streamers.pop(station_id, None)

            # 必ず参照を None に落として USB 解放を確実にする
            r = recv_ref[0]
            recv_ref[0] = None

            if entry is not None:
                # api_stop_stream() がまだ呼ばれていない（タブ閉じ等）
                # → 自分で停止・USB 解放・ロック解放
                if r:
                    try:
                        r.stop()
                        r.wait()
                    except Exception:
                        pass
                del r  # 参照カウント → 0、デストラクタ実行、USB 解放
                try:
                    _sdr_lock.release()
                except RuntimeError:
                    pass
            else:
                # api_stop_stream() が先に停止・USB 解放・ロック解放済み
                del r
            try:
                os.unlink(fifo_path)
            except Exception:
                pass

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ------------------------------
# ＜起動時の再スケジュール＞
def reschedule_pending():
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
if __name__ == "__main__":
    reschedule_pending()
    uvicorn.run(app, host="0.0.0.0", port=5000)
