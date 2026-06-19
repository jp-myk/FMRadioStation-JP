"""ASR バックエンドのファクトリ。

config.backend の文字列から具体実装を生成する。将来 ONNX / GPU / NeMo を
追加する場合はここに分岐を 1 行足すだけでよい。
"""
from asr_core.asr.backend import ASRBackend
from asr_core.asr.parakeet_cpp import ParakeetCppBackend
from asr_core.config import ASRConfig


def build_backend(config: ASRConfig) -> ASRBackend:
    if config.backend == "parakeet_cpp":
        return ParakeetCppBackend(config)
    raise ValueError(f"未知の ASR backend: {config.backend!r}")


__all__ = ["ASRBackend", "ParakeetCppBackend", "build_backend"]
