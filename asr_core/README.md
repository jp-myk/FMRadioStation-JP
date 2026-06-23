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
| `backend` | モデル由来 | ASR バックエンド種別（`parakeet_cpp` / `llama_mtmd`）|
| `asr_model` | `config/asr.yaml` | 使用モデル（下表のキー）|
| `parakeet_decoder` | モデル由来 | `--decoder` 値（parakeet=`tdt` / nemotron=空＝省略）|
| `parakeet_bin` / `parakeet_model` | env | parakeet.cpp バイナリ / モデルのパス |
| `llama_bin` / `qwen_model` / `qwen_mmproj` | env | llama-mtmd-cli バイナリ / 本体・mmproj GGUF のパス |

### ASR モデルの選択（`config/asr.yaml`）

使用モデルは `config/asr.yaml` の `model:` で選ぶ。各モデルのプロファイル（**backend** と
backend 別パラメータ）は同ファイルの `models:` で定義する（`config/vad.yaml` と同じ流儀。
`ASR_CONFIG` env でパス上書き可）。`ASRConfig(asr_model="…")` でコードからも切替でき、選択に
応じて backend・GGUF パス・各引数が自動で派生する。

| モデル | backend | 備考 |
|--------|---------|------|
| `parakeet-tdt-0.6b-ja` | `parakeet_cpp` | 日本語専用・TDT/CTC ハイブリッド（`--decoder tdt`）|
| `nemotron-3.5-asr-streaming-0.6b` | `parakeet_cpp` | 多言語（40+ locale）・RNN-T（`--decoder` 省略・`--lang ja-JP`）|
| `qwen3-asr-1.7b` | `llama_mtmd` | 多言語・Alibaba Qwen3-ASR。llama.cpp で本体+mmproj GGUF。CPU 可だが 0.6B より重い |

> **parakeet_cpp の注意**: parakeet-cli の `--decoder` は `ctc|tdt` しか受け付けない（`rnnt` は
> 無効値で `unknown --decoder 'rnnt'` エラー）。RNN-T モデル（nemotron）は `--decoder` を**省略**し、
> ランタイムが `arch=rnnt` から自動でデフォルトデコーダ（rnnt_greedy）を選ぶのに任せる。
> nemotron は多言語のため `--lang` は **BCP-47 ロケール `ja-JP`**（bare `ja` は不可）。
> parakeet-tdt は自前スキームの `ja`。
>
> **llama_mtmd の注意**: `qwen3-asr-1.7b` は llama.cpp の `llama-mtmd-cli` で動く別 backend。
> **本体 GGUF + mmproj（音声エンコーダ）GGUF の 2 ファイルが必須**（`ggml-org/Qwen3-ASR-1.7B-GGUF`
> の Q8_0）。`language` は Qwen 流儀の言語名（英語表記 `Japanese`。空で auto）。セットアップは下の
> 「Qwen3-ASR（llama.cpp）」節を参照。
>
> いずれも本パイプラインでは VAD で切った短いセグメント WAV を 1 件ずつ batch 推論するため、
> nemotron のストリーミング用 `--stream` は使わない。

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

`nvidia/nemotron-3.5-asr-streaming-0.6b`（多言語・RNN-T・ストリーミング）も同じ `parakeet-cli`
で動く。parakeet.cpp の変換スクリプトで GGUF 化して `data/models/nemotron-3.5-asr-streaming-0.6b.gguf`
に置き、`config/asr.yaml` の `model:` を `nemotron-3.5-asr-streaming-0.6b` に切替えると
`--decoder` 省略（デフォルトデコーダ＝rnnt_greedy）で呼ばれる（ファイル名は `config/asr.yaml` の
`models:` で変更可、`PARAKEET_MODEL` でパス直接指定も可）。
変換には NeMo 26.06（main 相当）が必要（このモデルの `EncDecRNNTBPEModelWithPrompt` クラスを含む版）。

> **注意（GGUF の形式）**: CrispASR 用 GGUF（`cstr/parakeet-tdt-0.6b-ja-GGUF` 等、metadata が
> フラットな `parakeet.d_model …`）は parakeet-cli では読めず `failed to load model` になる。
> parakeet.cpp は自前の schema（`parakeet.arch` / `parakeet.encoder.*`）を要求するため、必ず上の
> `scripts/convert_ja_gguf.sh`（= `convert_parakeet_to_gguf.py`）で作り直した GGUF を使う。

### Qwen3-ASR（llama.cpp / llama_mtmd backend）

`qwen3-asr-1.7b` は parakeet-cli ではなく **llama.cpp の `llama-mtmd-cli`**（音声マルチモーダル）で
動く別 backend。変換は不要で、**ggml-org が配布する GGUF をそのまま使う**（本体 + mmproj の 2 ファイル）。

```bash
# 1) ランタイム（llama-mtmd-cli）をビルド。BUILD_SHARED_LIBS=OFF で自己完結バイナリにする。
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp && cmake -B build -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
    -DLLAMA_CURL=OFF -DLLAMA_BUILD_SERVER=OFF && cmake --build build -j --target llama-mtmd-cli
#   → build/bin/llama-mtmd-cli

# 2) GGUF（Q8_0 本体 + mmproj 音声エンコーダ, 計 ~2.3GB）を data/models/ に取得
INSTALL_QWEN_ASR=1 ./scripts/install_models.sh
#   → data/models/Qwen3-ASR-1.7B-Q8_0.gguf
#   → data/models/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf

export LLAMA_MTMD_BIN=/path/to/llama.cpp/build/bin/llama-mtmd-cli
# 個別パス上書き: QWEN_ASR_MODEL（本体）/ QWEN_ASR_MMPROJ（mmproj）
```

`config/asr.yaml` の `model:` を `qwen3-asr-1.7b` に切替えると、各セグメントは
`llama-mtmd-cli -m <本体> --mmproj <mmproj> --audio <wav> -p <prompt>` で認識される。
**mmproj は必須**（無いと ASR を無効化して配信継続）。CPU でも動くが 1.7B のため 0.6B モデルより遅い。
`prompt` は `config/asr.yaml` で調整可（空なら `language` から既定のプロンプトを生成）。

> Docker では `DockerFile` の `llama-builder` ステージで `llama-mtmd-cli` をビルドして
> `/usr/local/bin/llama-mtmd-cli` へコピーし（`docker-compose` の `LLAMA_MTMD_BIN`）、GGUF は
> `./data/models` をマウントして供給する（イメージ非同梱）。

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
