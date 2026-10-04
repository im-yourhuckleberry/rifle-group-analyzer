#!/usr/bin/env bash
# Start the Rifle Group Analyzer server (loopback only) with the project venv.
# Usage: ./scripts/run.sh
# Env (all optional; an explicitly set value is never replaced, an empty value is rejected by config):
#   RGA_DATA_DIR   data directory                       (default <root>/data)
#   RGA_HOST       bind host: 127.0.0.1, ::1, localhost  (default 127.0.0.1)
#   RGA_PORT       bind port                             (default 8765)
#   RGA_LOG_LEVEL  uvicorn log level                     (default info)
# Execs `python -m rga`, which exits 2 without listening on an invalid or non-loopback config.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "run.sh: $VENV_PY not found; run ./scripts/bootstrap.sh first" >&2
  exit 1
fi

export RGA_DATA_DIR="${RGA_DATA_DIR-$ROOT/data}"
export RGA_HOST="${RGA_HOST-127.0.0.1}"
export RGA_PORT="${RGA_PORT-8765}"
export RGA_LOG_LEVEL="${RGA_LOG_LEVEL-info}"

exec "$VENV_PY" -m rga
