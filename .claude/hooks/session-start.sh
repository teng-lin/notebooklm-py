#!/bin/bash
set -euo pipefail

# Only run in remote Claude Code on the web environments
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Install all dev + optional extras using the locked lockfile
uv sync --frozen --extra browser --extra dev --extra markdown

# Activate the venv for this session
echo "source $CLAUDE_PROJECT_DIR/.venv/bin/activate" >> "$CLAUDE_ENV_FILE"
