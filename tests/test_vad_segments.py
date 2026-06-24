"""torch 非依存 get_speech_timestamps（asr_core.vad）の決定的検証。

実モデル不要。``probability`` が事前に与えた確率列を返すフェイク VAD を注入し、
threshold / min_speech / min_silence / speech_pad / max_speech の挙動を確認する。
窓サイズは 100 sample・16kHz とし、1 窓 = 100/16000 秒（計算を読みやすく）。
"""
import numpy as np

from fm_radio_station.asr_core.vad import get_speech_timestamps

WIN = 100  # window_size_samples（テスト用に小さく）


class _FakeVad:
    """与えた確率列を窓ごとに返す（state なし）。"""
    def __init__(self, probs):
        self._probs = probs
        self._i = 0

    def reset(self):
        self._i = 0

    def probability(self, frame):
        p = self._probs[self._i] if self._i < len(self._probs) else 0.0
        self._i += 1
        return p


def _run(probs, **kw):
    samples = np.zeros(len(probs) * WIN, dtype=np.int16)
    params = dict(
        sample_rate=16000, threshold=0.5,
        min_speech_duration_ms=0, min_silence_duration_ms=100,
        speech_pad_ms=0, window_size_samples=WIN,
    )
    params.update(kw)
    return get_speech_timestamps(samples, _FakeVad(probs), **params)


def test_single_speech_region():
    # 無音3 → 発話6 → 無音(>=min_silence)。min_silence=100ms=1600sample=16窓。
    probs = [0.0] * 3 + [0.9] * 6 + [0.0] * 20
    segs = _run(probs)
    assert len(segs) == 1
    assert segs[0]["start"] == 3 * WIN
    # temp_end は無音開始（窓9）で記録され、min_silence 経過後にそこで終端。
    assert segs[0]["end"] == 9 * WIN


def test_short_silence_does_not_split():
    # 発話 → 短い無音(min_silence 未満) → 発話 → 長い無音 → 1 区間に結合。
    short_gap = 10  # 10窓=1000sample < 1600(min_silence)
    probs = [0.9] * 6 + [0.0] * short_gap + [0.9] * 6 + [0.0] * 20
    segs = _run(probs)
    assert len(segs) == 1


def test_min_speech_duration_discards_short_blip():
    # 1 窓だけ発話 → min_speech=1000ms(=16000sample) 未満なので破棄。
    probs = [0.0] * 2 + [0.9] * 1 + [0.0] * 20
    segs = _run(probs, min_speech_duration_ms=1000)
    assert segs == []


def test_threshold_gates_detection():
    probs = [0.4] * 10  # すべて threshold(0.5) 未満 → 発話なし
    assert _run(probs) == []
    segs = _run(probs, threshold=0.3)  # 下げれば検出
    assert len(segs) == 1


def test_speech_pad_extends_edges():
    probs = [0.0] * 3 + [0.9] * 6 + [0.0] * 20
    no_pad = _run(probs, speech_pad_ms=0)[0]
    padded = _run(probs, speech_pad_ms=10)[0]  # 10ms=160sample
    assert padded["start"] < no_pad["start"]
    assert padded["end"] > no_pad["end"]


def test_max_speech_duration_forces_cut():
    # 長い連続発話。max_speech を小さくして複数区間に割れることを確認。
    probs = [0.9] * 200 + [0.0] * 20
    long_cut = _run(probs, max_speech_duration_s=float("inf"))
    short_cut = _run(probs, max_speech_duration_s=0.5)  # 0.5s=8000sample
    assert len(long_cut) == 1
    assert len(short_cut) >= 2
