# face_recog.py — Server-seitige Gesichtserkennung (SCRFD + ArcFace), CPU, offline.
# Rein beratend: liefert "wer ist im Bild" als Kontext, greift NIE in Identitaet/Heart ein.
import json
from pathlib import Path

import numpy as np

import yuki_core as yc  # nutzt _atomic_write_text, _cfg, MEMORY_DIR, load_people/save_people

MODEL_ID       = "buffalo_s"
FACES_FILE     = yc.MEMORY_DIR / "yuki_faces.json"
FACES_CROP_DIR = yc.MEMORY_DIR / "faces"
MODELS_DIR     = Path(__file__).resolve().parent / "data" / "face_models"


def _cfg_faces(key, default):
    return yc._cfg("faces", key, default)


def _empty_store():
    return {"model_id": MODEL_ID, "known": {}, "unassigned": []}


def load_faces():
    """Immer vollstaendiges Store-Dict; defensiv bei fehlender/kaputter Datei."""
    try:
        raw = json.loads(FACES_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _empty_store()
        raw.setdefault("model_id", MODEL_ID)
        raw.setdefault("known", {})
        raw.setdefault("unassigned", [])
        return raw
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_store()


def save_faces(store):
    yc._atomic_write_text(FACES_FILE, json.dumps(store, ensure_ascii=False, indent=2))


def match_embedding(vec, known_vecs, sim_threshold, margin):
    """Konservativer Cosinus-Match: nur wenn bester >= sim_threshold UND
    Abstand zur zweitbesten PERSON >= margin. Sonst (None, best_sim)."""
    per_person = []
    for pid, vecs in known_vecs.items():
        if not vecs:
            continue
        s = max(float(np.dot(vec, kv)) for kv in vecs)
        per_person.append((pid, s))
    if not per_person:
        return None, -1.0
    per_person.sort(key=lambda x: x[1], reverse=True)
    best_id, best_sim = per_person[0]
    second_sim = per_person[1][1] if len(per_person) > 1 else -1.0
    if best_sim >= sim_threshold and (best_sim - second_sim) >= margin:
        return best_id, best_sim
    return None, best_sim


def _all_known_vecs(store):
    out = {}
    for pid, entry in store.get("known", {}).items():
        out[pid] = [np.asarray(e["vec"], dtype=np.float32) for e in entry.get("embeddings", [])]
    return out


def should_collect_unassigned(vec, store, dedupe_threshold):
    """True wenn vec neu genug ist (Cosinus zu ALLEM Bekannten + Unassigned < dedupe_threshold)."""
    pools = []
    for vecs in _all_known_vecs(store).values():
        pools.extend(vecs)
    pools.extend(np.asarray(u["vec"], dtype=np.float32) for u in store.get("unassigned", []))
    for kv in pools:
        if float(np.dot(vec, kv)) >= dedupe_threshold:
            return False
    return True


def add_unassigned(store, vec, crop_rel, uid, ts, cap):
    store.setdefault("unassigned", []).append(
        {"id": uid, "vec": np.asarray(vec, dtype=np.float32).tolist(),
         "crop": crop_rel, "ts": ts, "source": "webcam"})
    overflow = len(store["unassigned"]) - cap
    if overflow > 0:
        store["unassigned"] = store["unassigned"][overflow:]
    return store


def person_display_name(person_id, store):
    if person_id == "michael":
        return "Michael"
    try:
        for p in yc.load_people():
            if p.get("id") == person_id or yc._people_slug(p.get("name", "")) == person_id:
                return p.get("name") or person_id
    except Exception:
        pass
    return person_id


def has_any_face(recognition):
    return bool(recognition)


def format_presence_context(recognition, store, live=True):
    """Kontextzeile 'wer wurde erkannt'. live=True (Kamera live / ungezeigt) ->
    'Present in view'. live=False (Michael zeigt ein Foto, evtl. aelter/fremd) ->
    'Recognized in this image', damit das LLM nicht faelschlich Live-Anwesenheit
    annimmt (z.B. ein altes Foto). Leer wenn keine BEKANNTE Person erkannt."""
    names = [person_display_name(r["identity"], store) for r in recognition if r.get("identity")]
    if not names:
        return ""
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n); uniq.append(n)
    joined = ", ".join(uniq)
    return f"[Present in view: {joined}]" if live else f"[Recognized in this image: {joined}]"


def recognized_people(recognition, store):
    """[{id, name}] fuer bekannte, erkannte Personen; dedupe nach id, Reihenfolge stabil.
    Rein fuer die UI-Anzeige (Icons an der Bild-Bubble) - unabhaengig vom Presence-Text."""
    out, seen = [], set()
    for r in (recognition or []):
        pid = r.get("identity")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "name": person_display_name(pid, store)})
    return out


import io, time
from PIL import Image

_APP = None  # lazy Singleton


def _get_app():
    """Lazy-Load des Inferenz-Backends (buffalo_s: SCRFD + ArcFace, CPU).
    Primaer via insightface.FaceAnalysis. Gibt None wenn nicht verfuegbar ->
    recognize_frame degradiert still (leere Liste)."""
    global _APP
    if _APP is not None:
        return _APP if _APP is not False else None
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _APP = app
        return app
    except Exception as e:
        print(f"  [Gesichtserkennung aus: {e} — tools/fetch_face_models.py ausfuehren?]", flush=True)
        _APP = False
        return None


