"""発話セグメントの状態機械。

VAD のフレーム単位の音声確率を消費し、発話の開始・継続・終了を管理して
**確定セグメント**（int16 サンプル列 + タイムスタンプ）を切り出す。

確定条件:
  - 無音が ``silence_end_ms`` 以上続いた（発話の自然終端）
  - セグメント長が ``max_segment_sec`` を超えた（強制 cut）
破棄条件:
  - 実発話フレーム数が ``min_speech_ms`` 未満（雑音とみなす）

セグメント先頭には ``context_sec`` 分の前方文脈（pre-roll）を付与し、
語頭の欠落を防ぐ。VAD 推論自体は呼び出し側が行い、確率だけを渡す
（テスト時に確率列を直接与えて決定的に検証できる）。
"""
from dataclasses import dataclass

import numpy as np

from fm_radio_station.asr_core.config import ASRConfig


@dataclass
class SpeechSegment:
    segment_id: int
    samples: np.ndarray
    t_start: float
    t_end: float


class SpeechSegmenter:
    def __init__(self, config: ASRConfig):
        """Initialise segmenter state from *config* parameters."""
        self._cfg = config
        self._sr = config.sample_rate
        self._threshold = config.vad_threshold
        self._silence_end_frames = config.silence_end_frames
        self._min_speech_frames = config.min_speech_frames
        self._max_segment_samples = config.max_segment_samples
        self._context_samples = config.context_samples
        self._speech_pad_samples = config.speech_pad_samples

        self._pos = 0               # これまでに見たサンプル総数
        self._next_id = 0
        self._reset_segment()
        self._preroll = np.empty(0, dtype=np.int16)

    def _reset_segment(self) -> None:
        """Clear per-segment accumulators to prepare for the next segment."""
        self._in_speech = False
        self._buf: list[np.ndarray] = []
        self._buf_len = 0
        self._seg_start = 0
        self._silence_frames = 0
        self._speech_frames = 0
        self._trailing_silence_samples = 0  # 末尾の連続無音サンプル数（speech_pad 用）

    def process_frame(self, frame: np.ndarray, prob: float) -> list[SpeechSegment]:
        """1 フレームを処理し、このフレームで確定したセグメントを返す（通常 0 or 1 件）。"""
        frame = np.ascontiguousarray(frame, dtype=np.int16)
        n = frame.shape[0]
        is_speech = prob >= self._threshold
        finalized: list[SpeechSegment] = []

        if not self._in_speech:
            if is_speech:
                # 発話開始: pre-roll を文脈として前置
                self._in_speech = True
                self._seg_start = self._pos - self._preroll.shape[0]
                self._buf = [self._preroll, frame]
                self._buf_len = self._preroll.shape[0] + n
                self._silence_frames = 0
                self._speech_frames = 1
            else:
                # 無音継続: pre-roll を更新（直近 context_samples 分だけ保持）
                self._append_preroll(frame)
        else:
            # 発話中: フレームを取り込む（末尾の無音も含めて終端判定に使う）
            self._buf.append(frame)
            self._buf_len += n
            if is_speech:
                self._silence_frames = 0
                self._speech_frames += 1
                self._trailing_silence_samples = 0
            else:
                self._silence_frames += 1
                self._trailing_silence_samples += n

            if self._silence_frames >= self._silence_end_frames:
                seg = self._finalize()
                if seg is not None:
                    finalized.append(seg)
            elif self._buf_len >= self._max_segment_samples:
                # 強制 cut。直後も発話中なら次セグメントを継続させたいので、
                # finalize 後にこのフレームを新セグメントの起点とする。
                seg = self._finalize(reset_preroll=False)
                if seg is not None:
                    finalized.append(seg)
                if is_speech:
                    self._in_speech = True
                    self._seg_start = self._pos
                    self._buf = [frame]
                    self._buf_len = n
                    self._silence_frames = 0
                    self._speech_frames = 1

        self._pos += n
        return finalized

    def flush(self) -> list[SpeechSegment]:
        """ストリーム終端で保留中のセグメントを確定する。"""
        if self._in_speech:
            seg = self._finalize()
            if seg is not None:
                return [seg]
        return []

    def _append_preroll(self, frame: np.ndarray) -> None:
        """Append *frame* to the pre-roll buffer, keeping only the last context_samples samples."""
        if self._context_samples <= 0:
            return
        self._preroll = np.concatenate([self._preroll, frame])
        if self._preroll.shape[0] > self._context_samples:
            self._preroll = self._preroll[-self._context_samples:]

    def _finalize(self, reset_preroll: bool = True) -> SpeechSegment | None:
        """Assemble the buffered frames into a SpeechSegment, apply padding trim, and reset state."""
        samples = (
            np.concatenate(self._buf) if self._buf else np.empty(0, dtype=np.int16)
        )
        # 末尾の無音は speech_pad_ms 分だけ残し、それを超える分は切り詰める
        # （前方は pre-roll が文脈を担う。強制 cut 時は trailing_silence=0 で無処理）。
        excess = self._trailing_silence_samples - self._speech_pad_samples
        if excess > 0 and samples.shape[0] > excess:
            samples = samples[: samples.shape[0] - excess]
        speech_ok = self._speech_frames >= self._min_speech_frames
        seg_start = self._seg_start
        self._reset_segment()
        if reset_preroll:
            self._preroll = np.empty(0, dtype=np.int16)
        if not speech_ok or samples.shape[0] == 0:
            return None
        seg = SpeechSegment(
            segment_id=self._next_id,
            samples=samples,
            t_start=max(0, seg_start) / self._sr,
            t_end=(seg_start + samples.shape[0]) / self._sr,
        )
        self._next_id += 1
        return seg
