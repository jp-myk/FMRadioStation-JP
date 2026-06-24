[English](README.md) | 日本語

[![CI](https://github.com/jp-myk/FMRadioStation-JP/actions/workflows/ci.yml/badge.svg)](https://github.com/jp-myk/FMRadioStation-JP/actions/workflows/ci.yml)

# FM Radio Station JP

SDR（Software Defined Radio）デバイスを使って FM ラジオの聴取・録音を管理するプログラムです。  
GNU Radioライブラリ(License: GPLv3)を使用したSDRラジオ受信です。  
Radiko の番組表 API を利用して番組単位でスケジュール録音できます。

## 特徴（Keypoint）

- **📻 インターネットが無くてもラジオが聴ける** — 受信はストリーミングではなく SDR デバイスによる電波受信です。アンテナに電波が届く限り、**Wi‑Fi / インターネットが無い環境でも聴取・録音ができます**。「放映中」ページは接続状況に関わらず全局を再生ボタン付きで一覧表示し、インターネットが必要なのは Radiko の番組表（番組名・時刻）だけです。番組表が取れないときは番組名が「番組情報なし」になるだけで、再生・録音はそのまま動作します。
- **🗓️ 番組単位のスケジュール録音** — Radiko 番組表を利用（オンライン時）。
- **📝 自動字幕** — ライブ再生・録音にオンデバイス音声認識で字幕を付与（任意）。

## 必要なハードウェア

- RTL-SDR などの USB 接続 SDR デバイス（OsmoSDR 対応）

## 対応局

| 種別 | 局 ID | 局名 |
|------|-------|------|
| FM | FMT | TOKYO FM |
| FM | J-WAVE | J-WAVE |
| FM | NHK-FM | NHK-FM 東京 |
| FM | NACK5 | NACK5 |
| FM | BAYFM | bayfm |
| FM | FMYOKOHAMA | FMヨコハマ |
| FM | FMFUJI | FM FUJI |
| FM | INT | InterFM |
| FM | MUSASHINO | むさしのFM |
| FM | SETAGAYA | FM世田谷 |
| FM | COMFM | コミュニティFM |
| FM | FM-TOSHIMA | FMとしま |
| FM | KATSUSHIKA | かつしかFM |
| FM | TAMALAKE | 多摩レイクサイドFM |
| FM | KAWASAKI | かわさきFM |
| FM | FM-TACHIKAWA | FMたちかわ |
| FM | CHOFU | 調布FM |
| AM | NHK-R2 | NHKラジオ第2 |
| AM | AFN | AFN Eagle 810 |
| ワイドFM | NHK-R1 | NHKラジオ第1（FM 91.5 MHz） |
| ワイドFM | TBS | TBSラジオ（FM 90.5 MHz） |
| ワイドFM | BUNKA | 文化放送（FM 91.6 MHz） |
| ワイドFM | NIPPON | ニッポン放送（FM 93.0 MHz） |
| ワイドFM | RADIO-NIHON | ラジオ日本（FM 92.4 MHz） |

---

## 開発環境の構築

### Docker を使う場合（推奨）

**前提条件**

- Docker / Docker Compose がインストール済みであること
- Docker Desktop / Colima / OrbStack などの Docker デーモンが起動していること
- Docker CLI の buildx プラグインが使えること（`docker buildx version` で確認）
- SDR デバイスがホストの USB に接続されていること

macOS で `failed to connect to the docker API at unix:///var/run/docker.sock` が出る場合は、Docker デーモンが起動していません。Docker Desktop 等を起動してから再実行してください。
`Docker Compose requires buildx plugin to be installed` が出る場合は buildx プラグインを導入してください（Docker Desktop には通常同梱されています）。

**手順**

```bash
# リポジトリをクローン
git clone <repository-url>
cd FMRadioStation-JP

# ログ・録音・データディレクトリを作成
mkdir -p log recordings data

# state.json を初期化（Web UI を使う場合。/app/data にマウントされる）
echo '{"scheduled": [], "in_progress": [], "completed": []}' > data/state.json

# イメージをビルド
docker compose build
```

### ローカルで直接動かす場合

Python の依存パッケージは `uv` で管理します。SDR 周りは OS ネイティブの GNU Radio パッケージに依存するため、先に OS のパッケージマネージャでインストールしてください。

#### Ubuntu / Debian

```bash
# GNU Radio と OsmoSDR の source block をインストール
sudo apt update
sudo apt install -y gnuradio gr-osmosdr ffmpeg

# uv を未導入の場合はインストール
curl -LsSf https://astral.sh/uv/install.sh | sh

# GNU Radio の system Python バインディングを見える venv を作ってから Python 依存関係を同期
uv venv --clear --system-site-packages --python python3
uv sync

# 実行時ディレクトリを作成
mkdir -p log recordings data
printf '{"scheduled": [], "in_progress": [], "completed": []}\n' > data/state.json

# Web UI を起動
uv run radio-webui
```

Uvicorn が表示した URL（通常は `http://localhost:5000`）をブラウザで開きます。ポートを固定したい場合は `WEBUI_PORT` を指定します。

```bash
WEBUI_PORT=5002 uv run radio-webui
```

#### macOS

```bash
# GNU Radio、RTL-SDR サポート、SoapySDR の RTL-SDR ドライバをインストール
brew install gnuradio librtlsdr soapysdr soapyrtlsdr ffmpeg

# uv を未導入の場合はインストール
curl -LsSf https://astral.sh/uv/install.sh | sh

# GNU Radio の Homebrew Python バインディングを見える venv を作ってから Python 依存関係を同期
uv venv --clear --system-site-packages --python /opt/homebrew/bin/python3.14
uv sync

# 実行時ディレクトリを作成
mkdir -p log recordings data
printf '{"scheduled": [], "in_progress": [], "completed": []}\n' > data/state.json

# Web UI を起動
uv run radio-webui
```

macOS では AirPlay レシーバー / Control Center が 5000 番ポートを使用していることがあります。Web UI は 5010 番までの空きポートを自動で探すため、Uvicorn の出力に表示された URL を開いてください。ポートを明示することもできます。

```bash
WEBUI_PORT=5002 uv run radio-webui
```

Web UI ではなく CLI スケジューラをローカル起動する場合:

```bash
uv run radio-scheduler --stations FMT,J-WAVE --rec-dir recordings --log-dir log
```

macOS で `radio-receiver` を使って SDR の直接録音を確認する場合:

```bash
# 例: TOKYO FM を 10 秒録音
uv run radio-receiver \
  --mode fm \
  --station FMT \
  --duration 10 \
  --output recordings/test.wav \
  --sample_rate 2400000 \
  --audio_rate 24000 \
  --bit_rate 16 \
  --gain 40
```

`radio-receiver` は `--station` または `--freq` のどちらかで受信対象を指定します。例えば `--freq 80e6` なら 80.0 MHz を直接指定します。`config/tunnels.yaml` を見つけられるよう、リポジトリ直下で実行してください。uv インストール後に `uv: file not found`（または `uv: command not found`）が出る場合は、新しいシェルを開くか、次のように uv を PATH に追加してから再実行します。

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v uv
```

`uv sync` は `pyproject.toml` に宣言された Python 依存関係をインストールします。ただし GNU Radio / SDR 周りはこのプロジェクトの Python パッケージではないため、Homebrew で別途インストールする必要があります。`uv run` で `No module named 'gnuradio'` が出る場合は、通常の分離された uv venv から Homebrew の GNU Radio バインディングが見えていない状態です。`uv venv --clear --system-site-packages --python /opt/homebrew/bin/python3.14` で `.venv` を作り直してから `uv sync` を再実行してください。

`uv run ...` を使う場合、`source .venv/bin/activate` は不要です。uv がプロジェクトの環境内でコマンドを実行します。`python ...` を直接実行したい場合や、環境を対話的に確認したい場合だけ `.venv` を activate してください。

### 音声認識モデル（字幕生成用）

Web UI の自動字幕機能は `data/models/` に置くモデルを使います:

- `silero_vad.onnx` — 音声区間検出（ダウンロード）
- `parakeet-tdt-0.6b-ja.gguf` — parakeet.cpp 用の日本語 ASR（`nvidia/parakeet-tdt_ctc-0.6b-ja` から変換）
- `nemotron-3.5-asr-streaming-0.6b.gguf` — parakeet.cpp 用の多言語 RNNT ストリーミング ASR（`mudler/parakeet-cpp-gguf` から変換済みをダウンロード）。`config/asr.yaml` の**既定モデル**

次のスクリプトでまとめて用意できます:

```bash
./scripts/install_models.sh
```

`silero_vad.onnx` はダウンロードし、`nvidia/parakeet-tdt_ctc-0.6b-ja` は `scripts/convert_ja_gguf.sh` で GGUF へ変換します（Python + torch/NeMo を使用、ホスト側で一度だけ）。nemotron は変換済み GGUF をダウンロードし、Qwen3-ASR の本体 GGUF と mmproj も既定でダウンロードします。nemotron は**ローカル変換しません** — プロンプト条件付き RNN-T の参照クラスが公開 NeMo(PyPI) に無いため、parakeet.cpp が変換・公開した `mudler/parakeet-cpp-gguf` の GGUF を取得します。

それぞれ `INSTALL_PARAKEET_JA=0` / `INSTALL_NEMOTRON=0` / `INSTALL_QWEN_ASR=0` で個別にスキップできます。parakeet-ja は `PARAKEET_GGUF_URL` を指定すると変換せず DL に切り替わります。nemotron は `NEMOTRON_GGUF_FILE` で量子化版（例 `nemotron-3.5-asr-streaming-0.6b-q8_0.gguf`）を選べ、`NEMOTRON_GGUF_URL` で取得元を完全に上書きできます:

```bash
# 既定（nemotron）モデルだけを小さい q8_0 量子化版で取得
INSTALL_PARAKEET_JA=0 INSTALL_QWEN_ASR=0 \
  NEMOTRON_GGUF_FILE=nemotron-3.5-asr-streaming-0.6b-q8_0.gguf \
  ./scripts/install_models.sh
```

各モデルは `./data:/app/data` でコンテナにマウントされます（イメージには焼きません）。保存先は `MODELS_DIR`、個別パスは `SILERO_VAD_ONNX` / `PARAKEET_MODEL` で上書きできます。`config/asr.yaml` で選択中のモデルが無い場合でも録音・再生は継続し、字幕のみ無効化されます。

#### ASR ランタイム CLI（ローカル実行時のみ）

ASR バックエンドはネイティブ CLI バイナリを呼び出します。Docker 実行時はイメージのビルド時に同梱される（`parakeet-cli` と `llama-mtmd-cli`）ため追加作業は不要です。**Docker を使わずローカルで直接動かす場合は、これらを自分でビルドしてください:**

```bash
# parakeet-cli — parakeet_cpp バックエンド用（既定の日本語 ASR）
./scripts/install_parakeet_cli.sh

# llama-mtmd-cli — llama_mtmd バックエンド用（Qwen3-ASR など）
./scripts/install_llama_cli.sh
```

各スクリプトは `.cache/parakeet.cpp/` / `.cache/llama.cpp/` 配下に clone・ビルドし、生成されたバイナリは Web UI が自動検出します。別の場所のビルドを使う場合は `PARAKEET_CPP_BIN` / `LLAMA_MTMD_BIN` を export してください。ビルドには `git` と `cmake` が必要です（`cmake` が無ければ `uv` 経由で取得します）。Docker 実行時は本手順は不要です。

---

## サーバ起動・停止方法

CLI スケジューラと Web UI は同じ SDR デバイスを占有するため、**どちらか一方のみ**起動してください。

### Web UI モード（ブラウザで番組表確認・予約）

```bash
# 起動
docker compose --profile web up -d

# 停止
docker compose --profile web down
```

起動後、ブラウザで `http://localhost:5001` にアクセスします。

ローカル実行の場合：

```bash
uv run radio-webui
```

### CLI スケジューラモード（ヘッドレス自動録音）

```bash
# 起動（録音対象局を --stations で指定）
docker compose --profile cli up -d

# 停止
docker compose --profile cli down
```

デフォルトの録音対象局は `FMT` です。変更する場合は `docker-compose.yaml` の `command` セクションを編集してください。

ローカル実行の場合：

```bash
uv run radio-scheduler --stations FMT,J-WAVE --rec-dir recordings --log-dir log
```

### ログ確認

```bash
# Docker の場合
docker compose logs -f

# ログファイル
tail -f log/*.log
```

---

## CLI オプション

`radio-scheduler` で指定できるオプションです。

| オプション | デフォルト | 説明 |
|-----------|-----------|------|
| `--stations` | （必須） | 録音対象局の ID（カンマ区切り）例: `FMT,J-WAVE,NHK-R1` |
| `--rec-dir` | `radio_recordings` | 録音ファイルの保存先ディレクトリ |
| `--log-dir` | `log` | ログファイルの保存先ディレクトリ |
| `--poll-interval` | `21600`（6時間） | 番組表を取得する間隔（秒） |
| `--sample-rate` | `2400000` | SDR サンプルレート（Hz） |
| `--audio-rate` | `24000` | 出力 WAV のサンプルレート（Hz） |
| `--bit-rate` | `16` | 出力 WAV のビット深度（16 / 24 / 32） |
| `--gain` | `40` | SDR ゲイン（感度） |

---

## ディレクトリ構成

```
FMRadioStation-JP/
├── docker-compose.yaml            # Docker Compose 設定
├── DockerFile                     # Docker イメージ定義
├── pyproject.toml                 # パッケージ定義・依存・console scripts
├── src/fm_radio_station/          # Python パッケージ（uv/pip で導入）
│   ├── apps/                      # エントリ（console scripts）
│   │   ├── receiver.py            #   radio-receiver
│   │   ├── scheduler.py           #   radio-scheduler（CLI スケジューラ）
│   │   └── webui.py               #   radio-webui（FastAPI Web UI）
│   ├── radio_core/                # SDR 受信・Radiko・局定義・トランスコード
│   ├── asr_core/                  # 音声認識（字幕）バックエンド
│   ├── web/                       # Web UI 資産（パッケージ同梱）
│   │   ├── templates/             #   Jinja2 テンプレート
│   │   └── static/                #   JS/CSS
│   └── paths.py                   # 設定/データ/キャッシュのパス解決
├── config/                        # 実行時 YAML（tunnels/asr/vad）
├── scripts/                       # モデル取得・変換／CLI ビルド（install_models.sh, convert_ja_gguf.sh, install_parakeet_cli.sh, install_llama_cli.sh）
├── tests/                         # pytest スイート
├── data/                          # モデル(data/models) と state.json（マウント・イメージ非同梱）
├── log/                           # ログ保存先
└── recordings/                    # 録音データ保存先
```

`uv sync`（または `uv pip install .`）後はコンソールスクリプトで起動します: `radio-webui` / `radio-scheduler` / `radio-receiver`（例: `uv run radio-webui`）。
