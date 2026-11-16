#!/bin/zsh
set -eu
TASK_DIR="${0:A:h}"
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
export DYLD_LIBRARY_PATH="/opt/homebrew/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
cd "$TASK_DIR"
exec "$PWD/.venv/bin/python" "$PWD/server.py" "$@"
