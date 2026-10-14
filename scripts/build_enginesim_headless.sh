#!/usr/bin/env bash
# Build a headless (no GUI) driver for AngeTheGreat's engine-sim, for the OPTIONAL
# OpenTune integration. Nothing in OpenTune needs this; the lab has its own model.
#
# Pinned sources (all MIT licensed — see third_party/engine-sim-headless/README.md):
#   engine-sim                    85f7c3b959a908ed5232ede4f1a4ac7eafe6b630
#   piranha                       004fa153  (= the submodule commit 432f0b1 plus its MIT LICENSE file)
#   csv-io, simple-2d-constraint-solver: the commits engine-sim pins as submodules
# delta-studio (the GUI/audio engine, Windows-only) is replaced by two empty stub headers.
#
# Requirements (Debian/Ubuntu): git cmake clang flex bison libboost-filesystem-dev
# macOS: xcode-select --install; brew install cmake flex bison boost
#
# Usage: scripts/build_enginesim_headless.sh [work-dir]
# Then:  export OPENTUNE_ENGINESIM_BIN=<work-dir>/build/headless_driver
#        export OPENTUNE_ENGINESIM_ASSETS=<work-dir>/engine-sim/assets
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/third_party/engine-sim-headless"
WORK="${1:-$HOME/.cache/opentune/engine-sim}"
ENGINE_SIM_COMMIT=85f7c3b959a908ed5232ede4f1a4ac7eafe6b630
PIRANHA_COMMIT=004fa153

for tool in git cmake flex bison; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
CXX_BIN="${CXX:-$(command -v clang++ || command -v c++)}"
CC_BIN="${CC:-$(command -v clang || command -v cc)}"

mkdir -p "$WORK"
cd "$WORK"
if [ ! -d engine-sim/.git ]; then
  git clone https://github.com/ange-yaghi/engine-sim.git engine-sim
fi
cd engine-sim
git fetch --quiet origin
git checkout --quiet "$ENGINE_SIM_COMMIT"
git submodule update --init dependencies/submodules/csv-io dependencies/submodules/simple-2d-constraint-solver \
  dependencies/submodules/piranha
git -C dependencies/submodules/piranha fetch --quiet origin
git -C dependencies/submodules/piranha checkout --quiet "$PIRANHA_COMMIT"
test -f dependencies/submodules/piranha/LICENSE || { echo "piranha LICENSE missing — wrong commit?" >&2; exit 1; }

# MSVC-only constructs that GCC/Clang reject (inline constexpr, missing includes, ...).
if ! git diff --quiet; then git checkout -- . ; fi
git -C dependencies/submodules/simple-2d-constraint-solver checkout -- .
patch -p1 --forward < "$SRC/headless.patch"

# piranha's CMake fetches googletest as a zip archive; use a git checkout of the same commit
# instead (works behind proxies that block archive downloads, and offline after the first run).
cd "$WORK"
GTEST_COMMIT=609281088cfefc76f9d0ce82e1ff6c30cc3591e5
if [ ! -d googletest/.git ]; then
  git clone --quiet https://github.com/google/googletest.git googletest
fi
git -C googletest checkout --quiet "$GTEST_COMMIT"

cmake -S "$SRC" -B build -DENGINE_SIM_DIR="$WORK/engine-sim" -DCMAKE_BUILD_TYPE=Release \
  -DFETCHCONTENT_SOURCE_DIR_GOOGLETEST="$WORK/googletest" \
  -DCMAKE_CXX_COMPILER="$CXX_BIN" -DCMAKE_C_COMPILER="$CC_BIN"
cmake --build build --target headless_driver -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

echo
echo "Built $WORK/build/headless_driver"
echo "export OPENTUNE_ENGINESIM_BIN=$WORK/build/headless_driver"
echo "export OPENTUNE_ENGINESIM_ASSETS=$WORK/engine-sim/assets"
