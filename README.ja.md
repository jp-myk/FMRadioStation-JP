[English](README.md) | 日本語

# FM Radio Station JP

SDR（Software Defined Radio）デバイスを使って FM ラジオの聴取・録音を管理するプログラムです。  
GNU Radioライブラリ(License: GPLv3)を使用したSDRラジオ受信です。  
Radiko の番組表 API を利用して番組単位でスケジュール録音できます。

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
cd radio_receiver

# ログ・録音ディレクトリを作成
mkdir -p log recordings

# state.json を初期化（Web UI を使う場合）
echo '{"scheduled": [], "in_progress": [], "completed": []}' > state.json

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
printf '{"scheduled": [], "in_progress": [], "completed": []}\n' > state.json

# Web UI を起動
uv run python radio_scheduler_webui.py
```

Uvicorn が表示した URL（通常は `http://localhost:5000`）をブラウザで開きます。ポートを固定したい場合は `WEBUI_PORT` を指定します。

```bash
WEBUI_PORT=5002 uv run python radio_scheduler_webui.py
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
printf '{"scheduled": [], "in_progress": [], "completed": []}\n' > state.json

# Web UI を起動
uv run python radio_scheduler_webui.py
```

macOS では AirPlay レシーバー / Control Center が 5000 番ポートを使用していることがあります。Web UI は 5010 番までの空きポートを自動で探すため、Uvicorn の出力に表示された URL を開いてください。ポートを明示することもできます。

```bash
WEBUI_PORT=5002 uv run python radio_scheduler_webui.py
```

Web UI ではなく CLI スケジューラをローカル起動する場合:

```bash
uv run python radio_scheduler.py --stations FMT,J-WAVE --rec-dir recordings --log-dir log
```

macOS で `radio_receiver.py` を使って SDR の直接録音を確認する場合:

```bash
# 例: TOKYO FM を 10 秒録音
uv run python radio_receiver.py \
  --mode fm \
  --station FMT \
  --duration 10 \
  --output recordings/test.wav \
  --sample_rate 2400000 \
  --audio_rate 24000 \
  --bit_rate 16 \
  --gain 40
```

`radio_receiver.py` は `--station` または `--freq` のどちらかで受信対象を指定します。例えば `--freq 80e6` なら 80.0 MHz を直接指定します。`config/tunnels.yaml` を見つけられるよう、リポジトリ直下で実行してください。uv インストール後に `uv: file not found`（または `uv: command not found`）が出る場合は、新しいシェルを開くか、次のように uv を PATH に追加してから再実行します。

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v uv
```

`uv sync` は `pyproject.toml` に宣言された Python 依存関係をインストールします。ただし GNU Radio / SDR 周りはこのプロジェクトの Python パッケージではないため、Homebrew で別途インストールする必要があります。`uv run` で `No module named 'gnuradio'` が出る場合は、通常の分離された uv venv から Homebrew の GNU Radio バインディングが見えていない状態です。`uv venv --clear --system-site-packages --python /opt/homebrew/bin/python3.14` で `.venv` を作り直してから `uv sync` を再実行してください。

`uv run ...` を使う場合、`source .venv/bin/activate` は不要です。uv がプロジェクトの環境内でコマンドを実行します。`python ...` を直接実行したい場合や、環境を対話的に確認したい場合だけ `.venv` を activate してください。

### 音声認識モデル（字幕生成用）

Web UI の自動字幕機能は `data/models/` に置く 2 つのモデルを使います:

- `silero_vad.onnx` — 音声区間検出（ダウンロード）
- `parakeet-tdt-0.6b-ja.gguf` — parakeet.cpp 用の日本語 ASR（`nvidia/parakeet-tdt_ctc-0.6b-ja` から変換）

次のスクリプトで両方をまとめて用意できます:

```bash
./scripts/install_models.sh
```

`silero_vad.onnx` はダウンロードし、parakeet.cpp 形式の日本語 GGUF は公開配布が無いため `nvidia/parakeet-tdt_ctc-0.6b-ja` を `scripts/convert_ja_gguf.sh` で GGUF へ変換します（Python + torch/NeMo を使用、ホスト側で一度だけ）。Qwen3-ASR 用の本体 GGUF と mmproj も既定でダウンロードします（不要な場合は `INSTALL_QWEN_ASR=0`）。自前で変換済み GGUF をホストしている場合は `PARAKEET_GGUF_URL` を指定するとダウンロードに切り替わります:

```bash
PARAKEET_GGUF_URL=https://example.com/parakeet-tdt-0.6b-ja.gguf ./scripts/install_models.sh
```

両モデルは `./data:/app/data` でコンテナにマウントされます（イメージには焼きません）。保存先は `MODELS_DIR`、個別パスは `SILERO_VAD_ONNX` / `PARAKEET_MODEL` で上書きできます。モデルが無い場合でも録音・再生は継続し、字幕のみ無効化されます。

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
uv run python radio_scheduler_webui.py
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
uv run python radio_scheduler.py --stations FMT,J-WAVE --rec-dir recordings --log-dir log
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

`radio_scheduler.py` で指定できるオプションです。

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
radio_receiver/
├── docker-compose.yaml        # Docker Compose 設定
├── DockerFile                 # Docker イメージ定義
├── pyproject.toml             # Python 依存関係
├── radio_scheduler.py         # CLI スケジューラ
├── radio_scheduler_webui.py   # Flask Web UI
├── templates/                 # Web UI テンプレート
├── static/                    # Web UI 静的ファイル
├── state.json                 # 予約状態ファイル（Web UI）
├── scripts/                   # モデル取得・変換スクリプト（install_models.sh, convert_ja_gguf.sh）
├── data/models/               # VAD/ASR モデル（マウント・イメージ非同梱）
├── log/                       # ログ保存先
└── recordings/                # 録音データ保存先
```
