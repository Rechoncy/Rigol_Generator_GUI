"""Render the editable SVG into Windows icon sizes (PyQt6 and Pillow)."""
from pathlib import Path
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication
from PIL import Image


def main() -> None:
    app = QApplication([])
    assets = Path(__file__).resolve().parents[1] / "src/rigol_control/assets"
    renderer = QSvgRenderer(str(assets / "app_icon.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid application SVG")
    canvas = QImage(512, 512, QImage.Format.Format_ARGB32)
    canvas.fill(0)
    painter = QPainter(canvas)
    renderer.render(painter)
    painter.end()
    if not canvas.save(str(assets / "app_icon.png")):
        raise RuntimeError("Could not save application icon")
    with Image.open(assets / "app_icon.png") as source:
        source.save(assets / "app_icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    app.quit()


if __name__ == "__main__":
    main()
