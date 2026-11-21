#!/usr/bin/env bash
# Sites change often; when downloads start failing, update yt-dlp first.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
"$DIR/.venv/bin/python" -m pip install --upgrade "yt-dlp[default]"
