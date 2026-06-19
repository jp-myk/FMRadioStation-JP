"""StreamingASRService の非同期結合検証（VAD/backend をフェイク注入）。"""
import asyncio
import time

import numpy as np

from asr_core.asr.backend import ASRBackend
from asr_core.config import ASRConfig
from asr_core.service import StreamingASRService


class FakeVad:
    """振幅で speech/silence を決める決定的 VAD。"""
    def probability(self, frame: np.ndarray) -> float:
        return 1.0 if np.abs(frame).mean() > 100 else 0.0


class FakeBackend(ASRBackend):
    def __init__(self):
        self.calls = 0

    def transcribe(self, samples, sample_rate):
        self.calls += 1
        return f"text{self.calls}"


def _pcm(value, n):
    return np.full(n, value, dtype=np.int16).tobytes()


def test_push_audio_is_non_blocking():
    async def run():
        svc = StreamingASRService(ASRConfig(), vad=FakeVad(), backend=FakeBackend())
        await svc.start()
        t0 = time.perf_counter()
        await svc.push_audio(_pcm(0, 16000))
        elapsed = time.perf_counter() - t0
        await svc.aclose()
        return elapsed

    assert asyncio.run(run()) < 0.05  # キューに置くだけなので即返る


def test_speech_segment_produces_final_result():
    async def run():
        cfg = ASRConfig(context_sec=0.0)
        backend = FakeBackend()
        svc = StreamingASRService(cfg, vad=FakeVad(), backend=backend)
        await svc.start()

        # 発話 1秒（高振幅）→ 無音 1秒（終端トリガ）
        await svc.push_audio(_pcm(8000, 16000))
        await svc.push_audio(_pcm(0, 16000))

        # consumer / executor が回るのを待って結果を回収
        results = []
        for _ in range(50):
            results = await svc.get_results()
            if results:
                break
            await asyncio.sleep(0.02)
        await svc.aclose()
        return results

    results = asyncio.run(run())
    assert len(results) == 1
    r = results[0]
    assert r.is_final is True
    assert r.text == "text1"
    assert r.t_end > r.t_start


def test_no_speech_produces_no_result():
    async def run():
        svc = StreamingASRService(ASRConfig(), vad=FakeVad(), backend=FakeBackend())
        await svc.start()
        await svc.push_audio(_pcm(0, 16000))
        await svc.push_audio(_pcm(0, 16000))
        await asyncio.sleep(0.1)
        await svc.aclose()
        return await svc.get_results()

    assert asyncio.run(run()) == []
