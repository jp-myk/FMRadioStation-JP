"""SpeechSegmenter の純ロジック検証（VAD/ASR 非依存・決定的）。"""
import numpy as np

from fm_radio_station.asr_core.config import ASRConfig
from fm_radio_station.asr_core.segmenter import SpeechSegmenter


def _frame(cfg, value=1000):
    return np.full(cfg.vad_frame_samples, value, dtype=np.int16)


def _run(cfg, probs):
    """確率列を 1 フレームずつ流し、確定セグメントを集める。"""
    seg = SpeechSegmenter(cfg)
    out = []
    for p in probs:
        out.extend(seg.process_frame(_frame(cfg), p))
    out.extend(seg.flush())
    return out


def test_speech_then_silence_finalizes_one_segment():
    cfg = ASRConfig(context_sec=0.0)
    # 発話 16 フレーム → 無音 silence_end_frames 分
    probs = [1.0] * 16 + [0.0] * cfg.silence_end_frames
    segs = _run(cfg, probs)
    assert len(segs) == 1
    assert segs[0].segment_id == 0
    assert segs[0].samples.shape[0] > 0


def test_short_silence_does_not_split():
    cfg = ASRConfig(context_sec=0.0)
    short_gap = cfg.silence_end_frames - 1
    probs = [1.0] * 16 + [0.0] * short_gap + [1.0] * 16 + [0.0] * cfg.silence_end_frames
    segs = _run(cfg, probs)
    assert len(segs) == 1  # 短い無音では分割されず 1 セグメント


def test_too_short_speech_is_discarded():
    cfg = ASRConfig(context_sec=0.0)
    probs = [1.0] * (cfg.min_speech_frames - 1) + [0.0] * cfg.silence_end_frames
    segs = _run(cfg, probs)
    assert segs == []


def test_max_segment_forces_cut():
    cfg = ASRConfig(context_sec=0.0, max_segment_sec=1.0)
    frames_per_seg = cfg.max_segment_samples // cfg.vad_frame_samples + 1
    # 3 セグメント分を連続発話（無音終端なし）→ 強制 cut で複数に分割される
    probs = [1.0] * (frames_per_seg * 3) + [0.0] * cfg.silence_end_frames
    segs = _run(cfg, probs)
    assert len(segs) >= 2  # 最大長で強制 cut → 複数セグメント
    # 各セグメントが最大長以下であること
    assert all(s.samples.shape[0] <= cfg.max_segment_samples + cfg.vad_frame_samples for s in segs)


def test_flush_emits_pending_segment():
    cfg = ASRConfig(context_sec=0.0)
    probs = [1.0] * 16  # 無音終端が来ないまま終了
    segs = _run(cfg, probs)
    assert len(segs) == 1


def test_speech_pad_ms_controls_trailing_silence():
    # 同じ「発話→無音終端」入力でも、speech_pad_ms が大きいほど末尾無音を多く残す。
    probs_n = ASRConfig(context_sec=0.0).silence_end_frames
    probs = [1.0] * 16 + [0.0] * probs_n
    short = _run(ASRConfig(context_sec=0.0, speech_pad_ms=0), probs)
    long = _run(ASRConfig(context_sec=0.0, speech_pad_ms=500), probs)
    assert len(short) == 1 and len(long) == 1
    # pad=0 は末尾無音を切り詰め、pad 大は残す → セグメント長が増える
    assert long[0].samples.shape[0] > short[0].samples.shape[0]


def test_context_preroll_is_prepended():
    cfg = ASRConfig(context_sec=0.1)  # 0.1s 分の pre-roll
    silence_lead = 10
    probs = [0.0] * silence_lead + [1.0] * 16 + [0.0] * cfg.silence_end_frames
    seg = SpeechSegmenter(cfg)
    out = []
    for p in probs:
        out.extend(seg.process_frame(np.full(cfg.vad_frame_samples, 500, dtype=np.int16), p))
    assert len(out) == 1
    # pre-roll が付くので開始時刻は最初の speech フレームより前
    assert out[0].t_start < silence_lead * cfg.frame_sec
