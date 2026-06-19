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
#     MODEL_ID（既定 nvidia/parakeet-tdt_ctc-0.6b-ja）, DTYPE（既定 f16）。
set -euo pipefail

PARAKEET_REF="${PARAKEET_REF:-v0.3.2}"
MODEL_ID="${MODEL_ID:-nvidia/parakeet-tdt_ctc-0.6b-ja}"
DTYPE="${DTYPE:-f16}"

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
echo "[convert] setting up venv (cpu torch + scripts/requirements.txt)"
if command -v uv >/dev/null 2>&1; then
  uv venv -p 3.12
  uv pip install torch --index-url https://download.pytorch.org/whl/cpu
  uv pip install -r scripts/requirements.txt
  PY=".venv/bin/python"
else
  command -v python3 >/dev/null 2>&1 || { echo "uv か python3 が必要です。" >&2; exit 1; }
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
  ./.venv/bin/pip install -r scripts/requirements.txt
  PY="./.venv/bin/python"
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
