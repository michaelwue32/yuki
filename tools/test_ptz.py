#!/usr/bin/env python3
"""Standalone-Test fuer PTZ + Snapshot einer HiSilicon-Hi3510-Netzwerkkamera (upCam).

Reines Experiment, fasst Yuki NICHT an. Steuert die Kamera ueber ihre NATIVE
Hi3510-CGI (KEIN ONVIF: der ONVIF-Layer dieser Billig-Cam uebersetzt Preset-
Tokens fehlerhaft und quittiert auch ungueltige Tokens mit "OK"). Die CGI ist
die Quelle der Wahrheit - es ist exakt das, was die Kamera-Oberflaeche selbst
aufruft. Deckt den Wunsch-Ablauf ab:

    Position anfahren -> kurz warten -> Snapshot holen -> zurueck auf Position 1

WICHTIG zur Nummerierung: Die Kamera-Oberflaeche zeigt Positionen 1..8, die CGI
ist 0-basiert -> UI-Position N == CGI -number=(N-1). Das Skript spricht in
UI-Positionsnummern (1..8) und rechnet den Versatz selbst raus.

Aufrufe (aus D:\\Projects\\yuki):
    .\\.venv\\Scripts\\python.exe tools\\test_ptz.py snap        # Snapshot -> runtime/_ptz_snap.jpg
    .\\.venv\\Scripts\\python.exe tools\\test_ptz.py goto 5      # auf UI-Position 5 fahren
    .\\.venv\\Scripts\\python.exe tools\\test_ptz.py tour 5      # UI 5 anfahren -> Snap -> zurueck auf UI 1
"""
import sys
import time
import json
from pathlib import Path

import requests

# ---- Kamera-Konfig aus config/cameras.json (KEINE Credentials hartkodiert) --
# Liest die Default-Quelle (gleiche Datei wie der Server). cameras.json ist
# gitignored - daher steht hier nie ein Passwort. Vorlage: cameras.template.json.
_CAM_JSON = Path(__file__).resolve().parent.parent / "config" / "cameras.json"


def _load_source():
    import sys
    if not _CAM_JSON.exists():
        sys.exit("config/cameras.json fehlt - aus config/cameras.template.json anlegen.")
    cfg = json.loads(_CAM_JSON.read_text(encoding="utf-8"))
    name = cfg.get("default_source")
    src = (cfg.get("sources") or {}).get(name) or {}
    if src.get("type") != "http_snap" or (src.get("ptz") or {}).get("kind") != "hi3510":
        sys.exit(f"Default-Quelle '{name}' ist keine Hi3510-http_snap-Cam - test_ptz "
                 f"unterstuetzt nur die.")
    return src


_SRC = _load_source()
_PTZ = _SRC.get("ptz") or {}
USER = _SRC.get("user", "")
PASS = _SRC.get("pass", "")
SNAP_URL = _SRC["snap_url"]                           # upCam-Schnappschuss
CGI = _PTZ["base"].rstrip("/")                        # native Hi3510-CGI-Basis
_OFFSET = int(_PTZ.get("number_offset", -1))          # UI 1-basiert -> CGI 0-basiert
HOME_UI = int(_PTZ.get("home", 1))                    # Grundstellung, wohin 'tour' zurueckfaehrt
SETTLE_S = 7.0                                        # Wartezeit, bis die Cam die Pos erreicht hat
                                                      # (langsame Mechanik; weite Wege brauchen ~6-7s)
OUT = Path(__file__).resolve().parent.parent / "runtime" / "_ptz_snap.jpg"
# -----------------------------------------------------------------------------

_AUTH = requests.auth.HTTPBasicAuth(USER, PASS)


def _cgi(path):
    r = requests.get(f"{CGI}/{path}", auth=_AUTH, timeout=10)
    r.raise_for_status()
    return r.text.strip()


def goto_ui(ui, speed=None):
    """Faehrt auf UI-Position (1-basiert wie in der Kamera-Oberflaeche).
    CGI ist 0-basiert -> -number = ui + number_offset. Optional -speed (Hi3510 1-63)."""
    num = int(ui) + _OFFSET
    q = f"preset.cgi?-act=goto&-number={num}"
    if speed is not None:
        q += f"&-speed={speed}"
    resp = _cgi(q)
    sfx = f" speed={speed}" if speed is not None else ""
    print(f"  goto UI {ui} (-number={num}{sfx}) -> {resp}")
    return resp


