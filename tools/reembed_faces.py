# tools/reembed_faces.py — bei Modellwechsel: alle Vektoren aus den Crops neu embedden.
# Embeddings verschiedener Modelle sind nicht kompatibel; die Crops bleiben erhalten,
# daher ist ein Modell-Upgrade nur dieser Batch-Lauf (kein Datenverlust, keine Neu-Enrollment).
import sys
from pathlib import Path
# Skript lebt in tools/, face_recog liegt im Projekt-Root - manuell in sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import face_recog as fr


def _reembed_one(crop_rel):
    p = fr.FACES_CROP_DIR / crop_rel.replace("faces/", "")
    if not p.exists():
        print(f"  crop fehlt, uebersprungen: {crop_rel}")
        return None
    res = fr.embed_jpeg_face(p.read_bytes())
    if res is None:
        print(f"  kein Gesicht im crop: {crop_rel}")
        return None
    return res[0].tolist()


def main():
    store = fr.load_faces()
    n = 0
    for entry in store.get("known", {}).values():
        for e in entry.get("embeddings", []):
            vec = _reembed_one(e.get("crop", ""))
            if vec is not None:
                e["vec"] = vec; n += 1
    kept = []
    for u in store.get("unassigned", []):
        vec = _reembed_one(u.get("crop", ""))
        if vec is not None:
            u["vec"] = vec; kept.append(u); n += 1
    store["unassigned"] = kept
    store["model_id"] = fr.MODEL_ID
    fr.save_faces(store)
    print(f"Re-Embedding fertig: {n} Vektoren neu berechnet, model_id={fr.MODEL_ID}.")


if __name__ == "__main__":
    main()
