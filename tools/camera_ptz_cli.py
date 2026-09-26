"""Manuelles Testtool fuer die Kamera-Steuerung (echte Hardware). Analog
tools/test_reolink.py: geht ueber den ECHTEN yuki_camera-Treiber (kein
Parallel-Stack), hinter einer simplen Passwort-Schranke, damit es nicht
versehentlich die Cam bewegt.

Aufruf (aus Repo-Wurzel):
  .venv\\Scripts\\python.exe tools/camera_ptz_cli.py presets roomcam
  .venv\\Scripts\\python.exe tools/camera_ptz_cli.py snap    roomcam out.jpg
  .venv\\Scripts\\python.exe tools/camera_ptz_cli.py goto    roomcam 0
  .venv\\Scripts\\python.exe tools/camera_ptz_cli.py jog     roomcam left 300
  .venv\\Scripts\\python.exe tools/camera_ptz_cli.py save    roomcam 5 "Testpunkt"
  .venv\\Scripts\\python.exe tools/camera_ptz_cli.py delete  roomcam 5
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yuki_camera as cam

GUARD = "yukicam"   # simple Schranke gegen Versehen


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    if input(f"Schranke (tippe '{GUARD}'): ").strip() != GUARD:
        print("abgebrochen."); return
    op, name = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    if op == "presets":
        print("Labels (cameras.json):", cam.presets(name))
        print("Hardware  (GetPtzPreset):", cam.hw_presets(name))
        print("naechste freie id:", cam.next_free_preset_id(name))
    elif op == "snap":
        data = cam.grab(name)
        out = Path(rest[0] if rest else "snap.jpg")
        out.write_bytes(data or b""); print(f"{len(data or b'')} bytes -> {out}")
    elif op == "goto":
        print(cam.goto_preset(name, int(rest[0])))
    elif op == "jog":
        print(cam.jog(name, rest[0], ms=(int(rest[1]) if len(rest) > 1 else None)))
    elif op == "save":
        print("gespeichert als id", cam.save_preset(name, ui_num=int(rest[0]), name=rest[1]))
    elif op == "delete":
        cam.delete_preset(name, int(rest[0])); print("geloescht")
    else:
        print("unbekannte Operation:", op); print(__doc__)


if __name__ == "__main__":
    main()
