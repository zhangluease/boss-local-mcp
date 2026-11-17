#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
uv sync --locked
uv run --locked boss-local-mcp install-browser
uv run --locked boss-local-mcp doctor
