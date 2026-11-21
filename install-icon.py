"""Install wavefall-media-downloader.svg (plus PNG sizes) into the user's hicolor icon theme."""
import sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

src = Path(__file__).resolve().parent / "wavefall-media-downloader.svg"
base = Path.home() / ".local/share/icons/hicolor"
app = QGuiApplication(sys.argv[:1])
r = QSvgRenderer(str(src))
d = base / "scalable/apps"; d.mkdir(parents=True, exist_ok=True)
(d / "wavefall-media-downloader.svg").write_bytes(src.read_bytes())
for n in (16, 22, 24, 32, 48, 64, 128, 256):
    img = QImage(n, n, QImage.Format_ARGB32); img.fill(Qt.transparent)
    p = QPainter(img); p.setRenderHint(QPainter.Antialiasing); r.render(p); p.end()
    d = base / f"{n}x{n}/apps"; d.mkdir(parents=True, exist_ok=True)
    img.save(str(d / "wavefall-media-downloader.png"))
print("icons installed in", base)
