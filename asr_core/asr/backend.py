"""ASR バックエンドの抽象基底クラス。

``StreamingASRService`` は具体実装（parakeet.cpp / ONNX / NeMo …）を一切知らず、
この ABC の ``transcribe`` だけを呼ぶ。バックエンド差し替えは
``asr_core.asr.build_backend`` のファクトリで行う。
"""
from abc import ABC, abstractmethod

import numpy as np


class ASRBackend(ABC):
    """音声サンプル列 → テキストの推論器。"""

    @abstractmethod
    def transcribe(self, samples: np.ndarray, sample_rate: int) -> str:
        """int16 モノラルサンプル列を 1 回推論してテキストを返す（同期・ブロッキング）。

        この呼び出しは CPU 律速でブロックする想定。呼び出し側
        （``StreamingASRService``）が executor スレッドで実行することで
        イベントループを塞がないようにする。
        """
        raise NotImplementedError
