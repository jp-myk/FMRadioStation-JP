English | [日本語](README.ja.md)

[![CI](https://github.com/jp-myk/FMRadioStation-JP/actions/workflows/ci.yml/badge.svg)](https://github.com/jp-myk/FMRadioStation-JP/actions/workflows/ci.yml)

# FM Radio Station JP

A program for managing FM radio listening and recording using SDR (Software-Defined Radio) devices.  
GNU Radio libraries(License: GPLv3) are used for radio reception with SDR.  
Uses the Radiko program guide API to schedule recordings by program.

## Key Features

- **📻 Works without internet** — Reception is done entirely by the SDR device over the airwaves, not by streaming. As long as the radio signal reaches your antenna, you can **listen and record even with no Wi‑Fi / internet connection**. The "On Air" page lists every station with a play button regardless of connectivity; only the Radiko program guide (titles/schedule) needs internet, and its absence simply hides the program names — playback and recording keep working.
- **🗓️ Program-based scheduled recording** via the Radiko program guide (when online).
- **📝 Automatic subtitles** for live playback and recordings using on-device speech recognition (optional).

## Required Hardware

- USB SDR device compatible with OsmoSDR (e.g. RTL-SDR)

## Supported Stations

| Type | Station ID | Station Name |
|------|-----------|--------------|
| FM | FMT | TOKYO FM |
| FM | J-WAVE | J-WAVE |
| FM | NHK-FM | NHK-FM Tokyo |
| FM | NACK5 | NACK5 |
| FM | BAYFM | bayfm |
| FM | FMYOKOHAMA | FM Yokohama |
| FM | FMFUJI | FM FUJI |
| FM | INT | InterFM |
| FM | MUSASHINO | Musashino FM |
| FM | SETAGAYA | FM Setagaya |
| FM | COMFM | Community FM |
| FM | FM-TOSHIMA | FM Toshima |
| FM | KATSUSHIKA | Katsushika FM |
| FM | TAMALAKE | Tama Lakeside FM |
| FM | KAWASAKI | Kawasaki FM |
| FM | FM-TACHIKAWA | FM Tachikawa |
| FM | CHOFU | Chofu FM |
| AM | NHK-R2 | NHK Radio 2 |
| AM | AFN | AFN Eagle 810 |
| Wide FM | NHK-R1 | NHK Radio 1 (FM 91.5 MHz) |
| Wide FM | TBS | TBS Radio (FM 90.5 MHz) |
| Wide FM | BUNKA | Bunka Hoso (FM 91.6 MHz) |
| Wide FM | NIPPON | Nippon Hoso (FM 93.0 MHz) |
| Wide FM | RADIO-NIHON | Radio Nippon (FM 92.4 MHz) |

---

## Setup

### Using Docker (Recommended)

**Prerequisites**

- Docker / Docker Compose installed
- A Docker daemon is running via Docker Desktop, Colima, OrbStack, or similar
- Docker CLI buildx plugin is available (`docker buildx version`)
- SDR device connected to host USB port

On macOS, `failed to connect to the docker API at unix:///var/run/docker.sock` means the Docker daemon is not running. Start Docker Desktop or your Docker runtime, then retry.
If `Docker Compose requires buildx plugin to be installed` appears, install/enable the buildx plugin (it is normally bundled with Docker Desktop).

**Steps**

```bash
# Clone the repository
git clone <repository-url>
cd radio_receiver

# Create log and recording directories
mkdir -p log recordings

# Initialize state.json (required for Web UI)
echo '{"scheduled": [], "in_progress": [], "completed": []}' > state.json

# Build the image
docker compose build
```

### Running Locally

The local Python dependencies are managed by `uv`. SDR support depends on native GNU Radio packages, so install those with your OS package manager first.

#### Ubuntu / Debian

```bash
# Install GNU Radio and the OsmoSDR source block
sudo apt update
sudo apt install -y gnuradio gr-osmosdr ffmpeg

# Install uv if it is not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create a venv that can see GNU Radio's system Python bindings, then sync Python dependencies
uv venv --clear --system-site-packages --python python3
uv sync

# Create runtime directories
mkdir -p log recordings data
printf '{"scheduled": [], "in_progress": [], "completed": []}\n' > state.json

# Start the Web UI
uv run python radio_scheduler_webui.py
```

Open the URL printed by Uvicorn, normally `http://localhost:5000`. To force a port, set `WEBUI_PORT`, for example:

```bash
WEBUI_PORT=5002 uv run python radio_scheduler_webui.py
```

#### macOS

```bash
# Install GNU Radio, RTL-SDR support, and the SoapySDR RTL-SDR driver
brew install gnuradio librtlsdr soapysdr soapyrtlsdr ffmpeg

# Install uv if it is not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create a venv that can see GNU Radio's Homebrew Python bindings, then sync Python dependencies
uv venv --clear --system-site-packages --python /opt/homebrew/bin/python3.14
uv sync

# Create runtime directories
mkdir -p log recordings data
printf '{"scheduled": [], "in_progress": [], "completed": []}\n' > state.json

# Start the Web UI
uv run python radio_scheduler_webui.py
```

On macOS, port 5000 is sometimes already used by AirPlay Receiver / Control Center. The Web UI automatically tries the next free port up to 5010; check the Uvicorn output and open that URL. You can also choose the port explicitly:

```bash
WEBUI_PORT=5002 uv run python radio_scheduler_webui.py
```

To run the CLI scheduler locally instead of the Web UI:

```bash
uv run python radio_scheduler.py --stations FMT,J-WAVE --rec-dir recordings --log-dir log
```

To run a direct SDR recording check with `radio_receiver.py` on macOS:

```bash
# Example: record TOKYO FM for 10 seconds
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

`radio_receiver.py` accepts either `--station` or `--freq`; for example, `--freq 80e6` tunes 80.0 MHz directly. Run it from the repository root so `config/tunnels.yaml` can be found. If `uv: file not found` (or `uv: command not found`) appears after installing uv, open a new shell or add uv to your PATH, for example:

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v uv
```

`uv sync` installs the Python dependencies declared in `pyproject.toml`. GNU Radio / SDR support still must be installed separately with Homebrew because those native packages are not Python packages in this project. If `No module named 'gnuradio'` appears under `uv run`, recreate `.venv` with `uv venv --clear --system-site-packages --python /opt/homebrew/bin/python3.14` and run `uv sync` again; a normal isolated uv venv cannot import Homebrew's GNU Radio bindings.

You do not need to run `source .venv/bin/activate` when using `uv run ...`; uv automatically runs the command inside the project environment. Activate `.venv` only if you want to run commands directly as `python ...` or inspect the environment interactively.

### Speech Recognition Models (for auto-subtitles)

The Web UI's auto-subtitle feature uses models placed under `data/models/`:

- `silero_vad.onnx` — voice activity detection (downloaded)
- `parakeet-tdt-0.6b-ja.gguf` — Japanese ASR for parakeet.cpp (converted from `nvidia/parakeet-tdt_ctc-0.6b-ja`)
- `nemotron-3.5-asr-streaming-0.6b.gguf` — multilingual RNNT streaming ASR for parakeet.cpp (downloaded prebuilt from `mudler/parakeet-cpp-gguf`); this is the **default** model in `config/asr.yaml`

Install them with one script:

```bash
./scripts/install_models.sh
```

This downloads `silero_vad.onnx`; converts `nvidia/parakeet-tdt_ctc-0.6b-ja` to GGUF via `scripts/convert_ja_gguf.sh` (needs Python + torch/NeMo; runs once on the host); downloads the prebuilt nemotron GGUF; and downloads the Qwen3-ASR GGUFs. The nemotron checkpoint is **not** converted locally — its prompt-conditioned RNNT reference class is unreleased in PyPI NeMo, so the already-converted parakeet.cpp-format GGUF from `mudler/parakeet-cpp-gguf` is downloaded instead.

Skip any of them with `INSTALL_PARAKEET_JA=0`, `INSTALL_NEMOTRON=0`, or `INSTALL_QWEN_ASR=0`. For parakeet-ja, set `PARAKEET_GGUF_URL` to download a prebuilt GGUF instead of converting. For nemotron, pick a quantized variant with `NEMOTRON_GGUF_FILE` (e.g. `nemotron-3.5-asr-streaming-0.6b-q8_0.gguf`) or override the source entirely with `NEMOTRON_GGUF_URL`:

```bash
# Only the default (nemotron) model, smaller q8_0 quant
INSTALL_PARAKEET_JA=0 INSTALL_QWEN_ASR=0 \
  NEMOTRON_GGUF_FILE=nemotron-3.5-asr-streaming-0.6b-q8_0.gguf \
  ./scripts/install_models.sh
```

The models are mounted into the container via `./data:/app/data` (not baked into the image). Override the directory with `MODELS_DIR`, or individual paths with `SILERO_VAD_ONNX` / `PARAKEET_MODEL`. If the model selected in `config/asr.yaml` is absent, recording/playback still works — only subtitles are disabled.

#### ASR runtime CLIs (local runs only)

The ASR backends shell out to native CLI binaries. With Docker these are built by the image (`parakeet-cli` and `llama-mtmd-cli`), so nothing extra is needed. **When running locally without Docker, build them yourself:**

```bash
# parakeet-cli — for the parakeet_cpp backend (default Japanese ASR)
./scripts/install_parakeet_cli.sh

# llama-mtmd-cli — for the llama_mtmd backend (Qwen3-ASR, etc.)
./scripts/install_llama_cli.sh
```

Each script clones and builds under `.cache/parakeet.cpp/` / `.cache/llama.cpp/`, and the Web UI auto-detects the resulting binaries. To point at a custom build instead, export `PARAKEET_CPP_BIN` / `LLAMA_MTMD_BIN`. The scripts need `git` and `cmake` (a `cmake` wheel is fetched via `uv` if it is missing). These steps are not required for Docker runs.

---

## Starting and Stopping

The CLI scheduler and Web UI both occupy the same SDR device, so run **only one at a time**.

### Web UI Mode (Browse program guide and make reservations)

```bash
# Start
docker compose --profile web up -d

# Stop
docker compose --profile web down
```

After starting, open `http://localhost:5001` in your browser.

To run locally:

```bash
uv run python radio_scheduler_webui.py
```

### CLI Scheduler Mode (Headless automatic recording)

```bash
# Start (specify target stations with --stations)
docker compose --profile cli up -d

# Stop
docker compose --profile cli down
```

The default recording station is `FMT`. To change it, edit the `command` section in `docker-compose.yaml`.

To run locally:

```bash
uv run python radio_scheduler.py --stations FMT,J-WAVE --rec-dir recordings --log-dir log
```

### Viewing Logs

```bash
# Docker
docker compose logs -f

# Log files
tail -f log/*.log
```

---

## CLI Options

Options available for `radio_scheduler.py`:

| Option | Default | Description |
|--------|---------|-------------|
| `--stations` | (required) | Comma-separated station IDs to record, e.g. `FMT,J-WAVE,NHK-R1` |
| `--rec-dir` | `radio_recordings` | Directory to save recording files |
| `--log-dir` | `log` | Directory to save log files |
| `--poll-interval` | `21600` (6 hours) | Interval for fetching program guide (seconds) |
| `--sample-rate` | `2400000` | SDR sample rate (Hz) |
| `--audio-rate` | `24000` | Output WAV sample rate (Hz) |
| `--bit-rate` | `16` | Output WAV bit depth (16 / 24 / 32) |
| `--gain` | `40` | SDR gain (sensitivity) |

---

## Directory Structure

```
radio_receiver/
├── docker-compose.yaml        # Docker Compose configuration
├── DockerFile                 # Docker image definition
├── pyproject.toml             # Python dependencies
├── radio_scheduler.py         # CLI scheduler
├── radio_scheduler_webui.py   # Flask Web UI
├── templates/                 # Web UI templates
├── static/                    # Web UI static files
├── state.json                 # Reservation state file (Web UI)
├── scripts/                   # Model/CLI install helpers (install_models.sh, convert_ja_gguf.sh, install_parakeet_cli.sh, install_llama_cli.sh)
├── data/models/               # VAD/ASR models (mounted, not baked into the image)
├── log/                       # Log output directory
└── recordings/                # Recording output directory
```
