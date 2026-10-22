#!/bin/bash
# Installs the gungors test environment (pytest-homeassistant-custom-component) in a venv at
# .venv, so `pytest` works in Claude Code cloud sessions. System pip cannot build it, hence the venv.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
VENV="$CLAUDE_PROJECT_DIR/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --quiet --python python3.13 "$VENV"
  else
    python3.13 -m venv "$VENV"
  fi
fi

# Idempotent: already installed packages are skipped (the container is cached after the hook).
if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python "$VENV/bin/python" -r requirements_test.txt
else
  "$VENV/bin/pip" install --quiet -r requirements_test.txt
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export VIRTUAL_ENV=\"$VENV\"" >> "$CLAUDE_ENV_FILE"
  echo "export PATH=\"$VENV/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
