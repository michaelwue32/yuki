r"""
preview_halfblock.py - Standalone-Vorschau fuer Half-Block-Bild-Rendering im Terminal.

Laedt ein PNG/JPG/WebP und gibt es als Unicode-Halfblock-"Pixel-Art" aus:
jede Konsolen-Zelle wird zu zwei vertikalen Pixeln (oben = Vordergrundfarbe,
unten = Hintergrundfarbe, getrennt via ANSI 24-bit Truecolor).

Damit kannst du beurteilen wie ein Bild im Dashboard aussehen WUERDE bevor wir
den Avatar-Renderer komplett umbauen.

Aufruf:
    .\.venv\Scripts\python.exe tools\preview_halfblock.py [PFAD] [--width N]

Default-Pfad ist avatar/backgrounds/tutor.jpg, Default-Breite 32 Spalten.
Hoehe wird proportional berechnet (Half-Block-Zellen sind ~2:1 Pixel-Ratio).

Hintergrund:
- Sixel waere "echte" Bilder im Terminal, aber libsixel.dll ist auf Windows
  nicht trivial zu beschaffen (vcpkg/MSVC Source-Build).
- Half-Block kommt mit Pillow + ANSI-Escapes aus, laeuft in jedem Terminal das
  24-bit Truecolor kann (Windows Terminal: ja, klassische cmd.exe: nein).

Warum 2:1?
- Eine Konsolenzelle ist optisch grob 2x so hoch wie breit (font-abhaengig).
- Wir packen vertikal 2 "Pixel" pro Zelle rein -> Pixel werden nahezu quadratisch.
- Bei Ziel-Breite 32 Spalten ergibt das 32x32 "Pixel" Anzeigeflaeche.
"""

import argparse
import sys
from pathlib import Path

from PIL import Image

# ANSI: 24-bit Truecolor Foreground + Background, dann der Half-Block.
# "\x1b[38;2;R;G;Bm" = Foreground, "\x1b[48;2;R;G;Bm" = Background.
# Half-Block-Glyph ▀ (U+2580) faerbt obere Haelfte FG, untere BG.
# "\x1b[0m" Reset am Zeilenende, damit der Hintergrund nicht weiterzieht.
_RESET = "\x1b[0m"


def render_halfblock(img: Image.Image, width: int) -> str:
    """Wandelt Pillow-Bild in einen mehrzeiligen Half-Block-String fuer das Terminal.

    img: beliebiges Pillow-Image; wird auf width x (2*proportional) skaliert.
    width: Ziel-Spaltenzahl (= Pixel-Breite des Sprites).
    """
    img = img.convert("RGBA")
    # Hoehe so, dass Aspect-Ratio passt UND auf 2er-Vielfaches gerundet
    # (jede Zelle braucht 2 Pixel). +0.5 fuer rundungs-faires Nearest.
    aspect = img.height / img.width
    target_h = max(2, int(width * aspect + 0.5))
    if target_h % 2:
        target_h += 1
    img = img.resize((width, target_h), Image.LANCZOS)
    px = img.load()
    lines = []
    for y in range(0, target_h, 2):
        row = []
        for x in range(width):
            r1, g1, b1, a1 = px[x, y]
            r2, g2, b2, a2 = px[x, y + 1]
            # Transparente Pixel als "kein Fill" simulieren -> 0,0,0 (Terminal-BG).
            # Reicht fuer schwarze Backgrounds; auf hellem Theme leicht stoerend.
            if a1 == 0:
                r1 = g1 = b1 = 0
            if a2 == 0:
                r2 = g2 = b2 = 0
            row.append(
                f"\x1b[38;2;{r1};{g1};{b1}m"
                f"\x1b[48;2;{r2};{g2};{b2}m"
                "▀"
            )
        row.append(_RESET)
        lines.append("".join(row))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Half-Block-Vorschau eines Bildes im Terminal")
    ap.add_argument("image", nargs="?",
                    default="avatar/backgrounds/tutor.jpg",
                    help="Pfad zum Bild (default: avatar/backgrounds/tutor.jpg)")
    ap.add_argument("--width", type=int, default=32,
                    help="Ziel-Spaltenzahl (default: 32)")
    args = ap.parse_args()

    path = Path(args.image)
    if not path.is_file():
        print(f"Bild nicht gefunden: {path}", file=sys.stderr)
        return 2
    try:
        img = Image.open(path)
    except Exception as e:
        print(f"Bild konnte nicht geladen werden: {e}", file=sys.stderr)
        return 2

    # cp1252-Konsolen-Bug entschaerfen (Stolperfalle 8 aus CLAUDE.md)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print(f"# {path.name}  ({img.width}x{img.height} -> {args.width} cols)")
    print(render_halfblock(img, width=args.width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
