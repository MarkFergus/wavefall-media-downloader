#!/usr/bin/env bash
# One-time setup (no sudo): creates .venv, installs PySide6 + yt-dlp,
# and adds "Wavefall Media Downloader" to the applications menu.
set -e
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"

if ! command -v python3 >/dev/null; then
  echo "Python 3 is required. Install it with: sudo apt install python3 python3-venv" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is required (found $(python3 --version 2>&1))." >&2
  exit 1
fi
if [ ! -d .venv ] && ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
  echo "python3-venv is missing. Install it with: sudo apt install python3-venv" >&2
  exit 1
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install --upgrade -r requirements.txt
chmod +x run.sh update.sh
QT_QPA_PLATFORM=offscreen .venv/bin/python install-icon.py
gtk-update-icon-cache -q -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/wavefall-media-downloader.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Wavefall Media Downloader
GenericName=Media Downloader
Keywords=download;video;audio;mp3;youtube;yt-dlp;
Comment=Download video and audio from links (yt-dlp)
Exec="$DIR/run.sh"
Icon=wavefall-media-downloader
Terminal=false
Categories=Network;FileTransfer;
StartupWMClass=wavefall-media-downloader
DESK
update-desktop-database ~/.local/share/applications 2>/dev/null || true
command -v deno >/dev/null || [ -x ~/.deno/bin/deno ] || command -v node >/dev/null || ls ~/.nvm/versions/node/*/bin/node >/dev/null 2>&1 || \
  echo "NOTE: no JavaScript runtime found. YouTube downloads need Deno: curl -fsSL https://deno.land/install.sh | sh"
command -v ffmpeg >/dev/null || echo "NOTE: ffmpeg not found. For MP3 and best video quality run: sudo apt install ffmpeg"
echo "Done. Launch 'Wavefall Media Downloader' from the menu, or run: $DIR/run.sh"
