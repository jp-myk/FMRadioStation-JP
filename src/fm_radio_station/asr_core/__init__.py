"""asr_core — VAD + 音声認識（ASR）モジュール。

WebUI から使う最小 API:

    from fm_radio_station.asr_core import StreamingASRService, ASRConfig

    asr = StreamingASRService(ASRConfig())
    await asr.start()
    await asr.push_audio(chunk)        # 1秒ごとの int16 PCM bytes
    results = await asr.get_results()  # list[ASRResult]
    await asr.aclose()
"""
from fm_radio_station.asr_core.config import ASRConfig
from fm_radio_station.asr_core.file_transcribe import asr_batch_available, transcribe_wav_to_vtt
from fm_radio_station.asr_core.results import ASRResult
from fm_radio_station.asr_core.runner import ThreadedASRSession
from fm_radio_station.asr_core.service import StreamingASRService

__all__ = [
    "StreamingASRService",
    "ThreadedASRSession",
    "ASRConfig",
    "ASRResult",
    "transcribe_wav_to_vtt",
    "asr_batch_available",
]
