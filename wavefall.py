#!/usr/bin/env python3
"""Wavefall Media Downloader - a simple queue-based downloader for video and audio, built on yt-dlp.

Paste a link, choose Video or Audio only, pick a quality, and click Add.
Downloads run in the background with a progress bar per item. The list is
saved between sessions.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QLocale, QObject, QProcess, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFrame, QInputDialog, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QStyle, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

APP_NAME = "Wavefall Media Downloader"
APP_VERSION = "0.2"
APP_REPO = "MarkFergus/wavefall-media-downloader"   # GitHub repo; its releases announce updates
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "wavefall-media-downloader"
HISTORY_FILE = DATA_DIR / "history.json"

VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
AUDIO_FORMATS = ["mp3", "m4a", "opus"]
AUDIO_BITRATES = ["Best", "320 kbps", "256 kbps", "192 kbps", "128 kbps"]

# Status values
QUEUED, RUNNING, PROCESSING, DONE, FAILED, PAUSED, CANCELLED = (
    "Queued", "Downloading", "Processing", "Done", "Failed", "Paused", "Cancelled")
ACTIVE = {RUNNING, PROCESSING}


# --------------------------------------------------------------------------- helpers

def find_ffmpeg() -> str | None:
    local = Path(__file__).resolve().parent / "bin" / "ffmpeg"
    if local.is_file():
        return str(local)
    return shutil.which("ffmpeg")


def find_js_runtimes() -> dict:
    """yt-dlp needs a JavaScript runtime for some sites (e.g. YouTube).
    Desktop launchers often don't have nvm's PATH, so look for node there too."""
    runtimes = {}
    deno = shutil.which("deno") or (str(Path.home() / ".deno/bin/deno")
                                    if (Path.home() / ".deno/bin/deno").is_file() else None)
    if deno:
        runtimes["deno"] = {"path": deno}
    node = shutil.which("node")
    if not node:
        found = sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/node")))
        node = found[-1] if found else None
    if node:
        runtimes["node"] = {"path": node}
    return runtimes


def human_bytes(n: float | None) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_eta(s: float | None) -> str:
    if s is None:
        return ""
    s = int(s)
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}:{s % 60:02d}"


def safe_dirname(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:120] or "Playlist"


# Bracketed clutter in music video titles, e.g. "(Official Video)", "[Lyrics]", "(HD)"
_TITLE_JUNK = re.compile(
    r"\s*[(\[][^)\]]*\b(official|lyrics?|audio|video|visuali[sz]er|hd|hq|4k|mv|m/v)\b[^)\]]*[)\]]",
    re.I)


def clean_title(title: str) -> str:
    t = _TITLE_JUNK.sub("", title)
    t = re.sub(r"\s+\|\s+.*$", "", t)          # "Song | Some Channel"
    return re.sub(r"\s{2,}", " ", t).strip()


def music_name(info: dict) -> tuple[str, bool]:
    """Work out "Artist - Song" for a track. Returns (name, sure); when not sure the
    name is only a best guess and the user should check it."""
    artist = (info.get("artist") or (info.get("artists") or [None])[0]
              or info.get("creator"))
    track = info.get("track")
    if artist and track:
        return f"{artist} - {track}", True
    title = clean_title(info.get("title") or "")
    m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", title)
    if m:
        artist, song = m[1].strip(), m[2].strip().strip('"“”')
        return f"{artist} - {song}", True
    channel = info.get("channel") or info.get("uploader") or ""
    if channel.endswith(" - Topic") and title:     # YouTube's auto-generated artist channels
        return f"{channel[:-8]} - {title}", True
    if artist and title:
        return f"{artist} - {title}", True
    if channel and title:
        return f"{channel} - {title}", False
    return title or info.get("id") or "", False


def subtitle_langs() -> list[str]:
    """Exact codes for the user's language, then English. Kept short on purpose: every code
    is a separate request, and YouTube rate-limits (HTTP 429) when asked for many."""
    lang, _, country = QLocale().name().partition("_")
    codes = [lang, f"{lang}-{country}"] if lang and lang != "C" else []
    codes += ["en", "en-GB", "en-US"]
    return list(dict.fromkeys(c for c in codes if c and not c.endswith("-")))


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/\x00-\x1f]', "_", name).strip(" .")[:180]


def playlist_kind(url: str) -> str | None:
    """Guess from the link alone whether it involves a playlist.

    Returns "playlist" for a link to a playlist itself, "item" for a single video or
    track that carries a playlist with it, or None. A quick guess, so the user can be
    asked before anything is looked up."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    path = u.path.lower().rstrip("/")
    qs = parse_qs(u.query)
    parts = path.split("/")
    if "youtube.com" in host or host == "youtu.be":
        if not qs.get("list"):
            return None
        return "item" if (qs.get("v") or host == "youtu.be" or "/shorts/" in path) else "playlist"
    if "soundcloud.com" in host:
        if "sets" in parts:
            return "playlist"
        return "item" if qs.get("in") else None
    if "bandcamp.com" in host and "album" in parts:
        return "playlist"
    if {"playlist", "playlists", "album", "albums"} & set(parts):
        return "playlist"
    return "item" if qs.get("list") else None


def playlist_url(url: str) -> str:
    """For a track link that names its playlist separately (SoundCloud's ?in=), return
    the playlist's own link, since the track link alone won't expand to the playlist."""
    u = urlparse(url)
    if "soundcloud.com" in (u.hostname or "") and (inn := parse_qs(u.query).get("in")):
        return f"https://soundcloud.com/{inn[0].strip('/')}"
    return url


def version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"wavefall-media-downloader/{APP_VERSION}",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def open_path(path: str) -> None:
    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- data

