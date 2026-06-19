# asr_core — VAD + 音声認識（ASR）モジュール

ラジオの音声ストリームを **silero-vad（VAD）→ 発話セグメント化 → parakeet.cpp（ASR）**
の順で処理し、確定テキストを返す非同期モジュール。

```
audio stream (1s int16 PCM)
  → VAD (silero-vad / onnxruntime, 512 sample/frame)
  → SpeechSegmenter (無音終端 / 最大長で確定)
  → ASR (parakeet-tdt_ctc-0.6b-ja via parakeet.cpp, subprocess)
  → ASRResult (final)
```

入出力は既存パイプラインと同じ **16kHz / mono / int16 PCM**（`radio_core/transcoder.py`）。

## 使い方（WebUI 側）

```python
from asr_core import StreamingASRService, ASRConfig

asr = StreamingASRService(ASRConfig())
await asr.start()

await asr.push_audio(chunk)          # 1秒ごとの int16 PCM bytes を投入（即 return）
results = await asr.get_results()     # たまった list[ASRResult] を回収

await asr.aclose()
# または: async with StreamingASRService(ASRConfig()) as asr: ...
```

`ASRResult`: `segment_id`, `text`, `is_final`(現状常に True), `t_start`, `t_end`, `confidence`。

設計上、`push_audio`（音声入力）と ASR 推論は分離されている。`push_audio` は
キューに置くだけで即返り、内部の consumer タスクが VAD・セグメント化を行い、
確定セグメントの推論は `ThreadPoolExecutor(max_workers=1)` 上で順次実行される
（uvicorn のイベントループを塞がない）。

## 主な設定（`ASRConfig`）

| 項目 | 既定 | 意味 |
|------|------|------|
| `sample_rate` | 16000 | 入力サンプルレート |
| `vad_frame_samples` | 512 | VAD フレーム長（16kHz で 32ms。silero 標準）|
| `vad_threshold` | 0.5 | speech 判定確率 |
| `silence_end_ms` | 700 | この無音で final 確定 |
| `min_speech_ms` | 250 | 未満は雑音として破棄 |
| `max_segment_sec` | 18 | 超過で強制 cut |
| `context_sec` | 0.5 | セグメント先頭の前方文脈 |
| `backend` | `parakeet_cpp` | ASR バックエンド種別 |
| `parakeet_bin` / `parakeet_model` | env | parakeet.cpp バイナリ / モデルのパス |

## セットアップ

### 1. Python 依存

```
pip install -e .   # onnxruntime, numpy を含む
```

### 2. silero-vad ONNX モデル

`asr_core/models/silero_vad.onnx` に配置するか、`SILERO_VAD_ONNX` で場所を指定する。

```bash
mkdir -p asr_core/models
# silero-vad リポジトリ等から v5 の ONNX を取得して配置
#   入出力: input[1,512], state[2,1,128], sr(int64) → output[1,1], stateN
```

### 3. ASR ランタイム（CrispASR）＋ GGUF モデル

実ランタイムは [`CrispStrobe/CrispASR`](https://github.com/CrispStrobe/CrispASR) の
`crispasr`（whisper.cpp フォーク、C++/ggml）。GGUF は
[`cstr/parakeet-tdt-0.6b-ja-GGUF`](https://huggingface.co/cstr/parakeet-tdt-0.6b-ja-GGUF)
の **F16**（`nvidia/parakeet-tdt_ctc-0.6b-ja` を変換済み・NeMo とビット一致、CER 6.4%）を使う。
torch/NeMo は不要。

```bash
# 1) ランタイムをビルド（C++17 + cmake のみ）
git clone https://github.com/CrispStrobe/CrispASR
cd CrispASR && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
#   → build/bin/crispasr（静的寄り・ランタイム共有ライブラリ不要）

# 2) 変換済み GGUF を取得（HF_TOKEN で）
curl -fsSL -H "Authorization: Bearer $HF_TOKEN" \
  -o data/models/parakeet-tdt-0.6b-ja.gguf \
  https://huggingface.co/cstr/parakeet-tdt-0.6b-ja-GGUF/resolve/main/parakeet-tdt-0.6b-ja.gguf

export PARAKEET_CPP_BIN=/path/to/build/bin/crispasr
export PARAKEET_MODEL=$PWD/data/models/parakeet-tdt-0.6b-ja.gguf
```

CLI は `crispasr -m <gguf> -f <wav>`。backend（parakeet/TDT）と言語は GGUF
メタデータから自動判定されるため、デコーダ／言語フラグは付けない。
`PARAKEET_CPP_BIN` が PATH に無い、または `PARAKEET_MODEL` 未設定のときは ASR を
無効化して放送再生は継続する（起動時に一度だけ警告）。

> Docker では `DockerFile` の `asr-builder` ステージが `crispasr` をビルドして
> 最終イメージへコピーし、GGUF は `docker-compose.yaml` で `./data/models` を
> マウントして `PARAKEET_MODEL` から参照する（イメージには焼かない）。

## エンドツーエンド手動確認（モデル配置後）

既存録音 WAV（`recordings/*.wav`, 16kHz mono）を 1 秒ずつ流して final テキストを見る:

```python
import asyncio
from asr_core import StreamingASRService, ASRConfig
from asr_core.wav_io import read_wav_int16

async def main(path):
    samples, sr = read_wav_int16(path)
    async with StreamingASRService(ASRConfig()) as asr:
        step = sr  # 1秒
        for i in range(0, len(samples), step):
            await asr.push_audio(samples[i:i+step].tobytes())
            for r in await asr.get_results():
                print(f"[{r.t_start:6.1f}-{r.t_end:6.1f}] {r.text}")
            await asyncio.sleep(0)
        # 終端で flush された分も回収
        for r in await asr.get_results():
            print(f"[{r.t_start:6.1f}-{r.t_end:6.1f}] {r.text}")

asyncio.run(main("recordings/xxxx.wav"))
```

## テスト

```
pytest tests/
```

VAD の実モデルや parakeet.cpp バイナリは不要（subprocess とバックエンドはモック／フェイク）。
`SileroVad` の実モデルを使うテストはモデル未配置時 skip される。
