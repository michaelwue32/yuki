"""Fetch openWakeWord ONNX-Modelle nach mobile/android/app/src/main/assets/wakeword/.

Drei Modelle bilden die openWakeWord-Pipeline:
- melspectrogram.onnx (~1.5 MB)  Audio-Samples -> Mel-Spektrogramm
- embedding_model.onnx (~13 MB)  Mel-Frames -> 96-dim Embedding
- hey_jarvis_v0.1.onnx (~30 KB)  Embeddings -> Wake-Prob [0, 1]

Custom 'Ohayoo Yuki'-Modell wird spaeter als ohayoo_yuki.onnx daneben gelegt
(selbe Pipeline, nur der letzte Classifier-Kopf neu trainiert).

Voraussetzung: `pip install openwakeword`.
Das Paket bringt die Pipeline-Modelle + Auto-Download fuer Wake-Word-Klassifier mit.

Idempotent: existierende Files mit korrekter Mindest-Groesse werden uebersprungen.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "mobile" / "android" / "app" / "src" / "main" / "assets" / "wakeword"

REQUIRED = [
    "melspectrogram.onnx",
    "embedding_model.onnx",
    "hey_jarvis_v0.1.onnx",
]
MIN_SIZE = 1024  # bytes - kleiner = leere/fehlerhafte Datei


def find_in_package() -> dict[str, Path]:
    """Sucht die Modelle im installierten openwakeword-Package + dessen Cache."""
    try:
        import openwakeword
    except ImportError:
        print("openwakeword nicht installiert. Bitte:")
        print("  pip install openwakeword")
        sys.exit(1)

    # Pipeline-Modelle (melspec + embedding) liegen IM Package, Wake-Modelle werden
    # bei Bedarf in ~/.cache/openwakeword oder %LOCALAPPDATA% nachgeladen.
    pkg_dir = Path(openwakeword.__file__).resolve().parent
    candidates = [
        pkg_dir / "resources" / "models",
        pkg_dir / "models",
    ]
    # Cache-Pfade (plattform-spezifisch)
    for env in ("LOCALAPPDATA", "APPDATA", "HOME"):
        import os
        v = os.environ.get(env)
        if v:
            candidates.append(Path(v) / ".cache" / "openwakeword")
            candidates.append(Path(v) / "openwakeword")

    # Bei Bedarf Wake-Modell explizit downloaden lassen
    try:
        from openwakeword.utils import download_models
        print("Stelle sicher dass alle Modelle lokal liegen (download_models)...")
        download_models(["hey_jarvis_v0.1"])
    except Exception as e:
        print(f"  (download_models Hinweis: {e})")

    found: dict[str, Path] = {}
    for name in REQUIRED:
        for c in candidates:
            p = c / name
            if p.exists() and p.stat().st_size > MIN_SIZE:
                found[name] = p
                break
    return found


def main() -> int:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Ziel: {ASSETS_DIR}")

    # Skip-Check: alle Modelle schon da?
    if all((ASSETS_DIR / n).exists() and (ASSETS_DIR / n).stat().st_size > MIN_SIZE for n in REQUIRED):
        print("Alle Modelle bereits vorhanden, nichts zu tun.")
        return 0

    found = find_in_package()
    missing = [n for n in REQUIRED if n not in found]
    if missing:
        print(f"FEHLT (im openwakeword-Package nicht gefunden): {missing}")
        print()
        print("Bitte pruefen:")
        print(f"  1. pip install --upgrade openwakeword")
        print(f"  2. python -c \"from openwakeword.utils import download_models; download_models()\"")
        print(f"  3. dann erneut: python tools/fetch_wakeword_models.py")
        return 1

    for name, src in found.items():
        dst = ASSETS_DIR / name
        if dst.exists() and dst.stat().st_size > MIN_SIZE:
            print(f"[skip] {name} ({dst.stat().st_size/1024:.1f} KB schon da)")
            continue
        print(f"[copy] {name}  ({src.stat().st_size/1024:.1f} KB)")
        print(f"       von {src}")
        shutil.copy2(src, dst)

    print("OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
