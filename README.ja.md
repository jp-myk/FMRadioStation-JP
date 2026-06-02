[English](README.md) | 日本語

# Radio Receiver

SDR（Software Defined Radio）デバイスを使って FM ラジオを録音するプログラムです。  
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
- SDR デバイスがホストの USB に接続されていること

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

**前提条件**

- Python 3.10 以上
- GNU Radio (`gnuradio`)
- OsmoSDR ドライバ (`gr-osmosdr`)

```bash
# GNU Radio と OsmoSDR をインストール（Debian/Ubuntu）
sudo apt install gnuradio gr-osmosdr

# uv をインストール
curl -LsSf https://astral.sh/uv/install.sh | sh

# 依存パッケージをインストール
uv pip install --system .
```

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

起動後、ブラウザで `http://localhost:5000` にアクセスします。

ローカル実行の場合：

```bash
python3 radio_scheduler_webui.py
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
python3 radio_scheduler.py --stations FMT,J-WAVE --rec-dir recordings --log-dir log
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
├── log/                       # ログ保存先
└── recordings/                # 録音データ保存先
```
