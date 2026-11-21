# Wavefall Media Downloader

A simple desktop app for downloading video and audio from links, built as a lighter
replacement for JDownloader. It's a Python + PySide6 (Qt) window on top of
[yt-dlp](https://github.com/yt-dlp/yt-dlp), with ffmpeg handling audio conversion and merging.

![Wavefall Media Downloader window](docs/screenshot.png)

## Features

- **Queue list**: add as many links as you like. Each one gets a row with a progress bar,
  size, speed and time left. Up to "At once" downloads run in parallel (default 2).
- **Video or audio only**: pick a video quality (Best, 2160p … 360p), or for audio only pick
  **MP3, M4A or Opus** and a bitrate. At each resolution the app prefers H.264/AAC `.mp4`
  files, so they play everywhere.
- **Playlists**: tick *Whole playlist* and every item in a playlist link is added to the
  queue, saved in a sub-folder named after the playlist. Leave it unticked to fetch just the
  one video, even if the link contains `&list=…`.
- **Pause / Resume / Cancel / Retry**: pausing keeps the partial file and resuming carries on
  from there. Cancelling deletes the partial file. Retry restarts a failed item. Hover over a
  failed row, or double-click it, to see the error.
- **History**: the list is kept between sessions. Downloads that were running when you closed
  the app come back as *Paused*, so you can resume them.
- **Save folder**: *Change…* picks the folder and *Open* opens it. Double-click a finished row
  to play the file. Right-click a row for more options.
- You can paste several links at once, separated by spaces or new lines.

It doesn't do anything to get around DRM, paywalls or logins. It downloads what a site serves
publicly.

## Setup (one time)

1. **ffmpeg** is needed for MP3/M4A/Opus conversion and for the best video quality, where video
   and audio are merged. It's already installed on this machine. If it ever isn't:

   ```bash
   sudo apt install ffmpeg
   ```

   Without ffmpeg the app still works, but it saves audio in its original format and only
   fetches video formats that come as a single file. A yellow notice in the window explains this.

2. **App environment** (no sudo needed). This creates `.venv/` in this folder, installs PySide6 and
   a current yt-dlp into it, and adds *Wavefall Media Downloader* to the Cinnamon menu:

   ```bash
   ~/projects/wavefall-media-downloader/setup.sh
   ```

   The app uses its own up-to-date yt-dlp from `.venv`, not the much older `yt-dlp` apt package.

## Launching

- From the **Cinnamon menu**: search for *Wavefall* or "downloader". It is listed under Internet.
  Right-click it and choose *Add to panel* or *Add to desktop* if you want a shortcut.
- From a terminal:

  ```bash
  ~/projects/wavefall-media-downloader/run.sh
  ```

## Keeping it working

Video sites change often. If downloads that used to work start failing, update yt-dlp:

```bash
~/projects/wavefall-media-downloader/update.sh
```

YouTube also needs a JavaScript runtime. The app finds `deno` or `node` on its own (including
node installed through nvm), so nothing extra is needed here.

## Files

| Path | What it is |
| --- | --- |
| `wavefall.py` | The whole app |
| `run.sh` | Starts the app with the bundled environment |
| `setup.sh` | Creates `.venv`, installs dependencies, adds the menu entry |
| `update.sh` | Updates yt-dlp |
| `wavefall-media-downloader.svg` | The app icon. `docs/icons/` has the other designs that were tried |
| `~/.local/share/wavefall-media-downloader/history.json` | The saved download list |
| `~/.config/wavefall-media-downloader/wavefall-media-downloader.conf` | Remembered settings (folder, quality, window size) |
| `~/.local/share/applications/wavefall-media-downloader.desktop` | Menu entry |
| `~/.local/share/icons/hicolor/*/apps/wavefall-media-downloader.*` | App icon (from `wavefall-media-downloader.svg`, installed by `install-icon.py`) |

To uninstall, delete this folder and the files listed above.
