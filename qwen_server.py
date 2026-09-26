"""
Yukis Qwen3-TTS-Service (faster-qwen3-tts, CUDAGraph-beschleunigt).
====================================================================
EINE Engine fuer DE+EN+JA aus je einer Referenz (DE-Clip fuer Deutsch, JA-Clip fuer
EN/JA), mit optionaler instruct-Emotion. Ersetzt (hinter tts.engine=qwen) SoVITS+F5.

Robustheit: faster-qwen3-tts 0.4.0 (CUDAGraph) loest gelegentlich einen CUDA
device-side assert aus (index_copy_ out of bounds), der den CUDA-Kontext vergiftet
-> danach alle Requests kaputt, nur Prozess-Neustart hilft. Darum:
  * SUPERVISOR-Modus (Default): startet den Worter als Subprozess und restartet ihn,
    wenn er an einem CUDA-Fehler stirbt. Der Supervisor selbst laedt KEIN Modell.
  * WORKER (QWEN_WORKER=1): der echte uvicorn-Server. Bei CUDA-Fehler: Input loggen +
    os._exit -> Supervisor holt ihn frisch zurueck. Ein boeser Turn = ein stummer Turn.
  * INPUT-LOG: jeder Request wird VOR dem Rendern in _qwen_inputs.log geschrieben ->
    nach einem Crash steht der Ausloeser als letzte Zeile drin.

Start (Dashboard oder manuell):
    set HF_HOME=D:\\Server\\qwen3-tts\\hf-cache
    D:\\Server\\qwen3-tts\\venv\\Scripts\\python.exe D:\\Projects\\yuki\\qwen_server.py
"""
import asyncio
import io
import os
import re
import sys
import threading
import time
import traceback

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("HF_HOME", r"D:\Server\qwen3-tts\hf-cache")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

try:
    from config_loader import settings as _CFG
    _Q_CFG = _CFG.get("qwen_tts", None, {}) or {}
except Exception:
    _Q_CFG = {}

