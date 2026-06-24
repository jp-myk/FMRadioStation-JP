"""パス解決の一元化。

パッケージを `src/` レイアウトへ移したことで「モジュールの 1 つ上＝リポジトリ直下」という
従来の仮定が崩れる。さらに pip install（Docker の ``uv pip install .``）では本パッケージは
site-packages に入り、設定/データは実行ディレクトリ側にある。そのため設定・データ・録音・
キャッシュは **実行ディレクトリ（CWD）基準**＋env 上書きで解決する（Docker は WORKDIR=/app、
ローカルはリポジトリ直下から実行する想定で、従来挙動と一致する）。WebUI のテンプレート/静的
アセットだけはパッケージ同梱物なので ``__file__`` 基準で解決する。
"""
import os
from pathlib import Path


def _base() -> Path:
    """設定・データ等の基準ディレクトリ（既定 CWD、env FMRADIO_ROOT で上書き可）。"""
    env = os.environ.get("FMRADIO_ROOT")
    return Path(env) if env else Path.cwd()


def config_dir() -> Path:
    env = os.environ.get("CONFIG_DIR")
    return Path(env) if env else _base() / "config"


def data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    return Path(env) if env else _base() / "data"


def models_dir() -> Path:
    """VAD/ASR モデル配置先（既存 env MODELS_DIR を優先）。"""
    env = os.environ.get("MODELS_DIR")
    return Path(env) if env else data_dir() / "models"


def cache_dir() -> Path:
    """ローカルビルド成果物（parakeet.cpp / llama.cpp）の置き場。"""
    env = os.environ.get("CACHE_DIR")
    return Path(env) if env else _base() / ".cache"


def recordings_dir() -> Path:
    env = os.environ.get("RECORDINGS_DIR")
    return Path(env) if env else _base() / "recordings"


def state_file() -> Path:
    """WebUI の予約状態ファイル（既定 data/state.json、env STATE_FILE で上書き可）。"""
    env = os.environ.get("STATE_FILE")
    return Path(env) if env else data_dir() / "state.json"


def web_dir() -> Path:
    """WebUI のテンプレート/静的アセット（パッケージ同梱）。"""
    return Path(__file__).resolve().parent / "web"


def asr_config_file() -> Path:
    env = os.environ.get("ASR_CONFIG")
    return Path(env) if env else config_dir() / "asr.yaml"


def vad_config_file() -> Path:
    env = os.environ.get("VAD_CONFIG")
    return Path(env) if env else config_dir() / "vad.yaml"


def tunnels_config_file() -> Path:
    env = os.environ.get("TUNNELS_CONFIG")
    return Path(env) if env else config_dir() / "tunnels.yaml"