def snapshot(path=OUT, quiet=False):
    r = requests.get(SNAP_URL, auth=_AUTH, timeout=10)
    r.raise_for_status()
    path.write_bytes(r.content)
    if not quiet:
        print(f"  Snapshot -> {path}  ({len(r.content)} bytes)")
    return path


def cmd_goto(ui, speed=None):
    print(f"Fahre auf UI-Position {ui} ...")
    goto_ui(ui, speed)


def _grab_gray():
    from io import BytesIO
    from PIL import Image
    r = requests.get(SNAP_URL, auth=_AUTH, timeout=10)
    return Image.open(BytesIO(r.content)).convert("L").resize((160, 90))


def wait_until_settled(max_s=15.0, interval=1.0, thresh=5.0, lead=1.5):
    """Pollt Snapshots bis die Cam steht (zwei Frames fast identisch). Gemessen:
    Bewegung ~40-70, Stillstand <1 -> Schwelle 5 trennt robust. 'lead' wartet
    erst kurz, damit die Mechanik sicher angelaufen ist (kein False-Positive vor
    Bewegungsbeginn). Gibt die gewartete Zeit zurueck."""
    from PIL import ImageChops
    time.sleep(lead)
    t = lead
    prev = _grab_gray()
    while t < max_s:
        time.sleep(interval)
        t += interval
        cur = _grab_gray()
        hist = ImageChops.difference(prev, cur).histogram()
        d = sum(j * c for j, c in enumerate(hist)) / (sum(hist) or 1)
        if d < thresh:
            return t
        prev = cur
    return t


def cmd_measure(ui, speed=None):
    """Misst die echte Anfahrtszeit: home -> Ziel -> Snapshots pollen + Bild-
    Differenz, bis die Cam steht. Bewegung erzeugt riesige Diffs, Monitor-
    Flackern nur kleine -> Schwelle ~6 trennt sauber. Beantwortet auch, ob
    -speed was bringt (zweimal mit/ohne speed laufen lassen + vergleichen)."""
    from PIL import ImageChops
    print(f"home (UI {HOME_UI}) ...")
    goto_ui(HOME_UI, speed)
    time.sleep(10)
    print(f"goto UI {ui}" + (f" speed={speed}" if speed is not None else "") + " + poll:")
    goto_ui(ui, speed)
    prev = _grab_gray()
    t, interval, settled_at = 0.0, 1.2, None
    for _ in range(14):
        time.sleep(interval)
        t += interval
        cur = _grab_gray()
        hist = ImageChops.difference(prev, cur).histogram()
        d = sum(j * c for j, c in enumerate(hist)) / (sum(hist) or 1)
        moving = d > 6
        print(f"  t={t:4.1f}s  diff={d:5.2f}  {'BEWEGT' if moving else 'steht '}")
        if not moving and settled_at is None and t > interval:
            settled_at = t
        elif moving:
            settled_at = None
        prev = cur
    if settled_at:
        print(f"-> steht ab ~{settled_at:.1f}s (SETTLE_S mit Reserve waehlen)")
    else:
        print("-> nach ~17s noch nicht eindeutig still")


def cmd_snap():
    snapshot()


def cmd_tour(ui):
    """Der Wunsch-Ablauf: Ziel anfahren -> warten bis still -> Snap -> Home (UI 1)."""
    print(f"1) Fahre auf UI-Position {ui} ...")
    goto_ui(ui)
    waited = wait_until_settled()
    print(f"   steht nach ~{waited:.1f}s")
    print("2) Snapshot ...")
    snapshot()
    print(f"3) Zurueck auf Home UI-Position {HOME_UI} ...")
    goto_ui(HOME_UI)
    print("Fertig.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "snap":
        cmd_snap()
    elif cmd == "goto" and len(sys.argv) >= 3:
        cmd_goto(sys.argv[2], sys.argv[3] if len(sys.argv) >= 4 else None)
    elif cmd == "tour" and len(sys.argv) >= 3:
        cmd_tour(sys.argv[2])
    elif cmd == "measure" and len(sys.argv) >= 3:
        cmd_measure(sys.argv[2], sys.argv[3] if len(sys.argv) >= 4 else None)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
