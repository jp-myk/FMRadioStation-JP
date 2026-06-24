# src/fm_radio_station/apps/webui.py — シーケンス図

## 配信フロー — シーケンス図

時系列での関数呼び出し順を示す。

> 起動ポートは `WEBUI_PORT`（無ければ `PORT`）で指定し、未指定時は 5000 番から 5010 番まで空きを自動探索する。

```mermaid
sequenceDiagram
    actor Browser as ブラウザ
    participant webui as apps/webui.py<br/>stream_audio() / api_stop_stream()
    participant receivers as radio_core/receivers.py<br/>StreamingFM/AMReceiver
    participant transcoder as radio_core/transcoder.py<br/>stream_fd_as_mp3()<br/>stream_growing_wav_as_mp3()
    participant ffmpeg as ffmpeg プロセス
    participant SDR as RTL-SDR ハードウェア<br/>(_sdr_lock 排他)
    participant FIFO as /tmp/radio_stream_*.pcm<br/>名前付き FIFO
    participant WAV as recordings/*.wav<br/>録音中ファイル
    participant asr as asr_core<br/>ThreadedASRSession<br/>(VAD→Segmenter→parakeet.cpp)

    Browser->>webui: GET /stream/{station_id}?asr=1<br/>（asr=0 でこのリクエストの字幕を抑止）

    alt タイムシフト（同局が IN_PROGRESS にある場合）
        webui->>asr: _start_asr(station_id)（失敗しても配信継続）
        webui->>transcoder: stream_growing_wav_as_mp3(wav_path, is_still_recording, on_pcm=_on_pcm)
        transcoder->>ffmpeg: Popen(ffmpeg -f s16le -ar 16000 -ac 1 -i pipe:0 -f mp3 -b:a 64k -)
        transcoder->>transcoder: feed() daemon スレッド起動
        webui-->>Browser: StreamingResponse(generate(), media_type="audio/mpeg")
        loop feed() daemon スレッド
            transcoder->>WAV: open(wav_path).seek(44).read(4096)
            transcoder->>asr: on_pcm(pcm_chunk) → feed()/poll() → _asr_transcript 追記
            transcoder->>ffmpeg: stdin.write(pcm_chunk)
            ffmpeg-->>Browser: stdout.read(4096) → MP3 yield
        end
        Note over transcoder,WAV: is_still_recording() が False になると feed() 終了 → ffmpeg 完了 → _stop_asr()

    else ライブ（通常）
        webui->>webui: _sdr_lock.acquire()
        webui->>FIFO: os.mkfifo(fifo_path)
        webui->>FIFO: os.open(O_RDONLY|O_NONBLOCK) → rfd
        webui->>receivers: StreamingFMReceiver(freq, 2.4e6, fifo_path, desired_audio_rate=16000, gain=40)<br/>または StreamingAMReceiver(同シグネチャ)
        webui->>receivers: receiver.start()
        webui->>asr: _start_asr(station_id)（失敗しても配信継続）
        receivers->>SDR: osmosdr.source 初期化・IQ サンプル取得開始
        receivers-->>FIFO: int16 PCM 書き込み（連続）
        webui-->>Browser: StreamingResponse(generate(), media_type="audio/mpeg")
        Note over webui,Browser: generate() ジェネレータが走り始める
        webui->>transcoder: stream_fd_as_mp3(rfd, on_pcm=_on_pcm)
        transcoder->>ffmpeg: Popen(ffmpeg -f s16le -ar 16000 -ac 1 -i pipe:0 -f mp3 -b:a 64k -)
        transcoder->>transcoder: feed() daemon スレッド起動<br/>（先頭 3 秒は無音で GNU Radio 起動待ち）
        loop PCM → MP3 変換ループ
            FIFO-->>transcoder: read(4096) PCM チャンク
            transcoder->>asr: on_pcm(pcm_chunk) → feed()/poll() → _asr_transcript 追記
            transcoder->>ffmpeg: stdin.write(pcm_chunk)
            ffmpeg-->>Browser: stdout.read(4096) → MP3 yield
        end
    end

    Note over asr: ThreadedASRSession（feed/poll/stop）内: VAD(silero) → SpeechSegmenter<br/>→ 既定バックエンド parakeet.cpp(subprocess) → final テキストを _asr_transcript に蓄積

    par 自動字幕ポーリング（再生中、別リクエストで並行）
        loop 1.5 秒間隔（onConnected〜stopStream）
            Browser->>webui: GET /api/transcript/{station_id}?since=N
            webui-->>Browser: {station_id, available, enabled, active, segments[since:], cursor}
            Browser->>Browser: 新セグメントを字幕パネルへ追記・自動スクロール
        end
    end

    Browser->>webui: POST /api/stop-stream
    webui->>receivers: receiver.stop() / receiver.wait()
    webui->>receivers: del receiver → rtlsdr_close()
    webui->>asr: _stop_asr()（ThreadedASRSession.stop()）
    webui->>FIFO: os.unlink(fifo_path)
    webui->>webui: _sdr_lock.release()
```

