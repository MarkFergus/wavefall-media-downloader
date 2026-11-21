#!/usr/bin/env bash
# Updates the app (if installed with git) and its packages, including yt-dlp.
# Sites change often; when downloads start failing, run this first.
set -e
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
if [ -d "$DIR/.git" ] && command -v git >/dev/null; then
  git -C "$DIR" pull --ff-only
fi
"$DIR/.venv/bin/python" -m pip install --upgrade -r "$DIR/requirements.txt"
