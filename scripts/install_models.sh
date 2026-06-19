#!/usr/bin/env bash
# VAD(silero_vad.onnx) と ASR(parakeet-tdt-0.6b-ja.gguf) のモデルを data/models/ に揃える。
#
#   - silero_vad.onnx : snakers4/silero-vad の v5 ONNX を直接ダウンロード。
#   - parakeet-tdt-0.6b-ja.gguf : parakeet.cpp 形式の公開配布物が無いため、既定では
#       scripts/convert_ja_gguf.sh で nvidia/parakeet-tdt_ctc-0.6b-ja から変換する
#       （torch/NeMo を使う一度きりの処理）。自前変換物を URL でホストしている場合は
#       PARAKEET_GGUF_URL を指定するとそこからダウンロードする。
#
# どちらも既に存在すればスキップする（再取得したいときは対象ファイルを削除してから実行）。
#
# 環境変数で上書き可:
#   MODELS_DIR        出力先（既定 <repo>/data/models。config の MODELS_DIR と共有）
#   SILERO_VAD_URL    silero_vad.onnx の取得元 URL
#   PARAKEET_GGUF_URL gguf を変換せずダウンロードする場合の URL
#   MODEL_ID / DTYPE / PARAKEET_REF  変換時の設定（convert_ja_gguf.sh に渡る）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="${MODELS_DIR:-${REPO_ROOT}/data/models}"
SILERO_VAD_URL="${SILERO_VAD_URL:-https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx}"
PARAKEET_GGUF_URL="${PARAKEET_GGUF_URL:-}"

VAD_OUT="${MODELS_DIR}/silero_vad.onnx"
GGUF_OUT="${MODELS_DIR}/parakeet-tdt-0.6b-ja.gguf"

command -v curl >/dev/null 2>&1 || { echo "curl が必要です。" >&2; exit 1; }
mkdir -p "${MODELS_DIR}"

# 1) VAD: silero_vad.onnx（v5）
if [ -f "${VAD_OUT}" ]; then
  echo "[install] skip VAD (already exists): ${VAD_OUT}"
else
  echo "[install] downloading silero_vad.onnx → ${VAD_OUT}"
  curl -fL --retry 3 -o "${VAD_OUT}.part" "${SILERO_VAD_URL}"
  mv -f "${VAD_OUT}.part" "${VAD_OUT}"
fi

# 2) ASR: parakeet-tdt-0.6b-ja.gguf（parakeet.cpp 形式）
if [ -f "${GGUF_OUT}" ]; then
  echo "[install] skip ASR (already exists): ${GGUF_OUT}"
elif [ -n "${PARAKEET_GGUF_URL}" ]; then
  echo "[install] downloading gguf from PARAKEET_GGUF_URL → ${GGUF_OUT}"
  curl -fL --retry 3 -o "${GGUF_OUT}.part" "${PARAKEET_GGUF_URL}"
  mv -f "${GGUF_OUT}.part" "${GGUF_OUT}"
else
  echo "[install] PARAKEET_GGUF_URL 未指定のため convert_ja_gguf.sh で変換します（torch/NeMo を使用）"
  MODELS_DIR="${MODELS_DIR}" "${REPO_ROOT}/scripts/convert_ja_gguf.sh"
fi

echo "[install] done. models in ${MODELS_DIR}:"
ls -la "${MODELS_DIR}"
