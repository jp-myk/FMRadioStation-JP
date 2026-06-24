#!/usr/bin/env python3
import fractions

try:
    from gnuradio import gr, analog, filter as gr_filter, blocks
    try:
        import osmosdr
    except ImportError:
        from gnuradio import soapy as _soapy

        class _SoapySourceAdapter:
            """Small osmosdr.source-compatible wrapper around GNU Radio's Soapy source."""

            def __init__(self, args=""):
                dev_args = "driver=rtlsdr"
                if args and "rtl" in args.lower():
                    dev_args = args
                self._source = _soapy.source(dev_args, "fc32", 1, "", "", [""], [""])

            def set_sample_rate(self, sample_rate):
                self._source.set_sample_rate(0, sample_rate)

            def set_center_freq(self, freq):
                self._source.set_frequency(0, freq)

            def set_gain(self, gain):
                self._source.set_gain(0, gain)

            def __getattr__(self, name):
                return getattr(self._source, name)

        class _SoapyOsmoCompat:
            @staticmethod
            def source(args=""):
                return _SoapySourceAdapter(args)

        osmosdr = _SoapyOsmoCompat()
    if not hasattr(gr_filter.firdes, 'WIN_HAMMING'):
        gr_filter.firdes.WIN_HAMMING = 1
except ImportError as e:
    raise ImportError(f"GNU Radio or osmosdr library is not installed: {e}") from e


class FMReceiver(gr.top_block):
    """FM放送受信・WAVファイル出力フローグラフ。"""
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate, bit_rate, gain):
        gr.top_block.__init__(self, "FM Receiver")

        decim1 = max(1, int(round(sdr_sample_rate / (desired_audio_rate * 10))))
        new_rate = sdr_sample_rate / decim1
        audio_decim = max(1, int(round(new_rate / desired_audio_rate)))
        initial_audio_rate = new_rate / audio_decim

        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        self.lpf = gr_filter.fir_filter_ccf(
            decim1,
            gr_filter.firdes.low_pass(1.0, sdr_sample_rate, 50e3, 10e3, gr_filter.firdes.WIN_HAMMING, 6.76)
        )
        self.wbfm = analog.wfm_rcv(quad_rate=new_rate, audio_decimation=audio_decim)

        dt = 1.0 / initial_audio_rate
        alpha = dt / (75e-6 + dt)  # 日本のFM放送は75μs
        self.deemph = gr_filter.single_pole_iir_filter_ff(alpha)

        use_resampler = abs(initial_audio_rate - desired_audio_rate) > 1
        if use_resampler:
            frac = fractions.Fraction(desired_audio_rate, int(initial_audio_rate)).limit_denominator(100)
            self.resampler = gr_filter.rational_resampler_fff(
                interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0
            )

        self.hq_filter = gr_filter.fir_filter_fff(
            1,
            gr_filter.firdes.low_pass(
                1.0, desired_audio_rate,
                desired_audio_rate / 2 * 0.9, desired_audio_rate * 0.05,
                gr_filter.firdes.WIN_HAMMING, 6.76
            )
        )

        wav_format = {16: blocks.FORMAT_PCM_16, 24: blocks.FORMAT_PCM_24, 32: blocks.FORMAT_PCM_32}.get(bit_rate)
        if wav_format is None:
            raise ValueError(f"Unsupported bit rate: {bit_rate}")
        self.wav_sink = blocks.wavfile_sink(
            output_file, 1, int(desired_audio_rate), blocks.FORMAT_WAV, wav_format, False
        )

        if use_resampler:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.resampler, self.hq_filter, self.wav_sink)
        else:
            self.connect(self.source, self.lpf, self.wbfm, self.deemph, self.hq_filter, self.wav_sink)


