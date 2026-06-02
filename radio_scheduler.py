#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import requests
import xml.etree.ElementTree as ET
import xmltodict
import time
import datetime
import json
import threading
import logging
import logging.handlers
import argparse
import re
import fractions
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Callable

# --- GNU Radioモジュールのインポート ---
# GNU Radioがインストールされていない場合でもエラーで即時終了しないように、try-exceptで囲む
try:
    from gnuradio import gr, analog, filter as gr_filter, blocks
    import osmosdr
except ImportError:
    # 必要なライブラリがない場合は、エラーメッセージを表示して終了する
    print("Error: GNU Radio or osmosdr library is not installed.")
    print("Please install it to run this script (e.g., `conda install -c conda-forge gnuradio`).")
    exit(1)
# WIN_HAMMING が定義されていなければ設定
if not hasattr(gr_filter.firdes, 'WIN_HAMMING'):
    gr_filter.firdes.WIN_HAMMING = 1
# -----------------------------------------------------------------------------
# 定数の定義 (Constants)
# -----------------------------------------------------------------------------
# 放送局の情報（ID、名称、周波数、種別）を定義した辞書
# 将来的に設定ファイルから読み込むように変更することも可能
STATION_MAP = {
    # FM 局
    "COMFM": {"name": "コミュニティFM", "freq": 76.1e6, "type": "fm"},
    "INT": {"name": "InterFM", "freq": 76.5e6, "type": "fm"},
    "FM-TOSHIMA": {"name": "FMとしま", "freq": 77.5e6, "type": "fm"},
    "BAYFM": {"name": "bayfm", "freq": 78.0e6, "type": "fm"},
    "MUSASHINO": {"name": "むさしのFM", "freq": 78.2e6, "type": "fm"},
    "FMFUJI": {"name": "FM FUJI", "freq": 78.6e6, "type": "fm"},
    "KATSUSHIKA": {"name": "かつしかFM", "freq": 78.9e6, "type": "fm"},
    "TAMALAKE": {"name": "多摩レイクサイドFM", "freq": 79.0e6, "type": "fm"},
    "KAWASAKI": {"name": "かわさきFM", "freq": 79.1e6, "type": "fm"},
    "NACK5": {"name": "NACK5", "freq": 79.5e6, "type": "fm"},
    "FMT": {"name": "TOKYO FM", "freq": 80.0e6, "type": "fm"},
    "J-WAVE": {"name": "J-WAVE", "freq": 81.3e6, "type": "fm"},
    "NHK-FM": {"name": "NHK-FM 東京", "freq": 82.5e6, "type": "fm"},
    "SETAGAYA": {"name": "FM世田谷", "freq": 83.4e6, "type": "fm"},
    "FM-TACHIKAWA": {"name": "FMたちかわ", "freq": 83.6e6, "type": "fm"},
    "CHOFU": {"name": "調布FM", "freq": 83.8e6, "type": "fm"},
    "FMYOKOHAMA": {"name": "FMヨコハマ", "freq": 84.7e6, "type": "fm"},
    # AM 局
    "NHK-R2": {"name": "NHKラジオ第2", "freq": 693e3, "type": "am"},
    "AFN": {"name": "AFN Eagle 810", "freq": 810e3, "type": "am"},
    # ワイドFM局（AM局がFM補完放送に移行）
    "NHK-R1": {"name": "NHKラジオ第1", "freq": 91.5e6, "type": "fm"},
    "TBS": {"name": "TBSラジオ", "freq": 90.5e6, "type": "fm"},
    "BUNKA": {"name": "文化放送", "freq": 91.6e6, "type": "fm"},
    "NIPPON": {"name": "ニッポン放送", "freq": 93.0e6, "type": "fm"},
    "RADIO-NIHON": {"name": "ラジオ日本", "freq": 92.4e6, "type": "fm"},
}

# -----------------------------------------------------------------------------
# ヘルパー関数 (Helper Functions)
# -----------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    """ファイル名として使用できない文字をハイフンに置換する。"""
    return re.sub(r'[\\/:*?"<>|]', '-', name).replace(' ', '_').replace('　', '_').strip

