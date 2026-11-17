#!/usr/bin/env python3
"""Regenerate the app icons from site/assets/icon.svg.

    uv run --with pillow scripts/build_icons.py        # needs rsvg-convert (librsvg)

Writes packaging/windows/rune-advisor.ico and packaging/macos/rune-advisor.icns, which are committed: the
build machines need neither librsvg nor Pillow. The AppImage uses the SVG itself.
"""

import io
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "site" / "assets" / "icon.svg"


def render(size: int) -> Image.Image:
    png = subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), str(SVG)], check=True,
                         capture_output=True).stdout
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main() -> None:
    ico = ROOT / "packaging" / "windows" / "rune-advisor.ico"
    render(256).save(ico, format="ICO", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    icns = ROOT / "packaging" / "macos" / "rune-advisor.icns"
    icns.parent.mkdir(parents=True, exist_ok=True)
    render(1024).save(icns, format="ICNS")
    print(f"wrote {ico.relative_to(ROOT)} ({ico.stat().st_size} bytes), {icns.relative_to(ROOT)} ({icns.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