# --- Konfiguration (leicht, kein Modell) -----------------------------------
MODEL_ID   = _Q_CFG.get("model_id", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
REF_AUDIO  = _Q_CFG.get("ref_audio", r"D:\Projects\yuki\voices\vv_109_東北イタコ.wav")
REF_TEXT   = _Q_CFG.get("ref_text",  "こんにちは。わたしの名前はゆきです。日本語の勉強を一緒にがんばりましょう。")
REF_DE_AUDIO = _Q_CFG.get("ref_audio_de", r"D:\Projects\yuki\voices\f5tts\ref_de_yuki.wav")
REF_DE_TEXT  = _Q_CFG.get("ref_text_de",  "Guten Morgen! Ich freue mich, dass wir uns wiedersehen. Setz dich, nimm dir Zeit, und dann fangen wir gemütlich an.")
PORT       = _Q_CFG.get("port", 5006)
HOST       = _Q_CFG.get("host", "127.0.0.1")   # "0.0.0.0" = LAN-erreichbar (Core-Split, Yuki-Core auf anderem Host)
SR_HDR     = int(_Q_CFG.get("sample_rate", 24000))
_VALID_LANGS = {"German", "English", "Japanese"}
# Serialisiert Modell-Zugriffe: faster-qwen3-tts nutzt EINEN statischen CUDAGraph-Puffer.
# Gleichzeitige generate()-Calls (Story prefetcht mehrere Absaetze parallel!) zerreissen
# ihn -> CUDA index-oob-Crash. Ein GPU = serielle TTS, gleichzeitige Requests warten kurz.
_GEN_LOCK = threading.Lock()
_INPUT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime", "qwen_inputs.log")

import json as _json
_NARRATOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config", "narrator_voices.json")


def _load_narrators(path=None):
    """Liest config/narrator_voices.json (qwen-lokal). Gibt (narrators, voices, default).
    'yuki' wird IMMER ergaenzt (aus REF_DE_AUDIO/REF_DE_TEXT), damit das Dropdown nie
    leer ist und das Bestandsverhalten erhalten bleibt. Robust: fehlende/kaputte Datei
    -> nur 'yuki'. Existenz der WAV-Dateien wird NICHT geprueft (qwen faellt beim
    Rendern auf die Default-Ref zurueck, wenn eine Ref fehlt)."""
    narrators, order = {}, []
    default = "yuki"
    try:
        with open(path or _NARRATOR_FILE, encoding="utf-8") as f:
            data = _json.loads(f.read())
        default = data.get("default") or "yuki"
        for v in (data.get("voices") or []):
            vid = (v.get("id") or "").strip()
            ra, rt = v.get("ref_audio"), v.get("ref_text")
            if vid and ra and rt:
                narrators[vid] = (ra, rt)
                order.append({"id": vid, "label": (v.get("label") or vid)})
    except Exception:
        pass
    if "yuki" not in narrators:
        narrators["yuki"] = (REF_DE_AUDIO, REF_DE_TEXT)
        order.insert(0, {"id": "yuki", "label": "Yuki"})
    if default not in narrators:
        default = "yuki"
    return narrators, order, default


_NARRATORS, _NARRATOR_LIST, _NARRATOR_DEFAULT = _load_narrators()


def _voices_payload():
    return {"voices": _NARRATOR_LIST, "default": _NARRATOR_DEFAULT}


def _ref_for(language, voice=None):
    """Referenz je Sprache: Deutsch -> DE-Clip (oder gewaehlte Erzaehlstimme),
    Englisch/Japanisch -> JA-Clip. Der voice-Override greift NUR fuer Deutsch
    (Erzaehl-Refs sind DE-Clips); JP/EN-Laeufe behalten die JA-Ref."""
    if language == "German":
        if voice and voice in _NARRATORS:
            return _NARRATORS[voice]
        return REF_DE_AUDIO, REF_DE_TEXT
    return REF_AUDIO, REF_TEXT


def _norm_lang(payload):
    lang = (payload.get("language") or "German").strip().capitalize()
    return lang if lang in _VALID_LANGS else "German"


_JA_SEG = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")          # Kana + CJK + Halbbreite-Katakana
_LATIN_SEG = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")       # Latein-Buchstaben inkl. Umlaute


def _segment_by_script(text, carrier_lang):
    """Zerlegt gemischten Text in Laeufe nach Schrift: JP-Zeichen -> 'Japanese',
    Latein-Buchstaben -> carrier_lang. Neutrale Zeichen (Space/Satzzeichen/Ziffern)
    kleben am aktuellen Lauf (starten keinen neuen). Behebt die gemischt-DE+JP-
    Aussprache: sonst wuerde eingebettetes Japanisch mit der Traegersprache verhunzt
    (おはよう -> 'o-ha-jo'). Rueckgabe [(run_text, run_lang), ...], nur sprechbare Laeufe.
    Reiner DE/EN-Text -> genau EIN Lauf (kein Regress)."""
    runs, cur, cur_ja = [], "", None
    for ch in text:
        if _JA_SEG.match(ch):
            cls = True
        elif _LATIN_SEG.match(ch):
            cls = False
        else:
            cur += ch                      # neutral -> an aktuellen Lauf
            continue
        if cur_ja is None:
            cur_ja = cls
        if cls != cur_ja:
            runs.append((cur, cur_ja))
            cur, cur_ja = "", cls
        cur += ch
    if cur:
        runs.append((cur, cur_ja if cur_ja is not None else False))
    return [(t.strip(), "Japanese" if is_ja else carrier_lang)
            for t, is_ja in runs if t.strip()]


def _log_input(kind, lang, instruct, text):
    """Request VOR dem Rendern protokollieren -> Crash-Ausloeser bleibt nachvollziehbar."""
    try:
        os.makedirs(os.path.dirname(_INPUT_LOG), exist_ok=True)
        with open(_INPUT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{kind}\t{lang}\t"
                    f"instruct={instruct!r}\tlen={len(text)}\t{text!r}\n")
    except Exception:
        pass


_CUDA_FATAL = ("CUDA error", "device-side assert", "index out of bounds",
               "an illegal memory access", "CUBLAS", "CUDA kernel errors")


def _is_cuda_fatal(exc):
    s = str(exc)
    return any(tok in s for tok in _CUDA_FATAL)


def _die_if_cuda(exc, ctx):
    """Bei vergiftetem CUDA-Kontext: Input-Log-Hinweis + harter Exit -> Supervisor
    startet frisch. Bei anderen Fehlern: nur melden (Request scheitert, Server lebt)."""
    if _is_cuda_fatal(exc):
        print(f"[Qwen] FATAL CUDA-Fehler ({ctx}) -> Prozess-Neustart via Supervisor. "
              f"Letzter Input in {_INPUT_LOG}", flush=True)
        try:
            with open(_INPUT_LOG, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\tCRASH\t{ctx}\t{exc!s:.200}\n")
        except Exception:
            pass
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(17)     # Supervisor respawnt


def _to_int16(arr):
    try:
        arr = arr.detach().cpu().numpy()
    except AttributeError:
        import numpy as _np
        arr = _np.asarray(arr)
    import numpy as np
    arr = np.squeeze(arr)
    if arr.ndim > 1:
        arr = arr[0]
    if np.issubdtype(arr.dtype, np.floating):
        return np.clip(arr * 32767.0, -32768, 32767).astype(np.int16), arr
    return arr.astype(np.int16), arr.astype(np.float32) / 32768.0


def build_app():
    """Laedt Modell + Warmup und baut die FastAPI-App. NUR im Worker aufgerufen."""
    import numpy as np
    from scipy.io import wavfile
    from fastapi import FastAPI, Request
    from fastapi.responses import Response, StreamingResponse
    from faster_qwen3_tts import FasterQwen3TTS

    print(f"[Qwen] Lade {MODEL_ID} ...", flush=True)
    t0 = time.time()
    model = FasterQwen3TTS.from_pretrained(MODEL_ID)
    print(f"[Qwen] geladen ({time.time()-t0:.1f}s). Warmup ...", flush=True)
    try:
        # Warmup mit BEIDEN Referenzen (DE+JA), damit die CUDAGraphen fuer beide
        # Ref-Formen aufgenommen sind (Ref-Wechsel zur Laufzeit war ein Crash-Verdacht).
        model.generate_voice_clone(text="Kurzer Aufwärmsatz.", language="German",
                                   ref_audio=REF_DE_AUDIO, ref_text=REF_DE_TEXT)
        model.generate_voice_clone(text="ウォームアップ。", language="Japanese",
                                   ref_audio=REF_AUDIO, ref_text=REF_TEXT)
        print("[Qwen] Warmup fertig.", flush=True)
    except Exception:
        print("[Qwen] Warmup fehlgeschlagen (nicht fatal):", flush=True)
        traceback.print_exc()

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok", "engine": "Qwen3-TTS", "model": MODEL_ID}

    @app.get("/voices")
    def voices():
        return _voices_payload()

    def _render(text, language, instruct, voice=None):
        """Voller Decode -> (int16-pcm @24k, sr) oder (None, None). Segmentiert gemischt
        DE/EN+JP: JP-Laeufe mit 'Japanese' + JA-Ref, Rest mit der Traegersprache + deren
        Ref; kurze Naht-Luecke zwischen Laeufen. _GEN_LOCK haelt ALLE Laeufe eines Requests
        (single CUDAGraph-Puffer)."""
        segs = _segment_by_script(text, language)
        if not segs:
            return None, None
        gap = np.zeros(int(0.08 * SR_HDR), np.int16)
        parts = []
        with _GEN_LOCK:
            for i, (run_text, run_lang) in enumerate(segs):
                ra, rt = _ref_for(run_lang, voice)
                wavs, sr = model.generate_voice_clone(
                    text=run_text, language=run_lang, ref_audio=ra, ref_text=rt, instruct=instruct)
                pcm, _wf = _to_int16(wavs[0])
                if pcm.size:
                    parts.append(pcm)
                    if i < len(segs) - 1:
                        parts.append(gap)
        if not parts:
            return None, None
        full = np.concatenate(parts)
        peak = float(np.abs(full).max()) / 32768.0 if full.size else 0.0
        if peak < 0.02:
            return None, None
        if peak > 0.99:
            full = np.clip(full.astype(np.float32) * (0.99 / peak), -32768, 32767).astype(np.int16)
        return full, SR_HDR

    @app.post("/tts")
    async def tts(req: Request):
        try:
            payload = await req.json()
        except Exception as e:
            return Response(b"", media_type="audio/wav", status_code=400)
        text = (payload.get("text") or "").strip()
        if not text:
            return Response(b"", media_type="audio/wav")
        language = _norm_lang(payload)
        instruct = payload.get("instruct") or None
        voice = payload.get("voice") or None
        _log_input("tts", language, instruct, text)
        print(f"[Qwen] synth [{language}] instruct={instruct!r}: {text[:80]!r}", flush=True)
        t0 = time.time()
        try:
            # In einen Thread auslagern: _render ist GPU-blockierend (mehrere Laeufe)
            # -> im async-Handler direkt aufgerufen wuerde er den Event-Loop (und damit
            # /health + alle anderen Requests) fuer die ganze Renderdauer blockieren.
            pcm, sr = await asyncio.to_thread(_render, text, language, instruct, voice)
        except Exception as e:
            traceback.print_exc()
            _die_if_cuda(e, "tts")
            return Response(b"", media_type="audio/wav", status_code=500)
        if pcm is None:
            return Response(b"", media_type="audio/wav", status_code=503)
        buf = io.BytesIO()
        wavfile.write(buf, sr, pcm)
        print(f"[Qwen] done ({time.time()-t0:.1f}s, {len(pcm)/sr:.1f}s audio)", flush=True)
        return Response(buf.getvalue(), media_type="audio/wav",
                        headers={"X-Sample-Rate": str(sr), "Cache-Control": "no-store"})

    @app.post("/tts_stream")
    async def tts_stream(req: Request):
        """Natives Token-Streaming (SoVITS-artig, ~0.5s TTFA; klanglich = Vollrender,
        A/B bestaetigt 2026-09-05). _GEN_LOCK haelt die GANZE Generierung: single
        CUDAGraph-Puffer -> gleichzeitige Requests wuerden ihn zerreissen (CUDA-Crash),
        sie warten stattdessen. Bei CUDA-Fehler: os._exit -> Supervisor respawnt."""
        try:
            payload = await req.json()
        except Exception:
            return Response(b"", media_type="application/octet-stream", status_code=400)
        text = (payload.get("text") or "").strip()
        if not text:
            return Response(b"", media_type="application/octet-stream")
        language = _norm_lang(payload)
        instruct = payload.get("instruct") or None
        voice = payload.get("voice") or None
        _log_input("tts_stream", language, instruct, text)
        print(f"[Qwen] stream [{language}] instruct={instruct!r}: {text[:80]!r}", flush=True)
        segs = _segment_by_script(text, language)
        chunk_size = int(_Q_CFG.get("chunk_size", 12))

        def generate():
            t0 = time.time(); first = True
            if not segs:
                return
            gap = np.zeros(int(0.08 * SR_HDR), np.int16).tobytes()
            try:
                with _GEN_LOCK:
                    for i, (run_text, run_lang) in enumerate(segs):
                        ra, rt = _ref_for(run_lang, voice)
                        for audio_chunk, sr, _timing in model.generate_voice_clone_streaming(
                                text=run_text, language=run_lang, ref_audio=ra, ref_text=rt,
                                instruct=instruct, chunk_size=chunk_size):
                            pcm, _ = _to_int16(audio_chunk)
                            if pcm.size == 0:
                                continue
                            if first:
                                print(f"[Qwen] TTFA {time.time()-t0:.2f}s", flush=True)
                                first = False
                            yield pcm.tobytes()
                        if i < len(segs) - 1:
                            yield gap
            except Exception as e:
                traceback.print_exc()
                _die_if_cuda(e, "tts_stream")

        return StreamingResponse(generate(), media_type="application/octet-stream",
                                 headers={"X-Sample-Rate": str(SR_HDR), "Cache-Control": "no-store"})

    return app


def _run_worker():
    import uvicorn
    app = build_app()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def _run_supervisor():
    """Startet den Worker als Subprozess, restartet ihn bei CUDA-Tod (rc!=0)."""
    import subprocess
    env = {**os.environ, "QWEN_WORKER": "1"}
    backoff = 2
    while True:
        print("[qwen-supervisor] starte Worker ...", flush=True)
        rc = subprocess.call([sys.executable, "-u", os.path.abspath(__file__)], env=env)
        if rc == 0:
            print("[qwen-supervisor] Worker sauber beendet (rc=0) -> Ende.", flush=True)
            break
        print(f"[qwen-supervisor] Worker rc={rc} (CUDA-Crash o.ae.) -> Neustart in {backoff}s ...", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 15)   # sanfter Backoff gegen Crash-Loops


if __name__ == "__main__":
    if os.environ.get("QWEN_WORKER") == "1":
        _run_worker()
    else:
        _run_supervisor()
