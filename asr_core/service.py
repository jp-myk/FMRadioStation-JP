"""ストリーミング音声認識サービス（非同期オーケストレーション）。

音声入力と ASR 推論を分離する:

    push_audio(chunk)            # 1秒ごとの int16 PCM を input_queue へ put して即 return
            │ (asyncio.Queue)
    _consumer task               # 512 sample に再フレーミング → VAD → Segmenter
            │                    # 確定セグメントごとに backend.transcribe を
            │                    # executor(max_workers=1) で実行（順序保証）
            ▼ (asyncio.Queue)
    _results_queue → get_results()  # ASRResult を非ブロッキングに drain

WebUI 側の使い方:

    asr = StreamingASRService(config)
    await asr.start()
    await asr.push_audio(chunk)
    results = await asr.get_results()
    await asr.aclose()

あるいは ``async with StreamingASRService(config) as asr: ...``。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from asr_core.asr import ASRBackend, build_backend
from asr_core.config import ASRConfig
from asr_core.results import ASRResult
from asr_core.segmenter import SpeechSegment, SpeechSegmenter
from asr_core.vad import SileroVad
from asr_core.wav_io import pcm_bytes_to_int16


class StreamingASRService:
    def __init__(
        self,
        config: ASRConfig | None = None,
        *,
        vad: SileroVad | None = None,
        backend: ASRBackend | None = None,
    ):
        """``vad`` / ``backend`` を渡すとそのまま使う（テストでフェイク注入に利用）。"""
        self._cfg = config or ASRConfig()
        self._vad = vad
        self._backend = backend
        self._segmenter: SpeechSegmenter | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._input_q: asyncio.Queue | None = None
        self._results_q: asyncio.Queue | None = None
        self._consumer: asyncio.Task | None = None
        self._asr_tasks: set[asyncio.Task] = set()
        self._leftover = np.empty(0, dtype=np.int16)
        self._started = False

    # ------------------------------------------------------------------ 起動/停止
    async def start(self) -> None:
        if self._started:
            return
        # 重い初期化はここで明示的に行う
        if self._vad is None:
            self._vad = SileroVad(self._cfg)
        if self._backend is None:
            self._backend = build_backend(self._cfg)
        self._segmenter = SpeechSegmenter(self._cfg)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._input_q = asyncio.Queue()
        self._results_q = asyncio.Queue()
        self._consumer = asyncio.create_task(self._consume())
        self._started = True

    async def aclose(self) -> None:
        if not self._started:
            return
        # consumer に終端を伝える
        await self._input_q.put(None)
        if self._consumer is not None:
            await self._consumer
        # 走行中の ASR タスクを待つ
        if self._asr_tasks:
            await asyncio.gather(*self._asr_tasks, return_exceptions=True)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        self._started = False

    async def __aenter__(self) -> "StreamingASRService":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 公開 API
    async def push_audio(self, chunk: bytes) -> None:
        """生の little-endian int16 PCM バイト列を投入する（安価・即 return）。"""
        if not self._started:
            raise RuntimeError("start() を先に呼んでください")
        await self._input_q.put(pcm_bytes_to_int16(chunk))

    async def get_results(self) -> list[ASRResult]:
        """現在たまっている認識結果を回収する（空なら空リスト）。

        ``aclose()`` 後でも未回収分を drain できる。
        """
        if self._results_q is None:
            return []
        results: list[ASRResult] = []
        while not self._results_q.empty():
            results.append(self._results_q.get_nowait())
        return results

    # ------------------------------------------------------------------ 内部
    async def _consume(self) -> None:
        frame_n = self._cfg.vad_frame_samples
        while True:
            item = await self._input_q.get()
            if item is None:
                # 終端: 残フレームを 0 埋めで処理し、保留セグメントを flush
                if self._leftover.shape[0] > 0:
                    self._process_frame(self._leftover)
                    self._leftover = np.empty(0, dtype=np.int16)
                for seg in self._segmenter.flush():
                    self._dispatch(seg)
                break

            self._leftover = (
                item if self._leftover.shape[0] == 0
                else np.concatenate([self._leftover, item])
            )
            while self._leftover.shape[0] >= frame_n:
                frame = self._leftover[:frame_n]
                self._leftover = self._leftover[frame_n:]
                self._process_frame(frame)

    def _process_frame(self, frame: np.ndarray) -> None:
        prob = self._vad.probability(frame)
        for seg in self._segmenter.process_frame(frame, prob):
            self._dispatch(seg)

    def _dispatch(self, seg: SpeechSegment) -> None:
        task = asyncio.create_task(self._run_asr(seg))
        self._asr_tasks.add(task)
        task.add_done_callback(self._asr_tasks.discard)

    async def _run_asr(self, seg: SpeechSegment) -> None:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(
                self._executor,
                self._backend.transcribe,
                seg.samples,
                self._cfg.sample_rate,
            )
        except Exception as e:  # 推論失敗はセグメントを落として続行
            text = ""
            print(f"[asr_core] transcribe failed for segment {seg.segment_id}: {e}")
        if not text:
            return
        await self._results_q.put(
            ASRResult(
                segment_id=seg.segment_id,
                text=text,
                is_final=True,
                t_start=seg.t_start,
                t_end=seg.t_end,
            )
        )
