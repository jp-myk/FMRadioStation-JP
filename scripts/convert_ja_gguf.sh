#!/usr/bin/env bash
# nvidia/parakeet-tdt_ctc-0.6b-ja を parakeet.cpp 互換 GGUF へ変換し、
# data/models/parakeet-tdt-0.6b-ja.gguf に出力するワンショット変換ヘルパ。
#
# なぜ必要か:
#   CrispASR 用の GGUF（cstr/parakeet-tdt-0.6b-ja-GGUF）は metadata schema が
#   フラット（parakeet.d_model …）で、mudler/parakeet.cpp の loader が要求する
#   schema（parakeet.arch / parakeet.encoder.* …）と異なるため parakeet-cli では
#   "failed to load model" となる。parakeet.cpp 付属の変換スクリプトで作り直す。
#
# 注意:
#   - 推論（parakeet-cli）に torch/NeMo は不要だが、この変換には必要（数 GB）。
#   - GGUF はイメージに焼かず data/models をマウントする設計のため、ホスト側で 1 回実行する。
#   - 環境変数で上書き可: PARAKEET_REF（既定 v0.3.2 = DockerFile の ARG と一致）,
#     MODEL_ID（既定 nvidia/parakeet-tdt_ctc-0.6b-ja）, DTYPE（既定 f16）,
#     PARAKEET_CONVERT_PYTHON（既定 3.11）。
set -euo pipefail

PARAKEET_REF="${PARAKEET_REF:-v0.3.2}"
MODEL_ID="${MODEL_ID:-nvidia/parakeet-tdt_ctc-0.6b-ja}"
DTYPE="${DTYPE:-f16}"
PARAKEET_CONVERT_PYTHON="${PARAKEET_CONVERT_PYTHON:-3.11}"
VENV_DIR=".venv-py${PARAKEET_CONVERT_PYTHON}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# config と同じ既定（MODELS_DIR、無ければ <repo>/data/models）へ出力する。
MODELS_DIR="${MODELS_DIR:-${REPO_ROOT}/data/models}"
OUT="${MODELS_DIR}/parakeet-tdt-0.6b-ja.gguf"
WORK="${REPO_ROOT}/.cache/parakeet.cpp"

command -v git >/dev/null 2>&1 || { echo "git が必要です。" >&2; exit 1; }

# 1) parakeet.cpp（変換スクリプト一式）を pin タグで取得。
#    変換は Python スクリプトのみ使うため submodule（ggml）は不要 → 軽量な shallow clone。
if [ ! -d "${WORK}/.git" ]; then
  echo "[convert] cloning mudler/parakeet.cpp@${PARAKEET_REF} → ${WORK}"
  git clone --depth 1 --branch "${PARAKEET_REF}" \
    https://github.com/mudler/parakeet.cpp.git "${WORK}"
fi
cd "${WORK}"

# 2) 変換用の隔離 venv（CPU 版 torch + scripts の依存）。
#    uv があれば使い、無ければ標準の python3 -m venv + pip にフォールバックする。
echo "[convert] setting up venv (Python ${PARAKEET_CONVERT_PYTHON}, cpu torch + scripts/requirements.txt)"
if command -v uv >/dev/null 2>&1; then
  PY="${VENV_DIR}/bin/python"
  if [ ! -x "${PY}" ]; then
    uv venv -p "${PARAKEET_CONVERT_PYTHON}" "${VENV_DIR}"
  else
    echo "[convert] using existing venv: ${WORK}/${VENV_DIR}"
  fi
  # kaldialign（NeMo ASR の依存）はビルド時に cmake コマンドを呼ぶ。
  # macOS のクリーン環境では Homebrew cmake が無いことがあるため、venv 内の
  # cmake wheel を先に入れて PATH に出し、ビルド隔離環境から見えるようにする。
  export PATH="${PWD}/${VENV_DIR}/bin:${PATH}"
  uv pip install --python "${PY}" cmake
  uv pip install --python "${PY}" torch --index-url https://download.pytorch.org/whl/cpu
  uv pip install --python "${PY}" -r scripts/requirements.txt
else
  PYTHON_BIN="${PARAKEET_CONVERT_PYTHON_BIN:-python${PARAKEET_CONVERT_PYTHON}}"
  command -v "${PYTHON_BIN}" >/dev/null 2>&1 || {
    echo "uv か ${PYTHON_BIN} が必要です（例: PARAKEET_CONVERT_PYTHON_BIN=/path/to/python3.11）。" >&2
    exit 1
  }
  PY="./${VENV_DIR}/bin/python"
  if [ ! -x "${PY}" ]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  else
    echo "[convert] using existing venv: ${WORK}/${VENV_DIR}"
  fi
  export PATH="${PWD}/${VENV_DIR}/bin:${PATH}"
  "${PY}" -m pip install --upgrade pip
  "${PY}" -m pip install cmake
  "${PY}" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
  "${PY}" -m pip install -r scripts/requirements.txt
fi

# 3) 変換 → data/models へ F16 で出力。失敗時に既存ファイルを壊さないよう .new に書いて差し替える。
echo "[convert] converting ${MODEL_ID} (${DTYPE}) → ${OUT}"
mkdir -p "$(dirname "${OUT}")"
"${PY}" scripts/convert_parakeet_to_gguf.py \
  --model "${MODEL_ID}" \
  --dtype "${DTYPE}" \
  --output "${OUT}.new"
mv -f "${OUT}.new" "${OUT}"

echo "[convert] done: ${OUT}"