# -----------------------------------------------------------------------------
# データクラスの定義 (Data Classes)
# -----------------------------------------------------------------------------
@dataclass
class RadioProgram:
    """ラジオ番組の情報を保持するためのデータクラス。"""
    station_id: str                 # 放送局ID (例: "FMT")
    detail:dict                     # 番組の詳細情報（progノード）
    title: str                      # 番組名
    start_time: datetime.datetime   # 開始時刻
    end_time: datetime.datetime     # 終了時刻
    duration_sec: int               # 番組の長さ（秒）

    @property
    def unique_id(self) -> str:
        """番組を一位に識別するためのIDを生成する。"""
        # "放送局ID_開始日時_番組名" の形式でIDを生成
        return f"{self.station_id}_{self.start_time.strftime('%Y%m%d%H%M')}_{sanitize_filename(self.title)}"

@dataclass
class RadioConfig:
    """スクリプト全体の設定を保持するためのデータクラス。"""
    poll_interval: int = 3600*6             # 番組表をチェックする間隔（秒）
    rec_dir: str = "radio_recordings"       # 録音ファイルの保存先ディレクトリ
    stations_to_record: List[str] = field(default_factory=list) # 録音対象の放送局IDリスト
    sdr_sample_rate: float = 2.4e6          # SDRのサンプルレート
    audio_rate: int = 48000                 # 出力するWAVファイルのサンプルレート
    bit_rate: int = 16                      # 出力するWAVファイルのビットレート
    sdr_gain: int = 40                      # SDRのゲイン（感度）
    log_dir: str = "log"                    # ログファイルの保存先ディレクトリ

    @classmethod
    def from_args(cls) -> 'RadioConfig':
        """コマンドライン引数を解析し、設定オブジェクトを生成する。"""
        parser = argparse.ArgumentParser(
            description="A script to schedule radio recordings using GNU Radio."
        )
        # 各種設定をコマンドライン引数として定義
        parser.add_argument("--poll-interval", type=int, default=3600*6,
                            help="Interval in seconds to check the program guide.")
        parser.add_argument("--rec-dir", type=str, default="radio_recordings",
                            help="Directory to save recorded files.")
        parser.add_argument("--stations", type=str, required=True,
                            help=f"Comma-separated station IDs to record. e.g., TFM,J-WAVE. Available: {', '.join(STATION_MAP.keys())}")
        parser.add_argument("--sample-rate", type=float, default=2.4e6, help="SDR sample rate.")
        parser.add_argument("--audio-rate", type=int, default=24000, help="Output audio sample rate.")
        parser.add_argument("--bit-rate", type=int, choices=[16, 24, 32], default=16, help="Output WAV bit rate.")
        parser.add_argument("--gain", type=int, default=40, help="SDR gain.")
        parser.add_argument("--log-dir", type=str, default="log", help="Directory to save log files.")
        args = parser.parse_args()

        # カンマ区切りの放送局IDをリストに変換
        stations = [s.strip().upper() for s in args.stations.split(",")]
        # 指定された放送局IDがSTATION_MAPに存在するかチェック
        for station_id in stations:
            if station_id not in STATION_MAP:
                raise ValueError(f"Invalid station ID specified: {station_id}")

        # 解析した引数を元に設定クラスのインスタンスを生成して返す
        return cls(
            poll_interval=args.poll_interval,
            rec_dir=args.rec_dir,
            stations_to_record=stations,
            sdr_sample_rate=args.sample_rate,
            audio_rate=args.audio_rate,
            bit_rate=args.bit_rate,
            sdr_gain=args.gain,
            log_dir=args.log_dir,
        )

# -----------------------------------------------------------------------------
# GNU Radio フローグラフ (GNU Radio Flowgraphs)
# -----------------------------------------------------------------------------
# GNU Radioの信号処理ブロックを接続して作成した受信機の実装

