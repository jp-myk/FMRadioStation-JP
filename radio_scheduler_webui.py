import os
import json
import requests
import xml.etree.ElementTree as ET
import datetime
import threading
import fractions
import struct
import io
import fcntl
import time
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, Response, stream_with_context, jsonify
from flask_bootstrap import Bootstrap5
try:
    from gnuradio import gr, analog, filter, blocks
    import osmosdr
except ImportError:
    print("Error: GNU Radio or osmosdr library is not installed.")
    exit(1)
import re

JST = datetime.timezone(datetime.timedelta(hours=9))

app = Flask(__name__)
Bootstrap5(app)

app.jinja_env.filters['basename'] = os.path.basename

# ------------------------------
# ＜状態管理＞
STATE_FILE = "./state.json"
RECORDINGS_DIR = "./recordings"
if not os.path.exists(RECORDINGS_DIR):
    os.makedirs(RECORDINGS_DIR)

def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|]', '-', name).replace(' ', '_').replace('　', '_').strip()

def convert_datetime(o):
    if isinstance(o, datetime.datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_state = json.load(f)
        for category in ["scheduled", "in_progress", "completed"]:
            for item in raw_state.get(category, []):
                item["start_time"] = datetime.datetime.strptime(item["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        return raw_state
    return {"scheduled": [], "in_progress": [], "completed": []}

def save_state(state):
    def _convert(obj):
        if isinstance(obj, datetime.datetime):
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        return obj
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4, default=_convert)

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
# ＜局定義（Radiko station_id対応版）＞
STATIONS = [
    # FM局
    {"id": "INT",         "name": "interfm",            "freq": 76.1e6, "type": "fm"},
    {"id": "BAYFM78",     "name": "bayfm78",            "freq": 78.0e6, "type": "fm"},
    {"id": "FM-FUJI",      "name": "FM FUJI",            "freq": 78.6e6, "type": "fm"},
    {"id": "NACK5",       "name": "NACK5",              "freq": 79.5e6, "type": "fm"},
    {"id": "FMT",         "name": "TOKYO FM",           "freq": 80.0e6, "type": "fm"},
    {"id": "FMJ",         "name": "J-WAVE",             "freq": 81.3e6, "type": "fm"},
    {"id": "JOAK-FM",     "name": "NHK-FM 東京",        "freq": 82.5e6, "type": "fm"},
    {"id": "YFM",         "name": "FMヨコハマ",         "freq": 84.7e6, "type": "fm"},

    # ワイドFM / AM系
    {"id": "JOAK",        "name": "NHKラジオ第1",       "freq": 594e3,  "type": "am"},
    {"id": "JOAB",        "name": "NHKラジオ第2",       "freq": 693e3,  "type": "am"},
    {"id": "AFN",         "name": "AFN Tokyo",          "freq": 89.1e6, "type": "fm"},
    {"id": "TBS",         "name": "TBSラジオ",          "freq": 90.5e6, "type": "fm"},
    {"id": "QRR",         "name": "文化放送",           "freq": 91.6e6, "type": "fm"},
    {"id": "LFR",         "name": "ニッポン放送",       "freq": 93.0e6, "type": "fm"},
    {"id": "JORF",        "name": "ラジオ日本",         "freq": 92.4e6, "type": "fm"},
]

# ------------------------------
# ＜Radiko番組表＞
RADIKO_API_URL = "https://radiko.jp/v3/program/station/date/{YYYYMMDD}/{station_id}.xml"

_schedule_cache: dict = {}
SCHEDULE_CACHE_TTL = datetime.timedelta(minutes=30)

def get_radio_schedule(station_id: str, date: datetime.date = None) -> list:
    if date is None:
        date = datetime.datetime.now(JST).date()
    date_str = date.strftime("%Y%m%d")
    url = RADIKO_API_URL.format(YYYYMMDD=date_str, station_id=station_id)
    response = requests.get(url, timeout=10)
    if response.status_code != 200:
        return []
    root = ET.fromstring(response.content)
    schedule_list = []
    for prog in root.findall(".//prog"):
        prog_data = {}
        prog_data["id"] = prog.get("id")
        prog_data["ft"] = prog.get("ft")
        prog_data["to"] = prog.get("to")
        title_el = prog.find("title")
        prog_data["title"] = title_el.text.strip() if title_el is not None and title_el.text and title_el.text.strip() else "（タイトル未取得）"
        for key in ["info", "pfm", "tag", "genre"]:
            el = prog.find(key)
            prog_data[key] = el.text.strip() if el is not None and el.text and el.text.strip() else None
        prog_data["start_time"] = datetime.datetime.strptime(prog_data["ft"], "%Y%m%d%H%M%S").replace(tzinfo=JST)
        prog_data["end_time"] = datetime.datetime.strptime(prog_data["to"], "%Y%m%d%H%M%S").replace(tzinfo=JST)
        prog_data["duration"] = int((prog_data["end_time"] - prog_data["start_time"]).total_seconds())
        schedule_list.append(prog_data)
    return schedule_list

def get_radio_schedule_cached(station_id: str, date: datetime.date = None) -> list:
    if date is None:
        date = datetime.datetime.now(JST).date()
    date_str = date.strftime("%Y%m%d")
    now = datetime.datetime.now(JST)
    cache_key = (station_id, date_str)
    if cache_key in _schedule_cache:
        programs, cached_at = _schedule_cache[cache_key]
        if now - cached_at < SCHEDULE_CACHE_TTL:
            return programs
    programs = get_radio_schedule(station_id, date)
    _schedule_cache[cache_key] = (programs, now)
    return programs

# ------------------------------
# ＜GNURadio＞
if not hasattr(filter.firdes, 'WIN_HAMMING'):
    filter.firdes.WIN_HAMMING = 1

class FMReceiver(gr.top_block):
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate=16000, bit_rate=16, gain=40):
        gr.top_block.__init__(self, "FM Receiver")
        self.freq = freq
        self.sdr_sample_rate = sdr_sample_rate
        self.output_file = output_file

        decim1 = max(1, int(round(sdr_sample_rate / (desired_audio_rate * 10))))
        new_rate = sdr_sample_rate / decim1
        audio_decim = max(1, int(round(new_rate / desired_audio_rate)))
        initial_audio_rate = new_rate / audio_decim

        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        self.lpf = filter.fir_filter_ccf(
            decim1,
            filter.firdes.low_pass(1.0, sdr_sample_rate, 50e3, 10e3, filter.firdes.WIN_HAMMING, 6.76)
        )
        self.wbfm = analog.wfm_rcv(quad_rate=new_rate, audio_decimation=audio_decim)

        dt = 1.0 / initial_audio_rate
        alpha = dt / (75e-6 + dt)
        self.deemph = filter.single_pole_iir_filter_ff(alpha)

        use_resampler = abs(initial_audio_rate - desired_audio_rate) > 1
        if use_resampler:
            frac = fractions.Fraction(int(round(desired_audio_rate)), int(round(initial_audio_rate))).limit_denominator(50)
            self.resampler = filter.rational_resampler_fff(
                interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0.0
            )
            final_rate = desired_audio_rate
        else:
            final_rate = initial_audio_rate

        self.hq_filter = filter.fir_filter_fff(
            1,
            filter.firdes.low_pass(1.0, final_rate, final_rate / 2 * 0.9, final_rate * 0.05, filter.firdes.WIN_HAMMING, 6.76)
        )

        if bit_rate == 24:
            wav_format = blocks.FORMAT_PCM_24
        elif bit_rate == 32:
            wav_format = blocks.FORMAT_PCM_32
        else:
            wav_format = blocks.FORMAT_PCM_16

        print(f"output file: {output_file}")
        self.wav_sink = blocks.wavfile_sink(output_file, 1, int(final_rate), blocks.FORMAT_WAV, wav_format, False)

        if use_resampler:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.resampler, self.hq_filter, self.wav_sink)
        else:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.hq_filter, self.wav_sink)


