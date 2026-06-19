"""録音済み WAV ファイルをまとめて WebVTT 字幕へ変換するバッチヘルパ。

silero-vad の ``get_speech_timestamps`` を torch 非依存で再現した
``asr_core.vad.get_speech_timestamps`` でファイル全体の発話区間を検出し
（threshold / min_speech_duration_ms / min_silence_duration_ms / speech_pad_ms /
max_speech_duration_s は ``config/vad.yaml`` 由来）、各区間を parakeet-cli で認識して
WebVTT のキューとして書き出す。区間の開始・終了サンプルがそのまま再生時刻
（``audio.currentTime``）にアラインメントできる。

検出パラメータは ``asr_core.config.ASRConfig`` 経由で ``config/vad.yaml`` から読む。
parakeet-cli の実行は既存の ``ParakeetCppBackend`` を再利用する（バイナリ／モデル／言語の
env 解決・1 区間ごとの subprocess 呼び出し）。
"""
import os
import shutil

import numpy as np

from asr_core.asr.parakeet_cpp import ParakeetCppBackend
from asr_core.config import ASRConfig
from asr_core.vad import SileroVad, get_speech_timestamps
from asr_core.wav_io import read_wav_int16


def _resolve_bin(bin_path: str) -> str | None:
    if not bin_path:
        return None
    return shutil.which(bin_path)


def asr_batch_available(config: ASRConfig | None = None) -> bool:
    """字幕生成に必要な要素が揃っていれば True。

    parakeet-cli バイナリが解決でき、GGUF モデルが指定され、silero-vad の ONNX モデルが
    実在することを確認する（VAD は torch 非依存の自前実装＋同梱 onnx を使う）。
    """
    config = config or ASRConfig()
    return (
        _resolve_bin(config.parakeet_bin) is not None
        and bool(config.parakeet_model)
        and os.path.exists(config.vad_model_path)
    )


def _fmt_ts(sec: float) -> str:
    """秒 → WebVTT タイムスタンプ ``HH:MM:SS.mmm``（ミリ秒区切りはピリオド）。"""
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _write_vtt(cues: list[tuple[float, float, str]], vtt_path: str) -> None:
    """(start, end, text) 列を WebVTT として ``vtt_path`` へ原子的に書き出す。"""
    lines = ["WEBVTT", ""]
    for start, end, text in cues:
        text = text.strip()
        if not text:
            continue
        if end <= start:
            end = start + 0.5
        lines.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        lines.append(text)
        lines.append("")
    os.makedirs(os.path.dirname(vtt_path) or ".", exist_ok=True)
    tmp = vtt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, vtt_path)


def _detect_speech(samples: np.ndarray, sample_rate: int, config: ASRConfig) -> list[dict]:
    """torch 非依存の自前 get_speech_timestamps で発話区間を検出する。"""
    vad = SileroVad(config)
    return get_speech_timestamps(
        samples,
        vad,
        sample_rate=sample_rate,
        threshold=config.vad_threshold,
        min_speech_duration_ms=config.min_speech_ms,
        min_silence_duration_ms=config.silence_end_ms,
        speech_pad_ms=config.speech_pad_ms,
        max_speech_duration_s=config.max_segment_sec,
        window_size_samples=config.vad_frame_samples,
    )


def transcribe_wav_to_vtt(
    wav_path: str, vtt_path: str, config: ASRConfig | None = None
) -> None:
    """WAV を認識し WebVTT を ``vtt_path`` に書き出す（同期・完了までブロック）。

    バイナリ／モデル未設定や読み込み失敗時は例外を送出する。
    """
    config = config or ASRConfig()
    if _resolve_bin(config.parakeet_bin) is None:
        raise RuntimeError(f"ASR バイナリ '{config.parakeet_bin}' が見つかりません")
    if not config.parakeet_model:
        raise RuntimeError("PARAKEET_MODEL（GGUF パス）が未設定です")
    if not os.path.exists(wav_path):
        raise FileNotFoundError(wav_path)

    samples, sample_rate = read_wav_int16(wav_path)
    speech = _detect_speech(samples, sample_rate, config)

    backend = ParakeetCppBackend(config)
    cues: list[tuple[float, float, str]] = []
    for seg in speech:
        start, end = int(seg["start"]), int(seg["end"])
        chunk = samples[start:end]
        if chunk.shape[0] == 0:
            continue
        text = backend.transcribe(chunk, sample_rate)
        if text:
            cues.append((start / sample_rate, end / sample_rate, text))
    _write_vtt(cues, vtt_path)
