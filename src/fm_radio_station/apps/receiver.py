#!/usr/bin/env python3
"""
FM/AM Radio Receiver — SDR動作確認・録音検証CLI
------------------------------------------------
SDRを用いてFM/AM局を受信し、WAVファイルに記録します。
局IDはRadiko正式IDを使用します。

利用可能な局ID例:
  FM: FMT (TOKYO FM 80.0MHz), FMJ (J-WAVE 81.3MHz), JOAK-FM (NHK-FM 82.5MHz)
  AM: JOAK (NHKラジオ第1 594kHz), JOAB (NHKラジオ第2 693kHz)

例:
  python3 radio_receiver.py --mode fm --station FMT --duration 60 --output output.wav
  python3 radio_receiver.py --mode fm --freq 80e6 --duration 60 --output output.wav
"""
import sys
import time
import argparse

try:
    from fm_radio_station.radio_core.receivers import FMReceiver, AMReceiver
except ImportError as e:
    print(f"Error: GNU Radio or osmosdr library is not installed: {e}")
    sys.exit(1)

from fm_radio_station.radio_core.stations import STATIONS, get_station


def main():
    """Parse CLI arguments and record a single FM/AM broadcast to a WAV file."""
    fm_stations = [s for s in STATIONS if s["type"] == "fm"]
    am_stations = [s for s in STATIONS if s["type"] == "am"]

    parser = argparse.ArgumentParser(
        description="SDRを用いてFM/AM局を受信し、WAVファイルに記録するプログラム"
    )
    parser.add_argument("--mode", choices=["fm", "am"], required=True,
                        help="受信モードを選択: fm または am")
    parser.add_argument("--station", type=str, default=None,
                        help=(
                            "受信局のID（Radiko正式ID）。"
                            f"【FM】: {', '.join(s['id'] for s in fm_stations)}  "
                            f"/ 【AM】: {', '.join(s['id'] for s in am_stations)}"
                        ))
    parser.add_argument("--freq", type=float, default=None,
                        help="受信周波数 (Hz)。--station 未指定時に使用 (例: 80e6)")
    parser.add_argument("--sample_rate", type=float, default=2.4e6,
                        help="SDR サンプルレート (Hz) (デフォルト: 2.4e6)")
    parser.add_argument("--duration", type=float, required=True,
                        help="録音時間 (秒)")
    parser.add_argument("--output", type=str, required=True,
                        help="出力WAVファイルパス")
    parser.add_argument("--gain", type=float, default=40,
                        help="SDRのRFゲイン (デフォルト: 40)")
    parser.add_argument("--audio_rate", type=float, default=16000,
                        help="出力オーディオサンプルレート (デフォルト: 16000)")
    parser.add_argument("--bit_rate", type=int, choices=[16, 24, 32], default=16,
                        help="出力WAVビット深度 (デフォルト: 16)")
    args = parser.parse_args()

    freq = None
    station_name = args.station or f"{args.freq} Hz"

    if args.station:
        station = get_station(args.station)
        if station is None:
            print(f"エラー: 未知の局ID '{args.station}'。--freq で周波数を直接指定するか、"
                  f"有効なIDを指定してください。", file=sys.stderr)
            sys.exit(1)
        freq = station["freq"]
        station_name = f"{station['name']} ({station['id']})"
    elif args.freq is not None:
        freq = args.freq
    else:
        print("エラー: --station または --freq のいずれかを指定してください。", file=sys.stderr)
        sys.exit(1)

    if args.mode == "fm":
        receiver = FMReceiver(freq, args.sample_rate, args.output, args.audio_rate, args.bit_rate, args.gain)
    else:
        receiver = AMReceiver(freq, args.sample_rate, args.output, args.audio_rate, args.bit_rate, args.gain)

    print(f"録音開始: {station_name}、周波数 {freq/1e6:.3f} MHz、モード {args.mode}")
    receiver.start()
    time.sleep(args.duration)
    receiver.stop()
    receiver.wait()
    print(f"録音終了。ファイル: {args.output}")


if __name__ == '__main__':
    main()