class AMReceiver(gr.top_block):
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate=16000, bit_rate=16, gain=40):
        gr.top_block.__init__(self, "AM Receiver")

        decim = int(sdr_sample_rate / (desired_audio_rate * 4))

        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        self.lpf = filter.fir_filter_ccf(
            decim,
            filter.firdes.low_pass(1.0, sdr_sample_rate, 5e3, 1e3, filter.firdes.WIN_HAMMING, 6.76)
        )
        self.demod = blocks.complex_to_mag(1)

        resample_rate = sdr_sample_rate / decim
        frac = fractions.Fraction(int(round(desired_audio_rate)), int(round(resample_rate))).limit_denominator(100)
        self.resampler = filter.rational_resampler_fff(
            interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0
        )

        wav_format = {16: blocks.FORMAT_PCM_16, 24: blocks.FORMAT_PCM_24, 32: blocks.FORMAT_PCM_32}.get(bit_rate, blocks.FORMAT_PCM_16)
        print(f"output file: {output_file}")
        self.wav_sink = blocks.wavfile_sink(output_file, 1, int(desired_audio_rate), blocks.FORMAT_WAV, wav_format, False)

        self.connect(self.source, self.lpf, self.demod, self.resampler, self.wav_sink)


class StreamingFMReceiver(gr.top_block):
    """FMReceiverと同じパイプラインだが出力をFIFOへの生int16 PCMにする"""
    def __init__(self, freq, sdr_sample_rate, fifo_path, desired_audio_rate=16000, gain=40):
        gr.top_block.__init__(self, "FM Streaming Receiver")

        decim1 = max(1, int(round(sdr_sample_rate / (desired_audio_rate * 10))))
        new_rate = sdr_sample_rate / decim1
        audio_decim = max(1, int(round(new_rate / desired_audio_rate)))
        initial_audio_rate = new_rate / audio_decim

        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        self.lpf = filter.fir_filter_ccf(
            decim1,
            filter.firdes.low_pass(1.0, sdr_sample_rate, 50e3, 10e3, filter.firdes.WIN_HAMMING, 6.76)
        )
        self.wbfm = analog.wfm_rcv(quad_rate=new_rate, audio_decimation=audio_decim)

        dt = 1.0 / initial_audio_rate
        alpha = dt / (75e-6 + dt)
        self.deemph = filter.single_pole_iir_filter_ff(alpha)

        use_resampler = abs(initial_audio_rate - desired_audio_rate) > 1
        if use_resampler:
            frac = fractions.Fraction(int(round(desired_audio_rate)), int(round(initial_audio_rate))).limit_denominator(50)
            self.resampler = filter.rational_resampler_fff(
                interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0.0
            )
            final_rate = desired_audio_rate
        else:
            final_rate = initial_audio_rate

        self.hq_filter = filter.fir_filter_fff(
            1,
            filter.firdes.low_pass(1.0, final_rate, final_rate / 2 * 0.9, final_rate * 0.05, filter.firdes.WIN_HAMMING, 6.76)
        )

        # float → int16 変換してFIFOへ書き込む
        self.converter = blocks.float_to_short(1, 32767)
        self.file_sink = blocks.file_sink(gr.sizeof_short, fifo_path)
        self.file_sink.set_unbuffered(True)

        if use_resampler:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.resampler, self.hq_filter, self.converter, self.file_sink)
        else:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.hq_filter, self.converter, self.file_sink)

        self._sample_rate = int(final_rate)


