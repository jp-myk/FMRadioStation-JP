English | [日本語](README.ja.md)

# Radio Receiver

A program for recording FM radio using SDR (Software Defined Radio) devices.  
GNU Radio libraries are used for radio reception with SDR.  
Uses the Radiko program guide API to schedule recordings by program.

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
- SDR device connected to host USB port

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

**Prerequisites**

- Python 3.10 or higher
- GNU Radio (`gnuradio`)
- OsmoSDR driver (`gr-osmosdr`)

```bash
# Install GNU Radio and OsmoSDR (Debian/Ubuntu)
sudo apt install gnuradio gr-osmosdr

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv pip install --system .
```

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

After starting, open `http://localhost:5000` in your browser.

To run locally:

```bash
python3 radio_scheduler_webui.py
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
python3 radio_scheduler.py --stations FMT,J-WAVE --rec-dir recordings --log-dir log
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
├── log/                       # Log output directory
└── recordings/                # Recording output directory
```
