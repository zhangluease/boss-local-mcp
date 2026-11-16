#!/bin/zsh
set -eu
TASK_DIR="${0:A:h}"
cd "$TASK_DIR"
exec "$TASK_DIR/.venv/bin/python" -m cli browser
