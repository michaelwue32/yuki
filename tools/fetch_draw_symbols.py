"""fetch_draw_symbols.py - holt die OpenMoji-Stempel-Bibliothek fuer Yukis Kuenstlerin
(Symbol-Komposition via <use>, [[yuki-drawing-feature]]).

Analog zu tools/fetch_mediapipe.py / fetch_web_vendor.py: self-hosted Offline-Assets,
KEIN CDN/Netz zur Laufzeit. Ergebnis liegt unter data/draw_symbols/ und ist GITIGNORED
(wieder besorgbar, ~20 MB) - nach einem frischen Clone bzw. Disaster-Recovery einmal laufen.

Was es tut:
  1. Laedt openmoji.json (Metadaten: annotation/tags/group/subgroups je Symbol).
  2. Laedt die beiden Release-Zips (color + black/line) und entpackt die SVGs.
  3. Normalisiert je Motiv ZWEI <symbol>-Varianten:
       - line : OpenMoji-black, Schwarz -> currentColor (damit Yuki pro <use color='..'> tinten kann)
       - color: OpenMoji-color unveraendert (flach bunt)
     viewBox bleibt erhalten (72x72), aeusseres <svg> faellt weg.
  4. Schreibt kompakte Bundles statt ~7400 Einzeldateien:
       data/draw_symbols/symbols_line.json   {slug: "<symbol id='slug' ...>...</symbol>"}
       data/draw_symbols/symbols_color.json  {slug: "<symbol id='slug-color' ...>...</symbol>"}
       data/draw_symbols/index.json          [{slug,hexcode,annotation,tags,group,subgroups,has_color}]
       data/draw_symbols/CREDITS.md          OpenMoji CC BY-SA 4.0 Namensnennung

Skintone-Varianten werden uebersprungen (Basis-Emoji genuegt, vermeidet Slug-Kollisionen).

Aufruf:  python tools/fetch_draw_symbols.py  [--limit N]  [--force]
stdlib-only (urllib/zipfile/json/re) - keine Extra-Deps.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "draw_symbols"

META_URL = "https://raw.githubusercontent.com/hfg-gmuend/openmoji/master/data/openmoji.json"
ZIP_COLOR = "https://github.com/hfg-gmuend/openmoji/releases/latest/download/openmoji-svg-color.zip"
ZIP_BLACK = "https://github.com/hfg-gmuend/openmoji/releases/latest/download/openmoji-svg-black.zip"

_UA = {"User-Agent": "yuki-fetch-draw-symbols/1.0 (+local tooling)"}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _slugify(annotation: str) -> str:
    s = annotation.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "symbol"


# Schwarz -> currentColor, damit <use color='#e0729a'/> den Strich einfaerbt. Nur echtes
# Schwarz; laengere Hex (#0001ff) bleibt unangetastet (negative Lookahead auf Hex-Ziffern).
# fill='none' (reine Outline) NICHT anfassen.
_BLACK_HEX6 = re.compile(r"#000000(?![0-9a-fA-F])", re.IGNORECASE)
_BLACK_HEX3 = re.compile(r"#000(?![0-9a-fA-F])", re.IGNORECASE)
_BLACK_WORD = re.compile(r"\b(fill|stroke)\s*(=|:)\s*(['\"]?)black\b")


def _to_current_color(svg_inner: str) -> str:
    s = _BLACK_HEX6.sub("currentColor", svg_inner)
    s = _BLACK_HEX3.sub("currentColor", s)
    s = _BLACK_WORD.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}currentColor", s)
    return s


_SVG_OPEN_RE = re.compile(r"<svg\b([^>]*)>(.*)</svg>", re.DOTALL | re.IGNORECASE)
_VIEWBOX_RE = re.compile(r"viewBox\s*=\s*['\"]\s*([^'\"]+?)\s*['\"]", re.IGNORECASE)


def _to_symbol(raw_svg: str, sym_id: str, *, tint: bool) -> str | None:
    """OpenMoji-SVG-String -> '<symbol id=...>inner</symbol>'. None wenn unparsebar."""
    m = _SVG_OPEN_RE.search(raw_svg)
    if not m:
        return None
    open_attrs, inner = m.group(1), m.group(2)
    vb_m = _VIEWBOX_RE.search(open_attrs)
    viewbox = vb_m.group(1).strip() if vb_m else "0 0 72 72"
    inner = inner.strip()
    if tint:
        inner = _to_current_color(inner)
    # 'id="emoji"' & Co aus dem Inneren stoeren nicht; nur die <symbol>-Huelle zaehlt.
    return f"<symbol id='{sym_id}' viewBox='{viewbox}'>{inner}</symbol>"


def _load_zip_svgs(zip_bytes: bytes) -> dict[str, str]:
    """Entpackt ein OpenMoji-Release-Zip -> {HEXCODE_UPPER: svg_string}."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".svg"):
                continue
            stem = Path(name).stem.upper()
            try:
                out[stem] = zf.read(name).decode("utf-8", "replace")
            except Exception:
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="nur N Motive (Test)")
    ap.add_argument("--force", action="store_true", help="auch bei vorhandenem Output neu bauen")
    args = ap.parse_args()

    if (OUT_DIR / "index.json").exists() and not args.force:
        print(f"[skip] {OUT_DIR/'index.json'} existiert schon - --force zum Neubauen.")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[1/4] Lade openmoji.json (Metadaten) ...")
    meta = json.loads(_get(META_URL).decode("utf-8"))
    print(f"      {len(meta)} Eintraege.")

    print("[2/4] Lade Release-Zips (color + black) ...")
    color_svgs = _load_zip_svgs(_get(ZIP_COLOR))
    black_svgs = _load_zip_svgs(_get(ZIP_BLACK))
    print(f"      color={len(color_svgs)} svg, black={len(black_svgs)} svg.")

    print("[3/4] Normalisiere zu <symbol> ...")
    line_bundle: dict[str, str] = {}
    color_bundle: dict[str, str] = {}
    index: list[dict] = []
    seen: set[str] = set()
    skipped = 0

    for entry in meta:
        if args.limit and len(index) >= args.limit:
            break
        if entry.get("skintone"):           # Skintone-Variante -> Basis genuegt
            continue
        hexcode = (entry.get("hexcode") or "").upper()
        if not hexcode:
            continue
        black_raw = black_svgs.get(hexcode)
        if not black_raw:                    # ohne Line-Variante kein Stempel
            skipped += 1
            continue
        slug = _slugify(entry.get("annotation") or hexcode)
        if slug in seen:                     # Kollision -> Hex anhaengen
            slug = f"{slug}-{hexcode.lower()}"
        if slug in seen:
            continue
        line_sym = _to_symbol(black_raw, slug, tint=True)
        if not line_sym:
            skipped += 1
            continue
        seen.add(slug)
        line_bundle[slug] = line_sym

        has_color = False
        color_raw = color_svgs.get(hexcode)
        if color_raw:
            color_sym = _to_symbol(color_raw, f"{slug}-color", tint=False)
            if color_sym:
                color_bundle[slug] = color_sym
                has_color = True

        index.append({
            "slug": slug,
            "hexcode": hexcode,
            "annotation": entry.get("annotation") or "",
            "tags": entry.get("tags") or "",
            "group": entry.get("group") or "",
            "subgroups": entry.get("subgroups") or "",
            "has_color": has_color,
        })

    print(f"      {len(index)} Motive normalisiert ({skipped} ohne brauchbares SVG uebersprungen).")

    print("[4/4] Schreibe Bundles ...")
    (OUT_DIR / "symbols_line.json").write_text(
        json.dumps(line_bundle, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "symbols_color.json").write_text(
        json.dumps(color_bundle, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=0), encoding="utf-8")
    (OUT_DIR / "CREDITS.md").write_text(
        "# Draw-Symbol-Bibliothek\n\n"
        "Stempel-Motive aus **OpenMoji** (https://openmoji.org) - das Open-Source-Emoji-Projekt "
        "der HfG Schwaebisch Gmuend.\n\n"
        "Lizenz: **CC BY-SA 4.0** (https://creativecommons.org/licenses/by-sa/4.0/).\n"
        "Verwendet in Yukis Kuenstlerin-Persona als Kompositions-Stempel (line = einfarbig/tintbar, "
        "color = flach bunt). Normalisiert zu <symbol>-Defs via tools/fetch_draw_symbols.py.\n",
        encoding="utf-8")

    print(f"[fertig] {len(index)} Motive -> {OUT_DIR}")
    print(f"         line-Bundle {(OUT_DIR/'symbols_line.json').stat().st_size//1024} KB, "
          f"color-Bundle {(OUT_DIR/'symbols_color.json').stat().st_size//1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
