#!/usr/bin/env bash
# macOS/ローカル実行用に mudler/parakeet.cpp の parakeet-cli をビルドする。
#
# 出力:
#   .cache/parakeet.cpp/build/examples/cli/parakeet-cli
#
# 環境変数で上書き可:
#   PARAKEET_REF   parakeet.cpp のタグ/ブランチ/コミット（既定 v0.3.2）
#   PARAKEET_WORK  clone/build 先（既定 <repo>/.cache/parakeet.cpp）
#   CMAKE_BIN      cmake 実行ファイル（未指定時は PATH 上の cmake、無ければ uv で取得）
set -euo pipefail

PARAKEET_REF="${PARAKEET_REF:-v0.3.2}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${PARAKEET_WORK:-${REPO_ROOT}/.cache/parakeet.cpp}"
OUT="${WORK}/build/examples/cli/parakeet-cli"

command -v git >/dev/null 2>&1 || { echo "git が必要です。" >&2; exit 1; }

if [ ! -d "${WORK}/.git" ]; then
  echo "[parakeet-cli] cloning mudler/parakeet.cpp@${PARAKEET_REF} → ${WORK}"
  git clone --depth 1 --branch "${PARAKEET_REF}" \
    https://github.com/mudler/parakeet.cpp.git "${WORK}"
else
  echo "[parakeet-cli] using existing source: ${WORK}"
fi

cd "${WORK}"

# convert_ja_gguf.sh は変換スクリプトだけ使うため submodule 不要だが、
# parakeet-cli の C++ ビルドには ggml submodule が必要。
echo "[parakeet-cli] ensuring submodules"
git submodule update --init --recursive

if [ -n "${CMAKE_BIN:-}" ]; then
  CMAKE="${CMAKE_BIN}"
elif command -v cmake >/dev/null 2>&1; then
  CMAKE="$(command -v cmake)"
else
  command -v uv >/dev/null 2>&1 || {
    echo "cmake が PATH に無く、uv も見つかりません。Homebrew 等で cmake を入れるか uv を入れてください。" >&2
    exit 1
  }
  echo "[parakeet-cli] cmake not found; installing cmake wheel into build venv"
  uv venv -p 3.11 .venv-build
  uv pip install --python .venv-build/bin/python cmake
  CMAKE="${WORK}/.venv-build/bin/cmake"
fi

if [ -x "${OUT}" ]; then
  echo "[parakeet-cli] already built: ${OUT}"
else
  echo "[parakeet-cli] building → ${OUT}"
  "${CMAKE}" -S . -B build -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_NATIVE=ON \
    -DPARAKEET_BUILD_TESTS=OFF
  "${CMAKE}" --build build -j "$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"
fi

"${OUT}" --help >/dev/null

echo "[parakeet-cli] done: ${OUT}"
echo "[parakeet-cli] local WebUI will auto-detect this path; alternatively export:"
echo "export PARAKEET_CPP_BIN=${OUT}"
