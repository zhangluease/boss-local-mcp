#!/bin/zsh
set -eu
TASK_DIR="${0:A:h}"
export DYLD_LIBRARY_PATH="/opt/homebrew/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
cd "$TASK_DIR"
exec "$TASK_DIR/.venv/bin/python" -m cli check "$@"
