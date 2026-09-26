r"""
sprite_compose.py - Bibliothek + Standalone-Preview fuer die Yuki-Sprite-Pipeline.

Pipeline pro Sprite (4 Schritte):
1. Lade Sprite-PNG (z.B. avatar/sprites/Yuki_idle_1.png).
2. Chroma-Key: Magenta (#FF00FF +/- Toleranz fuer Anti-Aliasing-Saeume) -> Alpha=0.
3. Bounding-Box: schmeiss leeren Rand rundrum weg, sonst landet Yuki winzig auf
   dem quadratischen Background.
4. Komposition: Background auf Ziel-Aufloesung skalieren, Sprite proportional
   auf 80% der BG-Hoehe rechnen, mittig+unten kleben (mit Alpha-Blend).
5. Half-Block-Render via preview_halfblock.render_halfblock.

Aufruf (Standalone-Test):
    .\.venv\Scripts\python.exe tools\sprite_compose.py
        [--state idle] [--frame 1] [--persona tutor] [--width 44]

Beispiel:
    tools\sprite_compose.py --state speaking --frame 2 --persona kyoto --width 60

Wird vom Dashboard genutzt: AvatarPanel ruft render_sprite_halfblock() pro Frame.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

# preview_halfblock liegt im selben Verzeichnis. Beim Standalone-Aufruf ist
# tools/ in sys.path[0]; beim Import vom Dashboard koennen wir sicherheits-
# halber den eigenen Ordner reinpushen.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from preview_halfblock import render_halfblock  # noqa: E402

PROJ = Path(__file__).resolve().parent.parent
SPRITES_DIR = PROJ / "avatar" / "sprites"
BG_DIR = PROJ / "avatar" / "backgrounds"

# Chroma-Key-Detection im HSV-Farbraum. Vorgaenger war RGB-Threshold
# (R>=220, G<=60, B>=220), das hat aber abgedunkelte Magenta-Varianten wie
# (219,0,219) oder (180,1,177) verfehlt - die kommen wenn der Browser/das
# Screenshot-Tool an den Bildraendern dunklere Verlaeufe einfaerbt.
# HSV-Vorteil: ich pruefe nur den HUE (= Magenta-Farbachse, ~300 Grad),
# nicht die Helligkeit - ein dunkles (50,0,50) wird genauso erkannt wie
# pures (255,0,255), solange die Saettigung hoch ist.
#
# Pillow's HSV-Range: H=0..255 (steht fuer 0..360 Grad), S=0..255, V=0..255.
# Magenta liegt bei H~212 (= 300/360 * 255). Wir greifen 200..230 (~283..324
# Grad) - das ist ein 41-Grad-Sektor symmetrisch um Magenta. Eng genug um
# Yukis warme Hauttoene (HUE ~5..15) und Lippen (HUE ~245) nicht zu treffen,
# aber breit genug fuer Hellpink-Verlaeufe.
_HUE_MAGENTA_LOW = 200
_HUE_MAGENTA_HIGH = 230
_SAT_MIN = 80    # >= ~31% Saettigung; darunter ist's grau-magenta, irrelevant
_VAL_MIN = 60    # >= ~24% Helligkeit; darunter ist's quasi-schwarz, sieht eh aus


def chroma_key_to_alpha(img: Image.Image) -> Image.Image:
    """Magenta-Pixel -> Alpha=0. HSV-basierte Detection, robust gegen
    Pillow-LANCZOS-Mischwerte und Browser-Gradient-Saeume am Bildrand.

    Vektorisiert: PIL converts to HSV bytewise -> numpy mask via 3 Schwellen
    -> zurueck zu RGBA. ~30ms fuer 1.3M Pixel."""
    img = img.convert("RGBA")
    arr = np.asarray(img).copy()
    # Pillow's HSV-Konversion ueber convert(); ignoriert Alpha (egal).
    hsv = np.asarray(img.convert("HSV"))
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = ((h >= _HUE_MAGENTA_LOW) & (h <= _HUE_MAGENTA_HIGH)
            & (s >= _SAT_MIN) & (v >= _VAL_MIN))
    arr[mask] = (0, 0, 0, 0)
    return Image.fromarray(arr)


def crop_to_content(img: Image.Image, padding: int = 4) -> Image.Image:
    """Schneidet leere (alpha=0) Raender weg. Padding behaelt etwas Luft fuer
    Anti-Aliasing am Crop-Rand."""
    bbox = img.getbbox()
    if not bbox:
        return img
    x0, y0, x1, y1 = bbox
    x0 = max(0, x0 - padding)
    y0 = max(0, y0 - padding)
    x1 = min(img.width, x1 + padding)
    y1 = min(img.height, y1 + padding)
    return img.crop((x0, y0, x1, y1))


def load_sprite(state: str, frame_idx: int) -> Optional[Image.Image]:
    """Lade Sprite + Chroma-Key + Crop. None wenn Datei fehlt."""
    # User-Schema 'Yuki_<state>_<n>.png' mit Capital-Y. Case-insensitive
    # gesucht damit auch yuki_idle_1.png funktionieren wuerde.
    candidates = [
        SPRITES_DIR / f"Yuki_{state}_{frame_idx}.png",
        SPRITES_DIR / f"yuki_{state}_{frame_idx}.png",
    ]
    for path in candidates:
        if path.is_file():
            img = Image.open(path)
            img = chroma_key_to_alpha(img)
            img = crop_to_content(img)
            return img
    return None


def compose_on_background(sprite: Image.Image, bg_path: Optional[Path],
                          out_size: int = 256,
                          sprite_height_ratio: float = 1.0,
                          sprite_bottom_crop_px: int = 2) -> Image.Image:
    """Sprite + Background -> quadratisches Composite-Image.

    bg_path None oder fehlt -> dunkler Fallback statt Background.
    sprite_height_ratio: wieviel der Canvas-Hoehe das Sprite VOR dem Bottom-Crop
        einnimmt. 1.0 = volle Hoehe (Yuki dominiert), 0.9 = etwas Luft oben.
    sprite_bottom_crop_px: wieviele Pixel von Yukis Unterkante out-of-canvas
        rutschen sollen. 2 = eine Half-Block-Zeile (2 Source-Px pro Cell) wird
        abgeschnitten, Yuki sitzt buendig am unteren Rand statt drueber zu
        schweben. 0 = bottom-aligned ohne Crop. Negativ = Bodenluft.

    Bei breiten Sprites (Yuki + Browser-Aspect 1306x1024) wird die Sprite-
    Hoehe konstant gehalten und nur horizontal mittig gecroppt - links/rechts
    wird etwas vom Sprite abgeschnitten, dafuer bleibt Yuki gross.
    """
    canvas = Image.new("RGBA", (out_size, out_size), (15, 12, 22, 255))
    if bg_path and bg_path.is_file():
        bg = Image.open(bg_path).convert("RGBA")
        # cover-fit (kuerzere Seite passt, Ueberschuss wird gecroppt)
        bg_ratio = bg.width / bg.height
        if bg_ratio > 1:
            new_h = out_size
            new_w = int(out_size * bg_ratio)
        else:
            new_w = out_size
            new_h = int(out_size / bg_ratio)
        bg = bg.resize((new_w, new_h), Image.LANCZOS)
        offset = ((out_size - new_w) // 2, (out_size - new_h) // 2)
        canvas.paste(bg, offset)

    # Sprite proportional auf die Ziel-Hoehe skalieren - KEIN Shrink mehr wenn
    # zu breit, stattdessen unten beim horizontalen Center-Crop wegschneiden.
    target_h = int(out_size * sprite_height_ratio)
    sp_ratio = sprite.width / sprite.height
    target_w = int(target_h * sp_ratio)
    sprite_resized = sprite.resize((target_w, target_h), Image.LANCZOS)

    # Horizontal: wenn breiter als Canvas -> mittig croppen (links+rechts gleich
    # viel weg). Sonst zentriert ueber Canvas-Breite ablegen.
    if target_w > out_size:
        crop_x = (target_w - out_size) // 2
        sprite_resized = sprite_resized.crop((crop_x, 0, crop_x + out_size, target_h))
        target_w = out_size
        x = 0
    else:
        x = (out_size - target_w) // 2

    # Vertikal: am Boden ausrichten + bottom_crop_px nach unten schieben so dass
    # die letzten N Pixel von Yuki ausserhalb der Canvas landen (alpha_composite
    # zeichnet die gar nicht erst). Yuki sitzt buendig statt darueber zu schweben.
    y = out_size - target_h + sprite_bottom_crop_px
    canvas.alpha_composite(sprite_resized, (x, y))
    return canvas


def render_sprite_halfblock(state: str, frame_idx: int, persona: str,
                            width: int = 44) -> Optional[str]:
    """Komplettes Pipeline-Output als ANSI-Halfblock-String. None wenn das
    Sprite fehlt."""
    sprite = load_sprite(state, frame_idx)
    if sprite is None:
        return None
    bg_path = BG_DIR / f"{persona}.png" if persona and persona not in ("?", "") else None
    if bg_path and not bg_path.is_file():
        bg_path = None
    # Composition-Aufloesung: 4x die Halfblock-Pixel (sodass LANCZOS-Downsample
    # genug Originalinfo hat). Zu hoch = teuer; 4x ist ein guter Mittelweg.
    composite = compose_on_background(sprite, bg_path, out_size=width * 4)
    return render_halfblock(composite, width=width)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sprite-Compose-Preview")
    ap.add_argument("--state", default="idle",
                    choices=["sleeping", "idle", "thinking", "speaking", "working", "busy"])
    ap.add_argument("--frame", type=int, default=1, help="1..4")
    ap.add_argument("--persona", default="tutor",
                    help="Persona fuer Background (tutor/kyoto/smalltalk/...)")
    ap.add_argument("--width", type=int, default=44)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    out = render_sprite_halfblock(args.state, args.frame, args.persona, args.width)
    if out is None:
        print(f"Sprite nicht gefunden: Yuki_{args.state}_{args.frame}.png in {SPRITES_DIR}",
              file=sys.stderr)
        return 2
    print(f"# {args.state} frame={args.frame} persona={args.persona} width={args.width}")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