@dataclass
class Job:
    url: str
    mode: str                     # "video" or "audio"
    quality: str                  # VIDEO_QUALITIES entry
    audio_format: str
    audio_bitrate: str
    folder: str
    subdir: str = ""
    title: str = ""
    filename: str = ""            # "Artist - Song" for audio; empty uses the default name
    subtitles: bool = False       # video only: download and embed subtitles
    status: str = QUEUED
    progress: float = 0.0
    filepath: str = ""
    error: str = ""
    size: float = 0.0
    tmpfiles: list = field(default_factory=list)   # partial files, removed on cancel
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    added: float = field(default_factory=time.time)
    # runtime only (not saved)
    speed: float | None = field(default=None, repr=False)
    eta: float | None = field(default=None, repr=False)

    SAVED = ("url", "mode", "quality", "audio_format", "audio_bitrate", "folder", "subdir",
             "title", "filename", "subtitles", "status", "progress", "filepath", "error", "size", "tmpfiles", "id", "added")

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: d[k] for k in self.SAVED}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        job = cls(**{k: d[k] for k in cls.SAVED if k in d})
        if job.status in ACTIVE or job.status == QUEUED:
            job.status = PAUSED   # interrupted by closing the app; resume manually
        return job

    @property
    def type_label(self) -> str:
        if self.mode == "audio":
            return self.audio_format.upper()
        label = "Video" if self.quality == "Best" else f"Video {self.quality}"
        return label + " + subs" if self.subtitles else label


# --------------------------------------------------------------------------- workers

class Signals(QObject):
    progress = Signal(str, dict)          # job id, fields to update
    finished = Signal(str, str, str)      # job id, final status, message/filepath
    resolved = Signal(list, str, dict)    # list of (url, title, seconds, name, sure) entries, subdir, choice
    resolve_failed = Signal(str, str)     # url, error
    updates_found = Signal(dict)          # {"app": {"version", "url"} | None, "ytdlp": str | None}
    update_done = Signal(bool, str)       # success, message


class StopRequested(DownloadCancelled):
    pass