class StreamingAMReceiver(gr.top_block):
    """AM放送ストリーミング用。エンベロープ検波でFIFOへ生int16 PCMを出力する。"""
    def __init__(self, freq, sdr_sample_rate, fifo_path, desired_audio_rate=16000, gain=40):
        gr.top_block.__init__(self, "AM Streaming Receiver")

        decim = int(sdr_sample_rate / (desired_audio_rate * 4))

        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        self.lpf = filter.fir_filter_ccf(
            decim,
            filter.firdes.low_pass(1.0, sdr_sample_rate, 5e3, 1e3, filter.firdes.WIN_HAMMING, 6.76)
        )
        self.demod = blocks.complex_to_mag(1)

        resample_rate = sdr_sample_rate / decim
        frac = fractions.Fraction(int(round(desired_audio_rate)), int(round(resample_rate))).limit_denominator(100)
        self.resampler = filter.rational_resampler_fff(
            interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0
        )

        final_rate = desired_audio_rate
        self.hq_filter = filter.fir_filter_fff(
            1,
            filter.firdes.low_pass(1.0, final_rate, final_rate / 2 * 0.9, final_rate * 0.05, filter.firdes.WIN_HAMMING, 6.76)
        )

        self.converter = blocks.float_to_short(1, 32767)
        self.file_sink = blocks.file_sink(gr.sizeof_short, fifo_path)
        self.file_sink.set_unbuffered(True)

        self.connect(self.source, self.lpf, self.demod, self.resampler, self.hq_filter, self.converter, self.file_sink)

        self._sample_rate = int(final_rate)


