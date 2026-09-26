"""
Yuki - Standalone Whisper-STT-Microservice (GPU)
=================================================
Loest Whisper aus dem Core heraus: der Core (server.py) ruft diesen Dienst per HTTP an,
statt Whisper in-process zu laden. Use-Case Core-Split: der Core laeuft auf einer Box
ohne brauchbare GPU (z.B. Notebook mit firmware-gedeckelter Max-Q, die nicht boostet),
STT laeuft auf der echten GPU-Box (z.B. .101 mit RTX 3060).

Wiederverwendet yuki_core.load_whisper() + transcribe_bytes() -> IDENTISCHE JP-Bias-,
Halluzinations- und Confidence-Logik wie der lokale In-Process-Pfad. Keine Dopplung.

Start (auf der GPU-Box, in der Yuki-.venv, CUDA-DLLs setzt yuki_core beim Import):
    .\\.venv\\Scripts\\python.exe stt_server.py            # -> 0.0.0.0:5007
    python stt_server.py --host 0.0.0.0 --port 5007

Der Core zeigt per config stt.remote_url = http://<GPU-BOX>:5007/stt hierher.
WICHTIG: stt.remote_url MUSS auf DIESER Box leer sein - sonst riefe der Dienst sich
selbst rekursiv auf. Bei leerer remote_url laedt load_whisper das Modell lokal auf die GPU.

Antwort-JSON auf POST /stt (Felder wie transcribe_bytes-Tuple):
    {"text": "...", "language": "de", "prob": 0.98, "conf": {"mean":..,"min":..,"n":..}}
"""

import argparse
import threading

from flask import Flask, request, jsonify

import yuki_core as yc

_LOCK = threading.Lock()   # faster-whisper MODEL ist NICHT threadsafe -> Zugriffe serialisieren
MODEL = None

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": MODEL is not None,
                    "model": yc.WHISPER_MODEL,
                    "device": yc.WHISPER_DEVICE})


@app.route("/stt", methods=["POST"])
def stt():
    f = request.files.get("audio")
    if f is None:
        return jsonify({"error": "kein audio-Feld"}), 400
    data = f.read()
    language = (request.form.get("language") or "").strip() or None
    filt = request.form.get("filter", "1") != "0"
    with _LOCK:
        text, lang, prob, conf = yc.transcribe_bytes(
            MODEL, data, language=language, filter_hallucination=filt)
    return jsonify({"text": text, "language": lang, "prob": prob, "conf": conf})


def main():
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5007)
    args = ap.parse_args()
    if yc.STT_REMOTE_URL:
        raise SystemExit(
            "FEHLER: stt.remote_url ist auf DIESER Box gesetzt - der STT-Dienst wuerde "
            "sich selbst aufrufen. In config/settings.jsonc hier stt.remote_url leeren.")
    MODEL = yc.load_whisper()
    print(f"STT-Dienst bereit auf {args.host}:{args.port} "
          f"(Modell {yc.WHISPER_MODEL} / {yc.WHISPER_DEVICE})", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
