#!/usr/bin/env bash
# Regenerate requirements.lock and requirements-dev.lock (hash-pinned) from the .in files.
# Usage: ./scripts/lock.sh [--upgrade]
# Env:   BOOTSTRAP_PYTHON  Python 3.14 interpreter (default /opt/homebrew/bin/python3.14)
# Creates .venv-lock/ (gitignored) with pinned pip-tools; never touches .venv or global site-packages.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PIP_REQUIRE_VIRTUALENV=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1

UPGRADE_FLAG=""
case "${1:-}" in
  "") ;;
  --upgrade) UPGRADE_FLAG="--upgrade" ;;
  *) echo "usage: scripts/lock.sh [--upgrade]" >&2; exit 2 ;;
esac

if [ -n "${BOOTSTRAP_PYTHON:-}" ]; then
  PY="$BOOTSTRAP_PYTHON"
elif [ -x /opt/homebrew/bin/python3.14 ]; then
  PY=/opt/homebrew/bin/python3.14
else
  PY="$(command -v python3.14 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ] \
  || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 14) else 1)'; then
  echo "lock.sh: a Python 3.14 interpreter is required (got '${PY:-none}'; set BOOTSTRAP_PYTHON)" >&2
  exit 1
fi

LOCK_VENV="$ROOT/.venv-lock"
LOCK_PY="$LOCK_VENV/bin/python"
if [ ! -x "$LOCK_PY" ]; then
  "$PY" -m venv "$LOCK_VENV"
fi
"$LOCK_PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 14) else 1)' \
  || { echo "lock.sh: .venv-lock is not Python 3.14; remove it and re-run" >&2; exit 1; }

# Pinned lock tooling (pip-tools and its closure).
"$LOCK_PY" -m pip install --quiet --only-binary=:all: \
  "pip==26.2.1" "pip-tools==7.6.1" "build==1.6.1" "click==8.5.0" \
  "packaging==26.3" "pyproject_hooks==1.3.3" "setuptools==84.0.0" "wheel==0.48.0"

compile_lock() {
  local src="$1" out="$2"
  # shellcheck disable=SC2086
  "$LOCK_PY" -m piptools compile --quiet --generate-hashes --allow-unsafe --strip-extras \
    --pip-args "--only-binary=:all:" $UPGRADE_FLAG --output-file "$out" "$src"
}

compile_lock requirements.in requirements.lock
compile_lock requirements-dev.in requirements-dev.lock
echo "lock.sh: wrote requirements.lock and requirements-dev.lock"