class QuietLogger:
    """Errors are shown in the app; keep yt-dlp's chatter out of the terminal."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def build_options(job: Job, ffmpeg: str | None, js_runtimes: dict) -> dict:
    outdir = Path(job.folder) / job.subdir if job.subdir else Path(job.folder)
    opts = {
        "logger": QuietLogger(),
        "outtmpl": str(outdir / (safe_filename(job.filename).replace("%", "%%") + ".%(ext)s"
                                 if job.filename else "%(title).180B [%(id)s].%(ext)s")),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "windowsfilenames": False,
        "trim_file_name": 200,
    }
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    if job.mode == "audio":
        if ffmpeg:
            opts["format"] = "bestaudio/best"
            q = "0" if job.audio_bitrate == "Best" else job.audio_bitrate.split()[0]
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": job.audio_format,
                 "preferredquality": q},
                {"key": "FFmpegMetadata"},
            ]
        else:
            # No ffmpeg: grab the audio stream as-is (usually .m4a or .webm)
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        h = None if job.quality == "Best" else int(job.quality.rstrip("p"))
        limit = f"[height<={h}]" if h else ""
        if ffmpeg:
            opts["format"] = f"bestvideo{limit}+bestaudio/best{limit}/best"
            # At the chosen resolution prefer H.264 + m4a, so the .mp4 plays everywhere
            opts["format_sort"] = ["res", "vcodec:h264", "ext:mp4:m4a"]
            opts["merge_output_format"] = "mp4/mkv"
        else:
            opts["format"] = f"best{limit}[acodec!=none][vcodec!=none]/best"
        if job.subtitles:
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True       # used only when there are no real ones
            opts["subtitleslangs"] = subtitle_langs()
            if ffmpeg:   # embed into the video; without ffmpeg they're saved beside it
                opts.setdefault("postprocessors", []).append(
                    {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    return opts


class DownloadWorker(threading.Thread):
    def __init__(self, job: Job, signals: Signals, ffmpeg, js_runtimes):
        super().__init__(daemon=True)
        self.job = job
        self.signals = signals
        self.ffmpeg = ffmpeg
        self.js_runtimes = js_runtimes
        self.stop_reason: str | None = None   # "pause" or "cancel"
        self.streams: list[str] = []
        self.sizes: dict[str, float] = {}
        self._last_emit = 0.0

    def request_stop(self, reason: str) -> None:
        self.stop_reason = reason

    def _hook(self, d: dict) -> None:
        if self.stop_reason:
            raise StopRequested()
        tmp = d.get("tmpfilename")
        if tmp and tmp not in self.job.tmpfiles:
            self.job.tmpfiles.append(tmp)
        status = d.get("status")
        now = time.monotonic()
        if status == "downloading" and now - self._last_emit < 0.25:
            return
        self._last_emit = now
        info = d.get("info_dict") or {}
        upd = {}
        if info.get("title") and not self.job.title:
            upd["title"] = info["title"]
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            # Best video quality downloads video and audio as two streams
            # (files named "….f137.mp4") that ffmpeg merges afterwards; count
            # them as halves of one bar instead of letting the bar restart.
            fname = d.get("filename") or ""
            if fname and fname not in self.streams:
                self.streams.append(fname)
            n = 2 if re.search(r"\.f[\w-]+\.\w+$", fname) else 1
            idx = min(self.streams.index(fname), n - 1) if fname in self.streams else 0
            frac = (done / total) if total else 0
            if total:
                self.sizes[fname] = total
            upd.update(status=RUNNING, progress=(idx + frac) / n * 100,
                       speed=d.get("speed"), eta=d.get("eta"), size=sum(self.sizes.values()))
        elif status == "finished":
            upd.update(speed=None, eta=None)
        self.signals.progress.emit(self.job.id, upd)

    def _pp_hook(self, d: dict) -> None:
        if self.stop_reason:
            raise StopRequested()
        if d.get("status") == "started":
            self.signals.progress.emit(self.job.id, {"status": PROCESSING, "speed": None,
                                                     "eta": None, "progress": 100})

    def _download(self, subtitles: bool) -> dict:
        opts = build_options(self.job, self.ffmpeg, self.js_runtimes)
        if not subtitles:
            for k in ("writesubtitles", "writeautomaticsub", "subtitleslangs"):
                opts.pop(k, None)
            opts["postprocessors"] = [pp for pp in opts.get("postprocessors", [])
                                      if pp["key"] != "FFmpegEmbedSubtitle"]
        opts["progress_hooks"] = [self._hook]
        opts["postprocessor_hooks"] = [self._pp_hook]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.job.url, download=True)
            return ydl.sanitize_info(info)

    def _remove_subtitle_files(self, info: dict) -> None:
        """Delete subtitle files left over from a failed subtitle download."""
        vid = info.get("id")
        if not vid:
            return
        outdir = Path(self.job.folder) / self.job.subdir
        for f in glob.glob(str(outdir / f"*[[]{glob.escape(vid)}[]].*.vtt")) + \
                glob.glob(str(outdir / f"*[[]{glob.escape(vid)}[]].*.srt")):
            try:
                os.remove(f)
            except OSError:
                pass

    def run(self) -> None:
        try:
            try:
                info = self._download(self.job.subtitles)
            except DownloadError as e:
                # A subtitle failure (often YouTube rate-limiting) shouldn't cost the video
                if not (self.job.subtitles and "subtitles" in str(e) and not self.stop_reason):
                    raise
                info = self._download(False)
                self._remove_subtitle_files(info)
            path = ""
            for rd in info.get("requested_downloads") or []:
                path = rd.get("filepath") or path
            self.job.tmpfiles.clear()
            self.signals.finished.emit(self.job.id, DONE, path)
        except DownloadCancelled:
            self._stopped()
        except DownloadError as e:
            if self.stop_reason:
                self._stopped()
            else:
                msg = re.sub(r"^ERROR:\s*", "", str(e))
                msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
                self.signals.finished.emit(self.job.id, FAILED, msg)
        except Exception as e:  # noqa: BLE001 - surface anything to the user
            if self.stop_reason:
                self._stopped()
            else:
                self.signals.finished.emit(self.job.id, FAILED, f"{type(e).__name__}: {e}")

    def _stopped(self) -> None:
        if self.stop_reason == "cancel":
            for f in list(self.job.tmpfiles):
                for p in (f, f + ".ytdl"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            self.signals.finished.emit(self.job.id, CANCELLED, "")
        else:
            self.signals.finished.emit(self.job.id, PAUSED, "")


class ResolveWorker(threading.Thread):
    """Looks up a link first, so playlists can be expanded into separate items."""

    def __init__(self, url: str, want_playlist: bool, choice: dict, signals: Signals,
                 js_runtimes):
        super().__init__(daemon=True)
        self.url, self.want_playlist, self.choice = url, want_playlist, choice
        self.signals, self.js_runtimes = signals, js_runtimes

    def run(self) -> None:
        opts = {"logger": QuietLogger(), "quiet": True, "no_warnings": True,
                "skip_download": True,
                "extract_flat": "in_playlist", "noplaylist": not self.want_playlist}
        if self.js_runtimes:
            opts["js_runtimes"] = self.js_runtimes
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
        except Exception as e:  # noqa: BLE001
            msg = re.sub(r"^ERROR:\s*", "", str(e))
            self.signals.resolve_failed.emit(self.url, re.sub(r"\x1b\[[0-9;]*m", "", msg))
            return
        if info.get("_type") == "playlist" and info.get("entries") is not None:
            entries = []
            for e in info["entries"]:
                if not e:
                    continue
                u = e.get("webpage_url") or e.get("url")
                if u:
                    entries.append((u, e.get("title") or "", e.get("duration"),
                                    *music_name(e)))
            subdir = safe_dirname(info.get("title") or info.get("id") or "Playlist")
            self.signals.resolved.emit(entries, subdir, self.choice)
        else:
            url = info.get("webpage_url") or self.url
            self.signals.resolved.emit(
                [(url, info.get("title") or "", info.get("duration"), *music_name(info))],
                "", self.choice)


class UpdateChecker(threading.Thread):
    """Looks for a newer app release on GitHub and a newer yt-dlp on PyPI. Stays quiet on
    any network error: an update check should never get in the way."""

    def __init__(self, signals: Signals):
        super().__init__(daemon=True)
        self.signals = signals

    def run(self) -> None:
        found = {"app": None, "ytdlp": None}
        try:
            rel = fetch_json(f"https://api.github.com/repos/{APP_REPO}/releases/latest")
            tag = rel.get("tag_name", "")
            if tag and version_tuple(tag) > version_tuple(APP_VERSION):
                found["app"] = {"version": tag.lstrip("vV"), "url": rel.get("html_url", "")}
        except Exception:  # noqa: BLE001  (no releases yet, offline, rate-limited…)
            pass
        try:
            latest = fetch_json("https://pypi.org/pypi/yt-dlp/json")["info"]["version"]
            if version_tuple(latest) > version_tuple(yt_dlp.version.__version__):
                found["ytdlp"] = latest
        except Exception:  # noqa: BLE001
            pass
        if found["app"] or found["ytdlp"]:
            self.signals.updates_found.emit(found)


class Updater(threading.Thread):
    """Installs updates: git pull for the app (when it was installed from git), then pip."""

    def __init__(self, signals: Signals, app: bool):
        super().__init__(daemon=True)
        self.signals, self.app = signals, app

    def run(self) -> None:
        steps = []
        if self.app:
            steps.append(["git", "-C", str(APP_DIR), "pull", "--ff-only"])
            steps.append([sys.executable, "-m", "pip", "install", "--upgrade",
                          "-r", str(APP_DIR / "requirements.txt")])
        else:
            steps.append([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"])
        for cmd in steps:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except Exception as e:  # noqa: BLE001
                self.signals.update_done.emit(False, f"{' '.join(cmd[:4])}…\n\n{e}")
                return
            if r.returncode != 0:
                out = (r.stderr or r.stdout).strip()[-1500:]
                self.signals.update_done.emit(False, f"{' '.join(cmd[:4])}… failed:\n\n{out}")
                return
        self.signals.update_done.emit(True, "")


# --------------------------------------------------------------------------- UI

COLS = ["Title", "Type", "Progress", "Size", "Speed", "ETA", "Status"]
C_TITLE, C_TYPE, C_PROG, C_SIZE, C_SPEED, C_ETA, C_STATUS = range(len(COLS))


class PlaylistDialog(QDialog):
    """Shows a playlist's items so the user can pick which ones to add."""

    def __init__(self, parent, name: str, entries: list, audio: bool):
        super().__init__(parent)
        self.setWindowTitle("Playlist")
        self.resize(860 if audio else 640, 480)
        self.audio = audio
        lay = QVBoxLayout(self)
        heading = QLabel(name, textFormat=Qt.PlainText)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        lay.addWidget(heading)
        unsure = sum(not e[4] for e in entries) if audio else 0
        if unsure:
            note = QLabel(f"⚠ {unsure} file name{'s' if unsure != 1 else ''} couldn't be worked "
                          "out as “Artist - Song”. Double-click a name to edit it.")
            note.setWordWrap(True)
            lay.addWidget(note)

        self.tree = QTreeWidget(rootIsDecorated=False, uniformRowHeights=True)
        self.tree.setHeaderLabels(["#", "Title", "Length"] + (["Save as"] if audio else []))
        warn = QIcon.fromTheme("dialog-warning",
                               self.style().standardIcon(QStyle.SP_MessageBoxWarning))
        for i, (url, title, secs, fname, sure) in enumerate(entries, 1):
            cols = [str(i), title or url, human_eta(secs) if secs else ""]
            item = QTreeWidgetItem(cols + ([fname] if audio else []))
            item.setCheckState(0, Qt.Checked)
            item.setToolTip(1, url)
            item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            if audio:
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                if not sure:
                    item.setIcon(3, warn)
                    item.setToolTip(3, "Couldn't find the artist and song name. "
                                       "Double-click to edit.")
            self.tree.addTopLevelItem(item)
        self.tree.setEditTriggers(QAbstractItemView.EditKeyPressed)
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        if audio:
            hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.itemChanged.connect(lambda *_: self._update_count())
        self.tree.itemDoubleClicked.connect(self._toggle)
        lay.addWidget(self.tree)

        row = QHBoxLayout()
        for text, state in (("Select all", Qt.Checked), ("Select none", Qt.Unchecked)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, st=state: self._set_all(st))
            row.addWidget(b)
        row.addStretch(1)
        self.count_label = QLabel()
        row.addWidget(self.count_label)
        lay.addLayout(row)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._entries = entries
        self._update_count()

    def _items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _toggle(self, item, col):
        if self.audio and col == 3:
            self.tree.editItem(item, 3)
            return
        item.setCheckState(0, Qt.Unchecked if item.checkState(0) == Qt.Checked else Qt.Checked)

    def _set_all(self, state):
        self.tree.blockSignals(True)
        for item in self._items():
            item.setCheckState(0, state)
        self.tree.blockSignals(False)
        self._update_count()

    def _update_count(self):
        n = sum(it.checkState(0) == Qt.Checked for it in self._items())
        self.count_label.setText(f"{n} of {len(self._entries)} selected")
        ok = self.buttons.button(QDialogButtonBox.Ok)
        ok.setText(f"Add {n} item{'s' if n != 1 else ''}")
        ok.setEnabled(n > 0)

    def selected(self) -> list:
        out = []
        for e, it in zip(self._entries, self._items()):
            if it.checkState(0) == Qt.Checked:
                if self.audio:
                    e = (*e[:3], it.text(3).strip(), True)
                out.append(e)
        return out


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "wavefall-media-downloader.svg")))
        self.resize(980, 600)

        self.settings = QSettings("wavefall-media-downloader", "wavefall-media-downloader")
        self.ffmpeg = find_ffmpeg()
        self.js_runtimes = find_js_runtimes()
        self.jobs: list[Job] = []
        self.workers: dict[str, DownloadWorker] = {}
        self.pending_resolves = 0

        self.signals = Signals()
        self.signals.progress.connect(self.on_progress)
        self.signals.finished.connect(self.on_finished)
        self.signals.resolved.connect(self.on_resolved)
        self.signals.resolve_failed.connect(self.on_resolve_failed)
        self.signals.updates_found.connect(self.on_updates_found)
        self.signals.update_done.connect(self.on_update_done)

        self._build_ui()
        self._load_history()
        self.updates: dict = {}
        UpdateChecker(self.signals).start()

        self.save_timer = QTimer(self, interval=3000, timeout=self._save_history)
        self.save_timer.start()

    # ---- layout
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top = QGridLayout()
        root.addLayout(top)

        top.addWidget(QLabel("Link:"), 0, 0)
        self.url_edit = QLineEdit(placeholderText="Paste a video, audio or playlist link…")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.returnPressed.connect(self.add_clicked)
        top.addWidget(self.url_edit, 0, 1, 1, 6)
        self.paste_btn = QPushButton("Paste")
        self.paste_btn.clicked.connect(self.paste_clicked)
        top.addWidget(self.paste_btn, 0, 7)
        self.add_btn = QPushButton("Add")
        self.add_btn.setDefault(True)
        self.add_btn.setIcon(QIcon.fromTheme("list-add"))
        self.add_btn.clicked.connect(self.add_clicked)
        top.addWidget(self.add_btn, 0, 8)
        about_btn = QToolButton(text="?", autoRaise=True)
        about_btn.setToolTip(f"About {APP_NAME}")
        about_btn.clicked.connect(self.show_about)
        top.addWidget(about_btn, 0, 9)

        mode_row = QHBoxLayout()
        self.video_btn = QPushButton(QIcon.fromTheme("video-x-generic"), "Video")
        self.audio_btn = QPushButton(QIcon.fromTheme("audio-x-generic"), "Audio")
        self.mode_group = QButtonGroup(self)
        switch = QHBoxLayout()
        switch.setSpacing(0)
        for b in (self.video_btn, self.audio_btn):
            b.setCheckable(True)
            self.mode_group.addButton(b)
            switch.addWidget(b)
        self.mode_group.setExclusive(True)
        # Same style in both states so the two halves stay the same size
        base = ("QPushButton { padding: 0 12px; font-weight: bold; color: palette(button-text); "
                "background: palette(button); border: 1px solid palette(mid); }"
                "QPushButton:hover:!checked { background: palette(midlight); }"
                "QPushButton:checked { background: palette(highlight); "
                "color: palette(highlighted-text); border-color: palette(highlight); }")
        self.video_btn.setStyleSheet(base.replace("border: 1px solid palette(mid); }",
            "border: 1px solid palette(mid); border-top-left-radius: 4px; "
            "border-bottom-left-radius: 4px; border-right: none; }"))
        self.audio_btn.setStyleSheet(base.replace("border: 1px solid palette(mid); }",
            "border: 1px solid palette(mid); border-top-right-radius: 4px; "
            "border-bottom-right-radius: 4px; border-left: none; }"))
        height = self.paste_btn.sizeHint().height()
        width = max(110, *(b.sizeHint().width() for b in (self.video_btn, self.audio_btn)))
        for b in (self.video_btn, self.audio_btn):
            b.setFixedSize(width, height)
        self.video_btn.setToolTip("Download video (saved to the Video folder)")
        self.audio_btn.setToolTip("Download audio only, as Artist - Song "
                                    "(saved to the Audio folder)")
        mode_row.addLayout(switch)
        mode_row.addSpacing(16)

        self.quality_label = QLabel("Quality:")
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(VIDEO_QUALITIES)
        self.format_label = QLabel("Format:")
        self.format_combo = QComboBox()
        self.format_combo.addItems(AUDIO_FORMATS)
        self.bitrate_label = QLabel("Bitrate:")
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(AUDIO_BITRATES)
        self.subs_check = QCheckBox("Subtitles")
        self.subs_check.setToolTip("Download subtitles in your language (or English) and add\n"
                                   "them to the video. Uses auto-generated ones if needed.")
        for w in (self.quality_label, self.quality_combo, self.subs_check, self.format_label,
                  self.format_combo, self.bitrate_label, self.bitrate_combo):
            mode_row.addWidget(w)
        mode_row.addSpacing(16)
        self.playlist_check = QCheckBox("Whole playlist")
        self.playlist_check.setToolTip("If the link is (or is part of) a playlist, add every item.\n"
                                       "Items go into a sub-folder named after the playlist.")
        mode_row.addWidget(self.playlist_check)
        mode_row.addStretch(1)
        mode_row.addWidget(QLabel("At once:"))
        self.parallel_spin = QSpinBox(minimum=1, maximum=6)
        self.parallel_spin.setToolTip("How many downloads run at the same time")
        self.parallel_spin.valueChanged.connect(lambda _: self.schedule())
        mode_row.addWidget(self.parallel_spin)
        top.addLayout(mode_row, 1, 0, 1, 9)

        self.folder_label = QLabel()
        top.addWidget(self.folder_label, 2, 0)
        self.folder_edit = QLineEdit(readOnly=True)
        top.addWidget(self.folder_edit, 2, 1, 1, 6)
        change = QPushButton("Change…")
        change.clicked.connect(self.choose_folder)
        top.addWidget(change, 2, 7)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda: open_path(self.folder_edit.text()))
        top.addWidget(open_btn, 2, 8)
        top.setColumnStretch(1, 1)

        self.update_bar = QFrame(frameShape=QFrame.StyledPanel)
        bar = QHBoxLayout(self.update_bar)
        bar.setContentsMargins(8, 4, 4, 4)
        self.update_label = QLabel()
        self.update_label.setWordWrap(True)
        bar.addWidget(self.update_label, 1)
        self.update_notes_btn = QPushButton("What's new")
        self.update_notes_btn.clicked.connect(
            lambda: open_path((self.updates.get("app") or {}).get("url", "")))
        self.update_now_btn = QPushButton("Update now")
        self.update_now_btn.clicked.connect(self.install_updates)
        self.update_later_btn = QPushButton("Later")
        self.update_later_btn.clicked.connect(self.update_bar.hide)
        for b in (self.update_notes_btn, self.update_now_btn, self.update_later_btn):
            bar.addWidget(b)
        self.update_bar.hide()
        root.addWidget(self.update_bar)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color: #b36b00;")
        root.addWidget(self.notice)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_TITLE, QHeaderView.Stretch)
        for c, w in ((C_TYPE, 90), (C_PROG, 170), (C_SIZE, 80), (C_SPEED, 90), (C_ETA, 60),
                     (C_STATUS, 110)):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
            self.table.setColumnWidth(c, w)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.cellDoubleClicked.connect(self.row_double_clicked)
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.pause_btn = self._button("Pause", "media-playback-pause", self.pause_selected)
        self.resume_btn = self._button("Resume / Retry", "view-refresh", self.resume_selected)
        self.cancel_btn = self._button("Cancel", "process-stop", self.cancel_selected)
        self.remove_btn = self._button("Remove from list", "list-remove", self.remove_selected)
        self.open_file_btn = self._button("Play file", "media-playback-start", self.open_selected_file)
        for b in (self.pause_btn, self.resume_btn, self.cancel_btn, self.remove_btn,
                  self.open_file_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self._button("Clear finished", "edit-clear", self.clear_finished))
        root.addLayout(btns)

        self.status = self.statusBar()

        delete_act = QAction(self, shortcut=QKeySequence.Delete, triggered=self.remove_selected)
        self.table.addAction(delete_act)

        # restore settings
        def default(sub):
            p = Path.home() / sub
            return str(p if p.is_dir() else Path.home() / "Downloads")
        old = self.settings.value("folder")   # v0.1 had one folder; it becomes the audio one
        self.folders = {
            "video": self.settings.value("folder_video", default("Videos")),
            "audio": self.settings.value("folder_audio", old or default("Music")),
        }
        (self.audio_btn if self.settings.value("mode", "video") == "audio"
         else self.video_btn).setChecked(True)
        self.quality_combo.setCurrentText(self.settings.value("quality", "1080p"))
        self.format_combo.setCurrentText(self.settings.value("audio_format", "mp3"))
        self.bitrate_combo.setCurrentText(self.settings.value("audio_bitrate", "Best"))
        self.playlist_check.setChecked(self.settings.value("playlist", False, type=bool))
        self.subs_check.setChecked(self.settings.value("subtitles", False, type=bool))
        self.parallel_spin.setValue(self.settings.value("parallel", 2, type=int))
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        self.mode_group.buttonToggled.connect(lambda *_: self.update_mode_widgets())
        self.update_mode_widgets()
        self.update_buttons()

        if not self.ffmpeg:
            self.notice.setText(
                "ffmpeg was not found. Audio files are saved in their original format (no MP3 "
                "conversion) and video is limited to single-file formats. "
                "Install it with:  sudo apt install ffmpeg  then restart the app.")
        else:
            self.notice.hide()

    def show_about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b><br>Version {APP_VERSION}<br><br>"
            f"yt-dlp {yt_dlp.version.__version__}<br>"
            f"ffmpeg: {'found' if self.ffmpeg else 'not found'}")

    # ---- updates
    def on_updates_found(self, found: dict):
        self.updates = found
        parts = []
        if found["app"]:
            parts.append(f"{APP_NAME} {found['app']['version']} is available "
                         f"(you have {APP_VERSION}).")
        if found["ytdlp"]:
            parts.append(f"yt-dlp {found['ytdlp']} is available (you have "
                         f"{yt_dlp.version.__version__}). Sites change often, so keeping it "
                         "current keeps downloads working.")
        self.update_label.setText(" ".join(parts))
        self.update_notes_btn.setVisible(bool(found["app"] and found["app"]["url"]))
        self.update_now_btn.setText("Update now")
        self.update_now_btn.setEnabled(True)
        self.update_later_btn.setEnabled(True)
        self.update_bar.show()

    def install_updates(self):
        if self.update_now_btn.text() == "Restart now":
            self.restart()
            return
        app = bool(self.updates.get("app"))
        if app and not (APP_DIR / ".git").is_dir():
            # Not installed from git: send the user to the release page instead
            QMessageBox.information(
                self, "Update", "This copy wasn't installed with git, so it can't update "
                "itself. The release page will open so you can download the new version.")
            open_path(self.updates["app"]["url"])
            if not self.updates.get("ytdlp"):
                return
            app = False
        if app and not shutil.which("git"):
            QMessageBox.warning(self, "Update", "git isn't installed, so the app can't update "
                                "itself. Install it with:  sudo apt install git")
            return
        self.update_label.setText("Updating… downloads can carry on meanwhile.")
        for b in (self.update_now_btn, self.update_later_btn, self.update_notes_btn):
            b.setEnabled(False)
        Updater(self.signals, app).start()

    def on_update_done(self, ok: bool, msg: str):
        self.update_later_btn.setEnabled(True)
        self.update_notes_btn.setEnabled(True)
        if ok:
            self.update_label.setText("Update installed. Restart the app to start using it.")
            self.update_now_btn.setText("Restart now")
            self.update_now_btn.setEnabled(True)
        else:
            self.update_label.setText("The update didn't finish.")
            self.update_now_btn.setText("Try again")
            self.update_now_btn.setEnabled(True)
            QMessageBox.warning(self, "Update failed", msg)

    def restart(self):
        # close() asks first if downloads are running (they're paused and resumable)
        if self.close():
            QProcess.startDetached(sys.executable, [str(APP_DIR / "wavefall.py")] + sys.argv[1:])

    def _button(self, text, icon, slot):
        b = QPushButton(QIcon.fromTheme(icon), text)
        b.clicked.connect(slot)
        return b

    def update_mode_widgets(self):
        audio = self.audio_btn.isChecked()
        for w in (self.quality_label, self.quality_combo, self.subs_check):
            w.setVisible(not audio)
        self.folder_label.setText("Save audio to:" if audio else "Save video to:")
        self.folder_edit.setText(self.folders["audio" if audio else "video"])
        for w in (self.format_label, self.format_combo, self.bitrate_label, self.bitrate_combo):
            w.setVisible(audio)
        self.format_combo.setEnabled(bool(self.ffmpeg))
        self.bitrate_combo.setEnabled(bool(self.ffmpeg))

    # ---- adding
    def paste_clicked(self):
        self.url_edit.setText(QApplication.clipboard().text().strip())
        self.url_edit.setFocus()

    def add_clicked(self):
        text = self.url_edit.text().strip()
        urls = [u for u in re.split(r"\s+", text) if u]
        if not urls:
            self.url_edit.setFocus()
            return
        for url in urls:
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
                url = "https://" + url
            want_playlist = self.playlist_check.isChecked()
            if not want_playlist and (kind := playlist_kind(url)):
                answer = self._ask_playlist(url, kind)
                if answer is None:
                    continue
                want_playlist = answer
                if want_playlist:
                    url = playlist_url(url)
            self.pending_resolves += 1
            ResolveWorker(url, want_playlist, self._current_choice(),
                          self.signals, self.js_runtimes).start()
        self.url_edit.clear()
        self._update_status()

    def _ask_playlist(self, url: str, kind: str) -> bool | None:
        """Ask what to do with a playlist link while "Whole playlist" is off.
        Returns True for the whole playlist, False for just the one item, None to skip it."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Playlist link")
        whole = box.addButton("Whole playlist", QMessageBox.AcceptRole)
        single = None
        if kind == "item":
            box.setText("This link is part of a playlist.")
            box.setInformativeText("Add the whole playlist, or just this one item?\n\n" + url)
            single = box.addButton("Just this item", QMessageBox.AcceptRole)
            box.setDefaultButton(single)
        else:
            box.setText("This link is a playlist.")
            box.setInformativeText("“Whole playlist” is off, but this link has no single "
                                   "item to add. Add every item in the playlist?\n\n" + url)
            box.setDefaultButton(whole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is whole:
            return True
        if single is not None and clicked is single:
            return False
        return None

    def _current_choice(self) -> dict:
        return dict(
            mode="audio" if self.audio_btn.isChecked() else "video",
            quality=self.quality_combo.currentText(),
            audio_format=self.format_combo.currentText() if self.ffmpeg else "original",
            audio_bitrate=self.bitrate_combo.currentText(),
            folder=self.folder_edit.text(),
            subtitles=self.video_btn.isChecked() and self.subs_check.isChecked(),
        )

    def on_resolved(self, entries: list, subdir: str, choice: dict):
        self.pending_resolves -= 1
        self._update_status()
        if subdir:
            if not entries:
                self.status.showMessage(f"Playlist “{subdir}” has no items to add", 6000)
                return
            dlg = PlaylistDialog(self, subdir, entries, choice["mode"] == "audio")
            if dlg.exec() != QDialog.Accepted:
                return
            entries = dlg.selected()
        elif choice["mode"] == "audio" and entries and not entries[0][4]:
            url, title, secs, fname, _ = entries[0]
            fname, ok = QInputDialog.getText(
                self, "Name this file",
                f"Couldn't find the artist and song name for:\n{title or url}\n\n"
                "Save as (Artist - Song):", text=fname)
            if not ok:
                self.status.showMessage("Not added", 4000)
                return
            entries = [(url, title, secs, fname.strip(), True)]
        skipped = 0
        for url, title, _secs, fname, _sure in entries:
            audio = choice["mode"] == "audio"
            job = Job(url=url, title=(fname or title) if audio else title,
                      filename=fname if audio else "", subdir=subdir, **choice)
            if any(j.url == job.url and j.type_label == job.type_label
                   and j.status in ACTIVE | {QUEUED, PAUSED} for j in self.jobs):
                skipped += 1   # already in the list; a second copy would clash
                continue
            self.add_job(job)
        self._update_status()
        if subdir and entries:
            self.status.showMessage(f"Added {len(entries) - skipped} items from playlist "
                                    f"“{subdir}”", 6000)
        if skipped:
            self.status.showMessage(f"Skipped {skipped} link(s) already in the list", 6000)
        self.schedule()

    def on_resolve_failed(self, url: str, err: str):
        self.pending_resolves -= 1
        self._update_status()
        QMessageBox.warning(self, "Couldn't add link", f"{url}\n\n{err}")

    def add_job(self, job: Job):
        self.jobs.append(job)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for c in range(len(COLS)):
            if c != C_PROG:
                self.table.setItem(row, c, QTableWidgetItem())
        bar = QProgressBar(minimum=0, maximum=1000, textVisible=True)
        self.table.setCellWidget(row, C_PROG, bar)
        self.refresh_row(job)

    # ---- scheduling
    def schedule(self):
        limit = self.parallel_spin.value()
        running = sum(1 for j in self.jobs if j.id in self.workers)
        for job in self.jobs:
            if running >= limit:
                break
            if job.status == QUEUED and job.id not in self.workers:
                Path(job.folder, job.subdir).mkdir(parents=True, exist_ok=True)
                job.status, job.error = RUNNING, ""
                w = DownloadWorker(job, self.signals, self.ffmpeg, self.js_runtimes)
                self.workers[job.id] = w
                w.start()
                running += 1
                self.refresh_row(job)
        self._update_status()

    def on_progress(self, job_id: str, upd: dict):
        job = self.job_by_id(job_id)
        if not job:
            return
        for k, v in upd.items():
            setattr(job, k, v)
        self.refresh_row(job)

    def on_finished(self, job_id: str, status: str, msg: str):
        self.workers.pop(job_id, None)
        job = self.job_by_id(job_id)
        if job:
            job.status, job.speed, job.eta = status, None, None
            if status == DONE:
                job.progress, job.filepath = 100, msg
            elif status == FAILED:
                job.error = msg
            elif status == CANCELLED:
                job.progress = 0
            self.refresh_row(job)
        self.schedule()
        self.update_buttons()
        self._save_history()

    # ---- row display
    def job_by_id(self, job_id) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def row_of(self, job: Job) -> int:
        return self.jobs.index(job)

    def refresh_row(self, job: Job):
        row = self.row_of(job)
        title = job.title or job.url
        it = self.table.item(row, C_TITLE)
        it.setText(title)
        tip = job.url if not job.error else f"{job.url}\n\nError: {job.error}"
        if job.filepath:
            tip += f"\n\nSaved as: {job.filepath}"
        it.setToolTip(tip)
        self.table.item(row, C_TYPE).setText(job.type_label)
        self.table.item(row, C_SIZE).setText(human_bytes(job.size))
        self.table.item(row, C_SPEED).setText(human_bytes(job.speed) + "/s" if job.speed else "")
        self.table.item(row, C_ETA).setText(human_eta(job.eta) if job.status == RUNNING else "")
        st = self.table.item(row, C_STATUS)
        st.setText(job.status)
        st.setToolTip(job.error)
        bar: QProgressBar = self.table.cellWidget(row, C_PROG)
        bar.setValue(int(job.progress * 10))
        if job.status == PROCESSING:
            bar.setFormat("Converting…")
        elif job.status == FAILED:
            bar.setFormat("Failed - hover for details")
        else:
            bar.setFormat(f"{job.progress:.0f}%")
        if self.table.currentRow() == row:
            self.update_buttons()

    def _update_status(self):
        active = sum(1 for j in self.jobs if j.status in ACTIVE)
        queued = sum(1 for j in self.jobs if j.status == QUEUED)
        done = sum(1 for j in self.jobs if j.status == DONE)
        parts = [f"{active} downloading", f"{queued} waiting", f"{done} done"]
        if self.pending_resolves:
            parts.insert(0, f"looking up {self.pending_resolves} link(s)…")
        self.status.showMessage(" · ".join(parts))

    # ---- actions on selection
    def selected_jobs(self) -> list[Job]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.jobs[r] for r in rows if r < len(self.jobs)]

    def update_buttons(self):
        sel = self.selected_jobs()
        st = {j.status for j in sel}
        self.pause_btn.setEnabled(bool(st & (ACTIVE | {QUEUED})))
        self.resume_btn.setEnabled(bool(st & {PAUSED, FAILED, CANCELLED}))
        self.cancel_btn.setEnabled(bool(st & (ACTIVE | {QUEUED, PAUSED})))
        self.remove_btn.setEnabled(bool(sel))
        self.open_file_btn.setEnabled(any(j.status == DONE and j.filepath for j in sel))
        self._update_status()

    def pause_selected(self):
        for j in self.selected_jobs():
            if j.id in self.workers:
                self.workers[j.id].request_stop("pause")
                j.status = "Pausing…"
            elif j.status == QUEUED:
                j.status = PAUSED
            self.refresh_row(j)
        self.update_buttons()

    def resume_selected(self):
        for j in self.selected_jobs():
            if j.status in (PAUSED, FAILED, CANCELLED):
                j.status, j.error = QUEUED, ""
                self.refresh_row(j)
        self.schedule()
        self.update_buttons()

    def cancel_selected(self):
        for j in self.selected_jobs():
            if j.id in self.workers:
                self.workers[j.id].request_stop("cancel")
                j.status = "Cancelling…"
            elif j.status in (QUEUED, PAUSED):
                for f in j.tmpfiles:
                    for p in (f, f + ".ytdl"):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                j.tmpfiles.clear()
                j.status, j.progress = CANCELLED, 0
            self.refresh_row(j)
        self.update_buttons()

    def remove_selected(self):
        sel = self.selected_jobs()
        if any(j.id in self.workers for j in sel):
            QMessageBox.information(self, APP_NAME,
                                    "Pause or cancel running downloads before removing them.")
            return
        for j in sel:
            self.table.removeRow(self.row_of(j))
            self.jobs.remove(j)
        self._save_history()
        self.update_buttons()

    def clear_finished(self):
        for j in [j for j in self.jobs if j.status in (DONE, CANCELLED)]:
            self.table.removeRow(self.row_of(j))
            self.jobs.remove(j)
        self._save_history()
        self.update_buttons()

    def open_selected_file(self):
        for j in self.selected_jobs():
            if j.status == DONE and j.filepath:
                open_path(j.filepath)

    def row_double_clicked(self, row, _col):
        job = self.jobs[row]
        if job.status == DONE and job.filepath and os.path.exists(job.filepath):
            open_path(job.filepath)
        elif job.status == FAILED:
            QMessageBox.warning(self, "Download failed", f"{job.title or job.url}\n\n{job.error}")

    def context_menu(self, pos):
        if not self.selected_jobs():
            return
        m = QMenu(self)
        for b in (self.open_file_btn, self.pause_btn, self.resume_btn, self.cancel_btn,
                  self.remove_btn):
            a = m.addAction(b.icon(), b.text(), b.click)
            a.setEnabled(b.isEnabled())
        m.addSeparator()
        jobs = self.selected_jobs()
        m.addAction(QIcon.fromTheme("folder-open"), "Open containing folder",
                    lambda: open_path(str(Path(jobs[0].filepath).parent) if jobs[0].filepath
                                      else str(Path(jobs[0].folder) / jobs[0].subdir)))
        m.addAction(QIcon.fromTheme("edit-copy"), "Copy link",
                    lambda: QApplication.clipboard().setText("\n".join(j.url for j in jobs)))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def choose_folder(self):
        audio = self.audio_btn.isChecked()
        d = QFileDialog.getExistingDirectory(
            self, "Audio folder" if audio else "Video folder", self.folder_edit.text())
        if d:
            self.folders["audio" if audio else "video"] = d
            self.folder_edit.setText(d)

    # ---- persistence
    def _load_history(self):
        try:
            data = json.loads(HISTORY_FILE.read_text())
        except (OSError, ValueError):
            return
        for d in data.get("jobs", []):
            try:
                self.add_job(Job.from_dict(d))
            except TypeError:
                continue

    def _save_history(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"jobs": [j.to_dict() for j in self.jobs]}, indent=1))
            tmp.replace(HISTORY_FILE)
        except OSError:
            pass

    def closeEvent(self, ev):
        if self.workers:
            r = QMessageBox.question(
                self, APP_NAME,
                f"{len(self.workers)} download(s) still running. Quit anyway?\n"
                "They will be paused and can be resumed next time.")
            if r != QMessageBox.Yes:
                ev.ignore()
                return
            for w in self.workers.values():
                w.request_stop("pause")
            for w in list(self.workers.values()):
                w.join(timeout=3)
        s = self.settings
        c = self._current_choice()
        s.setValue("folder_video", self.folders["video"])
        s.setValue("folder_audio", self.folders["audio"])
        s.remove("folder")
        s.setValue("subtitles", self.subs_check.isChecked())
        s.setValue("mode", c["mode"])
        s.setValue("quality", c["quality"])
        s.setValue("audio_format", self.format_combo.currentText())
        s.setValue("audio_bitrate", c["audio_bitrate"])
        s.setValue("playlist", self.playlist_check.isChecked())
        s.setValue("parallel", self.parallel_spin.value())
        s.setValue("geometry", self.saveGeometry())
        self._save_history()
        ev.accept()


def main():
    # argv[0] sets the X11 window class; keep it matching the menu entry so the panel icon matches
    app = QApplication(["wavefall-media-downloader"] + sys.argv[1:])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("wavefall-media-downloader")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
