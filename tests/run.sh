#!/usr/bin/env bash
# Run the static suite. Uses .pytest-lib (pip --target) when pytest is not installed system-wide.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -d .pytest-lib ]; then export PYTHONPATH="$PWD/.pytest-lib${PYTHONPATH:+:$PYTHONPATH}"; fi
if ! python3 -c "import pytest" 2>/dev/null; then
  python3 -m pip install -q --target .pytest-lib pytest
  export PYTHONPATH="$PWD/.pytest-lib${PYTHONPATH:+:$PYTHONPATH}"
fi
exec python3 -m pytest "$@"