def _crop_jpeg(bgr_img, bbox):
    """bbox [x1,y1,x2,y2] aus dem BGR-numpy-Bild als JPEG-Bytes (RGB)."""
    x1, y1, x2, y2 = [max(0, int(v)) for v in bbox]
    crop = bgr_img[y1:y2, x1:x2][:, :, ::-1]  # BGR->RGB
    buf = io.BytesIO()
    Image.fromarray(crop).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _jpeg_to_bgr(jpeg_bytes):
    rgb = np.asarray(Image.open(io.BytesIO(jpeg_bytes)).convert("RGB"))
    return rgb[:, :, ::-1].copy()  # RGB->BGR (insightface erwartet BGR)


def recognize_frame(jpeg_bytes):
    """Einziger oeffentlicher Erkennungs-Einstieg. [] bei aus/Fehler/kein Gesicht."""
    if not _cfg_faces("enabled", True) or not jpeg_bytes:
        return []
    app = _get_app()
    if app is None:
        return []
    try:
        bgr = _jpeg_to_bgr(jpeg_bytes)
        faces = app.get(bgr)
    except Exception as e:
        print(f"  [Gesichts-Detektion fehlgeschlagen: {e}]", flush=True)
        return []
    if not faces:
        return []
    store = load_faces()
    known = _all_known_vecs(store)
    sim_thr = float(_cfg_faces("sim_threshold", 0.42))
    margin = float(_cfg_faces("margin", 0.06))
    out = []
    for f in faces:
        vec = np.asarray(f.normed_embedding, dtype=np.float32)  # insightface liefert L2-normalisiert
        pid, sim = match_embedding(vec, known, sim_thr, margin)
        out.append({"identity": pid, "sim": sim, "bbox": [float(v) for v in f.bbox],
                    "crop_jpeg": _crop_jpeg(bgr, f.bbox), "vec": vec})
    return out


def embed_jpeg_face(jpeg_bytes):
    """Groesstes Gesicht -> (normalisierter Vektor, Crop-JPEG). None wenn keins. Fuer Bootstrap/Assign."""
    app = _get_app()
    if app is None or not jpeg_bytes:
        return None
    try:
        bgr = _jpeg_to_bgr(jpeg_bytes)
        faces = app.get(bgr)
    except Exception:
        return None
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    return np.asarray(f.normed_embedding, dtype=np.float32), _crop_jpeg(bgr, f.bbox)


def _cap_embeddings(entry):
    cap = int(_cfg_faces("max_embeddings_per_person", 12))
    if len(entry["embeddings"]) > cap:
        entry["embeddings"] = entry["embeddings"][-cap:]


def assign_unassigned(store, crop_id, target):
    idx = next((i for i, u in enumerate(store.get("unassigned", [])) if u["id"] == crop_id), None)
    if idx is None:
        return store
    u = store["unassigned"].pop(idx)
    if isinstance(target, dict) and target.get("new_person"):
        name = target["new_person"].strip()
        pid = yc._people_slug(name)
        people = yc.load_people()
        if not any(yc._people_slug(p.get("name", "")) == pid for p in people):
            people.append({"name": name, "aliases": [], "relationship": "", "of": "", "bricks": []})
            yc.save_people(people)
    else:
        pid = "michael" if target == "michael" else str(target)
    entry = store["known"].setdefault(pid, {"embeddings": []})
    entry["embeddings"].append({"vec": u["vec"], "crop": u["crop"], "added": u.get("ts", ""), "source": u.get("source", "webcam")})
    _cap_embeddings(entry)
    return store


def delete_face(store, person_id, crop_ref):
    if person_id is None:  # Unassigned per id
        store["unassigned"] = [u for u in store.get("unassigned", []) if u["id"] != crop_ref]
        return store
    entry = store.get("known", {}).get(person_id)
    if entry:
        entry["embeddings"] = [e for e in entry["embeddings"] if e.get("crop") != crop_ref]
    return store


def reassign_face(store, from_id, crop_ref, to_id):
    src = store.get("known", {}).get(from_id)
    if not src:
        return store
    moved = [e for e in src["embeddings"] if e.get("crop") == crop_ref]
    src["embeddings"] = [e for e in src["embeddings"] if e.get("crop") != crop_ref]
    dst = store["known"].setdefault(to_id, {"embeddings": []})
    dst["embeddings"].extend(moved)
    _cap_embeddings(dst)
    return store


def store_bootstrap(jpeg_bytes):
    """Aktuellen Frame -> groesstes Gesicht als 'michael' speichern (+Crop-Datei)."""
    res = embed_jpeg_face(jpeg_bytes)
    if res is None:
        return False
    vec, crop_jpeg = res
    FACES_CROP_DIR.mkdir(parents=True, exist_ok=True)
    uid = f"michael_{int(time.time()*1000)}"
    (FACES_CROP_DIR / f"{uid}.jpg").write_bytes(crop_jpeg)
    store = load_faces()
    entry = store["known"].setdefault("michael", {"embeddings": []})
    entry["embeddings"].append({"vec": vec.tolist(), "crop": f"faces/{uid}.jpg",
                                "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "webcam"})
    _cap_embeddings(entry)
    save_faces(store)
    return True
