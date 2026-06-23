#!/usr/bin/env bash
# VAD(silero_vad.onnx) と ASR モデルを data/models/ に揃える。
#
#   - silero_vad.onnx : snakers4/silero-vad の v5 ONNX を直接ダウンロード。
#   - parakeet-tdt-0.6b-ja.gguf : parakeet.cpp 形式の公開配布物が無いため、既定では
#       scripts/convert_ja_gguf.sh で nvidia/parakeet-tdt_ctc-0.6b-ja から変換する
#       （torch/NeMo を使う一度きりの処理）。自前変換物を URL でホストしている場合は
#       PARAKEET_GGUF_URL を指定するとそこからダウンロードする。不要なら INSTALL_PARAKEET_JA=0。
#   - nemotron-3.5-asr-streaming-0.6b.gguf : config/asr.yaml の既定モデル（多言語 RNNT
#       ストリーミング）。プロンプト条件付き RNN-T の参照クラスが公開 NeMo に無く
#       ローカル変換できないため、parakeet.cpp 形式の変換済み公開 GGUF
#       （mudler/parakeet-cpp-gguf）をダウンロードする。既定で取得（不要なら INSTALL_NEMOTRON=0）。
#   - Qwen3-ASR-1.7B-Q8_0.gguf / mmproj-Qwen3-ASR-1.7B-Q8_0.gguf :
#       llama_mtmd backend 用。既定で取得する（不要なら INSTALL_QWEN_ASR=0）。
#
# いずれも既に存在すればスキップする（再取得したいときは対象ファイルを削除してから実行）。
#
# 環境変数で上書き可:
#   MODELS_DIR         出力先（既定 <repo>/data/models。config の MODELS_DIR と共有）
#   SILERO_VAD_URL     silero_vad.onnx の取得元 URL
#   INSTALL_PARAKEET_JA parakeet-tdt-0.6b-ja を変換/取得するか（既定 1。0 でスキップ）
#   PARAKEET_GGUF_URL  parakeet-ja gguf を変換せずダウンロードする場合の URL
#   INSTALL_NEMOTRON   nemotron-3.5-asr-streaming-0.6b を取得するか（既定 1。0 でスキップ）
#   NEMOTRON_GGUF_BASE nemotron gguf の取得元 base URL（既定 mudler/parakeet-cpp-gguf）
#   NEMOTRON_GGUF_FILE 取得する gguf ファイル名（既定 f16。量子化版 *-q8_0.gguf 等に差替可）
#   NEMOTRON_GGUF_URL  取得元 URL の完全上書き（指定時は BASE/FILE より優先）
#   DTYPE / PARAKEET_REF  変換時の設定（convert_ja_gguf.sh に渡る）
#   INSTALL_QWEN_ASR   Qwen3-ASR GGUF を取得するか（既定 1。0 でスキップ）
#   QWEN_ASR_BASE      Qwen3-ASR GGUF の取得元 base URL
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="${MODELS_DIR:-${REPO_ROOT}/data/models}"
SILERO_VAD_URL="${SILERO_VAD_URL:-https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx}"
PARAKEET_GGUF_URL="${PARAKEET_GGUF_URL:-}"
NEMOTRON_GGUF_BASE="${NEMOTRON_GGUF_BASE:-https://huggingface.co/mudler/parakeet-cpp-gguf/resolve/main}"
NEMOTRON_GGUF_FILE="${NEMOTRON_GGUF_FILE:-nemotron-3.5-asr-streaming-0.6b-f16.gguf}"
NEMOTRON_GGUF_URL="${NEMOTRON_GGUF_URL:-${NEMOTRON_GGUF_BASE}/${NEMOTRON_GGUF_FILE}}"

VAD_OUT="${MODELS_DIR}/silero_vad.onnx"
GGUF_OUT="${MODELS_DIR}/parakeet-tdt-0.6b-ja.gguf"
NEMOTRON_OUT="${MODELS_DIR}/nemotron-3.5-asr-streaming-0.6b.gguf"

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

# 2) ASR: parakeet-tdt-0.6b-ja.gguf（parakeet.cpp 形式・日本語専用）
if [ "${INSTALL_PARAKEET_JA:-1}" = "0" ]; then
  echo "[install] skip parakeet-tdt-0.6b-ja (INSTALL_PARAKEET_JA=0)"
elif [ -f "${GGUF_OUT}" ]; then
  echo "[install] skip ASR (already exists): ${GGUF_OUT}"
elif [ -n "${PARAKEET_GGUF_URL}" ]; then
  echo "[install] downloading gguf from PARAKEET_GGUF_URL → ${GGUF_OUT}"
  curl -fL --retry 3 -o "${GGUF_OUT}.part" "${PARAKEET_GGUF_URL}"
  mv -f "${GGUF_OUT}.part" "${GGUF_OUT}"
else
  echo "[install] PARAKEET_GGUF_URL 未指定のため convert_ja_gguf.sh で変換します（torch/NeMo を使用）"
  MODELS_DIR="${MODELS_DIR}" "${REPO_ROOT}/scripts/convert_ja_gguf.sh"
fi

# 3) ASR: nemotron-3.5-asr-streaming-0.6b.gguf（parakeet.cpp 形式・多言語 RNNT ストリーミング）
#    config/asr.yaml の既定モデル。プロンプト条件付き RNN-T の参照クラスが公開 NeMo に
#    無くローカル変換できないため、parakeet.cpp が変換・公開した GGUF を直接 DL する。
if [ "${INSTALL_NEMOTRON:-1}" = "0" ]; then
  echo "[install] skip nemotron-3.5-asr-streaming-0.6b (INSTALL_NEMOTRON=0)"
elif [ -f "${NEMOTRON_OUT}" ]; then
  echo "[install] skip ASR (already exists): ${NEMOTRON_OUT}"
else
  echo "[install] downloading gguf → ${NEMOTRON_OUT}"
  echo "[install]   from ${NEMOTRON_GGUF_URL}"
  curl -fL --retry 3 -o "${NEMOTRON_OUT}.part" "${NEMOTRON_GGUF_URL}"
  mv -f "${NEMOTRON_OUT}.part" "${NEMOTRON_OUT}"
fi

# 4) Qwen3-ASR（llama_mtmd backend）: 本体 GGUF + mmproj（音声エンコーダ）GGUF
#    ggml-org/Qwen3-ASR-1.7B-GGUF（gated 無し）の Q8_0 を直接ダウンロード。本体と mmproj の
#    2 ファイルが必須。既定で取得し、不要な環境だけ INSTALL_QWEN_ASR=0 でスキップする。
QWEN_ASR_BASE="${QWEN_ASR_BASE:-https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF/resolve/main}"
if [ "${INSTALL_QWEN_ASR:-1}" != "0" ]; then
  for f in Qwen3-ASR-1.7B-Q8_0.gguf mmproj-Qwen3-ASR-1.7B-Q8_0.gguf; do
    out="${MODELS_DIR}/${f}"
    if [ -f "${out}" ]; then
      echo "[install] skip Qwen3-ASR (already exists): ${out}"
    else
      echo "[install] downloading ${f} → ${out}"
      curl -fL --retry 3 -o "${out}.part" "${QWEN_ASR_BASE}/${f}"
      mv -f "${out}.part" "${out}"
    fi
  done
else
  echo "[install] skip Qwen3-ASR GGUF (INSTALL_QWEN_ASR=0)"
fi

echo "[install] done. models in ${MODELS_DIR}:"
ls -la "${MODELS_DIR}"
