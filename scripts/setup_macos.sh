#!/usr/bin/env bash
# Create a virtual environment and install OpenTune AI with GUI, serial and dev tools.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "OpenTune AI needs Python 3.10+. Found: $("$PYTHON" --version 2>&1)"
    echo "Install one from https://www.python.org/downloads/macos/ or 'brew install python@3.12',"
    echo "then re-run with: PYTHON=python3.12 ./scripts/setup_macos.sh"
    exit 1
fi

"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

echo
opentune selftest
echo
echo "Done. Launch the app with:"
echo "  source .venv/bin/activate && opentune-gui"
