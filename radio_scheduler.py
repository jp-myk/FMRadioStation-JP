#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import datetime
import json
import threading
import logging
import logging.handlers
import argparse
from dataclasses import dataclass, field
from typing import List, Set, Callable

try:
    from radio_core.receivers import FMReceiver, AMReceiver
except ImportError as e:
    print(f"Error: GNU Radio or osmosdr library is not installed: {e}")
    sys.exit(1)

from radio_core.stations import STATIONS, get_station
from radio_core.radiko import RadikoClient, RadioProgram
from radio_core.utils import sanitize_filename, JST


# -----------------------------------------------------------------------------
# データクラスの定義 (Data Classes)
# -----------------------------------------------------------------------------
@dataclass
class RadioConfig:
    """スクリプト全体の設定を保持するデータクラス。"""
    poll_interval: int = 3600 * 6
    rec_dir: str = "radio_recordings"
    stations_to_record: List[str] = field(default_factory=list)
    sdr_sample_rate: float = 2.4e6
    audio_rate: int = 48000
    bit_rate: int = 16
    sdr_gain: int = 40
    log_dir: str = "log"

    @classmethod
    def from_args(cls) -> 'RadioConfig':
        """コマンドライン引数を解析して設定オブジェクトを生成する。"""
        valid_ids = ", ".join(s["id"] for s in STATIONS)
        parser = argparse.ArgumentParser(
            description="A script to schedule radio recordings using GNU Radio."
        )
        parser.add_argument("--poll-interval", type=int, default=3600 * 6,
                            help="Interval in seconds to check the program guide.")
        parser.add_argument("--rec-dir", type=str, default="radio_recordings",
                            help="Directory to save recorded files.")
        parser.add_argument("--stations", type=str, required=True,
                            help=f"Comma-separated Radiko station IDs to record. Available: {valid_ids}")
        parser.add_argument("--sample-rate", type=float, default=2.4e6,
                            help="SDR sample rate.")
        parser.add_argument("--audio-rate", type=int, default=24000,
                            help="Output audio sample rate.")
        parser.add_argument("--bit-rate", type=int, choices=[16, 24, 32], default=16,
                            help="Output WAV bit rate.")
        parser.add_argument("--gain", type=int, default=40, help="SDR gain.")
        parser.add_argument("--log-dir", type=str, default="log",
                            help="Directory to save log files.")
        args = parser.parse_args()

        stations = [s.strip().upper() for s in args.stations.split(",")]
        valid_set = {s["id"] for s in STATIONS}
        for station_id in stations:
            if station_id not in valid_set:
                raise ValueError(f"Invalid station ID: '{station_id}'. Available: {valid_ids}")

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
# レコーダー (Recorder)
# -----------------------------------------------------------------------------
class RadioRecorder:
    """録音処理とSDRデバイスリソースの管理を行うクラス。"""

    def __init__(self, config: RadioConfig):
        self.config = config
        self._lock = threading.Lock()
        os.makedirs(self.config.rec_dir, exist_ok=True)

    def record_program(self, program: RadioProgram, on_complete: Callable[[str], None]):
        """一つの番組を録音する。独立したスレッドで実行されることを想定。"""
        self._lock.acquire(blocking=True)

        station_info = get_station(program.station_id)
        if station_info is None:
            logger.error(f"Unknown station ID: {program.station_id}")
            self._lock.release()
            on_complete(program.unique_id)
            return

        receiver = None
        wav_output_path = None
        try:
            prefix = (
                f"{program.start_time.strftime('%Y%m%d-%H%M')}"
                f"_{program.station_id}"
                f"_{sanitize_filename(program.title)}"
            )
            wav_output_path = os.path.join(self.config.rec_dir, f"{prefix}.wav")
            json_output_path = os.path.join(self.config.rec_dir, f"{prefix}.json")

            logger.info(
                f"Recording '{program.title}' on {station_info['name']} "
                f"({station_info['freq'] / 1e6} MHz)"
            )
            logger.info(f"Output file: {wav_output_path}")

            if station_info["type"] == "fm":
                receiver = FMReceiver(
                    freq=station_info["freq"],
                    sdr_sample_rate=self.config.sdr_sample_rate,
                    output_file=wav_output_path,
                    desired_audio_rate=self.config.audio_rate,
                    bit_rate=self.config.bit_rate,
                    gain=self.config.sdr_gain,
                )
            elif station_info["type"] == "am":
                receiver = AMReceiver(
                    freq=station_info["freq"],
                    sdr_sample_rate=self.config.sdr_sample_rate,
                    output_file=wav_output_path,
                    desired_audio_rate=self.config.audio_rate,
                    bit_rate=self.config.bit_rate,
                    gain=self.config.sdr_gain,
                )
            else:
                raise ValueError(f"Unknown station type: {station_info['type']}")

            receiver.start()
            time.sleep(program.duration_sec)

        except Exception as e:
            logger.error(f"An error occurred during recording of '{program.title}': {e}")
            if wav_output_path and os.path.exists(wav_output_path):
                os.remove(wav_output_path)
        finally:
            if receiver:
                logger.info(f"Stopping recording for '{program.title}'.")
                receiver.stop()
                receiver.wait()
                json.dump(
                    program.detail,
                    open(json_output_path, "w", encoding="utf-8"),
                    ensure_ascii=False,
                    indent=2,
                )
            self._lock.release()
            logger.info(f"Lock released. Recording task finished for: {program.title}")
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
        self.scheduled_ids: Set[str] = set()
        self._lock = threading.Lock()

    def _on_record_complete(self, program_unique_id: str):
        with self._lock:
            logger.info(f"Removing finished task from schedule list: {program_unique_id}")
            self.scheduled_ids.discard(program_unique_id)

    def _schedule_task(self, program: RadioProgram):
        now = datetime.datetime.now(JST)

        if program.end_time < now:
            return

        with self._lock:
            if program.unique_id in self.scheduled_ids:
                return

            delay = max(0, (program.start_time - now).total_seconds())
            logger.info(
                f"Scheduling '{program.title}' on {program.station_id} "
                f"to start in {delay / 60:.2f} minutes."
            )

            timer = threading.Timer(
                delay,
                self.recorder.record_program,
                args=(program, self._on_record_complete),
            )
            timer.name = f"Recorder-{program.unique_id}"
            timer.daemon = True
            timer.start()

            self.scheduled_ids.add(program.unique_id)

    def _check_and_schedule_programs(self):
        logger.info("----- Starting program guide check -----")
        for station_id in self.config.stations_to_record:
            logger.info(f"Fetching schedule for {station_id}...")
            programs = self.client.fetch_programs_for_station(station_id)
            if programs:
                for program in programs:
                    self._schedule_task(program)
            time.sleep(1)

    def run(self):
        logger.info("Radio Scheduler started.")
        logger.info(f"Recording target stations: {', '.join(self.config.stations_to_record)}")
        try:
            while True:
                self._check_and_schedule_programs()
                logger.info(
                    f"Next schedule check will be in {self.config.poll_interval / 60:.1f} minutes."
                )
                time.sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            logger.info("Scheduler interrupted by user. Shutting down.")
        except Exception as e:
            logger.exception(f"An unexpected error occurred in the main loop: {e}")


# -----------------------------------------------------------------------------
# メイン処理
# -----------------------------------------------------------------------------
def main():
    global logger
    try:
        config = RadioConfig.from_args()
    except (ValueError, FileNotFoundError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return

    os.makedirs(config.log_dir, exist_ok=True)

    log_filename = os.path.join(config.log_dir, "radio_recorder.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
        handlers=[
            logging.handlers.TimedRotatingFileHandler(
                filename=log_filename,
                when="midnight",
                interval=1,
                backupCount=7,
                encoding="utf-8",
                utc=False,
            )
        ],
    )
    logger = logging.getLogger(__name__)

    client = RadikoClient()
    recorder = RadioRecorder(config=config)
    scheduler = RadioScheduler(config=config, client=client, recorder=recorder)
    scheduler.run()


if __name__ == "__main__":
    main()