class FMReceiver(gr.top_block):
    """FM放送を受信するためのGNU Radioフローグラフ。"""
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate, bit_rate, gain):
        gr.top_block.__init__(self, "FM Receiver")

        # --- サンプルレート変換のための計算 ---
        decim1 = max(1, int(round(sdr_sample_rate / (desired_audio_rate * 10))))
        new_rate = sdr_sample_rate / decim1
        audio_decim = max(1, int(round(new_rate / desired_audio_rate)))
        initial_audio_rate = new_rate / audio_decim

        # --- GNU Radio ブロックの定義 ---
        # SDRソース: SDRデバイスから信号を受信する
        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        # ローパスフィルタ: 目的のFM局の信号帯域だけを抽出する
        self.lpf = gr_filter.fir_filter_ccf(
            decim1, #gr_filter.firdes.low_pass(1.0, sdr_sample_rate, 75e3, 25e3, gr_filter.firdes.WIN_HAMMING, 6.76)
            gr_filter.firdes.low_pass(
                1.0, sdr_sample_rate, 50e3, 10e3, gr_filter.firdes.WIN_HAMMING, 6.76
            )
        )
        # WFM復調器: FM信号を音声信号に変換する
        self.wbfm = analog.wfm_rcv(quad_rate=new_rate, audio_decimation=audio_decim)
        # ディエンファシスフィルタ: FM放送で高音域を強調する処理の逆を行う
        dt = 1.0 / initial_audio_rate
        alpha = dt / (75e-6 + dt) # 日本のFM放送は75us
        self.deemph = gr_filter.single_pole_iir_filter_ff(alpha)

        # Rational Resampler: 音声のサンプルレートを目的のレートに正確に変換する
        use_resampler = abs(initial_audio_rate - desired_audio_rate) > 1
        if use_resampler:
            frac = fractions.Fraction(desired_audio_rate, int(initial_audio_rate)).limit_denominator(100)
            self.resampler = gr_filter.rational_resampler_fff(
                interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0
            )
        # FIRフィルタ（高音質化）
        self.hq_filter = gr_filter.fir_filter_fff(1, gr_filter.firdes.low_pass(
                1.0,
                desired_audio_rate,
                desired_audio_rate/2 * 0.9,
                desired_audio_rate * 0.05,
                gr_filter.firdes.WIN_HAMMING,
                6.76
            )
        )
        # WAVファイルシンク: 最終的な音声信号をWAVファイルとして保存する
        wav_format = {16: blocks.FORMAT_PCM_16, 24: blocks.FORMAT_PCM_24, 32: blocks.FORMAT_PCM_32}.get(bit_rate)
        if wav_format is None:
            raise ValueError(f"Unsupported bit rate: {bit_rate}")
        self.wav_sink = blocks.wavfile_sink(output_file, 1, int(desired_audio_rate), blocks.FORMAT_WAV, wav_format, False)

        # --- ブロックの接続 ---
        #self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.resampler, self.wav_sink)
        # 接続
        if use_resampler:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.resampler, self.hq_filter, self.wav_sink)
        else:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.hq_filter, self.wav_sink)


class AMReceiver(gr.top_block):
    """AM放送を受信するためのGNU Radioフローグラフ。"""
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate, bit_rate, gain):
        gr.top_block.__init__(self, "AM Receiver")

        decim = int(sdr_sample_rate / (desired_audio_rate * 4)) # フィルタ性能向上のためオーバーサンプリング

        # SDRソース
        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        # ローパスフィルタ: AM局の信号帯域を抽出
        self.lpf = gr_filter.fir_filter_ccf(
            decim, gr_filter.firdes.low_pass(1.0, sdr_sample_rate, 5e3, 1e3, gr_filter.firdes.WIN_HAMMING, 6.76)
        )
        # AM復調器（エンベロープ検波）
        self.demod = blocks.complex_to_mag(1)

        # Rational Resampler: 音声サンプルレートを正確に変換
        resample_rate = sdr_sample_rate / decim
        frac = fractions.Fraction(desired_audio_rate, int(resample_rate)).limit_denominator(100)
        self.resampler = gr_filter.rational_resampler_fff(
            interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0
        )

        # WAVファイルシンク
        wav_format = {16: blocks.FORMAT_PCM_16, 24: blocks.FORMAT_PCM_24, 32: blocks.FORMAT_PCM_32}.get(bit_rate)
        if wav_format is None:
            raise ValueError(f"Unsupported bit rate: {bit_rate}")
        self.wav_sink = blocks.wavfile_sink(output_file, 1, int(desired_audio_rate), blocks.FORMAT_WAV, wav_format, False)

        # --- ブロックの接続 ---
        self.connect(self.source, self.lpf, self.demod, self.resampler, self.wav_sink)