### タイムシフト

- `stream_audio()` が `IN_PROGRESS_RECORDINGS` に同局を検出 → `stream_from_recording()` 経由で `stream_growing_wav_as_mp3()` を呼ぶ
- `feed()` daemon スレッドが WAV の 44 バイト目（PCM 本体）から 4096 B ずつ読んで ffmpeg stdin へ送り続ける
- `is_still_recording()` が `False` になった時点で `feed()` が終了 → ffmpeg が完了 → ストリーム終了

### ライブ

- `stream_audio()` が `_sdr_lock` を取得 → FIFO 作成 → `StreamingFMReceiver` / `StreamingAMReceiver` を生成・`.start()`
- GNU Radio（osmosdr）が RTL-SDR から IQ サンプルを取得し、復調・リサンプル後の int16 PCM を FIFO に書き込み続ける
- `stream_fd_as_mp3(rfd)` が ffmpeg を Popen し、`feed()` daemon スレッドが FIFO → ffmpeg stdin → MP3 → ブラウザへ yield する
- 先頭 3 秒は無音を ffmpeg stdin に流し、GNU Radio の起動遅延によるブラウザ側のバッファ枯渇を防ぐ

#### 配信経路は 2 系統（標準 / Safari）

`stream_audio()` はクライアント判定（`_needs_mp3`）で配信経路を分ける。タイムシフトの `stream_from_recording()` も同様。

- **標準経路** — 上記の通り `transcoder.stream_fd_as_mp3()`（ライブ）/ `stream_growing_wav_as_mp3()`（タイムシフト）を経由。ffmpeg は `-b:a 64k`、無音プリアンブルは 3 秒。
- **Safari など MP3 必須クライアント経路** — transcoder を介さず webui 内で直接 ffmpeg を Popen するインライン実装。エンコードは `-q:a 5`、無音プリアンブルは 2 秒。挙動（PCM タップ＝字幕、FIFO/WAV 読み出し）は標準経路と同等。

### 停止

- `POST /api/stop-stream` で `receiver.stop()/wait()` → `del receiver`（`rtlsdr_close()` 発火）→ `_stop_asr()` → FIFO 削除 → `_sdr_lock.release()`
- ライブの `generate()` の `finally`（タブ閉じ等）は `_cleanup_stream(station_id, recv_ref, fifo_path)` を呼び、`_stop_asr()` → receiver の `stop()/wait()` → `del`（`rtlsdr_close()` 発火）→ FIFO 削除 → `_sdr_lock.release()` までまとめて行う
- タイムシフトの `generate()` の `finally` は SDR / FIFO を持たないため `_stop_asr()` のみを呼ぶ

### 自動字幕（ASR）

- ライブ／タイムシフト両分岐で `_start_asr(station_id)` を呼び、`transcoder` に `on_pcm=_on_pcm` を渡す（リクエストの `asr=0` 指定時は渡さない）
- `feed()` が ffmpeg に送るのと同じ PCM チャンクを `_on_pcm` がタップ → `ThreadedASRSession.feed()` へ給餌し、`poll()` 結果を `_asr_transcript` に蓄積。`ThreadedASRSession` は背景 asyncio ループ上の `asr_core.StreamingASRService`（`push_audio()/get_results()/aclose()`）を駆動する同期ファサード
- `asr_core` 内部: silero-vad → `SpeechSegmenter`（無音 300ms / 最大 18s で確定。値は `config/vad.yaml`）→ 既定バックエンド parakeet.cpp（`parakeet-tdt-0.6b-ja`、`config/asr.yaml`）→ final テキスト
  - バックエンド／モデルは `config/asr.yaml` で切替可能（nemotron は parakeet_cpp 経由、Qwen3-ASR は llama_mtmd 経由）。既定は parakeet.cpp
- ブラウザは `onConnected` 後に `GET /api/transcript/{station_id}?since=N` を 1.5 秒間隔でポーリングし、新セグメントを下部の字幕パネルへ追記する（配信の MP3 ストリームとは別リクエスト）
  - レスポンスは `{station_id, available, enabled, active, segments, cursor}`。`available` は ASR セッション稼働中か、`enabled` は `ASR_ENABLED` の状態。要求局が現在の配信局と異なる場合は `active:false, segments:[], cursor:0` を返す
- silero モデル / parakeet バイナリ未設置や依存欠如では `_start_asr` が握りつぶされ、**字幕は出ないが配信は正常**（graceful degradation、`ASR_ENABLED=0` で無効化可）
