"""Generiert App-Icon-Assets aus dem Yuki-Screenshot.

Quelle: tests/screenshots/Screenshot 2026-06-05 125628.png (Yuki Portrait,
rotes Cheongsam, Sakura-Hintergrund).

Pipeline:
1) rembg stellt Yuki frei (Alpha-Mask via u2net)
2) Bounding-Box auf die freigestellte Yuki
3) Quadratischer Container mit etwas Padding um den Crop
4) Composit auf Sakura-Pink-Background

Output:
- mobile/.../mipmap-{mdpi..xxxhdpi}/ic_launcher.png         (Legacy-Icon)
- mobile/.../mipmap-{mdpi..xxxhdpi}/ic_launcher_round.png   (Legacy-Round)
- mobile/.../mipmap-{mdpi..xxxhdpi}/ic_launcher_foreground.png (Adaptive)
- values/ic_launcher_background.xml (BG-Farbe)

Run via separate rembg-venv (Py 3.11):
    D:\\Server\\rembg-venv\\Scripts\\python.exe tools/build_app_icon.py
"""
from __future__ import annotations

import io
from pathlib import Path
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "tests" / "screenshots" / "Screenshot 2026-06-05 125628.png"
RES = REPO / "mobile" / "android" / "app" / "src" / "main" / "res"
DEBUG_DIR = REPO / "runtime" / "icon_debug"

BACKGROUND_COLOR_HEX = "#F4B6BB"  # weiches Sakura-Pink

# Adaptive-Icon Safe-Zone (Yuki nimmt nur diesen Anteil des Containers ein)
SAFE_ZONE_RATIO = 0.78
# Extra-Padding um die rembg-bbox (in % der Bbox-Kante) damit Yuki nicht am Rand klebt
BBOX_PADDING = 0.08

DENSITIES = [
    ("mdpi", 48, 108),
    ("hdpi", 72, 162),
    ("xhdpi", 96, 216),
    ("xxhdpi", 144, 324),
    ("xxxhdpi", 192, 432),
]


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def make_round_mask(size: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size, size), fill=255)
    return m


def remove_background(src: Image.Image) -> Image.Image:
    """rembg um Yuki freizustellen."""
    from rembg import remove, new_session
    session = new_session("u2net")
    buf = io.BytesIO()
    src.convert("RGB").save(buf, format="PNG")
    out = remove(buf.getvalue(), session=session)
    return Image.open(io.BytesIO(out)).convert("RGBA")


def square_around_bbox(rgba: Image.Image, padding_pct: float) -> Image.Image:
    """Quadratischer Crop um die Alpha-Bbox, mit Padding."""
    bbox = rgba.getbbox()
    if not bbox:
        return rgba
    left, top, right, bottom = bbox
    w = right - left
    h = bottom - top
    side = max(w, h)
    pad = int(side * padding_pct)
    side += 2 * pad
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    new_left = cx - side // 2
    new_top = cy - side // 2
    new_right = new_left + side
    new_bottom = new_top + side
    # Transparenter Container, dann freigestelltes Yuki einkleben
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    crop_left = max(new_left, 0)
    crop_top = max(new_top, 0)
    crop_right = min(new_right, rgba.width)
    crop_bottom = min(new_bottom, rgba.height)
    cropped = rgba.crop((crop_left, crop_top, crop_right, crop_bottom))
    paste_x = crop_left - new_left
    paste_y = crop_top - new_top
    out.paste(cropped, (paste_x, paste_y), cropped)
    return out


def main() -> int:
    if not SRC.exists():
        print(f"FEHLT: {SRC}")
        return 1

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Lade {SRC}")
    src = Image.open(SRC).convert("RGBA")
    print(f"  Original: {src.size}")

    print("rembg - Hintergrund entfernen (erster Lauf laedt u2net.onnx ~170MB)...")
    freed = remove_background(src)
    freed.save(DEBUG_DIR / "01_freed.png")
    print(f"  Freigestellt -> {DEBUG_DIR / '01_freed.png'}")

    yuki_sq = square_around_bbox(freed, BBOX_PADDING)
    yuki_sq.save(DEBUG_DIR / "02_yuki_square.png")
    print(f"  Square-Crop: {yuki_sq.size}")

    bg_rgb = hex_to_rgb(BACKGROUND_COLOR_HEX)

    # Master fuer Adaptive-Foreground (transparenter Hintergrund)
    fg_master_size = 432
    safe_size = int(fg_master_size * SAFE_ZONE_RATIO)
    fg_master = Image.new("RGBA", (fg_master_size, fg_master_size), (0, 0, 0, 0))
    fg_safe = yuki_sq.resize((safe_size, safe_size), Image.LANCZOS)
    fg_off = (fg_master_size - safe_size) // 2
    fg_master.paste(fg_safe, (fg_off, fg_off), fg_safe)
    fg_master.save(DEBUG_DIR / "03_foreground_master.png")

    # Master fuer Legacy (mit Pink-Background, voll-ausgefuellt)
    legacy_master_size = 432
    legacy_master = Image.new("RGB", (legacy_master_size, legacy_master_size), bg_rgb)
    legacy_yuki = yuki_sq.resize((legacy_master_size, legacy_master_size), Image.LANCZOS)
    legacy_master.paste(legacy_yuki, (0, 0), legacy_yuki)
    legacy_master.save(DEBUG_DIR / "04_legacy_master.png")

    for name, legacy_size, fg_size in DENSITIES:
        out_dir = RES / f"mipmap-{name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        fg = fg_master.resize((fg_size, fg_size), Image.LANCZOS)
        fg.save(out_dir / "ic_launcher_foreground.png", "PNG", optimize=True)

        lg = legacy_master.resize((legacy_size, legacy_size), Image.LANCZOS)
        lg.save(out_dir / "ic_launcher.png", "PNG", optimize=True)

        mask = make_round_mask(legacy_size)
        rd = lg.convert("RGBA")
        rd.putalpha(mask)
        rd.save(out_dir / "ic_launcher_round.png", "PNG", optimize=True)

        print(f"  [{name}]  legacy={legacy_size}px  foreground={fg_size}px")

    bg_xml = RES / "values" / "ic_launcher_background.xml"
    bg_xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<resources>\n'
        f'    <color name="ic_launcher_background">{BACKGROUND_COLOR_HEX}</color>\n'
        '</resources>\n',
        encoding="utf-8",
    )
    print(f"  Background-Color {BACKGROUND_COLOR_HEX} -> {bg_xml.relative_to(REPO)}")

    print(f"\nDebug-Zwischenstaende in {DEBUG_DIR.relative_to(REPO)}/")
    print("OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
