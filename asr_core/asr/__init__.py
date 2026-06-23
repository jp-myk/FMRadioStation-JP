"""ASR バックエンドのファクトリ。

config.backend の文字列から具体実装を生成する。backend は asr_model のプロファイル
（config/asr.yaml）由来。新しいランタイムを足す場合はここに分岐を 1 行加える。
"""
from asr_core.asr.backend import ASRBackend
from asr_core.asr.llama_mtmd import LlamaMtmdBackend
from asr_core.asr.parakeet_cpp import ParakeetCppBackend
from asr_core.config import ASRConfig


def build_backend(config: ASRConfig) -> ASRBackend:
    if config.backend == "parakeet_cpp":
        return ParakeetCppBackend(config)
    if config.backend == "llama_mtmd":
        return LlamaMtmdBackend(config)
    raise ValueError(f"未知の ASR backend: {config.backend!r}")


__all__ = ["ASRBackend", "ParakeetCppBackend", "LlamaMtmdBackend", "build_backend"]
