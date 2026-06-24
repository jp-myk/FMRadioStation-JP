"""ThreadedASRSession — StreamingASRService の同期ファサード。

``StreamingASRService`` は asyncio 前提だが、WebUI 側のストリーミング配信は
プレーンスレッド（transcoder の feed）と同期 FastAPI ルートから駆動される。
本クラスはバックグラウンドで asyncio ループを 1 本回し、スレッドから安全に呼べる
同期 API（``start`` / ``feed`` / ``poll`` / ``stop``）を提供する。

使い方:

    session = ThreadedASRSession(ASRConfig())
    session.start()                 # 失敗（モデル未設置等）は例外送出
    session.feed(pcm_bytes)         # 投げっぱなし・軽量
    results = session.poll()        # list[ASRResult]
    session.stop()
"""
import asyncio
import threading

from fm_radio_station.asr_core.config import ASRConfig
from fm_radio_station.asr_core.results import ASRResult
from fm_radio_station.asr_core.service import StreamingASRService


class ThreadedASRSession:
    def __init__(self, config: ASRConfig | None = None):
        self._config = config or ASRConfig()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._service: StreamingASRService | None = None
        self._started = False

    def start(self) -> None:
        """ループスレッドを起動し service を初期化する（同期・完了まで待つ）。

        ``SileroVad`` / backend の初期化に失敗した場合は例外を送出し、
        ループスレッドは後始末される（呼び出し側で握りつぶせる）。
        """
        if self._started:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="asr-loop", daemon=True
        )
        self._thread.start()
        self._service = StreamingASRService(self._config)
        try:
            self._run_coro(self._service.start()).result()
        except Exception:
            # 起動失敗: ループを畳んでから送出
            self._service = None
            self._shutdown_loop()
            raise
        self._started = True

    def feed(self, pcm: bytes) -> None:
        """生の int16 PCM を投入する（結果を待たない軽量呼び出し）。"""
        if not self._started or self._service is None:
            return
        # 投げっぱなし。例外はループ側で握られる（push_audio は失敗しない想定）
        asyncio.run_coroutine_threadsafe(self._service.push_audio(pcm), self._loop)

    def poll(self, timeout: float = 5.0) -> list[ASRResult]:
        """たまっている認識結果を回収する（空なら空リスト）。"""
        if not self._started or self._service is None:
            return []
        try:
            return self._run_coro(self._service.get_results()).result(timeout=timeout)
        except Exception:
            return []

    def stop(self) -> None:
        """service を閉じてループスレッドを停止する（同期）。"""
        if self._service is not None:
            try:
                self._run_coro(self._service.aclose()).result(timeout=10.0)
            except Exception:
                pass
            self._service = None
        self._shutdown_loop()
        self._started = False

    # ------------------------------------------------------------------ 内部
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _shutdown_loop(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        self._loop.close()
        self._loop = None
        self._thread = None
