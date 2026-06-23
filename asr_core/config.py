"""ASR パイプラインの設定。

WebUI 側からは ``ASRConfig()`` で既定値、あるいは個別フィールドを上書きして使う。
VAD パラメータは ``config/vad.yaml`` から読み込む（無ければ組み込み既定値に
フォールバック）。parakeet.cpp のバイナリ・モデルパスは環境変数で解決する。
"""
import copy
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


# 本モジュールから見た既定の ASR 設定ファイル（config/asr.yaml）。
_DEFAULT_ASR_CONFIG = os.path.join(
    os.path.dirname(__file__), "..", "config", "asr.yaml"
)

# config/asr.yaml が無い／読めないときの組み込み既定値（キーは asr.yaml と対応）。
# model: 使用モデル名（models のキー）。models: モデルごとのプロファイル。
# 各プロファイルは backend を宣言する（省略時は parakeet_cpp）。
# - parakeet_cpp: mudler/parakeet.cpp の parakeet-cli。filename(gguf)/decoder/language。
#   parakeet-tdt は日本語専用 TDT/CTC、nemotron は多言語 RNNT ストリーミング。
# - llama_mtmd: llama.cpp の llama-mtmd-cli（音声マルチモーダル）。filename(本体 gguf)/
#   mmproj(音声エンコーダ gguf)/language/prompt。qwen3-asr は Alibaba の transformers 系 ASR を
#   ggml-org/Qwen3-ASR-1.7B-GGUF(Q8_0) で CPU 実行する。
_ASR_DEFAULTS = {
    "model": "parakeet-tdt-0.6b-ja",
    "live_fallback_segment_sec": 10.0,
    "live_fallback_min_rms": 300.0,
    "models": {
        "parakeet-tdt-0.6b-ja": {
            "backend": "parakeet_cpp",
            "filename": "parakeet-tdt-0.6b-ja.gguf",
            "decoder": "tdt",
            "language": "ja",
        },
        "nemotron-3.5-asr-streaming-0.6b": {
            # decoder 空 = --decoder 省略。parakeet-cli は ctc|tdt のみ受け付け、RNN-T は
            # デフォルトデコーダ（arch=rnnt → rnnt_greedy）で動く。多言語モデルのため
            # language は BCP-47 ロケール ja-JP（bare 'ja' は不可）。詳細は config/asr.yaml。
            "backend": "parakeet_cpp",
            "filename": "nemotron-3.5-asr-streaming-0.6b.gguf",
            "decoder": "",
            "language": "ja-JP",
        },
        "qwen3-asr-1.7b": {
            # llama.cpp の llama-mtmd-cli で本体 gguf + mmproj(音声エンコーダ) を使う。
            # language は Qwen 流儀の言語名（英語表記）。空なら auto 検出。詳細は config/asr.yaml。
            "backend": "llama_mtmd",
            "filename": "Qwen3-ASR-1.7B-Q8_0.gguf",
            "mmproj": "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf",
            "language": "Japanese",
            "prompt": "",
        },
    },
}


