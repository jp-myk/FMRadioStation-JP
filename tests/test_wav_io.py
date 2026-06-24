import numpy as np

from fm_radio_station.asr_core.wav_io import (
    pcm_bytes_to_int16,
    read_wav_int16,
    write_wav_bytes,
    write_wav_file,
)


def test_int16_wav_roundtrip(tmp_path):
    samples = (np.sin(np.arange(16000) / 10.0) * 10000).astype(np.int16)
    path = tmp_path / "x.wav"
    write_wav_file(str(path), samples, 16000)
    back, sr = read_wav_int16(str(path))
    assert sr == 16000
    np.testing.assert_array_equal(back, samples)


def test_write_wav_bytes_has_riff_header():
    data = write_wav_bytes(np.zeros(16, dtype=np.int16), 16000)
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"


def test_pcm_bytes_to_int16_drops_odd_byte():
    samples = np.array([1, 2, 3], dtype="<i2")
    raw = samples.tobytes() + b"\x00"  # 末尾に半端な 1 バイト
    out = pcm_bytes_to_int16(raw)
    np.testing.assert_array_equal(out, samples)
