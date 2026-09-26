#!/usr/bin/env python3
"""Standalone-Test fuer eine Reolink-Netzwerkkamera (PTZ + Snapshot, JSON-API).

Anders als tools/test_ptz.py (Hi3510-CGI) faehrt dieses Tool ueber den ECHTEN
Yuki-Treiber: es importiert yuki_camera und ruft dessen reolink-Pfad (Login-
Token-Cache, Snap, PtzCtrl ToPos) auf. Damit validiert der Test exakt den Code,
den der Server spaeter nutzt - kein Parallel-Stack, der auseinanderlaufen kann.

Credentials kommen aus config/cameras.json (gitignored) - nie hartkodiert.
Standard-Quelle ist 'roomcam'; mit letztem Argument override-bar.

Aufrufe (aus D:\\Projects\\yuki):
    .\\.venv\\Scripts\\python.exe tools\\test_reolink.py snap          # Snapshot -> runtime/_reolink_snap.jpg
    .\\.venv\\Scripts\\python.exe tools\\test_reolink.py presets       # konfigurierte Presets auflisten
    .\\.venv\\Scripts\\python.exe tools\\test_reolink.py goto 2        # auf Preset-id 2 fahren
    .\\.venv\\Scripts\\python.exe tools\\test_reolink.py tour 2        # id 2 -> Settle -> Snap -> Home
    .\\.venv\\Scripts\\python.exe tools\\test_reolink.py measure 5     # echte Anfahrtszeit Home->5 messen
    ... [roomcam]                                                      # optionaler Quellname als letztes Arg
"""
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yuki_camera as yc           # noqa: E402  (Pfad muss zuerst stehen)

OUT = Path(__file__).resolve().parent.parent / "runtime" / "_reolink_snap.jpg"


def _check_source(name):
    _, src = yc._source(name)
    if src.get("type") != "reolink":
        sys.exit(f"Quelle '{name}' ist nicht type 'reolink' (sondern "
                 f"{src.get('type')!r}) - test_reolink unterstuetzt nur die. "
                 f"Eintrag in config/cameras.json pruefen.")
    if "DEIN_REOLINK_PASSWORT" in str(src.get("pass")) or src.get("pass") in (None, "", "PASSWORT"):
        sys.exit(f"Quelle '{name}': Passwort noch nicht gesetzt. Trage das echte "
                 f"Reolink-Passwort in config/cameras.json ein (Feld 'pass').")
    return name


def _gray(source):
    from PIL import Image
    data = yc.grab(source)
    if not data:
        raise RuntimeError("Snapshot fehlgeschlagen (None) - Cam/Token/Netz pruefen.")
    return Image.open(io.BytesIO(data)).convert("L").resize((160, 90))


def _diff(a, b):
    from PIL import ImageChops
    hist = ImageChops.difference(a, b).histogram()
    return sum(i * c for i, c in enumerate(hist)) / (sum(hist) or 1)


def cmd_snap(source):
    data = yc.grab(source)
    if not data:
        sys.exit("Snapshot fehlgeschlagen (None).")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(data)
    print(f"  Snapshot -> {OUT}  ({len(data)} bytes)")


def cmd_presets(source):
    ps = yc.presets(source)
    if not ps:
        print("  (keine Presets konfiguriert)")
        return
    home = yc.home_preset(source)
    for pid, label in ps.items():
        print(f"  id {pid}{'  [home]' if pid == home else '':8}  {label}")


def cmd_goto(source, pid):
    print(f"Fahre auf Preset-id {pid} ...")
    print("  ->", yc.goto_preset(source, pid))


def cmd_tour(source, pid):
    home = yc.home_preset(source)
    print(f"1) Fahre auf Preset-id {pid} + warte bis still ...")
    img, label = yc.look_at(source, pid)        # goto + _wait_until_settled + grab (Treiberpfad)
    if not img:
        sys.exit("   Snapshot nach Anfahrt fehlgeschlagen.")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(img)
    print(f"   '{label}' -> {OUT}  ({len(img)} bytes)")
    print(f"2) Zurueck auf Home id {home} ...")
    print("  ->", yc.goto_preset(source, home))
    print("Fertig.")


def cmd_measure(source, pid):
    """Misst die echte Anfahrtszeit: Home -> Ziel, dann Snapshots pollen +
    Grauwert-Diff bis das Bild still steht. Bewegung erzeugt grosse Diffs,
    Stillstand ~0 -> die settle-Schwelle in cameras.json danach waehlen."""
    home = yc.home_preset(source)
    print(f"Home (id {home}) ...")
    yc.goto_preset(source, home)
    time.sleep(8)
    print(f"goto id {pid} + poll:")
    yc.goto_preset(source, pid)
    prev = _gray(source)
    t, interval, settled_at = 0.0, 0.8, None
    for _ in range(20):
        time.sleep(interval)
        t += interval
        cur = _gray(source)
        d = _diff(prev, cur)
        moving = d > 5
        print(f"  t={t:4.1f}s  diff={d:6.2f}  {'BEWEGT' if moving else 'steht '}")
        if not moving and settled_at is None and t > interval:
            settled_at = t
        elif moving:
            settled_at = None
        prev = cur
    if settled_at is not None:
        print(f"-> steht ab ~{settled_at:.1f}s. settle.max_s mit etwas Reserve "
              f"(z.B. {settled_at + 2:.0f}s) setzen; lead < {settled_at:.1f}s halten.")
    else:
        print("-> in der Messzeit nicht eindeutig still geworden (laenger messen "
              "oder Schwelle pruefen).")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    # optionaler Quellname als letztes Arg (Default 'roomcam')
    source = "roomcam"
    rest = args[1:]
    if rest and not rest[-1].lstrip("-").isdigit():
        source = rest[-1]
        rest = rest[:-1]
    _check_source(source)

    if cmd == "snap":
        cmd_snap(source)
    elif cmd == "presets":
        cmd_presets(source)
    elif cmd == "goto" and rest:
        cmd_goto(source, rest[0])
    elif cmd == "tour" and rest:
        cmd_tour(source, rest[0])
    elif cmd == "measure" and rest:
        cmd_measure(source, rest[0])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
