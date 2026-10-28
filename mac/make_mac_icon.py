"""Картинка 1024×1024 для иконки macOS (из неё install.sh делает qamyal.icns через sips + iconutil).
По сетке macOS сам значок занимает 824 px в центре — иначе он выглядит крупнее соседей в Dock.
Запуск (в Windows, где есть Chrome): python mac/make_mac_icon.py"""
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app" / "icon"))
import make_icon  # noqa: E402

make_icon.BIG = 824
tmp = Path(tempfile.mkdtemp(prefix="qamyal-mac-icon-"))
try:
    art = make_icon.render(make_icon.LARGE, tmp, "mac")
    canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    canvas.alpha_composite(art, (100, 100))
    canvas.save(HERE / "qamyal-1024.png")
    print("ok:", HERE / "qamyal-1024.png")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
