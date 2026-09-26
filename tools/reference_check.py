#!/usr/bin/env python3
r"""reference_check.py - Smoke-Test-Gate fuer Versions-Wechsel (TTS/STT/Vision/LLM).

Das Problem: "Teste die neue Modell-/Runtime-Version" ist sonst Bauchgefuehl.
Dieses Tool jagt eine FIXE Eingabe-Batterie (tests/reference/manifest.json) durch
die laufenden Services und vergleicht das Ergebnis mit einer einmal abgenommenen
Baseline. Ablauf rund um ein Update:

    1. Auf der ALTEN (bekannt-guten) Version einmal die Baseline abnehmen:
         .venv\Scripts\python.exe tools\reference_check.py --capture
    2. Neue Version daneben hochziehen (Side-by-Side, siehe docs/versioning.md).
    3. Gegen die Baseline pruefen:
         .venv\Scripts\python.exe tools\reference_check.py
    4. PASS -> Version promoten. DRIFT -> anschauen (WAVs/Replies liegen daneben).

Was geprueft wird (jede Sektion einzeln per --only filterbar):
  llm    : fixe Prompts -> Reply; harte Fehler bei verbotenen Strings (<think> etc.),
           sonst Laengen-/Text-Drift zum Vergleich gemeldet.
  tts    : Qwen3-TTS (DE/EN/JA) rendern; HTTP-OK, nicht-still (Peak), Dauer
           in Toleranz zur Baseline. WAVs liegen unter baselines/ zum Anhoeren.
  stt    : transkribiert die bei --capture aus TTS erzeugten WAV-Fixtures; vergleicht
           den Text fuzzy zur Baseline (gleiche Bytes rein -> isoliert STT-Drift).
  vision : beschreibt ein Bild-Fixture; Keyword-Overlap zur Baseline.

Defensiv: fehlt ein Service oder ein Fixture, wird die Probe als SKIP/ERROR
gemeldet, der Rest laeuft weiter. Nichts an Yukis State wird angefasst (read-only).

Aus der Yuki-Haupt-venv ausfuehren (requests + numpy + faster_whisper noetig).
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import wave
from pathlib import Path

import requests

# config_loader liegt im Projektroot (stdlib-only, laeuft aus jeder venv).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config_loader import settings  # noqa: E402

REF_DIR = ROOT / "tests" / "reference"
BASELINE_DIR = REF_DIR / "baselines"
FIXTURE_DIR = REF_DIR / "fixtures"
MANIFEST = REF_DIR / "manifest.json"

# Toleranzen
TTS_PEAK_MIN = 0.02          # darunter = Stille (vgl. Qwen Silent/Clip-Schutz)
TTS_DURATION_TOL = 0.30      # +-30% Dauer-Abweichung zur Baseline ist ok
STT_SIMILARITY_MIN = 0.70    # Transkript-Aehnlichkeit (0..1) als PASS-Schwelle


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------
def _wav_stats(data: bytes):
    """(peak 0..1, dauer_sek) aus WAV-Bytes. None bei Parse-Fehler."""
    try:
        import numpy as np
        with wave.open(io.BytesIO(data)) as w:
            n = w.getnframes()
            sr = w.getframerate()
            sw = w.getsampwidth()
            raw = w.readframes(n)
        if sw == 2:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sw == 4:
            arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 255.0 - 0.5
        peak = float(abs(arr).max()) if arr.size else 0.0
        dur = n / sr if sr else 0.0
        return peak, dur
    except Exception:
        return None


def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    norm = lambda s: "".join(c.lower() for c in s if c.isalnum() or c in "ぁ-んァ-ヶ一-龯")
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def _load_baseline(probe_id: str):
    p = BASELINE_DIR / f"{probe_id}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _save_baseline(probe_id: str, obj: dict):
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    (BASELINE_DIR / f"{probe_id}.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Service-Calls
# --------------------------------------------------------------------------
def call_llm(system: str, user: str) -> str:
    servers = settings.get("llm", "ollama_servers", [])
    last_err = "kein Server in settings"
    for entry in servers:
        try:
            name, base, model = entry[0], entry[1], entry[2]
            r = requests.post(f"{base.rstrip('/')}/api/chat", timeout=60, json={
                "model": model, "stream": False,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "options": {"temperature": 0.2, "num_ctx": 8192},
            })
            if r.ok:
                return r.json().get("message", {}).get("content", "")
            last_err = f"HTTP {r.status_code} @ {name}"
        except Exception as e:
            last_err = f"{type(e).__name__} @ {entry[0] if entry else '?'}: {e}"
            continue
    raise RuntimeError(last_err)


def call_tts_qwen(text: str, language: str) -> bytes:
    """Qwen3-TTS /tts -> komplettes WAV. language ist German|English|Japanese."""
    url = settings.get("qwen_tts", "url", "http://localhost:5006/tts")
    r = requests.post(url, json={"text": text, "language": language}, timeout=120)
    r.raise_for_status()
    return r.content


def call_vision(image_bytes: bytes, prompt: str) -> str:
    url = settings.get("vision", "url", "http://127.0.0.1:8081/v1/chat/completions")
    b64 = base64.b64encode(image_bytes).decode("ascii")
    r = requests.post(url, timeout=settings.get("vision", "timeout_seconds", 30), json={
        "max_tokens": settings.get("vision", "max_tokens", 150),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
    })
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


_WHISPER = {}
def transcribe(wav_path: Path, language: str) -> str:
    """faster-whisper, gleiches DLL-Setup wie tests/test_stt.py."""
    if "model" not in _WHISPER:
        import os
        venv_root = Path(sys.executable).parent.parent
        nvidia_root = venv_root / "Lib" / "site-packages" / "nvidia"
        dll_paths = []
        if nvidia_root.exists():
            for bin_dir in nvidia_root.rglob("bin"):
                if bin_dir.is_dir():
                    dll_paths.append(str(bin_dir))
                    try: os.add_dll_directory(str(bin_dir))
                    except Exception: pass
        # PATH-Erweiterung ist laut test_stt.py "der entscheidende Schritt" fuer
        # ctranslate2 - ohne sie findet cuDNN nicht und faellt still auf CPU.
        if dll_paths:
            os.environ["PATH"] = os.pathsep.join(dll_paths) + os.pathsep + os.environ.get("PATH", "")
        # Offline erzwingen: das Modell ist von Yukis Live-Betrieb gecacht. Ohne
        # das macht faster-whisper/HF beim Laden einen Netz-Check, der nach einem
        # frischen Neustart bei 0% CPU ewig haengen kann (genau der 5-min-Hang).
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from faster_whisper import WhisperModel
        name = settings.get("stt", "whisper_model", "medium")
        dev = settings.get("stt", "whisper_device", "cuda")
        print(f"    (lade faster-whisper '{name}' auf {dev}, offline/cache, ~30-60s ...)", flush=True)
        def _load(device, ct):
            try:
                return WhisperModel(name, device=device, compute_type=ct, local_files_only=True)
            except TypeError:  # aeltere faster-whisper ohne local_files_only-Kwarg
                return WhisperModel(name, device=device, compute_type=ct)
        try:
            _WHISPER["model"] = _load(dev, "float16")
        except Exception as e:
            print(f"    (cuda fehlgeschlagen: {type(e).__name__}; CPU-Fallback)", flush=True)
            _WHISPER["model"] = _load("cpu", "int8")
    segs, _ = _WHISPER["model"].transcribe(str(wav_path), language=language)
    return "".join(s.text for s in segs).strip()


# --------------------------------------------------------------------------
# Probe-Runner
# --------------------------------------------------------------------------
def run_llm(items, capture):
    out = []
    for it in items:
        pid = it["id"]
        try:
            reply = call_llm(it.get("system", ""), it["user"])
        except Exception as e:
            out.append((pid, "ERROR", str(e))); continue
        hits = [s for s in it.get("forbid", []) if s in reply]
        base = _load_baseline(f"llm_{pid}")
        if capture:
            _save_baseline(f"llm_{pid}", {"reply": reply, "len": len(reply)})
            out.append((pid, "CAPTURED", f"{len(reply)} Zeichen")); continue
        if hits:
            out.append((pid, "FAIL", f"verbotene Strings: {hits}")); continue
        if base is None:
            out.append((pid, "NO-BASE", "noch keine Baseline (--capture)")); continue
        dl = len(reply) - base["len"]
        out.append((pid, "PASS", f"Laengen-Delta {dl:+d} Zeichen"))
    return out


def run_tts(items, capture):
    out = []
    for it in items:
        pid = it["id"]
        try:
            data = call_tts_qwen(it["text"], it["language"])
        except Exception as e:
            out.append((pid, "ERROR", str(e))); continue
        # WAV ablegen (zum Anhoeren + als STT-Fixture)
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        (BASELINE_DIR / f"tts_{pid}.wav").write_bytes(data)
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        (FIXTURE_DIR / f"{pid}.wav").write_bytes(data)
        stats = _wav_stats(data)
        if stats is None:
            out.append((pid, "ERROR", "WAV nicht parsebar")); continue
        peak, dur = stats
        base = _load_baseline(f"tts_{pid}")
        if capture:
            _save_baseline(f"tts_{pid}", {"language": it["language"], "peak": peak, "dur": dur, "bytes": len(data)})
            out.append((pid, "CAPTURED", f"peak {peak:.3f}, {dur:.2f}s")); continue
        if peak < TTS_PEAK_MIN:
            out.append((pid, "FAIL", f"still (peak {peak:.3f})")); continue
        if base is None:
            out.append((pid, "NO-BASE", "noch keine Baseline (--capture)")); continue
        dd = abs(dur - base["dur"]) / base["dur"] if base["dur"] else 0
        status = "PASS" if dd <= TTS_DURATION_TOL else "DRIFT"
        out.append((pid, status, f"peak {peak:.3f}, Dauer {dur:.2f}s (base {base['dur']:.2f}s, {dd*100:.0f}% ab)"))
    return out


def run_stt(items, capture):
    out = []
    for it in items:
        pid = it["id"]
        wav = FIXTURE_DIR / f"{it['from_tts']}.wav"
        if not wav.exists():
            out.append((pid, "SKIP", f"Fixture fehlt ({wav.name}) - erst TTS --capture")); continue
        try:
            text = transcribe(wav, it["language"])
        except Exception as e:
            out.append((pid, "ERROR", str(e))); continue
        base = _load_baseline(f"stt_{pid}")
        if capture:
            _save_baseline(f"stt_{pid}", {"text": text})
            out.append((pid, "CAPTURED", text[:50])); continue
        if base is None:
            out.append((pid, "NO-BASE", "noch keine Baseline (--capture)")); continue
        sim = _similarity(text, base["text"])
        status = "PASS" if sim >= STT_SIMILARITY_MIN else "DRIFT"
        out.append((pid, status, f"Sim {sim:.2f}  '{text[:40]}'"))
    return out


def run_vision(items, capture):
    out = []
    for it in items:
        pid = it["id"]
        img = REF_DIR / it["image"]
        if not img.exists():
            out.append((pid, "SKIP", f"Bild-Fixture fehlt ({it['image']})")); continue
        try:
            desc = call_vision(img.read_bytes(), it["prompt"])
        except Exception as e:
            out.append((pid, "ERROR", str(e))); continue
        base = _load_baseline(f"vision_{pid}")
        if capture:
            _save_baseline(f"vision_{pid}", {"desc": desc})
            out.append((pid, "CAPTURED", desc[:50])); continue
        if base is None:
            out.append((pid, "NO-BASE", "noch keine Baseline (--capture)")); continue
        wa = set(base["desc"].lower().split()); wb = set(desc.lower().split())
        overlap = len(wa & wb) / max(1, len(wa))
        status = "PASS" if overlap >= 0.4 else "DRIFT"
        out.append((pid, status, f"Overlap {overlap:.2f}  '{desc[:40]}'"))
    return out


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Referenz-Smoke-Gate fuer Versions-Wechsel.")
    ap.add_argument("--capture", action="store_true", help="Baseline NEU abnehmen (auf bekannt-guter Version).")
    ap.add_argument("--only", default="", help="Komma-Liste: llm,tts,stt,vision (Default: alle).")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"Manifest fehlt: {MANIFEST}", file=sys.stderr); return 1
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    only = {s.strip() for s in args.only.split(",") if s.strip()} or {"llm", "tts", "stt", "vision"}

    print(f"=== reference_check {'(CAPTURE)' if args.capture else '(COMPARE)'} ===")
    print(f"Sektionen: {', '.join(sorted(only))}\n")

    runners = [("llm", run_llm), ("tts", run_tts), ("stt", run_stt), ("vision", run_vision)]
    icon = {"PASS": "OK ", "CAPTURED": "CAP", "DRIFT": ">> ", "FAIL": "XX ",
            "ERROR": "ERR", "SKIP": "-- ", "NO-BASE": "?? "}
    worst = 0
    for sect, fn in runners:
        if sect not in only:
            continue
        items = man.get(sect, [])
        if not items:
            continue
        print(f"[{sect}]")
        t0 = time.time()
        for pid, status, detail in fn(items, args.capture):
            print(f"  {icon.get(status, status):3s} {pid:16s} {status:9s} {detail}")
            if status in ("FAIL", "ERROR"): worst = max(worst, 2)
            elif status in ("DRIFT", "NO-BASE"): worst = max(worst, 1)
        print(f"  ({time.time()-t0:.1f}s)\n")

    if args.capture:
        print("Baseline abgenommen -> tests/reference/baselines/. Bei spaeterem Lauf (ohne --capture) wird dagegen verglichen.")
    else:
        print({0: "Alles im gruenen Bereich.", 1: "Drift/fehlende Baseline - anschauen.",
               2: "Harte Fehler (FAIL/ERROR) - Version NICHT promoten."}[worst])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
