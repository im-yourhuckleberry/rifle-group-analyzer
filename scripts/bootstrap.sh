#!/usr/bin/env bash
# Create .venv (Python 3.14) from the hash-locked dev dependencies and install Playwright Chromium.
# Usage: ./scripts/bootstrap.sh            (idempotent, non-interactive, safe to re-run)
# Env:   BOOTSTRAP_PYTHON           Python 3.14 interpreter (default /opt/homebrew/bin/python3.14)
#        PLAYWRIGHT_BROWSERS_PATH   browser cache dir (default <root>/.playwright-browsers)
# Writes only under the project root: .venv/, .playwright-browsers/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PIP_REQUIRE_VIRTUALENV=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/.playwright-browsers}"

is_py314() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 14) else 1)'
}

if [ -n "${BOOTSTRAP_PYTHON:-}" ]; then
  PY="$BOOTSTRAP_PYTHON"
elif [ -x /opt/homebrew/bin/python3.14 ]; then
  PY=/opt/homebrew/bin/python3.14
else
  PY="$(command -v python3.14 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ] || ! is_py314 "$PY"; then
  echo "bootstrap: a Python 3.14 interpreter is required (got '${PY:-none}'; set BOOTSTRAP_PYTHON)" >&2
  exit 1
fi

VENV_PY="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "bootstrap: creating .venv with $PY"
  "$PY" -m venv "$ROOT/.venv"
fi
if ! is_py314 "$VENV_PY"; then
  echo "bootstrap: existing .venv is not Python 3.14; remove .venv and re-run" >&2
  exit 1
fi

echo "bootstrap: installing requirements-dev.lock (hash-verified)"
"$VENV_PY" -m pip install --quiet --require-hashes --no-deps --only-binary=:all: \
  -r "$ROOT/requirements-dev.lock"
"$VENV_PY" -m pip check

# Make `rga` importable from src/ without an editable install (no unpinned build backend).
SITE_PACKAGES="$("$VENV_PY" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
printf '%s\n' "$ROOT/src" > "$SITE_PACKAGES/rga_src.pth"

echo "bootstrap: installing Playwright Chromium into $PLAYWRIGHT_BROWSERS_PATH"
"$ROOT/.venv/bin/playwright" install chromium

(cd / && "$VENV_PY" -c 'import rga, cv2; print("bootstrap: ok, rga importable, cv2", cv2.__version__)')
