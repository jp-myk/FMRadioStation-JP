"""silero-vad（ONNX / onnxruntime）のラッパ。

torch 非依存で軽量に動かすため、silero-vad の ONNX モデルを onnxruntime で実行する。
silero-vad v5 の ONNX 入出力に合わせている:
    inputs : input [1, frame], state [2, 1, 128], sr (int64 scalar)
    outputs: output [1, 1] (speech 確率), stateN [2, 1, 128]

16kHz では 1 フレーム = 512 sample が標準。フレーム間で state を持ち越すため、
``reset()`` でセグメント境界の状態をクリアできる。
"""
import numpy as np

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - 依存未導入環境向け
    ort = None

from fm_radio_station.asr_core.config import ASRConfig


class SileroVad:
    def __init__(self, config: ASRConfig):
        if ort is None:
            raise ImportError("onnxruntime が必要です（pip install onnxruntime）")
        self._sample_rate = config.sample_rate
        self._frame_samples = config.vad_frame_samples
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            config.vad_model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._sr = np.array(self._sample_rate, dtype=np.int64)
        self.reset()

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    def reset(self) -> None:
        """LSTM 状態を初期化する（セグメント境界などで呼ぶ）。"""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        """1 フレーム（int16 または float32）の speech 確率を返す。

        frame が ``frame_samples`` 未満ならゼロパディング、超過なら先頭を使う。
        """
        x = np.asarray(frame)
        if x.dtype != np.float32:
            # int16 → [-1, 1) の float32
            x = x.astype(np.float32) / 32768.0
        if x.shape[0] < self._frame_samples:
            x = np.pad(x, (0, self._frame_samples - x.shape[0]))
        elif x.shape[0] > self._frame_samples:
            x = x[: self._frame_samples]
        x = x.reshape(1, -1)

        out, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        return float(out[0][0])


def get_speech_timestamps(
    samples: np.ndarray,
    vad: "SileroVad",
    *,
    sample_rate: int = 16000,
    threshold: float = 0.5,
    min_speech_duration_ms: int = 250,
    max_speech_duration_s: float = float("inf"),
    min_silence_duration_ms: int = 100,
    speech_pad_ms: int = 30,
    min_silence_at_max_speech_ms: int = 98,
    use_max_poss_sil_at_max_speech: bool = True,
    window_size_samples: int = 512,
) -> list[dict]:
    """silero-vad の ``get_speech_timestamps`` を torch 非依存で再現する。

    公式実装（snakers4/silero-vad）の状態機械をそのまま numpy / プレーン Python に
    移植し、フレーム確率は ``vad``（``reset()`` と ``probability(frame)`` を持つ
    onnxruntime ラッパ ``SileroVad``）で計算する。戻り値は発話区間のサンプル
    インデックス列 ``[{"start": int, "end": int}, ...]``。

    ``vad`` を引数で受けることで、テストでは確率列を返すフェイクを注入できる。
    """
    audio_length_samples = int(samples.shape[0])

    # 1) 窓ごとの speech 確率を計算（state を持ち越すため reset → 逐次）。
    vad.reset()
    speech_probs: list[float] = []
    for i in range(0, audio_length_samples, window_size_samples):
        speech_probs.append(vad.probability(samples[i:i + window_size_samples]))

    # 2) 各種しきい値（公式と同じ計算式）。
    min_speech_samples = sample_rate * min_speech_duration_ms / 1000
    speech_pad_samples = sample_rate * speech_pad_ms / 1000
    max_speech_samples = (
        sample_rate * max_speech_duration_s
        - window_size_samples
        - 2 * speech_pad_samples
    )
    min_silence_samples = sample_rate * min_silence_duration_ms / 1000
    min_silence_samples_at_max_speech = sample_rate * min_silence_at_max_speech_ms / 1000
    neg_threshold = max(threshold - 0.15, 0.01)

    # 3) 状態機械（公式を逐語移植）。
    triggered = False
    speeches: list[dict] = []
    current_speech: dict = {}
    temp_end = 0
    prev_end = next_start = 0
    possible_ends: list[tuple[int, int]] = []

    for i, speech_prob in enumerate(speech_probs):
        cur_sample = window_size_samples * i

        if (speech_prob >= threshold) and temp_end:
            sil_dur = cur_sample - temp_end
            if sil_dur > min_silence_samples_at_max_speech:
                possible_ends.append((temp_end, sil_dur))
            temp_end = 0
            if next_start < prev_end:
                next_start = cur_sample

        if (speech_prob >= threshold) and not triggered:
            triggered = True
            current_speech["start"] = cur_sample
            continue

        if triggered and (cur_sample - current_speech["start"] > max_speech_samples):
            if use_max_poss_sil_at_max_speech and possible_ends:
                prev_end, dur = max(possible_ends, key=lambda x: x[1])
                current_speech["end"] = prev_end
                speeches.append(current_speech)
                current_speech = {}
                next_start = prev_end + dur
                if next_start < prev_end + cur_sample:
                    current_speech["start"] = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0
                possible_ends = []
            else:
                if prev_end:
                    current_speech["end"] = prev_end
                    speeches.append(current_speech)
                    current_speech = {}
                    if next_start < prev_end:
                        triggered = False
                    else:
                        current_speech["start"] = next_start
                    prev_end = next_start = temp_end = 0
                    possible_ends = []
                else:
                    current_speech["end"] = cur_sample
                    speeches.append(current_speech)
                    current_speech = {}
                    prev_end = next_start = temp_end = 0
                    triggered = False
                    possible_ends = []
                    continue

        if (speech_prob < neg_threshold) and triggered:
            if not temp_end:
                temp_end = cur_sample
            sil_dur_now = cur_sample - temp_end
            if (not use_max_poss_sil_at_max_speech) and sil_dur_now > min_silence_samples_at_max_speech:
                prev_end = temp_end
            if sil_dur_now < min_silence_samples:
                continue
            else:
                current_speech["end"] = temp_end
                if (current_speech["end"] - current_speech["start"]) > min_speech_samples:
                    speeches.append(current_speech)
                current_speech = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                possible_ends = []
                continue

    if current_speech and (audio_length_samples - current_speech["start"]) > min_speech_samples:
        current_speech["end"] = audio_length_samples
        speeches.append(current_speech)

    # 4) speech_pad_ms による前後パディング（公式と同じ後処理）。
    for i, speech in enumerate(speeches):
        if i == 0:
            speech["start"] = int(max(0, speech["start"] - speech_pad_samples))
        if i != len(speeches) - 1:
            silence_duration = speeches[i + 1]["start"] - speech["end"]
            if silence_duration < 2 * speech_pad_samples:
                speech["end"] += int(silence_duration // 2)
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - silence_duration // 2))
            else:
                speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - speech_pad_samples))
        else:
            speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))

    return speeches
