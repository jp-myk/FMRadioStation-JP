"""ASR パイプラインの設定。

WebUI 側からは ``ASRConfig()`` で既定値、あるいは個別フィールドを上書きして使う。
VAD パラメータは ``config/vad.yaml`` から読み込む（無ければ組み込み既定値に
フォールバック）。parakeet.cpp のバイナリ・モデルパスは環境変数で解決する。
"""
import os
from dataclasses import dataclass, field

import yaml

# 本モジュール（asr_core/config.py）から見た既定の VAD 設定ファイル。
# .. はリポジトリ直下（コンテナでは /app）の config/ を指す。
_DEFAULT_VAD_CONFIG = os.path.join(
    os.path.dirname(__file__), "..", "config", "vad.yaml"
)

# config/vad.yaml が無い／読めないときの組み込み既定値（キーは vad.yaml と対応）。
_VAD_DEFAULTS = {
    "threshold": 0.4,
    "min_speech_duration_ms": 250,
    "min_silence_duration_ms": 100,
    "speech_pad_ms": 30,
    "frame_samples": 512,
    "max_speech_duration_s": 18,
    "context_sec": 0.5,
}


def _load_vad_config() -> dict:
    """``config/vad.yaml`` の ``vad:`` セクションを読み込む。

    ファイルが無い／壊れている場合は ``_VAD_DEFAULTS`` にフォールバックする
    （VAD 設定は必須ではなく、欠けても既定値で動かしたいため）。
    """
    path = os.environ.get("VAD_CONFIG", _DEFAULT_VAD_CONFIG)
    cfg = dict(_VAD_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg.update(data.get("vad") or {})
    except (OSError, yaml.YAMLError):
        pass
    return cfg


_VAD = _load_vad_config()


@dataclass
class ASRConfig:
    # --- 音声フォーマット（既存パイプラインに合わせて 16k/mono/int16 固定） ---
    sample_rate: int = 16000

    # --- 入力チャンク ---
    input_chunk_sec: float = 1.0  # WebUI が push_audio する単位（参考値）

    # --- VAD（既定値は config/vad.yaml 由来。個別に上書きも可） ---
    vad_frame_samples: int = field(default_factory=lambda: int(_VAD["frame_samples"]))
    vad_threshold: float = field(default_factory=lambda: float(_VAD["threshold"]))

    # --- セグメント化（無音終端 / 最大長 / 最小長 / 余白 / 文脈付与） ---
    # silence_end_ms ← min_silence_duration_ms（この長さ以上の無音で発話を区切る）
    silence_end_ms: int = field(default_factory=lambda: int(_VAD["min_silence_duration_ms"]))
    # min_speech_ms ← min_speech_duration_ms（これ未満は雑音として破棄）
    min_speech_ms: int = field(default_factory=lambda: int(_VAD["min_speech_duration_ms"]))
    # max_segment_sec ← max_speech_duration_s（1 セグメント上限）
    max_segment_sec: float = field(default_factory=lambda: float(_VAD["max_speech_duration_s"]))
    # speech_pad_ms: 検出区間の前後に付与するパディング余白
    speech_pad_ms: int = field(default_factory=lambda: int(_VAD["speech_pad_ms"]))
    context_sec: float = field(default_factory=lambda: float(_VAD["context_sec"]))

    # --- ASR バックエンド ---
    backend: str = "parakeet_cpp"
    parakeet_bin: str = field(
        default_factory=lambda: os.environ.get("PARAKEET_CPP_BIN", "parakeet-cli")
    )
    parakeet_model: str = field(
        default_factory=lambda: os.environ.get("PARAKEET_MODEL", "")
    )
    parakeet_language: str = "ja"
    asr_timeout_sec: float = 120.0  # 1 セグメント推論のタイムアウト

    # --- VAD モデル ---
    vad_model_path: str = field(
        default_factory=lambda: os.environ.get(
            "SILERO_VAD_ONNX",
            os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx"),
        )
    )

    # --- 派生値 ---
    @property
    def frame_sec(self) -> float:
        return self.vad_frame_samples / self.sample_rate

    @property
    def silence_end_frames(self) -> int:
        return max(1, round((self.silence_end_ms / 1000.0) / self.frame_sec))

    @property
    def min_speech_frames(self) -> int:
        return max(1, round((self.min_speech_ms / 1000.0) / self.frame_sec))

    @property
    def max_segment_samples(self) -> int:
        return int(self.max_segment_sec * self.sample_rate)

    @property
    def context_samples(self) -> int:
        return int(self.context_sec * self.sample_rate)

    @property
    def speech_pad_samples(self) -> int:
        return int((self.speech_pad_ms / 1000.0) * self.sample_rate)
