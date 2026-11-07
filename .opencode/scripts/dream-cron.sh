#!/usr/bin/env bash
# Scheduled dream: collect first (no model call when nothing is new), then let the Dreamer consolidate.
# Nothing installs this for you; see .opencode/scheduling/ for a systemd user timer and a crontab line.
set -euo pipefail

ROOT="${ATEAM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"
export PWD="$ROOT"   # opencode resolves its project directory from $PWD

mkdir -p .ateam/knowledge
exec 9>.ateam/knowledge/.dream.lock
if ! flock -n 9; then
  echo "dream already running, skipping" >&2
  exit 0
fi

ATEAM="python3 .opencode/scripts/ateam.py"

# pre-check: exit 20 means nothing new, so no model is called
set +e
$ATEAM knowledge collect --require-new >/dev/null
rc=$?
set -e
if [ "$rc" -eq 20 ]; then
  echo "nothing new to dream about"
  exit 0
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

# `knowledge dream` runs the dreamer with --auto, validates the store, indexes and checkpoints on success only
exec $ATEAM knowledge dream
