#!/usr/bin/env python3
"""Laedt die Browser-Avatar-/UI-Libs self-hosted nach web/vendor/.

Bis 2026-06-14 zog web/index.html three.js, three-vrm und marked direkt von
unpkg.com (CDN). Das ist genau der Single-Point-of-Failure, den wir bei den
Modellen vermeiden: faellt unpkg eine Version, ist der Avatar (oder im Offline-
Demo-Fall ueberhaupt jedes UI-Markdown) tot. Dieses Skript spiegelt die exakt
gepinnten Versionen lokal, analog tools/fetch_mediapipe.py.

Danach zeigt die <importmap> in index.html auf /vendor/... statt unpkg. Der
Yuki-Server liefert web/vendor/ unter /vendor/ aus (gleiche Strecke wie die
MediaPipe-Assets).

Die Dateien sind ~2 MB total und re-downloadbar -> .gitignore haelt sie draussen
(analog mediapipe/). Nach einem frischen Clone einmal ausfuehren:

    .venv\\Scripts\\python.exe tools\\fetch_web_vendor.py

Stdlib-only (urllib), keine Extra-Deps.
"""
import sys
import urllib.request
from pathlib import Path

# Gepinnte Versionen - MUESSEN mit der <importmap> in web/index.html und mit
# config/versions.lock.jsonc:web_vendor uebereinstimmen. Bei Update: hier + dort
# hochziehen, neu fetchen, im Browser hart neu laden (Cache!).
THREE = "0.166.0"
VRM = "3.3.4"
MARKED = "12.0.2"
HLS = "1.5.17"

UNPKG = "https://unpkg.com"
DEST = Path(__file__).resolve().parent.parent / "web" / "vendor"

# (relativer Zielpfad unter web/vendor/, Quell-URL)
FILES = [
    # three core
    ("three/three.module.js",
     f"{UNPKG}/three@{THREE}/build/three.module.js"),
    # three addons - nur was index.html wirklich importiert (GLTFLoader) + dessen
    # transitive Addon-Dep (BufferGeometryUtils, von GLTFLoader relativ geladen).
    ("three/addons/loaders/GLTFLoader.js",
     f"{UNPKG}/three@{THREE}/examples/jsm/loaders/GLTFLoader.js"),
    ("three/addons/utils/BufferGeometryUtils.js",
     f"{UNPKG}/three@{THREE}/examples/jsm/utils/BufferGeometryUtils.js"),
    # three-vrm (vorgebundelt, nur 'three' als externer Peer)
    ("three-vrm/three-vrm.module.js",
     f"{UNPKG}/@pixiv/three-vrm@{VRM}/lib/three-vrm.module.js"),
    ("three-vrm/three-vrm-animation.module.js",
     f"{UNPKG}/@pixiv/three-vrm-animation@{VRM}/lib/three-vrm-animation.module.js"),
    # marked (Markdown im Cheatsheet/Research-Modal)
    ("marked/marked.min.js",
     f"{UNPKG}/marked@{MARKED}/marked.min.js"),
    # hls.js (Film-HLS-Wiedergabe im Medien-Modal, Bau 2) - klassischer <script>,
    # NICHT in der importmap; liegt flach unter web/vendor/hls.min.js.
    ("hls.min.js",
     f"{UNPKG}/hls.js@{HLS}/dist/hls.min.js"),
]


def _fetch(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "yuki-fetch-web-vendor"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    return len(data)


def _scan_unresolved_imports(dest: Path) -> list[str]:
    """Warnt, falls eine gefetchte JS-Datei relative Addon-Imports hat, die wir
    nicht mitgezogen haben (sonst bricht der Avatar erst zur Laufzeit)."""
    import re
    have = {p.resolve() for p in dest.rglob("*.js")}
    missing = []
    for js in dest.rglob("*.js"):
        try:
            txt = js.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"""from\s+['"](\.\.?/[^'"]+)['"]""", txt):
            rel = m.group(1)
            target = (js.parent / rel).resolve()
            if target not in have:
                missing.append(f"{js.name} -> {rel}")
    return missing


def main() -> int:
    print(f"Web-Vendor: three@{THREE}, three-vrm@{VRM}, marked@{MARKED}, hls.js@{HLS} -> {DEST}")
    total = 0
    for rel, url in FILES:
        dest = DEST / rel
        try:
            n = _fetch(url, dest)
        except Exception as e:  # noqa: BLE001
            print(f"  FEHLER {rel}: {e}", file=sys.stderr)
            return 1
        total += n
        print(f"  ok {rel:42s} {n/1024:8.1f} KB")
    print(f"Fertig. {total/1024/1024:.2f} MB gesamt.")

    missing = _scan_unresolved_imports(DEST)
    if missing:
        print("\n  WARNUNG - nicht aufgeloeste relative Imports (nachfetchen!):", file=sys.stderr)
        for m in missing:
            print(f"    {m}", file=sys.stderr)
        return 2
    print("Alle relativen Imports aufgeloest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
