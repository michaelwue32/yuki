# tools/fetch_face_models.py — holt das buffalo_s-Modellpack einmalig self-hosted.
# insightface legt Modelle nach ~/.insightface/models/buffalo_s/ ab; der Download
# geschieht beim ersten FaceAnalysis(name="buffalo_s"). Dieses Skript triggert genau das
# einmal bewusst (mit Netz), danach laeuft alles offline.
import sys


def main():
    try:
        from insightface.app import FaceAnalysis
    except Exception as e:
        print(f"insightface nicht installiert ({e}).")
        print("Installiere:  .venv/Scripts/python.exe -m pip install insightface onnxruntime")
        print("Falls der Build auf Py3.14 scheitert -> Fallback-Weg in face_recog.py (_get_app).")
        sys.exit(1)
    app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))  # ctx_id=-1 => CPU
    print("buffalo_s bereit (Detektor + ArcFace). Modelle unter ~/.insightface/models/buffalo_s/.")


if __name__ == "__main__":
    main()