# -----------------------------------------------------------------------------
# APIクライアント (API Client)
# -----------------------------------------------------------------------------
class RadikoClient:
    """Radikoの番組表API(XML)から番組情報を取得するクライアント。"""
    API_URL_TEMPLATE = "https://radiko.jp/v3/program/station/date/{YYYYMMDD}/{STATION_ID}.xml"

    def fetch_programs_for_station(self, station_id: str) -> Optional[List[RadioProgram]]:
        """指定された放送局の今日の番組表を取得する。"""
        today_str = datetime.datetime.now().strftime("%Y%m%d")
        url = self.API_URL_TEMPLATE.format(YYYYMMDD=today_str, STATION_ID=station_id)

        try:
            # APIにGETリクエストを送信
            response = requests.get(url, timeout=10)
            response.raise_for_status() # エラーがあれば例外を発生させる

            # 取得したXMLコンテンツを解析
            root = ET.fromstring(response.content)
            programs = []
            # 各番組(<prog>)タグをループ処理
            for prog_node in root.findall(".//prog"):
                start_str = prog_node.get("ft")
                end_str = prog_node.get("to")
                title = prog_node.find("title").text if prog_node.find("title") is not None else "No Title"

                # 時刻文字列をdatetimeオブジェクトに変換
                start_dt = datetime.datetime.strptime(start_str, "%Y%m%d%H%M%S")
                end_dt = datetime.datetime.strptime(end_str, "%Y%m%d%H%M%S")

                # RadioProgramオブジェクトを作成してリストに追加
                program = RadioProgram(
                    station_id=station_id,
                    detail=xmltodict.parse(ET.tostring(prog_node)),
                    title=title,
                    start_time=start_dt,
                    end_time=end_dt,
                    duration_sec=int((end_dt - start_dt).total_seconds())
                )
                programs.append(program)
            return programs
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch schedule for {station_id}: {e}")
            return None
        except ET.ParseError as e:
            logger.error(f"Failed to parse XML for {station_id}: {e}")
            return None

