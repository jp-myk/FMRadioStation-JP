"""asr_core — VAD + 音声認識（ASR）モジュール。

WebUI から使う最小 API:

    from asr_core import StreamingASRService, ASRConfig

    asr = StreamingASRService(ASRConfig())
    await asr.start()
    await asr.push_audio(chunk)        # 1秒ごとの int16 PCM bytes
    results = await asr.get_results()  # list[ASRResult]
    await asr.aclose()
"""
from asr_core.config import ASRConfig
from asr_core.file_transcribe import asr_batch_available, transcribe_wav_to_vtt
from asr_core.results import ASRResult
from asr_core.runner import ThreadedASRSession
from asr_core.service import StreamingASRService

__all__ = [
    "StreamingASRService",
    "ThreadedASRSession",
    "ASRConfig",
    "ASRResult",
    "transcribe_wav_to_vtt",
    "asr_batch_available",
]
