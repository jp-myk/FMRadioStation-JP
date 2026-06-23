#!/usr/bin/env bash
# macOS/ローカル実行用に ggml-org/llama.cpp の llama-mtmd-cli をビルドする。
# Qwen3-ASR 等の音声マルチモーダル（llama_mtmd backend）で使う CLI。
#
# 出力:
#   .cache/llama.cpp/build/bin/llama-mtmd-cli
#
# 環境変数で上書き可:
#   LLAMA_REF   llama.cpp のタグ/ブランチ/コミット（既定 b9748）
#   LLAMA_WORK  clone/build 先（既定 <repo>/.cache/llama.cpp）
#   CMAKE_BIN   cmake 実行ファイル（未指定時は PATH 上の cmake、無ければ uv で取得）
set -euo pipefail

LLAMA_REF="${LLAMA_REF:-b9748}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${LLAMA_WORK:-${REPO_ROOT}/.cache/llama.cpp}"
OUT="${WORK}/build/bin/llama-mtmd-cli"

command -v git >/dev/null 2>&1 || { echo "git が必要です。" >&2; exit 1; }

if [ ! -d "${WORK}/.git" ]; then
  echo "[llama-mtmd-cli] cloning ggml-org/llama.cpp@${LLAMA_REF} → ${WORK}"
  # llama.cpp は submodule を使わないため --recursive 不要。
  git clone --depth 1 --branch "${LLAMA_REF}" \
    https://github.com/ggml-org/llama.cpp.git "${WORK}"
else
  echo "[llama-mtmd-cli] using existing source: ${WORK}"
fi

cd "${WORK}"

if [ -n "${CMAKE_BIN:-}" ]; then
  CMAKE="${CMAKE_BIN}"
elif command -v cmake >/dev/null 2>&1; then
  CMAKE="$(command -v cmake)"
else
  command -v uv >/dev/null 2>&1 || {
    echo "cmake が PATH に無く、uv も見つかりません。Homebrew 等で cmake を入れるか uv を入れてください。" >&2
    exit 1
  }
  echo "[llama-mtmd-cli] cmake not found; installing cmake wheel into build venv"
  uv venv -p 3.11 .venv-build
  uv pip install --python .venv-build/bin/python cmake
  CMAKE="${WORK}/.venv-build/bin/cmake"
fi

if [ -x "${OUT}" ]; then
  echo "[llama-mtmd-cli] already built: ${OUT}"
else
  echo "[llama-mtmd-cli] building → ${OUT}"
  "${CMAKE}" -S . -B build -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_NATIVE=ON \
    -DLLAMA_CURL=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_SERVER=OFF
  "${CMAKE}" --build build -j "$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)" \
    --target llama-mtmd-cli
fi

"${OUT}" --help >/dev/null

echo "[llama-mtmd-cli] done: ${OUT}"
echo "[llama-mtmd-cli] local WebUI will auto-detect this path; alternatively export:"
echo "export LLAMA_MTMD_BIN=${OUT}"
