#!/usr/bin/env python3
"""
FM Radio Recorder for Tokyo FM Stations
-----------------------------------------
このプログラムは SDR を用いて東京域内のFMラジオ局を受信し、WAVファイルに記録します。

利用可能な局の例:
  - Tokyo_FM:      80.0 MHz
  - J-WAVE:        81.3 MHz
  - InterFM897:    89.7 MHz
  - TBS:           91.1 MHz

例:
  python3 radio_recorder.py --station Tokyo_FM --duration 60 --gain 30 --output output.wav
"""
#!/usr/bin/env python3
import sys
import time
import argparse
import math
import fractions
from gnuradio import gr, analog, filter, blocks
import osmosdr

# WIN_HAMMING が定義されていなければ設定
if not hasattr(filter.firdes, 'WIN_HAMMING'):
    filter.firdes.WIN_HAMMING = 1

class FMReceiver(gr.top_block):
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate, bit_rate, gain=40):
        gr.top_block.__init__(self, "FM Receiver")
        self.freq = freq
        self.sdr_sample_rate = sdr_sample_rate
        self.output_file = output_file

        # LPFによる初期帯域調整
        decim1 = max(1, int(round(sdr_sample_rate / (desired_audio_rate * 10))))
        new_rate = sdr_sample_rate / decim1
        audio_decim = max(1, int(round(new_rate / desired_audio_rate)))
        initial_audio_rate = new_rate / audio_decim

        # SDRソース
        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        # LPFフィルタ（FM信号抽出）
        self.lpf = filter.fir_filter_ccf(
            decim1,
            filter.firdes.low_pass(
                1.0, sdr_sample_rate, 50e3, 10e3, filter.firdes.WIN_HAMMING, 6.76
            )
        )

        # FM復調
        self.wbfm = analog.wfm_rcv(quad_rate=new_rate, audio_decimation=audio_decim)

        # デエンファシスフィルタ（75μs）
        dt = 1.0 / initial_audio_rate
        alpha = dt / (75e-6 + dt)
        self.deemph = filter.single_pole_iir_filter_ff(alpha)

        # Rational Resamplerで最終レート補正
        use_resampler = abs(initial_audio_rate - desired_audio_rate) > 1
        if use_resampler:
            frac = fractions.Fraction(int(round(desired_audio_rate)), int(round(initial_audio_rate))).limit_denominator(50)
            interpolation = frac.numerator
            decimation = frac.denominator
            self.resampler = filter.rational_resampler_fff(
                interpolation=interpolation, decimation=decimation, taps=[], fractional_bw=0.0
            )
            final_rate = desired_audio_rate
        else:
            final_rate = initial_audio_rate


        # FIRフィルタ（高音質化）
        self.hq_filter = filter.fir_filter_fff(
            1,
            filter.firdes.low_pass(
                1.0, final_rate, final_rate/2 * 0.9, final_rate * 0.05, filter.firdes.WIN_HAMMING, 6.76
            )
        )

        # WAV File Sink（ユーザ指定ビット深度）
        wav_format = blocks.FORMAT_PCM_16 if bit_rate == 16 else blocks.FORMAT_PCM_32
        self.wav_sink = blocks.wavfile_sink(output_file, 1, int(final_rate), blocks.FORMAT_WAV, wav_format, False)

        # 接続
        if use_resampler:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.resampler, self.hq_filter, self.wav_sink)
        else:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.hq_filter, self.wav_sink)