def _load_asr_config() -> dict:
    """``config/asr.yaml`` の ``asr:`` セクションを読み込む。

    ファイルが無い／壊れている場合は ``_ASR_DEFAULTS`` にフォールバックする
    （VAD 設定と同じ方針）。``models`` はプロファイル単位でマージし、既定の 2 モデルを
    残したまま yaml 側の上書き・追加を反映する。
    """
    path = os.environ.get("ASR_CONFIG", _DEFAULT_ASR_CONFIG)
    cfg = copy.deepcopy(_ASR_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        section = data.get("asr") or {}
        if section.get("model"):
            cfg["model"] = section["model"]
        for key in ["live_fallback_segment_sec", "live_fallback_min_rms"]:
            if key in section:
                cfg[key] = section[key]
        for name, profile in (section.get("models") or {}).items():
            cfg["models"][name] = {**cfg["models"].get(name, {}), **(profile or {})}
    except (OSError, yaml.YAMLError):
        pass
    return cfg


_ASR = _load_asr_config()


def _asr_profile(model: str) -> dict:
    """モデル名 → プロファイル（backend と backend 別パラメータ）。未知なら既定モデル。"""
    return _ASR["models"].get(model) or _ASR["models"][_ASR_DEFAULTS["model"]]


# VAD（onnx）・ASR（gguf）の両モデルを置く単一ディレクトリ。
# 既定はリポジトリ直下（コンテナでは /app）の data/models。1.4GB の GGUF は
# イメージに焼かず ./data をマウントして供給する設計のため、小さな VAD onnx も
# 同じ data/models に揃える（保存先の一貫性）。MODELS_DIR で一括変更でき、
# 個別パスは SILERO_VAD_ONNX / PARAKEET_MODEL で上書きできる。
_DEFAULT_MODELS_DIR = os.path.abspath(
    os.environ.get(
        "MODELS_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "models")
    )
)


def _resolve_model_path(filename: str, env_var: str = "PARAKEET_MODEL") -> str:
    """ASR モデル（gguf）のパスを ``filename`` から解決して返す。

    ``env_var``（既定 PARAKEET_MODEL。qwen は QWEN_ASR_MODEL / QWEN_ASR_MMPROJ）があれば
    それを優先（モデル名に依らず明示パスを使う）。無ければ data/models/<filename> を使うが、
    ファイルが実在する時だけパスを返し、不在なら "" を返す。これにより未配置時は ASR を
    graceful に無効化し、セグメント毎の "failed to load model" を出さない（従来の
    parakeet_model 未設定時と同じ挙動を保つ）。
    """
    env = os.environ.get(env_var) if env_var else None
    if env:
        return env
    if not filename:
        return ""
    path = os.path.join(_DEFAULT_MODELS_DIR, filename)
    return path if os.path.exists(path) else ""


def _default_parakeet_bin() -> str:
    """parakeet-cli の既定値を返す。

    PARAKEET_CPP_BIN があればそれを優先する。未指定時は PATH 上の
    ``parakeet-cli`` に加えて、このリポジトリのヘルパが使う
    ``.cache/parakeet.cpp`` のローカルビルド成果物も候補にする。
    これにより macOS で Docker を使わず WebUI を直接起動した場合でも、
    ``.cache/parakeet.cpp/build/examples/cli/parakeet-cli`` があれば ASR を有効化できる。
    """
    env = os.environ.get("PARAKEET_CPP_BIN")
    if env:
        return env
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    local_bin = os.path.join(
        repo_root, ".cache", "parakeet.cpp", "build", "examples", "cli", "parakeet-cli"
    )
    return local_bin if os.path.exists(local_bin) else "parakeet-cli"


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
    # ライブ配信向けフォールバック: broadcast 音声では silero-vad が発話を検出できず
    # 「認識待機中」のままになることがあるため、一定以上の音量がある区間は固定長で
    # ASR に渡す。通常の VAD セグメントが確定した場合は重複を避けるため破棄される。
    live_fallback_segment_sec: float = field(
        default_factory=lambda: float(_ASR["live_fallback_segment_sec"])
    )
    live_fallback_min_rms: float = field(
        default_factory=lambda: float(_ASR["live_fallback_min_rms"])
    )

    # --- ASR バックエンド ---
    # 使用モデル（config/asr.yaml の models キー）。これに応じて backend と各パラメータを
    # __post_init__ で派生する。ASRConfig(asr_model="qwen3-asr-1.7b") のようにコードからも切替できる。
    asr_model: str = field(default_factory=lambda: _ASR["model"])
    # backend は None のとき asr_model のプロファイルから派生（既定 parakeet_cpp）。
    backend: str | None = None

    # --- parakeet_cpp バックエンド（parakeet-cli） ---
    parakeet_bin: str = field(default_factory=_default_parakeet_bin)
    # 以下 3 つは None のとき asr_model のプロファイルから派生する（None センチネル）。
    # 明示指定（テストの parakeet_model="m.gguf"、language="" で --lang 省略 など）は尊重する。
    parakeet_model: str | None = None
    parakeet_decoder: str | None = None
    parakeet_language: str | None = None

    # --- llama_mtmd バックエンド（llama.cpp の llama-mtmd-cli・Qwen3-ASR 等） ---
    llama_bin: str = field(
        default_factory=lambda: os.environ.get("LLAMA_MTMD_BIN", "llama-mtmd-cli")
    )
    # 以下 4 つは None のとき asr_model のプロファイルから派生する（None センチネル）。
    qwen_model: str | None = None    # 本体 GGUF パス
    qwen_mmproj: str | None = None   # 音声エンコーダ（mmproj）GGUF パス
    qwen_language: str | None = None  # Qwen 流儀の言語名（英語表記。空で auto）
    qwen_prompt: str | None = None    # ASR 用プロンプト（既定空）
    asr_timeout_sec: float = 120.0  # 1 セグメント推論のタイムアウト

    # --- VAD モデル ---
    vad_model_path: str = field(
        default_factory=lambda: os.environ.get(
            "SILERO_VAD_ONNX",
            os.path.join(_DEFAULT_MODELS_DIR, "silero_vad.onnx"),
        )
    )

    def __post_init__(self) -> None:
        """未指定（None）の ASR 設定を asr_model のプロファイルから backend 別に補完する。"""
        profile = _asr_profile(self.asr_model)
        if self.backend is None:
            self.backend = profile.get("backend", "parakeet_cpp")

        if self.backend == "parakeet_cpp":
            if self.parakeet_model is None:
                self.parakeet_model = _resolve_model_path(profile.get("filename", ""))
            if self.parakeet_decoder is None:
                self.parakeet_decoder = profile.get("decoder", "tdt")
            if self.parakeet_language is None:
                self.parakeet_language = profile.get("language", "ja")
        elif self.backend == "llama_mtmd":
            if self.qwen_model is None:
                self.qwen_model = _resolve_model_path(
                    profile.get("filename", ""), "QWEN_ASR_MODEL"
                )
            if self.qwen_mmproj is None:
                self.qwen_mmproj = _resolve_model_path(
                    profile.get("mmproj", ""), "QWEN_ASR_MMPROJ"
                )
            if self.qwen_language is None:
                self.qwen_language = profile.get("language", "")
            if self.qwen_prompt is None:
                self.qwen_prompt = profile.get("prompt", "")

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
