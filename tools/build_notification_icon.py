"""Generiert das Notification-Icon (Status-Bar) als monochrome 'ゆ'-Glyphe.

Android-Anforderungen: weiss auf transparent, alle Density-Buckets, im
'ic_stat_'-Prefix-Konvention.

Output:
- mobile/.../drawable-{mdpi..xxxhdpi}/ic_stat_yuki.png

Run via Yuki-venv (PIL ist da):
    .venv\\Scripts\\python.exe tools/build_notification_icon.py
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
RES = REPO / "mobile" / "android" / "app" / "src" / "main" / "res"

FONT_PATH = r"C:\Windows\Fonts\YuGothB.ttc"  # Yu Gothic Bold
GLYPH = "ゆ"

# Status-Bar-Icon: Container in dp, Glyph nimmt ~82% (Android-Guidelines:
# "optical center" liegt drin, plus Status-Bar trimmt aussen oft etwas)
DENSITIES = [
    ("mdpi", 24),
    ("hdpi", 36),
    ("xhdpi", 48),
    ("xxhdpi", 72),
    ("xxxhdpi", 96),
]
GLYPH_RATIO = 0.86


def render_glyph(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    font_size = int(size * GLYPH_RATIO)
    font = ImageFont.truetype(FONT_PATH, font_size)
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), GLYPH, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) // 2 - bbox[0]
    y = (size - text_h) // 2 - bbox[1]
    draw.text((x, y), GLYPH, fill=(255, 255, 255, 255), font=font)
    return img


def main() -> int:
    for name, size in DENSITIES:
        out_dir = RES / f"drawable-{name}"
        out_dir.mkdir(parents=True, exist_ok=True)
        img = render_glyph(size)
        out_path = out_dir / "ic_stat_yuki.png"
        img.save(out_path, "PNG", optimize=True)
        print(f"  [{name}] {size}px -> {out_path.relative_to(REPO)}")

    print("OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
