#!/usr/bin/env bash
# Start Wavefall Media Downloader using the app's own Python environment.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/wavefall.py" "$@"