class AMReceiver(gr.top_block):
    """AM放送受信・WAVファイル出力フローグラフ（rational_resampler + hq_filter）。"""
    def __init__(self, freq, sdr_sample_rate, output_file, desired_audio_rate, bit_rate, gain):
        gr.top_block.__init__(self, "AM Receiver")

        decim = int(sdr_sample_rate / (desired_audio_rate * 4))

        self.source = osmosdr.source(args="numchan=1")
        self.source.set_sample_rate(sdr_sample_rate)
        self.source.set_center_freq(freq)
        self.source.set_gain(gain)

        self.lpf = gr_filter.fir_filter_ccf(
            decim,
            gr_filter.firdes.low_pass(1.0, sdr_sample_rate, 5e3, 1e3, gr_filter.firdes.WIN_HAMMING, 6.76)
        )
        self.demod = blocks.complex_to_mag(1)

        resample_rate = sdr_sample_rate / decim
        frac = fractions.Fraction(desired_audio_rate, int(resample_rate)).limit_denominator(100)
        self.resampler = gr_filter.rational_resampler_fff(
            interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0
        )

        self.hq_filter = gr_filter.fir_filter_fff(
            1,
            gr_filter.firdes.low_pass(
                1.0, desired_audio_rate,
                min(5e3, desired_audio_rate / 2 * 0.9), desired_audio_rate * 0.05,
                gr_filter.firdes.WIN_HAMMING, 6.76
            )
        )

        wav_format = {16: blocks.FORMAT_PCM_16, 24: blocks.FORMAT_PCM_24, 32: blocks.FORMAT_PCM_32}.get(bit_rate)
        if wav_format is None:
            raise ValueError(f"Unsupported bit rate: {bit_rate}")
        self.wav_sink = blocks.wavfile_sink(
            output_file, 1, int(desired_audio_rate), blocks.FORMAT_WAV, wav_format, False
        )

        self.connect(self.source, self.lpf, self.demod, self.resampler, self.hq_filter, self.wav_sink)


class StreamingFMReceiver(gr.top_block):
    """FMReceiverと同じパイプラインだが出力をFIFOへの生int16 PCMにする。"""
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

        self.lpf = gr_filter.fir_filter_ccf(
            decim1,
            gr_filter.firdes.low_pass(1.0, sdr_sample_rate, 50e3, 10e3, gr_filter.firdes.WIN_HAMMING, 6.76)
        )
        self.wbfm = analog.wfm_rcv(quad_rate=new_rate, audio_decimation=audio_decim)

        dt = 1.0 / initial_audio_rate
        alpha = dt / (75e-6 + dt)
        self.deemph = gr_filter.single_pole_iir_filter_ff(alpha)

        use_resampler = abs(initial_audio_rate - desired_audio_rate) > 1
        if use_resampler:
            frac = fractions.Fraction(
                int(round(desired_audio_rate)), int(round(initial_audio_rate))
            ).limit_denominator(50)
            self.resampler = gr_filter.rational_resampler_fff(
                interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0.0
            )
            final_rate = desired_audio_rate
        else:
            final_rate = initial_audio_rate

        self.hq_filter = gr_filter.fir_filter_fff(
            1,
            gr_filter.firdes.low_pass(
                1.0, final_rate, final_rate / 2 * 0.9, final_rate * 0.05,
                gr_filter.firdes.WIN_HAMMING, 6.76
            )
        )

        self.converter = blocks.float_to_short(1, 32767)
        self.file_sink = blocks.file_sink(gr.sizeof_short, fifo_path)
        self.file_sink.set_unbuffered(True)

        if use_resampler:
            self.connect(
                self.source, self.lpf, self.wbfm, self.deemph,
                self.resampler, self.hq_filter, self.converter, self.file_sink
            )
        else:
            self.connect(
                self.source, self.lpf, self.wbfm, self.deemph,
                self.hq_filter, self.converter, self.file_sink
            )

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

        self.lpf = gr_filter.fir_filter_ccf(
            decim,
            gr_filter.firdes.low_pass(1.0, sdr_sample_rate, 5e3, 1e3, gr_filter.firdes.WIN_HAMMING, 6.76)
        )
        self.demod = blocks.complex_to_mag(1)

        resample_rate = sdr_sample_rate / decim
        frac = fractions.Fraction(
            int(round(desired_audio_rate)), int(round(resample_rate))
        ).limit_denominator(100)
        self.resampler = gr_filter.rational_resampler_fff(
            interpolation=frac.numerator, decimation=frac.denominator, taps=[], fractional_bw=0
        )

        final_rate = desired_audio_rate
        self.hq_filter = gr_filter.fir_filter_fff(
            1,
            gr_filter.firdes.low_pass(
                1.0, final_rate, final_rate / 2 * 0.9, final_rate * 0.05,
                gr_filter.firdes.WIN_HAMMING, 6.76
            )
        )

        self.converter = blocks.float_to_short(1, 32767)
        self.file_sink = blocks.file_sink(gr.sizeof_short, fifo_path)
        self.file_sink.set_unbuffered(True)

        self.connect(
            self.source, self.lpf, self.demod, self.resampler,
            self.hq_filter, self.converter, self.file_sink
        )

        self._sample_rate = int(final_rate)
