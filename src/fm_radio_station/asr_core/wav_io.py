"""int16 PCM サンプル列と WAV ファイルの相互変換（16kHz/mono/16bit 既定）。

``radio_scheduler_webui.py`` の ``make_infinite_wav_header`` はストリーミング用に
データサイズを擬似無限に書くが、こちらは ASR に 1 セグメントを渡すための
「実サイズ付き有限 WAV」を書く点が異なる。
"""
import io
import wave

import numpy as np


def write_wav_bytes(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    """int16 のモノラルサンプル列を WAV バイト列に変換する。"""
    pcm = np.ascontiguousarray(samples, dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16bit
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def write_wav_file(path: str, samples: np.ndarray, sample_rate: int = 16000) -> None:
    """int16 のモノラルサンプル列を WAV ファイルに書き出す。"""
    with open(path, "wb") as f:
        f.write(write_wav_bytes(samples, sample_rate))


def read_wav_int16(path: str) -> tuple[np.ndarray, int]:
    """WAV を int16 モノラルサンプル列として読み込む（多チャンネルは先頭chを使用）。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        if sampwidth != 2:
            raise ValueError(f"16bit PCM のみ対応: sampwidth={sampwidth}")
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16)
    if n_channels > 1:
        data = data.reshape(-1, n_channels)[:, 0]
    return data, sr


def pcm_bytes_to_int16(chunk: bytes) -> np.ndarray:
    """生の little-endian int16 PCM バイト列を numpy 配列に変換する。

    奇数バイト（半端なサンプル）は末尾を切り捨てる。
    """
    if len(chunk) & 1:
        chunk = chunk[:-1]
    return np.frombuffer(chunk, dtype="<i2")