class AMReceiver(gr.top_block):
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate, bit_rate, gain=40):
        gr.top_block.__init__(self, "AM Receiver")
        self.freq = freq
        self.sdr_sample_rate = sdr_sample_rate
        self.output_file = output_file

        # SDRソース
        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(40)

        # LPFフィルタ（AM帯域抽出）
        decim = max(1, int(round(sdr_sample_rate / desired_audio_rate)))
        new_rate = sdr_sample_rate / decim
        self.lpf = filter.fir_filter_ccf(
            decim,
            filter.firdes.low_pass(
                1.0, sdr_sample_rate, 5e3, 1e3, filter.firdes.WIN_HAMMING, 6.76
            )
        )

        # エンベロープ検出
        self.env_det = blocks.complex_to_mag(1)

        # 平滑化フィルタ
        dt = 1.0 / new_rate
        tau = 1.0 / (2 * math.pi * 5000)
        alpha = dt / (tau + dt)
        self.smooth = filter.single_pole_iir_filter_ff(alpha)

        # FIRフィルタ（高音質化）
        cutoff_am = min(5e3, desired_audio_rate/2 * 0.9)
        self.hq_filter = filter.fir_filter_fff(
            1,
            filter.firdes.low_pass(
                1.0, desired_audio_rate, cutoff_am, desired_audio_rate * 0.05, filter.firdes.WIN_HAMMING, 6.76
            )
        )

        # WAV File Sink
        wav_format = blocks.FORMAT_PCM_16 if bit_rate == 16 else blocks.FORMAT_PCM_32
        self.wav_sink = blocks.wavfile_sink(output_file, 1, int(desired_audio_rate), blocks.FORMAT_WAV, wav_format, False)

        # 接続
        self.connect(self.source, self.lpf, self.env_det, self.smooth, self.hq_filter, self.wav_sink)


def main():
    # FM局のマッピング（Hz単位）
    fm_station_frequencies = {
        "Tokyo_FM":    80e6,    # 80.0 MHz
        "J-WAVE":      81.3e6,  # 81.3 MHz
        "InterFM897":  89.7e6,  # 89.7 MHz
        "TBS":         91.1e6,  # 91.1 MHz
    }
    # AM局のマッピング（Hz単位：AM信号はkHz帯）
    am_station_frequencies = {
        "TBS_AM":            954e3,   # 954 kHz
        "Nippon_Broadcast": 1242e3,   # 1242 kHz
    }

    parser = argparse.ArgumentParser(
        description="東京域内のFM／AMラジオ局をSDRで受信し、WAVファイルに記録するプログラム"
    )
    parser.add_argument("--mode", choices=["fm", "am"], default="fm", required=True,
                        help="受信モードを選択: fm (デフォルト) または am")
    parser.add_argument("--station", type=str, default=None,
                        help="受信局の名前（モードに応じて）。" +
                             "【FM】：" + ", ".join(fm_station_frequencies.keys()) +
                             "  / 【AM】：" + ", ".join(am_station_frequencies.keys()))
    parser.add_argument("--freq", type=float,
                        help="受信周波数 (Hz) (例: Tokyo FM は80e6)")
    parser.add_argument("--sample_rate", type=float, default=2.4e6,
                        help="SDR サンプルレート (Hz) (例: 2.4e6)")
    parser.add_argument("--duration", type=float, required=True,
                        help="録音時間 (秒)")
    parser.add_argument("--output", type=str, required=True,
                        help="出力WAVファイルパス")
    parser.add_argument("--gain", type=float, default=40,
                        help="SDRのRFゲイン（デフォルトは30）")
    parser.add_argument("--audio_rate", type=float, default=16000)
    parser.add_argument("--bit_rate", type=int, choices=[16, 24, 32], default=16)
    args = parser.parse_args()

    if args.mode == "fm":
        freq = fm_station_frequencies.get(args.station, args.freq)
        receiver = FMReceiver(freq, args.sample_rate, args.output, args.audio_rate, args.bit_rate, args.gain)
    elif args.mode == "am":
        freq = am_station_frequencies.get(args.station, args.freq)
        receiver = AMReceiver(freq, args.sample_rate, args.output, args.audio_rate, args.bit_rate, args.gain)
    else:
        sys.stderr.write("エラー: サポートされていないモードです。\n")
        sys.exit(1)

    print("録音開始: ラジオ局 {},  周波数 {} Hz、モード {} で録音中…".format(args.station, freq, args.mode))
    receiver.start()
    time.sleep(args.duration)
    receiver.stop()
    receiver.wait()
    print("録音終了。ファイル: {}".format(args.output))

if __name__ == '__main__':
    main()
