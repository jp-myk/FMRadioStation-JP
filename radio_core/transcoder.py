import os
import subprocess
import threading
import time
from typing import Callable, Iterator

SAMPLE_RATE = 16000
CHANNELS = 1
_CHUNK = 4096
_MP3_BITRATE = "64k"  # 帯域とのバランス調整はここで変更

_FFMPEG_CMD = [
    "ffmpeg",
    "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
    "-i", "pipe:0",
    "-f", "mp3", "-b:a", _MP3_BITRATE, "-",
]


def stream_fd_as_mp3(rfd: int, startup_silence_sec: float = 3.0) -> Iterator[bytes]:
    """FIFO fd からリアルタイムで PCM を読み込み MP3 チャンクを生成する（ライブストリーム用）"""
    proc = subprocess.Popen(
        _FFMPEG_CMD,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    def feed():
        try:
            # GNU Radio 起動待ち(2〜5秒)の間、無音PCMを先行送信してブラウザのストールを防ぐ
            silence = bytes(int(SAMPLE_RATE * startup_silence_sec) * CHANNELS * 2)
            proc.stdin.write(silence)
            with os.fdopen(rfd, "rb") as f:
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    proc.stdin.write(chunk)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    threading.Thread(target=feed, daemon=True).start()
    try:
        yield from iter(lambda: proc.stdout.read(_CHUNK), b"")
    finally:
        proc.terminate()
        proc.wait()


def stream_growing_wav_as_mp3(
    wav_path: str, is_still_recording: Callable[[], bool]
) -> Iterator[bytes]:
    """成長中の WAV ファイルの PCM を MP3 チャンクとして生成する（タイムシフト用）"""
    WAV_HEADER_SIZE = 44

    proc = subprocess.Popen(
        _FFMPEG_CMD,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    def feed():
        try:
            with open(wav_path, "rb") as f:
                f.seek(WAV_HEADER_SIZE)
                while True:
                    chunk = f.read(_CHUNK)
                    if chunk:
                        proc.stdin.write(chunk)
                    elif is_still_recording():
                        time.sleep(0.05)
                    else:
                        break
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    threading.Thread(target=feed, daemon=True).start()
    try:
        yield from iter(lambda: proc.stdout.read(_CHUNK), b"")
    finally:
        proc.terminate()
        proc.wait()
