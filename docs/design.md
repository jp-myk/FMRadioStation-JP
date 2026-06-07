# radio_scheduler_webui.py — シーケンス図

## 配信フロー — シーケンス図

時系列での関数呼び出し順を示す。

```mermaid
sequenceDiagram
    actor Browser as ブラウザ
    participant webui as radio_scheduler_webui.py<br/>stream_audio() / api_stop_stream()
    participant receivers as radio_core/receivers.py<br/>StreamingFM/AMReceiver
    participant transcoder as radio_core/transcoder.py<br/>stream_fd_as_mp3()<br/>stream_growing_wav_as_mp3()
    participant ffmpeg as ffmpeg プロセス
    participant SDR as RTL-SDR ハードウェア<br/>(_sdr_lock 排他)
    participant FIFO as /tmp/radio_stream_*.pcm<br/>名前付き FIFO
    participant WAV as recordings/*.wav<br/>録音中ファイル

    Browser->>webui: GET /stream/{station_id}

    alt タイムシフト（同局が IN_PROGRESS にある場合）
        webui->>transcoder: stream_growing_wav_as_mp3(wav_path, is_still_recording)
        transcoder->>ffmpeg: Popen(ffmpeg -f s16le -ar 16000 -ac 1 -i pipe:0 -f mp3 -b:a 64k -)
        transcoder->>transcoder: feed() daemon スレッド起動
        webui-->>Browser: StreamingResponse(generate(), media_type="audio/mpeg")
        loop feed() daemon スレッド
            transcoder->>WAV: open(wav_path).seek(44).read(4096)
            transcoder->>ffmpeg: stdin.write(pcm_chunk)
            ffmpeg-->>Browser: stdout.read(4096) → MP3 yield
        end
        Note over transcoder,WAV: is_still_recording() が False になると feed() 終了 → ffmpeg 完了

    else ライブ（通常）
        webui->>webui: _sdr_lock.acquire()
        webui->>FIFO: os.mkfifo(fifo_path)
        webui->>FIFO: os.open(O_RDONLY|O_NONBLOCK) → rfd
        webui->>receivers: StreamingFMReceiver(freq, fifo_path)<br/>または StreamingAMReceiver(freq, fifo_path)
        webui->>receivers: receiver.start()
        receivers->>SDR: osmosdr.source 初期化・IQ サンプル取得開始
        receivers-->>FIFO: int16 PCM 書き込み（連続）
        webui-->>Browser: StreamingResponse(generate(), media_type="audio/mpeg")
        Note over webui,Browser: generate() ジェネレータが走り始める
        webui->>transcoder: stream_fd_as_mp3(rfd)
        transcoder->>ffmpeg: Popen(ffmpeg -f s16le -ar 16000 -ac 1 -i pipe:0 -f mp3 -b:a 64k -)
        transcoder->>transcoder: feed() daemon スレッド起動<br/>（先頭 3 秒は無音で GNU Radio 起動待ち）
        loop PCM → MP3 変換ループ
            FIFO-->>transcoder: read(4096) PCM チャンク
            transcoder->>ffmpeg: stdin.write(pcm_chunk)
            ffmpeg-->>Browser: stdout.read(4096) → MP3 yield
        end
    end

    Browser->>webui: POST /api/stop-stream
    webui->>receivers: receiver.stop() / receiver.wait()
    webui->>receivers: del receiver → rtlsdr_close()
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

### 停止

- `POST /api/stop-stream` で `receiver.stop()/wait()` → `del receiver`（`rtlsdr_close()` 発火）→ FIFO 削除 → `_sdr_lock.release()`
