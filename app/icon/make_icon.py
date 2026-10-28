"""Иконка Qamyal: облачко реплики в форме буквы Q со звуковой волной.

SVG → крупный PNG через headless Chrome → Pillow уменьшает до размеров Windows → qamyal.ico.
Запуск: python make_icon.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
BIG = 512
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
BAR = "#4457e6"     # цвет волны — середина фирменного градиента

DEFS = """<defs>
  <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#2f6fed"/><stop offset="1" stop-color="#8a5cf6"/>
  </linearGradient>
  <linearGradient id="shine" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#fff" stop-opacity=".16"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/>
  </linearGradient>
</defs>"""

# Крупные размеры: два облачка (разговор), в главном — волна из пяти столбиков.
LARGE = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{DEFS}
  <rect x="2" y="2" width="60" height="60" rx="15" fill="url(#g)"/>
  <rect x="2" y="2" width="60" height="60" rx="15" fill="url(#shine)"/>
  <circle cx="41.5" cy="21.5" r="11.5" fill="#fff" fill-opacity=".38"/>
  <path d="M27 50a17 17 0 1 1 13.2-6.3l6.8 8.3-11.2-3.6A16.9 16.9 0 0 1 27 50z" fill="#fff"/>
  <g stroke="{BAR}" stroke-linecap="round" stroke-width="2.9">
    <path d="M19 31v4M23 27.5v11M27 24v18M31 28v10M35 31v4"/>
  </g>
</svg>"""

# Мелкие размеры: одно облачко крупнее, три столбика потолще — чтобы читалось на 16 px.
SMALL = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{DEFS}
  <rect x="0" y="0" width="64" height="64" rx="13" fill="url(#g)"/>
  <path d="M30 53a21 21 0 1 1 16.4-7.9l8.6 10.4-14-4.5A20.9 20.9 0 0 1 30 53z" fill="#fff"/>
  <g stroke="{BAR}" stroke-linecap="round" stroke-width="5.5">
    <path d="M21 29v6M30 23v18M39 28v8"/>
  </g>
</svg>"""


def chrome() -> str:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe")):
        if Path(p).exists():
            return p
    sys.exit("Chrome не найден")


def render(svg: str, tmp: Path, name: str) -> Image.Image:
    """SVG → RGBA BIG×BIG. Окно берём с запасом и обрезаем: в headless окно больше видимой области."""
    page, shot = tmp / f"{name}.html", tmp / f"{name}.png"
    sized = svg.replace("<svg ", f'<svg width="{BIG}" height="{BIG}" style="display:block" ', 1)
    page.write_text(f"<!doctype html><html><body style='margin:0;background:transparent'>{sized}</body></html>",
                    encoding="utf-8")
    subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                    "--force-device-scale-factor=1", "--default-background-color=00000000",
                    f"--user-data-dir={tmp / 'profile'}", f"--window-size={BIG + 200},{BIG + 200}",
                    f"--screenshot={shot}", page.as_uri()], check=True, capture_output=True, timeout=60)
    return Image.open(shot).convert("RGBA").crop((0, 0, BIG, BIG))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="qamyal-icon-"))
    try:
        large, small = render(LARGE, tmp, "large"), render(SMALL, tmp, "small")
        frames = [(small if s <= 32 else large).resize((s, s), Image.LANCZOS) for s in SIZES]
        frames[-1].save(HERE / "qamyal-256.png")
        sheet = Image.new("RGBA", (sum(SIZES) + 10 * len(SIZES), 256), (255, 255, 255, 255))
        x = 0
        for f in frames:
            sheet.alpha_composite(f, (x, 256 - f.size[1]))
            x += f.size[0] + 10
        sheet.save(HERE / "preview.png")
        frames[-1].save(HERE / "qamyal.ico", format="ICO", sizes=[(s, s) for s in SIZES],
                        append_images=frames[:-1])
        (WEB / "icon.svg").write_text(LARGE, encoding="utf-8")
        print("ok:", HERE / "qamyal.ico")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