def make_infinite_wav_header(sample_rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    """ストリーミング用WAVヘッダー（データサイズ=0xFFFFFFF0で事実上無限）"""
    data_size = 0xFFFFFFF0
    buf = io.BytesIO()
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', 0xFFFFFFFF))
    buf.write(b'WAVE')
    buf.write(b'fmt ')
    buf.write(struct.pack('<I', 16))
    buf.write(struct.pack('<H', 1))          # PCM
    buf.write(struct.pack('<H', channels))
    buf.write(struct.pack('<I', sample_rate))
    buf.write(struct.pack('<I', sample_rate * channels * bits // 8))  # ByteRate
    buf.write(struct.pack('<H', channels * bits // 8))                # BlockAlign
    buf.write(struct.pack('<H', bits))
    buf.write(b'data')
    buf.write(struct.pack('<I', data_size))
    return buf.getvalue()


# アクティブストリーマー管理
_active_streamers: dict = {}  # station_id → (StreamingFMReceiver, fifo_path)
_stream_lock = threading.Lock()

# ------------------------------
# ＜録音処理＞
def record_radio(station_id, output_file, duration, title, start_time, program_detail):
    station = next((s for s in STATIONS if s["id"] == station_id), None)
    if station is None:
        print("不明な局:", station_id)
        return

    # ストリーミング中なら停止してから録音
    with _stream_lock:
        for sid, (recv, fifo_path) in list(_active_streamers.items()):
            try:
                recv.stop()
                recv.wait()
            except Exception:
                pass
            try:
                os.unlink(fifo_path)
            except Exception:
                pass
        _active_streamers.clear()

    in_progress_item = {
        "title": title,
        "station_id": station_id,
        "station_name": station["name"],
        "start_time": start_time,
        "duration": duration,
        "output": output_file,
        "program_detail": program_detail,
    }
    IN_PROGRESS_RECORDINGS.append(in_progress_item)
    update_global_state()
    print(f"[{datetime.datetime.now(JST)}] 録音開始: {station['name']} - {output_file}")
    if station.get("type") == "am":
        receiver = AMReceiver(station["freq"], 2.4e6, output_file, 16000)
    else:
        receiver = FMReceiver(station["freq"], 2.4e6, output_file, 16000)
    receiver.start()
    time.sleep(duration)
    receiver.stop()
    receiver.wait()
    print(f"[{datetime.datetime.now(JST)}] 録音終了: {output_file}")
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
@app.route("/")
def index():
    return render_template(
        "index.html",
        scheduled=SCHEDULED_RECORDINGS,
        in_progress=IN_PROGRESS_RECORDINGS,
        completed=COMPLETED_RECORDINGS,
    )

@app.route("/on-air")
def on_air():
    return render_template("on_air.html")

@app.route("/programs")
def programs():
    today = datetime.datetime.now(JST).date()
    tomorrow = today + datetime.timedelta(days=1)
    program_table: dict = {}
    for date in [today, tomorrow]:
        for station in STATIONS:
            for prog in get_radio_schedule_cached(station["id"], date):
                slot = prog["start_time"].strftime("%Y-%m-%d %H:%M")
                if slot not in program_table:
                    program_table[slot] = {}
                program_table[slot][station["id"]] = prog
    reserved_map = {}
    for r in SCHEDULED_RECORDINGS:
        start_str = r["start_time"] if isinstance(r["start_time"], str) else r["start_time"].strftime("%Y-%m-%d %H:%M:%S")
        reserved_map[(r["station_id"], start_str)] = True
    WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]
    date_labels = {
        today.strftime("%Y-%m-%d"): f"今日 ({today.month}/{today.day}・{WEEKDAYS_JA[today.weekday()]})",
        tomorrow.strftime("%Y-%m-%d"): f"明日 ({tomorrow.month}/{tomorrow.day}・{WEEKDAYS_JA[tomorrow.weekday()]})",
    }
    return render_template("programs.html", stations=STATIONS, program_table=program_table, reservations=reserved_map, date_labels=date_labels)

@app.route("/recording")
def recording():
    return render_template("recording.html", in_progress=IN_PROGRESS_RECORDINGS)

@app.route("/recorded")
@app.route("/recordings")
def recorded():
    files = [f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".wav")]
    return render_template("recorded.html", files=files, completed=COMPLETED_RECORDINGS)

@app.route("/reservations")
def reservations():
    return render_template("reservations.html", scheduled=SCHEDULED_RECORDINGS)

@app.route('/recordings/<path:filename>')
def serve_recordings(filename):
    return send_from_directory(RECORDINGS_DIR, filename)

# ------------------------------
# ＜予約処理＞
@app.route("/schedule", methods=["POST"])
def schedule_recording():
    station_id = request.form.get("station_id")
    title = request.form.get("title")
    start_time = request.form.get("start_time")
    duration = int(request.form.get("duration"))
    program_id = request.form.get("program_id")

    safe_title = sanitize_filename(title)
    safe_start = start_time.replace(" ", "_").replace(":", "-")
    output_file = os.path.join(RECORDINGS_DIR, f"{safe_start}_{station_id}_{safe_title}.wav")

    new_item = {
        "title": title,
        "station_id": station_id,
        "station": next((s["name"] for s in STATIONS if s["id"] == station_id), "不明な局"),
        "start_time": start_time,
        "duration": duration,
        "output": output_file,
        "program_id": program_id,
    }
    SCHEDULED_RECORDINGS.append(new_item)
    update_global_state()

    programs_list = get_radio_schedule(station_id)
    program_detail = next((p for p in programs_list if p.get("id") == program_id), None)
    start_dt = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    delay = max(0.0, (start_dt - datetime.datetime.now(JST)).total_seconds())
    print(f"[{start_dt}] book: {title}")
    threading.Timer(delay, record_radio, args=(station_id, output_file, duration, title, start_time, program_detail)).start()
    return redirect(url_for("programs"))

# ------------------------------
# ＜JSON API＞
@app.route("/api/dashboard")
def api_dashboard():
    return jsonify({
        "scheduled": len(SCHEDULED_RECORDINGS),
        "in_progress": len(IN_PROGRESS_RECORDINGS),
        "completed": len(COMPLETED_RECORDINGS),
    })

@app.route("/api/on-air")
def api_on_air():
    now = datetime.datetime.now(JST)
    results = []
    lock = threading.Lock()

    def fetch_station(station):
        try:
            for prog in get_radio_schedule_cached(station["id"]):
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
                            "is_recording": any(r["station_id"] == station["id"] for r in IN_PROGRESS_RECORDINGS),
                        })
                    break
        except Exception:
            pass

    threads = [threading.Thread(target=fetch_station, args=(s,)) for s in STATIONS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    station_order = {s["id"]: i for i, s in enumerate(STATIONS)}
    results.sort(key=lambda x: station_order.get(x["station_id"], 999))
    return jsonify(results)

@app.route("/api/recording")
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
    return jsonify(items)

@app.route("/api/recorded")
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
    return jsonify(items)

@app.route("/api/reservations")
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
    return jsonify(items)

@app.route("/api/reservations/<int:idx>", methods=["DELETE"])
def api_delete_reservation(idx):
    if idx < 0 or idx >= len(SCHEDULED_RECORDINGS):
        return jsonify({"error": "不正なインデックス"}), 400
    removed = SCHEDULED_RECORDINGS.pop(idx)
    update_global_state()
    title = removed["title"] if isinstance(removed["title"], str) else str(removed["title"])
    return jsonify({"deleted": title})

# ------------------------------
# ＜ストリーミング＞
@app.route("/api/stream-status/<station_id>")
def api_stream_status(station_id):
    station = next((s for s in STATIONS if s["id"] == station_id), None)
    if station is None:
        return jsonify({"available": False, "reason": "局が見つかりません"}), 404

    in_progress = next((r for r in IN_PROGRESS_RECORDINGS if r["station_id"] == station_id), None)
    if in_progress:
        return jsonify({"available": True, "mode": "timeshift"})

    if IN_PROGRESS_RECORDINGS:
        rec = IN_PROGRESS_RECORDINGS[0]
        station_name = rec.get("station_name", rec["station_id"])
        return jsonify({"available": False, "reason": f"「{station_name}」を録音中のため配信できません"})

    # 別局がストリーミング中ならSDRデバイスは占有中
    with _stream_lock:
        active_sids = list(_active_streamers.keys())
    if active_sids and station_id not in active_sids:
        active_sid = active_sids[0]
        st = next((s for s in STATIONS if s["id"] == active_sid), None)
        name = st["name"] if st else active_sid
        return jsonify({"available": False, "reason": f"「{name}」をストリーミング中のため配信できません"})

    return jsonify({"available": True, "mode": "live"})


def stream_from_recording(wav_path: str):
    """録音中のWAVファイルを先頭から読んでタイムシフトストリーミング配信"""
    WAV_HEADER_SIZE = 44  # GNU Radio wavfile_sink の標準WAVヘッダサイズ

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

    return Response(
        stream_with_context(generate()),
        mimetype="audio/wav",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/stream/<station_id>")
def stream_audio(station_id):
    station = next((s for s in STATIONS if s["id"] == station_id), None)
    if station is None:
        return jsonify({"error": "局が見つかりません"}), 404

    # 同一局が録音中 → タイムシフト再生
    in_progress = next((r for r in IN_PROGRESS_RECORDINGS if r["station_id"] == station_id), None)
    if in_progress:
        return stream_from_recording(in_progress["output"])

    # 別局が録音中 → SDR使用不可
    if IN_PROGRESS_RECORDINGS:
        return jsonify({"error": "他の局を録音中のため配信できません"}), 409

    # 別局がストリーミング中 → SDRデバイス占有中（排他制御）
    with _stream_lock:
        if _active_streamers and station_id not in _active_streamers:
            return jsonify({"error": "SDRデバイスは使用中です。現在のストリームを停止してから再試行してください"}), 409

    fifo_path = f"/tmp/radio_stream_{station_id}.pcm"
    if os.path.exists(fifo_path):
        os.unlink(fifo_path)
    os.mkfifo(fifo_path)

    # 読み込み側を先に開く（O_NONBLOCK）→ file_sinkコンストラクタがブロックしない
    rfd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
    # O_NONBLOCK を解除してread()をブロッキングモードに
    flags = fcntl.fcntl(rfd, fcntl.F_GETFL)
    fcntl.fcntl(rfd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

    try:
        if station.get("type") == "am":
            receiver = StreamingAMReceiver(station["freq"], 2.4e6, fifo_path, desired_audio_rate=16000, gain=40)
        else:
            receiver = StreamingFMReceiver(station["freq"], 2.4e6, fifo_path, desired_audio_rate=16000, gain=40)
    except Exception as e:
        os.close(rfd)
        os.unlink(fifo_path)
        return jsonify({"error": f"SDR初期化エラー: {e}"}), 500

    with _stream_lock:
        _active_streamers[station_id] = (receiver, fifo_path)

    receiver.start()

    def generate():
        try:
            yield make_infinite_wav_header()
            # GNURadio 起動待ち(2〜5秒)の間、2秒分の無音PCMを先行送信してブラウザのバッファを満たす
            yield bytes(16000 * 2 * 2)  # 16kHz * int16(2byte) * 2sec = 64000 bytes
            with os.fdopen(rfd, "rb") as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    yield chunk
        finally:
            with _stream_lock:
                _active_streamers.pop(station_id, None)
            try:
                receiver.stop()
                receiver.wait()
            except Exception:
                pass
            try:
                os.unlink(fifo_path)
            except Exception:
                pass

    return Response(
        stream_with_context(generate()),
        mimetype="audio/wav",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )

# ------------------------------
# ＜起動時の再スケジュール＞
def reschedule_pending():
    now = datetime.datetime.now(JST)
    for rec in SCHEDULED_RECORDINGS[:]:
        start_dt = rec["start_time"] if isinstance(rec["start_time"], datetime.datetime) else datetime.datetime.strptime(rec["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        if start_dt > now:
            start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            programs_list = get_radio_schedule(rec["station_id"])
            program_detail = next((p for p in programs_list if p.get("id") == rec.get("program_id")), None)
            delay = (start_dt - now).total_seconds()
            threading.Timer(delay, record_radio, args=(rec["station_id"], rec["output"], rec["duration"], rec["title"], start_str, program_detail)).start()
        else:
            SCHEDULED_RECORDINGS.remove(rec)

# ------------------------------
if __name__ == "__main__":
    reschedule_pending()
    app.run(host="0.0.0.0", port=5000, threaded=True)
