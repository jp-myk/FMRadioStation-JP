"""ThreadedASRSession の同期ファサード検証（フェイク VAD/backend 注入）。"""
import numpy as np

from fm_radio_station.asr_core.asr.backend import ASRBackend
from fm_radio_station.asr_core.config import ASRConfig
from fm_radio_station.asr_core.runner import ThreadedASRSession


class FakeVad:
    def probability(self, frame: np.ndarray) -> float:
        return 1.0 if np.abs(frame).mean() > 100 else 0.0


class FakeBackend(ASRBackend):
    def transcribe(self, samples, sample_rate):
        return "やあ"


def _pcm(value, n):
    return np.full(n, value, dtype=np.int16).tobytes()


def _session_with_fakes():
    # service を差し替えて実モデル無しで起動できるようにする
    session = ThreadedASRSession(ASRConfig(context_sec=0.0))
    from fm_radio_station.asr_core.service import StreamingASRService

    original = session._service  # None
    assert original is None

    def make_service(cfg):
        return StreamingASRService(cfg, vad=FakeVad(), backend=FakeBackend())

    # start() は内部で StreamingASRService(self._config) を生成するため、
    # ここでは start をラップせず、生成箇所をモンキーパッチする。
    import fm_radio_station.asr_core.runner as runner_mod
    runner_mod.StreamingASRService = make_service  # type: ignore
    return session, runner_mod


def test_start_feed_poll_stop():
    session, runner_mod = _session_with_fakes()
    try:
        session.start()
        # 発話 → 無音 で 1 セグメント確定させる
        session.feed(_pcm(8000, 16000))
        session.feed(_pcm(0, 16000))

        import time
        results = []
        for _ in range(50):
            results = session.poll()
            if results:
                break
            time.sleep(0.02)
        assert len(results) == 1
        assert results[0].text == "やあ"
        assert results[0].is_final is True
    finally:
        session.stop()
        # 後始末: パッチを戻す
        from fm_radio_station.asr_core.service import StreamingASRService as RealService
        runner_mod.StreamingASRService = RealService


def test_poll_returns_empty_before_start():
    session = ThreadedASRSession(ASRConfig())
    assert session.poll() == []
    session.feed(_pcm(0, 16000))  # start 前 feed は no-op（例外を出さない）
