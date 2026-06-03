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


def _cleanup_stream(station_id: str, recv_ref: list, fifo_path: str):
    """ストリーム終了時の共通リソース解放"""
    with _stream_lock:
        entry = _active_streamers.pop(station_id, None)
    r = recv_ref[0]
    recv_ref[0] = None
    if entry is not None:
        if r:
            try:
                r.stop()
                r.wait()
            except Exception:
                pass
        del r
        try:
            _sdr_lock.release()
        except RuntimeError:
            pass
    else:
        del r
    try:
        os.unlink(fifo_path)
    except Exception:
        pass


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
    gc.collect()

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
    return templates.TemplateResponse(request, "recorded.html", {
        "files": files,
        "completed": COMPLETED_RECORDINGS,
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
    try:
        _sdr_lock.release()
    except RuntimeError:
        pass  # generate() の finally が先に解放した場合
    return {"stopped": True}


def stream_from_recording(wav_path: str, use_mp3: bool = False):
    """録音中のWAVファイルを先頭から読んでタイムシフトストリーミング配信"""
    WAV_HEADER_SIZE = 44

    if use_mp3:
        def generate():
            ffmpeg_proc = None
            try:
                ffmpeg_proc = subprocess.Popen(
                    ["ffmpeg", "-y", "-loglevel", "quiet",
                     "-f", "s16le", "-ar", "16000", "-ac", "1",
                     "-i", "pipe:0",
                     "-f", "mp3", "-q:a", "5", "pipe:1"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                def forward_pcm():
                    try:
                        with open(wav_path, "rb") as f:
                            f.seek(WAV_HEADER_SIZE)
                            while True:
                                chunk = f.read(4096)
                                if chunk:
                                    ffmpeg_proc.stdin.write(chunk)
                                else:
                                    if any(r["output"] == wav_path for r in IN_PROGRESS_RECORDINGS):
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

        return StreamingResponse(
            generate(),
            media_type="audio/mpeg",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    def generate():
        yield make_infinite_wav_header()
        with open(wav_path, "rb") as f:
            f.seek(WAV_HEADER_SIZE)
            while True:
                chunk = f.read(4096)
                if chunk:
                    yield chunk
                else:
                    if any(r["output"] == wav_path for r in IN_PROGRESS_RECORDINGS):
                        time.sleep(0.05)
                    else:
                        break

    return StreamingResponse(
        generate(),
        media_type="audio/wav",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/stream/{station_id}")
def stream_audio(station_id: str, request: Request):
    station = get_station(station_id)
    if station is None:
        return JSONResponse(content={"error": "局が見つかりません"}, status_code=404)

    use_mp3 = _needs_mp3(request)

    in_progress = next((r for r in IN_PROGRESS_RECORDINGS if r["station_id"] == station_id), None)
    if in_progress:
        return stream_from_recording(in_progress["output"], use_mp3=use_mp3)

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

    except Exception:
        _sdr_lock.release()
        raise

    if use_mp3:
        def generate():
            ffmpeg_proc = None
            try:
                ffmpeg_proc = subprocess.Popen(
                    ["ffmpeg", "-y", "-loglevel", "quiet",
                     "-f", "s16le", "-ar", "16000", "-ac", "1",
                     "-i", "pipe:0",
                     "-f", "mp3", "-q:a", "5", "pipe:1"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                def forward_pcm():
                    try:
                        # GNU Radio 起動待ちの間、2秒分の無音PCMを先行送信
                        ffmpeg_proc.stdin.write(bytes(16000 * 2 * 2))
                        with os.fdopen(rfd, "rb") as f:
                            while True:
                                chunk = f.read(4096)
                                if not chunk:
                                    break
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
        try:
            yield make_infinite_wav_header()
            # GNU Radio 起動待ち(2〜5秒)の間、2秒分の無音PCMを先行送信
            yield bytes(16000 * 2 * 2)  # 16kHz * int16(2byte) * 2sec
            with os.fdopen(rfd, "rb") as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    yield chunk
        finally:
            _cleanup_stream(station_id, recv_ref, fifo_path)

    return StreamingResponse(
        generate(),
        media_type="audio/wav",
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
