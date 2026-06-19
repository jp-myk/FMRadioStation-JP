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

VAD・ASR のモデルは **`data/models/`** に揃えて置く（`MODELS_DIR` で一括変更、
個別は `SILERO_VAD_ONNX` / `PARAKEET_MODEL` で上書き可）。1.4GB の GGUF はイメージに
焼かず `./data` をマウントして供給するため、小さな VAD onnx も同じ場所に統一している。

```bash
mkdir -p data/models
# silero-vad リポジトリ等から v5 の ONNX を取得して data/models/silero_vad.onnx に配置
#   入出力: input[1,512], state[2,1,128], sr(int64) → output[1,1], stateN
```

### 3. ASR ランタイム（parakeet.cpp）＋ GGUF モデル

実ランタイムは [`mudler/parakeet.cpp`](https://github.com/mudler/parakeet.cpp) の
`parakeet-cli`（C++/ggml）。モデルは `nvidia/parakeet-tdt_ctc-0.6b-ja`（日本語専用・
TDT/CTC ハイブリッド）を変換した GGUF を使う。torch/NeMo は不要。

```bash
# 1) ランタイムをビルド（C++17 + cmake のみ。ggml は submodule なので --recursive 必須）
git clone --recursive https://github.com/mudler/parakeet.cpp
cd parakeet.cpp && cmake -B build -DPARAKEET_BUILD_CLI=ON && cmake --build build -j
#   → build/examples/cli/parakeet-cli

# 2) parakeet.cpp 形式の GGUF へ変換して data/models/ に出力（一度きり・torch/NeMo を使用）
./scripts/convert_ja_gguf.sh
#   → data/models/parakeet-tdt-0.6b-ja.gguf（F16, 約1.4GB）
#   nvidia/parakeet-tdt_ctc-0.6b-ja を parakeet.cpp の convert_parakeet_to_gguf.py で変換する。

export PARAKEET_CPP_BIN=/path/to/build/examples/cli/parakeet-cli
export PARAKEET_MODEL=$PWD/data/models/parakeet-tdt-0.6b-ja.gguf
```

> **注意（GGUF の形式）**: CrispASR 用 GGUF（`cstr/parakeet-tdt-0.6b-ja-GGUF` 等、metadata が
> フラットな `parakeet.d_model …`）は parakeet-cli では読めず `failed to load model` になる。
> parakeet.cpp は自前の schema（`parakeet.arch` / `parakeet.encoder.*`）を要求するため、必ず上の
> `scripts/convert_ja_gguf.sh`（= `convert_parakeet_to_gguf.py`）で作り直した GGUF を使う。

CLI は `parakeet-cli transcribe --model <gguf> --input <wav> --decoder tdt --lang ja`。
ハイブリッドモデルのため `--decoder tdt` を明示し、`--lang ja` で言語を固定する。入力は
16kHz / mono、出力は plain text を stdout へ返す。
`PARAKEET_CPP_BIN` が PATH に無い、または `PARAKEET_MODEL` 未設定のときは ASR を
無効化して放送再生は継続する（起動時に一度だけ警告）。

> Docker では `DockerFile` の `asr-builder` ステージで ASR バイナリをビルドして
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
