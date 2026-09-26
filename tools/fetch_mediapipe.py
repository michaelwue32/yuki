#!/usr/bin/env python3
"""Laedt die MediaPipe-tasks-vision-Assets fuer den Kamera-Hintergrund-Nebel.

Der Nebel (Person scharf / Hintergrund weich in der Kamera-Vorschau, server.py +
web/index.html) nutzt MediaPipes Selfie-Segmentation. Die Inferenz laeuft 100%
lokal im Browser; nur die WASM-Runtime + das .tflite-Modell muessen einmal
heruntergeladen werden. Sie liegen unter web/vendor/mediapipe/ und werden vom
Yuki-Server unter /vendor/... ausgeliefert (bewusst self-hosted statt CDN, damit
der Nebel auch ohne Internet/CDN laeuft - genau die Demo-Situation).

Die Dateien sind ~19 MB und re-downloadbar -> .gitignore haelt sie aus dem Repo
(analog F5/Piper/Wadoku). Nach einem frischen Clone einmal ausfuehren:

    .venv\\Scripts\\python.exe tools\\fetch_mediapipe.py

Stdlib-only (urllib), keine Extra-Deps.
"""
import sys
import urllib.request
from pathlib import Path

# Gepinnte Version - bei Update hier hochziehen und neu fetchen.
VERSION = "0.10.18"
CDN = f"https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{VERSION}"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/image_segmenter/"
             "selfie_segmenter/float16/latest/selfie_segmenter.tflite")

DEST = Path(__file__).resolve().parent.parent / "web" / "vendor" / "mediapipe"

# (relativer Zielpfad, URL)
FILES = [
    ("vision_bundle.mjs",                  f"{CDN}/vision_bundle.mjs"),
    ("wasm/vision_wasm_internal.js",       f"{CDN}/wasm/vision_wasm_internal.js"),
    ("wasm/vision_wasm_internal.wasm",     f"{CDN}/wasm/vision_wasm_internal.wasm"),
    ("wasm/vision_wasm_nosimd_internal.js",   f"{CDN}/wasm/vision_wasm_nosimd_internal.js"),
    ("wasm/vision_wasm_nosimd_internal.wasm", f"{CDN}/wasm/vision_wasm_nosimd_internal.wasm"),
    ("selfie_segmenter.tflite",            MODEL_URL),
]


def _fetch(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "yuki-fetch-mediapipe"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    return len(data)


def main() -> int:
    print(f"MediaPipe tasks-vision @ {VERSION} -> {DEST}")
    total = 0
    for rel, url in FILES:
        dest = DEST / rel
        try:
            n = _fetch(url, dest)
        except Exception as e:  # noqa: BLE001 - Fetch-Fehler sollen klar rauskommen
            print(f"  FEHLER {rel}: {e}", file=sys.stderr)
            return 1
        total += n
        print(f"  ok {rel:38s} {n/1024:8.1f} KB")
    print(f"Fertig. {total/1024/1024:.1f} MB gesamt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