# -----------------------------------------------------------------------------
# レコーダー (Recorder)
# -----------------------------------------------------------------------------
class RadioRecorder:
    """録音処理とSDRデバイスリソースの管理を行うクラス。"""
    def __init__(self, config: RadioConfig):
        self.config = config
        # 一度に一つの録音しか実行しないようにするためのロック
        self._lock = threading.Lock()
        # 録音ディレクトリが存在しなければ作成
        os.makedirs(self.config.rec_dir, exist_ok=True)

    def record_program(self, program: RadioProgram, on_complete: Callable[[str], None]):
        """
        一つの番組を録音する。このメソッドは独立したスレッドで実行されることを想定している。
        """
        # ロックを取得するまで待機する (ブロッキングモード)
        # これにより、前の録音が終了するのを待ってから次の録音を開始できる
        self._lock.acquire(blocking=True)

        station_info = STATION_MAP[program.station_id]
        receiver = None
        # try-finally構文で、処理中にエラーが発生しても必ずロックが解放されるようにする
        try:
            # 出力ファイル名を生成
            prefix = f"{program.start_time.strftime('%Y%m%d-%H%M')}_{program.station_id}_{sanitize_filename(program.title)}"
            wav_output_path = os.path.join(self.config.rec_dir, f"{prefix}.wav")
            json_output_path = os.path.join(self.config.rec_dir, f"{prefix}.json")

            logger.info(f"Recording '{program.title}' on {station_info['name']} ({station_info['freq']/1e6} MHz)")
            logger.info(f"Output file: {wav_output_path}")

            # 放送局の種別（AM/FM）に応じて、適切な受信機フローグラフをインスタンス化
            if station_info['type'] == 'fm':
                receiver = FMReceiver(
                    freq=station_info['freq'], output_file=wav_output_path,
                    desired_audio_rate=self.config.audio_rate, bit_rate=self.config.bit_rate,
                    sdr_sample_rate=self.config.sdr_sample_rate, gain=self.config.sdr_gain
                )
                #def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate, bit_rate, gain):
            elif station_info['type'] == 'am':
                receiver = AMReceiver(
                    freq=station_info['freq'], output_file=wav_output_path,
                    desired_audio_rate=self.config.audio_rate, bit_rate=self.config.bit_rate,
                    sdr_sample_rate=self.config.sdr_sample_rate, gain=self.config.sdr_gain
                )
            else:
                raise ValueError(f"Unknown station type: {station_info['type']}")

            # 録音を開始し、番組の長さだけ待機
            receiver.start()
            time.sleep(program.duration_sec)

        except Exception as e:
            logger.error(f"An error occurred during recording of '{program.title}': {e}")
            os.remove(wav_output_path)  # エラー時は出力ファイルを削除
        finally:
            # 受信機が起動していれば停止させる
            if receiver:
                logger.info(f"Stopping recording for '{program.title}'.")
                receiver.stop()
                receiver.wait() # フローグラフが完全に停止するのを待つ
                json.dump(program.detail, open(json_output_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
            # ロックを解放して、他の録音タスクが実行できるようにする
            self._lock.release()
            logger.info(f"Lock released. Recording task finished for: {program.title}")
            # スケジューラに録音完了を通知
            on_complete(program.unique_id)

# -----------------------------------------------------------------------------
# スケジューラー (Scheduler)
# -----------------------------------------------------------------------------
class RadioScheduler:
    """番組表を監視し、録音タスクをスケジュールするクラス。"""

    def __init__(self, config: RadioConfig, client: RadikoClient, recorder: RadioRecorder):
        self.config = config
        self.client = client
        self.recorder = recorder
        self.scheduled_ids: Set[str] = set() # 予約済みの番組IDを保持するセット（重複防止）
        self._lock = threading.Lock() # scheduled_idsへのアクセスを保護するためのロック

    def _on_record_complete(self, program_unique_id: str):
        """録音が完了したときに実行されるコールバック関数。"""
        with self._lock:
            logger.info(f"Removing finished task from schedule list: {program_unique_id}")
            # 予約済みリストから完了した番組のIDを削除
            self.scheduled_ids.discard(program_unique_id)

    def _schedule_task(self, program: RadioProgram):
        """一つの番組の録音をタイマーで予約する。"""
        now = datetime.datetime.now()

        # 既に終了した番組はスキップ
        if program.end_time < now:
            return

        with self._lock:
            # 既に予約済みの番組はスキップ
            if program.unique_id in self.scheduled_ids:
                return

            # 番組開始までの遅延時間（秒）を計算
            # 2秒早くタイマーを開始し、ロック取得の待機時間に充てることで、
            # 前の番組終了から次の番組開始までのラグを最小化する
            grace_period = 0
            delay = max(0, (program.start_time - now).total_seconds() - grace_period)
            logger.info(f"Scheduling '{program.title}' on {program.station_id} to start in {delay / 60:.2f} minutes.")

            # threading.Timerを使って、指定時間後に関数を実行する予約を行う
            timer = threading.Timer(
                delay,
                self.recorder.record_program,
                args=(program, self._on_record_complete)
            )
            timer.name = f"Recorder-{program.unique_id}"
            timer.daemon = True # メインスレッドが終了したらタイマーも終了させる
            timer.start() # タイマーを開始

            # 予約済みリストに番組IDを追加
            self.scheduled_ids.add(program.unique_id)

    def _check_and_schedule_programs(self):
        """設定された全放送局の番組表を取得し、タスクを予約する。"""
        logger.info("----- Starting program guide check -----")
        for station_id in self.config.stations_to_record:
            logger.info(f"Fetching schedule for {station_id}...")
            programs = self.client.fetch_programs_for_station(station_id)
            if programs:
                for program in programs:
                    self._schedule_task(program)
            time.sleep(1) # APIサーバーへの負荷を軽減するために少し待機

    def run(self):
        """スケジューラーのメインループを開始する。"""
        logger.info("Radio Scheduler started.")
        logger.info(f"Recording target stations: {', '.join(self.config.stations_to_record)}")

        try:
            # 無限ループで定期的に番組表をチェック
            while True:
                self._check_and_schedule_programs()
                logger.info(f"Next schedule check will be in {self.config.poll_interval / 60:.1f} minutes.")
                time.sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            # Ctrl+Cが押されたら、ループを抜けて正常に終了
            logger.info("Scheduler interrupted by user. Shutting down.")
        except Exception as e:
            # 予期せぬエラーが発生した場合のログ出力
            logger.exception(f"An unexpected error occurred in the main loop: {e}")

# -----------------------------------------------------------------------------
# メイン処理 (Main Execution)
# -----------------------------------------------------------------------------
def main():
    """スクリプトのエントリーポイント。"""
    global logger  # グローバル変数としてロガーを使用
    try:
        # 設定を読み込み
        config = RadioConfig.from_args()
    except (ValueError, FileNotFoundError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return

    # ログディレクトリが存在しなければ作成
    os.makedirs(config.log_dir, exist_ok=True)

    # ロガーの設定 (Logger Configuration) を修正
    log_filename = os.path.join(config.log_dir, "radio_recorder.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
        handlers=[logging.handlers.TimedRotatingFileHandler(
            filename=log_filename,
            when="midnight",
            interval=1,
            backupCount=7,  # 過去7日間のログを保持
            encoding='utf-8',
            utc=False
        )]
    )
    logger = logging.getLogger(__name__)

    # 各クラスをインスタンス化
    client = RadikoClient()
    recorder = RadioRecorder(config=config)
    scheduler = RadioScheduler(config=config, client=client, recorder=recorder)

    # スケジューラーを実行
    scheduler.run()

if __name__ == '__main__':
    main()
