"""
Yuki - lokaler Capture-/Kamera-Frame-Dienst (dshow -> JPEG ueber HTTP)
======================================================================
Liefert Einzelbilder lokaler dshow-Quellen (HDMI-Capture, USB-Cam) als JPEG ueber
HTTP. Laeuft auf der Box, an der die Capture-Hardware physisch haengt (.101). Grund:
dshow ist ein WINDOWS-lokaler Grab auf GENAU diesem Host - der Yuki-Core laeuft seit
dem Umzug aber auf dem Linux-Notebook .103 und kann ein dshow-Geraet auf einer anderen
Maschine nicht ansprechen. Der Core holt die Frames deshalb als `http_snap` von hier.

Reused yuki_camera.grab() -> identische ffmpeg-dshow-Logik (Device, Warmup-Frames,
Video-Size) wie der lokale Pfad. Quellen kommen aus DIESER config/cameras.json (auf .101
bleibt z.B. `gamecap` ein dshow-Eintrag; auf dem Core .103 zeigt `gamecap` als http_snap
hierher).

Start (auf der Capture-Box, in der Yuki-.venv):
    .\\.venv\\Scripts\\python.exe capture_server.py            # -> 0.0.0.0:5008
    python capture_server.py --host 0.0.0.0 --port 5008

    GET /gamecap.jpg   -> aktueller Frame der Quelle 'gamecap' aus config/cameras.json
    GET /<source>.jpg  -> beliebige dshow/lokale Quelle
Voraussetzung: die HDMI-Capture muss ein Signal liefern (TV/Konsole an), sonst kann
ffmpeg keinen Frame ziehen (502).
"""

import argparse
import threading

from flask import Flask, Response, jsonify

import yuki_camera as ycam

_LOCK = threading.Lock()   # ein Capture-Device -> ffmpeg-Grabs serialisieren
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/<source>.jpg", methods=["GET"])
def frame(source):
    with _LOCK:
        jpg = ycam.grab(source)
    if not jpg:
        return jsonify({"error": f"Grab von Quelle '{source}' fehlgeschlagen "
                                 "(Quelle unbekannt oder kein Signal?)"}), 502
    return Response(jpg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5008)
    args = ap.parse_args()
    print(f"Capture-Frame-Dienst auf {args.host}:{args.port} "
          f"(Quellen aus config/cameras.json)", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
