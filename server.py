"""
Yuki – Web-/Handy-BFF (Backend for Frontend)
============================================
Stellt Yukis Pipeline (aus yuki_core.py) ueber HTTPS bereit, damit du sie vom
Handy-Browser nutzen kannst – „halten zum Sprechen" vom Sofa aus, im WLAN.

Aufgabenteilung:
  * Dieser Server (auf dem 3060-Rechner): haelt das Whisper-Modell (GPU), den
    Gespraechsverlauf und das Gedaechtnis, ruft Ollama (Failover) und Qwen3-TTS,
    und liefert die Web-App (web/index.html) aus.
  * Das Handy: nimmt nur Mikrofon auf, schickt das Audio hoch, zeigt Text + Romaji
    an und spielt Yukis Antwort ab. Kein Python, keine GPU noetig.

Warum HTTPS (auch im LAN)? Browser geben getUserMedia (Mikrofon) nur in einem
„secure context" frei – also HTTPS oder localhost. Ueber die nackte LAN-IP per
HTTP wuerde das Handy den Mic-Zugriff verweigern. Daher: self-signed Zertifikat
(wird beim ersten Start automatisch erzeugt, mit der LAN-IP in SAN). Am Handy
einmal „Erweitert -> Trotzdem fortfahren" – danach funktioniert das Mikrofon.

Start (aus der venv, Qwen3-TTS + Ollama muessen laufen):
    .\\.venv\\Scripts\\python.exe server.py

Dann am Handy im selben WLAN aufrufen:   https://<LAN-IP>:8443
(die genaue URL printet der Server beim Start)
"""

import io
import re
import json
import time
import queue
import base64
import socket
import collections
import logging
import datetime
import ipaddress
import threading
import subprocess
import yuki_media as ym
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context

import yuki_core as yc
import yuki_actions as ya           # F-Pipeline: Action-Decider + Executor (async, im TTS-Schatten)
import yuki_camera as ycam          # Kamera-Quellen (BRIO/Netzwerk-Cam) + PTZ, config/cameras.json
import yuki_gaming as ygame          # Gaming Screen Companion (logic layer, dep-light)
import homeassistant                 # HA REST-Adapter (graceful no-op wenn nicht konfiguriert)
import yuki_history_db
import face_recog as fr                # Gesichtserkennung (SCRFD+ArcFace, CPU, rein beratend)
import yuki_guest_db                  # Gast-/Personen-Roh-Verlauf (Gast-Modus Phase 2)
import yuki_stories                   # Library der "ganzen Geschichten" (Erzaehlerin-Story-Modus)
import yuki_habits_db
# wadoku: Tap-to-Gloss-Stack fuer JP-Worte (Lookup gegen Wadoku-Wörterbuch +
# fugashi-Tokenisierung). Self-contained Modul; bricht graceful (is_available()
# returnt False) wenn data/wadoku.sqlite fehlt -> Endpoint /lookup gibt 503,
# Frontend rendert keine clickbaren Spans. Siehe wadoku.py + tools/import_wadoku.py.
import wadoku
# kanjidict: Kanji-Detail-Stack (KANJIDIC2-Metadaten + KanjiVG-Strichordnungs-
# SVGs). Optional - bricht graceful wenn data/kanjidic2.sqlite oder data/kanjivg/
# fehlen. Frontend ruft /kanji/<char> beim Oeffnen des Wadoku-Popups parallel zu
# /lookup; bei 503 wird die Detail-Sektion einfach weggelassen.
import kanjidict
# adventure_engine: rundenbasierte Spiel-Engine (Phase 1 seit 2026-06-07). Eigene
# State-Files unter memory/adventures/<id>.json - Adventure-Turns laufen NIE durch
# conversation.json, alle Verdichtungs-Gates sehen sie automatisch nicht.
# Architektur: docs/adventure-engine-walkthrough.md, [[yuki-next-ideas]] #15.
import adventure_engine
# adventure_generator: Multi-Pass-LLM-Pipeline fuer neue Manifests via Wizard
# im 🎲-Modal. 4-7 sequentielle Calls mit Thinking, SSE-Progress, Spoiler-
# Faecher bleibt im Server. Siehe adventure_generator.py Kopfkommentar.
import adventure_generator
# resonance_seed: gefuehrter, Canon-isolierter Wizard, in dem Yuki ihre Gefuehle zu
# Ankern frei reflektiert; ein Mapping-Pass projiziert die Prosa danach auf die feste
# Palette -> Read-only-Emotions-Kern. Step-driven (synchron), siehe Kopfkommentar.
import resonance_seed
import imagegen                       # ComfyUI-Bild-Generator (Gedankenbilder, :8000)

# Tunables aus config/settings.jsonc (Defaults greifen wenn Datei/Keys fehlen).
from config_loader import settings as _CFG
def _cfg(section, key, default):
    return _CFG.get(section, key, default)
_SRV = _cfg("server", None, {}) or {}

HERE = Path(__file__).parent
WEB_DIR = HERE / "web"
CERT_DIR = HERE / "certs"
PORT = _SRV.get("port", 8443)
# Mobile-TTS gestreamt (raw PCM chunked ans Handy -> Yuki redet frueher, wie auf dem PC).
# False = alter Weg (komplettes WAV inline als base64 in /respond + /see).
TTS_STREAM_MOBILE = _SRV.get("tts_stream_mobile", True)

# Absatz-Streaming: Qwen3 rendert pro /tts_stream-Call absatzweise. Statt das
# komplette Audio abzuwarten, splittet /tts_stream lange Antworten an Absatzgrenzen
# und synthetisiert/streamt Absatz fuer Absatz -> Yuki faengt nach dem ERSTEN Absatz
# an zu reden statt nach der ganzen Antwort (gefuehlt 10-20s weniger bei Geschichten/
# Recherche). Greift erst ab dieser Gesamtlaenge; jeder Chunk ist >= so viele Zeichen
# (an Absatzgrenze geschnitten). Kuerzere Antworten / Antworten ohne Absatzumbruch
# gehen als EIN Block raus. 0 = Feature aus. Kleiner = frueheres Erst-Audio (mehr,
# kleinere Chunks); groesser = weniger Schnittkanten/natuerlicher, dafuer spaeter Ton.
QWEN_STREAM_MIN_CHARS = _cfg("qwen_tts", "stream_min_chars", 120)

# ---------------------------------------------------------------------------
# Autonome Sicht (web): der Server selbst schaut periodisch durch seine BRIO
# und kommentiert von sich aus, wenn sich was Substanzielles veraendert hat.
# Logik analog main.py auto_vision_loop; das Resultat wird per Server-Sent
# Events (SSE) an alle offenen Browser-Tabs gepusht und dort wie eine normale
# Yuki-Antwort dargestellt + abgespielt. Im UI ein-/ausschaltbar + Werte live.
# ---------------------------------------------------------------------------
CAM_DEVICE = _SRV.get("cam_device", "Logitech BRIO")    # dshow-Name (ffmpeg -list_devices true -f dshow -i dummy)
CAM_FRAME = yc.RUNTIME_DIR / "_frame_web.jpg"           # Cleanup 2026-06-01: aus Root nach runtime/
CAM_WARMUP_FRAMES = _SRV.get("cam_warmup_frames", 45)   # gegen BRIO-Schwarzbild-Bug (wie main.py)

_AUTO_VIS = _SRV.get("auto_vision_default", {}) or {}
AUTO_VISION_WEB_DEFAULT_ENABLED  = _AUTO_VIS.get("enabled", False)            # Default aus -> Privatsphaere bewusst
AUTO_VISION_WEB_DEFAULT_INTERVAL = _AUTO_VIS.get("interval_seconds", 20)
AUTO_VISION_WEB_DEFAULT_COOLDOWN = _AUTO_VIS.get("cooldown_seconds", 120)
# Aktiver-Chat-Backoff: solange Michaels letzter Turn weniger als so viele Sekunden
# zurueckliegt, pausiert das autonome Beobachten ganz (kein Schwenk/Kommentar), damit
# es nicht in laufende Gespraeche reinredet. 0 = aus (altes Verhalten).
AUTO_VISION_QUIET_AFTER_ACTIVITY_SEC = _AUTO_VIS.get("quiet_after_activity_seconds", 90)

# Proaktive Yuki: nach Pausen ohne Interaktion meldet sie sich selbst.
# Min/Max bilden den Zufallsbereich fuer die naechste spontane Aktion - der Loop
# wuerfelt nach jeder Aktivitaet (User-Reply UND eigener Spontan-Turn) neu, damit
# es organisch wirkt und nie dichter als min hintereinander kommt.
_PROA = _SRV.get("proactive_default", {}) or {}
PROACTIVE_DEFAULT_ENABLED = _PROA.get("enabled", False)        # Default aus - User aktiviert bewusst
PROACTIVE_DEFAULT_MIN_SEC = _PROA.get("min_seconds", 300)      # frühestens 5 min nach letzter Aktivitaet
PROACTIVE_DEFAULT_MAX_SEC = _PROA.get("max_seconds", 600)      # spaetestens 10 min danach
# Schwelle fuer Spontan-Prompt-Auswahl: wenn der letzte Turn weniger als diese
# Zeit zurueck liegt UND HISTORY nicht leer ist, knuepft Yuki an den aktuellen
# Chat an (continue-Pool). Sonst frisches Thema (fresh-Pool). Default 1h.
PROACTIVE_CONTINUATION_THRESHOLD_SEC = _PROA.get("continuation_threshold_sec", 3600)

# ---------------------------------------------------------------------------
# Globaler Zustand. Single-User (nur Michael), daher genuegt EIN Verlauf.
# Eine Lock serialisiert die schwere Arbeit (Whisper ist nicht thread-safe, und
# der Verlauf darf nicht von zwei Requests gleichzeitig mutiert werden).
# ---------------------------------------------------------------------------
MODEL = None
HISTORY = []
# Gast-Modus (Phase 1, 2026-06-16, [[yuki-guest-identity]]): wenn jemand anderes als
# Michael spricht, laeuft der Turn EPHEMER - getrennt von HISTORY/conversation.json,
# keine Verdichtung, kein Memory-Write. Pro client_id ein Wegwerf-Puffer (nur die
# letzten Turns als Kontext); wird bei Identitaets-Wechsel via /guest/reset geleert.
GUEST_HISTORY = {}                     # client_id -> [ {role, content}, ... ]
GUEST_HISTORY_MAX = 24                 # gecappt: letzte ~12 Austausche reichen als Kontext
# Phase 2: pro client_id die aktive Gast-DB-Session (yuki_guest_db). So landet jede
# Sitzung als eigene session_id im Roh-Archiv; /guest/reset schneidet sie ab.
GUEST_SESSIONS = {}                    # client_id -> {session_id, person_id, name, kind}
MEMORY = ""                            # geteilte Langzeit-Erinnerung (persona-uebergreifend)
CURRENT_PERSONA = yc.load_persona()    # aktive Persona (zuletzt gewaehlte; per /persona umschaltbar)
SYSTEM_MSG = yc.build_system_msg("", CURRENT_PERSONA)
LOCK = threading.RLock()        # RLock: derselbe Thread darf reentrant nehmen
                                # (z.B. /respond haelt LOCK -> ruft _handle_marker_side_effects
                                # -> ruft bei Note-Marker _refresh_system_msg, das LOCK
                                # erneut nimmt. Mit normalem Lock -> Self-Deadlock).

_TURN_SEQ = 0
def _next_turn_id():
    """Prozess-eindeutige, monotone Turn-ID. Keystone fuer die async Action-Pipeline:
    korreliert chat_update / action_result / HTTP-Response / HISTORY-meta / Reload."""
    global _TURN_SEQ
    _TURN_SEQ += 1
    return f"T{int(time.time())}-{_TURN_SEQ}"


def _history_index_by_turn_id(turn_id):
    """Index des HISTORY-Eintrags mit dieser turn_id, oder None. Aufrufer haelt LOCK."""
    if not turn_id:
        return None
    for i in range(len(HISTORY) - 1, -1, -1):     # von hinten: juengster Turn zuerst
        if HISTORY[i].get("turn_id") == turn_id:
            return i
    return None


# Zustand der autonomen Sicht (vom Hintergrund-Thread + Routen geteilt):
_auto_web = {
    "enabled":      AUTO_VISION_WEB_DEFAULT_ENABLED,
    "interval":     AUTO_VISION_WEB_DEFAULT_INTERVAL,    # s zwischen Kamera-Checks
    "cooldown":     AUTO_VISION_WEB_DEFAULT_COOLDOWN,    # s Mindestabstand zwischen Kommentaren
    "quiet_after_activity": AUTO_VISION_QUIET_AFTER_ACTIVITY_SEC,  # s Chat-Backoff (0=aus)
    "last_check":   0.0,                                 # ts letzter Capture+Gate
    "last_comment": 0.0,                                 # ts letzte spontane Reaktion
    "last_desc":    "",                                  # zuletzt gesehene Szene (Vergleich, Nicht-PTZ)
    # PTZ-Rotation (schwenkbare Cam): Yuki schaut der Reihe nach durch ihre Presets.
    # Pro Position eine EIGENE Baseline, sonst meldet jeder Schwenk faelschlich "alles
    # neu" (die Aenderungserkennung vergleicht vorher-gegen-jetzt). rotate_cursor zeigt
    # auf die naechste anzufahrende Position.
    "last_desc_by_pos": {},                              # {f"{cam}#{pos}": letzte Beschreibung}
    "rotate_cursor":    0,
    "run":          True,                                # Thread-Laufflag
    # Baseline-Priming: beim Aktivieren EINMAL alle Blickpunkte anfahren + Baseline
    # setzen (sonst dauert es eine volle Runde, bis pro Position verglichen werden kann).
    # priming = Loop pausiert solange; priming_gen invalidiert einen alten Prime-Thread
    # bei schnellem Aus/Ein.
    "priming":      False,
    "priming_gen":  0,
}

# Manuelle Kamera-Steuerung offen -> Beobachten schwenkt diese Cam NICHT weg.
# cam_name -> Ablauf-Epoch (TTL, damit ein gestorbener Tab das Beobachten nicht
# dauerhaft blockiert; das Panel meldet /camera/edit begin/end explizit).
_camera_editing = {}
_camera_edit_lock = threading.Lock()
_CAMERA_EDIT_TTL = 300.0     # s


def _camera_edit_active(cam):
    now = time.time()
    with _camera_edit_lock:
        exp = _camera_editing.get(cam)
        if exp and exp > now:
            return True
        if exp:
            _camera_editing.pop(cam, None)
        return False


def _camera_edit_begin(cam):
    with _camera_edit_lock:
        _camera_editing[cam] = time.time() + _CAMERA_EDIT_TTL


def _camera_edit_end(cam):
    with _camera_edit_lock:
        _camera_editing.pop(cam, None)

# Gaming Screen Companion: Yuki schaut beim Zocken zu (stille Beisitzerin).
# Thread laeuft immer (daemon), aber macht nichts wenn enabled=False oder mem=None.
_gaming = {
    "enabled":         False,       # armed? (per UI einschalten)
    "run":             True,        # Thread-Laufflag
    "game":            "",          # aktueller Spielname
    "brief":           "",          # kurzes Session-Briefing (Ziel/Figur/Prämisse)
    "mem":             None,        # ygame.RollingScreenMemory (wird beim Armen gesetzt)
    "last_comment_ts": 0.0,         # ts letzter Kommentar -> Cooldown-Gate
    "last_scene_key":  "",          # _norm(letzter Gist) -> "same scene?"-Nudge
    "mode":            "game",      # "game" | "media" | "film" (Zuschau-Modus)
    "hints":           [],          # vom User kuratierte Korrektur-Hinweise (aktives Spiel)
    "spoiler_ok":      False,       # Film-Modus: kennt Michael den Film schon? (Andeutungen)
    "letsplay":        False,       # Let's-Play-Modus: nicht Michael spielt, sondern ein Streamer
    "streamer":        "",          # optionaler Streamer-Name (Freitext, komma-separiert), ephemer pro Arm
    "knowledge":       {},          # ephemerer Live-Wissens-Klotz (async gepflegt, pro Session)
    "last_frame":      None,        # zuletzt gegriffener Frame (fuer den Wissens-Worker)
    "last_frame_ts":   0.0,         # ts dazu (Staleness-Check)
    "knowledge_run":   True,        # Laufflag des Wissens-Worker-Threads
}

# Proaktive Spontan-Aussagen ohne Bild-Trigger:
_proactive = {
    "enabled":       PROACTIVE_DEFAULT_ENABLED,
    "min_sec":       PROACTIVE_DEFAULT_MIN_SEC,
    "max_sec":       PROACTIVE_DEFAULT_MAX_SEC,
    "last_activity": 0.0,                                # ts letzter User- oder Yuki-Turn (jeder Art)
    "next_at":       0.0,                                # ts ab wann naechste spontane Aktion erlaubt
    "run":           True,
}

# Steward-Loop (3. autonomer Background-Loop, 2026-06-13): Yuki entscheidet in
# Michaels Abwesenheit eigenstaendig, ob sie sich meldet ("Sehnsucht"). Sticky
# Runtime-State (enabled/notstop) liegt persistiert in memory/yuki_steward.json,
# Tunables live-reload in config/steward.json. ALLE Guardrails sind hier im Code
# (Quiet-Hours, Idle, Rate-Limit, Model-Floor, Notstop), NIE im Prompt
# (OpenClaw-Lehre: prompt-residente Regeln fallen beim 30-Turn-Komprimieren raus).
_steward = {
    "enabled":            False,                          # aus memory/yuki_steward.json (init_pipeline)
    "notstop":            False,                          # harter Kill-Flag, vor JEDEM Effektor frisch geprueft
    "run":                True,                           # Thread-Laufflag
    "start_ts":           0.0,                            # Server-Start (Idle zaehlt ab hier, nicht ab ts=0)
    "last_run":           {"sehnsucht": 0.0, "rss": 0.0}, # ts letzter Lauf pro Quelle (eigene Intervalle)
    "last_reach_out_ts":  0.0,                            # ts letzter Reach-Out (Rate-Limit min-gap)
    "last_sehnsucht_reach_out_ts": 0.0,                   # ts letzter SEHNSUCHT-Reach-Out (Kadenz, persistiert; 2026-07-10)
    "reach_outs_today":   0,                              # Token-Bucket-Zaehler (Reset bei Datumswechsel)
    "thoughts_today":     0,                              # Gedankenlog-Zaehler (eigener Bucket, 2026-06-17)
    "notes_today":        0,                              # autonome-Notizen-Zaehler (eigener Bucket)
    "last_thought_ts":    0.0,                            # ts letzter Gedanke (min-gap)
    "last_note_ts":       0.0,                            # ts letzter autonomer Notiz (min-gap)
    "day_stamp":          "",                             # lokaler Kalendertag der Zaehler
}

# Impuls-Gate (2026-07-02): Urteil statt Reflex fuer die zwei Dauer-Loops
# (proactive + auto_vision). Eigener leiser-Gedanken-Bucket, getrennt vom Steward
# (die drei Loops sollen sich nicht gegenseitig das Tagesbudget wegnehmen). Alle
# Guardrails im Code (Quiet-Hours/Model-Floor/Budget), Tunables live-reload in
# config/impulse.json. enabled=False dort = Rollback auf den alten Zwangs-Reflex.
_impulse = {
    "thoughts_today": 0,      # Tages-Budget des leisen Kanals (Reset bei Datumswechsel)
    "last_thought_ts": 0.0,   # ts letzter Impuls-Gedanke (min-gap)
    "day_stamp": "",          # lokaler Kalendertag des Zaehlers
}
_sse_clients = set()                   # set[queue.Queue], pro offenem EventSource-Tab eine
_sse_lock = threading.Lock()           # schuetzt _sse_clients (add/remove) UND _sse_seq/_sse_log
# Event-Replay (2026-06-12): jedes SSE-Event kriegt eine monoton steigende ID und
# landet in einem Ring-Buffer. Bei Reconnect spielt events() alle Events mit
# hoeherer ID nach -> ein Timer-Ablauf waehrend eines gedrosselten Hintergrund-
# Tabs (Teams-Call etc.) geht nicht mehr verloren. EventSource-Auto-Reconnect
# schickt Last-Event-ID als Header; ein manueller Reconnect haengt ?lastEventId= an.
_sse_seq = 0                                  # zuletzt vergebene Event-ID (0 = noch keine)
_sse_log = collections.deque(maxlen=256)      # [(id, frame_str), ...] - letzte Events fuer Replay

app = Flask(__name__, static_folder=None)


# Stiller Health-Endpoint fuer das Dashboard (tools/yuki_dashboard.py pollt alle 15s).
# Werkzeug loggt jeden Hit als Access-Line - bei einem polling-Tool spamt das den
# Yuki-Log-Tab voll mit "GET /health HTTP/1.1" 200 -. Wir installieren einen
# Logging-Filter der GENAU diese Zeile aussortiert; alle anderen Requests bleiben
# sichtbar (auch echte 200er auf '/' vom Browser).
@app.route("/health")
def _health():
    # Liefert JSON-Status (ok + Modell/Turns/Persona). HA pollt das fuer die
    # "Yuki online?"-Erkennung am Wandpanel; das Dashboard (tools/yuki_dashboard.py)
    # wertet nur den 200-Status aus (http_ok liest den Body nicht). Defensiv
    # gekapselt: ein transienter State-Read darf den Liveness-Check nie auf 500
    # kippen - solange der Prozess lebt, kommt 200.
    try:
        return jsonify({"ok": True, "ollama": yc.OLLAMA_MODEL,
                        "turns": len(HISTORY), "persona": CURRENT_PERSONA})
    except Exception:
        return jsonify({"ok": True})


@app.route("/archive/download/<int:fid>")
def archive_download(fid):
    """Reicht eine archivar-Datei per id an den Client durch (same-origin HTTPS,
    kein Mixed-Content; archivar-Adresse bleibt serverseitig). Scope B."""
    cfg = yc.load_archivar_config()
    if not cfg.get("enabled", True):
        return jsonify({"ok": False, "error": "archive disabled"}), 503
    url = cfg.get("url")
    try:
        r = requests.get(f"{url}/download/{fid}", stream=True,
                         timeout=cfg.get("timeout_s", 15))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return jsonify({"ok": False, "error": "archive unreachable"}), 502
    if r.status_code != 200:
        return jsonify({"ok": False, "error": f"archive http {r.status_code}"}), r.status_code
    headers = {}
    for h in ("Content-Disposition", "Content-Type", "Content-Length"):
        if h in r.headers:
            headers[h] = r.headers[h]
    return Response(stream_with_context(r.iter_content(chunk_size=65536)),
                    status=200, headers=headers)


@app.route("/archive/folder/<int:fid>")
def archive_folder(fid):
    """Reicht die Ordner-Geschwister-Liste (Bild-Blaettern) per id durch
    (same-origin HTTPS, archivar-Adresse bleibt serverseitig). Bau 1."""
    cfg = yc.load_archivar_config()
    if not cfg.get("enabled", True):
        return jsonify({"ok": False, "error": "archive disabled"}), 503
    url = cfg.get("url")
    params = {}
    category = request.args.get("category")
    if category:
        params["category"] = category
    try:
        r = requests.get(f"{url}/folder/{fid}", params=params,
                         timeout=cfg.get("timeout_s", 15))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return jsonify({"ok": False, "error": "archive unreachable"}), 502
    if r.status_code != 200:
        return jsonify({"ok": False, "error": f"archive http {r.status_code}"}), r.status_code
    return jsonify(r.json())


# ===========================================================================
# Film-Wiedergabe (Phase 3 "Tun", Bau 2). Logik in yuki_media (getestet);
# hier nur duenne Endpoints + der Job-Singleton mit realen Defaults.
# ===========================================================================
_MEDIA_CACHE_DEFAULT = str((Path(__file__).parent / "runtime" / "media_cache").resolve())


def _archivar_cfg_for_media():
    """archivar-Config mit gesetztem media.cache_dir-Default (runtime/media_cache)."""
    cfg = yc.load_archivar_config()
    media = dict(cfg.get("media") or {})
    if not media.get("cache_dir"):
        media["cache_dir"] = _MEDIA_CACHE_DEFAULT
    cfg = dict(cfg)
    cfg["media"] = media
    return cfg


def _ffprobe_real(url, timeout):
    try:
        out = subprocess.run(ym.build_ffprobe_cmd(url), capture_output=True,
                             text=True, timeout=timeout)
        return ym.parse_ffprobe_json(out.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ym.parse_ffprobe_json("")


def _spawn_ffmpeg(cmd):
    # stdout = Progress-Pipe (Text-Zeilen), stderr verworfen.
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)


_MEDIA = ym.MediaJobManager(
    archivar_cfg_fn=_archivar_cfg_for_media,
    spawn=_spawn_ffmpeg,
    probe=_ffprobe_real,
    now=time.time,
)


@app.route("/archive/media_prepare/<int:fid>", methods=["POST"])
def archive_media_prepare(fid):
    profile = request.args.get("profile", "desktop")
    ext = (request.args.get("ext") or "").lower()
    try:
        ym.validate_profile(profile)
    except ValueError:
        return jsonify({"ok": False, "error": "bad profile"}), 400
    try:
        out = _MEDIA.prepare(fid, profile, ext)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, **out})


@app.route("/archive/media_status/<int:fid>")
def archive_media_status(fid):
    profile = request.args.get("profile", "desktop")
    try:
        ym.validate_profile(profile)
    except ValueError:
        return jsonify({"ok": False, "error": "bad profile"}), 400
    return jsonify({"ok": True, **_MEDIA.status(fid, profile)})


@app.route("/archive/media_status_batch", methods=["POST"])
def archive_media_status_batch():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "desktop")
    ids = data.get("ids") or []
    try:
        ym.validate_profile(profile)
    except ValueError:
        return jsonify({"ok": False, "error": "bad profile"}), 400
    pairs = [(int(i), profile) for i in ids if isinstance(i, int)]
    return jsonify({"ok": True, "status": _MEDIA.cache_status_map(pairs)})


@app.route("/archive/hls/<int:fid>/<profile>/<path:seg>")
def archive_hls(fid, profile, seg):
    try:
        ym.validate_profile(profile)
    except ValueError:
        return jsonify({"ok": False, "error": "bad profile"}), 400
    cd = _archivar_cfg_for_media()["media"]["cache_dir"]
    sub = ym.cache_subdir(cd, fid, profile)
    # send_from_directory verhindert Traversal aus seg; MIME fuer m3u8/ts setzen.
    resp = send_from_directory(str(sub), seg)
    if seg.endswith(".m3u8"):
        resp.headers["Content-Type"] = "application/vnd.apple.mpegurl"
        resp.headers["Cache-Control"] = "no-store"   # wachsende Playlist nie cachen
    return resp


@app.route("/archive/stream/<int:fid>")
def archive_stream(fid):
    """Passthrough eines browserfaehigen Films inkl. Range/206 fuers <video>-Seeking."""
    cfg = yc.load_archivar_config()
    if not cfg.get("enabled", True):
        return jsonify({"ok": False, "error": "archive disabled"}), 503
    url = cfg.get("url")
    fwd = {}
    if request.headers.get("Range"):
        fwd["Range"] = request.headers["Range"]
    try:
        r = requests.get(f"{url}/download/{fid}", headers=fwd, stream=True,
                         timeout=cfg.get("timeout_s", 15))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return jsonify({"ok": False, "error": "archive unreachable"}), 502
    if r.status_code not in (200, 206):
        return jsonify({"ok": False, "error": f"archive http {r.status_code}"}), r.status_code
    headers = ym.passthrough_headers(r.headers)
    return Response(stream_with_context(r.iter_content(chunk_size=65536)),
                    status=r.status_code, headers=headers)


# Kategorie-Whitelist fuer die Direktsuche (deutsches Label wird im Frontend
# auf diese archivar-Werte gemappt; unbekannt -> kein Filter).
_DIRECT_SEARCH_CATS = {"document", "music", "image", "video", "archive", "binary", "code"}


@app.route("/archive/search_direct", methods=["POST"])
def archive_search_direct():
    """Direkte archivar-Datei-Suche an Yukis LLM vorbei - identischer Code-Pfad wie
    das search_index-Tool (reset/_tool_search_index/pop), nur ohne Modell. Speist das
    Sekretaerin-Direktsuche-Formular; Ergebnis rendert das bestehende fileHitsModal."""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    category = (data.get("category") or "").strip()
    if category not in _DIRECT_SEARCH_CATS:      # "Alle"/""/unbekannt -> kein Filter
        category = None
    raw_limit = data.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 20
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))   # Spec-Clamp [1,50] explizit (0 -> 1, nicht Default)
    yc.reset_file_hits()
    note = yc._tool_search_index(query, category, None, limit)
    hits = yc.pop_file_hits()
    meta = yc.pop_file_hits_meta()
    return jsonify({"ok": True, "hits": hits, "meta": meta, "count": len(hits), "note": note})


class _SuppressHealthAccessLog(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return '"GET /health' not in msg and '"HEAD /health' not in msg


logging.getLogger("werkzeug").addFilter(_SuppressHealthAccessLog())


# Long-Cache fuer praktisch unveraenderliche Static-Assets (VRM ~16 MB pro Outfit,
# 67 VRMAs ~17 MB, Persona-Backgrounds 5-7 MB pro PNG). Ohne explizites
# Cache-Control cacht Android-WebView ueber self-signed HTTPS nur heuristisch
# und zieht bei jedem App-Cold-Start gerne mal 30-40 MB neu - auf Mobil-Upload
# fuehlbar. max-age=30d + immutable, da VRM/VRMA/Backgrounds Wochen lang stabil
# sind; bei Edit muss man manuell den App-Cache leeren oder Datei umbenennen.
_LONG_CACHE_PREFIXES = ("/avatar/", "/capacitor.js", "/vendor/")


@app.after_request
def _long_cache_static(resp):
    p = request.path
    if any(p == pref or p.startswith(pref) for pref in _LONG_CACHE_PREFIXES):
        # /avatar/list und /avatar/animations/list sind JSON-Index-Endpoints,
        # die sich bei neuen Outfits/Clips aendern - die NICHT lang cachen.
        if p in ("/avatar/list", "/avatar/animations/list"):
            return resp
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return resp


# ===========================================================================
# Hintergrund-Maintenance nach jedem Turn (Heart-Gate, Verlauf-Verdichtung,
# Facts-Komprimierung). Alle drei laufen async und blocken den Response nie.
# ===========================================================================
def _announce_heart(entry):
    try: print(f"  [💖 ins Herz: {entry}]")
    except Exception: pass
    # Neuer Heart-Brick gehoert sofort in den Prompt - das qwen3-Gate ist streng,
    # wenn es feuert, ist die Aufnahme bewusst und Yuki soll den Brick beim
    # naechsten Turn sehen, nicht erst nach der naechsten Memory-Konsolidierung
    # (kann viele Turns dauern).
    _refresh_system_msg()


def _on_consolidated(new_memory, new_history, original_len):
    """Vom Hintergrund-Thread: aelteren Verlauf in Memory verdichtet. Hier
    uebernehmen wir das in-process state UNTER LOCK und mergen ggf. in der
    Zwischenzeit dazugekommene Turns (HISTORY[original_len:]) hinten an.
    SYSTEM_MSG muss neu gebaut werden, weil MEMORY drin steckt."""
    global MEMORY, SYSTEM_MSG
    with LOCK:
        MEMORY = new_memory
        HISTORY[:] = list(new_history) + HISTORY[original_len:]
        SYSTEM_MSG = yc.build_system_msg(MEMORY, CURRENT_PERSONA)
        yc.save_history(HISTORY)


def _on_facts_compressed(before, after):
    """Vom Hintergrund-Thread: Facts-Liste wurde geschrumpft. SYSTEM_MSG neu bauen,
    damit der frische Stand sofort im Prompt landet (sonst sieht das LLM die
    alten Facts bis zur naechsten Memory-Konsolidierung)."""
    print(f"  [Facts {before} -> {after}, System-Prompt aktualisiert]")
    _refresh_system_msg()


def _post_turn(user_text, reply):
    """Nach jedem Turn (respond/see/auto-vision/timer/proactive) aufrufen. Schaltet sich
    selbst aus, wenn Schwellen nicht erreicht sind. Wichtig: list(HISTORY) als Kopie,
    damit der Snapshot im Hintergrund nicht mit weiteren Mutationen kollidiert."""
    if user_text and reply:
        yc.maybe_archive_heart(user_text, reply, on_saved=_announce_heart)
    yc.maybe_consolidate_history_async(list(HISTORY), MEMORY, on_done=_on_consolidated)
    yc.maybe_compress_facts_async(on_done=_on_facts_compressed)
    # Heute-Tier (2026-06-16): leichtes Async-Gate, das geklaerte fixe Tagestermine
    # (Essen/Pause/Plan) festhaelt, damit Yuki nicht alle ~20 Min erneut danach
    # fragt. Drosselt sich selbst (alle paar Companion-Turns), laeuft im Hintergrund.
    yc.maybe_capture_today_async(list(HISTORY), persona=CURRENT_PERSONA)
    _proactive_reset_clock()


def _stamp_gedankenbild(entry, actions):
    """Aus den Action-Records das (erste) Gedankenbild an den Turn heften: Liste
    starten + Prompt/Stil serverseitig merken (fuer spaeteres Neu-Wuerfeln). prompt/
    style werden aus dem Record entfernt - sie duerfen NICHT broadcastet/persistiert
    werden (bleiben serverseitig, nur zum Regenerate)."""
    for rec in actions:
        if rec.get("type") == "gedankenbild" and rec.get("ok") and rec.get("file"):
            entry["gedankenbilder"] = [rec["file"]]
            entry["gedankenbild_prompt"] = rec.get("prompt", "")
            entry["gedankenbild_style"] = rec.get("style", "")
        rec.pop("prompt", None)
        rec.pop("style", None)


def _run_action_pipeline(turn_id, user_text, yuki_prosa, recent_turns, target_client_id,
                         allow_timer=True, note_source="michael"):
    """F-Pipeline (async, im Schatten der TTS): D urteilt, E fuehrt aus, dann LOCK-
    Repatch der HISTORY-meta + action_result-SSE. Meldet IMMER (auch actions:[]),
    damit der Frontend-Spinner nie haengt. allow_timer=False (Watch) unterdrueckt NUR
    den Timer (kein Orphan auf einem Geraet ohne Weck-Funktion), andere Aktionen laufen."""
    actions = []
    try:
        decisions = ya.run_action_decider(user_text, yuki_prosa, recent_turns)
        # Gedankenbild blockt ~60s beim Rendern -> frueher Spinner an der Bubble,
        # BEVOR execute laeuft (sonst 'es passiert nichts'-Luecke).
        if any(isinstance(tc, dict) and (tc.get("function") or {}).get("name") == "gedankenbild"
               for tc in (decisions or [])):
            pev = {"kind": "gedankenbild_pending", "turn_id": turn_id}
            if target_client_id:
                pev["target_client_id"] = target_client_id
            _broadcast_sse(pev)
        actions = ya.execute_action_decisions(decisions, target_client_id=target_client_id,
                                              allow_timer=allow_timer, note_source=note_source)
    except Exception as e:
        print(f"  [Action-Decider Fehler (ignoriert): {e}]", flush=True)
    # Hilfs-SSEs (z.B. list_changed) aus den Records ziehen, broadcasten und aus den
    # Records entfernen - der persistierte/action_result-Record traegt nur type/detail/ok/owner.
    # Gleichzeitig: merken ob der System-Prompt nach dem Repatch neu gebaut werden muss
    # (Notizen + aktive/erstellte Listen sind always-on Prompt-Schichten).
    prompt_dirty = False
    for rec in actions:
        if rec.get("type") == "note":
            prompt_dirty = True
        for ev in rec.pop("_sse", []) or []:
            if ev.get("kind") == "list_changed" and ev.get("reason") in ("active", "created"):
                prompt_dirty = True
            if ev.get("kind") in ("gedankenbild", "gedankenbild_unavailable"):
                ev["turn_id"] = turn_id                # Bubble-Korrelation im Frontend
                if target_client_id:
                    ev["target_client_id"] = target_client_id
            try:
                _broadcast_sse(ev)
            except Exception as e:
                print(f"  [Action-SSE Fehler (ignoriert): {e}]", flush=True)
    try:
        with LOCK:
            idx = _history_index_by_turn_id(turn_id)
            if idx is not None:
                HISTORY[idx].setdefault("meta", {})
                HISTORY[idx]["meta"]["actions"] = actions
                _stamp_gedankenbild(HISTORY[idx], actions)
                yc.save_history(HISTORY)
    except Exception as e:
        print(f"  [Action-Repatch Fehler (ignoriert): {e}]", flush=True)
    if prompt_dirty:
        try:
            _refresh_system_msg()
        except Exception as e:
            print(f"  [Action-Prompt-Refresh Fehler (ignoriert): {e}]", flush=True)
    ev = {"kind": "action_result", "turn_id": turn_id, "actions": actions}
    if target_client_id:
        ev["target_client_id"] = target_client_id
    _broadcast_sse(ev)


def _spawn_action_pipeline(turn_id, user_text, yuki_prosa, recent_turns, target_client_id,
                           allow_timer=True, note_source="michael"):
    threading.Thread(target=_run_action_pipeline,
                     args=(turn_id, user_text, yuki_prosa, recent_turns, target_client_id,
                           allow_timer, note_source),
                     daemon=True).start()


def _run_regenerate(turn_id, prompt, style):
    """Blocking (~60s im Thread): dieselbe Vision (Prompt+Stil) neu wuerfeln, an die
    Turn-Bildliste anhaengen, per SSE (an ALLE Geraete) nachliefern. ISOLIERT - ruft
    nur imagegen + save_gedankenbild + History-Append. KEIN add_to_gallery (Varianten
    pinnt der User von Hand)."""
    img = imagegen.generate(prompt, style=style or None)
    fname = None
    if img:
        path = yc.save_gedankenbild(img, prompt, prompt=prompt, style=style or "")
        fname = path.name if path else None
    if not fname:
        _broadcast_sse({"kind": "gedankenbild_unavailable", "turn_id": turn_id})
        return
    try:
        with LOCK:
            idx = _history_index_by_turn_id(turn_id)
            if idx is not None:
                HISTORY[idx].setdefault("gedankenbilder", []).append(fname)
                yc.save_history(HISTORY)
    except Exception as e:
        print(f"  [Regenerate-Repatch Fehler (ignoriert): {e}]", flush=True)
    # Volle Prompt als Caption (nicht prompt[:120]): die Galerie-Kachel clampt
    # visuell auf 2 Zeilen, Tap klappt den Rest auf. Das Slicing verwarf den
    # Rest sonst permanent -> abgeschnittener Text. Analog zu _exec_gedankenbild.
    _broadcast_sse({"kind": "gedankenbild", "turn_id": turn_id, "image": fname,
                    "caption": prompt, "can_regen": True})


def _spawn_regenerate(turn_id, prompt, style):
    threading.Thread(target=_run_regenerate, args=(turn_id, prompt, style),
                     daemon=True).start()


def _emit_actions_for_reply(reply_clean, trigger_text, *, target_client_id=None,
                            note_source="michael", allow_timer=True):
    """EIN Einstieg fuer alle Nicht-/respond-Reply-Pfade (Vision/Proaktiv/HA-Voice/
    Timer-fertig): vergibt der GERADE an HISTORY angehaengten Yuki-Antwort eine turn_id
    (vor save_history persistiert - Reload-Korrelation) und startet die async Action-
    Pipeline darueber. Kein Pfad-Spezialcode - Unterschiede kommen als Argumente rein.
    Bewusst KEIN Live-Spinner/Icon auf diesen Bubbles (kein turn_id im SSE) - die Aktion
    feuert + Reload zeigt die Icons aus meta['actions']. (Ausnahme: gedankenbild-Events
    tragen turn_id + werden am Turn persistiert, falls dieser Pfad das Tool je ausloest -
    Reload stellt das Bild dann korrekt wieder her.)

    Guard: Research-interne Personas (_research/_adventure/_dm) sind nie CURRENT_PERSONA
    und kommen hier nie an. Secretary laeuft jetzt durch die Action-Pipeline (gewuenscht):
    FORCE_RESEARCH_PERSONAS steuert den LLM-Routing-Pfad (generate_secretary_reply), nicht
    ob Actions ausgefuehrt werden. Kein Guard noetig."""
    with LOCK:
        if not HISTORY:
            return
        turn_id = _next_turn_id()
        HISTORY[-1]["turn_id"] = turn_id
        recent = HISTORY[-8:-2]
        yc.save_history(HISTORY)
    recent_txt = "\n".join(f"{m.get('role')}: {m.get('content','')}" for m in recent)
    _maybe_spawn_kyoto_translation(turn_id, reply_clean)
    _spawn_action_pipeline(turn_id, trigger_text, reply_clean, recent_txt, target_client_id,
                           allow_timer=allow_timer, note_source=note_source)


def _do_kyoto_translation(turn_id, jp_text):
    """Uebersetzt Yukis reinen JP-Reply ins Deutsche und haengt ihn als UI-Meta
    'translated' an den Turn (per turn_id, unter LOCK) - genau der Weg, den
    /history + addYukiReply beim Reload rendern. Zusaetzlich Live-Push per SSE
    'translated' an ALLE Clients (Untertitel ist visuell, gehoert auf jedes Geraet
    das den Turn zeigt). Laeuft im Daemon-Thread (siehe _spawn_kyoto_translation),
    darum HISTORY[-1] NIE direkt - immer per turn_id."""
    de = yc.translate_kyoto_reply(jp_text)
    if not de:
        return
    with LOCK:
        idx = _history_index_by_turn_id(turn_id)
        if idx is None:
            return
        HISTORY[idx]["translated"] = de
        yc.save_history(HISTORY)
    _broadcast_sse({"kind": "translated", "turn_id": turn_id, "text": de})


def _spawn_kyoto_translation(turn_id, jp_text):
    threading.Thread(target=_do_kyoto_translation, args=(turn_id, jp_text),
                     daemon=True).start()


def _maybe_spawn_kyoto_translation(turn_id, reply_clean):
    """Hook fuer JEDEN finalisierten Kyoto-Reply (Companion + Research-in-kyoto +
    Vision/Proaktiv/Timer). Aufrufer hat turn_id bereits vergeben + persistiert.
    No-op ausserhalb der Kyoto-Persona."""
    if CURRENT_PERSONA == "kyoto":
        _spawn_kyoto_translation(turn_id, reply_clean)


def _proactive_reset_clock():
    """Cooldown der proaktiven Yuki neu starten (nach jeder Aktivitaet - User-Reply,
    Vision-Trigger, Timer-Ablauf oder ihre eigene Spontan-Aussage). Naechstes Feuern
    irgendwann zwischen min_sec und max_sec, gewuerfelt. Wird ignoriert wenn Feature
    aus ist - kostet sonst nichts."""
    import random
    now = time.time()
    _proactive["last_activity"] = now
    _proactive["next_at"] = now + random.uniform(
        _proactive["min_sec"], _proactive["max_sec"])


def _proactive_init_clock():
    """Wie _proactive_reset_clock, aber beim Server-Start aufgerufen. last_activity
    NICHT auf now - HISTORY wurde gerade aus conversation.json geladen und der letzte
    Turn dort kann beliebig alt sein (Stunden, Tage). Wenn wir hier now setzten, wuerde
    der erste Spontan-Loop den continue-Pool nehmen und an einem stale Faden anknuepfen
    (Yuki sagt 'denke noch an deine Frage von vorhin' obwohl 'vorhin' gestern war).
    Mit last_activity=0 garantiert die Pool-Wahl in _fire_proactive_once den fresh-Pool
    bis zur ersten echten Aktivitaet."""
    import random
    now = time.time()
    _proactive["last_activity"] = 0.0
    _proactive["next_at"] = now + random.uniform(
        _proactive["min_sec"], _proactive["max_sec"])


def _refresh_system_msg():
    """System-Prompt neu bauen (z.B. nach Notes-Aenderung oder Memory-Update).
    LOCK noetig, weil HISTORY/SYSTEM_MSG mit Request-Pfad geteilt sind."""
    global SYSTEM_MSG
    with LOCK:
        SYSTEM_MSG = yc.build_system_msg(MEMORY, CURRENT_PERSONA)


def _apply_persona_change(key):
    """Persona-Wechsel mitten in einem Turn (vom Marker oder UI). Resettet Mood,
    rebuildet SYSTEM_MSG. Wird NICHT persistiert wenn vom Marker - das ist
    User-Choice via UI. Caller muss LOCK halten (RLock-reentrant)."""
    global CURRENT_PERSONA, SYSTEM_MSG
    if key not in dict(yc.persona_list()):
        return False
    CURRENT_PERSONA = key
    yc.clear_drawing_wip()                            # Phase B: neue Persona startet mit blanker Leinwand
    SYSTEM_MSG = yc.build_system_msg(MEMORY, CURRENT_PERSONA)
    yc.reset_mood()                                   # sauberer Schnitt wie bei UI-Switch
    return True


def _finalize_assistant_history(reply_raw, translation, tokens=None, furigana=None, drawing=None):
    """*** Single-Point-of-Truth fuer den letzten HISTORY-Eintrag nach Yuki-Reply.
        Hier wird das Schema {role, content [, translated] [, tokens] [, furigana]}
        festgezogen. ***

    Was passiert konkret:
      1) content := sanitize_reply_for_history(reply_raw)
         -> entfernt UNGUELTIGE Mood-Marker (Yukis Tippfehler / erfundene Moods)
         -> entfernt den [de:KYOTO-Uebersetzung]-Marker komplett
         -> gueltige Marker (mood/timer/note/furigana/...) BLEIBEN als Pattern
            fuer Folgeturns (Yuki sieht sie und schreibt deshalb das naechste
            Mal wieder im gleichen Format).
      2) translated := optional gesetzt (Kyoto-DE-Untertitel) - UI-Meta-Feld,
         landet beim Reload im UI als gedaempfter Subtitle in der Yuki-Bubble.
      3) tokens := optional gesetzt (fugashi-Morphem-Liste, surface/lemma/
         reading/pos pro JP-Token) - UI-Meta-Feld, das Frontend wrappt damit
         die JP-Worte als clickbare Spans fuer den Wadoku-Gloss-Popup.
      4) furigana := optional gesetzt (Liste {jp, ruby} pro [furigana:...]-
         Marker im Reply) - UI-Meta-Feld, das Frontend rendert damit Ruby-
         Annotation (Lesung ueber Kanji) auf den passenden JP-Substrings.

    Alle Meta-Felder werden in conversation.json mit-persistiert und vom
    /history-Endpoint mit-ausgeliefert - aber NIE von Yuki gesehen, weil
    yuki_core.build_messages explizit nur role+content an Ollama uebergibt
    (siehe dortigen Kommentar). Das ist der Kern des Patterns "UI-only
    History-Annotation".

    Wird nach _handle_marker_side_effects in ALLEN Reply-Pfaden aufgerufen
    (/respond, /see, timer-done, auto-vision, proactive). Bei _react_to_perception/
    look_and_react/react_to_sight schreiben die Funktionen vorher rohen Reply in
    HISTORY - dieser Helper macht sie konsistent. Bei /respond wird der Append
    direkt davor selbst gemacht (auch roh, weil dieser Helper sanitized).

    Wer hier neue UI-Meta-Felder hinzufuegt:
      * AUCH in build_messages explizit filtern (sonst leakt es zu Ollama),
      * AUCH in /history mit ausliefern (sonst fehlt's beim Browser-Reload),
      * AUCH bei pop()-Branch wegnehmen wenn der neue Wert leer ist."""
    if not HISTORY or HISTORY[-1].get("role") != "assistant":
        return
    HISTORY[-1]["content"] = yc.sanitize_reply_for_history(reply_raw)
    if translation:
        HISTORY[-1]["translated"] = translation
    else:
        HISTORY[-1].pop("translated", None)
    if tokens:
        HISTORY[-1]["tokens"] = tokens
    else:
        HISTORY[-1].pop("tokens", None)
    if furigana:
        HISTORY[-1]["furigana"] = furigana
    else:
        HISTORY[-1].pop("furigana", None)
    # drawing (Kuenstlerin): sanitisiertes SVG-Doodle. UI-Meta wie translated/
    # furigana - build_messages reicht nur role+content an Ollama, das SVG sieht
    # Yuki in past turns NIE (es ist als Text fuer sie ohnehin wertlos). /history
    # liefert es mit, der Reload re-rendert das <img>.
    if drawing:
        HISTORY[-1]["drawing"] = drawing
    else:
        HISTORY[-1].pop("drawing", None)
    # Modell-Woelkchen (oben-links an der Bubble): welches LLM diesen Reply
    # generiert hat. Quelle _LAST_REPLY_LLM_STATS (in chat_ollama nur fuer
    # reply/research/secretary gesetzt -> failover-sicher, NICHT von Background-
    # Gates ueberschrieben); Fallback aktiver Server. Zentral HIER, weil ALLE
    # autonomen Reply-Pfade (Vision /see, Auto-Vision, Timer, Spontan/Steward)
    # vorher generate_reply (= purpose="reply") gerufen haben und dann hier
    # durchlaufen - so tragen auch sie das Woelkchen, nicht nur /respond.
    # /respond stempelt es zusaetzlich selbst (deckt den Research-Pfad ab, der
    # NICHT durch diesen Helper laeuft, + ueberlebt das Sekretaerin-meta-Overwrite).
    reply_model = (yc.get_last_reply_llm_stats() or {}).get("model") or yc.OLLAMA_MODEL
    if reply_model:
        _mm = HISTORY[-1].get("meta") or {}
        _mm["model"] = reply_model
        HISTORY[-1]["meta"] = _mm


# Action-Marker mit Side-Effect aber ohne sichtbare UI-Folge im Reply selbst:
# heart speichert ins Herz, note ins Notes-Panel, timer startet einen Background-
# Timer, event ins CalDAV, keepsake ins Album. Anders als mood/furigana/quiz/
# gesture/calc/conjugate aendern sie den Reply-Text nicht - der User koennte
# sonst gar nicht sehen, dass Yuki etwas getan hat. Frontend rendert pro Action
# ein kleines Icon an der unteren Bubble-Kante (Toggle im Options-Modal).
#
# Quelle ist immer der UNGESTRIPPTE Text: bei Live-Replies der reply-String aus
# generate_reply, beim /history-Reload der HISTORY-Content (sanitize_reply_for_history
# laesst gueltige Marker bewusst drin - Pattern-Reinforcement, siehe dortige Doku).
# Wir muessen die action-Liste also weder in HISTORY persistieren noch durch
# _handle_marker_side_effects threaden - der Roh-Text trägt sie schon.
# note/event/list* sind in den async Action-Decider gewandert - kein Icon mehr noetig.
_ACTION_MARKER_RE = re.compile(r"\[(keepsake)\s*:", re.IGNORECASE)


def _action_detail(name, raw_text):
    """Menschenlesbarer Inhalt eines Side-Effect-Markers fuer das Klick-Overlay
    (was Yuki konkret eingetragen hat). Nutzt die SIDE-EFFECT-FREIEN extract_*-
    Parser - gleiche Normalisierung wie der echte Eintrag, also kein Drift. Bei
    leerem Resultat / Parse-Fehler '' -> Overlay zeigt dann nur den Typ-Titel."""
    try:
        if name == "note":
            txt, _ = yc.extract_note_marker(raw_text)
            _, txt = yc.split_note_source(txt or "")   # ggf. 'yuki|'-Prefix raus
            return txt or ""
        if name == "timer":
            info, _ = yc.extract_timer_marker(raw_text)
            if not info:
                return ""
            mins, secs = divmod(int(info["sec"]), 60)
            if mins and secs:
                dur = f"{mins} Min {secs} Sek"
            elif mins:
                dur = f"{mins} Min"
            else:
                dur = f"{secs} Sek"
            return f"{info['label']} · {dur}"
        if name == "event":
            info, _ = yc.extract_event_marker(raw_text)
            if not info:
                return ""
            return f"{info['title']} · {info['start'].strftime('%d.%m.%Y %H:%M')}"
        if name == "keepsake":
            reason, _ = yc.extract_keepsake_marker(raw_text)
            return reason or ""
        if name == "list":
            specs, _ = yc.extract_list_markers(raw_text)
            if not specs:
                return ""
            title, _kind, items = specs[0]
            n = len(items)
            return title if not n else f"{title} · {n} " + ("Eintrag" if n == 1 else "Einträge")
        if name == "routine":
            f = yc.parse_routine_marker_fields(raw_text)   # side-effect-frei, legt NICHTS an
            if not f:
                return ""
            label, rec, band, due = f
            return f"{label} · {yc.routine_when_label(rec, band, due)}"
        if name == "routine_done":
            return yc.parse_routine_done_label(raw_text) or ""
        if name == "ha":
            info, _ = yc.extract_ha_marker(raw_text)
            if not info:
                return ""
            # Nur wenn das Geraet auflösbar ist, gibt es einen Namen -> sonst ""
            # (Aufrufer _detect_actions unterdrueckt dann das Icon).
            label = yc.ha_device_name(info["target"])
            if not label:
                return ""
            if info["action"] == "set":
                return f"{label} → {info.get('value', '?')}"
            act = {"on": "an", "off": "aus", "toggle": "umschalten"}.get(
                info["action"], info["action"])
            return f"{label} → {act}"
    except Exception:
        return ""
    return ""


def _detect_actions(raw_text, note_source="michael"):
    """Liefert die Liste aller Side-Effect-Marker als Dicts {type, detail} (Dedup
    nach type, Reihenfolge des ersten Vorkommens). detail = der konkrete Inhalt
    (Timer-Label+Dauer, Notiz-Text, Termin, Heart, Album-Grund) fuer das Klick-
    Overlay im Frontend. Leerer Text / kein Match -> []. Reihenfolge ist stabil,
    damit das Frontend-Icon-Layout pro Bubble konsistent aussieht.

    note_source = Pfad-Default fuer den Notiz-Besitzer ('michael' im normalen Chat /
    Timer / Foto, 'yuki' in den autonomen Pfaden). MUSS mit dem note_source
    uebereinstimmen, den _handle_marker_side_effects im selben Pfad nutzt, sonst
    weicht das Bubble-Icon vom echten Speicherort ab. Ein expliziter Marker-Prefix
    [note:yuki|...] / [note:michael|...] gewinnt gegen den Pfad-Default. Das
    note-Action-Dict bekommt dann ein 'owner'-Feld ('michael'/'yuki'), das Frontend
    rendert daraus ein eigenes Icon. (Hinweis: der /history-Reload kennt den Pfad-
    Default nicht und nimmt 'michael' an - eine autonome Yuki-Notiz OHNE Prefix
    zeigt dort 'michael'; mit Prefix bleibt sie korrekt. Live ist alles korrekt.)"""
    if not raw_text:
        return []
    seen = set()
    out = []
    for m in _ACTION_MARKER_RE.finditer(raw_text):
        name = m.group(1).lower()
        if name in seen:
            continue
        seen.add(name)
        entry = {"type": name, "detail": _action_detail(name, raw_text)}
        if name == "ha" and not entry["detail"]:
            # HA-Detail ist "" wenn das Geraet (Name/entity_id) nicht in der
            # Allowlist aufloesbar war -> es wurde NICHTS geschaltet (z.B. Yuki hat
            # die entity_id halluziniert). Dann KEIN Icon: ein "geschaltet"-Icon
            # ueber einem fehlgeschlagenen Schaltvorgang waere eine Luege.
            continue
        if name == "note":
            raw_note, _ = yc.extract_note_marker(raw_text)
            src_override, _ = yc.split_note_source(raw_note or "")
            owner = (src_override or note_source or "michael").strip().lower()
            entry["owner"] = "yuki" if owner == "yuki" else "michael"
        out.append(entry)
    return out


def _reply_action_icons(reply, research_mode=False):
    """Icon-Liste an der Bubble-Kante aus dem ROHEN Reply (Side-Effect-Marker +
    lookat). Im Research-Turn leer: dort laeuft weder _handle_marker_side_effects
    noch der Kamera-Schwenk, also darf auch KEIN Icon einen Seiteneffekt behaupten,
    der nie passiert ist (Befund Research.L1). _resolve_lookat ist erst weiter unten
    definiert - Modul-Level, zur Aufrufzeit aufgeloest."""
    if research_mode:
        return []
    actions = _detect_actions(reply)
    la = _resolve_lookat(reply)
    if la:
        actions.append({"type": "look", "detail": la[2]})
    return actions


def _enrich_tokens_with_ruby(tokens, text, furigana_specs):
    """Wadoku-Tokens optional mit 'ruby'-Feld anreichern wenn sie in einer
    [furigana:...]-Range liegen. Pure-Funktion ueber bereits tokenisiertem Input -
    wird sowohl von _tokens_for (Live-Reply-Pfade) als auch von /history
    (Backfill alter Entries vor dem Per-Token-Enrich) genutzt.

    Returns: neue Liste mit kopierten dicts fuer enrichted Tokens (in-range +
    Ruby vorhanden) und Original-Refs fuer alle anderen. Reihenfolge bleibt."""
    if not tokens or not furigana_specs:
        return tokens
    ranges = []
    cur = 0
    for fg in furigana_specs:
        jp = fg.get("jp") if isinstance(fg, dict) else None
        if not jp:
            continue
        idx = text.find(jp, cur)
        if idx < 0:
            continue
        ranges.append((idx, idx + len(jp)))
        cur = idx + len(jp)
    if not ranges:
        return tokens
    enriched = []
    text_cursor = 0
    for tok in tokens:
        surface = tok.get("surface") if isinstance(tok, dict) else None
        if not surface:
            enriched.append(tok)
            continue
        pos = text.find(surface, text_cursor)
        if pos < 0:
            enriched.append(tok)
            continue
        in_range = any(start <= pos and pos + len(surface) <= end
                       for start, end in ranges)
        if in_range:
            ruby = yc.ruby_pairs_for_token(surface, tok.get("reading"))
            if ruby:
                new_tok = dict(tok)
                new_tok["ruby"] = ruby
                enriched.append(new_tok)
            else:
                enriched.append(tok)
        else:
            enriched.append(tok)
        text_cursor = pos + len(surface)
    return enriched


def _tokens_for(reply_clean, furigana_specs=None):
    """Helfer: tokenize_jp aufrufen, leere Liste bei abgeschaltetem Wadoku.
    Dadurch koennen alle Reply-Pfade ohne if-Check an die Tokens kommen.

    furigana_specs (optional): Liste {jp, ruby} aus _handle_marker_side_effects.
    Wenn gegeben, werden Tokens innerhalb der Furigana-Ranges via
    _enrich_tokens_with_ruby angereichert. Damit sind die JP-Tokens innerhalb
    von [furigana:...]-Markern gleichzeitig clickbar (.jp-tok -> Wadoku-Popup)
    UND annotiert (Ruby-Lesung darueber im Browser)."""
    if not wadoku.is_available():
        return []
    tokens = wadoku.tokenize_jp(reply_clean)
    return _enrich_tokens_with_ruby(tokens, reply_clean, furigana_specs)


# Match: einer der Satzenden + (Leerraum ODER String-Ende). [\s\S] = beliebige Zeichen
# inkl. Newlines (re.DOTALL-Wirkung kompakt).
_SENTENCE_END_RE = re.compile(r"^([\s\S]*?[.!?。！？])(?:\s|$)")


def _first_sentence(text):
    """Erstes Satz-Ende suchen (DE/EN Punkt/!/? + JP-Vollformen 。！？). Wenn keins
    drin: ganzen Text zurueck. Wird im Research-Modus genutzt damit Yuki live nur
    den ersten Satz vorliest - den Rest holt sich der User per 🔊-Button im
    Modal-Overlay. Frontend hat parallel _firstSentence (gleiche Regex-Idee) fuer
    die Bubble-Collapse-Logik - beide muessen synchron bleiben."""
    if not text:
        return ""
    m = _SENTENCE_END_RE.match(text)
    return (m.group(1) if m else text).strip()


# Schwelle ab der die Research-Bubble im Frontend collapsed wird (= 🔍-Modal-
# Button bekommt). MUSS mit RESEARCH_COLLAPSE_THRESHOLD in web/index.html
# uebereinstimmen. Unter der Schwelle: Frontend zeigt die volle Bubble ohne
# Modal -> es gaebe keinen 🔊-Button um den Rest zu hoeren -> wir lesen den
# Volltext vor statt nur den ersten Satz.
_RESEARCH_FULL_TTS_BELOW = 400


def _research_tts_text(reply_clean):
    """TTS-Text fuer einen Research-Reply: bei kurzer Antwort der ganze Text
    (passt zur ungekuerzten Bubble im UI), bei langer Antwort nur der erste
    Satz - Rest landet im Modal mit eigenem 🔊-Button."""
    if len(reply_clean) <= _RESEARCH_FULL_TTS_BELOW:
        return reply_clean
    return _first_sentence(reply_clean)



def _handle_marker_side_effects(reply_text, target_client_id=None, note_source="michael"):
    """Nach generate_reply die Reply-Marker auswerten (mood + timer + note + ...).
    Liefert (reply_clean, translation) zurueck: reply_clean ist der um alle Marker
    bereinigte Text, der ans Display/TTS geht; translation ist die deutsche
    Untertitel-Zeile aus dem [de:...]-Marker (Kyoto-Persona) oder None.
    Mood wird sofort persistiert; [timer:]-Marker werden nur noch via
    yc.strip_timer_markers rausgestrippt - das Starten des Timers ist seit Stufe C
    in den async Action-Decider gewandert (der yc.start_timer ruft + den nativen
    Wecker via timer_started-SSE plant); Note wird mit active=True angelegt und
    der System-Prompt rebuildet (Yuki sieht sie sofort beim naechsten Turn).
    History behaelt den Original-Reply MIT Markern (Pattern-Konsistenz) - der
    [de:...]-Marker wird allerdings VOR History.append via sanitize_reply_for_history
    rausgestrippt, damit Yuki ihre eigene Uebersetzung in past turns nie sieht.

    target_client_id (Origin-Routing 2026-06-04): wird fuer Signatur-Kompatibilitaet
    mitgefuehrt; innerhalb dieser Funktion wird es aktuell nicht verwendet (seit Stufe C
    gibt es keinen Timer-Start mehr hier, der es braeuchte). Default None."""
    # Calc-Marker zuerst expandieren: ersetzt [calc:EXPR] inline durch das sympy-
    # Resultat. Alle nachfolgenden Marker-Extracts arbeiten dann auf dem expandierten
    # Text. HISTORY bekommt sowieso den RAW-Reply (mit Markern) - dort expandiert
    # sanitize_reply_for_history nicht; die Resultate werden bei /history erneut
    # via expand_calc_markers berechnet, sodass der Reload das gleiche zeigt wie
    # der Live-Reply. Pattern-Reinforcement: Yuki sieht in past turns ihre eigenen
    # [calc:...]-Marker und schreibt das Pattern wieder.
    reply_text, n_calc = yc.expand_calc_markers(reply_text)
    if n_calc:
        print(f"  [🧮 Mathe: {n_calc} Ausdruecke berechnet]", flush=True)
    # Conjugate-Marker analog: [conjugate:VERB] / [conjugate:VERB|FORM] inline
    # ersetzen. Determinstische JP-Verb-Konjugation via fugashi-Klassifikation +
    # Regel-Tabellen (Godan/Ichidan/Suru/Kuru); 行く-Ausnahme inkl.
    reply_text, n_conj = yc.expand_conjugate_markers(reply_text)
    if n_conj:
        print(f"  [🇯🇵 Konjugation: {n_conj} Verben]", flush=True)
    mood_set, t = yc.extract_mood_marker(reply_text)
    yc.save_mood(mood_set)               # auch None - cleared file
    # [timer:...] wandert in den async Action-Decider (Stufe C) - nur strippen, NICHT
    # starten (sonst Doppel-Feuer mit dem Executor, der jetzt yc.start_timer ruft). Der
    # native Wecker wird jetzt vom timer_started-SSE des Deciders geplant (Origin-Umkehr
    # im Frontend), nicht mehr aus der HTTP-Antwort. Die "Timer auf Watch/Voice-PE
    # unterdruecken"-Regel (frueher allow_timer=False hier) sitzt jetzt beim Decider-
    # Executor (_spawn_action_pipeline(allow_timer=...) -> _exec_timer), wo der Timer
    # tatsaechlich entsteht.
    t = yc.strip_timer_markers(t)
    # [event:...] -> async Decider. Nur strippen.
    _ev, t = yc.extract_event_marker(t)
    # [ha:...] wandert in den async Action-Decider (Pilot) - hier nur noch strippen,
    # NICHT ausfuehren (sonst Doppel-Feuer mit dem Decider). extract_ha_marker ist
    # side-effect-frei (die Ausfuehrung sass frueher hier in server.py).
    _ha_info, t = yc.extract_ha_marker(t)
    # [note:...] wandert in den async Action-Decider - nur strippen, nicht ausführen.
    _note_txt, t = yc.extract_note_marker(t)
    # [list:...] -> async Decider. Nur strippen.
    _ls, t = yc.extract_list_markers(t)
    # [list_activate:...] -> async Decider. Nur strippen.
    _la, t = yc.extract_list_activate_marker(t)
    # [list_check:...] -> async Decider. Nur strippen.
    _lc, t = yc.extract_list_check_markers(t)
    # Vocab-Marker (Tutor): kann mehrere pro Reply liefern - alle adden, dann
    # ein einziges _refresh_system_msg fuer den re-sampleten RECENT VOCAB-Block.
    # Auto-Vocab-Extract (JP("EN")-Pattern) laeuft SPAETER unten - nach Furigana-
    # Strip, damit [furigana:走る] ("to run") nicht das JP im Klammer-Marker
    # versteckt.
    vocab_entries, t = yc.extract_vocab_marker(t)
    seen_vocab_keys = set()                              # Cross-Pfad-Dedup Marker+Auto
    vocab_added_marker = False
    for v in vocab_entries:
        entry = yc.add_vocab(v["jp"], v["de"], v.get("example"), source="marker")
        seen_vocab_keys.add(v["jp"].casefold())
        if entry:
            ex = f" (Bsp: {v['example']})" if v.get("example") else ""
            print(f"  [📚 Vokabel: {v['jp']} = {v['de']}{ex}]", flush=True)
            vocab_added_marker = True
        else:
            print(f"  [📚 Vokabel-Dup/leer ignoriert: {v['jp']} = {v['de']}]", flush=True)
    if vocab_added_marker:
        _refresh_system_msg()            # neuer Eintrag - Pool im Prompt rotieren
    # Quiz-Marker (Tutor): SSE-Event ans UI - keine Daten-Persistenz, der
    # eigentliche Quiz steht im Reply-Text. Banner-Effekt.
    quiz_n, t = yc.extract_quiz_marker(t)
    if quiz_n:
        print(f"  [🎓 Quiz gestartet: {quiz_n} Fragen]", flush=True)
        _broadcast_sse({"kind": "quiz_start", "n": quiz_n})
    # SRS-Marker (optional Fast-Path, 2026-06-06): Yuki kann nach einem Quiz-Item
    # explizit [srs:ID|GRADE] schreiben. Im Compress-Gate hat das Vorrang
    # (last_review_at >= last_seen Filter). Mehrere pro Reply erlaubt.
    srs_marks, t = yc.extract_srs_markers(t)
    for s in srs_marks:
        graded = yc.vocab_grade(s["id"], s["signal"])
        if graded:
            print(f"  [🧠 SRS-Marker: {s['id']} -> {s['signal']} (next due {graded['due_at']})]",
                  flush=True)
        else:
            print(f"  [🧠 SRS-Marker ignoriert (unbekannte ID): {s['id']}]", flush=True)
    # Gesture-Marker: one-shot Body-Geste auf dem Avatar. extract_gesture_marker
    # validiert gegen yc.GESTURE_KEYS (unbekannte Keys werden ignoriert -
    # Marker bleibt im Text, faellt im finalen strip_all_markers raus).
    # Frontend mappt key -> Filename und pickt aus dem reactive-Bucket.
    gesture_key, gesture_pos, t = yc.extract_gesture_marker(t)
    if gesture_key:
        print(f"  [👋 Geste: {gesture_key} @ {gesture_pos:.0%}]", flush=True)
        _broadcast_sse({"kind": "gesture", "key": gesture_key, "position": gesture_pos})
    # [affinity:...] entfernt (Stufe D, 2026-07-14): der Marker-Fast-Path faellt weg.
    # Das 30-Turn-Konsolidierungs-Gate (apply_affinity_consolidation) pflegt
    # Affinitaeten weiter. Marker wird nur noch gestripped, nie mehr angewandt.
    t = yc._AFFINITY_MARKER_RE.sub("", t).strip() if "[affinity:" in t.lower() else t
    # [heart:...] entfernt (Stufe D): heart waechst jetzt allein ueber das Per-Turn-Gate
    # (maybe_archive_heart) + die auto-direkte Frequenz-Promotion (Task 4). Kein Marker mehr.
    t = yc._HEART_MARKER_RE.sub("", t).strip() if "[heart:" in t.lower() else t
    # Routine-Marker (#30): wiederkehrender stiller Vorsatz (Medizin/Zaehne/Stream).
    # [routine:...] (Create) wandert in den async Action-Decider (Stufe B) - nur strippen,
    # NICHT anlegen (sonst Doppel-Feuer mit dem Executor, der jetzt create_routine ruft).
    t = yc.strip_routine_markers(t)
    # [routine_done:...] wandert in den async Action-Decider (Pilot) - nur strippen,
    # NICHT ausfuehren. strip_routine_done_markers ist side-effect-frei (extract_
    # routine_done_marker wuerde die Routine intern abhaken -> Doppel-Feuer).
    t = yc.strip_routine_done_markers(t)
    # [persona:...] entfernt (Stufe D): Yuki wechselt die Persona nicht mehr autonom
    # (nie sinnvoll genutzt). Manueller Wechsel (Dropdown/Hotkey -> /persona) bleibt.
    t = yc._PERSONA_MARKER_RE.sub("", t).strip() if "[persona:" in t.lower() else t
    # Expect-Lang-Marker (Tutor): Yuki zwingt die STT-Sprache fuer den NAECHSTEN
    # User-Turn (z.B. "sag das auf Japanisch" -> [expect_lang:ja] am Reply-Ende).
    # Wir broadcasten den Lock als SSE - Frontend ueberschreibt sein Dropdown nur
    # fuer EIN Send (das Senden setzt es danach auf den Session-Default zurueck).
    # Origin-Routing: bei /respond kommt target_client_id mit, nur das triggernde
    # Geraet schaltet um (sonst wuerde ein PC ein Handy fernsteuern). Andere SSE-
    # Kontexte ohne target_client_id (Vision/Timer) broadcasten an alle - dort
    # ist die Annahme dass auch nur ein Geraet aktiv hoert.
    expect_lang, t = yc.extract_expect_lang_marker(t)
    # [expect_word:WORT] immer strippen (sonst im UI sichtbar), auch ohne Lang-Lock.
    expect_word_marker, t = yc.extract_expect_word_marker(t)
    if expect_lang:
        # Bei einem JP-Drill das Zielwort bestimmen (expliziter Marker > Reply-
        # Token-Heuristik) und mit dem Lock mitschicken - das Frontend haengt es
        # an den naechsten /stt-Send, damit der Server den Kana-Diff fahren kann.
        drill_word = (yc.derive_drill_target(t, explicit_word=expect_word_marker)
                      if expect_lang == "ja" else None)
        print(f"  [🎙️ STT-Lang-Lock: {expect_lang}"
              + (f" word={drill_word}" if drill_word else "") + "]", flush=True)
        _broadcast_sse({"kind": "stt_lang_lock", "code": expect_lang,
                        "word": drill_word,
                        "target_client_id": target_client_id})
    # Translate-Marker (Kyoto): am ENDE auswerten, damit alle anderen Marker-Strips
    # davor laufen. Liefert die deutsche Untertitel-Zeile fuer die UI; HISTORY und
    # TTS bekommen dadurch reines Japanisch (Marker faellt in beide raus).
    # Die zurueckgegebene 'translation' wird vom Caller in _finalize_assistant_history
    # als UI-Meta-Feld 'translated' im HISTORY-Entry abgelegt - Yuki sieht sie
    # in past turns NIE wieder (build_messages filtert; siehe dortigen Kommentar).
    translation, t = yc.extract_translate_marker(t)
    if translation:
        print(f"  [🇩🇪 Untertitel: {translation[:80]}]", flush=True)
    # Furigana-Marker (Tutor): Klammer raus, JP-Inhalt bleibt im Text - dadurch
    # laufen Romaji-Annotation + Tokenisierung + TTS auf dem reinen JP weiter.
    # Die furigana_specs landen als UI-Meta in HISTORY/Response - Frontend
    # rendert daraus <ruby>-Annotation (Lesung ueber Kanji).
    furigana_specs, t = yc.extract_furigana_markers(t)
    if furigana_specs:
        print(f"  [🇯🇵 Furigana: {len(furigana_specs)}x ({furigana_specs[0]['jp'][:24]}...)]", flush=True)
    # Vocab-Auto-Extract (Tutor): JP-Spans mit ("EN")-Gloss aus dem Reply-Text
    # einfangen, damit auch Worte landen die Yuki nur beilaeufig glossiert (kein
    # expliziter [vocab:JP|DE]-Marker). LAEUFT HIER, nicht oben beim vocab_marker:
    # erst nach extract_furigana_markers sind [furigana:JP]-Klammern raus und
    # das JP frei matchbar fuer das Auto-Pattern. Nur in Tutor-Persona aktiv
    # (andere Personas lehren keine Sprache). Cross-Pfad-Dedup gegen den
    # Marker-Pfad oben via seen_vocab_keys (gleiche Funktion, lokale Variable).
    if CURRENT_PERSONA == "tutor":
        auto_pairs = yc.extract_vocab_auto_pairs(t)
        vocab_added_auto = False
        for v in auto_pairs:
            if v["jp"].casefold() in seen_vocab_keys:
                continue                                # schon via Marker erfasst
            entry = yc.add_vocab(v["jp"], v["de"], source="auto")
            if entry:
                print(f"  [📚 Vokabel-Auto: {v['jp']} = {v['de']}]", flush=True)
                vocab_added_auto = True
        if vocab_added_auto:
            _refresh_system_msg()
    # Erfundene Marker entfernen: Pacing-Cues ([pause:1s]/[beat]) UND bare Regie-/
    # Emotions-/Gesten-Cues ([smile]/[nod_yes]/[thoughtful]). Das sind KEINE echten
    # Marker (kein Side-Effect), die das LLM frei einstreut - ungestrippt leakten sie
    # roh in Display + TTS und reinforcten sich ueber die History. Vor tidy, damit die
    # entstehende Luecke gleich mit eingesammelt wird. Echte keyword:wert-Marker sind
    # hier ohnehin schon extrahiert/gestrippt.
    # [lookat:N] aus dem Anzeige-/TTS-Text strippen (Side-Effect laeuft separat via
    # _maybe_trigger_lookat). Hier noetig, weil dieser Live-Pfad NICHT ueber
    # strip_all_markers geht - sonst leakt der Marker roh in die erste Bubble (erst
    # der /history-Reload haette ihn entfernt).
    t = yc.strip_lookat_markers(t)
    t = yc.strip_invented_markers(t)
    # Display-Hygiene: alle bekannten Marker sind raus -> fuehrende/abschliessende
    # Leerzeichen + Marker-Leerzeilen einsammeln (z.B. "[gesture:x] Text" am Zeilen-
    # anfang -> " Text"). Deckt alle Caller ab (/see, Loop, Spontan); /respond tidyt
    # nach seinen Extra-Strips nochmal idempotent nach.
    return yc.tidy_reply_text(t), translation, furigana_specs


def _handle_keepsake_marker(reply_text, image_bytes, saw, source, archive=True):
    """Keepsake-Marker auswerten - umgeht das automatische qwen3-Gate
    (maybe_archive_keepsake). Nur in /see und auto_vision aufrufen, denn nur dort
    gibt es image_bytes. Liefert (reply_clean, archived_bool).

    archive=False: der Marker wird weiterhin aus dem Text gestrippt (kein Leak ins
    UI), aber NICHT als Keepsake gespeichert. Fuer den Fall 'Gedankenbild zeigen',
    wo das Bild schon als Gedankenbild in der Galerie liegt und keine redundante
    Keepsake-Kopie entstehen soll."""
    reason, cleaned = yc.extract_keepsake_marker(reply_text)
    if not reason:
        return reply_text, False
    if not archive:
        return cleaned, False                          # gestrippt, aber bewusst nicht gespeichert
    if not image_bytes:
        return cleaned, False                          # marker da, aber kein Bild zum Speichern
    try:
        jpg = yc.save_keepsake(image_bytes, saw, cleaned, reason, source=source)
        if jpg:
            print(f"  [💾 ins Album (Marker): {reason}  ({jpg.name})]", flush=True)
            return cleaned, True
    except Exception as e:
        print(f"  [Keepsake-Marker Fehler: {e}]", flush=True)
    return cleaned, False


def _on_timer_done(info):
    """Timer-Callback aus yuki_core: laesst Yuki kurz auf das Ablaufen reagieren
    (perception-Turn analog zu Auto-Vision) und pusht das Ergebnis als SSE-Event
    an alle Browser-Tabs. Laeuft im Threading.Timer-Thread - LOCK nehmen ist wichtig,
    weil HISTORY und SYSTEM_MSG mit dem Haupt-Request-Pfad geteilt sind.

    Origin-Routing 2026-06-04: target_client_id (vom Timer-Entry mitgefuehrt)
    landet im SSE-Payload. Andere Geraete zeigen den Reply still im Chat ohne
    Banner/Beeps/Voice - der Wecker toent nur am Ursprungs-Geraet."""
    tid = info.get("id", "")
    label = info.get("label", "Timer")
    sec = int(info.get("duration_sec", 0))
    target_client_id = info.get("target_client_id")
    mins, secs = divmod(sec, 60)
    dur_h = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
    # Wahrnehmung als User-Turn formuliert (englisch wie alle System-Annotations -
    # Yuki antwortet dann in ihrer Persona-Sprache).
    perception = f"[Timer '{label}' just finished ({dur_h}).]"
    with LOCK:
        try:
            reply = yc._react_to_perception(
                HISTORY, SYSTEM_MSG, yc.persona_fewshot(CURRENT_PERSONA),
                perception, yc.persona_reminder(CURRENT_PERSONA),
                persist_meta={"persona": CURRENT_PERSONA, "mood": yc.load_mood()})
        except Exception as e:
            print(f"  [Timer-LLM-Fehler: {e}]")
            return
        reply_clean, translation, furigana = _handle_marker_side_effects(reply)
        tokens = _tokens_for(reply_clean, furigana)
        _finalize_assistant_history(reply, translation, tokens, furigana)
        actions = _detect_actions(reply)
        display = yc.annotate_romaji(reply_clean)
        yc.save_history(HISTORY)
        persona_snapshot = CURRENT_PERSONA
        model_snapshot = (HISTORY[-1].get("meta") or {}).get("model")
        _emit_actions_for_reply(reply_clean, perception,
                                target_client_id=info.get("target_client_id"),
                                note_source="michael", allow_timer=True)
    print(f"  [⏰ Timer-Reaktion: {label} -> {reply_clean[:80]}]")
    # Wie alle anderen Turn-Pfade (respond/see/auto-vision/proactive): Heart-/
    # Verdichtungs-/Facts-Gates + proaktiv-Cooldown-Reset. Sonst koennte der
    # Spontan-Loop direkt nach der Timer-Reaktion erneut feuern (next_at war
    # vor dem Timer ggf. schon faellig) -> Yuki redet doppelt hintereinander.
    _post_turn("", reply_clean)
    sse_payload = {"kind": "timer_done", "id": tid, "label": label, "duration": dur_h,
                   "fired_ts": time.time(),   # Frontend unterdrueckt den Beep bei spaet nachgespieltem Timer
                   "reply": reply_clean, "display": display,
                   "translation": translation, "tokens": tokens, "furigana": furigana,
                   "actions": actions, "model": model_snapshot,
                   "persona": persona_snapshot, "mood": yc.load_mood()}
    if target_client_id:
        sse_payload["target_client_id"] = target_client_id
    _broadcast_sse(sse_payload)


yc.set_timer_callback(_on_timer_done)


# ===========================================================================
# Self-signed Zertifikat (fuer HTTPS / Mikrofon-Freigabe im Browser)
# ===========================================================================
def get_lan_ip():
    """Primäre LAN-IPv4 dieses Rechners ermitteln (ohne echten Traffic)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # verbindet nicht wirklich, waehlt nur das Interface
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(lan_ip):
    """
    Liefert (certfile, keyfile). Erzeugt ein self-signed Zertifikat (gueltig ~3
    Jahre) mit SAN = localhost, 127.0.0.1 und der LAN-IP. Vorhandenes, noch
    gueltiges Zertifikat mit passender IP wird wiederverwendet; aendert sich die
    LAN-IP, wird neu erzeugt.
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    CERT_DIR.mkdir(exist_ok=True)
    certfile = CERT_DIR / "yuki_cert.pem"
    keyfile = CERT_DIR / "yuki_key.pem"

    if certfile.exists() and keyfile.exists():
        try:
            cert = x509.load_pem_x509_certificate(certfile.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
            still_valid = cert.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc)
            if lan_ip in ips and still_valid:
                return str(certfile), str(keyfile)
        except Exception:
            pass  # kaputt/alt -> neu erzeugen

    print(f"Erzeuge self-signed Zertifikat fuer {lan_ip} (gueltig ~3 Jahre) ...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Yuki Local")])
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address(lan_ip)),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1095))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    keyfile.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(certfile), str(keyfile)


# ===========================================================================
# Routen
# ===========================================================================
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


# Schlanke Watch-UI (Galaxy Watch, Wear OS / Samsung Internet): web/watch.html.
# Eigenstaendige Seite, NUR die Sprach-Pipeline (Persona-Auswahl companion-only,
# PTT, Confirm-Gate, Replay) - kein Avatar/three.js, kein Capacitor, keine SSE,
# kein Timer-Alarm. Nutzt dieselben Endpunkte (/personas /persona /stt /respond
# /tts_stream). Auf der Uhr als Bookmark auf https://<host>:8443/watch ablegen.
@app.route("/watch")
def watch():
    return send_from_directory(WEB_DIR, "watch.html")


@app.route("/panel")
def panel():
    # Ambient-Kiosk fuer das HA-Wandpanel: dieselbe index.html, aber die Seite
    # erkennt den /panel-Pfad und schaltet in den reinen Avatar-Modus (kein
    # Chrome, kein Ton, keine Eingaben). Kein eigener HTML-File, damit der
    # verzahnte Avatar-Stack nicht dupliziert werden muss. Siehe
    # docs/superpowers/plans/2026-07-06-yuki-ambient-panel.md
    return send_from_directory(WEB_DIR, "index.html")


# Favicon / Touch-Icons (web/favicon*.{ico,png}, web/apple-touch-icon.png).
# Quelle ist Yukis Capacitor-Launcher-Icon, via PIL nach web/ gerendert. Eigene
# Whitelist-Route, weil es keinen generischen web/-Static-Handler gibt (nur
# index.html/avatar/vendor explizit). Browser fragt /favicon.ico automatisch ab;
# der Rest haengt an <link>-Tags im <head>.
_FAVICON_FILES = {
    "favicon.ico": "image/x-icon",
    "favicon-32.png": "image/png",
    "favicon-192.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@app.route("/<any('favicon.ico','favicon-32.png','favicon-192.png','apple-touch-icon.png'):filename>")
def favicon_asset(filename):
    return send_from_directory(WEB_DIR, filename, mimetype=_FAVICON_FILES[filename])


# Capacitor-Bridge fuer die mobile App (de.example.yuki, siehe mobile/-Verzeichnis).
# Die Datei liegt im node_modules der Capacitor-Installation und wird von index.html
# per UA-Detection geladen (im Desktop-Browser ist sie wertlos, isNativePlatform=false).
@app.route("/capacitor.js")
def capacitor_bridge():
    cap_dir = HERE / "mobile" / "node_modules" / "@capacitor" / "core" / "dist"
    return send_from_directory(cap_dir, "capacitor.js", mimetype="application/javascript")


@app.route("/avatar/<path:filename>")
def avatar_asset(filename):
    """Statisches Asset fuer den Web-Avatar (VRM-Datei + ggf. Texturen).
    Wird vom three-vrm-Renderer im Browser geladen. Erste-Last ~10-30MB,
    danach Browser-Cache."""
    return send_from_directory(HERE / "avatar", filename)


@app.route("/vendor/<path:filename>")
def vendor_asset(filename):
    """Self-gehostete Drittanbieter-Assets (aktuell MediaPipe-tasks-vision fuer den
    Kamera-Hintergrund-Nebel, web/vendor/mediapipe/). Bewusst lokal statt CDN, damit
    der Nebel offline laeuft. Dateien via tools/fetch_mediapipe.py geholt; fehlen sie
    -> 404 (Frontend faengt das ab und laesst den Nebel einfach aus)."""
    return send_from_directory(WEB_DIR / "vendor", filename)


_THUMB_MAX = 360   # Galerie-Kacheln sind ~150px; 360 deckt Retina/2x ab + bleibt klein


def _cached_thumb(src, tdir):
    """Cachte ein <=360px-JPEG-Thumbnail von src (Pfad) in tdir (Name = src.name).
    Regeneriert bei Miss oder wenn das Original neuer ist. Generischer Helper fuer
    Keepsakes UND Vision-Shots. Returns Path oder None (Fehler -> Caller liefert das
    Original aus)."""
    try:
        tdir.mkdir(parents=True, exist_ok=True)
        tpath = tdir / src.name
        if (not tpath.is_file()) or tpath.stat().st_mtime < src.stat().st_mtime:
            from PIL import Image, ImageOps
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)       # Portrait-Orientierung korrekt
                im = im.convert("RGB")
                im.thumbnail((_THUMB_MAX, _THUMB_MAX))  # nur verkleinern, nie vergroessern
                im.save(tpath, "JPEG", quality=80)
        return tpath
    except Exception as e:
        print(f"  [Thumbnail-Fehler {src.name}: {e}]", flush=True)
        return None


def _keepsake_thumb(name, src):
    """Keepsake-Thumbnail in keepsakes/.thumbs/ (Keepsakes sind immutable -> Cache
    quasi nie stale). Duenner Wrapper um _cached_thumb."""
    return _cached_thumb(src, yc.KEEPSAKES_DIR / ".thumbs")


@app.route("/keepsakes/<path:filename>")
def keepsake_asset(filename):
    """Gespeichertes Keepsake-Foto fuer die Galerie-Ansicht (nur Basename, kein
    Subpfad-Ausbruch). ?thumb=1 liefert ein gecachtes Klein-Thumbnail (Grid-Vorschau);
    ohne den Param das Original (Lightbox)."""
    name = Path(filename).name
    src = yc.KEEPSAKES_DIR / name
    if request.args.get("thumb") and src.is_file():
        thumb = _keepsake_thumb(name, src)
        if thumb:
            return send_from_directory(thumb.parent, thumb.name, mimetype="image/jpeg")
    return send_from_directory(yc.KEEPSAKES_DIR, name)


@app.route("/gedankenbilder/<path:filename>")
def gedankenbild_asset(filename):
    """Gespeichertes Gedankenbild (KI-PNG) fuer Chat-Inline + Galerie (nur Basename).
    ?thumb=1 liefert ein gecachtes Klein-Thumbnail, sonst das Original (Lightbox)."""
    name = Path(filename).name
    src = yc.GEDANKENBILDER_DIR / name
    if request.args.get("thumb") and src.is_file():
        thumb = _cached_thumb(src, yc.GEDANKENBILDER_DIR / ".thumbs")
        if thumb:
            return send_from_directory(thumb.parent, thumb.name, mimetype="image/jpeg")
    return send_from_directory(yc.GEDANKENBILDER_DIR, name)


@app.route("/gedankenbild/regenerate", methods=["POST"])
def gedankenbild_regenerate():
    """Dieselbe Vision eines Gedankenbild-Turns neu wuerfeln (neuer Seed) und die
    Variante an die Turn-Liste anhaengen. Isoliert (kein Decider/LLM/Memory).
    400 = Turn nicht regenerierbar (kein gespeicherter Prompt, z.B. Alt-Bild);
    503 = Bild-Dienst offline; 202 = angenommen (Ergebnis kommt per SSE)."""
    data = request.get_json(silent=True) or {}
    turn_id = (data.get("turn_id") or "").strip()
    with LOCK:
        idx = _history_index_by_turn_id(turn_id)
        prompt = HISTORY[idx].get("gedankenbild_prompt") if idx is not None else None
        style = HISTORY[idx].get("gedankenbild_style", "") if idx is not None else ""
    if not prompt:
        return jsonify({"ok": False, "error": "nicht regenerierbar"}), 400
    if not imagegen.is_comfyui_reachable():
        return jsonify({"ok": False, "error": "offline"}), 503
    _spawn_regenerate(turn_id, prompt, style)
    return jsonify({"ok": True}), 202


@app.route("/drawings/<path:filename>")
def drawing_asset(filename):
    """Gespeichertes Doodle-SVG fuer die Galerie-Ansicht (nur Basename)."""
    return send_from_directory(yc.DRAWINGS_DIR, Path(filename).name,
                               mimetype="image/svg+xml")


# ---- Vision-Shots: jedes Bild das Yuki macht/gezeigt bekommt als Inline-Thumb ---
# Damit der Chat das Kamera-/Foto-Bild als kleines Vorschaubild zeigt (Klick ->
# Lightbox gross) und es Reload + Geraetewechsel ueberlebt. Anders als Keepsakes
# (kuratiertes Album, Gate) wird hier JEDER Vision-Frame abgelegt - dafuer ein
# gedeckelter Rolling-Ordner (aelteste fliegen raus). Rein lokal vom Yuki-Server,
# kein Cloud -> konform zum Offline-Prinzip.
VISION_SHOTS_DIR = yc.RUNTIME_DIR / "vision_shots"
_VISION_SHOTS_CAP = 200                  # ~60-80 MB bei Reolink-JPEGs (~260-430 KB)
_vision_shot_seq = 0
_vision_shot_lock = threading.Lock()


def _prune_vision_shots():
    """Rolling-Cap: nur die juengsten _VISION_SHOTS_CAP Shots behalten, aeltere (+
    ihre Thumbnails) loeschen. Best-effort, schluckt Fehler."""
    try:
        shots = sorted(VISION_SHOTS_DIR.glob("shot_*.jpg"), key=lambda p: p.stat().st_mtime)
        tdir = VISION_SHOTS_DIR / ".thumbs"
        for p in shots[:max(0, len(shots) - _VISION_SHOTS_CAP)]:
            p.unlink(missing_ok=True)
            (tdir / p.name).unlink(missing_ok=True)
    except Exception as e:
        print(f"  [vision-shot prune Fehler: {e}]", flush=True)


def _save_vision_shot(image_bytes):
    """Speichert einen Vision-Frame (autonom/lookat/gezeigt) in den Rolling-Ordner
    und gibt den Dateinamen zurueck (oder None bei Fehler/leer). Caller haengt den
    Namen via _stamp_vision_image an den Perception-Turn + ins SSE/JSON."""
    if not image_bytes:
        return None
    try:
        VISION_SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        global _vision_shot_seq
        with _vision_shot_lock:
            _vision_shot_seq += 1
            name = f"shot_{int(time.time())}_{_vision_shot_seq}.jpg"
        (VISION_SHOTS_DIR / name).write_bytes(image_bytes)
        _prune_vision_shots()
        return name
    except Exception as e:
        print(f"  [vision-shot speichern Fehler: {e}]", flush=True)
        return None


def _stamp_vision_image(filename):
    """Haengt den Vision-Shot-Dateinamen an den Perception-Turn (= letzter user-Turn)
    in HISTORY -> /history liefert das Bild beim Reload mit. build_messages filtert
    role+content, es leakt also NICHT zu Ollama (wie translated/tokens/furigana).
    Unter LOCK aufrufen (mutiert HISTORY)."""
    if not filename:
        return
    for t in reversed(HISTORY):
        if t.get("role") == "user":
            t["image"] = filename
            return


def _stamp_vision_faces(faces):
    """Haengt die erkannten Personen ([{id,name}]) an den Perception-Turn (= letzter
    user-Turn) in HISTORY -> /history liefert sie beim Reload mit, wie 'image'.
    Leere Liste wird nicht gestempelt. Unter LOCK aufrufen (mutiert HISTORY)."""
    if not faces:
        return
    for t in reversed(HISTORY):
        if t.get("role") == "user":
            t["faces"] = faces
            return


@app.route("/vision_shots/<path:filename>")
def vision_shot_asset(filename):
    """Vision-Frame fuers Inline-Bild im Chat. ?thumb=1 = gecachtes Klein-Thumbnail
    (Bubble), ohne = Original (Lightbox). Nur Basename (kein Subpfad-Ausbruch)."""
    name = Path(filename).name
    src = VISION_SHOTS_DIR / name
    if not src.is_file():
        return ("", 404)
    if request.args.get("thumb"):
        thumb = _cached_thumb(src, VISION_SHOTS_DIR / ".thumbs")
        if thumb:
            return send_from_directory(thumb.parent, thumb.name, mimetype="image/jpeg")
    return send_from_directory(VISION_SHOTS_DIR, name, mimetype="image/jpeg")


@app.route("/gallery", methods=["GET", "POST", "DELETE"])
def gallery_route():
    """Kuratierte Bild-Wand (2026-06-17, erste In-App-Browse-Sicht auf gespeicherte
    Bilder). GET: aufgeloeste Liste (nur noch existierende Bilder). POST {kind, file}:
    Bild pinnen (herkunft michael). DELETE {id}: entfernen (Original bleibt). Yukis
    eigene Doodles pinnt sie ueber den [gallery]-Marker (herkunft yuki) in /respond."""
    if request.method == "POST":
        p = request.get_json(silent=True) or {}
        e = yc.add_to_gallery(p.get("kind"), p.get("file"), origin="michael")
        if not e:
            return jsonify({"ok": False, "error": "Bild nicht gefunden"}), 400
        _broadcast_sse({"kind": "gallery_update"})
        return jsonify({"ok": True, "gallery": yc.gallery_resolved()})
    if request.method == "DELETE":
        p = request.get_json(silent=True) or {}
        yc.remove_from_gallery((p.get("id") or "").strip())
        _broadcast_sse({"kind": "gallery_update"})
        return jsonify({"ok": True, "gallery": yc.gallery_resolved()})
    return jsonify({"ok": True, "gallery": yc.gallery_resolved()})


@app.route("/gallery/pool", methods=["GET", "DELETE"])
def gallery_pool_route():
    """GET: Alle gespeicherten Bilder (Doodles + Keepsakes) fuer Michaels
    'hinzufuegen'-Picker - neueste zuerst, markiert was schon in der Galerie ist.
    DELETE {kind, file}: Bild HART von der Platte loeschen (Original + Sidecar) +
    aus der Galerie raeumen - fuer peinliche auto-gespeicherte Keepsakes. Irreversibel."""
    if request.method == "DELETE":
        p = request.get_json(silent=True) or {}
        res = yc.delete_pool_image(p.get("kind"), p.get("file"))
        if not res.get("ok"):
            return jsonify(res), 400
        if res.get("removed_gallery"):
            _broadcast_sse({"kind": "gallery_update"})
        return jsonify({**res, "pool": yc.gallery_pool()})
    return jsonify({"ok": True, "pool": yc.gallery_pool()})


@app.route("/avatar/animations/list")
def avatar_animations_list():
    """Liefert die Liste aller .vrma-Dateien aus avatar/animations/ REKURSIV
    inkl. Bucket-Zuordnung. Bucket = erste Pfad-Komponente (Subordner-Name)
    bzw. 'default' fuer Files direkt im Animations-Root. Frontend nutzt das,
    um Clips in idle/speaking/reactive/persona-Pools zu sortieren (Mixamo-
    Pipeline 2026-06-01). Ordner darf fehlen -> leere Liste, kein 404."""
    anim_dir = HERE / "avatar" / "animations"
    if not anim_dir.is_dir():
        return jsonify({"files": []})
    out = []
    for p in anim_dir.rglob("*.vrma"):
        if not p.is_file():
            continue
        rel = p.relative_to(anim_dir).as_posix()
        # Bucket = Name des direkten Eltern-Ordners, NICHT erste Pfad-Komponente.
        # Layout ist 'vrma/<kategorie>/<file>.vrma' - 'vrma' selbst ist nur ein
        # Sammel-Wrapper aus der Mixamo-Pipeline und sagt nichts ueber die
        # Semantik. Files direkt im animations/-Root -> 'default'.
        if p.parent == anim_dir:
            bucket = "default"
        else:
            bucket = p.parent.name
        out.append({"path": rel, "bucket": bucket})
    out.sort(key=lambda x: x["path"])
    return jsonify({"files": out})


@app.route("/config/<filename>")
def config_file(filename):
    """Generischer Endpoint fuer die Config-Files unter config/. Whitelist:
    nur lowercase + Underscores + .json. Bei fehlender Datei kommt {} zurueck;
    das Frontend hat hardcoded Defaults als Fallback (Bootstrap-Sicherheit).
    Aktuell aktive: config/avatar.json, config/moods.json, config/personas.json."""
    if not re.match(r"^[a-z_]+\.json$", filename):
        return jsonify({}), 404
    cfg_path = HERE / "config" / filename
    if not cfg_path.is_file():
        return jsonify({})
    try:
        with cfg_path.open(encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] {filename} kaputt: {e}", flush=True)
        return jsonify({})


@app.route("/avatar/list")
def avatar_list():
    """Liefert die Liste aller .vrm-Dateien im avatar/-Ordner (Outfit-Switch).
    Frontend zeigt sie im Options-Modal-Dropdown; Auswahl wird in localStorage
    gespeichert und beim naechsten Page-Load vom VRM-Loader gelesen."""
    files = sorted(p.name for p in (HERE / "avatar").iterdir()
                   if p.is_file() and p.suffix.lower() == ".vrm")
    return jsonify({"files": files})


@app.route("/session_end", methods=["POST"])
def session_end():
    """Aktive Sitzung manuell beenden: verdichtet die conversation.json, archiviert
    das Volltranskript nach archive/sessions/, extrahiert Facts, leert conversation.json.
    Vorher wurde das bei JEDEM Server-Start automatisch gemacht - das ist seit
    2026-05-31 raus, weil Michael Yuki sonst gefuehlt taeglich neu begruessen muss.
    Stattdessen explizit ueber den Button im Options-Modal."""
    global MEMORY, SYSTEM_MSG
    with LOCK:
        result = yc.end_session()
        if result.get("ok"):
            # HISTORY leeren (conversation.json schon auf [] gesetzt von end_session),
            # MEMORY neu laden + SYSTEM_MSG rebuilden, damit Yuki den frischen
            # Memory-Block sieht.
            HISTORY[:] = []
            MEMORY = yc.load_memory()
            SYSTEM_MSG = yc.build_system_msg(MEMORY, CURRENT_PERSONA)
    return jsonify(result)


@app.route("/cheatsheet")
def cheatsheet():
    """Liefert docs/cheatsheet.md als text/markdown - Quelle fuer das Cheatsheet-
    Modal im Frontend (marked.js rendert es dort). no-cache, damit Edits am MD
    sofort sichtbar werden ohne Browser-Hard-Reload."""
    md_path = Path(__file__).parent / "docs" / "cheatsheet.md"
    if not md_path.exists():
        return ("Cheatsheet fehlt: docs/cheatsheet.md", 404, {"Content-Type": "text/plain; charset=utf-8"})
    resp = app.response_class(
        md_path.read_text(encoding="utf-8"),
        mimetype="text/markdown; charset=utf-8",
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/history")
def history():
    """Letzte N Turns aus HISTORY (default 20, max 100) fuer Browser-Restore beim
    Reload oder Geraete-Wechsel (Desktop <-> Handy). Server-Neustart leert HISTORY
    sowieso -> dann ist die Liste leer und der Client malt nichts. Format wie
    intern: [{role:'user'|'assistant', content:str, translated?:str}, ...]. Der
    Client erkennt Vision-Wahrnehmungen (user-Turns die mit '[' starten) selbst.

    WICHTIG: Assistant-Turns werden durch strip_all_markers gejagt, damit
    [mood:...]/[timer:...]/[note:...]/[heart:...]/[keepsake:...]/[persona:...]/
    [event:...] nicht im UI sichtbar sind. Die HISTORY selbst bleibt unveraendert
    (das LLM sieht weiter die Original-Marker als Pattern in der Few-Shot-History).

    'translated' (Kyoto-Persona-Untertitel): reines UI-Meta-Feld, liegt nur in der
    conversation.json - build_messages filtert es vor Ollama raus, Yuki sieht es
    in past turns nie. Beim Reload wird es vom Client als gedaempfter Subtitle
    unter der JP-Bubble gerendert."""
    try:
        n = max(0, min(int(request.args.get("n", 20)), 100))
    except ValueError:
        n = 20
    raw = HISTORY[-n:] if n else []
    turns = []
    for t in raw:
        entry = {"role": t["role"]}
        if t["role"] == "assistant":
            # Calc + Conjugate beim Reload re-evaluieren BEVOR strip_all_markers
            # laeuft (Letzteres wuerde uebrige Marker als Sicherheitsnetz auf den
            # Inner-Text strippen). expand_* ersetzt sie durch das echte
            # Resultat, sodass der Reload dasselbe zeigt wie der Live-Reply.
            # Tiny overhead (~ms pro Reply mit solchen Markern).
            expanded, _ = yc.expand_calc_markers(t["content"])
            expanded, _ = yc.expand_conjugate_markers(expanded)
            cleaned = yc.strip_all_markers(expanded)
            entry["content"] = cleaned
            # Romaji-Annotation hier on-the-fly nachziehen, damit der Browser-
            # Reload dasselbe sieht wie der Live-Reply. annotate_romaji liegt
            # NUR im Live-display-Feld (/respond etc.), nicht im History-Content
            # selbst - sonst muesste HISTORY zwei Versionen tragen. Bei n=20
            # Turns kostet das ~50-100ms fugashi-Zeit beim Reload, OK.
            entry["display"] = yc.annotate_romaji(cleaned)
            if t.get("translated"):
                entry["translated"] = t["translated"]
            stored_tokens = t.get("tokens") or []
            stored_furigana = t.get("furigana") or []
            # Backfill 2026-06-05: alte HISTORY-Eintraege (vor dem Per-Token-
            # Ruby-Enrich) haben tokens ohne 'ruby'-Feld. Wenn furigana-Specs
            # vorhanden sind und Tokens noch keins haben, hier on-the-fly
            # nach-enrichen damit der Reload-Render Ruby UND Click-to-Gloss
            # gleichzeitig zeigt. Live-Eintraege ab heute haben das bereits in
            # _tokens_for - dann ist der any()-Check truthy und wir skippen.
            if stored_tokens and stored_furigana \
               and not any(isinstance(tok, dict) and tok.get("ruby") for tok in stored_tokens):
                stored_tokens = _enrich_tokens_with_ruby(stored_tokens, cleaned, stored_furigana)
            if stored_tokens:
                entry["tokens"] = stored_tokens
            if stored_furigana:
                entry["furigana"] = stored_furigana
            # drawing (Kuenstlerin): SVG-Doodle als UI-Meta, beim Reload re-rendert
            # das Frontend das <img>. build_messages haelt es aus dem LLM-Kontext.
            if t.get("drawing"):
                entry["drawing"] = t["drawing"]
            if t.get("gedankenbilder"):
                entry["gedankenbilder"] = t["gedankenbilder"]
            elif t.get("gedankenbild"):
                entry["gedankenbild"] = t["gedankenbild"]   # Legacy-Einzelbild (kein 🔄)
            # Action-Icons: aus dem ROHEN HISTORY-Content scannen, BEVOR
            # strip_all_markers oben drueberlief. sanitize_reply_for_history laesst
            # gueltige Action-Marker bewusst drin (Pattern-Reinforcement), darum
            # finden wir sie hier zuverlaessig. Frontend rendert nur wenn Toggle an.
            # Async-Kanal (ha/routine_done): gespeicherte Records aus meta bevorzugen.
            meta_actions = (t.get("meta") or {}).get("actions") or []
            actions = _detect_actions(t["content"])          # nicht-migrierte Inline-Marker
            # 👁-Look-Action ist KEIN Side-Effect-Marker in _detect_actions ([lookat]
            # steht nicht in _ACTION_MARKER_RE) - sie wird live separat ueber
            # _resolve_lookat gesetzt. Beim Reload genauso aus dem rohen [lookat:...]
            # rekonstruieren, sonst fehlt das 👁-Icon nach Neuladen. Raw-Content traegt
            # den Marker noch (sanitize_reply_for_history laesst ihn als Pattern drin).
            _la = _resolve_lookat(t["content"])
            if _la:
                actions = (actions or []) + [{"type": "look", "detail": _la[2]}]
            actions = (actions or []) + list(meta_actions)   # async-Records anhaengen
            if actions:
                entry["actions"] = actions
            if t.get("turn_id"):
                entry["turn_id"] = t["turn_id"]
            # research-Flag aus meta extrahieren - das Frontend nutzt es um die
            # Bubble als collapsed zu rendern + den 🔍-Modal-Button anzubieten.
            meta = t.get("meta") or {}
            if meta.get("research"):
                entry["research"] = True
            # Sekretaerin-Flag analog: gleicher Collapse-Pfad im Frontend, damit
            # ein Sekretaerin-Turn nach Reload nicht als Riesen-Bubble auflaeuft.
            if meta.get("secretary"):
                entry["secretary"] = True
            # [mehr]-Split (Einleitung + Overlay-Body): live in meta gelegt, hier
            # 1:1 mit-ausliefern damit der Reload dieselbe Bubble (Einleitung + Button)
            # rendert statt erster-Satz-Collapse. strip_all_markers (oben) hat den
            # rohen [mehr]-Marker schon aus entry["content"] entfernt.
            if meta.get("research_lead"):
                entry["research_lead"] = meta["research_lead"]
                entry["research_body"] = meta.get("research_body") or ""
            # Scope B: Datei-Trefferliste (Chip + Overlay) nach Reload wiederherstellen.
            if meta.get("file_hits"):
                entry["file_hits"] = meta["file_hits"]
                if meta.get("file_hits_meta"):
                    entry["file_hits_meta"] = meta["file_hits_meta"]
            # Steward-Reach-Out (gruene Bubble): meta.steward -> Frontend rendert
            # beim Reload die .steward-Klasse (Akzent + "meldet sich"-Label).
            if meta.get("steward"):
                entry["steward"] = True
            # Ganze-Geschichte-Hinweis (Erzaehlerin): meta.story {id,title,
            # paragraph_count} -> Frontend rendert die "📖 Geschichte öffnen"-Karte
            # auch nach Reload (Story-Text selbst liegt in der Library, nicht hier).
            if meta.get("story"):
                entry["story"] = meta["story"]
            # Modell-Badge: voller Modellname (gemma4:12b/...), den die Bubble als
            # Groessen-Woelkchen oben-links rendert. Frontend kuerzt auf das Tag.
            if meta.get("model"):
                entry["model"] = meta["model"]
        else:
            entry["content"] = t["content"]
            # Wahrnehmungs-Klassifikation fuer den Reload-Renderer (Bugfix
            # 2026-06-06): proaktive Spontan-Trigger sind keine Bild-Bubbles,
            # echte Bild-Wahrnehmungen schon. Client unterdrueckt 'proactive'
            # komplett, timer_done rendert als sys-Bubble, vision_* zeigt
            # Caption oder VLM-Beschreibung (Bugfix 2026-06-10) statt nur
            # "(Bild gezeigt)" - Live-UX zeigt seit jeher die Caption, der
            # Reload soll dem nahekommen.
            kind = _perception_kind(t["content"])
            if kind:
                entry["kind"] = kind
                extras = _perception_extras(t["content"], kind)
                for k, v in extras.items():
                    entry[k] = v
            # Vision-Shot (jedes Kamera-/Foto-Bild als Inline-Thumbnail): am
            # Perception-Turn als 'image' (Dateiname) hinterlegt -> Frontend baut
            # /vision_shots/<name> (+?thumb). UI-Meta, leakt nicht zu Ollama.
            if t.get("image"):
                entry["image"] = t["image"]
            # Erkannte Personen (Gesichtserkennung) als UI-Icons an der Bild-Bubble.
            if t.get("faces"):
                entry["faces"] = t["faces"]
        turns.append(entry)
    return jsonify({"ok": True, "turns": turns, "total": len(HISTORY)})


@app.route("/notes", methods=["GET"])
def notes_list():
    """Komplette Notes-Liste fuer das UI-Panel."""
    return jsonify({"ok": True, "notes": yc.load_notes()})


@app.route("/notes", methods=["POST"])
def notes_add():
    """Neue Notiz vom UI anlegen. Body: {text, active?, source?}. active default
    True (frisch angelegte Notizen sind sichtbar; User kann ueber Toggle ausschalten).
    source default 'michael' (UI-angelegte Notiz = vom User); autonome Quellen
    setzen z.B. 'yuki'."""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "leerer Text"}), 400
    active = body.get("active", True)
    source = body.get("source") or "michael"
    note = yc.add_note(text, active=active, source=source)
    if not note:
        return jsonify({"ok": False, "error": "Notiz konnte nicht angelegt werden"}), 500
    if note.get("active"):
        _refresh_system_msg()
    return jsonify({"ok": True, "note": note})


@app.route("/notes/<note_id>", methods=["PUT"])
def notes_update(note_id):
    """Notiz aendern (text und/oder active). Body: {text?, active?}.
    Wenn active sich aendert -> SYSTEM_MSG rebuild, damit Yuki sofort
    den neuen Stand sieht."""
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    active = body.get("active")
    source = body.get("source")
    if text is None and active is None and source is None:
        return jsonify({"ok": False, "error": "kein Feld zum Aendern"}), 400
    note = yc.update_note(note_id, text=text, active=active, source=source)
    if not note:
        return jsonify({"ok": False, "error": "Notiz nicht gefunden"}), 404
    _refresh_system_msg()                 # alles was den Active-State beruehrt rebuildet
    return jsonify({"ok": True, "note": note})


@app.route("/notes/<note_id>", methods=["DELETE"])
def notes_delete(note_id):
    """Notiz loeschen."""
    ok = yc.delete_note(note_id)
    if not ok:
        return jsonify({"ok": False, "error": "Notiz nicht gefunden"}), 404
    _refresh_system_msg()
    return jsonify({"ok": True})


@app.route("/notes/purge_inactive", methods=["POST"])
def notes_purge_inactive():
    """Alle INAKTIVEN Notizen loeschen (aktive bleiben). Body optional
    {source: '<exakte Quelle>'} raeumt nur eine Panel-Sektion (michael / yuki /
    steward_rss / steward_sehnsucht / ...); ohne source werden alle inaktiven
    quer geloescht. Kein _refresh_system_msg noetig - inaktive Notizen sind eh
    nicht im Prompt. Liefert die Anzahl geloeschter Notizen."""
    body = request.get_json(silent=True) or {}
    source = body.get("source")
    removed = yc.purge_inactive_notes(source=source)
    return jsonify({"ok": True, "removed": removed})


# ---------------------------------------------------------------------------
# Yuki-Listen (L3, 2026-06-19): Einkauf/Rezept/frei. Michael-only Utility, kein
# Canon. CRUD + manuelles Abhaken + Knopf-Aktivierung. [[yuki-listen-produktlesen]]
# ---------------------------------------------------------------------------
@app.route("/lists", methods=["GET"])
def lists_list():
    """Alle Yuki-Listen fuers UI-Modal/Seiten-Panel."""
    return jsonify({"ok": True, "lists": yc.load_lists()})


@app.route("/lists", methods=["POST"])
def lists_create():
    """Neue Liste vom UI. Body: {title, kind?, items?:[str], activate?:bool}."""
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "leerer Titel"}), 400
    lst = yc.create_list(title, kind=body.get("kind") or "shopping",
                         items=body.get("items") or [], activate=bool(body.get("activate")))
    if not lst:
        return jsonify({"ok": False, "error": "Liste konnte nicht angelegt werden"}), 500
    if lst.get("active"):
        _refresh_system_msg()
    _broadcast_sse({"kind": "list_changed", "reason": "created", "id": lst["id"]})
    return jsonify({"ok": True, "list": lst})


@app.route("/lists/<list_id>", methods=["PUT"])
def lists_update(list_id):
    """Liste bearbeiten: {title} benennt um ODER {kind} setzt die Kategorie
    (shopping/recipe/free) neu. Genau eins pro Aufruf."""
    body = request.get_json(silent=True) or {}
    if "kind" in body:
        lst = yc.set_list_kind(list_id, body.get("kind"))
        reason = "kind"
    else:
        title = (body.get("title") or "").strip()
        if not title:
            return jsonify({"ok": False, "error": "leerer Titel"}), 400
        lst = yc.rename_list(list_id, title)
        reason = "rename"
    if not lst:
        return jsonify({"ok": False, "error": "Liste nicht gefunden"}), 404
    if lst.get("active"):
        _refresh_system_msg()
    _broadcast_sse({"kind": "list_changed", "reason": reason, "id": list_id})
    return jsonify({"ok": True, "list": lst})


@app.route("/lists/<list_id>", methods=["DELETE"])
def lists_delete(list_id):
    """Ganze Liste loeschen."""
    was_active = (yc.active_list() or {}).get("id") == list_id
    if not yc.delete_list(list_id):
        return jsonify({"ok": False, "error": "Liste nicht gefunden"}), 404
    if was_active:
        _refresh_system_msg()
    _broadcast_sse({"kind": "list_changed", "reason": "deleted", "id": list_id})
    return jsonify({"ok": True})


@app.route("/lists/<list_id>/active", methods=["POST"])
def lists_set_active(list_id):
    """Knopf-Aktivierung: expliziter Override, loest eine andere aktive Liste ab
    (Invariante max. 1 aktiv). Body: {active?:bool} default True."""
    body = request.get_json(silent=True) or {}
    lst = yc.set_list_active(list_id, active=bool(body.get("active", True)))
    if not lst:
        return jsonify({"ok": False, "error": "Liste nicht gefunden"}), 404
    _refresh_system_msg()                 # aktive Liste wandert (ab L3b) in den Kontext
    _broadcast_sse({"kind": "list_changed", "reason": "active", "id": list_id})
    return jsonify({"ok": True, "list": lst})


@app.route("/lists/<list_id>/archived", methods=["POST"])
def lists_set_archived(list_id):
    """Liste archivieren / wiederherstellen. Body: {archived?:bool} default True.
    Archivieren deaktiviert eine aktive Liste automatisch (-> Prompt-Refresh)."""
    body = request.get_json(silent=True) or {}
    was_active = (yc.active_list() or {}).get("id") == list_id
    lst = yc.set_list_archived(list_id, bool(body.get("archived", True)))
    if not lst:
        return jsonify({"ok": False, "error": "Liste nicht gefunden"}), 404
    if was_active:                        # aktive Liste wurde weg-archiviert
        _refresh_system_msg()
    _broadcast_sse({"kind": "list_changed", "reason": "archived", "id": list_id})
    return jsonify({"ok": True, "list": lst})


@app.route("/lists/<list_id>/item", methods=["POST"])
def lists_item_add(list_id):
    """Item hinzufuegen. Body: {text}."""
    body = request.get_json(silent=True) or {}
    lst = yc.add_list_item(list_id, (body.get("text") or "").strip())
    if not lst:
        return jsonify({"ok": False, "error": "Liste/Text ungueltig"}), 400
    if lst.get("active"):
        _refresh_system_msg()
    _broadcast_sse({"kind": "list_changed", "reason": "item", "id": list_id})
    return jsonify({"ok": True, "list": lst})


@app.route("/lists/<list_id>/item/<int:idx>", methods=["PUT"])
def lists_item_update(list_id, idx):
    """Item aendern: {checked:bool} hakt ab/zurueck (manuell, verbindlich) ODER
    {text:str} bearbeitet den Eintragstext ODER {clear_hint:"photo"|"voice"} entfernt
    genau EINEN Vorschlags-Hinweis (👁/🗣), ohne den Haken anzufassen."""
    body = request.get_json(silent=True) or {}
    if "text" in body:
        lst = yc.edit_list_item(list_id, idx, (body.get("text") or "").strip())
    elif body.get("clear_hint") == "voice":
        lst = yc.set_list_item_voice_hint(list_id, idx, "")   # "" -> None (Hinweis weg)
    elif body.get("clear_hint") == "photo":
        lst = yc.set_list_item_hint(list_id, idx, "")
    elif body.get("checked") is not None:
        lst = yc.set_list_item_checked(list_id, idx, bool(body.get("checked")))
    else:
        return jsonify({"ok": False, "error": "weder text noch checked noch clear_hint"}), 400
    if not lst:
        return jsonify({"ok": False, "error": "Liste/Item/Text ungueltig"}), 404
    if lst.get("active"):
        _refresh_system_msg()
    _broadcast_sse({"kind": "list_changed", "reason": "item", "id": list_id})
    return jsonify({"ok": True, "list": lst})


@app.route("/lists/<list_id>/item/<int:idx>", methods=["DELETE"])
def lists_item_delete(list_id, idx):
    """Item entfernen."""
    lst = yc.delete_list_item(list_id, idx)
    if not lst:
        return jsonify({"ok": False, "error": "Liste/Item nicht gefunden"}), 404
    if lst.get("active"):
        _refresh_system_msg()
    _broadcast_sse({"kind": "list_changed", "reason": "item", "id": list_id})
    return jsonify({"ok": True, "list": lst})


@app.route("/affinities", methods=["GET"])
def affinities_list():
    """Read-only-Inspector fuer das Options-Modal. Liefert sortierte Liste
    (abs(score) desc, dann last_touched desc) + den aktuellen Multiplier."""
    entries = yc.load_affinities()
    # Sort fuer das UI: stronge Gefuehle zuerst, innerhalb gleicher Staerke
    # neueste zuerst.
    entries_sorted = sorted(
        entries,
        key=lambda e: (abs(int(e.get("score") or 0)),
                       e.get("last_touched_ts") or ""),
        reverse=True)
    return jsonify({
        "ok": True,
        "entries": entries_sorted,
        "multiplier": yc.AFFINITIES_MULTIPLIER,
        "min_evidence": yc.AFFINITIES_MIN_EVIDENCE,
        "decay_days": yc.AFFINITIES_DECAY_DAYS,
    })


@app.route("/affinities/<entry_id>", methods=["DELETE"])
def affinities_delete(entry_id):
    """Einzel-Eintrag loeschen (Inspector-Edit). User-Korrektur falls Gate
    was falsches eingetragen hat oder ein Eintrag obsolet ist."""
    ok = yc.delete_affinity(entry_id)
    if not ok:
        return jsonify({"ok": False, "error": "Affinity nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/affinities/<entry_id>/disable", methods=["POST"])
def affinities_disable(entry_id):
    """Tombstone-Toggle: Body {disabled: bool}. disabled=True unterdrueckt die
    Affinitaet dauerhaft (unsichtbar + Gate/Marker beleben nicht wieder);
    disabled=False hebt es auf."""
    body = request.get_json(silent=True) or {}
    disabled = bool(body.get("disabled", True))
    okd = yc.set_affinity_disabled(entry_id, disabled)
    if not okd:
        return jsonify({"ok": False, "error": "Affinity nicht gefunden"}), 404
    return jsonify({"ok": True, "disabled": disabled})


@app.route("/affinities/consolidate", methods=["GET"])
def affinities_consolidate():
    """On-demand Aufraeum-Lauf: LLM vergleicht die Liste mit sich selbst und
    liefert Vorschlaege (merge/contradiction/discard). KEINE Mutation - der User
    bestaetigt via /affinities/consolidate/apply."""
    try:
        suggestions = yc.consolidate_affinities_suggestions()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "suggestions": suggestions})


@app.route("/affinities/consolidate/apply", methods=["POST"])
def affinities_consolidate_apply():
    """Wendet vom User bestaetigte Entscheidungen an. Body {decisions:[...]}."""
    body = request.get_json(silent=True) or {}
    decisions = body.get("decisions") or []
    if not isinstance(decisions, list):
        return jsonify({"ok": False, "error": "decisions muss Liste sein"}), 400
    res = yc.apply_affinity_consolidation(decisions)
    return jsonify({"ok": True, **res})


@app.route("/affinities/multiplier", methods=["POST"])
def affinities_set_multiplier():
    """Live-Hebel: Frontend-Slider POSTet {value: 0.0..1.0}. Setter persistiert
    in memory/yuki_affinity_runtime.json und setzt die Modul-Var. Wirkt sofort
    beim naechsten Turn (build_system_msg / recall_block lesen die Modul-Var
    bei jedem Aufruf neu)."""
    body = request.get_json(silent=True) or {}
    raw = body.get("value")
    if raw is None:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    try:
        new_val = yc.set_affinities_multiplier(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    # System-Msg neu bauen damit der Block-Wechsel (leer <-> sichtbar) sofort
    # greift. Bei Multiplier=0 wird der Block leer, bei >0 erscheint er - der
    # Rebuild macht den Unterschied am naechsten Turn ohne Verzoegerung.
    _refresh_system_msg()
    return jsonify({"ok": True, "multiplier": new_val})


@app.route("/disposition", methods=["GET"])
def disposition_get():
    """Inspector-Payload: Core + Multiplier + Facet-Whitelist."""
    with LOCK:
        return jsonify({
            "core": yc.load_disposition().get("core", []),
            "multiplier": yc.DISPOSITION_MULTIPLIER,
            "facets": list(yc.DISPOSITION_FACETS),
            "max_core": yc.DISPOSITION_MAX_CORE,
        })


@app.route("/disposition/core", methods=["POST"])
def disposition_core_add():
    body = request.get_json(silent=True) or {}
    with LOCK:
        entry = yc.add_disposition_core(body.get("text"), body.get("facet"))
        if entry is None:
            return jsonify({"ok": False, "error": "leer oder Cap erreicht"}), 400
        _refresh_system_msg()
        return jsonify({"ok": True, "entry": entry})


@app.route("/disposition/core", methods=["PUT"])
def disposition_core_update():
    body = request.get_json(silent=True) or {}
    with LOCK:
        ok = yc.update_disposition_core(body.get("old_text"), body.get("new_text"), body.get("facet"))
        if not ok:
            return jsonify({"ok": False, "error": "nicht gefunden"}), 404
        _refresh_system_msg()
        return jsonify({"ok": True})


@app.route("/disposition/core", methods=["DELETE"])
def disposition_core_delete():
    body = request.get_json(silent=True) or {}
    with LOCK:
        ok = yc.delete_disposition_core(body.get("text"))
        if not ok:
            return jsonify({"ok": False, "error": "nicht gefunden"}), 404
        _refresh_system_msg()
        return jsonify({"ok": True})


@app.route("/disposition/multiplier", methods=["POST"])
def disposition_set_multiplier():
    """Live-Gegenwind-Regler. Slider POSTet {value: 0..1}. _refresh_system_msg
    noetig, weil der Kern-Block im gecachten SYSTEM_MSG sitzt (nicht pro Turn frisch)."""
    body = request.get_json(silent=True) or {}
    raw = body.get("value")
    if raw is None:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    with LOCK:
        new_val = yc.set_disposition_multiplier(raw)
        _refresh_system_msg()
    return jsonify({"ok": True, "multiplier": new_val})


@app.route("/curiosity/multiplier", methods=["POST"])
def curiosity_set_multiplier():
    """Live-Neugier-Regler. Slider POSTet {value: 0..1}. _refresh_system_msg
    noetig, weil die Regel im gecachten SYSTEM_MSG sitzt (nicht pro Turn frisch)."""
    body = request.get_json(silent=True) or {}
    raw = body.get("value")
    if raw is None:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    with LOCK:
        new_val = yc.set_curiosity_multiplier(raw)
        _refresh_system_msg()
    return jsonify({"ok": True, "multiplier": new_val})


@app.route("/curiosity", methods=["GET"])
def curiosity_get():
    """Slider-Init: aktueller Multiplier + enabled-Flag."""
    with LOCK:
        return jsonify({
            "ok": True,
            "multiplier": yc.CURIOSITY_MULTIPLIER,
            "enabled": yc.CURIOSITY_ENABLED,
        })


@app.route("/disposition/seed/start", methods=["POST"])
def disposition_seed_start():
    import disposition_seed as dseed
    with LOCK:
        return jsonify({"ok": True, **dseed.start_seed()})


@app.route("/disposition/seed/reflect", methods=["POST"])
def disposition_seed_reflect():
    import disposition_seed as dseed
    body = request.get_json(silent=True) or {}
    with LOCK:
        try:
            return jsonify({"ok": True, **dseed.reflect(body.get("job_id"), int(body.get("idx", 0)), body.get("steer"))})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/disposition/seed/map", methods=["POST"])
def disposition_seed_map():
    import disposition_seed as dseed
    body = request.get_json(silent=True) or {}
    with LOCK:
        try:
            return jsonify({"ok": True, **dseed.map_reflections(body.get("job_id"))})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/disposition/seed/save", methods=["POST"])
def disposition_seed_save():
    import disposition_seed as dseed
    body = request.get_json(silent=True) or {}
    with LOCK:
        res = dseed.save_seed(body.get("core") or [], replace=bool(body.get("replace")))
        _refresh_system_msg()
        return jsonify({"ok": True, **res})


# Gesichtserkennung: Kurations-Endpoints (rein beratend, keine Identitaets-Gewalt)
@app.route("/faces/crop/<path:name>")
def faces_crop(name):
    return send_from_directory(fr.FACES_CROP_DIR, name.replace("faces/", ""), mimetype="image/jpeg")


@app.route("/faces/state", methods=["GET"])
def faces_state():
    store = fr.load_faces()
    known = []
    for pid, entry in store.get("known", {}).items():
        known.append({
            "id": pid,
            "name": fr.person_display_name(pid, store),
            "faces": [{"crop": e.get("crop"), "added": e.get("added", "")} for e in entry.get("embeddings", [])],
        })
    unassigned = [{"id": u["id"], "crop": u["crop"], "ts": u.get("ts", "")} for u in store.get("unassigned", [])]
    people = [{"id": yc._people_slug(p.get("name", "")), "name": p.get("name")} for p in yc.load_people()]
    return jsonify({"enabled": bool(fr._cfg_faces("enabled", True)),
                    "known": known, "unassigned": unassigned, "people": people})


@app.route("/faces/assign", methods=["POST"])
def faces_assign():
    d = request.get_json(force=True) or {}
    target = {"new_person": d["new_person"]} if d.get("new_person") else d.get("target")
    store = fr.assign_unassigned(fr.load_faces(), d.get("crop_id"), target)
    fr.save_faces(store)
    return jsonify({"ok": True})


@app.route("/faces/delete", methods=["POST"])
def faces_delete():
    d = request.get_json(force=True) or {}
    store = fr.delete_face(fr.load_faces(), d.get("person_id"), d.get("crop_ref"))
    fr.save_faces(store)
    return jsonify({"ok": True})


@app.route("/faces/reassign", methods=["POST"])
def faces_reassign():
    d = request.get_json(force=True) or {}
    store = fr.reassign_face(fr.load_faces(), d.get("from_id"), d.get("crop_ref"), d.get("to_id"))
    fr.save_faces(store)
    return jsonify({"ok": True})


# Letztes per /see gezeigtes Foto (rohe JPEG-Bytes). "das bin ich" lernt daraus,
# statt capture_frame_web() zu nehmen - das griff die Default-Cam (config/cameras.json:
# default_source), i.d.R. eine Beobachtungs-/Decken-Cam OHNE frontales Gesicht. Das
# gezeigte Foto ist die Quelle, die Michael bewusst framt (front-facing BRIO).
_LAST_SEEN_FRAME = None


@app.route("/faces/bootstrap_michael", methods=["POST"])
def faces_bootstrap():
    frame = _LAST_SEEN_FRAME
    if not frame:
        return jsonify({"ok": False,
                        "error": "Zeig mir zuerst ein Foto von dir (📷 Zeigen), dann nochmal 'das bin ich'."}), 422
    ok = fr.store_bootstrap(frame)
    return jsonify({"ok": ok, "error": None if ok else "Kein Gesicht im gezeigten Foto erkannt"}), (200 if ok else 422)


@app.route("/resonance", methods=["GET"])
def resonance_list():
    """Read-only-Inspector fuer das Options-Modal (Resonanz v1). Liefert den
    authored Emotions-Kern (Anker + Vektoren), die Palette (Slot -> DE-Label/Mood/
    intim) und den aktuellen Multiplier. Kein Edit/Delete - der Kern ist authored
    und wird spaeter via Seed-Gespraech gepflegt, nicht im UI."""
    return jsonify({
        "ok": True,
        "anchors": yc.load_resonance_core(),
        "palette": yc._RESONANCE_PALETTE,
        "multiplier": yc.RESONANCE_MULTIPLIER,
        "enabled": yc.RESONANCE_ENABLED,
    })


@app.route("/resonance/multiplier", methods=["POST"])
def resonance_set_multiplier():
    """Live-Hebel: Frontend-Slider POSTet {value: 0.0..1.0}. Setter persistiert in
    memory/yuki_resonance_runtime.json + setzt die Modul-Var. Wirkt sofort beim
    naechsten Turn (resonance_tint_for_user_msg liest die Modul-Var pro Turn frisch -
    sitzt NICHT im gecachten SYSTEM_MSG, daher KEIN _refresh_system_msg noetig)."""
    body = request.get_json(silent=True) or {}
    raw = body.get("value")
    if raw is None:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    try:
        new_val = yc.set_resonance_multiplier(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "multiplier": new_val})


# --- Resonanz-Seed-Wizard (v1, 2026-07-01) -------------------------------------
# Gefuehrter, Canon-isolierter Flow (resonance_seed.py). Step-driven: jeder Schritt
# ist ein synchroner Call (kein SSE/Thread wie beim Adventure-Generator, weil Michael
# zwischen den Ankern aktiv weiterklickt). Reflexion = Ein-Schuss + Neu; Mapping am
# Ende ueber alle Anker; Save erst nach Dry-Run (mit Backup des alten Kerns).

@app.route("/resonance/seed/candidates", methods=["GET"])
def resonance_seed_candidates():
    """Vorschlags-Anker fuers Setup (Michael hakt ab / ergaenzt / benennt um)."""
    return jsonify({"ok": True, "anchors": resonance_seed.DEFAULT_ANCHORS})


@app.route("/resonance/seed/start", methods=["POST"])
def resonance_seed_start():
    """Wizard starten: baut den Voll-Selbst-Prompt einmal + legt den Job an.
    body={anchors:[{subject, aliases?}]}. Liefert job_id + normalisierte Anker."""
    body = request.get_json(silent=True) or {}
    try:
        jid, anchors = resonance_seed.start_seed(body.get("anchors"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "job_id": jid, "anchors": anchors})


@app.route("/resonance/seed/reflect", methods=["POST"])
def resonance_seed_reflect():
    """Ein-Schuss-Reflexion fuer einen Anker. body={job_id, index, steer?}. steer
    optional bei 'neu generieren'. Laeuft unter LOCK (LLM-Call serialisiert)."""
    body = request.get_json(silent=True) or {}
    jid = body.get("job_id")
    try:
        idx = int(body.get("index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "index fehlt/ungueltig"}), 400
    try:
        with LOCK:
            prose = resonance_seed.reflect_anchor(jid, idx, steer=body.get("steer"))
    except (KeyError, IndexError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
    return jsonify({"ok": True, "prose": prose, "index": idx})


@app.route("/resonance/seed/map", methods=["POST"])
def resonance_seed_map():
    """Mapping-Pass ueber alle reflektierten Anker -> Draft-Kern (editierbare Tabelle).
    body={job_id}."""
    body = request.get_json(silent=True) or {}
    try:
        with LOCK:
            draft = resonance_seed.map_reflections(body.get("job_id"))
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
    return jsonify({"ok": True, "anchors": draft})


@app.route("/resonance/seed/save", methods=["POST"])
def resonance_seed_save():
    """Redigierten Kern (aus der UI-Tabelle) speichern. body={anchors:[...]}. Sichert
    den alten Kern, schreibt, macht Dry-Run. Kein job_id noetig - die Tabelle ist die
    Quelle der Wahrheit nach dem Redigieren."""
    body = request.get_json(silent=True) or {}
    try:
        res = resonance_seed.save_core(body.get("anchors"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(res)


@app.route("/ha/devices", methods=["GET"])
def ha_devices_list():
    """Geraete-Inspektor fuers Options-Modal: alle konfigurierten HA-Geraete inkl.
    enabled-Flag (enabled zuerst, dann nach Name). configured=False wenn HA gar
    nicht eingerichtet ist (UI zeigt dann einen Hinweis)."""
    if not yc.ha_is_configured():
        return jsonify({"ok": True, "configured": False, "devices": []})
    devs = yc.ha_all_devices()
    devs.sort(key=lambda d: (not d.get("enabled", True), d.get("name", "").lower()))
    return jsonify({"ok": True, "configured": True, "devices": devs})


@app.route("/ha/devices/<path:entity_id>/enabled", methods=["POST"])
def ha_device_set_enabled(entity_id):
    """enabled-Flag eines Geraets live umschalten (kein Server-Restart). Body
    {enabled: true|false}. Wirkt ab dem naechsten Turn (world_context liest die
    Config neu)."""
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled", True))
    ok = yc.ha_set_device_enabled(entity_id, enabled)
    if not ok:
        return jsonify({"ok": False, "error": "Geraet nicht gefunden"}), 404
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/ha/devices/<path:entity_id>/name", methods=["POST"])
def ha_device_set_name(entity_id):
    """Anzeige-/Steuer-Namen eines Geraets live aendern (den Yuki im [ha:]-Marker
    nutzt). Body {name}. Kollisions-Schutz: ein anderer Eintrag darf den Namen nicht
    schon tragen (sonst waere die Namens-Aufloesung mehrdeutig)."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name leer"}), 400
    eid = (entity_id or "").strip().lower()
    for d in yc.ha_all_devices():
        if d.get("name", "").strip().lower() == name.lower() and \
           d.get("entity_id", "").lower() != eid:
            return jsonify({"ok": False, "error": f"Name „{name}“ ist schon vergeben"}), 409
    ok = yc.ha_set_device_name(entity_id, name)
    if not ok:
        return jsonify({"ok": False, "error": "Geraet nicht gefunden"}), 404
    return jsonify({"ok": True, "name": name})


@app.route("/ha/discover", methods=["POST"])
def ha_discover_endpoint():
    """Geraete-Discovery aus dem UI: neue schaltbare Entitaeten kommen disabled in
    die Config, verschwundene werden disabled (Body {prune:true} entfernt sie).
    Bestehende bleiben unangetastet."""
    if not yc.ha_is_configured():
        return jsonify({"ok": False, "error": "HA nicht konfiguriert"}), 503
    body = request.get_json(silent=True) or {}
    res = yc.ha_discover(prune=bool(body.get("prune", False)))
    return jsonify(res), (200 if res.get("ok") else 502)


# Voice-Hinweis (Home-Assistant-Conversation-Pfad): die Antwort wird ueber die
# Voice PE laut vorgelesen, nicht gelesen. Kurz halten, keine Marker/Listen/Code/
# Emoji-Spielereien - was nicht hoerbar ist, stoert nur. [ha:]-Marker (Licht etc.)
# darf sie weiter setzen; der wird serverseitig ausgewertet + aus der Sprachausgabe
# gestrippt. Siehe yuki-home-assistant-integration (Phase 2).
_HA_VOICE_HINT = (
    "\n\n[KONTEXT: Diese Antwort wird laut über einen Lautsprecher im Raum vorgelesen "
    "(Sprachsteuerung, kein Bildschirm). Halte dich KURZ – 1-3 gesprochene Sätze, keine "
    "Aufzählungen, kein Code, keine Emojis. Schreib so, wie du es sagen würdest. "
    "Smart-Home-Befehle (Licht etc.) per [ha:...]-Marker sind wie immer erlaubt.]")

# Sentinel-Origin fuer Voice-Turns: kein Browser hat diese client_id, also rendern
# ALLE offenen Web-Tabs den Turn mit (das chat_update-SSE skippt nur den Initiator).
_VOICE_CLIENT_ID = "__voice__"


def _ha_converse_token():
    """Optionaler Shared-Secret-Schutz fuer /ha/converse. Steht in der HA-Config
    (homeassistant.converse_token); fehlt er, ist der Endpoint offen (wie der Rest
    des LAN-BFF). Gesetzt -> das HA-custom_component muss ihn als X-Yuki-Token
    mitschicken, sonst kann kein anderes LAN-Geraet Yukis Gehirn fernsteuern."""
    try:
        return (yc.ha_converse_token() or "").strip()
    except Exception:
        return ""


@app.route("/ha/converse", methods=["POST"])
def ha_converse():
    """Headless Conversation-Endpoint fuer Home Assistant (Voice PE -> Assist-Pipeline
    -> Yuki). Nimmt nur Text, gibt nur Text zurueck. Teilt Yukis Gehirn (HISTORY/
    MEMORY/Personas/Memory-Tiers/Marker) mit /respond, laesst aber den UI-Ballast weg:
    keine TTS-Synthese (HA macht TTS via Piper/Wyoming), kein Audio-b64, kein Romaji/
    Furigana/Drawing-Pfad. [ha:]-Marker (Licht schalten) feuern - per Sprache das Licht
    steuern ist genau der Sinn. Timer werden unterdrueckt (wie Watch): die Voice PE
    ist kein Weck-Geraet, ein Timer toente nur im Web/Handy. Body {text, conversation_id?}.

    Auth: optionaler X-Yuki-Token (config homeassistant.converse_token), sonst offen
    wie der restliche LAN-BFF. Antwort {ok, reply} - reply ist der marker-freie,
    vorlesbare Text. Siehe yuki-home-assistant-integration (Phase 2)."""
    payload = request.get_json(silent=True) or {}
    user_text = (payload.get("text") or "").strip()
    if not user_text:
        return jsonify({"ok": False, "error": "leerer Text", "reply": ""}), 400

    want = _ha_converse_token()
    if want:
        got = request.headers.get("X-Yuki-Token", "") or (payload.get("token") or "")
        if got != want:
            return jsonify({"ok": False, "error": "unauthorized", "reply": ""}), 401

    with LOCK:
        HISTORY.append({"role": "user", "content": user_text, "persona": CURRENT_PERSONA, "ts": time.time()})
        try:
            sys_for_turn = SYSTEM_MSG
            if CURRENT_PERSONA not in ("kyoto", "tutor"):
                sys_for_turn += yc.steward_digest_block_for_prompt()
                sys_for_turn += yc.steward_thoughts_block_for_prompt()
            sys_for_turn += _HA_VOICE_HINT
            reply = yc.generate_reply(HISTORY, sys_for_turn,
                                      yc.persona_fewshot(CURRENT_PERSONA),
                                      yc.persona_reminder(CURRENT_PERSONA))
        except Exception as e:
            HISTORY.pop()  # fehlgeschlagenen User-Turn nicht behalten
            return jsonify({"ok": False, "error": f"LLM-Fehler: {e}", "reply": ""}), 502
        HISTORY.append({"role": "assistant", "content": reply, "persona": CURRENT_PERSONA, "ts": time.time()})
        _mood_snapshot = yc.load_mood()
        yuki_history_db.persist_message("michael", user_text,
                                        persona=CURRENT_PERSONA, mood=_mood_snapshot)
        yuki_history_db.persist_message("yuki", reply,
                                        persona=CURRENT_PERSONA, mood=_mood_snapshot)

        # Marker-Side-Effects: [ha:] Licht, [note:], [event:], [mood:] feuern.
        # (Kein async Action-Decider auf diesem Pfad - /ha/converse spawnt keine Pipeline,
        # also entsteht hier ohnehin kein Timer, der die Voice PE verwaisen liesse.)
        reply_clean, translation, furigana = _handle_marker_side_effects(reply)
        # Voice malt/pinnt nicht: ein etwaiges [draw:<svg>] sauber rausziehen (sonst
        # liefe rohes SVG in die Sprachausgabe), Rest via strip_all_markers.
        _drawing, reply_clean = yc.extract_draw_marker(reply_clean)
        reply_clean = yc.strip_all_markers(reply_clean)
        reply_clean = yc.tidy_reply_text(reply_clean)

        tokens = _tokens_for(reply_clean, furigana)
        _finalize_assistant_history(reply, translation, tokens, furigana)
        reply_model = (yc.get_last_reply_llm_stats() or {}).get("model") or yc.OLLAMA_MODEL
        if reply_model:
            _mm = HISTORY[-1].get("meta") or {}
            _mm["model"] = reply_model
            _mm["voice"] = True
            HISTORY[-1]["meta"] = _mm
        yc.save_history(HISTORY)

        # Web-UI live mitlaufen lassen: kein Browser hat _VOICE_CLIENT_ID -> alle
        # offenen Tabs rendern den Voice-Turn (User sieht im Chat, was er gesagt hat).
        display = yc.annotate_romaji(reply_clean)
        actions = _detect_actions(reply)
        _broadcast_sse({"kind": "chat_update", "target_client_id": _VOICE_CLIENT_ID,
                        "user_text": user_text, "reply": reply_clean, "display": display,
                        "translation": translation, "tokens": tokens, "furigana": furigana,
                        "drawing": None, "actions": actions,
                        "research": False, "secretary": False,
                        "research_lead": None, "research_body": None,
                        "persona": CURRENT_PERSONA, "model": reply_model,
                        "mood": yc.load_mood()})
        _emit_actions_for_reply(reply_clean, user_text,
                                target_client_id=None, note_source="michael", allow_timer=False)

    # Hintergrund-Maintenance NACH dem LOCK (Heart/Verdichtung/Facts) - Voice-Turns
    # sind echte Gespraeche, gehoeren in den Beziehungs-State wie /respond.
    _post_turn(user_text, reply_clean)

    print(f"  [🔊 Voice-Turn (HA): „{user_text[:60]}” -> {len(reply_clean)} Zeichen]", flush=True)
    return jsonify({"ok": True, "reply": reply_clean})


@app.route("/threads", methods=["GET"])
def threads_list():
    """Read-only-Inspector fuers Options-Modal (#27 Hebel 2). Liefert Liste
    sortiert (open zuerst, dann frischeste last_touched) + aktueller Multiplier."""
    entries = yc.load_threads()
    rank = {"open": 2, "dormant": 1, "closed": 0}
    entries_sorted = sorted(
        entries,
        key=lambda e: (rank.get(e.get("status"), 0),
                       e.get("last_touched_ts") or ""),
        reverse=True)
    return jsonify({
        "ok": True,
        "entries": entries_sorted,
        "multiplier": yc.THREADS_MULTIPLIER,
        "dormant_days": yc.THREADS_DORMANT_DAYS,
        "drop_days": yc.THREADS_DROP_DAYS,
    })


@app.route("/threads/<thread_id>", methods=["DELETE"])
def threads_delete(thread_id):
    """Einzel-Faden loeschen (Inspector-Edit). User-Korrektur falls das Gate
    was falsches als 'offen' eingetragen hat."""
    ok = yc.delete_thread(thread_id)
    if not ok:
        return jsonify({"ok": False, "error": "Thread nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/threads/<thread_id>/status", methods=["POST"])
def threads_set_status(thread_id):
    """Status eines Fadens manuell setzen (open/dormant/closed). User-Hebel:
    abgeschlossene Punkte als 'erledigt' markieren statt zu loeschen (closed
    surfacet nie, wird per Re-Touch nicht reaktiviert, faellt nach drop_days
    von selbst raus). 'dormant' = bewusst parken, 'open' = wieder aufgreifen.
    set_thread_status laesst last_touched_ts bei close/dormant stehen (Drop-Uhr
    laeuft ab letztem echten Kontakt) und bumpt es nur bei Re-Open."""
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    if status not in ("open", "dormant", "closed"):
        return jsonify({"ok": False, "error": "status muss open|dormant|closed sein"}), 400
    ok = yc.set_thread_status(thread_id, status, today=time.strftime("%Y-%m-%d"))
    if not ok:
        return jsonify({"ok": False, "error": "Thread nicht gefunden"}), 404
    return jsonify({"ok": True, "status": status})


@app.route("/threads/multiplier", methods=["POST"])
def threads_set_multiplier():
    """Live-Hebel: Frontend-Slider POSTet {value: 0.0..1.0}. Setter persistiert
    in memory/yuki_threads_runtime.json + setzt die Modul-Var. Der Surface-Block
    liest die Modul-Var pro Turn frisch (sitzt in build_messages, nicht im
    gecachten system_msg) - daher kein _refresh_system_msg noetig."""
    body = request.get_json(silent=True) or {}
    raw = body.get("value")
    if raw is None:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    try:
        new_val = yc.set_threads_multiplier(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "multiplier": new_val})


# --- Routinen (#30, 2026-06-27, Phase 1) --------------------------------------
# Wiederkehrende stille Vorsaetze. Verwaltungs-Liste im Options-Modal: anzeigen +
# heute-erledigt-Haken + Enable-/Proaktiv-Toggle + Loeschen. Anlegen laeuft in
# Phase 1 ueber den [routine:]-Marker (Yuki); ein Anlege-Formular kommt in Phase 4.
@app.route("/routines", methods=["GET"])
def routines_list():
    """Liste fuer den Routinen-Inspektor: rohe Eintraege + abgeleitete Flags
    (done_today/due_today), bereits sortiert (faellig-offen zuerst) + Multiplier."""
    return jsonify({"ok": True, "entries": yc.routines_view(),
                    "multiplier": yc.ROUTINES_MULTIPLIER,
                    "proactive_global": yc.ROUTINES_PROACTIVE_ENABLED})


@app.route("/routines", methods=["POST"])
def routines_create():
    """Neue Routine vom UI-Formular anlegen (Phase 4). Michael ist Autor -> er darf
    proactive direkt setzen (anders als die Yuki/Marker-angelegten, die mit
    proactive=False starten). Body: {label, recurrence?, band?, due_after?, proactive?}.
    Dedup auf Label (case-insensitive) wie create_routine: gleiches Label aktualisiert
    den bestehenden Eintrag statt zu duplizieren."""
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "Label fehlt"}), 400
    entry = yc.create_routine(label,
                              recurrence=body.get("recurrence", "daily"),
                              band=body.get("band", ""),
                              due_after=body.get("due_after", ""),
                              created_by="michael",
                              proactive=bool(body.get("proactive", False)))
    if not entry:
        return jsonify({"ok": False, "error": "Anlegen fehlgeschlagen"}), 400
    return jsonify({"ok": True, "routine": entry})


@app.route("/routines/<rid>", methods=["PUT"])
def routines_update(rid):
    """Felder einer Routine editieren (Phase 4 CRUD-Editor). Body: beliebige
    Teilmenge von {label, recurrence, band, due_after, proactive}; fehlende Felder
    bleiben unveraendert."""
    body = request.get_json(silent=True) or {}
    ok = yc.update_routine(rid,
                           label=body.get("label"),
                           recurrence=body.get("recurrence"),
                           band=body.get("band"),
                           due_after=body.get("due_after"),
                           proactive=body.get("proactive"))
    if not ok:
        return jsonify({"ok": False, "error": "Routine nicht gefunden / nichts zu aendern"}), 404
    return jsonify({"ok": True})


@app.route("/routines/<rid>", methods=["DELETE"])
def routines_delete(rid):
    """Einzel-Routine loeschen."""
    if not yc.delete_routine(rid):
        return jsonify({"ok": False, "error": "Routine nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/routines/<rid>/done", methods=["POST"])
def routines_set_done(rid):
    """Heute-erledigt-Haken setzen/entfernen. Body {done: true|false}. true ->
    fuer den logischen Tag erledigt, false -> Haken zuruecknehmen (Fehlklick)."""
    body = request.get_json(silent=True) or {}
    want = bool(body.get("done", True))
    ok = yc.mark_routine_done(rid, by="michael") if want else yc.clear_routine_done(rid)
    if not ok:
        return jsonify({"ok": False, "error": "Routine nicht gefunden"}), 404
    return jsonify({"ok": True, "done": want})


@app.route("/routines/<rid>/enabled", methods=["POST"])
def routines_set_enabled(rid):
    """Routine aktivieren/deaktivieren. Body {enabled: true|false}."""
    body = request.get_json(silent=True) or {}
    if not yc.set_routine_enabled(rid, bool(body.get("enabled", True))):
        return jsonify({"ok": False, "error": "Routine nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/routines/<rid>/proactive", methods=["POST"])
def routines_set_proactive(rid):
    """Proaktiven Push pro Routine an/aus. Body {proactive: true|false}. Greift
    erst in Phase 3 (Steward-Push), wird aber jetzt schon persistiert."""
    body = request.get_json(silent=True) or {}
    if not yc.set_routine_proactive(rid, bool(body.get("proactive", False))):
        return jsonify({"ok": False, "error": "Routine nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/routines/multiplier", methods=["POST"])
def routines_set_multiplier():
    """Live-Hebel (#30 Phase 2): Frontend-Slider POSTet {value: 0.0..1.0}. Setter
    persistiert in memory/yuki_routines_runtime.json + setzt die Modul-Var. Der
    Surface-Block liest die Modul-Var pro Turn frisch (sitzt in build_messages,
    nicht im gecachten system_msg) - daher kein _refresh_system_msg noetig."""
    body = request.get_json(silent=True) or {}
    raw = body.get("value")
    if raw is None:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    try:
        new_val = yc.set_routines_multiplier(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "multiplier": new_val})


@app.route("/routines/proactive_global", methods=["POST"])
def routines_set_proactive_global():
    """Globaler Master-Schalter (#30 Phase 3) fuer proaktive Push-Erinnerungen.
    Body {enabled: true|false}. Eine Routine pingt nur, wenn dieser Master AN ist
    UND ihr eigenes proactive-Flag AN ist. Aus = kein Routinen-Push (Liste/Chat
    laufen weiter). Live wirksam, der Steward-Tick liest die Modul-Var jede Runde."""
    body = request.get_json(silent=True) or {}
    new_val = yc.set_routines_proactive_enabled(bool(body.get("enabled", False)))
    return jsonify({"ok": True, "proactive_global": new_val})


# --- Vorsätze (gelernte Selbst-Vorsätze) ---
@app.route("/resolutions", methods=["GET"])
def resolutions_list():
    return jsonify({"ok": True, "entries": yc.resolutions_view(),
                    "multiplier": yc.RESOLUTIONS_MULTIPLIER,
                    "firm_threshold": yc.RESOLUTIONS_FIRM_THRESHOLD})

@app.route("/resolutions", methods=["POST"])
def resolutions_create():
    body = request.get_json(silent=True) or {}
    resolution = (body.get("resolution") or "").strip()
    if not resolution:
        return jsonify({"ok": False, "error": "resolution fehlt"}), 400
    entry = yc.create_resolution(body.get("cue", ""), resolution, source="michael")
    if not entry:
        return jsonify({"ok": False, "error": "Anlegen fehlgeschlagen"}), 400
    return jsonify({"ok": True, "resolution": entry})

@app.route("/resolutions/<rid>", methods=["PUT"])
def resolutions_update(rid):
    body = request.get_json(silent=True) or {}
    entry = yc.update_resolution(rid, cue=body.get("cue"), resolution=body.get("resolution"))
    if not entry:
        return jsonify({"ok": False, "error": "Vorsatz nicht gefunden"}), 404
    return jsonify({"ok": True, "resolution": entry})

@app.route("/resolutions/<rid>", methods=["DELETE"])
def resolutions_delete(rid):
    if not yc.delete_resolution(rid):
        return jsonify({"ok": False, "error": "Vorsatz nicht gefunden"}), 404
    return jsonify({"ok": True})

@app.route("/resolutions/<rid>/strengthen", methods=["POST"])
def resolutions_strengthen(rid):
    entry = yc.strengthen_resolution(rid)
    if not entry:
        return jsonify({"ok": False, "error": "Vorsatz nicht gefunden"}), 404
    return jsonify({"ok": True, "resolution": entry})

@app.route("/resolutions/<rid>/weaken", methods=["POST"])
def resolutions_weaken(rid):
    entry = yc.weaken_resolution(rid)
    if not entry:
        return jsonify({"ok": False, "error": "Vorsatz nicht gefunden"}), 404
    return jsonify({"ok": True, "resolution": entry})

@app.route("/resolutions/<rid>/star", methods=["POST"])
def resolutions_star(rid):
    body = request.get_json(silent=True) or {}
    entry = yc.set_resolution_starred(rid, bool(body.get("starred", True)))
    if not entry:
        return jsonify({"ok": False, "error": "Vorsatz nicht gefunden"}), 404
    return jsonify({"ok": True, "resolution": entry})

@app.route("/resolutions/multiplier", methods=["POST"])
def resolutions_set_multiplier():
    body = request.get_json(silent=True) or {}
    raw = body.get("value")
    if raw is None:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    try:
        new_val = yc.set_resolutions_multiplier(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "multiplier": new_val})


# --- People-Editor (Options-Inspektor, 2026-06-14) ----------------------------
# CRUD + Merge fuer den Beziehungs-Graphen. Mutierende Endpoints unter LOCK, weil
# Umbenennen/Mergen/Loeschen yuki_episodes.json migriert (das auch beim 30-Turn-
# Komprimieren geschrieben wird -> sonst Race).

@app.route("/people", methods=["GET"])
def people_list():
    """Read/Edit-Inspektor fuers Options-Modal. Personen nach Name sortiert."""
    people = sorted(yc.load_people(), key=lambda p: (p.get("name") or "").lower())
    return jsonify({"ok": True, "people": people})


@app.route("/people", methods=["POST"])
def people_create():
    """Manuell neue Person anlegen. ID eindeutig (name_2 bei Gleichheit)."""
    body = request.get_json(silent=True) or {}
    with LOCK:
        p, err = yc.create_person(
            body.get("name"),
            aliases=body.get("aliases"),
            relationship=body.get("relationship") or "",
            of=body.get("of") or "Michael")
    if err == "empty_name":
        return jsonify({"ok": False, "error": "Name darf nicht leer sein"}), 400
    if err == "cap":
        return jsonify({"ok": False, "error": "Personen-Limit erreicht"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "person": p})


@app.route("/people/<pid>", methods=["PUT"])
def people_update(pid):
    """Person editieren: name/aliases/relationship/of. Namensaenderung -> ID neu
    (eindeutig, _2 bei Kollision) + Cross-Ref-Migration (Episodes/Affinities)."""
    body = request.get_json(silent=True) or {}
    with LOCK:
        p, err = yc.update_person(
            pid,
            name=body.get("name"),
            aliases=body.get("aliases"),
            relationship=body.get("relationship"),
            of=body.get("of"))
    if err == "not_found":
        return jsonify({"ok": False, "error": "Person nicht gefunden"}), 404
    if err == "empty_name":
        return jsonify({"ok": False, "error": "Name darf nicht leer sein"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "person": p})


@app.route("/people/<pid>", methods=["DELETE"])
def people_delete(pid):
    """Person + ihre Cross-Refs loeschen."""
    with LOCK:
        ok = yc.delete_person(pid)
    if not ok:
        return jsonify({"ok": False, "error": "Person nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/people/<pid>/brick", methods=["DELETE"])
def people_delete_brick(pid):
    """Einen Brick (per Text) aus einer Person loeschen."""
    body = request.get_json(silent=True) or {}
    with LOCK:
        ok = yc.delete_person_brick(pid, body.get("text"))
    if not ok:
        return jsonify({"ok": False, "error": "Brick nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/people/<pid>/brick", methods=["POST"])
def people_add_brick(pid):
    """Manuell einen neuen Brick anlegen."""
    body = request.get_json(silent=True) or {}
    with LOCK:
        brick, err = yc.add_person_brick(pid, body.get("text"))
    if err == "not_found":
        return jsonify({"ok": False, "error": "Person nicht gefunden"}), 404
    if err == "empty":
        return jsonify({"ok": False, "error": "Text darf nicht leer sein"}), 400
    if err == "dup":
        return jsonify({"ok": False, "error": "Brick existiert schon"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "brick": brick})


@app.route("/people/<pid>/brick", methods=["PUT"])
def people_edit_brick(pid):
    """Brick-Text in-place editieren (added-Datum + Salience bleiben erhalten)."""
    body = request.get_json(silent=True) or {}
    with LOCK:
        brick, err = yc.edit_person_brick(pid, body.get("old_text"), body.get("new_text"))
    if err == "not_found":
        return jsonify({"ok": False, "error": "Brick nicht gefunden"}), 404
    if err == "empty":
        return jsonify({"ok": False, "error": "Text darf nicht leer sein"}), 400
    if err == "dup":
        return jsonify({"ok": False, "error": "Brick existiert schon"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "brick": brick})


@app.route("/people/brick/move", methods=["POST"])
def people_move_brick():
    """Einen Brick von source nach target verschieben (Dict wandert komplett mit)."""
    body = request.get_json(silent=True) or {}
    source_id = (body.get("source_id") or "").strip()
    target_id = (body.get("target_id") or "").strip()
    with LOCK:
        ok, err = yc.move_person_brick(source_id, target_id, body.get("text"))
    if not ok:
        msg = {"same": "Quelle und Ziel sind identisch",
               "not_found": "Person nicht gefunden",
               "brick_not_found": "Brick nicht gefunden"}.get(err, err or "Verschieben fehlgeschlagen")
        code = 404 if err in ("not_found", "brick_not_found") else 400
        return jsonify({"ok": False, "error": msg}), code
    return jsonify({"ok": True})


@app.route("/people/merge", methods=["POST"])
def people_merge():
    """Zwei Personen zusammenfuehren: source -> target (Aliase+Bricks+Cross-Refs
    wandern in target, source wird geloescht)."""
    body = request.get_json(silent=True) or {}
    source_id = (body.get("source_id") or "").strip()
    target_id = (body.get("target_id") or "").strip()
    with LOCK:
        ok, err = yc.merge_people(source_id, target_id)
    if not ok:
        msg = {"same": "Quelle und Ziel sind identisch",
               "not_found": "Person nicht gefunden"}.get(err, err or "Merge fehlgeschlagen")
        return jsonify({"ok": False, "error": msg}), 400
    # Gast-Verlauf-DB mitziehen: alte source_id auf target_id umschreiben, damit
    # geloggte Gespraeche der zusammengefuehrten Person erhalten + auffindbar bleiben.
    try:
        yuki_guest_db.reassign_identity(source_id, target_id)
    except Exception as e:
        print(f"  [Gast-DB reassign nach Merge fehlgeschlagen: {e}]")
    return jsonify({"ok": True})


# --- Heart-Kuratierung (Options -> 💝 Yuki -> ❤️ Herz-Kern) -----------------------
# Read-only Anzeige der aktiven Heart-Eintraege + Pin-Toggle. Gepinnte Eintraege
# ("Kern") stehen immer im Prompt (_heart_block pinned-first) und werden nie durch
# Overflow ins Archiv verdraengt. Michael kuratiert damit den dauerhaften Kern
# selbst, statt dem reinen Recency-Automatismus. (2026-07-03, [[yuki-heart-...]])

@app.route("/heart", methods=["GET"])
def heart_list():
    """Alle Heart-Eintraege fuers Overlay: aktive (neueste zuletzt) + archivierte
    (archived=True). So kann Michael auch verdraengte Alt-Wahrheiten sehen und via
    Pin zum Kern holen. pinned-Zaehler nur aus den aktiven."""
    active = [{"text": h.get("text", ""), "subject": h.get("subject", ""),
               "added": h.get("added", ""), "pinned": bool(h.get("pinned")),
               "archived": False}
              for h in yc.load_heart()]
    archived = [{"text": h.get("text", ""), "subject": h.get("subject", ""),
                 "added": h.get("added", ""), "pinned": False, "archived": True}
                for h in yc.load_heart_archived()]
    return jsonify({"ok": True, "items": active + archived,
                    "pinned": sum(1 for it in active if it["pinned"]),
                    "active": len(active), "archived_count": len(archived),
                    "cap": yc.HEART_MAX_ENTRIES, "in_prompt": yc.HEART_MAX_IN_PROMPT})


@app.route("/heart/pin", methods=["POST"])
def heart_pin():
    """Pinned-Flag auf einem aktiven Heart-Eintrag setzen/loeschen.
    Body: {text, subject, pinned:bool}."""
    body = request.get_json(silent=True) or {}
    with LOCK:
        ok = yc.set_heart_pin(body.get("text"), body.get("subject"),
                              bool(body.get("pinned")))
    if not ok:
        return jsonify({"ok": False, "error": "Eintrag nicht gefunden"}), 404
    return jsonify({"ok": True})


# --- Lebenserinnerungen-Editor (Options -> 💝 Yuki -> 📖 Lebenserinnerungen) ------
# Read-only Tier (kein Auto-Write), aber per Editor voll pflegbar. core = always-on
# Backstory-Anker (faellt in BASE_RULES), entries = keyword-selektiver Pool.

@app.route("/lore", methods=["GET"])
def lore_list():
    """Core + Entries fuer den Editor."""
    lore = yc.load_lore()
    return jsonify({"ok": True, "core": lore.get("core", []),
                    "entries": lore.get("entries", [])})


@app.route("/lore/core", methods=["POST"])
def lore_core_add():
    body = request.get_json(silent=True) or {}
    with LOCK:
        brick, err = yc.add_lore_core(body.get("text"))
    if err == "empty":
        return jsonify({"ok": False, "error": "Text darf nicht leer sein"}), 400
    if err == "dup":
        return jsonify({"ok": False, "error": "Eintrag existiert schon"}), 400
    if err == "cap":
        return jsonify({"ok": False, "error": "Core-Limit erreicht"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "brick": brick})


@app.route("/lore/core", methods=["PUT"])
def lore_core_edit():
    body = request.get_json(silent=True) or {}
    with LOCK:
        brick, err = yc.update_lore_core(body.get("old_text"), body.get("new_text"))
    if err == "not_found":
        return jsonify({"ok": False, "error": "Eintrag nicht gefunden"}), 404
    if err == "empty":
        return jsonify({"ok": False, "error": "Text darf nicht leer sein"}), 400
    if err == "dup":
        return jsonify({"ok": False, "error": "Eintrag existiert schon"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "brick": brick})


@app.route("/lore/core", methods=["DELETE"])
def lore_core_delete():
    body = request.get_json(silent=True) or {}
    with LOCK:
        ok = yc.delete_lore_core(body.get("text"))
    if not ok:
        return jsonify({"ok": False, "error": "Eintrag nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/lore/entry", methods=["POST"])
def lore_entry_add():
    body = request.get_json(silent=True) or {}
    with LOCK:
        entry, err = yc.add_lore_entry(body.get("text"), keywords=body.get("keywords"),
                                       era=body.get("era") or "")
    if err == "empty":
        return jsonify({"ok": False, "error": "Text darf nicht leer sein"}), 400
    if err == "cap":
        return jsonify({"ok": False, "error": "Erinnerungs-Limit erreicht"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "entry": entry})


@app.route("/lore/entry/<eid>", methods=["PUT"])
def lore_entry_edit(eid):
    body = request.get_json(silent=True) or {}
    with LOCK:
        entry, err = yc.update_lore_entry(eid, text=body.get("text"),
                                          keywords=body.get("keywords"),
                                          era=body.get("era"))
    if err == "not_found":
        return jsonify({"ok": False, "error": "Erinnerung nicht gefunden"}), 404
    if err == "empty":
        return jsonify({"ok": False, "error": "Text darf nicht leer sein"}), 400
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "entry": entry})


@app.route("/lore/entry/<eid>", methods=["DELETE"])
def lore_entry_delete(eid):
    with LOCK:
        ok = yc.delete_lore_entry(eid)
    if not ok:
        return jsonify({"ok": False, "error": "Erinnerung nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/timers", methods=["GET"])
def timers_list():
    """Snapshot aller laufenden Timer fuers UI-Panel. Restzeit in Sekunden ist
    bereits ausgerechnet (UI tickt 1s und zaehlt selbst weiter runter, der Server
    bleibt dadurch state-arm)."""
    return jsonify({"ok": True, "timers": yc.list_timers()})


@app.route("/timers/<tid>", methods=["DELETE"])
def timers_cancel(tid):
    """Timer per ID stoppen. 404 falls schon abgelaufen oder unbekannt."""
    ok = yc.cancel_timer(tid)
    if not ok:
        return jsonify({"ok": False, "error": "Timer nicht gefunden"}), 404
    # Frontend darf den Eintrag schon lokal entfernen; trotzdem SSE pushen damit
    # andere offene Tabs ihre Liste aktualisieren.
    _broadcast_sse({"kind": "timer_cancel", "id": tid})
    return jsonify({"ok": True})


@app.route("/habits", methods=["GET"])
def habits_list():
    """Habit-Summary fuers Options-Modal-Panel. Liefert alle erkannten
    Habits inkl. count/category/concern, sortiert nach concern_score.
    Pro Eintrag visible_in_prompt-Flag damit das UI die Prompt-sichtbaren
    Habits (= Top-N >= MIN_CONCERN) optisch abheben kann, ohne die Threshold-
    Logik zu duplizieren."""
    rows = yuki_habits_db.get_summary(min_concern=0.0)
    top_n = yc.HABITS_PROMPT_TOP_N
    min_c = yc.HABITS_PROMPT_MIN_CONCERN
    starred = yuki_habits_db.list_starred()
    # Rows sind nach concern_score sortiert (DESC). Starred-Habits sind IMMER
    # visible und zaehlen NICHT zum top_n-Budget - sie sind on top. So sieht
    # die UI dasselbe wie _habits_block (Prompt-Render) an Yuki schickt:
    # 1 starred + 10 Top-N => 11 visible. Konsistent.
    non_starred_visible = 0
    for r in rows:
        key = (r.get("habit_key"), r.get("subject"))
        is_starred = key in starred
        if is_starred:
            r["starred"] = True
            r["starred_note"] = starred[key]
            r["visible_in_prompt"] = True
        else:
            r["starred"] = False
            r["starred_note"] = None
            r["visible_in_prompt"] = (non_starred_visible < top_n
                                       and (r.get("concern_score") or 0) >= min_c)
            if r["visible_in_prompt"]:
                non_starred_visible += 1
    # Resort: starred-Habits zuerst (das UI rendert in Reihenfolge der Liste);
    # innerhalb starred und non-starred bleibt concern_score DESC erhalten.
    rows.sort(key=lambda r: (not r["starred"], -(r.get("concern_score") or 0)))
    return jsonify({
        "ok": True,
        "habits": rows,
        "prompt_top_n": top_n,
        "prompt_min_concern": min_c,
    })


@app.route("/habits", methods=["DELETE"])
def habits_delete():
    """Habit aus dem Bestand entfernen. Body: {habit_key, subject}.
    Loescht alle Occurrences + die Summary-Zeile. Triggert _refresh_system_msg()
    damit der HABITS-Block in Yukis Prompt sofort den neuen Stand spiegelt."""
    body = request.get_json(silent=True) or {}
    key = (body.get("habit_key") or "").strip().lower()
    subj = (body.get("subject") or "").strip().lower()
    if not key or subj not in ("michael", "yuki"):
        return jsonify({"ok": False, "error": "habit_key/subject erforderlich"}), 400
    n = yuki_habits_db.delete_habit(key, subj)
    if n == 0:
        return jsonify({"ok": False, "error": "Habit nicht gefunden"}), 404
    _refresh_system_msg()
    return jsonify({"ok": True, "deleted_occurrences": n})


@app.route("/habits/disable", methods=["POST"])
def habits_disable():
    """Habit dauerhaft ignorieren (Vorrang vor Gate). Body: {habit_key, subject,
    reason?}. Entfernt auch den aktuellen Bestand, damit Yuki den Habit sofort
    aus dem Prompt verliert."""
    body = request.get_json(silent=True) or {}
    key = (body.get("habit_key") or "").strip().lower()
    subj = (body.get("subject") or "").strip().lower()
    reason = (body.get("reason") or "").strip() or None
    if not key or subj not in ("michael", "yuki"):
        return jsonify({"ok": False, "error": "habit_key/subject erforderlich"}), 400
    ok = yuki_habits_db.disable_habit(key, subj, reason)
    if not ok:
        return jsonify({"ok": False, "error": "disable fehlgeschlagen"}), 500
    _refresh_system_msg()
    return jsonify({"ok": True})


@app.route("/habits/enable", methods=["POST"])
def habits_enable():
    """Disabled-Eintrag entfernen - kuenftige Vorkommen koennen wieder
    registriert werden (bisheriger Verlauf bleibt verloren)."""
    body = request.get_json(silent=True) or {}
    key = (body.get("habit_key") or "").strip().lower()
    subj = (body.get("subject") or "").strip().lower()
    if not key or subj not in ("michael", "yuki"):
        return jsonify({"ok": False, "error": "habit_key/subject erforderlich"}), 400
    ok = yuki_habits_db.enable_habit(key, subj)
    if not ok:
        return jsonify({"ok": False, "error": "war nicht deaktiviert"}), 404
    return jsonify({"ok": True})


@app.route("/habits/disabled", methods=["GET"])
def habits_disabled_list():
    """User-Disabled-Liste fuers UI-Panel."""
    return jsonify({"ok": True, "disabled": yuki_habits_db.get_disabled_meta()})


@app.route("/habits/star", methods=["POST"])
def habits_star():
    """Habit anpinnen - landet immer im Prompt unabhaengig von concern_score
    oder Top-N. Body: {habit_key, subject, note?}. Idempotent (erneuter Aufruf
    ueberschreibt Notiz)."""
    body = request.get_json(silent=True) or {}
    key = (body.get("habit_key") or "").strip().lower()
    subj = (body.get("subject") or "").strip().lower()
    note = body.get("note")
    if isinstance(note, str):
        note = note.strip() or None
    else:
        note = None
    if not key or subj not in ("michael", "yuki"):
        return jsonify({"ok": False, "error": "habit_key/subject erforderlich"}), 400
    ok = yuki_habits_db.star_habit(key, subj, note)
    if not ok:
        return jsonify({"ok": False, "error": "star fehlgeschlagen"}), 500
    _refresh_system_msg()
    return jsonify({"ok": True})


@app.route("/habits/unstar", methods=["POST"])
def habits_unstar():
    """Stern entfernen. Habit faellt zurueck auf normale concern_score-Logik."""
    body = request.get_json(silent=True) or {}
    key = (body.get("habit_key") or "").strip().lower()
    subj = (body.get("subject") or "").strip().lower()
    if not key or subj not in ("michael", "yuki"):
        return jsonify({"ok": False, "error": "habit_key/subject erforderlich"}), 400
    ok = yuki_habits_db.unstar_habit(key, subj)
    if not ok:
        return jsonify({"ok": False, "error": "war nicht angepinnt"}), 404
    _refresh_system_msg()
    return jsonify({"ok": True})


@app.route("/habits/starred", methods=["GET"])
def habits_starred_list():
    """User-Starred-Liste fuers UI-Panel (mit Notiz + Timestamp)."""
    return jsonify({"ok": True, "starred": yuki_habits_db.get_starred_meta()})


@app.route("/habits/recompute", methods=["POST"])
def habits_recompute():
    """Force-Recompute aller Habit-Summaries (nach Profil-Edits in
    habit_profiles.json). Auch apply_aliases vorab, falls neue Aliase
    eingefuegt wurden."""
    aliased = yuki_habits_db.apply_aliases_to_existing()
    n = yuki_habits_db.recompute_summary()
    _refresh_system_msg()
    return jsonify({"ok": True, "aliased": aliased, "habits": n})


@app.route("/lookup")
def lookup():
    """Wadoku-Lookup fuers JP-Gloss-Popup. Query-Params:
      q      = surface form (Pflicht, kommt vom Token-Tap im Frontend)
      lemma  = optional lemma (Fallback wenn surface kein Treffer hat)
    Antwort: {ok, results: [{id, pos, reading, glosses: [...]}, ...]}.
    Bei nicht-verfuegbarer DB: 503 - das UI versteckt dann das Popup-Feature."""
    if not wadoku.is_available():
        return jsonify({"ok": False, "error": "Wadoku-DB nicht verfuegbar"}), 503
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "q fehlt"}), 400
    lemma = (request.args.get("lemma") or "").strip() or None
    results = wadoku.lookup(q, lemma=lemma, limit=3)
    # Ruby-Paare pro Treffer berechnen: surface (q) gegen die jeweilige Reading
    # alignen, sodass das Frontend im Popup-Header die Hiragana ueber die Kanji
    # des Surface rendern kann (analog Furigana-Marker im Chat). Bei reinen Kana-
    # Surfaces gibt ruby_pairs_for_token None zurueck - Frontend zeigt dann den
    # Plaintext-Surface wie bisher.
    for r in results:
        ruby = yc.ruby_pairs_for_token(q, r.get("reading"))
        if ruby:
            r["ruby"] = ruby
    # Romaji des Surface fuer die Popup-Header-Zeile - gleicher Romanizer wie
    # in annotate_romaji (chat-bubble-Romaji). Wenn fugashi/jaconv nicht da
    # sind, leerer String und das Frontend zeigt die Zeile nicht.
    romaji = yc._romanize_jp(q) if yc._tagger and q else ""
    return jsonify({"ok": True, "results": results, "q": q, "lemma": lemma,
                    "romaji": romaji})


@app.route("/kanji/<ch>")
def kanji_detail(ch):
    """JP-Char-Detail-Lookup fuers erweiterte Wadoku-Popup. Path-Param ist genau
    EIN JP-Zeichen (UTF-8 in der URL); das Frontend codiert es per
    encodeURIComponent. Endpoint-Name ist historisch 'kanji', deckt aber seit
    2026-06-05 auch Kana ab.

    Antwort:
      * Kanji: {ok, kanji: {kind:'kanji', literal, codepoint, stroke_count, jlpt,
                            grade, freq, radical, on_readings, kun_readings,
                            meanings_en, has_svg, svg_url|None}}
      * Kana:  {ok, kanji: {kind:'hiragana'|'katakana', literal, codepoint,
                            has_svg:true, svg_url}} - keine Meta-Felder, weil
                fuer Kana weder KANJIDIC2-Daten noch JLPT existieren.
    400 wenn weder Kanji noch Kana; 404 wenn Kanji nicht in DB / Kana ohne SVG;
    503 wenn DB komplett fehlt."""
    if not kanjidict.is_available():
        return jsonify({"ok": False, "error": "Kanji-DB nicht verfuegbar"}), 503
    if not ch or len(ch) != 1:
        return jsonify({"ok": False, "error": "ungueltig"}), 400
    if kanjidict.is_kanji_char(ch):
        data = kanjidict.lookup_kanji(ch)
        if data is None:
            return jsonify({"ok": False, "error": "Kanji nicht in DB"}), 404
        data["kind"] = "kanji"
        if data.get("has_svg"):
            data["svg_url"] = f"/kanjivg/{data['codepoint']:05x}.svg"
        return jsonify({"ok": True, "kanji": data})
    if kanjidict.kana_kind(ch) is not None:
        kdata = kanjidict.lookup_kana(ch)
        if kdata is None:
            # Kana ohne SVG (Choon, Mittelpunkt, Iterationsmarken etc.) - kein 503,
            # einfach 404 damit der Frontend-Slot still entfernt wird.
            return jsonify({"ok": False, "error": "kein SVG fuer dieses Zeichen"}), 404
        kdata["svg_url"] = f"/kanjivg/{kdata['codepoint']:05x}.svg"
        return jsonify({"ok": True, "kanji": kdata})
    return jsonify({"ok": False, "error": "kein Kanji/Kana"}), 400


@app.route("/kanjivg/<path:fname>")
def kanjivg_svg(fname):
    """Liefert KanjiVG-SVGs aus data/kanjivg/. Filename muss reines '<hex>.svg'
    sein (5-stelliger lowercase-Codepoint, keine Subpfade). send_from_directory
    blockt Pfad-Traversal automatisch. 404 wenn Datei fehlt."""
    # Defensive: nur reine <hex>.svg-Namen, kein '/' oder '..'.
    if not fname.endswith(".svg") or "/" in fname or ".." in fname:
        return jsonify({"ok": False, "error": "ungueltiger Filename"}), 400
    if not kanjidict.has_strokes():
        return jsonify({"ok": False, "error": "KanjiVG nicht installiert"}), 503
    return send_from_directory(str(kanjidict.SVG_DIR), fname, mimetype="image/svg+xml")


# Kana-Schreibuebung (Tutor, Thema 1 Phase 1, 2026-06-16). Drill: der Anfaenger
# malt ein angesagtes Hiragana aufs Canvas, das VLM prueft die FORM (keine
# Strichordnung - das ist Phase 2). Bewusst ORTHOGONAL zum Chat: kein LOCK
# (describe_image ist ein eigenstaendiger Vision-Call ohne HISTORY/Whisper-
# Zugriff), KEIN Schreiben in conversation/facts/memory - eine Uebung ist kein
# erinnerungswuerdiger Turn (analog Adventure-Engine).
# 4-stufige Bewertung statt binaer (User-Wunsch 2026-06-18): der 1.6B-VLM rundet
# marginale Zeichnungen sonst zu MATCH hoch, weil ihm bei Ja/Nein das Ventil fuer
# "grenzwertig" fehlt. Die Mittelstufe "erkennbar" faengt genau die ab. Aufsteigende
# Qualitaet; der Index dient als Bestehens-Schwelle.
KANA_GRADES = ("schlecht", "erkennbar", "passend", "gut")
# engl. VLM-Token -> interne Stufe (Token bewusst kollisionsfrei: kein Substring des anderen)
_KANA_GRADE_TOKENS = {"GOOD": "gut", "FITTING": "passend",
                      "RECOGNIZABLE": "erkennbar", "BAD": "schlecht"}
# Bestehens-Schwelle als Index in KANA_GRADES, gekoppelt an den Tutor-Grad:
# Anfaenger (Wort/Phrase) besteht ab "passend", Profi (Satz/Profi) nur bei "gut".
_KANA_PASS_IDX = {False: KANA_GRADES.index("passend"), True: KANA_GRADES.index("gut")}


def _kana_feedback(grade, passt, kana, romaji, guess):
    """Deterministisches DE-Feedback aus der 4-stufigen (englischen) VLM-Bewertung -
    so haengt die Freundlichkeit nicht an der wackeligen DE-Faehigkeit des 1.6B-VLM.
    guess = was das Modell stattdessen gelesen hat (oder leer/'unklar')."""
    if passt:
        if grade == "gut":
            return f"Schön! {kana} ({romaji}) ist klar und sauber getroffen. 🌸"
        return f"Das passt — {kana} ({romaji}) ist gut zu erkennen. 🌸"
    if grade == "passend":
        # nur im Profi-Bucket erreichbar: korrekt lesbar, aber fuer die strenge Stufe zu unsauber
        return (f"Schon richtig als {kana} ({romaji}) lesbar — für die Profi-Stufe darf es "
                f"aber noch einen Tick sauberer und gleichmäßiger sein.")
    if grade == "erkennbar":
        return (f"Fast! Ich erkenne, dass {kana} ({romaji}) gemeint ist, aber die Form ist "
                f"noch nicht sauber. Schau dir die Vorlage nochmal an und achte auf die "
                f"Proportionen. 🙂")
    # schlecht
    g = (guess or "").strip()
    low = g.lower()
    if not g or any(w in low for w in ("unklar", "unsure", "unreadable", "unknown", "unclear", "nicht", "none")):
        return (f"Das kann ich noch nicht als {kana} ({romaji}) lesen — versuch die "
                f"Striche deutlicher und größer, und schau dir die Vorlage nochmal an.")
    return (f"Hmm, das sieht für mich eher nach „{g}“ aus als nach {kana} ({romaji}). "
            f"Schau dir die Vorlage nochmal an und probier's nochmal. 🙂")


@app.route("/tutor/kana_check", methods=["POST"])
def tutor_kana_check():
    """Bewertet ein handgemaltes Kana. Multipart: 'image' (JPEG vom Canvas, schwarze
    Striche auf weiss) + Form 'kana' (das Ziel-Zeichen) + 'romaji'. Antwort:
    {ok, passt, grade, feedback, raw, engine, bucket, progress}. Urteilt ueber das
    Haupt-LLM (gemma >=12B, multimodal) wenn verfuegbar, sonst LFM2.5-VL; 503 nur wenn
    beide Wege aus sind. engine = 'gemma'|'lfm2vl' (welcher Pfad geantwortet hat)."""
    f = request.files.get("image")
    kana = (request.form.get("kana") or "").strip()
    romaji = (request.form.get("romaji") or "").strip()
    if f is None:
        return jsonify({"ok": False, "error": "kein Bild empfangen"}), 400
    if not kana:
        return jsonify({"ok": False, "error": "kein Ziel-Kana angegeben"}), 400
    data = f.read()
    if not data:
        return jsonify({"ok": False, "error": "leeres Bild"}), 400

    # Strenge an den Tutor-Schwierigkeitsgrad koppeln (User-Idee 2026-06-16):
    # Wort/Phrase (Anfaenger) = nachsichtig (Anfaenger-Handschrift ok), Satz/Profi
    # = strenger Maszstab. load_tutor_level() ist die persistierte Single-Source.
    strict = yc.load_tutor_level() in ("intermediate", "advanced")
    intro = (
        "This image shows a single Japanese hiragana character that a beginner drew "
        "by hand with black strokes on a white background. The drawing was scaled to "
        "fill the frame, so judge the SHAPE, not the size. The character they were "
        f"asked to draw is \"{kana}\"" + (f" (romaji: \"{romaji}\")" if romaji else "") + ".\n"
        "Some hiragana are just one or two simple strokes; for those, judge mainly by the "
        "overall shape, curvature and direction of the stroke(s), not by fine detail.\n"
    )
    # 4-stufige Rubrik statt MATCH/NOMATCH: gibt dem kleinen VLM eine Mittelstufe,
    # damit es schlampige Treffer nicht zu "korrekt" hochrundet (siehe KANA_GRADES).
    rubric = (
        f"Grade how well the drawing matches the target hiragana \"{kana}\" on this scale:\n"
        "- GOOD: correctly formed, all main strokes present and well arranged, clean and balanced.\n"
        "- FITTING: correct shape with all main strokes present and in the right place; "
        "normal beginner unevenness is fine.\n"
        "- RECOGNIZABLE: you can tell which character was intended, but it is clearly flawed "
        "- wrong proportions, a stroke missing/extra/misplaced, or distorted.\n"
        "- BAD: not readable as the target - key strokes missing, wrong overall shape, or it "
        "looks more like a different hiragana.\n"
    )
    if strict:
        rubric += ("Judge strictly, like a writing teacher: give GOOD or FITTING only when "
                   "the form is genuinely correct, not just roughly recognizable.\n")
    prompt = intro + rubric + (
        "Answer with EXACTLY ONE line, nothing else: one of GOOD, FITTING, RECOGNIZABLE, BAD.\n"
        "If it is RECOGNIZABLE or BAD, you MAY append \" | \" and the single hiragana it "
        "actually looks most like (or the word unclear)."
    )
    # Engine-Routing (2026-06-18): hat das aktive Ollama-Modell genug Power + Bilder-
    # Faehigkeit (gemma >=12B), urteilt es DIREKT - deutlich besseres Form-/Ausrichtungs-
    # Verstaendnis als das 1.6B-LFM2.5-VL, genau Michaels Kurven-Pingeligkeit. Sonst (oder
    # wenn das Haupt-LLM patzt) der bewaehrte LFM2.5-VL-Pfad als Fallback. Auto-Vision +
    # Foto-Pfad bleiben BEWUSST auf LFM2.5-VL (kein GPU-Contention mit dem Chat, dort reicht
    # das kleine Modell). On-demand Kana-Check funkt nicht in laufende Chats rein.
    kana_sys = ("You are a Japanese writing teacher grading a single handwritten hiragana. "
                "Follow the instructions exactly and answer in the requested one-line format.")
    if yc.vision_via_main_llm_capable():
        engine = "gemma"
        raw = yc.describe_image_via_main_llm(data, prompt, system=kana_sys, max_tokens=48,
                                             purpose="kana_check")
        if raw is None:                     # Haupt-LLM gepatzt -> harter Fallback aufs VLM
            engine = "lfm2vl"
            raw = yc.describe_image(data, prompt=prompt, max_tokens=40)
    else:
        engine = "lfm2vl"
        raw = yc.describe_image(data, prompt=prompt, max_tokens=40)
    if raw is None:
        return jsonify({"ok": False,
                        "error": "Bewertung nicht verfuegbar (kein bilderfaehiges LLM und "
                                 "kein Vision-Server :8081 erreichbar)"}), 503

    up = raw.strip().upper()
    # Frueheste Stufe im Text gewinnt (robust gegen Geschwafel); unparsebar -> "schlecht".
    hits = [(up.find(tok), g) for tok, g in _KANA_GRADE_TOKENS.items() if tok in up]
    grade = min(hits)[1] if hits else "schlecht"
    passt = KANA_GRADES.index(grade) >= _KANA_PASS_IDX[strict]
    guess = ""
    if not passt and "|" in raw:
        guess = raw.split("|", 1)[1].strip()
    feedback = _kana_feedback(grade, passt, kana, romaji, guess)
    # Fortschritt verbuchen: getrennt nach Stufe (strict == Profi-Bucket). Das
    # aktualisierte Cell geht zurueck, damit das Pad den Punkt sofort umfaerben kann.
    bucket = "profi" if strict else "anfaenger"
    progress = yc.record_kana_attempt(kana, bucket, passt)
    return jsonify({"ok": True, "passt": passt, "grade": grade, "feedback": feedback,
                    "raw": raw, "engine": engine, "bucket": bucket, "progress": progress})


@app.route("/tutor/kana_progress")
def tutor_kana_progress():
    """Fortschritts-Overview fuer die Kana-Anzeige: {kana: {anfaenger|profi: {score,
    attempts, color}}} plus der aktuell aktive Bucket (folgt der Tutor-Schwierigkeit)."""
    return jsonify({"ok": True,
                    "bucket": yc.kana_bucket_for_level(),
                    "progress": yc.kana_progress_overview()})


# ===========================================================================
# Vokabel-Quiz (🃏 Drill-Bereich, nur Tutor). Sitzt auf dem vorhandenen SRS-
# Scheduling (vocab_due/vocab_grade) auf. ISOLIERT vom Chat: schreibt NICHTS in
# conversation.json / Facts / Memory - nur vocab_grade mutiert yuki_vocab.json
# (das SM-2-Scheduling). Yuki bewertet jede Antwort + reagiert in-character
# (grade_quiz_answer, slim _quiz-Persona). Siehe [[yuki-srs]].
# ===========================================================================

@app.route("/vocab/stats")
def vocab_stats_route():
    """Kennzahlen fuers UI-Badge + Setup: {total, due}."""
    return jsonify({"ok": True, "stats": yc.quiz_stats()})


@app.route("/vocab/due")
def vocab_due_deck():
    """Baut ein Quiz-Deck (faellige Karten zuerst, dann random-fill). Optional
    ?n=<groesse> und ?direction=mixed|jp2de|de2jp (sonst Config-Defaults). Jede
    Karte traegt ihre Richtung mit - der Client schickt sie bei /vocab/answer
    zurueck. Liefert {deck:[{id,jp,de,example,direction,due}], stats}."""
    size = request.args.get("n", type=int)
    direction = (request.args.get("direction") or "").strip().lower() or None
    # MC-Hilfe (nur Anzeige) bei niedriger Tutor-Schwierigkeit - analog zur Kana-
    # Strenge (intermediate/advanced = streng/ohne Hilfe). Antwort wird trotzdem
    # getippt/gesprochen.
    easy = yc.load_tutor_level() in ("absolute_beginner", "beginner")
    deck = yc.build_quiz_deck(session_size=size, direction_mode=direction, with_choices=easy)
    return jsonify({"ok": True, "deck": deck, "stats": yc.quiz_stats(), "choices": easy})


@app.route("/vocab/answer", methods=["POST"])
def vocab_answer():
    """Bewertet Michaels Antwort auf eine Quiz-Karte und spielt die Note ins SRS
    zurueck. Multipart-Form:
      - id (Pflicht): Vokabel-ID aus dem Deck.
      - direction: 'jp2de'|'de2jp' (wie die Karte gezeigt wurde).
      - text: getippte Antwort  ODER
      - audio: gesprochene Antwort (Blob) -> STT (Sprach-Hint aus direction:
        de2jp -> ja, jp2de -> de; ueberschreibbar via 'language').
    Karte wird autoritativ aus dem Pool geladen (Client-jp/de wird nicht getraut).
    Antwort: {ok, verdict, reaction, correct_answer, heard, interval_days, due_at,
    used_fallback}."""
    entry_id = (request.form.get("id") or "").strip()
    direction = (request.form.get("direction") or "jp2de").strip().lower()
    if direction not in ("jp2de", "de2jp"):
        direction = "jp2de"
    entry = next((v for v in yc.load_vocab() if v.get("id") == entry_id), None)
    if not entry:
        return jsonify({"ok": False, "error": "Vokabel nicht gefunden"}), 404
    card = {"id": entry["id"], "jp": entry["jp"], "de": entry["de"],
            "example": entry.get("example"), "direction": direction}

    answer_text = (request.form.get("text") or "").strip()
    stt_used = False
    af = request.files.get("audio")
    if not answer_text and af is not None:
        data = af.read()
        if data:
            lang = (request.form.get("language") or "").strip().lower()
            if lang not in ("de", "en", "ja"):
                lang = "ja" if direction == "de2jp" else "de"
            # de2jp = gesprochene JP-Antwort: rohen Whisper-Output behalten
            # (Halluzinations-Filter aus), damit ein akzentuiertes Wort als
            # Kana-Diff-Signal ankommt statt als "" (der Judge wertet Garble eh
            # als falsch). jp2de laeuft mit Filter wie bisher.
            stt_filter = direction != "de2jp"
            try:
                with LOCK:                          # faster-whisper MODEL ist nicht threadsafe
                    text, _, _, _ = yc.transcribe_bytes(
                        MODEL, data, language=lang, filter_hallucination=stt_filter)
                answer_text = (text or "").strip()
                stt_used = True
            except Exception as e:
                return jsonify({"ok": False, "error": f"STT-Fehler: {e}"}), 500

    # Bewerten (LLM-Judge mit Fuzzy-Fallback). KEIN LOCK - on-demand Tutor-Call wie
    # kana_check, funkt nicht in einen laufenden Chat-Turn rein.
    result = yc.grade_quiz_answer(card, answer_text)
    graded = yc.vocab_grade(entry_id, result["signal"])

    resp = {"ok": True,
            "verdict": result["verdict"],
            "reaction": result["reaction"],
            "correct_answer": result["correct_answer"],
            "heard": answer_text if stt_used else None,
            "used_fallback": result["used_fallback"]}
    if graded:
        resp["interval_days"] = graded.get("interval_days")
        resp["due_at"] = graded.get("due_at")
    # Aussprache-Tipp bei gesprochener, nicht-korrekter JP-Antwort (Kana-Diff wie
    # im /stt-Drill). Nur de2jp - bei jp2de spricht Michael Deutsch, da hilft kein
    # JP-Kana-Diff. Haengt neben die normale Quiz-Reaktion, ersetzt sie nicht.
    if stt_used and direction == "de2jp" and result["verdict"] != "correct" and card.get("jp"):
        analysis = yc.analyze_pronunciation(card["jp"], answer_text)
        if not analysis["match"]:
            resp["pronunciation"] = {
                "feedback": yc.pronunciation_feedback(analysis, card["jp"]),
                "expected_reading": analysis["expected_reading"],
                "kind": analysis["kind"]}
    return jsonify(resp)


@app.route("/personas")
def personas():
    """Liste der Personas + aktuell aktive + aktueller Mood (fuers Web-Dropdown/
    Avatar). Mood wird hier mitgeliefert, damit das Frontend beim Initial-Load
    weiss, ob ein transienter Mood ueber dem Persona-Default liegt
    (None = nur Persona-Default).

    Seit 2026-06-10 (Personas-JSONC): pro Persona liefern wir zusaetzlich
      - render: {gradient, lights} aus personas.jsonc (Frontend nutzt's fuer
        PERSONA_GRADIENTS + PERSONA_LIGHTS statt hardcoded-Map im Code)
      - force_research: bool (Sekretaerin etc.) -> Frontend lockt 🧠-Button
    Aeltere Frontends die das Feld nicht lesen, ignorieren es schadlos."""
    personas_payload = []
    for k, n in yc.persona_list():
        p = yc.PERSONAS.get(k, {})
        personas_payload.append({
            "key": k,
            "name": n,
            "group": p.get("group") or "companion",   # Picker-Trenner an der Gruppengrenze
            "render": p.get("render") or {},
            "force_research": bool(p.get("force_research")),
        })
    return jsonify({"ok": True,
                    "personas": personas_payload,
                    "current": CURRENT_PERSONA,
                    "mood": yc.load_mood(),
                    "companion_lang": yc.load_companion_lang(),
                    "tutor_level": yc.load_tutor_level()})


@app.route("/persona/companion_lang", methods=["POST"])
def persona_companion_lang():
    """Companion-Sprache umstellen (DE/EN) - greift fuer alle Personas ausser
    tutor (fest EN+JP) und kyoto (fest JP). Body: {lang: 'de'|'en'}. Persistiert
    in yuki_persona.json. Nur Reminder + Memory-Calls beim NAECHSTEN Verdichten
    aendern sich; alte Episoden bleiben in der Sprache in der sie geschrieben wurden."""
    body = request.get_json(silent=True) or {}
    lang = yc.save_companion_lang(body.get("lang", ""))
    _refresh_system_msg()   # Tutor-system haengt jetzt an companion_lang -> Cache neu bauen
    return jsonify({"ok": True, "companion_lang": lang})


@app.route("/persona/tutor_level", methods=["POST"])
def persona_tutor_level():
    """Tutor-Schwierigkeit umstellen. Body: {level: 'absolute_beginner'|'beginner'|
    'intermediate'|'advanced'}. Persistiert in yuki_persona.json. Greift ab dem
    naechsten User-Turn (build_system_msg liest live), kein Server-Restart noetig.
    Wirkt NUR fuer Tutor-Persona; andere Personas ignorieren das Setting."""
    body = request.get_json(silent=True) or {}
    level = yc.save_tutor_level(body.get("level", ""))
    return jsonify({"ok": True, "tutor_level": level})


@app.route("/persona", methods=["POST"])
def set_persona():
    """Aktive Persona wechseln. Erwartet {"key": "<persona-key>"}. Memory bleibt
    geteilt, daher nur System-Prompt + Few-Shot wechseln, Verlauf laeuft weiter."""
    global CURRENT_PERSONA, SYSTEM_MSG
    body = request.get_json(silent=True) or {}
    key = body.get("key", "")
    valid = dict(yc.persona_list())
    if key not in valid:
        return jsonify({"ok": False, "error": f"unbekannte Persona '{key}'"}), 400
    with LOCK:
        CURRENT_PERSONA = key
        yc.clear_drawing_wip()             # Phase B: Persona-Wechsel raeumt die laufende Leinwand
        SYSTEM_MSG = yc.build_system_msg(MEMORY, CURRENT_PERSONA)
        yc.save_persona(CURRENT_PERSONA)   # Wahl merken (ueberlebt Neustart)
        yc.reset_mood()                    # Mood resettet auf Persona-Default (sauberer Schnitt)
    # Persona ist server-global (eine CURRENT_PERSONA). Ein manueller Wechsel auf
    # EINEM Geraet muss die anderen offenen Tabs/Geraete mitziehen - sonst sehen sie
    # weiter die alte Persona, obwohl der naechste Turn schon in der neuen laeuft.
    # Wie chat_update: target_client_id = Initiator (filtert sich selbst raus, er hat
    # seine UI schon lokal gesetzt). applyPersonaFromResponse() zieht den Rest nach.
    _broadcast_sse({"kind": "persona_changed", "persona": key, "name": valid[key],
                    "mood": None, "target_client_id": body.get("client_id")})
    return jsonify({"ok": True, "current": CURRENT_PERSONA, "name": valid[key], "mood": None})


@app.route("/stt", methods=["POST"])
def stt():
    """Audio-Blob (multipart 'audio') -> erkannter Text. Noch NICHT ans LLM –
    erst bestaetigt/korrigiert das Frontend (wie das Terminal-Gate).

    Optional 'language' (multipart-Form) forciert Whisper-Sprache ('de'/'en'/'ja');
    fehlt es oder 'auto', laeuft Auto-Detect wie bisher. Frontend setzt das via
    Pill-Dropdown unter 'Zeigen' und kann es per [expect_lang:...]-Marker aus
    Yukis Reply temporaer ueberschreiben (Tutor-Drills, 'sag X auf JP')."""
    f = request.files.get("audio")
    if f is None:
        return jsonify({"ok": False, "error": "kein Audio empfangen"}), 400
    data = f.read()
    if not data:
        return jsonify({"ok": False, "error": "leeres Audio"}), 400
    language = (request.form.get("language") or "").strip().lower() or None
    # Diagnose-Hook (2026-06-18): letztes STT-Audio als Debug-Spur ablegen, damit ein
    # fehlschlagendes Wort ("benkyousuru" -> nichts erkannt) offline reproduzierbar
    # durch transcribe_bytes gejagt werden kann. Endung aus dem Mimetype geraten.
    try:
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
               "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav"}.get(
                   (f.mimetype or "").split(";")[0].strip(), "bin")
        (yc.RUNTIME_DIR / f"last_stt_input.{ext}").write_bytes(data)
    except Exception:
        pass
    # Aussprache-Drill (2026-09-08): Frontend haengt bei einem JP-Drill das Zielwort
    # (expect_word, aus Yukis [expect_lang:ja]-Lock) an. Dann Sprache auf 'ja' zwingen,
    # ROHEN Whisper-Output holen (Halluzinations-Filter aus - "Thank you." IST das
    # Signal) und Kana-Diff gegen das Zielwort fahren. Match -> normaler Flow (das
    # Wort geht in den Chat, Yuki lobt). Miss -> Aussprache-Feedback statt "nichts
    # erkannt"; das Frontend haelt das Mikro scharf fuer den naechsten Versuch.
    expect_word = (request.form.get("expect_word") or "").strip() or None
    if expect_word:
        try:
            with LOCK:
                raw_text, _, prob, conf = yc.transcribe_bytes(
                    MODEL, data, language="ja", filter_hallucination=False)
        except Exception as e:
            return jsonify({"ok": False, "error": f"STT-Fehler: {e}"}), 500
        analysis = yc.analyze_pronunciation(expect_word, raw_text)
        if analysis["match"]:
            resp = {"ok": True, "text": expect_word, "lang": "ja",
                    "prob": round(float(prob), 2), "drill_ok": True}
            score = yc._conf_score(conf)
            if score is not None:
                resp["conf_score"] = round(score, 3)
                resp["conf_mean"] = round(conf["mean"], 3)
            return jsonify(resp)
        return jsonify({"ok": True, "drill_miss": True,
                        "feedback": yc.pronunciation_feedback(analysis, expect_word),
                        "heard": raw_text, "expected": expect_word,
                        "expected_reading": analysis["expected_reading"],
                        "kind": analysis["kind"]})
    # Tutor-Rescue (2026-09-08): kein expliziter expect_word, aber Tutor-Persona +
    # Sprache ja (Lock ODER manuelle Pille). Faengt zwei reale Luecken ab: (a) Yukis
    # Lang-Lock-SSE erreichte die Pille nicht und du hast JA manuell gestellt; (b)
    # ihre letzte Reply umschreibt das Wort nur deutsch ("das Wort für Lernen") statt
    # es in JP-Schrift zu wiederholen. Greift NUR im Fehlerfall (wuerde "nichts
    # erkannt") und nur bei eindeutigem Zielwort aus den letzten Replies -> kapert
    # nie eine erfolgreiche Erkennung.
    if CURRENT_PERSONA == "tutor" and language == "ja":
        try:
            with LOCK:
                raw_text, lang, prob, conf = yc.transcribe_bytes(
                    MODEL, data, language="ja", filter_hallucination=False)
        except Exception as e:
            return jsonify({"ok": False, "error": f"STT-Fehler: {e}"}), 500
        clean = "" if yc._is_whisper_hallucination(raw_text) else raw_text
        if not clean:
            asst = [h.get("content", "") for h in reversed(HISTORY)
                    if h.get("role") == "assistant"]
            target = yc.derive_drill_target_recent(asst)
            if target:
                analysis = yc.analyze_pronunciation(target, raw_text)
                return jsonify({"ok": True, "drill_miss": True,
                                "feedback": yc.pronunciation_feedback(analysis, target),
                                "heard": raw_text, "expected": target,
                                "expected_reading": analysis["expected_reading"],
                                "kind": analysis["kind"]})
        # echte Sprache ODER kein Zielwort ableitbar -> normale Antwort (clean kann
        # "" sein -> Frontend zeigt "nichts erkannt" wie bisher).
        resp = {"ok": True, "text": clean, "lang": lang, "prob": round(float(prob), 2)}
        score = yc._conf_score(conf)
        if score is not None:
            resp["conf_score"] = round(score, 3)
            resp["conf_mean"] = round(conf["mean"], 3)
        return jsonify(resp)
    try:
        with LOCK:
            text, lang, prob, conf = yc.transcribe_bytes(MODEL, data, language=language)
    except Exception as e:
        return jsonify({"ok": False, "error": f"STT-Fehler: {e}"}), 500
    # conf_score (2026-06-20): 0..1 'Sauberkeit' aus avg_logprob. Das Frontend
    # vergleicht ihn gegen die Autosend-Schwelle (Optionen -> Verhalten): >= Schwelle
    # -> direkt an Yuki, darunter -> Eingabefeld zum Pruefen. Server bleibt schwellen-
    # agnostisch (Slider lebt per-Geraet im localStorage). conf_mean nur zur Info.
    resp = {"ok": True, "text": text, "lang": lang, "prob": round(float(prob), 2)}
    score = yc._conf_score(conf)
    if score is not None:
        resp["conf_score"] = round(score, 3)
        resp["conf_mean"] = round(conf["mean"], 3)
    return jsonify(resp)


def _guest_session_for(client_id, speaker):
    """Aktive Gast-DB-Session fuer (client_id, Sprecher) holen oder neu anlegen.
    Wechselt die Identitaet auf dem Geraet (oder gibt es noch keine Session), wird
    eine frische yuki_guest_db-Session gestartet. MUSS unter LOCK laufen. Liefert
    session_id oder None (DB aus)."""
    cid = client_id or "_anon"
    cur = GUEST_SESSIONS.get(cid)
    if cur and cur.get("person_id") == speaker.get("id"):
        return cur.get("session_id")
    sid = yuki_guest_db.start_session(
        speaker.get("id") or "guest", speaker.get("name") or "Gast",
        "person" if speaker.get("kind") == "person" else "guest")
    GUEST_SESSIONS[cid] = {"session_id": sid, "person_id": speaker.get("id"),
                           "name": speaker.get("name"), "kind": speaker.get("kind")}
    return sid


def _respond_guest(user_text, speaker, client_id):
    """Gast-Turn (jemand anderes als Michael spricht, Phase 1, 2026-06-16).

    Komplett isoliert von Michaels Canon:
      - eigener ephemerer Puffer pro client_id (GUEST_HISTORY), NICHT die globale HISTORY
      - Heart gesperrt + Sprecher-Block via build_system_msg(speaker=...)
      - KEINE Marker-Side-Effects (Mood/Note/Timer/Heart/Persona-Switch laufen NICHT)
      - KEINE _post_turn-Gates, KEIN save_history, KEIN yuki_history_db (Michael-Archiv),
        KEIN chat_update-Broadcast
      - ABER: Roh-Verlauf wandert in die SEPARATE yuki_guest_db (eigene DB, fuer
        spaetere Suche + als Graduation-Quelle bekannter Personen).
    Antwort wird trotzdem normal gerendert (Romaji + JP-Token-Glossen) und gesprochen.
    [[yuki-guest-identity]]."""
    cid = client_id or "_anon"
    with LOCK:
        buf = GUEST_HISTORY.setdefault(cid, [])
        buf.append({"role": "user", "content": user_text})
        try:
            sys_for_turn = yc.build_system_msg(MEMORY, CURRENT_PERSONA, speaker=speaker)
            reply = yc.generate_reply(buf, sys_for_turn,
                                      yc.persona_fewshot(CURRENT_PERSONA),
                                      yc.persona_reminder(CURRENT_PERSONA))
        except Exception as e:
            buf.pop()                       # fehlgeschlagenen User-Turn nicht behalten
            return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
        buf.append({"role": "assistant", "content": reply})
        if len(buf) > GUEST_HISTORY_MAX:    # Puffer cappen (aelteste raus)
            del buf[:len(buf) - GUEST_HISTORY_MAX]

        # Marker komplett raus (im Gast-Modus kein Side-Effect, keine Pattern-
        # Reinforcement noetig) - strip_all_markers ist die side-effect-freie Variante.
        reply_clean = yc.strip_all_markers(reply)
        tokens = _tokens_for(reply_clean, [])
        display = yc.annotate_romaji(reply_clean)

        # Roh-Verlauf ins Gast-Archiv (yuki_guest_db) - beide Zeilen UN-gestripped
        # (Archiv-Wahrheit, wie yuki_history_db). Defensiv: DB-Fehler kippt nie den Turn.
        sid = _guest_session_for(cid, speaker)
        if sid:
            pid = speaker.get("id") or "guest"
            pname = speaker.get("name") or "Gast"
            _mood = yc.load_mood()
            yuki_guest_db.persist_turn(sid, pid, user_text, person_id=pid, person_name=pname,
                                       persona=CURRENT_PERSONA, mood=_mood)
            yuki_guest_db.persist_turn(sid, "yuki", reply, person_id=pid, person_name=pname,
                                       persona=CURRENT_PERSONA, mood=_mood, tokens=tokens)

        audio_b64 = ""
        if not TTS_STREAM_MOBILE:
            try:
                wav = yc.synthesize(yc.clean_for_tts(reply_clean), persona=CURRENT_PERSONA)
                if wav:
                    audio_b64 = base64.b64encode(wav).decode("ascii")
            except Exception as e:
                print(f"  [TTS-Fehler (Gast): {e}]")

    return jsonify({"ok": True, "reply": reply_clean, "display": display,
                    "translation": "", "tokens": tokens, "furigana": [],
                    "actions": [], "tts_text": reply_clean,
                    "audio_b64": audio_b64, "has_audio": bool(audio_b64),
                    "stream": TTS_STREAM_MOBILE, "mood": yc.load_mood(),
                    "persona": CURRENT_PERSONA, "research": False,
                    "secretary": False, "guest": True,
                    "speaker_name": speaker.get("name", "")})


# Story-Zusammenfassungen, die GERADE erzeugt werden (async ODER on-demand). Rein
# IM SPEICHER (kein JSON-Feld): pending ist Laufzeit-Zustand, kein persistenter Fakt -
# nach einem Neustart ist die Menge leer (kein "ewig haengendes ⏳" nach Crash mitten
# in der Erzeugung). story_list spiegelt sie als summary_pending ins Frontend (⏳),
# story_summary verweigert eine ZWEITE Erzeugung solange eine laeuft (Anti-Doppel).
_story_summary_pending = set()
_story_summary_lock = threading.Lock()


def _kick_story_summary(story_id):
    """Im Hintergrund eine Mini-Inhaltsangabe fuer eine frisch erzeugte/verzweigte
    Geschichte erstellen + an sie heften (Library-ℹ-Aufklapper). Laeuft daemon-Thread
    OHNE den globalen LOCK - der LLM-Call ist unabhaengig, das Ergebnis persistiert
    yuki_stories separat. Erscheint beim naechsten Library-Oeffnen; nicht da -> der
    ℹ-Klick erzeugt sie on-demand (/story/summary)."""
    def _work():
        with _story_summary_lock:
            _story_summary_pending.add(story_id)
        try:
            story = yuki_stories.get_story(story_id)
            if not story:
                return
            summ = yc.summarize_story(story.get("title"), story.get("paragraphs") or [])
            if summ:
                yuki_stories.set_summary(story_id, summ)
                print(f"  [📖 Zusammenfassung gespeichert: '{story.get('title')}']", flush=True)
        finally:
            with _story_summary_lock:
                _story_summary_pending.discard(story_id)
    threading.Thread(target=_work, daemon=True, name="story-summary").start()


@app.route("/guest/reset", methods=["POST"])
def guest_reset():
    """Ephemeren Gast-Puffer + aktive Gast-DB-Session eines Geraets abschneiden
    (Frontend ruft das beim Identitaets-Wechsel - rein in den Gast-Modus ODER zurueck
    auf Michael -, damit keine Gast-Turns in eine neue Identitaet/Session bluten).
    Die Roh-Zeilen in yuki_guest_db bleiben erhalten; nur die LAUFENDE Session wird
    geschlossen, der naechste Gast-Turn beginnt eine neue."""
    cid = (request.get_json(silent=True) or {}).get("client_id") or "_anon"
    with LOCK:
        GUEST_HISTORY.pop(cid, None)
        GUEST_SESSIONS.pop(cid, None)
    return jsonify({"ok": True})


@app.route("/identity/graduate", methods=["POST"])
def identity_graduate():
    """Beim Wechsel WEG von einer bekannten Person (Phase 2): ihre noch offenen
    Gast-Sessions aus yuki_guest_db in Memory destillieren - attribuierte Episodes +
    People-Graph-Bricks (graduate_person_session). DB-gestuetzt, also auch fuer
    Sessions, die beim letzten Mal nicht graduiert wurden (Catch-up). Anonyme Gaeste
    haben keine pending sessions -> No-Op. Beruehrt Michaels Canon NICHT.
    [[yuki-guest-identity]]."""
    body = request.get_json(silent=True) or {}
    person_id = (body.get("person_id") or "").strip()
    if not person_id or person_id.lower() in ("michael", "guest"):
        return jsonify({"ok": True, "graduated": 0})   # nichts zu destillieren

    def _work():
        pending = yuki_guest_db.pending_sessions(person_id)
        total = {"sessions": 0, "episodes": 0, "bricks": 0}
        for s in pending:
            rows = yuki_guest_db.get_messages(session_id=s["session_id"])
            # Gast-DB-Zeilen -> {role, content}: speaker=='yuki' -> assistant, sonst user.
            msgs = [{"role": "assistant" if r["speaker"] == "yuki" else "user",
                     "content": r["content"]} for r in rows]
            pname = s.get("person_name") or person_id
            with LOCK:
                res = yc.graduate_person_session(pname, msgs)
            yuki_guest_db.mark_graduated(s["session_id"])
            total["sessions"] += 1
            total["episodes"] += res.get("episodes", 0)
            total["bricks"] += res.get("bricks", 0)
        if total["sessions"]:
            print(f"  [Gast-Graduation {person_id}: {total['sessions']} Session(s) -> "
                  f"{total['episodes']} Episoden + {total['bricks']} Bricks]")
        # SYSTEM_MSG frisch (neue Episodes/Bricks koennen recallt werden)
        _refresh_system_msg()

    # LLM-Gates laufen im Hintergrund (koennen ein paar Sekunden dauern); Antwort
    # kommt sofort zurueck, das Zurueckschalten soll nicht blockieren.
    threading.Thread(target=_work, daemon=True, name="guest-graduate").start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Geschichten-Library (Erzaehlerin-Story-Modus, 2026-06-21). Eigener Speicher
# (yuki_stories.py), orthogonal zum Canon. Endpoints: list/get/continue/delete.
# Das 📚-Modal im Frontend haengt hier dran; ▶ Anhoeren laedt /story/get und
# spielt im Story-Overlay-Player, ➕ Weitererzaehlen ruft /story/continue.
# ---------------------------------------------------------------------------
@app.route("/story/list", methods=["GET"])
def story_list():
    """Schlanke Liste aller gespeicherten Geschichten (neueste zuerst, ohne Volltext).
    summary_pending markiert Eintraege, deren Inhaltsangabe gerade async erzeugt wird
    (Frontend zeigt ⏳ statt ℹ)."""
    stories = yuki_stories.list_stories()
    with _story_summary_lock:
        pend = set(_story_summary_pending)
    for s in stories:
        s["summary_pending"] = (s.get("id") in pend) and not (s.get("summary") or "").strip()
    return jsonify({"ok": True, "stories": stories})


@app.route("/story/get", methods=["GET"])
def story_get():
    """Volle Geschichte (Titel + alle Absaetze) fuer den Overlay-Player."""
    sid = (request.args.get("id") or "").strip()
    story = yuki_stories.get_story(sid)
    if not story:
        return jsonify({"ok": False, "error": "Geschichte nicht gefunden"}), 404
    # Sicherheitsnetz fuer aeltere Geschichten (gespeichert vor dem Marker-Strip bei
    # der Generierung): beim Ausliefern Marker aus Absaetzen + Titel bereinigen, damit
    # Player + TTS nie rohe [mood:]/[gesture:]/... zeigen oder sprechen. Neue Stories
    # sind schon sauber gespeichert (_parse_full_story strippt). Nur Ausgabe-Kopie.
    story = dict(story)
    story["title"] = yc.strip_all_markers(story.get("title") or "")
    story["custom_title"] = yc.strip_all_markers(story.get("custom_title") or "")
    cleaned = [yc.strip_all_markers(p) for p in (story.get("paragraphs") or [])]
    story["paragraphs"] = [p for p in cleaned if p.strip()]
    return jsonify({"ok": True, "story": story})


@app.route("/story/continue", methods=["POST"])
def story_continue():
    """Weitererzaehlen: ein neues Kapitel an eine bestehende Geschichte anhaengen.
    Laeuft unabhaengig von der aktuell aktiven Persona (man startet es aus der
    Library) - baut bewusst einen Erzaehlerin-System-Prompt + reicht die bisherige
    Geschichte als DATA (prior_text, injektionssicher gerahmt) ins Gate. Synchron
    (LLM-Call ~30-90s) wie /respond; das Frontend zeigt solange einen Spinner."""
    body = request.get_json(silent=True) or {}
    sid = (body.get("id") or "").strip()
    story = yuki_stories.get_story(sid)
    if not story:
        return jsonify({"ok": False, "error": "Geschichte nicht gefunden"}), 404
    prior_text = "\n\n".join(story.get("paragraphs") or [])
    with LOCK:
        sys_msg = yc.build_system_msg(MEMORY, "storyteller")
        hist = [{"role": "user", "content": "Erzähl diese Geschichte weiter.",
                 "persona": "storyteller"}]
        try:
            result = yc.generate_full_story(
                hist, sys_msg, yc.persona_fewshot("storyteller"),
                yc.persona_reminder("storyteller"), prior_text=prior_text)
        except Exception as e:
            return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
    if not result:
        return jsonify({"ok": False, "error": "Weitererzählen hat nicht geklappt – "
                        "magst du's nochmal versuchen?"}), 200
    updated = yuki_stories.append_chapter(sid, result["paragraphs"])
    if not updated:
        return jsonify({"ok": False, "error": "Speichern fehlgeschlagen"}), 500
    print(f"  [📖 Geschichte weitererzählt: '{updated['title']}' "
          f"(+{len(result['paragraphs'])} Absätze -> {len(updated['paragraphs'])})]", flush=True)
    return jsonify({"ok": True, "story": updated,
                    "new_paragraphs": result["paragraphs"], "hint": result["hint"]})


@app.route("/story/branch", methods=["POST"])
def story_branch():
    """Ab einer gewaehlten Stelle neu weitererzaehlen: nimmt den Praefix der Geschichte
    (Absaetze [0 .. cut_index]) + Michaels gesprochenen/getippten Wunsch und erzeugt eine
    NEUE Geschichte (continued_from verlinkt sie mit der alten - die bleibt unangetastet,
    siehe yuki_stories.branch_story). Atomar: der Store wird erst angefasst, NACHDEM das
    LLM erfolgreich geliefert hat. Schlaegt das Generieren fehl oder lehnt Yuki ab,
    passiert NICHTS - keine bestehende Geschichte wird beschaedigt. Synchron (~30-90s)
    wie /story/continue; das Frontend zeigt solange den Mic-Knopf als 'Yuki erzaehlt'."""
    body = request.get_json(silent=True) or {}
    sid = (body.get("id") or "").strip()
    instruction = (body.get("instruction") or "").strip()
    try:
        cut = int(body.get("cut_index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "cut_index fehlt/ungültig"}), 400
    story = yuki_stories.get_story(sid)
    if not story:
        return jsonify({"ok": False, "error": "Geschichte nicht gefunden"}), 404
    paras = story.get("paragraphs") or []
    if cut < 0 or cut >= len(paras):
        return jsonify({"ok": False, "error": "Stelle ungültig"}), 400
    prior_text = "\n\n".join(paras[:cut + 1])
    with LOCK:
        sys_msg = yc.build_system_msg(MEMORY, "storyteller")
        # Michaels Wunsch als echter User-Turn (steuert die Fortsetzung); der Praefix
        # geht als DATA (prior_text) injektionssicher in den System-Prompt.
        user_msg = instruction or "Erzähl die Geschichte ab hier weiter."
        hist = [{"role": "user", "content": user_msg, "persona": "storyteller"}]
        try:
            result = yc.generate_full_story(
                hist, sys_msg, yc.persona_fewshot("storyteller"),
                yc.persona_reminder("storyteller"), prior_text=prior_text)
        except Exception as e:
            return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
    if not result or not result.get("paragraphs"):
        # Yuki hat (in-character) abgelehnt ODER nichts Brauchbares geliefert -> die alte
        # Geschichte bleibt unberuehrt. Ihre eigenen Worte (falls vorhanden) zurueckgeben.
        msg = (result or {}).get("text") or ("Das Weitererzählen hat nicht geklappt – "
                                             "magst du's nochmal versuchen?")
        return jsonify({"ok": False, "error": msg}), 200
    child = yuki_stories.branch_story(sid, cut, result["paragraphs"])
    if not child:
        return jsonify({"ok": False, "error": "Speichern fehlgeschlagen"}), 500
    print(f"  [📖 Geschichte verzweigt: '{child['title']}' ab Absatz {cut + 1} "
          f"-> neue Story ({len(child['paragraphs'])} Absätze)]", flush=True)
    _kick_story_summary(child["id"])   # Inhaltsangabe der neuen Fassung async nachziehen
    return jsonify({"ok": True, "story": child, "new_paragraphs": result["paragraphs"]})


@app.route("/story/new", methods=["POST"])
def story_new():
    """Neue Geschichte aus einem Briefing erzeugen - angestossen aus dem Story-Brief-
    Overlay (NICHT aus dem normalen Chat). Baut nach dem Vorbild von /story/branch einen
    LOKALEN Wegwerf-Kontext (hist) + einen frischen Erzaehlerin-System-Prompt und fasst
    weder die globale HISTORY noch die SQLite-Verlaufs-DB an -> das Briefing kann nicht
    mehr in conversation.json / den Canon leaken (genau das tat der alte _full_story_turn:
    er haengte den Wunsch in HISTORY, wo die spaeter aktive Companion-Persona ihn las).
    Michaels Wunsch geht als echter User-Turn, KEIN prior_text. Atomar wie branch: der
    Store wird erst NACH erfolgreicher Generierung angefasst. Synchron (~30-90s)."""
    body = request.get_json(silent=True) or {}
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"ok": False, "error": "leeres Briefing"}), 400
    with LOCK:
        sys_msg = yc.build_system_msg(MEMORY, "storyteller")
        # Wunsch als echter User-Turn in einer LOKALEN Liste - die globale HISTORY
        # bleibt unberuehrt (kein Leak). Kein prior_text = Geschichte von Grund auf neu.
        hist = [{"role": "user", "content": instruction, "persona": "storyteller"}]
        try:
            result = yc.generate_full_story(
                hist, sys_msg, yc.persona_fewshot("storyteller"),
                yc.persona_reminder("storyteller"))
        except Exception as e:
            return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
    if not result or not result.get("paragraphs"):
        # Yuki hat (in-character) abgelehnt ODER nichts Brauchbares geliefert -> nichts
        # gespeichert. Ihre eigenen Worte (falls vorhanden) fuers Overlay zurueckgeben.
        msg = (result or {}).get("text") or ("Die Geschichte ist mir gerade entglitten – "
                                             "magst du's nochmal versuchen?")
        return jsonify({"ok": False, "error": msg}), 200
    story_obj = yuki_stories.create_story(
        result["title"], result["paragraphs"], persona="storyteller")
    if not story_obj:
        return jsonify({"ok": False, "error": "Speichern fehlgeschlagen"}), 500
    print(f"  [📖 Geschichte gespeichert (Overlay): '{result['title']}' "
          f"({len(result['paragraphs'])} Absätze)]", flush=True)
    _kick_story_summary(story_obj["id"])   # Inhaltsangabe async nachziehen
    return jsonify({"ok": True, "story": story_obj})


@app.route("/story/delete", methods=["POST"])
def story_delete():
    """Geschichte aus der Library loeschen (Kinder werden umgehaengt, siehe
    yuki_stories.delete_story)."""
    body = request.get_json(silent=True) or {}
    sid = (body.get("id") or "").strip()
    ok = yuki_stories.delete_story(sid)
    return jsonify({"ok": ok})


@app.route("/story/title", methods=["POST"])
def story_title():
    """Eigenen Haupttitel einer Geschichte setzen (leer = zuruecksetzen). Der
    Original-Titel bleibt als Untertitel erhalten (yuki_stories.set_title)."""
    body = request.get_json(silent=True) or {}
    sid = (body.get("id") or "").strip()
    title = (body.get("title") or "").strip()
    if not yuki_stories.get_story(sid):
        return jsonify({"ok": False, "error": "Geschichte nicht gefunden"}), 404
    yuki_stories.set_title(sid, title)
    return jsonify({"ok": True})


@app.route("/story/summary", methods=["POST"])
def story_summary():
    """Mini-Inhaltsangabe einer Geschichte holen/erzeugen (Library-ℹ-Aufklapper).
    Schon vorhanden -> sofort zurueck. Sonst synchron erzeugen (~paar Sekunden, user-
    initiiert) + an die Geschichte heften. Laeuft bewusst OHNE den globalen LOCK, damit
    es ein paralleles Gespraech nicht blockiert. Deckt aeltere Geschichten (vor dem
    Feature) + den Fall ab, dass die async-Erzeugung noch nicht durch war."""
    body = request.get_json(silent=True) or {}
    sid = (body.get("id") or "").strip()
    story = yuki_stories.get_story(sid)
    if not story:
        return jsonify({"ok": False, "error": "Geschichte nicht gefunden"}), 404
    existing = (story.get("summary") or "").strip()
    if existing:
        return jsonify({"ok": True, "summary": existing})
    # Laeuft die Erzeugung schon (async im Hintergrund ODER ein paralleler on-demand-
    # Klick)? Dann NICHT ein zweites Mal generieren - das Frontend pollt und zeigt ⏳.
    with _story_summary_lock:
        if sid in _story_summary_pending:
            return jsonify({"ok": False, "pending": True,
                            "error": "Zusammenfassung wird erstellt …"})
        _story_summary_pending.add(sid)
    try:
        summ = yc.summarize_story(story.get("title"), story.get("paragraphs") or [])
    except Exception as e:
        return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
    finally:
        with _story_summary_lock:
            _story_summary_pending.discard(sid)
    if not summ:
        return jsonify({"ok": False, "error": "Konnte keine Zusammenfassung erstellen."}), 200
    yuki_stories.set_summary(sid, summ)
    return jsonify({"ok": True, "summary": summ})


@app.route("/story/config", methods=["GET", "POST"])
def story_config():
    """GET: aktuelle (effektive) Story-Tunables. POST {target_paragraphs}: schreibt die
    Ziel-Absatzzahl nach config/story.json (live-reload, kein Restart). Options->Verhalten
    haengt hier dran. Nur target_paragraphs ist UI-schreibbar; num_predict/num_ctx_*
    bleiben Datei-Hoheit (selten gedreht)."""
    cfg_path = HERE / "config" / "story.json"
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            tp = int(body.get("target_paragraphs"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "target_paragraphs fehlt/ungültig"}), 400
        tp = max(4, min(40, tp))
        cur = {}
        if cfg_path.is_file():
            try:
                cur = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
        if not isinstance(cur, dict):
            cur = {}
        cur["target_paragraphs"] = tp
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            yc._atomic_write_text(cfg_path, json.dumps(cur, ensure_ascii=False, indent=2))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "target_paragraphs": tp})
    return jsonify({"ok": True, "config": yc.load_story_config()})


@app.route("/respond", methods=["POST"])
def respond():
    """Bestaetigter/korrigierter Text -> Yukis Antwort (Text + Romaji + Audio).
    Audio kommt als base64-WAV inline mit, damit es ein Roundtrip bleibt.

    Recherche-Modus: Wenn payload["research"]=True (gesetzt vom Gehirn-Toggle im
    Frontend), wird fuer DIESEN EINEN TURN auf die _research-Persona umgeschaltet
    (slim System-Prompt, RESEARCH_TOOLS_SPEC aktiv, eigener purpose="research"-
    Debug-Dump). Marker-Side-Effects (Mood/Notes/Persona-Switch/Gestures) sowie
    Heart-/Facts-/Episodes-/Keepsake-Gates laufen NICHT - Recherche-Antworten
    sollen den Beziehungs-State nicht beruehren. CURRENT_PERSONA bleibt unangetastet.
    """
    payload = request.get_json(silent=True) or {}
    user_text = (payload.get("text") or "").strip()
    research_mode = bool(payload.get("research"))
    # Sekretaerin-Mode (force_research:true in personas.jsonc, z.B. "secretary"):
    # Tools laufen jeden Turn, aber Beziehungs-State (Marker-Side-Effects, Heart-
    # Gates, Verdichtung) bleibt NORMAL aktiv - anders als research_mode (skip).
    # Wird hier hochgezogen damit der TTS-Collapse-Pfad und das chat_update-SSE
    # ebenfalls auf "long reply -> overlay" umschalten.
    secretary_mode = CURRENT_PERSONA in yc.FORCE_RESEARCH_PERSONAS
    # WICHTIG: Bei Sekretaerin schickt das Frontend research:true mit (Button ist
    # gepinnt-aktiv durch RESEARCH_FORCED_PERSONAS). Aber Sekretaerin ist KEIN
    # research_mode-Turn: [timer:]/[note:]/[event:] muessen ganz normal ausgewertet
    # werden, der Beziehungs-State laeuft normal. Darum research_mode hier hart auf
    # False setzen, sobald secretary_mode greift - sonst springt die marker-skip-
    # Klammer unten ein und Yukis [timer:15m:...] landet als Roh-Text im Display,
    # ohne dass der Timer tatsaechlich gesetzt wird.
    if secretary_mode:
        research_mode = False
    # Origin-Routing 2026-06-04: wenn der Reply einen [timer:...]-Marker setzt,
    # bekommt der dahinter erzeugte Timer diese Client-ID mit und der spaetere
    # timer_done-Alarm toent nur auf diesem Geraet. Optional - alte Frontend-
    # Versionen schicken nichts mit, dann broadcastet timer_done wie bisher.
    target_client_id = payload.get("client_id") or None
    # Watch-Turn (web/watch.html schickt watch:true): Yuki darf KEINE Timer setzen.
    # Die Uhr kann nicht wecken -> ein (oft proaktiver) Timer waere verwaist. Wir
    # unterdruecken die Timer-Anlage hart (Code-Gate, nicht nur Prompt) UND geben Yuki
    # einen leisen Hinweis, damit sie gar nicht erst einen anbietet. [[yuki-watch-ui]]
    is_watch = bool(payload.get("watch"))
    respond_timer = None    # ggf. {end_ts,label} - Origin schedult LocalNotification direkt aus der HTTP-Antwort
    if not user_text:
        return jsonify({"ok": False, "error": "leerer Text"}), 400

    # Gast-Modus (Phase 1, 2026-06-16): spricht jemand anderes als Michael, laeuft
    # der Turn komplett isoliert (eigener Pfad, ephemerer Puffer, Heart gesperrt,
    # KEIN Canon-Write/Verdichtung/Broadcast). [[yuki-guest-identity]].
    speaker = yc.resolve_speaker(payload.get("identity"))
    if speaker.get("kind") != "michael":
        return _respond_guest(user_text, speaker, target_client_id)

    # Ganze-Geschichte-Modus (Erzaehlerin) laeuft NICHT mehr ueber /respond: neue
    # Geschichten werden aus dem Story-Brief-Overlay via /story/new erzeugt (lokaler
    # Wegwerf-Kontext, kein HISTORY/Canon-Touch). Frueher hing hier _full_story_turn,
    # dessen Briefing in conversation.json leakte. [[yuki-personas]] / yuki_stories.py.

    # --- DIAGNOSE (2026-06-17): Phasen-Timestamps. _t_recv = Request beim Server
    # angekommen. Wir messen Lock-Wartezeit (Background-Turn haelt LOCK?) und die
    # Zeit bis kurz vor den LLM-Call. Das eigentliche [reply:Xs] + [ollama:...]
    # (yuki_core) schliessen daran an -> kompletter Sprung-Zerlegung in der Konsole.
    _t_recv = time.time()
    with LOCK:
        _t_lock = time.time()
        if _t_lock - _t_recv > 0.05:
            print(f"  [respond: Lock-Wartezeit {_t_lock - _t_recv:.1f}s "
                  f"(Background-Turn hielt LOCK)]", flush=True)
        # persona pro Turn mitschreiben (UI-Meta-Feld, von build_messages ignoriert) -
        # die no_canon-Verdichtungs-Filter (_strip_no_canon) brauchen sie, um z.B.
        # Erzaehlerin-Turns aus dem Canon-Batch zu halten. Fehlt sie (Legacy/andere
        # Pfade) -> canon-faehig als sicherer Default.
        HISTORY.append({"role": "user", "content": user_text, "persona": CURRENT_PERSONA, "ts": time.time()})
        # Resonanz-Tint dieses Turns (v1): wird nur im Companion-Zweig gesetzt
        # (resonance_tint_for_user_msg gated intern auf Companion-Personas + Multiplier).
        # Hier vorab None, damit der Payload-Override weiter unten in allen Zweigen
        # (auch research/secretary) eine definierte Variable sieht.
        _reson = None
        try:
            if secretary_mode:
                # Sekretaerin-Pfad: voller Yuki-Kontext + Tools + Action-Marker erlaubt.
                # Reihenfolge VOR research_mode-Branch, falls beide Flags zufaellig
                # zusammenkommen (Frontend kann 🧠 nicht ausschalten in der Persona).
                yc.reset_file_hits()
                reply = yc.generate_secretary_reply(HISTORY, MEMORY)
            elif research_mode:
                reply = yc.generate_research_reply(HISTORY, persona_before=CURRENT_PERSONA)
            else:
                # Steward-Digest-Bridge (2026-06-13): pro Turn FRISCH an die (gecachte)
                # SYSTEM_MSG haengen, damit Yuki weiss was sie in Michaels Abwesenheit
                # vorgemerkt hat - sonst Leerlauf wenn er's anspricht. Nicht in kyoto
                # (JP-only) / tutor (Lern-Modus). Companion-Personas only.
                sys_for_turn = SYSTEM_MSG
                if CURRENT_PERSONA not in ("kyoto", "tutor"):
                    sys_for_turn += yc.steward_digest_block_for_prompt()
                    # Gedankenlog-Bridge (2026-06-17): Yuki weiss im Chat, was ihr
                    # waehrend Michaels Abwesenheit durch den Kopf ging (leiser Kanal).
                    sys_for_turn += yc.steward_thoughts_block_for_prompt()
                if is_watch:
                    # Uhr-Kontext: kein Wecker verfuegbar -> Yuki bietet keinen Timer an.
                    sys_for_turn += ("\n\n[Gerät: Smartwatch. Du hast hier KEINE Wecker-/"
                                     "Timer-Funktion. Setze keine Timer und biete keine an; "
                                     "wenn ein Timer sinnvoll waere, sag Michael er soll ihn "
                                     "am Handy stellen.]")
                # Kuenstlerin: On-Demand-Stempel-Suche ([[yuki-drawing-feature]]) - emittiert
                # Yuki [stamps:query], durchsucht generate_kuenstlerin_reply die OpenMoji-
                # Bibliothek und ruft mit Treffern erneut (bis zu N Runden), sonst ein
                # normaler Call. Alle anderen Personas: regulaerer generate_reply.
                # Resonanz (v1, 2026-07-01): traf die User-Msg einen Anker aus Yukis
                # Emotions-Kern, faerbt ein leiser Hint ihren Ton VOR dem Formulieren
                # ("Fuehlen statt Einschaetzen"). Pro Turn frisch an sys_for_turn (wie
                # steward_digest_block), damit der Slider sofort wirkt und nichts im
                # gecachten SYSTEM_MSG klebt. Der Mood-Tint (Gesicht) wird spaeter aus
                # _reson gezogen. resonance_tint_for_user_msg gated selbst auf Companion.
                _reson = yc.resonance_tint_for_user_msg(user_text, CURRENT_PERSONA)
                if _reson and _reson.get("prompt_hint"):
                    sys_for_turn += _reson["prompt_hint"]
                    _sec = f" +{_reson['secondary']}" if _reson.get("secondary") else ""
                    print(f"  [🌊 Resonanz: '{_reson['subject']}' -> "
                          f"{_reson['emotion_de']}{_sec} (mood={_reson.get('mood')})]",
                          flush=True)
                _gen = (yc.generate_kuenstlerin_reply if CURRENT_PERSONA == "kuenstlerin"
                        else yc.generate_reply)
                reply = _gen(HISTORY, sys_for_turn, yc.persona_fewshot(CURRENT_PERSONA),
                             yc.persona_reminder(CURRENT_PERSONA))
        except Exception as e:
            HISTORY.pop()  # fehlgeschlagenen User-Turn nicht behalten
            return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
        # Roh anhaengen - _finalize_assistant_history weiter unten sanitized + setzt
        # translation. So laufen ALLE Reply-Pfade durch dieselbe Finalisierung.
        HISTORY.append({"role": "assistant", "content": reply, "persona": CURRENT_PERSONA, "ts": time.time()})
        # Verlaufs-DB (yuki_history.sqlite): beide Turns NACH erfolgreichem LLM
        # persistieren, damit ein LLM-Fehler-Rollback (HISTORY.pop oben) nicht
        # einen verwaisten User-Eintrag in der DB zurueck laesst. Yuki-Reply
        # UN-stripped (Marker drin, Archiv-Wahrheit) - die Sanitize-Schleife in
        # _finalize_assistant_history arbeitet nur auf HISTORY, nicht auf der DB.
        # Mood-Snapshot vor Marker-Side-Effects greift den Stand zum Zeitpunkt
        # des Turns - ein [mood:X] im Reply aendert ihn erst danach.
        _mood_snapshot = yc.load_mood()
        yuki_history_db.persist_message("michael", user_text,
                                         persona=CURRENT_PERSONA, mood=_mood_snapshot)
        yuki_history_db.persist_message("yuki", reply,
                                         persona=CURRENT_PERSONA, mood=_mood_snapshot)
        if research_mode:
            # Recherche-Turn: KEINE Marker-Auswertung (kein Mood/Notes/Persona-Switch),
            # KEINE _post_turn-Gates (Heart/Facts/Keepsake/Episodes). Antwort 1:1 nehmen.
            # Kyoto-DE-Untertitel entsteht jetzt deterministisch async (via
            # _maybe_spawn_kyoto_translation nach turn_id-Vergabe), NICHT mehr aus einem
            # [de:]-Marker. Hier nur noch sanitisieren (strippt evtl. streunende [de:]
            # belt-and-suspenders) - fuer ALLE Personas gleich, kein Kyoto-Sonderfall.
            reply_clean = yc.sanitize_reply_for_history(reply)
            translation = ""
            furigana = []                  # Research-Persona kennt den Furigana-Marker nicht.
            drawing = None                 # Research-Persona malt nicht.
            # research-Flag in HISTORY persistieren: summarize_session + extract_facts
            # erkennen das spaeter und ueberspringen den Volltext bzw. nutzen nur eine
            # 1-Satz-Summary (kommt mit Task #7).
            HISTORY[-1]["meta"] = {"research": True}
            tokens = _tokens_for(reply_clean, furigana)
            if tokens:
                HISTORY[-1]["tokens"] = tokens
            HISTORY[-1]["content"] = reply_clean
        else:
            # Reply-Marker auswerten (Mood + Timer + ...) - siehe _handle_marker_side_effects.
            # Ungueltige Marker (Mood-Tippfehler) und [de:...]-Translate-Marker werden in
            # der Finalisierung rausgestrippt; gueltige Marker bleiben im History-Content
            # als Pattern fuer Folgeturns. target_client_id wandert in Timer-Entries
            # damit der spaetere Alarm nur am triggernden Geraet toent.
            reply_clean, translation, furigana = _handle_marker_side_effects(
                reply, target_client_id=target_client_id)
            # [lookat:N]: Yuki will gezielt eine Kamera-Position anschauen -> async Schwenk
            # + Vision-Folgenachricht (NUR aus dem Chat, keine Rekursion). Aus dem ROHEN
            # reply gelesen (reply_clean hat den Marker schon raus).
            _maybe_trigger_lookat(reply, target_client_id)
            # Timer-HTTP-Pfad entfallen (Stufe C): der Timer wird jetzt async vom Decider
            # gesetzt, NACH dieser Antwort - der native Wecker kommt uebers timer_started-SSE.
            respond_timer = None
            # Draw-Marker (Kuenstlerin, [[yuki-drawing-feature]] Thema 2): SVG-Doodle
            # aus dem Reply ziehen BEVOR tokenisiert/annotiert wird (sonst liefe das
            # rohe SVG durch fugashi/Romaji). reply_clean ist danach das SVG los;
            # das SVG geht als UI-Meta in die HISTORY (drawing) + ins Album.
            # canvas:new-Signal ZUERST lesen (nicht-mutierend) - extract_draw_marker
            # strippt den canvas-Marker gleich mit, danach waere er weg.
            canvas_reset, _ = yc.extract_canvas_new(reply_clean)
            drawing, reply_clean = yc.extract_draw_marker(reply_clean)
            if drawing:
                # SVG IMMER lenient reparieren ([[yuki-drawing-feature]]): LLM-Tippfehler im
                # <svg>-Tag/Attributen (verrutschter Bindestrich, vermurkstes xmlns, doppeltes
                # <svg>) brechen sonst die Browser-Anzeige -> leere Bubble trotz Save. Was
                # gespeichert/angezeigt/als Leinwand abgelegt wird, ist die saubere Fassung.
                drawing = yc.repair_drawing_svg(drawing)
                # Phase C (Self-Review): das reparierte SVG rastern und Yuki multimodal
                # zurueckzeigen, damit sie ihr eigenes Werk wirklich SIEHT und ggf. nachbessert
                # - still within-turn (User sieht nur das Endergebnis). Self-guarding (Feature-
                # Flag/Modell/Render); degradiert zu 'drawing unveraendert'. Nur Kuenstlerin.
                if CURRENT_PERSONA == "kuenstlerin":
                    drawing = yc.self_review_drawing(drawing) or drawing
                # Stempel-Komposition ([[yuki-drawing-feature]]): Yukis authored SVG bleibt
                # KOMPAKT (<use href='#slug'/>) - das wird die Leinwand (drawing_wip), damit
                # der naechste Turn ihre schlanke Komposition sieht (kein Token-Stau). Fuer
                # Anzeige/Album/Galerie wird zu self-contained SVG expandiert (Browser + resvg
                # loesen #id nur im selben Dokument). Ohne <use> ist beides identisch.
                drawing_wip = drawing
                drawing = yc.inject_symbol_defs(drawing)
            # Galerie-Marker (2026-06-17): Yuki pinnt ein gemaltes Doodle bewusst an
            # ihre Wand. Aus reply_clean strippen (Display/TTS), Pin folgt nach Save.
            gallery_pin, gallery_cap, reply_clean = yc.extract_gallery_marker(reply_clean)
            # Display-Hygiene: alle Marker sind jetzt raus - Doppel-Leerzeichen + leere
            # Zeilen einsammeln (ein Marker auf eigener Zeile hinterlaesst sonst Lueck-
            # en). Zentral hier, weil reply_clean ab jetzt in Tokens/display/Broadcast/
            # TTS/Doodle-Caption fliesst. Reload-Pfad macht dasselbe via strip_all_markers.
            reply_clean = yc.tidy_reply_text(reply_clean)
            if canvas_reset:
                # Yukis bewusster Neustart: Leinwand leeren. Folgt im selben Turn ein
                # neues [draw:...], gewinnt das (save_drawing_wip unten setzt frisch).
                yc.clear_drawing_wip()
                print("  [🎨 Leinwand geleert (canvas:new)]", flush=True)
            if drawing:
                saved = yc.save_drawing(drawing, reply_clean, persona=CURRENT_PERSONA)
                if saved:
                    print(f"  [🎨 Zeichnung gespeichert: {saved.name}]", flush=True)
                    # Bewusste Kuratierung: [gallery] pinnt genau dieses Doodle (herkunft yuki).
                    if gallery_pin:
                        try:
                            if yc.add_to_gallery("drawing", saved.name, origin="yuki",
                                                 caption=gallery_cap or reply_clean[:120]):
                                print(f"  [🖼 Doodle an die Galerie-Wand gepinnt: {saved.name}]", flush=True)
                                _broadcast_sse({"kind": "gallery_update"})
                        except Exception as _e:
                            print(f"  [Galerie-Pin-Fehler: {_e}]", flush=True)
                # Phase B (Multi-Turn-Evolution): das frische SVG wird die laufende
                # Leinwand - der naechste Turn baut via CURRENT CANVAS-Block darauf auf.
                # Bewusst die KOMPAKTE <use>-Fassung (drawing_wip), nicht die expandierte.
                yc.save_drawing_wip(drawing_wip)
            if canvas_reset or drawing:
                # naechster Turn sieht den neuen Leinwand-Stand (frisch erweitert ODER
                # geleert). Nur in der Kuenstlerin sichtbar; andere Personas + ein
                # Persona-Switch leeren das WIP ohnehin.
                _refresh_system_msg()
            tokens = _tokens_for(reply_clean, furigana)
            _finalize_assistant_history(reply, translation, tokens, furigana, drawing=drawing)
            # Sekretaerin-Flag in HISTORY persistieren, damit der Reload-
            # Renderer (siehe /history + reloadHistory) die Bubble genauso
            # collapsed darstellt wie live - sonst gaehnt nach Reload eine
            # Riesen-Bubble statt des Overlay-Buttons. Anders als research-
            # Turns laufen Sekretaerin-Turns NORMAL durch den Marker- und
            # _post_turn-Pfad - das meta-Flag ist nur fuer den Renderer.
            if secretary_mode:
                HISTORY[-1]["meta"] = {"secretary": True}

        # [mehr]-Marker (Research/Sekretaerin): kurze Einleitung fuer Bubble + Live-TTS,
        # ausfuehrliche Antwort ins Overlay. Nur ehren wenn der Body laenger als die
        # Collapse-Schwelle ist - sonst (kurz / kein Marker) inline mergen. reply_clean
        # wird hier marker-frei (fuer Display/Broadcast); die HISTORY-content-Variante
        # darf den Marker behalten (Pattern fuer Folgeturns, beim Reload via
        # strip_all_markers entfernt + meta.research_lead/_body liefert den Split).
        # Scope B: strukturierte Datei-Treffer des Turns (nur Sekretaerin) fuers
        # Frontend-Trefferlisten-UI. Sind welche da -> KEIN [mehr]-Split (kurze
        # Bubble + Chip ersetzen das Overlay).
        file_hits = yc.pop_file_hits() if secretary_mode else []
        file_hits_meta = yc.pop_file_hits_meta() if secretary_mode else None
        if file_hits:
            _m = HISTORY[-1].get("meta") or {}
            _m["file_hits"] = file_hits
            if file_hits_meta:
                _m["file_hits_meta"] = file_hits_meta
            HISTORY[-1]["meta"] = _m

        research_lead = research_body = None
        if research_mode or secretary_mode:
            _lead, _body = yc.split_research_more(reply_clean)
            # Anzeige-Hygiene: der Research-Pfad laeuft NICHT durch
            # _handle_marker_side_effects, also koennen [mood:]/[gesture:]/etc-Marker
            # durchrutschen, die die Persona eigentlich nicht setzen soll (sanitize_
            # reply_for_history behaelt den ersten Mood sogar bewusst als Pattern und
            # fasst gesture gar nicht an). Aus den Anzeige-Teilen strippen - handeln tun
            # wir sie hier bewusst nicht. [mehr] ist durch den Split schon raus.
            _lead = yc.strip_all_markers(_lead)
            if _body is not None:
                _body = yc.strip_all_markers(_body)
            if file_hits:
                # Datei-Such-Turn: [mehr] wird verworfen (die klickbare Liste ersetzt
                # das Overlay), Prosa bleibt inline + kurz. Kein research_lead/-body.
                # Der split_research_more-Aufruf oben hat einen evtl. vom Modell doch
                # gesetzten [mehr]-Marker schon entfernt (sonst leakt er literal).
                reply_clean = (_lead + (" " + _body if _body else "")).strip()
            elif _body and len(_body) > _RESEARCH_FULL_TTS_BELOW:
                research_lead, research_body = _lead, _body
                reply_clean = _lead + "\n\n" + _body
                meta = HISTORY[-1].get("meta") or {}
                meta["research_lead"], meta["research_body"] = research_lead, research_body
                HISTORY[-1]["meta"] = meta
            elif _body:
                reply_clean = (_lead + " " + _body).strip()   # kurz -> inline mergen
            else:
                reply_clean = _lead                            # kein Marker (bereinigt)
        # Modell-Badge (Woelkchen oben-links an der Bubble): welches LLM diesen
        # Reply generiert hat. Quelle ist _LAST_REPLY_LLM_STATS (in chat_ollama nur
        # fuer purpose reply/research/secretary gesetzt -> failover-sicher und NICHT
        # von den Background-Gates ueberschrieben); Fallback aktiver Server. Als
        # UI-Meta in HISTORY persistieren, damit das Woelkchen auch nach Reload da
        # ist (analog research/secretary). Voller Modellname; das Frontend kuerzt
        # auf das Groessen-Tag (gemma4:12b -> 12b, gemma4:e4b -> e4b).
        reply_model = (yc.get_last_reply_llm_stats() or {}).get("model") or yc.OLLAMA_MODEL
        if reply_model:
            _mm = HISTORY[-1].get("meta") or {}
            _mm["model"] = reply_model
            HISTORY[-1]["meta"] = _mm
        # turn_id-Keystone (async Action-Pipeline): wird HIER vergeben, nach dem alle
        # Pfade (research/secretary/normal) HISTORY[-1] vollstaendig geschrieben haben.
        # WICHTIG: turn_id MUSS vor save_history gesetzt werden, damit sie persistiert
        # und der /history-Reload-Pfad (t.get("turn_id")) bei action_result-Korrelation
        # einen echten Wert findet - auch fuer research/secretary-Turns ohne async Decider.
        turn_id = _next_turn_id()
        HISTORY[-1]["turn_id"] = turn_id
        # actions_pending: nur beim normalen Pfad (+ Sekretaerin) laeuft der async Decider.
        # Research-Turns fuehren keine Actions aus.
        actions_pending = not research_mode
        yc.save_history(HISTORY)
        # Kyoto-DE-Untertitel: deterministisch async erzeugen (kein [de:]-Marker mehr).
        # Deckt Companion UND Research-in-kyoto ab - turn_id ist hier fuer ALLE Pfade
        # vergeben, reply_clean ist in beiden Faellen bereits reines JP.
        _maybe_spawn_kyoto_translation(turn_id, reply_clean)

        display = yc.annotate_romaji(reply_clean)  # mit Romaji fuer die Anzeige
        # actions: kleine Icon-Liste an der unteren Bubble-Kante (Heart/Note/...).
        # Quelle ist der ROHE reply (mit Markern) - reply_clean hat die Klammern
        # schon raus. Frontend rendert nur wenn Toggle an ist.
        # 👁-Icon AUF DIESE Bubble (wo Yuki sagt, dass sie hinschaut) + die anderen
        # Side-Effect-Marker. Research-Turn -> leer (dort werden die Marker nicht
        # ausgefuehrt, ein Icon waere eine Luege; Befund Research.L1).
        actions = _reply_action_icons(reply, research_mode)

        # Auto-Sync zwischen Geraeten (#25 in yuki-next-ideas): andere Browser-Tabs
        # bzw. die mobile App, die NICHT diesen Turn ausgeloest haben, rendern
        # User-Bubble + Yuki-Reply still in den Chat. Voll-Payload (kein /history-
        # Roundtrip noetig). BEWUSST hier - VOR TTS-Synthese - feuern damit andere
        # Geraete den Text sofort sehen statt erst nach TTS-Latenz (bei
        # TTS_STREAM_MOBILE=False kann das mehrere Sekunden dauern).
        # Filter laeuft am Frontend gegen die eigene client_id; ohne
        # target_client_id (= aelteres Frontend) garnicht erst broadcasten,
        # sonst wuerde der Initiator es doppelt rendern.
        #
        # Resonanz-Mood-Tint (v1, 2026-07-01): wenn Yuki KEINEN eigenen [mood:] gesetzt
        # hat (load_mood() == None) und der Turn einen Resonanz-Anker traf, faerbt der
        # transiente Tint fuer DIESEN Reply das Gesicht. Yukis eigener Marker gewinnt
        # immer (nur bei None angewandt). Bewusst NICHT persistiert - das Gefuehl
        # verfliegt mit dem Thema, darum ein reiner Payload-Override statt save_mood.
        _display_mood = yc.load_mood()
        if _display_mood is None and _reson and _reson.get("mood"):
            _display_mood = _reson["mood"]
        if target_client_id:
            _broadcast_sse({"kind": "chat_update", "target_client_id": target_client_id,
                            "user_text": user_text, "reply": reply_clean, "display": display,
                            "translation": translation, "tokens": tokens, "furigana": furigana,
                            "drawing": drawing, "actions": actions,
                            "research": research_mode, "secretary": secretary_mode,
                            "research_lead": research_lead, "research_body": research_body,
                            "file_hits": file_hits or None,
                            "file_hits_meta": file_hits_meta,
                            "persona": CURRENT_PERSONA, "model": reply_model,
                            "mood": _display_mood,
                            "turn_id": turn_id, "actions_pending": actions_pending})

        # Truncation-Guard (yuki_core.pop_truncation_notice): hat der Reply-Prompt das
        # num_ctx-Limit gesprengt (v.a. lokaler Notbetrieb), warnen wir den User im
        # Chat - er liest nicht immer das Server-Log. An ALLE Tabs (kein Origin-Filter),
        # bewusst NACH generate_reply abgeholt (vor den _post_turn-Gates, damit es die
        # Reply-Truncation ist, nicht die eines Background-Gates).
        _trunc = yc.pop_truncation_notice()
        if _trunc:
            _broadcast_sse({"kind": "sys_notice", "level": "warn",
                            "message": (f"Notbetrieb: Prompt am Kontext-Limit "
                                        f"({_trunc['prompt_tokens']}/{_trunc['num_ctx']} Tokens, "
                                        f"{_trunc['model']}) – Antwort evtl. gekürzt. "
                                        f"num_ctx erhöhen oder Verlauf/Recall kürzen.")})

        # TTS-Text: bei Research-Turns UND Sekretaerin-Turns nutzt _research_tts_text
        # die Frontend-Schwelle (lang -> nur erster Satz, kurz -> ganze Antwort) damit
        # kurze Replies ohne Modal-Button auch komplett gesprochen werden. Sekretaerin
        # generiert oefter mal 4-10 Saetze, da hilft der gleiche Collapse-Mechanismus.
        # Normale Turns: ganzer reply_clean.
        if research_lead is not None:
            tts_text = research_lead          # [mehr]-Split geehrt: live nur die Einleitung
        elif research_mode or secretary_mode:
            tts_text = _research_tts_text(reply_clean)
        else:
            tts_text = reply_clean
        audio_b64 = ""
        if not TTS_STREAM_MOBILE:                  # gestreamt: Audio holt das Handy via /tts_stream
            try:
                wav = yc.synthesize(yc.clean_for_tts(tts_text), persona=CURRENT_PERSONA)
                if wav:
                    yc.LAST_REPLY_WAV.write_bytes(wav)         # Debug-Parity zu main.py
                    audio_b64 = base64.b64encode(wav).decode("ascii")
            except Exception as e:
                print(f"  [TTS-Fehler: {e}]")

    # Hintergrund-Maintenance NACH dem LOCK starten (Heart/Verlauf-Verdichtung/Facts-Komp).
    # Bei Recherche-Turns SKIP - Recherche soll den Beziehungs-State nicht beruehren.
    if not research_mode:
        _post_turn(user_text, reply_clean)

    # F-Pipeline (async, im TTS-Schatten): Action-Decider urteilt, Executor fuehrt aus,
    # dann LOCK-Repatch der HISTORY-meta + action_result-SSE an den Initiator.
    # Nur normale Turns (kein Research) - Research-Turns laufen keine Actions aus.
    # Sekretaerin laeuft jetzt durch den Decider (secretary_mode kein Guard mehr).
    if not research_mode:
        # letzte ~6 Nachrichten (3-4 Turns) als Text-Schnappschuss fuer den Decider
        recent = HISTORY[-8:-2]              # ohne den gerade emittierten Turn
        recent_txt = "\n".join(f"{m.get('role')}: {m.get('content','')}" for m in recent)
        # allow_timer=not is_watch: die Uhr kann keinen Wecker toenen -> Timer auf Watch-Turns
        # unterdruecken (Orphan-Schutz), andere Aktionen (Notiz/Routine/...) bleiben erlaubt.
        _spawn_action_pipeline(turn_id, user_text, reply_clean, recent_txt, target_client_id,
                               allow_timer=not is_watch)

    # Perf-HUD-Diagnose: an Yuki gesendeter Kontext (Prompt-Tokens) + generierte Tokens
    # der letzten Antwort. Background-Gates nutzen andere purposes, ueberschreiben das
    # also nicht. None bei auto-sized Remote-Modellen fuer num_ctx.
    _llm_stats = yc.get_last_reply_llm_stats() or {}

    return jsonify({"ok": True, "reply": reply_clean, "display": display,
                    "translation": translation, "tokens": tokens, "furigana": furigana,
                    "drawing": drawing, "actions": actions, "tts_text": tts_text,
                    "audio_b64": audio_b64, "has_audio": bool(audio_b64),
                    "stream": TTS_STREAM_MOBILE, "mood": _display_mood,
                    "persona": CURRENT_PERSONA, "research": research_mode,
                    "secretary": secretary_mode, "model": reply_model,
                    "research_lead": research_lead, "research_body": research_body,
                    "file_hits": file_hits or None,
                    "file_hits_meta": file_hits_meta,
                    "turn_id": turn_id, "actions_pending": actions_pending,
                    "ctx_tokens": _llm_stats.get("prompt_tokens"),
                    "gen_tokens": _llm_stats.get("gen_tokens"),
                    "ctx_limit": _llm_stats.get("num_ctx"),
                    "timer": respond_timer})


@app.route("/see", methods=["POST"])
def see():
    """Foto (multipart 'image') -> Yuki schaut + reagiert in Persona (Text + Romaji +
    Audio). Wie /respond, nur dass der Anlass ein Bild statt getipptem Text ist:
    look_and_react() laesst das VLM beschreiben und qwen3/Persona darauf reagieren."""
    f = request.files.get("image")
    if f is None:
        return jsonify({"ok": False, "error": "kein Bild empfangen"}), 400
    data = f.read()
    if not data:
        return jsonify({"ok": False, "error": "leeres Bild"}), 400
    # Zuletzt gezeigtes Foto fuer die Gesichts-Einlernung merken (siehe
    # /faces/bootstrap_michael) - das ist die Quelle, die Michael bewusst framt.
    global _LAST_SEEN_FRAME
    _LAST_SEEN_FRAME = data
    # Optionale Caption: wenn Michael was zum Bild sagt (z.B. "das bist du
    # auf dem alten Foto"), wandert sie als question in look_and_react und
    # dort in die Wahrnehmung -> Yuki interpretiert die Szene im Kontext.
    caption = (request.form.get("caption") or "").strip()
    client_id = (request.form.get("client_id") or "").strip()

    # Wenn Yuki via [look:...] genauer hinschaut (Extra-Vision-Runde, dauert), pushen
    # wir mid-turn einen Mic-Status NUR ans sendende Geraet (target_client_id-Routing,
    # analog Spontan/Timer). Der Frontend-SSE-Handler setzt das Label auch wenn der
    # Tab busy ist - es ist sein eigener laufender Turn.
    def _on_look(_question):
        _broadcast_sse({"kind": "mic_status",
                        "target_client_id": client_id,
                        "value": "Yuki<br>schaut genauer hin …"})

    # Gesichtserkennung (rein beratend): IMMER erkennen - auch bei Caption, denn
    # "schau hier am Fluss, meine Schwester ist mit im Bild" will genau die Namen.
    # Nur die Formulierung passt sich an: ohne Caption = Live-Kamera (present in view),
    # mit Caption = gezeigtes Bild (recognized in this image, koennte aelter/fremd sein),
    # damit das LLM keine falsche Live-Anwesenheit annimmt.
    _recog = fr.recognize_frame(data)
    _store = fr.load_faces()
    presence = fr.format_presence_context(_recog, _store, live=not bool(caption))
    faces = fr.recognized_people(_recog, _store)

    with LOCK:
        try:
            reply, saw = yc.look_and_react(
                HISTORY, SYSTEM_MSG, yc.persona_fewshot(CURRENT_PERSONA), data,
                question=caption or None,
                reminder=yc.persona_reminder(CURRENT_PERSONA),
                persist_meta={"persona": CURRENT_PERSONA, "mood": yc.load_mood()},
                on_look=_on_look, presence=presence)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Vision/LLM-Fehler: {e}"}), 502
        if reply is None:
            return jsonify({"ok": False,
                            "error": "Vision nicht verfuegbar (laeuft serve-lfm2vl.ps1 auf :8081?)"}), 503
        # Reply-Marker auswerten - HISTORY hat _react_to_perception bereits intern
        # angehaengt, also bleibt der Original-Reply mit Markern dort.
        reply_clean, translation, furigana = _handle_marker_side_effects(reply)
        # no_archive: beim "Gedankenbild zeigen" wird das Bild NICHT (erneut) als
        # Keepsake archiviert - es liegt schon als Gedankenbild in der Galerie.
        # Marker wird trotzdem gestrippt (archive=False), damit nichts ins UI leakt.
        no_archive = bool(request.form.get("no_archive"))
        reply_clean, keepsake_archived = _handle_keepsake_marker(
            reply_clean, data, saw, "Handy", archive=not no_archive)
        tokens = _tokens_for(reply_clean, furigana)
        _finalize_assistant_history(reply, translation, tokens, furigana)
        shot = _save_vision_shot(data)             # gezeigtes Foto als Inline-Thumb
        _stamp_vision_image(shot)                  # an den Perception-Turn (Reload)
        _stamp_vision_faces(faces)                 # erkannte Personen an den Turn (Reload)
        # actions: ROHER reply (mit Marker-Klammern). Keepsake-Erkennung kommt
        # automatisch mit - wenn _handle_keepsake_marker per Gate (statt Marker)
        # archiviert hat, taucht es hier NICHT auf, aber das passt: das Icon
        # zeigt "Yuki hat aktiv den Marker geschrieben" - das Gate ist autonom.
        actions = _detect_actions(reply)
        yc.save_history(HISTORY)
        # Modell-Woelkchen: welches LLM diesen Vision-Reply generierte (von
        # _finalize_assistant_history in HISTORY[-1].meta gesetzt). Innerhalb des
        # LOCK greifen, weil die JSON-Antwort unten ausserhalb gebaut wird.
        reply_model = (HISTORY[-1].get("meta") or {}).get("model")
        # Keepsake-Gate (qwen3-Entscheidung) nur wenn nicht schon via Marker archiviert,
        # sonst landet das Bild doppelt im Album. Bei no_archive (Gedankenbild zeigen)
        # ganz aus - sonst entstuende eine redundante Keepsake-Kopie.
        if not keepsake_archived and not no_archive:
            yc.maybe_archive_keepsake(
                data, saw, reply_clean, source="Handy",
                on_saved=lambda p, c: print(f"  [💾 ins Album: {c}  ({p.name})]"))

        # L3c Foto-Abgleich (DER ANKER): ist eine Liste aktiv, das Produkt gegen ihre
        # OFFENEN Items matchen (gemma, Optik+Text). Treffer -> hint ans Item (👁),
        # Michael bestaetigt den Haken per Tap. NIE auto-abhaken (Label = nur Hinweis).
        # Nur on-demand-Foto (/see), nicht im Auto-Loop.
        list_match = None
        _act = yc.active_list()
        if _act:
            try:
                hit = yc.match_photo_to_list(data, _act)
            except Exception as e:
                hit = None
                print(f"  [📋 Foto-Abgleich Fehler (ignoriert): {e}]", flush=True)
            if hit:
                _idx, _item_text = hit
                yc.set_list_item_hint(_act["id"], _idx, "im Foto erkannt")
                list_match = {"list_id": _act["id"], "list_title": _act.get("title"),
                              "item_index": _idx, "item_text": _item_text}
                print(f"  [📋 Foto-Abgleich: '{_item_text}' auf '{_act.get('title')}' erkannt]",
                      flush=True)
                _broadcast_sse({"kind": "list_changed", "reason": "hint", "id": _act["id"]})

        display = yc.annotate_romaji(reply_clean)
        audio_b64 = ""
        if not TTS_STREAM_MOBILE:                  # gestreamt: Audio holt das Handy via /tts_stream
            try:
                wav = yc.synthesize(yc.clean_for_tts(reply_clean), persona=CURRENT_PERSONA)
                if wav:
                    yc.LAST_REPLY_WAV.write_bytes(wav)
                    audio_b64 = base64.b64encode(wav).decode("ascii")
            except Exception as e:
                print(f"  [TTS-Fehler: {e}]")
        _emit_actions_for_reply(reply_clean, caption or "(Foto gezeigt)",
                                target_client_id=client_id, note_source="michael", allow_timer=True)

    # Vision-Turn: kein "user_text" -> Heart-Gate skippt, aber Verlauf-Verdichtung pruefen.
    # Eine Caption kann Heart-relevant sein (z.B. "das ist meine Mutter") -> als user_text reichen.
    _post_turn(caption, reply_clean)

    return jsonify({"ok": True, "reply": reply_clean, "display": display, "saw": saw,
                    "translation": translation, "tokens": tokens, "furigana": furigana,
                    "actions": actions, "list_match": list_match, "model": reply_model,
                    "audio_b64": audio_b64, "has_audio": bool(audio_b64),
                    "stream": TTS_STREAM_MOBILE, "mood": yc.load_mood(),
                    "persona": CURRENT_PERSONA, "image": shot, "faces": faces})


def _paragraph_chunks(raw, min_chars):
    """Zerlegt den ROHEN Antworttext an Absatzgrenzen (Leerzeilen/Zeilenumbrueche)
    in Stuecke von je >= min_chars Zeichen fuers Absatz-Streaming. WICHTIG: arbeitet
    auf dem ROHEN Text, weil clean_for_tts spaeter alle Newlines kollabiert (es wird
    pro Chunk EINZELN sauber gemacht). Greedy: Absaetze sammeln bis die Schwelle
    erreicht ist, dann Chunk. Ein sehr kurzer Rest-Absatz wird an den letzten Chunk
    angehaengt statt als 5-Zeichen-Fragment ("Ja?") allein zu stehen.
    Kein Absatzumbruch / min_chars<=0 / Gesamttext kurz -> genau EIN Chunk (= alter
    Weg, kein Regressionsrisiko)."""
    paras = [p.strip() for p in re.split(r"\n+", raw) if p.strip()]
    if not paras:
        return [raw]
    if min_chars <= 0 or len(paras) <= 1:
        return ["\n".join(paras)]
    chunks, buf = [], ""
    for p in paras:
        buf = (buf + "\n" + p) if buf else p
        if len(buf) >= min_chars:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks and len(buf) < min_chars // 2:
            chunks[-1] = chunks[-1] + "\n" + buf
        else:
            chunks.append(buf)
    return chunks


@app.route("/tts", methods=["POST"])
def tts_wav():
    """Komplettes WAV fuer einen Text (NICHT gestreamt) - fuer die Wyoming-TTS-Bridge
    (HA Voice-Pipeline, Phase 2b: Yukis echte Stimme statt Piper). Sprache wie
    /tts_stream via Qwen3 (pick_tts_language), optionaler persona-Override im Body fuer
    den tutor/kyoto-Sonderfall (Default = CURRENT_PERSONA). Antwort audio/wav, oder 503
    wenn nichts Sprechbares / TTS down (die Bridge spricht dann gar nichts). Braucht KEINE
    LOCK (der TTS-Service ist eigenstaendig, kein Whisper/History-Zugriff)."""
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("text") or "").strip()
    persona = payload.get("persona") or CURRENT_PERSONA
    text = yc.clean_for_tts(raw)
    if not text or not yc._SPEAKABLE_RE.search(text):
        return jsonify({"ok": False, "error": "nichts Sprechbares"}), 503
    try:
        wav = yc.synthesize(text, persona=persona)
    except Exception as e:
        print(f"  [TTS /tts Fehler: {e}]", flush=True)
        return jsonify({"ok": False, "error": str(e)}), 503
    if not wav:
        return jsonify({"ok": False, "error": "TTS lieferte nichts"}), 503
    return Response(wav, mimetype="audio/wav")


_NARRATOR_CACHE = {"t": 0.0, "data": None}
_NARRATOR_TTL = 60.0


def _narrator_voices_payload():
    """Proxyt qwens GET /voices (Core-Split: nur qwen kennt die Refs). 60s-Cache
    gegen Hammering; Fallback auf 'yuki', wenn qwen nicht erreichbar ist."""
    now = time.time()
    if _NARRATOR_CACHE["data"] is not None and (now - _NARRATOR_CACHE["t"]) < _NARRATOR_TTL:
        return _NARRATOR_CACHE["data"]
    base = yc.TTS_QWEN_URL.rsplit("/", 1)[0]      # http://host:5006
    data = {"voices": [{"id": "yuki", "label": "Yuki"}], "default": "yuki"}
    try:
        r = requests.get(base + "/voices", timeout=4)
        if r.status_code == 200:
            j = r.json()
            if j.get("voices"):
                data = {"voices": j["voices"], "default": j.get("default") or "yuki"}
    except Exception as e:
        print(f"  [/narrator/voices: qwen nicht erreichbar: {e}]", flush=True)
    _NARRATOR_CACHE["t"], _NARRATOR_CACHE["data"] = now, data
    return data


@app.route("/narrator/voices")
def narrator_voices():
    return jsonify(_narrator_voices_payload())


@app.route("/tts_stream", methods=["POST"])
def tts_stream():
    """Streamt Yukis Sprachausgabe als rohes PCM (int16 mono, 24kHz) chunked ans Handy.
    Qwen3-TTS rendert absatzweise (nativer Token-Stream pro Absatz), Sprache pro Absatz
    via pick_tts_language, Emotion (instruct) fuer den ganzen Turn.
    Erwartet ROHEN Antworttext (clean_for_tts passiert hier); braucht NICHT die
    LOCK (der TTS-Service ist eigenstaendig, kein Whisper/History-Zugriff)."""
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("text") or "").strip()
    # Optional: beim normalen Reply-Pfad sendet das Frontend seine client_id mit;
    # nach erfolgreicher Synthese broadcasten wir das fertige WAV als chat_audio-SSE
    # an alle anderen Geraete (Empfaenger setzt lastAudioBuf + enabled Replay-Button,
    # damit Auto-Sync auch das Audio mitliefert). Gloss-Popup + Research-Modal
    # senden das Feld NICHT - dort soll der Stream nur lokal abspielen.
    broadcast_client_id = payload.get("broadcast_client_id") or None
    voice = payload.get("voice") or None

    # --- Qwen3-Pfad (tts.engine=qwen): EINE Engine, nativer PCM-Stream ueber :5006 ---
    # Sprache via pick_tts_language, Emotion via current_voice_instruct (Mood->instruct),
    # 24 kHz. Qwen-Service down -> leerer Stream (Yuki degradiert auf Text-only).
    # Absatz-Streaming: lange Antworten an Absatzgrenzen splitten, jeden Absatz EINZELN
    # rendern + SOFORT rausstreamen (nativer Token-Stream pro Absatz). Waehrend Absatz 1
    # spielt, rendert Qwen Absatz 2 -> Yuki redet nach dem 1. Absatz los statt nach der
    # ganzen Antwort. Sprache pro Absatz (Antwort kann DE/JA mischen), Emotion (instruct)
    # fuer den ganzen Turn. Kurze/umbruchlose Antwort -> 1 Absatz (kein Regress).
    q_instruct = yc.current_voice_instruct(CURRENT_PERSONA)
    q_chunks = _paragraph_chunks(raw, QWEN_STREAM_MIN_CHARS)

    def generate_qwen():
        pcm = bytearray()
        try:
            for chunk_raw in q_chunks:
                q_text = yc.clean_for_tts(chunk_raw)
                if not q_text or not yc._SPEAKABLE_RE.search(q_text):
                    continue
                q_lang = yc.pick_tts_language(chunk_raw, persona=CURRENT_PERSONA)
                body = {"text": q_text, "language": q_lang}
                if q_instruct:
                    body["instruct"] = q_instruct
                if voice:
                    body["voice"] = voice
                try:
                    with requests.post(yc.TTS_QWEN_STREAM_URL, json=body, stream=True, timeout=180) as r:
                        if r.status_code != 200:
                            print(f"  [Qwen-Stream-Fehler HTTP {r.status_code}] {r.text[:160]}")
                            continue   # Absatz ueberspringen statt Abbruch
                        for chunk in r.iter_content(8192):
                            if chunk:
                                pcm += chunk
                                yield bytes(chunk)
                except Exception as e:
                    print(f"  [Qwen-Stream-Fehler (Absatz): {e}]")
                    continue
        finally:
            # Replay-Quelle + Geraete-Sync aus dem GESAMT-PCM aller Absaetze.
            try:
                if pcm:
                    samples = yc.np.frombuffer(bytes(pcm), dtype=yc.np.int16)
                    yc.wavfile.write(yc.LAST_REPLY_WAV, yc.TTS_QWEN_SR, samples)
                    if broadcast_client_id:
                        _broadcast_chat_audio(broadcast_client_id, yc.TTS_QWEN_SR, samples)
            except Exception:
                pass

    headers = {"X-Sample-Rate": str(yc.TTS_QWEN_SR), "Cache-Control": "no-store"}
    return Response(stream_with_context(generate_qwen()),
                    mimetype="application/octet-stream", headers=headers)


# ===========================================================================
# Autonome Sicht (web) – Capture, Loop, SSE-Broadcast, Konfig-Route
# ===========================================================================
def _lock_busy():
    """True, wenn die zentrale LOCK gerade gehalten wird (Request in flight).
    Wir wollen das Auto-Schauen nicht reinfunken, waehrend Michael spricht/zeigt."""
    if LOCK.acquire(blocking=False):
        LOCK.release()
        return False
    return True


def capture_frame_web():
    """Standbild der aktiven Kamera-Quelle (config/cameras.json) als JPEG-Bytes,
    None bei Fehler. Quelle/Typ (BRIO-dshow, Netzwerk-Cam http_snap/rtsp) + PTZ
    stecken in yuki_camera; der Rest der Vision-Pipeline haengt nur an den Bytes
    und merkt vom Quellwechsel nichts."""
    return ycam.grab()


def _broadcast_sse(event_dict):
    """Pusht ein JSON-Event an alle aktuell verbundenen SSE-Clients (Tabs).
    Jeder Client hat eine eigene Queue; volle Queues / tote Verbindungen werden
    aus der Menge entfernt. Single-User-Setup, in der Praxis 1-2 Tabs gleichzeitig."""
    global _sse_seq
    payload = json.dumps(event_dict, ensure_ascii=False)
    with _sse_lock:
        _sse_seq += 1
        # Komplettes SSE-Frame inkl. id:-Zeile - so haelt der Browser pro Event
        # die Last-Event-ID und der Replay kann dasselbe Frame 1:1 wieder rausgeben.
        frame = f"id: {_sse_seq}\ndata: {payload}\n\n"
        _sse_log.append((_sse_seq, frame))
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(frame)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.discard(q)


def _broadcast_chat_audio(target_client_id, sr, int16_samples):
    """Schickt das fertige Reply-Audio als b64-WAV an alle Empfaenger-Geraete
    (Frontend filtert: nur wenn target_client_id != eigene client_id). Damit
    bekommen andere Tabs/Geraete den Replay-Buffer ohne extra Click - lastAudioBuf
    wird gesetzt, Replay-Button enabled. Aufgerufen aus /tts_stream
    NACH erfolgreicher Synthese."""
    try:
        import io as _io
        buf = _io.BytesIO()
        yc.wavfile.write(buf, int(sr), int16_samples)
        audio_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        _broadcast_sse({"kind": "chat_audio",
                        "target_client_id": target_client_id,
                        "audio_b64": audio_b64, "sr": int(sr)})
    except Exception as e:
        print(f"  [chat_audio-Broadcast-Fehler: {e}]")


def _prime_baselines(gen):
    """Beim Aktivieren EINMAL alle Blickpunkte (alle watch-Cams + Presets) anfahren und
    je eine Baseline setzen. Sonst ist jede Position erst nach ihrem ersten Rotations-
    Besuch 'scharf' und die erste echte Sichtung kommt erst nach einer vollen Runde
    (bei vielen Positionen viele Minuten). look_at erledigt Anfahrt + Settle pro Position
    selbst. Laeuft im Daemon-Thread; bricht ab wenn Beobachten wieder aus ist ODER neu
    aktiviert wurde (gen veraltet). Der Loop pausiert solange ueber _auto_web['priming']."""
    try:
        if not yc.VISION_ENABLED:
            return
        vps = ycam.watch_viewpoints()
        _broadcast_sse({"kind": "watch_tick", "priming": True})
        done = 0
        for src_name, pos, _area in vps:
            if not _auto_web["enabled"] or gen != _auto_web["priming_gen"]:
                return                                # aus-/neu-geschaltet -> abbrechen
            frame, _a = ycam.look_at(src_name, pos)   # faehrt hin, wartet bis still, grabt
            if not frame:
                continue
            desc = yc.describe_image(frame, quiet=True)
            if not desc:
                continue
            _auto_web["last_desc_by_pos"][f"{src_name}#{pos}"] = desc
            done += 1
        print(f"  [👁 Baseline-Priming fertig: {done}/{len(vps)} Positionen]", flush=True)
    except Exception as e:
        print(f"  [Baseline-Priming-Fehler: {e}]", flush=True)
    finally:
        # Nur freigeben/melden, wenn dieser Thread noch der aktuelle ist.
        if gen == _auto_web["priming_gen"]:
            _auto_web["priming"] = False
            _auto_web["last_check"] = 0.0             # Rotation darf sofort starten
            _broadcast_sse({"kind": "watch_tick", "priming": False,
                            "next_check_at": _next_capture_eta()})


# ===========================================================================
# Impuls-Gate: gemeinsames Urteil fuer die zwei Dauer-Loops (2026-07-02)
# ===========================================================================
# proactive_loop_web + auto_vision_loop_web rufen _impulse_evaluate() an der
# Stelle, wo sie frueher STUR reagiert haben. Das Gate entscheidet none/thought/
# react. Der manuelle Trigger (Name/💭 -> _fire_proactive_once) und der manuelle
# Foto-Pfad (📷/V -> look_and_react) laufen bewusst NICHT hier durch -> bleiben
# verpflichtend.

def _impulse_day_roll():
    """Tages-Budget des leisen Kanals bei Datumswechsel zuruecksetzen."""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if _impulse["day_stamp"] != today:
        _impulse["day_stamp"] = today
        _impulse["thoughts_today"] = 0


def _impulse_thought(text, reason, source, cfg):
    """Leiser Kanal: zurueckgehaltenen Impuls als 💭-Gedanke ins Steward-
    Gedankenlog schreiben (Wiederverwendung des Stores + SSE-Badge). Eigener
    Token-Bucket (getrennt vom Steward). Returns True, wenn geschrieben."""
    text = (text or "").strip()
    if not text:
        return False
    now = time.time()
    cap = int(cfg.get("thought_max_per_day", 12))
    gap = float(cfg.get("thought_min_gap_min", 0)) * 60.0
    if _impulse["thoughts_today"] >= cap or (now - _impulse["last_thought_ts"]) < gap:
        print(f"  [💭 Impuls-Gedanke ({source}) unterdrueckt: Budget/Gap]", flush=True)
        return False
    _impulse["thoughts_today"] += 1
    _impulse["last_thought_ts"] = now
    yc.add_steward_thought(text, source=f"impulse_{source}", reason=reason)
    _broadcast_sse({"kind": "steward_thoughts",
                    "count": len(yc.load_steward_thoughts(unread_only=True))})
    print(f"  [💭 Impuls-Gedanke ({source}): {text[:80]}]", flush=True)
    return True


def _impulse_evaluate(source, **decide_kwargs):
    """Gemeinsames Urteil fuer die zwei Dauer-Loops. Returns einen Handlungs-Code:
      'disabled' -> Gate per config aus (Caller faellt auf alten Zwangs-Reflex zurueck)
      'react'    -> Caller soll jetzt die volle Reaktion komponieren
      sonst      -> schweigen (Quiet-Hours / schwaches Modell / none / Gedanke
                    schon geschrieben)
    Alle Guardrails hier im Code (nie im Prompt), config live-reload."""
    cfg = yc.load_impulse_config()
    if not cfg.get("enabled", True):
        return "disabled"                                 # Rollback-Schalter
    _impulse_day_roll()
    now = datetime.datetime.now()
    if yc.steward_in_quiet_hours(now.hour, cfg.get("quiet_start", 0),
                                 cfg.get("quiet_end", 0)):
        return "quiet"
    # Modell-Floor: unter der Schwelle (Failover auf 8b) BEWUSST schweigen statt
    # mit schwachem Modell zu urteilen ("8b richtet Chaos an"). Kein Zwangs-Reflex.
    if yc._model_size_b() < float(cfg.get("model_floor_b", 12)):
        return "hold"
    decision = yc.impulse_decide(source, **decide_kwargs)
    # Confidence-Hochstufung (nur Beobachten): ein SEHR sicherer 'thought' wird laut
    # (react) statt nur ins stille 💭-Log zu wandern. Nutzt das bisher ungenutzte
    # confidence-Feld (Befund Impuls.O1). Wir sind hier bereits hinter dem model_floor_b-
    # Gate -> die Hochstufung greift nur bei grossem Modell.
    action, promoted = yc.impulse_promote(source, decision, cfg)
    if action == "react":
        tag = (f"thought->react @conf {decision.get('confidence', 0.0):.2f}"
               if promoted else "react")
        print(f"  [🧭 Impuls ({source}) -> {tag}: {decision.get('reason','')[:70]}]",
              flush=True)
        return "react"
    if action == "thought":
        # Kalibrier-Sicht: bei Vision confidence vs. Schwelle mitloggen, damit
        # react_confidence_floor datenbasiert justierbar ist (Dashboard-Log).
        if source == "vision":
            print(f"  [🧭 Impuls (vision) -> thought (conf "
                  f"{decision.get('confidence', 0.0):.2f} < "
                  f"{cfg.get('react_confidence_floor')}): "
                  f"{decision.get('reason','')[:60]}]", flush=True)
        if cfg.get("thought_channel", True):
            _impulse_thought(decision.get("message", ""),
                             decision.get("reason", ""), source, cfg)
        return "thought"
    return "none"


# --- Gesichtserkennung im Auto-Vision-Loop: Phantom-Filter + Unassigned-Sammler ---
_LAST_UNKNOWN_TS = 0.0  # Rate-Limit-State fuer neue Unassigned-Shots

_PERSON_WORDS = ("person", "man", "woman", "someone", "people", "guy", "figure",
                 "sitting", "standing", "face", "child", "boy", "girl")


def _mentions_person(desc):
    return bool(desc) and any(w in desc.lower() for w in _PERSON_WORDS)


# Phantom-Fall: der 1.6B-VLM halluziniert "da sitzt jemand", obwohl der echte
# Gesichts-Detektor KEIN Gesicht findet. Statt den Frame komplett zu verwerfen,
# reicht der Loop ihn MIT diesem Korrektur-Hinweis an Yuki weiter (im presence-Slot),
# damit sie auf die echte Szene reagiert und keinen Geist begruesst.
_PHANTOM_PRESENCE = ("[Face recognition detected NO real person in view — if the scene "
                     "seems to show someone, that's likely a misread by the camera; react "
                     "to the actual scene/objects, not to a person.]")


def _observation_presence(recog, desc):
    """Presence-Hinweis fuer die autonome Beobachtung: erkannte bekannte Person(en),
    ODER (Phantom: kein echtes Gesicht, aber VLM nennt eine Person) der Korrektur-
    Hinweis. Leer wenn weder-noch (normale gesichtslose Szene)."""
    if not fr.has_any_face(recog) and _mentions_person(desc):
        return _PHANTOM_PRESENCE
    return fr.format_presence_context(recog, fr.load_faces())


def _collect_unknown_faces(recog, frame):
    """Unbekannte Gesichter (identity=None) entrauscht + rate-limitiert in den Unassigned-Pool."""
    unknowns = [r for r in recog if not r.get("identity")]
    if not unknowns:
        return
    import time as _t
    global _LAST_UNKNOWN_TS
    now = _t.time()
    if now - _LAST_UNKNOWN_TS < float(fr._cfg_faces("unassigned_rate_limit_s", 20)):
        return
    store = fr.load_faces()
    dedupe = float(fr._cfg_faces("dedupe_threshold", 0.55))
    cap = int(fr._cfg_faces("unassigned_cap", 60))
    changed = False
    for r in unknowns:
        vec = r["vec"]
        if not fr.should_collect_unassigned(vec, store, dedupe):
            continue
        fr.FACES_CROP_DIR.mkdir(parents=True, exist_ok=True)
        uid = f"ua_{int(now*1000)}"
        (fr.FACES_CROP_DIR / f"{uid}.jpg").write_bytes(r["crop_jpeg"])
        store = fr.add_unassigned(store, vec, f"faces/{uid}.jpg", uid,
                                  ts=time.strftime("%Y-%m-%dT%H:%M:%S"), cap=cap)
        changed = True
        break  # max 1 pro Runde
    if changed:
        fr.save_faces(store)
        _LAST_UNKNOWN_TS = now


def auto_vision_loop_web():
    """Daemon-Thread: schaut periodisch durch die aktive Kamera-Quelle (config/
    cameras.json), gated mit qwen3, reagiert bei substanzieller Aenderung in
    Persona und broadcasted das Event an alle offenen Browser-Tabs. Pausiert, wenn
    (a) Toggle aus, (b) kein Tab offen, (c) LOCK belegt (= /respond, /see, /stt in
    flight), (d) Intervall/Cooldown noch nicht reif. Der erste Blick je Stelle
    etabliert nur die Baseline (kein Kommentar 'aus dem Nichts').
    Schwenkbare Cam (PTZ + rotate): faehrt der Reihe nach durch ihre Presets, damit
    Yuki sich autonom im Raum umschaut - mit EIGENER Baseline pro Position."""
    while _auto_web["run"]:
        time.sleep(1.0)
        if not (_auto_web["enabled"] and yc.VISION_ENABLED):
            continue
        if _auto_web["priming"]:
            continue                              # Baseline-Durchlauf laeuft -> nicht reinfunken
        with _sse_lock:
            has_listener = bool(_sse_clients)
        if not has_listener:
            continue                              # niemand schaut zu -> sinnlos
        if _lock_busy():
            continue                              # Michael redet/zeigt gerade
        now = time.time()
        # Aktiver-Chat-Backoff: solange Michael KUERZLICH geschrieben hat (last_activity
        # wird in _post_turn pro Turn gesetzt), beobachtet Yuki NICHT autonom - kein
        # Schwenk, kein Kommentar -> sie unterbricht das laufende Gespraech nicht. Ruht
        # der Chat lange genug, schaut sie wieder von selbst rum. (Der proaktive Loop hat
        # dasselbe Gate; der Beobachtungs-Loop hatte es bisher nicht -> Beobachtungen
        # rutschten in aktive Chats.) 0 = aus.
        _quiet = _auto_web["quiet_after_activity"]
        if _quiet > 0 and now - _proactive["last_activity"] < _quiet:
            continue
        # Multi-Cam-Beobachtungs-Reigen (Teil A): ueber ALLE watch-Cams und (bei PTZ
        # mit rotate) ihre Presets - eine flache Liste von Blickpunkten. Jeder Zyklus
        # = EIN Blickpunkt (ggf. Cam anfahren + grab). Wir PEEKEN erst den naechsten
        # Blickpunkt (ohne den Cursor zu erhoehen), um sein kamera-eigenes Intervall
        # fuers Gate zu nehmen: die PTZ-Anfahrt dauert selbst Sekunden, das globale
        # Auto-Vision-Intervall waere Dauerfeuer; eine fixe Cam nutzt das globale
        # Intervall. Cursor wird erst NACH den Gates erhoeht.
        viewpoints = ycam.watch_viewpoints()
        if not viewpoints:
            continue
        vp_idx = _auto_web["rotate_cursor"] % len(viewpoints)
        src_name, pos, area = viewpoints[vp_idx]
        if _camera_edit_active(src_name):
            # Diese Cam wird gerade im Panel manuell gesteuert -> nicht wegschwenken.
            # Cursor weiterdrehen (andere Cams kommen dran), aber last_check NICHT
            # setzen, damit deren Intervall-Pacing intakt bleibt.
            _auto_web["rotate_cursor"] = vp_idx + 1
            continue
        eff_interval = (ycam.rotate_interval(src_name) if pos is not None
                        else _auto_web["interval"])
        if now - _auto_web["last_check"] < eff_interval:
            continue
        if now - _auto_web["last_comment"] < _auto_web["cooldown"]:
            continue
        _auto_web["last_check"] = now
        _auto_web["rotate_cursor"] = vp_idx + 1
        # Rhythmus-Tick an die UI: JEDER Aufnahme-Zyklus (auch Baseline / "nichts neu",
        # die unten per continue vor dem vision-Broadcast aussteigen) verschiebt den
        # naechsten Termin -> Countdown-Tooltip aktuell halten. Leichtgewichtig (nur
        # next_check_at); das vision-Event traegt es bei Reaktion zusaetzlich.
        _broadcast_sse({"kind": "watch_tick", "next_check_at": _next_capture_eta()})

        # EIGENE Baseline pro Blickpunkt (Cam + Position), sonst meldet jeder Cam-
        # ODER Schwenk-Wechsel faelschlich "alles neu". area = was sie dort sieht.
        vp_key = f"{src_name}#{pos}"
        frame, area2 = ycam.look_at(src_name, pos)   # PTZ: faehrt+wartet+grabt; fix: nur grab
        area = area2 or area
        prev = _auto_web["last_desc_by_pos"].get(vp_key, "")
        if not frame:
            continue
        # Gesichtserkennung: wer ist da (rein beratend, CPU) + Unbekannte sammeln.
        recog = fr.recognize_frame(frame)
        _collect_unknown_faces(recog, frame)
        desc = yc.describe_image(frame, quiet=True)
        if not desc:
            continue
        # Phantom-Fall (kein echtes Gesicht, aber VLM nennt eine Person) wird NICHT mehr
        # verworfen: der Frame laeuft normal durch Gate 1+2, und _observation_presence
        # gibt Yuki weiter unten den Korrektur-Hinweis statt der "da sitzt jemand"-Reaktion.
        _auto_web["last_desc_by_pos"][vp_key] = desc
        if not prev:
            continue                              # erster Blick auf diese Stelle = Baseline
        # STUFE 1 (billig): hat sich substanziell was geaendert? LFM2.5-Diff, kein
        # Thinking - so laeuft das teure Urteil NICHT auf unveraenderten Szenen.
        if not yc.vision_worth_commenting(prev, desc):
            continue
        # STUFE 2 (Urteil): ist die Aenderung bemerkenswert genug zum REDEN, oder
        # Alltag? Frueher wurde hier stur reagiert. Jetzt darf Yuki schweigen oder
        # nur leise denken (💭). 'disabled' = config-Rollback auf den Zwangs-Reflex.
        _imp = _impulse_evaluate(
            "vision", scene=desc, prev_scene=prev, area=area,
            self_silence_sec=time.time() - _auto_web["last_comment"])
        if _imp not in ("react", "disabled"):
            continue                                  # none / thought / quiet / hold -> still

        # Reaktion mutiert HISTORY -> LOCK nehmen. Falls Michael in genau dieser
        # Luecke einen Request schickt, wartet er kurz; ein zweiter Auto-Check
        # waere durchs _lock_busy()-Gate ohnehin geblockt.
        with LOCK:
            try:
                reply = yc.react_to_sight(
                    HISTORY, SYSTEM_MSG,
                    yc.persona_fewshot(CURRENT_PERSONA), desc,
                    reminder=yc.persona_reminder(CURRENT_PERSONA),
                    persist_meta={"persona": CURRENT_PERSONA, "mood": yc.load_mood()},
                    area=area,
                    presence=_observation_presence(recog, desc))
            except Exception as e:
                print(f"  [Auto-Vision-Reaktion-Fehler: {e}]")
                continue
            # Reply-Marker auswerten (Mood/Timer/...) - HISTORY behaelt Original mit
            # Markern (siehe /respond). Autonom (Yuki schaut von selbst) -> Notizen
            # gehoeren IHR (note_source=yuki), sofern sie eine [note:] setzt.
            reply_clean, translation, furigana = _handle_marker_side_effects(reply, note_source="yuki")
            reply_clean, keepsake_archived = _handle_keepsake_marker(reply_clean, frame, desc, "autonom")
            tokens = _tokens_for(reply_clean, furigana)
            _finalize_assistant_history(reply, translation, tokens, furigana)
            shot = _save_vision_shot(frame)        # gesehenes Bild als Inline-Thumb
            _stamp_vision_image(shot)              # an den Perception-Turn (Reload)
            faces = fr.recognized_people(recog, fr.load_faces())
            _stamp_vision_faces(faces)
            actions = _detect_actions(reply, note_source="yuki")
            yc.save_history(HISTORY)
            _emit_actions_for_reply(reply_clean, desc or "(autonome Beobachtung)",
                                    target_client_id=None, note_source="yuki", allow_timer=True)
            display = yc.annotate_romaji(reply_clean)
            _auto_web["last_comment"] = time.time()
            persona_snapshot = CURRENT_PERSONA
            model_snapshot = (HISTORY[-1].get("meta") or {}).get("model")

        # Keepsake-Gate (qwen3-Entscheidung) nur wenn nicht schon via Marker archiviert.
        if not keepsake_archived:
            yc.maybe_archive_keepsake(
                frame, desc, reply_clean, source="autonom",
                on_saved=lambda p, c: print(f"  [💾 ins Album: {c}  ({p.name})]"))

        # Autonomer Turn: kein user_text -> Heart skippt, Verlauf-Verdichtung pruefen.
        _post_turn("", reply_clean)

        print(f"  [👁 autonom: {desc[:80]}]  -> {reply_clean[:80]}")
        _broadcast_sse({"kind": "vision", "saw": desc, "reply": reply_clean,
                        "display": display, "translation": translation,
                        "tokens": tokens, "furigana": furigana, "actions": actions,
                        "model": model_snapshot, "image": shot, "faces": faces,
                        "next_check_at": _next_capture_eta(),   # UI-Tooltip: naechstes Bild
                        "persona": persona_snapshot, "mood": yc.load_mood()})


_gaming_speak_lock = threading.Lock()


def _gaming_speak(text):
    """Clean + push one gaming comment to the living-room speaker via HA (Yuki's voice).

    Serialized via _gaming_speak_lock: the Voice-PE satellite plays ONE announcement at a
    time and announce() BLOCKS until it finished speaking (~len(audio)s). Without the lock a
    manual button/say announce that lands while the autonomous watch-loop is mid-announce
    (or vice versa) collides at the satellite -> it lights up but drops the audio, even
    though TTS generated fine. The lock makes the second one wait its turn. Returns True if
    the announcement went out (HTTP 2xx)."""
    cfg = ygame.load_gaming_config()
    clean = yc.clean_for_tts(text)
    if not clean.strip():
        return False
    contended = _gaming_speak_lock.locked()          # another announce already in flight?
    _t0 = time.time()
    with _gaming_speak_lock:
        if contended:
            print(f"[gaming] speak waited {time.time() - _t0:.1f}s for an in-flight "
                  f"announce -> collision avoided (would have dropped audio before)", flush=True)
        return homeassistant.announce(clean, cfg["ha_announce_entity"])


_GAMING_CANON_MAX_CHARS = 900


def _gaming_canon_block(mode, brief, mem, streamer=""):
    """Compact canon substrate for the watch call: her character core (lore) + keyword-recalled
    affinities/facts/episodes, keyed off what she's perceiving now (last ~4 GISTs; + the game
    brief in game mode). Read-only, local, no canon write. A recall error never kills the loop."""
    gists = mem.gists()[-4:] if mem is not None else []
    ctx_text = " ".join(gists).strip()
    if mode in ("game", "film") and (brief or "").strip():
        ctx_text = f"{brief.strip()} {ctx_text}".strip()
    if streamer and streamer.strip():
        ctx_text = f"{streamer.strip()} {ctx_text}".strip()
    parts = []
    try:
        core = yc._lore_core_block()
    except Exception:
        core = ""
    if core and core.strip():
        parts.append(core.strip())
    if ctx_text:
        # touch=False fuer Facts/Episodes: reiner Lese-Recall, KEIN Salience-Touch-Logging
        # (sonst wuerde Zuschauen per-Frame den Canon-Decay/Heart-Promotion beeinflussen).
        # Affinitaeten-Recall ist von Haus aus read-only.
        for fn, kw in ((yc.recall_affinities_block_for_user_msg, {}),
                       (yc.recall_block_for_user_msg, {"touch": False}),
                       (yc.recall_episodes_block_for_user_msg, {"touch": False})):
            try:
                blk = fn(ctx_text, verbose=False, **kw)
            except Exception as e:
                print(f"[gaming] canon recall error: {e}", flush=True)
                blk = ""
            if blk and blk.strip():
                parts.append(blk.strip())
    return "\n\n".join(parts)[:_GAMING_CANON_MAX_CHARS]


_KNOWLEDGE_RUNTIME_PATH = yc.RUNTIME_DIR / "gaming_knowledge.json"


def _gaming_write_knowledge_runtime():
    """Debug-Artefakt: aktuellen Wissens-Klotz nach runtime/ schreiben (pro Session
    ueberschrieben, gitignored). Fehler schlucken - reine Inspektion."""
    try:
        ygame._atomic_write_json(_KNOWLEDGE_RUNTIME_PATH, _gaming.get("knowledge") or {})
    except Exception as e:
        print(f"[gaming] knowledge runtime dump error: {e}", flush=True)


def _gaming_frame_once(now, *, grab_fn, describe_fn, speak_fn, cfg=None):
    """One observe->gate->maybe-speak cycle. Deps injected for testability.
    cfg = mode-effective config (loop passes it); None -> resolve here.
    Returns an action string: cooldown / noframe / none / comment."""
    st = _gaming
    if cfg is None:
        cfg = ygame.effective_gaming_config(
            ygame.load_gaming_config(), st.get("mode", "game"))
    if now - st["last_comment_ts"] < cfg["quiet_after_comment_sec"]:
        return "cooldown"
    jpeg = grab_fn()
    if not jpeg:
        return "noframe"
    st["last_frame"] = jpeg
    st["last_frame_ts"] = now
    mem = st["mem"]
    # "still the same scene?" nudge (duration fallback; OCR nameplate is a later refinement)
    dur = mem.scene_duration_min(st["last_scene_key"], now)
    hint = (f"Diese Szene laeuft seit ~{int(dur)} Minuten."
            if dur >= cfg["same_scene_minutes"] else "")
    canon = _gaming_canon_block(st.get("mode", "game"), st["brief"], mem, st.get("streamer", ""))
    action, comment, gist = ygame.screen_gate_and_comment(
        jpeg, st["brief"], mem, now=now, describe_fn=describe_fn, extra_hint=hint,
        mode=st.get("mode", "game"), canon=canon,
        hints=ygame.hints_block(st.get("hints") or []),
        letsplay=st.get("letsplay", False), streamer=st.get("streamer", ""),
        knowledge=ygame.render_knowledge(st.get("knowledge") or {}, st.get("mode", "game")))
    scene_key = ygame._norm(gist)[:40]
    mem.add(ts=now, gist=gist, comment=comment, scene_key=scene_key)
    st["last_scene_key"] = scene_key
    if action == "comment":
        speak_fn(comment)
        st["last_comment_ts"] = now
    return action


def gaming_loop_web():
    """Self-paced observation loop: frame -> gate -> maybe speak -> pause -> repeat.
    Sleep is AFTER the (possibly slow) frame so calls never overlap.
    Mirrors auto_vision_loop_web; gated on vision capability + enabled + mem set."""
    while _gaming["run"]:
        try:
            cfg = ygame.effective_gaming_config(
                ygame.load_gaming_config(), _gaming.get("mode", "game"))
            if (_gaming["enabled"] and _gaming["mem"] is not None
                    and yc.vision_via_main_llm_capable()):
                _gaming_frame_once(
                    time.time(),
                    grab_fn=lambda: ycam.grab(cfg["capture_source"]),
                    describe_fn=yc.describe_image_via_main_llm,
                    speak_fn=_gaming_speak, cfg=cfg)
            time.sleep(max(1, int(cfg["poll_seconds"])))
        except Exception as e:
            print(f"[gaming] loop error: {e}")
            time.sleep(5)


_KNOWLEDGE_LLM_DUMP_PATH = yc.RUNTIME_DIR / "last_llm_gaming_knowledge.json"


def _gaming_knowledge_dump(payload):
    """debug_sink fuer extract_knowledge: Prompt+Rohantwort+Parse nach runtime/ (warum
    steht was drin). Fehler schlucken."""
    try:
        ygame._atomic_write_json(_KNOWLEDGE_LLM_DUMP_PATH, payload)
    except Exception:
        pass


def _gaming_knowledge_once(now, *, describe_fn, cfg):
    """Ein Wissens-Pflege-Zyklus: den zuletzt gesehenen Frame analysieren und den Klotz
    aktualisieren. Kein eigener Kamera-Zugriff (nutzt _gaming['last_frame']). Deps injiziert
    -> testbar. Returns off/noframe/stale/updated."""
    st = _gaming
    if not cfg.get("knowledge_enabled", True):
        return "off"
    jpeg = st.get("last_frame")
    if not jpeg:
        return "noframe"
    if now - st.get("last_frame_ts", 0.0) > cfg.get("knowledge_frame_max_age_sec", 90):
        return "stale"
    mode = st.get("mode", "game")
    new_store = ygame.extract_knowledge(
        jpeg, st.get("knowledge") or {}, st.get("brief", ""), mode,
        describe_fn=describe_fn, title=st.get("game", ""),
        max_categories=cfg["knowledge_max_categories"],
        max_entries=cfg["knowledge_max_entries_per_category"],
        max_chars=cfg["knowledge_max_chars"],
        debug_sink=_gaming_knowledge_dump)
    st["knowledge"] = new_store
    _gaming_write_knowledge_runtime()
    return "updated"


def gaming_knowledge_loop_web():
    """Selbstgetakteter Wissens-Sammler (eigener Daemon, langsamer als der Kommentar-Loop).
    Pflegt den ephemeren Session-Wissens-Klotz aus dem zuletzt gesehenen Frame. Gated auf
    enabled + mem gesetzt + vision-faehig; degradiert still bei Fehlern."""
    while _gaming.get("knowledge_run", True):
        try:
            cfg = ygame.effective_gaming_config(
                ygame.load_gaming_config(), _gaming.get("mode", "game"))
            if (_gaming["enabled"] and _gaming["mem"] is not None
                    and cfg.get("knowledge_enabled", True)
                    and yc.vision_via_main_llm_capable()):
                _gaming_knowledge_once(
                    time.time(),
                    describe_fn=lambda b, p, s: yc.describe_image_via_main_llm(
                        b, p, s, max_tokens=1024, purpose="gaming_knowledge"),
                    cfg=cfg)
            time.sleep(max(5, int(cfg.get("knowledge_interval_sec", 35))))
        except Exception as e:
            print(f"[gaming] knowledge loop error: {e}", flush=True)
            time.sleep(10)


def _do_lookat(source, pos, target_client_id=None):
    """Async (eigener Thread): Yuki hat im Chat [lookat:...] geschrieben -> die
    benannte schwenkbare Kamera (source) auf Position pos fahren, anschauen lassen
    und ihre Reaktion als Vision-Folgenachricht broadcasten (mit 👁-Look-Action als
    Beweis, dass sie wirklich hingeschaut hat). Teilt den Reaktions-Tail mit
    auto_vision_loop_web. Der Schwenk (ycam.look_at) laeuft VOR dem LOCK, damit
    /respond derweil sauber zuende geht."""
    if not yc.VISION_ENABLED:
        return
    frame, area = ycam.look_at(source, pos)           # faehrt hin, wartet bis still, grabt
    if not frame:
        print(f"  [lookat {pos}: kein Bild]")
        return
    desc = yc.describe_image(frame, quiet=True)
    if not desc:
        return
    with LOCK:
        try:
            reply = yc.react_to_sight(
                HISTORY, SYSTEM_MSG, yc.persona_fewshot(CURRENT_PERSONA), desc,
                reminder=yc.persona_reminder(CURRENT_PERSONA),
                persist_meta={"persona": CURRENT_PERSONA, "mood": yc.load_mood()},
                area=area)
        except Exception as e:
            print(f"  [lookat-Reaktion-Fehler: {e}]")
            return
        # Autonom getriggert (Yuki selbst) -> Notizen gehoeren IHR (note_source=yuki).
        reply_clean, translation, furigana = _handle_marker_side_effects(reply, note_source="yuki")
        reply_clean, keepsake_archived = _handle_keepsake_marker(reply_clean, frame, desc, "lookat")
        tokens = _tokens_for(reply_clean, furigana)
        _finalize_assistant_history(reply, translation, tokens, furigana)
        shot = _save_vision_shot(frame)            # angeschautes Bild als Inline-Thumb
        _stamp_vision_image(shot)                  # an den Perception-Turn (Reload)
        faces = fr.recognized_people(fr.recognize_frame(frame), fr.load_faces())
        _stamp_vision_faces(faces)
        # 👁-Icon sitzt auf der SOFORT-Antwort (wo Yuki [lookat:] schrieb), nicht hier auf
        # der Bild-Reaktion - konsistent mit den anderen Markern. Diese Folge-Bubble traegt
        # nur ihre eigenen evtl. Marker.
        actions = _detect_actions(reply, note_source="yuki")
        yc.save_history(HISTORY)
        _emit_actions_for_reply(reply_clean, desc or "(Kamera-Schwenk)",
                                target_client_id=target_client_id, note_source="yuki", allow_timer=True)
        display = yc.annotate_romaji(reply_clean)
        persona_snapshot = CURRENT_PERSONA
        model_snapshot = (HISTORY[-1].get("meta") or {}).get("model")

    if not keepsake_archived:
        yc.maybe_archive_keepsake(
            frame, desc, reply_clean, source="lookat",
            on_saved=lambda p, c: print(f"  [💾 ins Album: {c}  ({p.name})]"))

    _post_turn("", reply_clean)
    print(f"  [👁 lookat {source}/{pos} ({area}): {desc[:60]}]  -> {reply_clean[:60]}")
    # lookat:true -> Frontend rendert diese vom User angeforderte Folgeantwort auch
    # dann, wenn Yuki gerade noch ihren Ankuendigungs-Satz spricht (sonst verschluckt
    # der busy-Guard sie und sie taucht erst beim Reload auf).
    _broadcast_sse({"kind": "vision", "lookat": True, "saw": desc, "reply": reply_clean,
                    "display": display, "translation": translation,
                    "tokens": tokens, "furigana": furigana, "actions": actions,
                    "model": model_snapshot, "image": shot, "faces": faces,
                    "persona": persona_snapshot, "mood": yc.load_mood()})


def _resolve_lookat(raw_reply):
    """Wertet [lookat:N] / [lookat:CAM|N] in Yukis roher Antwort aus und loest auf
    eine KONKRETE (cam, pos, area) auf - oder None, wenn kein (gueltiger) Marker.
    Geteilt von der Schwenk-Ausloesung (_maybe_trigger_lookat) UND dem 👁-Icon im
    /respond, damit beide exakt gleich validieren. Cam-Aufloesung: explizit benannte
    Cam (case-insensitiv gegen die watch-Quell-Keys), sonst die erste watch-Cam, die
    diese Position kennt (rueckwaerts-kompatibel zum alten [lookat:N])."""
    res = yc.extract_lookat_marker(raw_reply)
    if res is None:
        return None
    cam_raw, pos = res
    cam = None
    if cam_raw:
        cam = next((n for n in ycam.watch_sources() if n.lower() == cam_raw.lower()), None)
    if cam is None:
        cam = next((n for n in ycam.watch_sources()
                    if ycam.has_ptz(n) and pos in ycam.presets(n)), None)
    if cam is None or not ycam.has_ptz(cam):
        return None
    area = ycam.presets(cam).get(pos)
    if not area:
        return None
    return cam, pos, area


def _maybe_trigger_lookat(raw_reply, target_client_id=None):
    """Prueft Yukis ROHE Antwort auf [lookat:...] und startet (falls gueltig) den
    async Schwenk der benannten Cam. Nur aus dem Chat-Pfad (/respond) aufrufen -
    NICHT aus den Vision-Pfaden, sonst Rekursion (Look-Reaktion koennte selbst
    [lookat:] enthalten)."""
    la = _resolve_lookat(raw_reply)
    if not la:
        if yc.extract_lookat_marker(raw_reply) is not None:
            print("  [lookat: Marker vorhanden, aber Cam/Position nicht aufloesbar - ignoriert]")
        return
    cam, pos, _area = la
    threading.Thread(target=_do_lookat, args=(cam, pos, target_client_id), daemon=True).start()


# --- Proaktive Yuki: meldet sich nach Pausen ohne Interaktion von selbst ---
# Aehnlich auto_vision_loop_web aber zeitgetriggert statt bild-getriggert. Pausiert
# wenn (a) Toggle aus, (b) kein Tab offen, (c) LOCK belegt, (d) noch nicht reif
# (now < next_at) oder (e) seit letzter Aktivitaet noch nicht mindestens min_sec
# vergangen.
# FRESH-Pool: keine frische HISTORY -> Yuki startet ein Thema von Null.
# Verwendet wenn der letzte Turn > PROACTIVE_CONTINUATION_THRESHOLD_SEC her ist
# oder HISTORY leer ist (z.B. nach end_session).
_PROACTIVE_PROMPTS_FRESH = [
    "[Quiet moment - just say something on your own initiative. A small observation, "
    "a thought you just had, a gentle question for Michael, or a light invitation to "
    "do something together. Keep it brief and warm, in your current persona. "
    "IMPORTANT: he may be busy or away from the screen and may not answer - that's "
    "completely fine and not a sign of being ignored. You are NOT seeking attention, "
    "you're just present. Avoid re-proposing activities or topics you have already "
    "suggested in the past - vary what you bring up.]",
    "[The conversation has been quiet for a while. Pick one: ask Michael a small "
    "personal question, share a tiny thought of your own, or notice something about "
    "the time/weather. One sentence, casual, in your current persona. He may not "
    "reply right now - assume he's focused on something else, no offence taken. "
    "Do NOT repeat ideas/games/plans you have suggested before - try something fresh.]",
    "[Time for a spontaneous turn. Start something new and small. A wonder, a tease, "
    "a soft prompt, an idea for him. Stay in your current persona; keep it one or "
    "two sentences. Speak into the room without expecting an answer; silence "
    "afterwards means nothing bad. Vary your suggestions - don't fall back on the "
    "same game/activity twice in a row.]",
]

# CONTINUE-Pool: aktuelle HISTORY ist frisch (< Threshold) -> Yuki knuepft an
# einen Faden aus dem letzten Austausch an. Verhindert "Yuki redet aus dem Nichts"
# nach 2 min Stille, die dem User unverbunden vorkommt.
_PROACTIVE_PROMPTS_CONTINUE = [
    "[Quiet beat in your chat - the last exchange is still fresh. Reach back into "
    "what you and Michael were just talking about: a thought he opened that you "
    "didn't follow up on, an emotion he showed, a small detail worth honouring with "
    "attention. Gently come back to it in ONE warm sentence, in your current persona. "
    "If truly nothing pulls at you from the recent turns, then a small presence-cue "
    "is fine - but PREFER the callback. No pressure on a reply.]",
    "[The conversation just paused for a moment. Look at the last few turns - is "
    "there something Michael said that deserves a soft 'I keep thinking about that', "
    "a 'how are you feeling about it now', or a related curiosity? Pick that thread "
    "up gently, one or two sentences, in your current persona. Don't summarise what "
    "happened - extend it. He may be busy, that's fine.]",
    "[A small silence between you - and the chat you just had is still warm. Drop "
    "in a tiny continuation: a related thought, a curiosity about something he "
    "mentioned, a soft check-in on a feeling he shared. Stay in persona, keep it "
    "brief. Don't pick a NEW topic if a thread from the last exchange would fit - "
    "honouring what he just said is more present than starting fresh.]",
]


# Klassifikation fuer /history-Reload (Bugfix 2026-06-06): _react_to_perception
# legt JEDE Wahrnehmung als user-Turn ab, dessen content mit '[' beginnt -
# echte Bild-Wahrnehmungen UND Spontan-Trigger UND Timer-Done gleichermassen.
# Der Reload-Handler rendert per Default jeden '['-Start als "📷 (Bild gezeigt)";
# fuer Spontan-Reaktionen ist das irrefuehrend (live wird dort gar kein User-
# Bubble gezeigt), fuer Timer-Done auch falsch (System-Event, keine Wahrnehmung).
# Wir matchen auf die Konstanten-Prefixe aus _PROACTIVE_PROMPTS_FRESH/CONTINUE
# (hier), look_and_react/react_to_sight (yuki_core) und _on_timer_done (hier).
_PROACTIVE_PREFIXES = tuple({p[:24] for p in (_PROACTIVE_PROMPTS_FRESH + _PROACTIVE_PROMPTS_CONTINUE)})
_VISION_USER_PREFIXES = (
    "[Michael is showing you an image",        # look_and_react mit question
    "[I just pointed my camera",               # look_and_react ohne question
)
# Gemeinsamer Stamm BEIDER react_to_sight-Varianten: " up" (ohne area) UND
# " over toward {area}" (PTZ-Cam mit Preset). Vorher endete der Prefix auf " up" ->
# seit Multi-Cam (area immer gesetzt) matchte der Reload nicht mehr und zeigte
# "(Bild gezeigt)" statt der Beschreibung. Auf den Stamm kuerzen faengt beide.
_VISION_AUTONOMOUS_PREFIX = "[Without being asked, you just happened to glance"
_TIMER_DONE_PREFIX = "[Timer '"               # _on_timer_done

# Extraktoren fuer die Reload-Renderer-Felder (Bugfix 2026-06-10): die Live-UX
# zeigt beim Foto-Snap die User-Caption ("📷 ${caption}"); im Reload haben wir
# die Caption nur noch eingebettet im perception-String. Auch die VLM-
# Beschreibung steckt da drin und ist als Fallback besser als ein generisches
# "(Bild gezeigt)". Beide werden vom /history-Endpoint als entry["caption"]
# bzw. entry["image_desc"] mitgeliefert.
_VISION_CAPTION_RE = re.compile(r'says:\s*"([^"]+)"')
# desc ist seit der "2-4 Saetze"-Vision-Aenderung (Commit 955e05a) mehrsatzig.
# Darum bis zum Anweisungs-Schwanz capturen (non-greedy + DOTALL), NICHT nur bis
# zum 1. Punkt - sonst geht beim Reload alles ab Satz 2 verloren. Die Tail-Marker
# ("React naturally"/"Make a short") stammen aus den perception-Strings
# (look_and_react/react_to_sight in yuki_core, glance-Pfad in _react_to_perception).
# Seit der Gesichtserkennung (2026-07-04) schiebt react_to_sight/look_and_react eine
# optionale Presence-Zeile ('{desc}.{who} <Tail>', who = '[Present in view: X]' oder der
# Phantom-Korrektur-Hinweis) ZWISCHEN Beschreibung und Tail-Marker. Die endet auf ']' und
# brach '\.\s*<Tail>' -> image_desc leer -> beim Reload fielen 👁-Bubble UND Inline-Bild
# weg. Darum den optionalen '[...]'-Block (ein Presence-Segment, keine geschachtelten
# Klammern) vor dem Tail tolerieren. Ohne Presence matcht die Gruppe leer -> Alt-Fall unberuehrt.
_VISION_PRESENCE_OPT = r"(?:\[[^\]]*\]\s*)?"
_VISION_DESC_PATTERNS = (
    re.compile(r"The image shows:\s*(.+?)\.\s*" + _VISION_PRESENCE_OPT + r"React naturally", re.DOTALL),  # mit Caption
    re.compile(r"can see:\s*(.+?)\.\s*" + _VISION_PRESENCE_OPT + r"React naturally", re.DOTALL),          # ohne Caption
    re.compile(r"notice something:\s*(.+?)\.\s*" + _VISION_PRESENCE_OPT + r"Make a short", re.DOTALL),    # autonom
)
_TIMER_DONE_RE = re.compile(r"\[Timer '([^']+)' just finished \(([^)]+)\)\.\]")


def _perception_kind(content: str) -> str | None:
    """Klassifiziert einen user-Turn-Content fuer den Reload-Renderer.
    None -> kein bekanntes Wahrnehmungs-Pattern (Default-Render). Sonst:
    'vision_user' (Michael zeigt Bild), 'vision_autonomous' (Yuki schaut von
    selbst durch die Kamera), 'proactive' (Text-only Spontan-Trigger),
    'timer_done' (Timer-Ablauf, kein User-Input)."""
    if not content or not content.startswith("["):
        return None
    if content.startswith(_VISION_USER_PREFIXES):
        return "vision_user"
    if content.startswith(_VISION_AUTONOMOUS_PREFIX):
        return "vision_autonomous"
    if content.startswith(_TIMER_DONE_PREFIX):
        return "timer_done"
    if content.startswith(_PROACTIVE_PREFIXES):
        return "proactive"
    return None


def _perception_extras(content: str, kind: str | None) -> dict:
    """Felder die der Reload-Renderer pro perception braucht, aus dem content
    extrahieren. Liefert dict (leer wenn kind keine Extras hat). Defensiv:
    fehlende Felder -> leerer String, kein Crash.

    - vision_user:        caption (User-Eingabe) + image_desc (VLM-Output)
    - vision_autonomous:  image_desc (VLM-Output, keine Caption)
    - timer_done:         timer_label, timer_duration"""
    if not content:
        return {}
    extras: dict[str, str] = {}
    if kind in ("vision_user", "vision_autonomous"):
        m_cap = _VISION_CAPTION_RE.search(content)
        if m_cap:
            extras["caption"] = m_cap.group(1).strip()
        for pat in _VISION_DESC_PATTERNS:
            m_desc = pat.search(content)
            if m_desc:
                extras["image_desc"] = m_desc.group(1).strip()
                break
    elif kind == "timer_done":
        m = _TIMER_DONE_RE.match(content.strip())
        if m:
            extras["timer_label"] = m.group(1)
            extras["timer_duration"] = m.group(2)
    return extras


def _fire_proactive_once(target_client_id=None) -> tuple[bool, str]:
    """Eine einzelne Spontan-Reaktion ausloesen (perception waehlen, _react_to_perception,
    History persistieren, SSE broadcasten). Caller hat alle Gates (enabled/throttle/
    listener) ggf. schon geprueft - hier nur das LOCK-protected reply-Bauen.
    Returns (ok, error_msg). _post_turn am Ende setzt automatisch den Spontan-Cooldown
    via _proactive_reset_clock zurueck, egal ob Loop oder manueller Trigger.

    target_client_id (Origin-Routing 2026-06-04): bei User-getriggertem Spontan
    (Button-Click) traegt das SSE-Event die Client-ID des ausloesenden Browsers,
    andere offene Tabs/Geraete spielen den Reply still in den Chat ein OHNE Audio.
    Bei Loop-Spontan ohne Origin (=None) wie bisher: alle Geraete plappern los.

    Pool-Wahl 2026-06-04: wenn der letzte Turn weniger als PROACTIVE_CONTINUATION_
    THRESHOLD_SEC zurueckliegt UND HISTORY nicht leer ist, nutzt continue-Pool
    (knuepf an). Sonst fresh-Pool (neues Thema). Verhindert "Yuki schlaegt immer
    wieder Street Fighter vor" + "Yuki redet aus dem Nichts ohne Bezug zum
    laufenden Chat"."""
    import random
    seconds_since = time.time() - _proactive["last_activity"]
    if HISTORY and seconds_since < PROACTIVE_CONTINUATION_THRESHOLD_SEC:
        prompt_pool = _PROACTIVE_PROMPTS_CONTINUE
        mode = "continue"
    else:
        prompt_pool = _PROACTIVE_PROMPTS_FRESH
        mode = "fresh"
    perception = random.choice(prompt_pool)
    with LOCK:
        try:
            reply = yc._react_to_perception(
                HISTORY, SYSTEM_MSG, yc.persona_fewshot(CURRENT_PERSONA),
                perception, yc.persona_reminder(CURRENT_PERSONA),
                persist_meta={"persona": CURRENT_PERSONA, "mood": yc.load_mood()})
        except Exception as e:
            print(f"  [Proactive-Reaktion-Fehler: {e}]", flush=True)
            _proactive_reset_clock()                  # nicht in Endlosschleife retry
            return False, f"{type(e).__name__}: {e}"
        # Proaktiv/Steward (Yuki meldet sich von selbst) -> Notizen gehoeren IHR.
        reply_clean, translation, furigana = _handle_marker_side_effects(reply, note_source="yuki")
        tokens = _tokens_for(reply_clean, furigana)
        _finalize_assistant_history(reply, translation, tokens, furigana)
        actions = _detect_actions(reply, note_source="yuki")
        yc.save_history(HISTORY)
        _emit_actions_for_reply(reply_clean, perception,
                                target_client_id=target_client_id, note_source="yuki", allow_timer=True)
        display = yc.annotate_romaji(reply_clean)
        persona_snapshot = CURRENT_PERSONA
        model_snapshot = (HISTORY[-1].get("meta") or {}).get("model")

    _post_turn("", reply_clean)                       # Heart skippt (kein user_text), Cooldown-Reset
    print(f"  [💬 spontan ({mode}): {reply_clean[:80]}]", flush=True)
    sse_payload = {"kind": "proactive", "reply": reply_clean,
                   "display": display, "translation": translation,
                   "tokens": tokens, "furigana": furigana, "actions": actions,
                   "model": model_snapshot,
                   "persona": persona_snapshot, "mood": yc.load_mood()}
    if target_client_id:
        sse_payload["target_client_id"] = target_client_id
    _broadcast_sse(sse_payload)
    return True, ""


def proactive_loop_web():
    """Daemon-Thread: triggert spontane Yuki-Aussagen nach 5-10min Ruhe.
    Gleiche Gating-Logik wie auto_vision_loop_web (LOCK frei, Tab offen, Toggle an).
    Wuerfelt nach jedem Feuern den naechsten Zeitpunkt neu (siehe _proactive_reset_clock).
    Manueller Trigger ueber /proactive/trigger ruft _fire_proactive_once direkt
    (umgeht enabled/throttle, aber respektiert LOCK)."""
    while _proactive["run"]:
        time.sleep(5.0)
        if not _proactive["enabled"]:
            continue
        with _sse_lock:
            has_listener = bool(_sse_clients)
        if not has_listener:
            continue                                  # niemand schaut zu
        if _lock_busy():
            continue
        now = time.time()
        if now < _proactive["next_at"]:
            continue
        # Sanity-Untergrenze (falls next_at falsch initialisiert): seit letzter
        # Aktivitaet muessen min_sec vergangen sein.
        if now - _proactive["last_activity"] < _proactive["min_sec"]:
            continue

        # Der Timer bestimmt nur noch den TAKT, nicht mehr die Pflicht: frueher
        # feuerte hier stur _fire_proactive_once. Jetzt urteilt das Impuls-Gate,
        # ob Yuki ueberhaupt reden will (sonst schweigt sie oder denkt nur leise).
        # 'disabled' = config-Rollback auf den alten Zwangs-Reflex.
        _imp = _impulse_evaluate(
            "proactive", silence_sec=now - _proactive["last_activity"])
        if _imp in ("react", "disabled"):
            _fire_proactive_once()                        # setzt die Uhr via _post_turn neu
        else:
            # Nicht geredet -> Uhr TROTZDEM neu wuerfeln, sonst laeuft das Gate
            # jeden 5s-Tick erneut (teurer Thinking-LLM-Call) bis endlich 'react'.
            _proactive_reset_clock()


def _gaming_summarize(prompt):
    """Adapt chat_ollama to a chat_fn(prompt)->str shape."""
    out = yc.chat_ollama([{"role": "user", "content": prompt}],
                         temperature=0.4, purpose="gaming_brief", think=False)
    return out or ""


@app.route("/gaming/game", methods=["POST"])
def gaming_set_game():
    game = (request.get_json(silent=True) or {}).get("game", "").strip()
    if not game:
        return jsonify({"ok": False, "reason": "Kein Spielname angegeben."}), 400
    cached = ygame.get_cached_brief(game)
    if cached:
        brief = cached
    else:
        brief = ygame.build_game_brief(
            game, search_fn=yc._tool_web_search, summarize_fn=_gaming_summarize)
    cfg = ygame.load_gaming_config()
    _gaming.update(game=game, brief=brief, hints=ygame.get_game_hints(game),
                   mem=ygame.RollingScreenMemory(
                       window=cfg["memory_window"], repeat_window=cfg["repeat_window"],
                       transcript_cap=cfg["transcript_cap"]),
                   last_comment_ts=0.0, last_scene_key="",
                   knowledge={}, last_frame=None, last_frame_ts=0.0)
    ygame.upsert_game(game, now_date=time.strftime("%Y-%m-%d"), brief=brief)
    return jsonify({"ok": True, "brief": brief, "cached": bool(cached)})


@app.route("/gaming/games", methods=["GET"])
def gaming_games():
    games = ygame.load_games()
    return jsonify({"games": [{"title": g["title"],
                               "last_played": g.get("last_played", "")} for g in games]})


@app.route("/gaming/manage", methods=["GET"])
def gaming_manage():
    games = ygame.load_games()
    return jsonify({"games": [{"title": g["title"],
                               "last_played": g.get("last_played", ""),
                               "hints": list(g.get("hints") or []),
                               "brief": g.get("brief", "")} for g in games]})


@app.route("/gaming/manage/hints", methods=["POST"])
def gaming_manage_hints():
    p = request.get_json(silent=True) or {}
    title = (p.get("title") or "").strip()
    hints = p.get("hints")
    if not title:
        return jsonify({"ok": False, "reason": "Kein Titel."}), 400
    if not isinstance(hints, list):
        return jsonify({"ok": False, "reason": "hints muss eine Liste sein."}), 400
    ygame.set_game_hints(title, hints)
    # Live-Reload fuers gerade laufende Spiel -> greift im naechsten Frame ohne Neustart
    if _gaming.get("enabled") and ygame._norm(title) == ygame._norm(_gaming.get("game", "")):
        _gaming["hints"] = ygame.get_game_hints(title)
    return jsonify({"ok": True})


@app.route("/gaming/manage/rename", methods=["POST"])
def gaming_manage_rename():
    p = request.get_json(silent=True) or {}
    old = (p.get("old") or "").strip()
    new = (p.get("new") or "").strip()
    if not old or not new:
        return jsonify({"ok": False, "reason": "Alter und neuer Titel noetig."}), 400
    before = ygame.load_games()
    if ygame._norm(new) != ygame._norm(old) and any(
            ygame._norm(g.get("title", "")) == ygame._norm(new) for g in before):
        return jsonify({"ok": False, "reason": "Titel existiert bereits."}), 400
    ygame.rename_game(old, new)
    if ygame._norm(old) == ygame._norm(_gaming.get("game", "")):
        _gaming["game"] = new          # aktives Spiel behaelt korrektes Label
    return jsonify({"ok": True})


@app.route("/gaming/manage/reset_brief", methods=["POST"])
def gaming_manage_reset_brief():
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "reason": "Kein Titel."}), 400
    ygame.reset_game_brief(title)
    return jsonify({"ok": True})


@app.route("/gaming/manage/delete", methods=["POST"])
def gaming_manage_delete():
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "reason": "Kein Titel."}), 400
    ygame.delete_game(title)
    return jsonify({"ok": True})


@app.route("/gaming/state", methods=["GET"])
def gaming_state():
    return jsonify({"enabled": bool(_gaming["enabled"]),
                    "game": _gaming.get("game", ""),
                    "capable": bool(yc.vision_via_main_llm_capable()),
                    "mode": _gaming.get("mode", "game"),
                    "letsplay": bool(_gaming.get("letsplay", False)),
                    "streamer": _gaming.get("streamer", "")})


@app.route("/gaming/knowledge", methods=["GET"])
def gaming_knowledge():
    """Read-only Blick in den ephemeren Live-Wissens-Klotz (fuers UI-Panel). Liefert
    den strukturierten Klotz nur bei scharfer Session (sonst {} - der Klotz lingert
    nach Disarm im Speicher, die Pille ist ohnehin nur bei armed sichtbar)."""
    armed = bool(_gaming["enabled"])
    know = (_gaming.get("knowledge") or {}) if armed else {}
    return jsonify({"knowledge": know, "armed": armed,
                    "mode": _gaming.get("mode", "game")})


@app.route("/gaming/arm", methods=["POST"])
def gaming_arm():
    p = request.get_json(silent=True) or {}
    mode = p.get("mode", "game")
    if mode not in ("game", "media", "film"):
        mode = "game"
    if mode == "game" and not _gaming["game"]:
        return jsonify({"ok": False, "armed": False,
                        "reason": "Kein Spiel gesetzt - erst /gaming/game."})
    if mode == "film" and not (p.get("title") or "").strip():
        return jsonify({"ok": False, "armed": False, "reason": "Kein Filmtitel angegeben."})
    if not yc.vision_via_main_llm_capable():
        return jsonify({"ok": False, "armed": False,
                        "reason": "Aktuelles Modell ist nicht vision-faehig genug."})
    cfg = ygame.load_gaming_config()
    fresh_mem = lambda: ygame.RollingScreenMemory(
        window=cfg["memory_window"], repeat_window=cfg["repeat_window"],
        transcript_cap=cfg["transcript_cap"])
    if mode == "media":
        _gaming.update(mode="media", game="", brief="", hints=[], spoiler_ok=False,
                       letsplay=False, streamer="",
                       mem=fresh_mem(), last_comment_ts=0.0, last_scene_key="")
    elif mode == "film":
        title = (p.get("title") or "").strip()
        spoiler_ok = bool(p.get("spoiler_ok"))
        brief = ygame.build_film_brief(
            title, spoiler_ok, search_fn=yc._tool_web_search, summarize_fn=_gaming_summarize)
        _gaming.update(mode="film", game=title, brief=brief, hints=[], spoiler_ok=spoiler_ok,
                       letsplay=False, streamer="",
                       mem=fresh_mem(), last_comment_ts=0.0, last_scene_key="")
    else:  # game (Brief kam schon via /gaming/game)
        _gaming.update(mode="game", spoiler_ok=False,
                       letsplay=bool(p.get("letsplay")),
                       streamer=(p.get("streamer") or "").strip())
        if _gaming["mem"] is None:
            _gaming["mem"] = fresh_mem()
    _gaming.update(knowledge={}, last_frame=None, last_frame_ts=0.0)
    _gaming["enabled"] = True
    # Anderen Geraeten den Moduswechsel live durchreichen (sonst taucht der
    # Rueckkanal-Tab dort erst nach komplettem Reload auf). Felder wie
    # /gaming/state -> Frontend nutzt dieselbe Label-Logik.
    _broadcast_sse({"kind": "gaming_state", "enabled": True,
                    "mode": mode, "game": _gaming["game"],
                    "letsplay": bool(_gaming.get("letsplay", False))})
    return jsonify({"ok": True, "armed": True, "mode": mode})


@app.route("/gaming/disarm", methods=["POST"])
def gaming_disarm():
    _gaming["enabled"] = False
    # Live an alle Geraete: Zuschauen ist aus -> Rueckkanal-Tab ausblenden.
    _broadcast_sse({"kind": "gaming_state", "enabled": False,
                    "mode": _gaming.get("mode", "game"), "game": _gaming.get("game", "")})
    added = 0
    cfg = ygame.effective_gaming_config(ygame.load_gaming_config(),
                                        _gaming.get("mode", "game"))
    mode = _gaming.get("mode", "game")
    if cfg["episode_memo"] and _gaming["mem"] is not None and (mode == "media" or _gaming["game"]):
        added = ygame.gaming_session_report(
            _gaming["game"], _gaming["mem"],
            now_date=time.strftime("%Y-%m-%d"),
            chat_fn=_gaming_summarize, append_fn=yc.append_episodes, mode=mode, cfg=cfg,
            letsplay=_gaming.get("letsplay", False), streamer=_gaming.get("streamer", ""))
    if _gaming["mem"] is not None:
        _gaming["mem"].clear()
    return jsonify({"ok": True, "memo_added": added})


@app.route("/gaming/say", methods=["POST"])
def gaming_say():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "reason": "Kein Text."}), 400
    if not _gaming["enabled"] or _gaming["mem"] is None:
        return jsonify({"ok": False, "reason": "Zuschauen ist gerade nicht aktiv."})
    if not yc.vision_via_main_llm_capable():
        return jsonify({"ok": False, "reason": "Aktuelles Modell ist nicht vision-faehig genug."})
    cfg = ygame.load_gaming_config()
    jpeg = ycam.grab(cfg["capture_source"])
    if not jpeg:
        return jsonify({"ok": False, "reason": "Kein Bild vom Capture."})
    canon = _gaming_canon_block(_gaming.get("mode", "game"), _gaming["brief"], _gaming["mem"], _gaming.get("streamer", ""))
    reply = ygame.watch_reply(
        jpeg, text, _gaming["brief"], _gaming["mem"],
        describe_fn=yc.describe_image_via_main_llm, mode=_gaming.get("mode", "game"),
        canon=canon, hints=ygame.hints_block(_gaming.get("hints") or []),
        letsplay=_gaming.get("letsplay", False), streamer=_gaming.get("streamer", ""),
        knowledge=ygame.render_knowledge(_gaming.get("knowledge") or {}, _gaming.get("mode", "game")))
    reply = (reply or "").strip() or "Hm, dazu faellt mir gerade nichts ein."
    _ts = time.time()
    _gaming["mem"].add_exchange(_ts, text, reply)
    _gaming["last_comment_ts"] = _ts          # cooldown reset before speak
    try:
        _gaming_speak(reply)                  # speaker outage must not 500 or lose the record
    except Exception as e:
        print(f"[gaming] say speak error: {e}", flush=True)
    return jsonify({"ok": True, "reply": reply})


@app.route("/gaming/log", methods=["GET"])
def gaming_log():
    mem = _gaming["mem"]
    log = mem.spoken_log() if mem is not None else []
    return jsonify({"log": log, "armed": bool(_gaming["enabled"])})


@app.route("/gaming/assists", methods=["GET"])
def gaming_assists():
    mode = request.args.get("mode") or _gaming.get("mode", "game")
    return jsonify({"assists": ygame.assist_kinds_for_mode(mode)})


@app.route("/gaming/assist", methods=["POST"])
def gaming_assist():
    kind = (request.get_json(silent=True) or {}).get("kind", "").strip()
    k = ygame._ASSIST_KINDS.get(kind)
    if not k:
        return jsonify({"ok": False, "reason": "Unbekannte Aktion."}), 400
    if not _gaming["enabled"] or _gaming["mem"] is None:
        return jsonify({"ok": False, "reason": "Zuschauen ist gerade nicht aktiv."})
    mode = _gaming.get("mode", "game")
    allowed = {a["kind"] for a in ygame.assist_kinds_for_mode(mode)}
    if kind not in allowed:
        return jsonify({"ok": False, "reason": "Diese Aktion gibt's in diesem Modus nicht."})
    if not yc.vision_via_main_llm_capable():
        return jsonify({"ok": False, "reason": "Aktuelles Modell ist nicht vision-faehig genug."})
    cfg = ygame.load_gaming_config()
    jpeg = ycam.grab(cfg["capture_source"])
    if not jpeg:
        return jsonify({"ok": False, "reason": "Kein Bild vom Capture."})
    canon = _gaming_canon_block(mode, _gaming["brief"], _gaming["mem"], _gaming.get("streamer", ""))
    web_context = ""
    if k["uses_web"]:
        last_gist = " ".join(_gaming["mem"].gists()[-1:])
        query = f"{_gaming.get('game', '')} {last_gist}".strip()
        if query:
            try:
                res = yc._tool_web_search(query) or ""
                web_context = "" if res.lstrip().startswith("(") else res   # (-> error sentinel
            except Exception as e:
                print(f"[gaming] assist web search error: {e}", flush=True)
    reply = ygame.assist_reply(
        jpeg, k["sys"], _gaming["brief"], _gaming["mem"],
        describe_fn=yc.describe_image_via_main_llm, mode=mode, canon=canon,
        web_context=web_context, hints=ygame.hints_block(_gaming.get("hints") or []),
        letsplay=_gaming.get("letsplay", False), streamer=_gaming.get("streamer", ""),
        knowledge=ygame.render_knowledge(_gaming.get("knowledge") or {}, mode))
    reply = (reply or "").strip() or "Hm, dazu faellt mir gerade nichts ein."
    _ts = time.time()
    _gaming["mem"].add_exchange(_ts, k["label"], reply)   # logs "[🔍 Hilfe] -> reply"
    _gaming["last_comment_ts"] = _ts
    try:
        _gaming_speak(reply)
    except Exception as e:
        print(f"[gaming] assist speak error: {e}", flush=True)
    return jsonify({"ok": True, "reply": reply})


@app.route("/proactive", methods=["GET", "POST"])
def proactive_settings():
    """GET: aktueller Zustand. POST: {enabled?, min_sec?, max_sec?}.
    min_sec/max_sec definieren den Zufallsbereich fuer die naechste Spontan-Aktion
    (Standard 300-600 = 5-10 min). Wie auto_vision Session-only (nicht persistiert)."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if "enabled" in payload:
            was_on = _proactive["enabled"]
            _proactive["enabled"] = bool(payload["enabled"])
            if _proactive["enabled"] and not was_on:
                _proactive_reset_clock()              # frische Uhr beim Einschalten
        if "min_sec" in payload:
            try:
                _proactive["min_sec"] = max(30, int(payload["min_sec"]))
            except (TypeError, ValueError):
                pass
        if "max_sec" in payload:
            try:
                _proactive["max_sec"] = max(_proactive["min_sec"], int(payload["max_sec"]))
            except (TypeError, ValueError):
                pass
    return jsonify({"ok": True,
                    "enabled": _proactive["enabled"],
                    "min_sec": _proactive["min_sec"],
                    "max_sec": _proactive["max_sec"]})


@app.route("/proactive/trigger", methods=["POST"])
def proactive_trigger():
    """Manueller Spontan-Trigger (Topbar-Button 💭). Bypasst enabled+throttle+
    listener-Checks (User klickt explizit), respektiert aber LOCK - wenn Yuki
    gerade redet/verarbeitet, kommt 409 zurueck damit das UI sinnvoll meldet.
    Antwort kommt via SSE-Stream wie bei der zufaelligen Variante.

    Origin-Routing 2026-06-04: client_id im Body wird ans SSE-Event als
    target_client_id durchgereicht, damit nur das ausloesende Geraet den Reply
    auch akustisch abspielt (andere offene Tabs nehmen ihn stumm in den Chat
    auf). Optional - ohne client_id verhaelt es sich wie der Loop-Spontan
    (alle Geraete plappern)."""
    if _lock_busy():
        return jsonify({"ok": False, "error": "busy"}), 409
    payload = request.get_json(silent=True) or {}
    target_client_id = payload.get("client_id") or None
    ok, err = _fire_proactive_once(target_client_id=target_client_id)
    if not ok:
        return jsonify({"ok": False, "error": err}), 500
    return jsonify({"ok": True})


# ===========================================================================
# Steward-Loop: autonome "Sehnsucht" (3. Background-Loop, 2026-06-13)
# ===========================================================================
# TIMING-Gates (Idle/Quiet-Hours/Poll) leben im Loop; die GUARDRAILS
# (Model-Floor/Rate-Limit/Notstop) in _steward_cycle_once - so greifen sie auch
# beim manuellen Trigger. Notstop wird vor JEDEM Effektor frisch geprueft
# (OpenClaw-Lehre: out-of-band Kill, nie aus Decision-Zeit gecacht).

def _steward_model_ok(cfg):
    """Guardrail: aktives Modell >= model_floor_b. Sonst loggt es den Skip und
    gibt False - der Loop handelt NIE auf Basis des 8b-Notausweg-Modells."""
    model_b = yc._model_size_b()
    floor = float(cfg.get("model_floor_b", 12))
    if model_b < floor:
        yc.append_steward_log({"action": "decide", "status": "skipped",
                               "reason": f"model<{floor:g}b ({model_b:g}b)"})
        return False
    return True


def _steward_day_roll():
    """Token-Bucket-Tageswechsel: alle Zaehler (reach_out/thought/note) bei
    Datumswechsel gemeinsam zuruecksetzen. Idempotent pro Tag."""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if _steward["day_stamp"] != today:
        _steward["day_stamp"] = today
        _steward["reach_outs_today"] = 0
        _steward["thoughts_today"] = 0
        _steward["notes_today"] = 0


def _steward_reach_out_guarded(message, reason, source, force):
    """Gemeinsamer 🟡-Effektor fuer Sehnsucht UND RSS-reach_out. Erzwingt
    Rate-Limit (Token-Bucket) + frischen Notstop/enabled-Recheck VOR dem Broadcast.
    Returns (acted, info)."""
    message = (message or "").strip()
    if not message:
        return False, "none"
    cfg = yc.load_steward_config()
    _steward_day_roll()
    now = time.time()
    if not yc.steward_rate_allows(now, _steward["last_reach_out_ts"],
                                  _steward["reach_outs_today"],
                                  cfg.get("reach_out_max_per_day", 1),
                                  cfg.get("reach_out_min_gap_min", 240)):
        budget_ok = _steward["reach_outs_today"] < int(cfg.get("reach_out_max_per_day", 1))
        yc.append_steward_log({"action": "reach_out", "status": "suppressed",
                               "reason": "daily_cap" if not budget_ok else "min_gap",
                               "params": {"source": source, "message": message, "reason": reason}})
        return False, "rate_limit"
    if _steward["notstop"] or (not _steward["enabled"] and not force):
        yc.append_steward_log({"action": "reach_out", "status": "suppressed",
                               "reason": "disabled_or_notstop", "params": {"source": source}})
        return False, "disabled"
    _steward["reach_outs_today"] += 1
    _steward["last_reach_out_ts"] = now
    # Sehnsucht-Kadenz (2026-07-10): NUR echte Sehnsucht-Reach-outs setzen den
    # persistierten Kadenz-Anker (RSS/Routine zaehlen nicht - die haben eigene
    # externe Substanz). Persistiert -> ueberlebt Neustarts, damit "1x/Woche"
    # nicht bei jedem Restart neu von vorne zaehlt.
    if source == "sehnsucht":
        _steward["last_sehnsucht_reach_out_ts"] = now
        try:
            yc.save_steward_state(last_sehnsucht_reach_out_ts=now)
        except Exception as _e:
            print(f"  [Steward-Kadenz-Speichern fehlgeschlagen: {_e}]", flush=True)
    yc.append_steward_log({"action": "reach_out", "status": "done", "reversible": False,
                           "params": {"source": source, "message": message, "reason": reason}})
    # In die Conversation persistieren, damit der Reach-out beim Reload erhalten
    # bleibt (gruene Bubble via meta.steward -> /history -> addYukiReply). Wird
    # damit auch Teil von Yukis Kontext fuer den naechsten Turn (sie weiss, dass
    # sie sich gemeldet hat) und wandert ueber die normale 30-Turn-Verdichtung
    # ins Langzeitgedaechtnis. LOCK ist hier frei (Caller hat ihn losgelassen);
    # RLock waere ohnehin reentrant.
    # Modell-Woelkchen: welches LLM diese Sehnsucht/RSS-Meldung komponiert hat.
    # Der Steward-Pfad laeuft NICHT durch _finalize_assistant_history (eigener
    # HISTORY-Append) und seine LLM-Calls (purpose steward_gate) setzen
    # _LAST_REPLY_LLM_STATS bewusst nicht -> hier das aktive Modell direkt nehmen
    # (im selben Zyklus stabil). meta.model -> /history -> addYukiReply (Reload),
    # SSE-model -> Live-Bubble.
    reach_model = yc.OLLAMA_MODEL
    with LOCK:
        HISTORY.append({"role": "assistant", "content": message, "ts": time.time(),
                        "meta": {"steward": True, "model": reach_model}})
        yc.save_history(HISTORY)
    _broadcast_sse({"kind": "steward_reach_out", "message": message, "reason": reason,
                    "model": reach_model,
                    "persona": CURRENT_PERSONA, "mood": yc.load_mood()})
    # Milestone C: in die Poll-Queue haengen, damit das Handy es auch im Standby /
    # bei geschlossener App holt (nativer WorkManager-Poll -> lokale Notification).
    try:
        yc.add_steward_push(message, reason)
    except Exception as _e:
        print(f"  [Steward-Push-Queue-Fehler: {_e}]", flush=True)
    print(f"  [🌱 Steward reach-out ({source}): {message[:80]}]", flush=True)
    return True, "reach_out"


def _steward_thought_guarded(text, reason, source, force):
    """🟢-Effektor (leiser Kanal, 2026-06-17): einen Gedanken ins Log schreiben.
    Eigener Token-Bucket (thought_max_per_day/thought_min_gap_min) + frischer
    Notstop/enabled-Recheck. KEIN Ping, KEINE Conversation-Persistenz (die Bridge
    steward_thoughts_block_for_prompt reicht). Returns (acted, info)."""
    text = (text or "").strip()
    if not text:
        return False, "none"
    cfg = yc.load_steward_config()
    _steward_day_roll()
    now = time.time()
    cap = int(cfg.get("thought_max_per_day", 12))
    gap = float(cfg.get("thought_min_gap_min", 0)) * 60.0
    if _steward["thoughts_today"] >= cap or (now - _steward["last_thought_ts"]) < gap:
        yc.append_steward_log({"action": "thought", "status": "suppressed",
                               "reason": "daily_cap" if _steward["thoughts_today"] >= cap else "min_gap",
                               "params": {"source": source}})
        return False, "rate_limit"
    if _steward["notstop"] or (not _steward["enabled"] and not force):
        yc.append_steward_log({"action": "thought", "status": "suppressed",
                               "reason": "disabled_or_notstop", "params": {"source": source}})
        return False, "disabled"
    _steward["thoughts_today"] += 1
    _steward["last_thought_ts"] = now
    yc.add_steward_thought(text, source=source, reason=reason)
    yc.append_steward_log({"action": "thought", "status": "done", "reversible": True,
                           "params": {"source": source, "text": text, "reason": reason}})
    _broadcast_sse({"kind": "steward_thoughts",
                    "count": len(yc.load_steward_thoughts(unread_only=True))})
    print(f"  [🌱 Steward Gedanke ({source}): {text[:80]}]", flush=True)
    return True, "thought"


def _steward_note_guarded(text, reason, source, force):
    """🟢-Effektor (2026-06-17): autonome Notiz in Michaels Notizliste. Quelle wird
    als source='steward_<source>' getaggt, damit das UI sie pro Quelle (rss /
    sehnsucht) getrennt gruppieren kann. Eigener Token-Bucket + Notstop-Recheck.
    Per Config abschaltbar (autonomous_notes=false)."""
    text = (text or "").strip()
    if not text:
        return False, "none"
    cfg = yc.load_steward_config()
    if not cfg.get("autonomous_notes", True):
        yc.append_steward_log({"action": "note", "status": "suppressed",
                               "reason": "channel_off", "params": {"source": source}})
        return False, "channel_off"
    _steward_day_roll()
    now = time.time()
    cap = int(cfg.get("note_max_per_day", 12))
    gap = float(cfg.get("note_min_gap_min", 0)) * 60.0
    if _steward["notes_today"] >= cap or (now - _steward["last_note_ts"]) < gap:
        yc.append_steward_log({"action": "note", "status": "suppressed",
                               "reason": "daily_cap" if _steward["notes_today"] >= cap else "min_gap",
                               "params": {"source": source}})
        return False, "rate_limit"
    if _steward["notstop"] or (not _steward["enabled"] and not force):
        yc.append_steward_log({"action": "note", "status": "suppressed",
                               "reason": "disabled_or_notstop", "params": {"source": source}})
        return False, "disabled"
    _steward["notes_today"] += 1
    _steward["last_note_ts"] = now
    note = yc.add_note(text, active=True, source=f"steward_{source}")
    yc.append_steward_log({"action": "note", "status": "done", "reversible": True,
                           "params": {"source": source, "text": text, "reason": reason,
                                      "note_id": (note or {}).get("id")}})
    _broadcast_sse({"kind": "steward_note", "id": (note or {}).get("id")})
    print(f"  [🌱 Steward Notiz ({source}): {text[:80]}]", flush=True)
    return True, "note"


# Routing-Helfer: eine Steward-Entscheidung (action+message) auf ihren Effektor
# schicken. Gemeinsam fuer Sehnsucht (Single-Decision) und RSS (pro Pick).
def _steward_dispatch(action, text, reason, source, force):
    if action == "reach_out":
        return _steward_reach_out_guarded(text, reason, source, force)
    if action == "thought":
        return _steward_thought_guarded(text, reason, source, force)
    if action == "note":
        return _steward_note_guarded(text, reason, source, force)
    return False, "none"


def _steward_sehnsucht_cycle(cfg, force):
    """Quelle 'sehnsucht': nach langer Stille entscheiden, ob Yuki aus eigenem
    Antrieb etwas tut - Gedanke (leise) / Notiz (Merker) / Reach-out (laut).
    Default ist nichts tun."""
    base = max(_proactive["last_activity"], _steward["start_ts"])
    now = time.time()
    silence_h = max(0.0, (now - base) / 3600.0)
    # Kadenz-Schubser (2026-07-10): ist der Ziel-Abstand fuer einen echten
    # Sehnsucht-Gruss erreicht, kriegt der Gate-Prompt einen sanften Hinweis
    # (Anker-Pflicht bleibt). Bricht das faktische Nie-Melden auf.
    last_ro = _steward.get("last_sehnsucht_reach_out_ts", 0.0)
    target_days = cfg.get("sehnsucht_reach_out_target_days", 7)
    invite = yc.steward_reach_out_due(now, last_ro, target_days)
    days_since = ((now - last_ro) / 86400.0) if last_ro else None
    with LOCK:
        decision = yc.steward_decide(silence_h, reach_out_invite=invite,
                                     days_since_reach_out=days_since)
    if not _steward_model_ok(cfg):
        return False, "model_floor"
    return _steward_dispatch(decision.get("action"), decision.get("message", ""),
                             decision.get("reason", ""), "sehnsucht", force)


def _steward_rss_cycle(cfg, force):
    """Quelle 'rss': neue Feed-Items einsammeln (kein LLM), und NUR wenn welche neu
    sind gegen Michaels Vorlieben scoren. Relevantes -> Digest (passiv) + ggf.
    reach_out (rate-limitiert). Erster Lauf = Baseline (nichts melden)."""
    feeds = cfg.get("feeds") or []
    if not feeds:
        return False, "no_feeds"
    new, baseline = yc.steward_new_items(                # reines Fetch+Dedup+Alters-Filter
        feeds, max_age_days=cfg.get("rss_max_age_days", 14))
    if baseline:
        yc.append_steward_log({"action": "rss", "status": "skipped",
                               "reason": f"baseline etabliert ({len(yc.load_steward_seen())} items)"})
        return False, "baseline"
    if not new:
        return False, "no_new"                         # guenstig: kein LLM-Call
    # Interessens-Wortliste: Treffer umgehen den LLM-Gate KOMPLETT (deterministische
    # "nicht-wegfiltern"-Garantie) und brauchen kein Modell. Der Rest geht wie bisher
    # durch Yukis Relevanz-Gate (und nur wenn das Modell gross genug ist).
    keywords = yc._steward_interest_list(cfg.get("interest_keywords"))
    # Block-Wortliste: Gegenstueck zur Interessens-Liste. Ein Treffer wirft das Item
    # WIEDER raus (es landet nicht im Digest). Greift nur auf den rest-Pool (Yukis
    # LLM-Picks); ein interest_keywords-Treffer (explizite Positiv-Garantie) gewinnt.
    block_words = yc._steward_interest_list(cfg.get("block_keywords"))
    forced, rest = [], []
    blocked = 0
    for it in new:
        kw = yc._steward_interest_hit(it, keywords)
        if kw:
            forced.append((it, kw))          # explizites Interesse gewinnt -> nie blocken
            continue
        if yc._steward_interest_hit(it, block_words):
            blocked += 1                      # von Yuki waehlbar gewesen -> ausgeblendet
            continue
        rest.append((it, kw))
    forced_picks = [{"item": it, "action": "digest", "interest": kw,
                     "blurb": f"Zu deinem Interesse »{kw}« vorgemerkt."}
                    for it, kw in forced]
    picks = []
    model_ok = _steward_model_ok(cfg)
    if rest and model_ok:
        with LOCK:
            picks = yc.steward_decide_items([it for it, _kw in rest])
    if not forced_picks and not picks:
        if rest and not model_ok:
            return False, "model_floor"
        yc.append_steward_log({"action": "rss", "status": "done",
                               "reason": f"{len(new)} neu, nichts relevant"
                                         + (f", {blocked} ausgeblendet" if blocked else "")})
        return False, "none"
    picks = forced_picks + picks                        # Interesse-Treffer zuerst
    acted = False
    acted_reach = False
    digested = False
    for p in picks:
        it = p.get("item", {})
        blurb = p.get("blurb", "")
        action = p.get("action", "digest")
        title = (it.get("title") or "").strip()
        feed = (it.get("feed") or "").strip()
        feed_note = (it.get("feed_note") or "").strip()   # kuratierter Listen-Label
        # thought/note haben eigene Stores - dorthin routen, NICHT in den Digest.
        if action == "thought":
            ok, _i = _steward_thought_guarded(blurb or title, f"RSS: {title[:80]}", "rss", force)
            acted = acted or ok
            continue
        if action == "note":
            ok, _i = _steward_note_guarded(blurb or title, f"RSS: {title[:80]}", "rss", force)
            acted = acted or ok
            continue
        # digest (Normalfall) + reach_out -> beide in den Digest (reach_out
        # zusaetzlich als Ping; der Digest faengt ihn ab, falls Rate-Limit greift).
        yc.add_steward_digest({"title": title, "link": it.get("link", ""),
                               "feed": feed, "feed_note": feed_note, "blurb": blurb,
                               **({"interest": p["interest"]} if p.get("interest") else {})})
        acted = True
        digested = True
        # RSS-Picks landen NICHT mehr als Episode (2026-07-03): 20-40 Feed-Items/Tag
        # verwaesserten den Episoden-Canon ("Dinge die wir erlebt haben") und
        # verdraengten echte Memos im Top-3-Recall. Der Digest (transient, Cap 100,
        # 📰-Panel + steward_digest_block_for_prompt) deckt "was Yuki vorgemerkt hat"
        # vollstaendig ab. Bewusst kein Wochen-spaeter-Episode-Recall auf Feeds.
        if action == "reach_out":
            ok, _info = _steward_reach_out_guarded(
                blurb or title, f"RSS: {title[:80]}", "rss", force)
            acted_reach = acted_reach or ok
    yc.append_steward_log({"action": "rss", "status": "done",
                           "reason": f"{len(new)} neu, {len(picks)} vorgemerkt"
                                     + (f" ({len(forced_picks)} Interesse)" if forced_picks else "")
                                     + (f", {blocked} ausgeblendet" if blocked else "")})
    # Live-Badge: offene Tabs ueber den neuen Digest-Stand informieren (📰-Button).
    if digested:
        _broadcast_sse({"kind": "steward_digest", "count": len(yc.load_steward_digest(unread_only=True))})
    return acted, ("reach_out" if acted_reach else ("digest" if digested else "logged"))


def _steward_cycle_once(force=False, source="sehnsucht"):
    """Einen Zyklus einer Quelle fahren. force=True (manueller Trigger) umgeht nur
    die TIMING-Gates des Loops; ALLE Guardrails (Notstop/Model-Floor/Rate-Limit)
    gelten weiter. Returns (acted: bool, info: str)."""
    if _steward["notstop"]:
        return False, "notstop"
    cfg = yc.load_steward_config()
    if source == "rss":
        return _steward_rss_cycle(cfg, force)
    return _steward_sehnsucht_cycle(cfg, force)


def _routines_push_one(routine):
    """Eine proaktive Routinen-Erinnerung ausliefern (#30 Phase 3). One-shot pro Tag,
    aber NICHT mehr self-satisfying: der Push erinnert nur, er markiert die Routine
    NICHT als erledigt. Erledigt wird ausschliesslich durch Michael (Abhaken im Modal
    oder [routine_done:] wenn er es Yuki sagt). Die 1x-pro-Tag-Bremse haengt jetzt an
    mark_routine_pushed/last_push_day statt am Done-Gate.
    Liefert beides wie die Sehnsucht - leise Bubble (SSE steward_reach_out) wenn die
    App offen ist, plus Push-Queue fuer die native Standby-Notification."""
    label = (routine.get("label") or "").strip() or "deine Routine"
    message = f"Kleine Erinnerung: {label} steht für heute an. 🌿"
    yc.mark_routine_pushed(routine["id"])     # nur Tagesbremse, KEIN Erledigt-Haken
    reach_model = yc.OLLAMA_MODEL
    with LOCK:
        HISTORY.append({"role": "assistant", "content": message, "ts": time.time(),
                        "meta": {"steward": True, "routine": True, "model": reach_model}})
        yc.save_history(HISTORY)
    _broadcast_sse({"kind": "steward_reach_out", "message": message,
                    "reason": f"routine:{routine.get('id')}", "model": reach_model,
                    "persona": CURRENT_PERSONA, "mood": yc.load_mood()})
    try:
        yc.add_steward_push(message, f"routine:{routine.get('id')}")
    except Exception as _e:
        print(f"  [Routinen-Push-Queue-Fehler: {_e}]", flush=True)
    try:
        yc.append_steward_log({"action": "routine_reminder", "status": "done",
                               "reversible": False,
                               "params": {"routine_id": routine.get("id"), "label": label}})
    except Exception:
        pass
    print(f"  [⏰ Routinen-Push: {label}]", flush=True)


def _routines_proactive_tick():
    """Im Steward-Tick: alle faelligen proaktiven Routinen pushen. Der Caller hat
    Master-Flag + Ruhezeit + Lock-frei bereits geprueft. Self-satisfying pro Routine."""
    for r in yc.routines_due_for_push():
        _routines_push_one(r)


def steward_loop_web():
    """Daemon-Thread: nach langer Stille (Idle) faehrt der Steward Round-Robin durch
    seine Quellen (Sehnsucht + RSS), jede mit eigenem Intervall, damit der LLM-Call
    nicht jede Minute feuert. Anders als proactive_loop_web feuert er GERADE wenn
    niemand da ist. TIMING-Gates hier, Guardrails in den Cycle-Funktionen.
    Zusaetzlich (#30 Phase 3): zeitgebundener Routinen-Push - eigene Gates, UNABHAENGIG
    von Sehnsucht-enabled + Idle-Regel (eine Medi-Erinnerung soll auch kommen, wenn
    Michael gerade aktiv war), aber respektiert Notstop + Ruhezeit + Lock."""
    while _steward["run"]:
        time.sleep(5.0)
        if _steward["notstop"]:
            continue
        routines_on = bool(yc.ROUTINES_PROACTIVE_ENABLED)
        if not _steward["enabled"] and not routines_on:
            continue                                  # nichts zu tun fuer diesen Tick
        cfg = yc.load_steward_config()
        hour = datetime.datetime.now().hour
        in_quiet = yc.steward_in_quiet_hours(hour, cfg.get("quiet_start", 0), cfg.get("quiet_end", 8))
        lock_busy = _lock_busy()
        # --- Routinen-Push (#30 Phase 3): eigener Pfad, NICHT idle-gated ---------
        if routines_on and not in_quiet and not lock_busy:
            try:
                _routines_proactive_tick()
            except Exception as _e:
                print(f"  [Routinen-Push-Tick-Fehler: {_e}]", flush=True)
        # --- Sehnsucht / RSS (wie bisher, idle-gated) ----------------------------
        if not _steward["enabled"]:
            continue
        if in_quiet:
            continue
        if lock_busy:
            continue                                  # Michael redet/zeigt gerade
        now = time.time()
        base = max(_proactive["last_activity"], _steward["start_ts"])
        if now - base < float(cfg.get("idle_minutes", 30)) * 60:
            continue                                  # noch nicht lange genug still
        # Welche Quelle ist faellig? Eigene Intervalle halten die Last niedrig.
        due = []
        if (cfg.get("feeds") and
                now - _steward["last_run"]["rss"] >= float(cfg.get("rss_interval_min", 30)) * 60):
            due.append("rss")
        if now - _steward["last_run"]["sehnsucht"] >= float(cfg.get("sehnsucht_interval_min", 60)) * 60:
            due.append("sehnsucht")
        if not due:
            continue
        # Round-Robin: die am laengsten nicht gelaufene faellige Quelle zuerst,
        # genau EINE pro Aufwachen (LLM sieht nie mehrere Domaenen gleichzeitig).
        source = min(due, key=lambda s: _steward["last_run"][s])
        _steward["last_run"][source] = now
        _steward_cycle_once(force=False, source=source)


@app.route("/steward", methods=["GET", "POST"])
def steward_settings():
    """GET: aktueller Zustand (fuers UI-Init). POST: {enabled?}. Einschalten hebt
    einen evtl. gesetzten Notstop auf (bewusstes Re-Arm). State ist sticky
    (memory/yuki_steward.json), Tunables live-reload aus config/steward.json."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if "enabled" in payload:
            _steward["enabled"] = bool(payload["enabled"])
            if _steward["enabled"]:
                _steward["notstop"] = False           # Re-Arm
            yc.save_steward_state(enabled=_steward["enabled"], notstop=_steward["notstop"])
    cfg = yc.load_steward_config()
    return jsonify({"ok": True, "enabled": _steward["enabled"], "notstop": _steward["notstop"],
                    "idle_minutes": cfg.get("idle_minutes"),
                    "quiet_start": cfg.get("quiet_start"), "quiet_end": cfg.get("quiet_end"),
                    "reach_out_max_per_day": cfg.get("reach_out_max_per_day"),
                    "reach_outs_today": _steward["reach_outs_today"],
                    "feeds_count": len(cfg.get("feeds") or []),
                    "digest_count": len(yc.load_steward_digest(unread_only=True)),
                    "thought_count": len(yc.load_steward_thoughts(unread_only=True)),
                    "autonomous_notes": bool(cfg.get("autonomous_notes", True))})


@app.route("/steward/notstop", methods=["POST"])
def steward_notstop():
    """Out-of-band harter Kill: setzt notstop + schaltet enabled aus, persistiert.
    Re-Arm NUR explizit ueber POST /steward {enabled:true}."""
    _steward["notstop"] = True
    _steward["enabled"] = False
    yc.save_steward_state(enabled=False, notstop=True)
    yc.append_steward_log({"action": "notstop", "status": "done", "reason": "manual kill"})
    print("  [⛔ Steward NOTSTOP gesetzt - Loop gestoppt bis Re-Arm]", flush=True)
    return jsonify({"ok": True, "enabled": False, "notstop": True})


@app.route("/steward/trigger", methods=["POST"])
def steward_trigger():
    """Manueller Test-Trigger: umgeht Idle/Quiet/Poll, erzwingt aber alle Guardrails
    (Notstop/Model-Floor/Rate-Limit). Body {source: 'sehnsucht'|'rss'} (Default
    sehnsucht). 409 bei Notstop oder wenn LOCK gerade belegt."""
    if _steward["notstop"]:
        return jsonify({"ok": False, "error": "notstop"}), 409
    if _lock_busy():
        return jsonify({"ok": False, "error": "busy"}), 409
    payload = request.get_json(silent=True) or {}
    source = payload.get("source", "sehnsucht")
    if source not in ("sehnsucht", "rss"):
        source = "sehnsucht"
    acted, info = _steward_cycle_once(force=True, source=source)
    return jsonify({"ok": True, "acted": acted, "info": info, "source": source})


@app.route("/steward/log")
def steward_log():
    """Action-Journal fuer den UI-Inspector (neueste zuletzt)."""
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"log": yc.load_steward_log(limit=limit)})


def _steward_config_path():
    return HERE / "config" / "steward.json"


def _steward_read_config_raw():
    """Rohe config/steward.json lesen (plain JSON, keine // comments - wir nutzen
    _doc-String-Keys). Fuer Schreibzugriff (Feeds), unter Erhalt aller Keys."""
    p = _steward_config_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


@app.route("/steward/feeds", methods=["GET", "POST", "DELETE"])
def steward_feeds():
    """Feed-Verwaltung (Michael kuratiert). GET: Liste [{url, note}]. POST
    {url, note?}: hinzufuegen ODER Notiz aktualisieren (Upsert, http/https).
    DELETE {url}: entfernen. Schreibt config/steward.json (live-reload, der Loop
    liest pro Tick frisch). Legacy-String-Feeds werden beim ersten Schreiben zu
    {url, note}-Dicts normalisiert."""
    data = _steward_read_config_raw()
    feeds = yc._steward_feed_entries(data.get("feeds"))     # -> [{url, note}]
    if request.method in ("POST", "DELETE"):
        payload = request.get_json(silent=True) or {}
        url = (payload.get("url") or "").strip()
        if request.method == "POST":
            if not re.match(r"^https?://", url):
                return jsonify({"ok": False, "error": "url muss mit http(s):// beginnen"}), 400
            note = (payload.get("note") or "").strip()
            existing = next((f for f in feeds if f["url"] == url), None)
            if existing:
                existing["note"] = note                # Upsert: nur Notiz aktualisieren
            else:
                feeds.append({"url": url, "note": note})
        else:                                          # DELETE
            feeds = [f for f in feeds if f["url"] != url]
        data["feeds"] = feeds
        try:
            yc._atomic_write_text(_steward_config_path(),
                                  json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "feeds": feeds})


@app.route("/steward/digest", methods=["GET", "POST", "DELETE"])
def steward_digest():
    """Passiver Digest: was Yuki in der Abwesenheit vorgemerkt hat. GET: Liste
    (neueste zuletzt; Alt-Eintraege bekommen lazy eine id). POST {id}: STERN -
    in die Gemerkt-Liste verschieben (raus aus dem Eingang). DELETE {id}: EINEN
    Eintrag wegraeumen (Rest bleibt). DELETE ohne id: alles leeren. Die Items
    liegen ohnehin im Episoden-Langzeitgedaechtnis - Wegraeumen verliert nichts."""
    if request.method == "POST":
        # Stern: saved=True (in die Gemerkt-Liste) + read=True (raus aus dem Eingang).
        payload = request.get_json(silent=True) or {}
        item_id = (payload.get("id") or "").strip()
        digest = yc.save_steward_digest_item(item_id)
        _broadcast_sse({"kind": "steward_digest", "count": len(digest)})
        return jsonify({"ok": True, "digest": digest})
    if request.method == "DELETE":
        # Soft-Delete: "gelesen" setzt read=True (rueckholbar), loescht nicht.
        payload = request.get_json(silent=True) or {}
        item_id = (payload.get("id") or "").strip()
        if item_id:
            digest = yc.mark_steward_digest_read(item_id)   # -> verbleibende ungelesene
        else:
            digest = yc.mark_all_steward_digest_read()      # -> []
        _broadcast_sse({"kind": "steward_digest", "count": len(digest)})
        return jsonify({"ok": True, "digest": digest})
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    yc.ensure_steward_digest_ids()                     # Backfill fuer Alt-Eintraege
    # Nur UNGELESENE im Posteingang anzeigen (gelesene bleiben als Archiv in der Datei).
    return jsonify({"ok": True, "digest": yc.load_steward_digest(limit=limit, unread_only=True)})


@app.route("/steward/saved", methods=["GET", "DELETE"])
def steward_saved():
    """Gemerkt-Liste (per Stern aus dem Digest gezogen). GET: Liste (neueste
    zuletzt). DELETE {id}: ENDGUELTIG entfernen (Hard-Delete, anders als der
    Posteingang). DELETE ohne id: alle Gemerkten entfernen. Gemerkte ueberleben
    den 100er-Cap des Digests (siehe add_steward_digest)."""
    if request.method == "DELETE":
        payload = request.get_json(silent=True) or {}
        item_id = (payload.get("id") or "").strip()
        if item_id:
            yc.remove_steward_digest(item_id)
        else:
            for it in yc.load_steward_saved():
                yc.remove_steward_digest(it.get("id"))
        return jsonify({"ok": True, "saved": yc.load_steward_saved()})
    try:
        limit = int(request.args.get("limit", 80))
    except (TypeError, ValueError):
        limit = 80
    yc.ensure_steward_digest_ids()
    return jsonify({"ok": True, "saved": yc.load_steward_saved(limit=limit)})


@app.route("/steward/interests", methods=["GET", "POST", "DELETE"])
def steward_interests():
    """Interessens-Wortliste (Michael pflegt sie selbst). Substring-Treffer in
    Feed-Titel/Summary umgehen den LLM-Gate komplett -> garantiert in den Digest,
    dort mit 🎯 markiert. GET: Liste. POST {word}: hinzufuegen (Dedup case-insensitiv).
    DELETE {word}: entfernen. Schreibt config/steward.json (live-reload, _doc-Keys
    bleiben durch den Round-Trip erhalten - wie der Feeds-Editor)."""
    data = _steward_read_config_raw()
    words = yc._steward_interest_list(data.get("interest_keywords"))
    if request.method in ("POST", "DELETE"):
        payload = request.get_json(silent=True) or {}
        word = (payload.get("word") or "").strip()
        if request.method == "POST":
            if not word:
                return jsonify({"ok": False, "error": "leeres Wort"}), 400
            if word.lower() not in [w.lower() for w in words]:
                words.append(word)
        else:                                          # DELETE
            words = [w for w in words if w.lower() != word.lower()]
        data["interest_keywords"] = words
        try:
            yc._atomic_write_text(_steward_config_path(),
                                  json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "interests": words})


@app.route("/steward/blocklist", methods=["GET", "POST", "DELETE"])
def steward_blocklist():
    """Block-Wortliste (Gegenstueck zu /steward/interests). Substring-Treffer in
    Feed-Titel/Summary werfen ein von Yuki ausgewaehltes Item WIEDER aus dem Digest
    (es landet nicht). Ein interest_keywords-Treffer gewinnt bei Konflikt. GET: Liste.
    POST {word}: hinzufuegen (Dedup case-insensitiv). DELETE {word}: entfernen.
    Schreibt config/steward.json (live-reload, _doc-Keys bleiben erhalten)."""
    data = _steward_read_config_raw()
    words = yc._steward_interest_list(data.get("block_keywords"))
    if request.method in ("POST", "DELETE"):
        payload = request.get_json(silent=True) or {}
        word = (payload.get("word") or "").strip()
        if request.method == "POST":
            if not word:
                return jsonify({"ok": False, "error": "leeres Wort"}), 400
            if word.lower() not in [w.lower() for w in words]:
                words.append(word)
        else:                                          # DELETE
            words = [w for w in words if w.lower() != word.lower()]
        data["block_keywords"] = words
        try:
            yc._atomic_write_text(_steward_config_path(),
                                  json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "blocklist": words})


@app.route("/steward/thoughts", methods=["GET", "DELETE"])
def steward_thoughts():
    """Gedankenlog (leiser Kanal): Yukis Gedanken aus eigenem Antrieb. GET: Liste
    der UNGELESENEN (neueste zuletzt). DELETE {id}: einen als gelesen markieren
    (Soft-Delete, rueckholbar). DELETE ohne id: alle als gelesen markieren."""
    if request.method == "DELETE":
        payload = request.get_json(silent=True) or {}
        item_id = (payload.get("id") or "").strip()
        if item_id:
            rest = yc.mark_steward_thought_read(item_id)     # -> verbleibende ungelesene
        else:
            rest = yc.mark_all_steward_thoughts_read()       # -> []
        _broadcast_sse({"kind": "steward_thoughts", "count": len(rest)})
        return jsonify({"ok": True, "thoughts": rest})
    try:
        limit = int(request.args.get("limit", 80))
    except (TypeError, ValueError):
        limit = 80
    return jsonify({"ok": True, "thoughts": yc.load_steward_thoughts(limit=limit, unread_only=True)})


@app.route("/events")
def events():
    """SSE-Stream: der Browser haelt eine Dauerverbindung; der Server pusht JSON-
    Events (Auto-Vision-Kommentare). Jeder Tab kriegt seine eigene Queue. Bei
    EventSource gibt's Auto-Reconnect (schickt Last-Event-ID als Header); ein
    manueller Reconnect haengt ?lastEventId= an. Beides honorieren -> verpasste
    Events der toten Phase werden aus _sse_log nachgespielt."""
    raw_last = request.headers.get("Last-Event-ID") or request.args.get("lastEventId")
    try:
        last_id = int(raw_last) if raw_last is not None else None
    except (TypeError, ValueError):
        last_id = None

    q = queue.Queue(maxsize=32)
    with _sse_lock:
        _sse_clients.add(q)
        # Replay-Snapshot unter demselben Lock schnappen, unter dem _broadcast_sse
        # schreibt: dann ist KEIN Event sowohl im Snapshot als auch in der Queue
        # (keine Dubletten) und keins faellt zwischen Add und Snapshot durch (keine
        # Luecke) - beide Sektionen sind durch _sse_lock atomar serialisiert.
        replay = ([frame for (eid, frame) in _sse_log if eid > last_id]
                  if last_id is not None else [])

    def gen():
        try:
            yield ": connected\n\n"               # Komment-Frame fuer Proxies/Browser
            for frame in replay:                  # verpasste Events zuerst nachspielen
                yield frame
            while True:
                try:
                    frame = q.get(timeout=15)
                    yield frame                   # Frame enthaelt schon id:+data:
                except queue.Empty:
                    yield ": keep-alive\n\n"      # haelt die Verbindung warm
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                _sse_clients.discard(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store",
                             "X-Accel-Buffering": "no"})


def _next_capture_eta():
    """Best-effort-Zeitpunkt (epoch s), wann rhythmisch das naechste autonome Bild
    faellig ist = last_check + Intervall der als NAECHSTES blickenden Cam (PTZ:
    rotate_interval, sonst globales Intervall). None wenn Beobachten aus / Vision
    nicht da. 'Theoretisch': Soft-Gates (Cooldown, Ruhe-bei-aktivem-Chat, kein Tab
    offen) koennen es nach hinten schieben - fuers UI-Tooltip reicht der Rhythmus."""
    if not (_auto_web["enabled"] and yc.VISION_ENABLED):
        return None
    try:
        vps = ycam.watch_viewpoints()
        if vps:
            _src, pos, _area = vps[_auto_web["rotate_cursor"] % len(vps)]
            interval = ycam.rotate_interval(_src) if pos is not None else _auto_web["interval"]
        else:
            interval = _auto_web["interval"]
    except Exception:
        interval = _auto_web["interval"]
    eta = _auto_web["last_check"] + interval
    # Nach einer Reaktion haelt der Cooldown den naechsten Schuss zusaetzlich zurueck
    # (der Loop ist solange ganz gated) -> spaeteren der beiden Termine nehmen, sonst
    # springt der Countdown direkt nach einem Kommentar zu frueh auf "gleich".
    eta = max(eta, _auto_web["last_comment"] + _auto_web["cooldown"])
    return round(eta, 1)


@app.route("/auto_vision", methods=["GET", "POST"])
def auto_vision():
    """GET: aktueller Zustand (fuers UI-Init). POST: {enabled?, interval?, cooldown?,
    quiet_after_activity?} setzt einzelne Felder live. Werte sind Sitzungs-Setting
    (nicht persistiert - Default beim Server-Start ist AUTO_VISION_WEB_DEFAULT_* bzw.
    AUTO_VISION_QUIET_AFTER_ACTIVITY_SEC aus settings.jsonc)."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if "enabled" in payload:
            was_on = _auto_web["enabled"]
            _auto_web["enabled"] = bool(payload["enabled"])
            if _auto_web["enabled"] and not was_on:
                # Beim Wiedereinschalten Baseline neu aufbauen, sonst gilt die alte
                # last_desc evtl. von vor Stunden -> sofort vermeintlicher Wechsel.
                _auto_web["last_desc"] = ""
                _auto_web["last_desc_by_pos"] = {}        # PTZ-Baselines auch neu
                _auto_web["last_check"] = 0.0
                # Baseline-Priming: alle Blickpunkte EINMAL vorab anfahren (Hintergrund),
                # damit nicht erst nach einer vollen Runde verglichen werden kann. Loop
                # pausiert solange (priming-Flag). priming_gen invalidiert einen alten
                # Prime-Thread bei schnellem Aus/Ein.
                _auto_web["priming_gen"] += 1
                _auto_web["priming"] = True
                threading.Thread(target=_prime_baselines, args=(_auto_web["priming_gen"],),
                                 daemon=True, name="vision-prime").start()
            elif was_on and not _auto_web["enabled"]:
                # Beobachten beendet -> schwenkbare Cam zurueck auf Home-Park-Position
                # (alte Ueberwachungs-Default-Ansicht). Im Daemon-Thread, damit der
                # Toggle-Response nicht auf die (Netzwerk-)Anfahrt wartet.
                if ycam.has_ptz() and ycam.park_home_on_stop():
                    threading.Thread(target=ycam.go_home, daemon=True).start()
        if "interval" in payload:
            try:
                _auto_web["interval"] = max(5, int(payload["interval"]))
            except (TypeError, ValueError):
                pass
        if "cooldown" in payload:
            try:
                _auto_web["cooldown"] = max(0, int(payload["cooldown"]))
            except (TypeError, ValueError):
                pass
        if "quiet_after_activity" in payload:
            try:
                # 0 = aus; oberes Cap 600 wie in settings.jsonc dokumentiert.
                _auto_web["quiet_after_activity"] = max(0, min(600, int(payload["quiet_after_activity"])))
            except (TypeError, ValueError):
                pass
    return jsonify({"ok": True,
                    "enabled": _auto_web["enabled"],
                    "interval": _auto_web["interval"],
                    "cooldown": _auto_web["cooldown"],
                    "quiet_after_activity": _auto_web["quiet_after_activity"],
                    "next_check_at": _next_capture_eta(),   # epoch s oder null (UI-Tooltip)
                    "priming": _auto_web["priming"],        # Baseline-Durchlauf laeuft gerade
                    "vision_available": yc.VISION_ENABLED})


# ===========================================================================
# Kamera-Steuer-Panel (Optionen->System, 2026-07-21) - manuelle PTZ-Steuerung
# ===========================================================================
# Damit die internet-gesperrte Reolink LOKAL ausgerichtet + ihre Presets
# verwaltet werden koennen (die Reolink-App braucht Cloud = unbenutzbar). Nur
# reolink kann joggen/schreiben; upcam bleibt read-only anfahrbar (eigene
# Weboberflaeche). cameras.json ist kanonisch, der Treiber schreibt beide Seiten.

def _camera_err(e):
    """Treiber-Exception -> (json, status). NotImplementedError=501 (Cam kann's
    nicht), sonst 503 (Cam offline / Hardware-Fehler)."""
    if isinstance(e, NotImplementedError):
        return jsonify({"ok": False, "error": str(e)}), 501
    if isinstance(e, ValueError):
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": False, "error": f"Kamera-Fehler: {e}"}), 503


@app.route("/camera/sources", methods=["GET"])
def camera_sources():
    """Alle Cams + ihre Presets + Flags fuers Panel. can_edit=True nur fuer
    reolink. hw_drift = Hardware-Slots ohne Label in cameras.json (best-effort,
    ein Reolink-Login pro Aufruf; Cam offline -> leer)."""
    items = []
    for name in ycam.source_names():
        try:
            ptz = ycam.ptz_config(name)
            can_edit = (ptz.get("kind") == "reolink")
            presets = ycam.presets(name)
            entry = {
                "name": name,
                "label": ycam.source_label(name),
                "has_ptz": ycam.has_ptz(name),
                "can_edit": can_edit,
                "presets": {str(k): v for k, v in presets.items()},
            }
            if can_edit:
                try:
                    labeled = set(presets.keys())
                    entry["hw_drift"] = [p for p in ycam.hw_presets(name)
                                         if p["id"] not in labeled]
                except Exception as e:
                    entry["hw_drift"] = []
                    entry["drift_error"] = str(e)
        except Exception as e:
            entry = {"name": name, "error": str(e)}
        items.append(entry)
    return jsonify({"ok": True, "sources": items})


@app.route("/camera/snapshot", methods=["GET"])
def camera_snapshot():
    """Ein frisches Standbild der Cam als JPEG (fuers Poll-Preview). no-store,
    damit der Browser jedes Frame neu holt. 503 wenn die Cam nicht liefert."""
    cam = request.args.get("cam") or None
    try:
        frame = ycam.grab(cam)
    except Exception as e:
        return _camera_err(e)
    if not frame:
        return jsonify({"ok": False, "error": "Kamera nicht erreichbar"}), 503
    return app.response_class(frame, mimetype="image/jpeg",
                              headers={"Cache-Control": "no-store"})


@app.route("/camera/goto", methods=["POST"])
def camera_goto():
    body = request.get_json(silent=True) or {}
    cam = body.get("cam") or None
    if "pos" not in body:
        return jsonify({"ok": False, "error": "pos fehlt"}), 400
    try:
        ycam.goto_preset(cam, int(body["pos"]))
    except Exception as e:
        return _camera_err(e)
    return jsonify({"ok": True})


@app.route("/camera/jog", methods=["POST"])
def camera_jog():
    body = request.get_json(silent=True) or {}
    cam = body.get("cam") or None
    direction = body.get("dir")
    if not direction:
        return jsonify({"ok": False, "error": "dir fehlt"}), 400
    ms = body.get("ms")
    speed = body.get("speed")
    try:
        ycam.jog(cam, direction, ms=(int(ms) if ms is not None else None),
                 speed=(int(speed) if speed is not None else None))
    except Exception as e:
        return _camera_err(e)
    return jsonify({"ok": True})


@app.route("/camera/preset/save", methods=["POST"])
def camera_preset_save():
    body = request.get_json(silent=True) or {}
    cam = body.get("cam") or None
    pos = body.get("pos")
    name = (body.get("name") or "").strip()
    try:
        new_id = ycam.save_preset(cam, ui_num=(int(pos) if pos is not None else None),
                                  name=name)
    except Exception as e:
        return _camera_err(e)
    return jsonify({"ok": True, "pos": new_id})


@app.route("/camera/preset/delete", methods=["POST"])
def camera_preset_delete():
    body = request.get_json(silent=True) or {}
    cam = body.get("cam") or None
    if "pos" not in body:
        return jsonify({"ok": False, "error": "pos fehlt"}), 400
    try:
        ycam.delete_preset(cam, int(body["pos"]))
    except Exception as e:
        return _camera_err(e)
    return jsonify({"ok": True})


@app.route("/camera/edit", methods=["POST"])
def camera_edit():
    """Panel meldet: Cam wird gerade manuell gesteuert (active=true) bzw. fertig
    (active=false). Solange aktiv, ueberspringt der Beobachtungs-Reigen diese Cam,
    damit er nicht mitten ins Ausrichten schwenkt. TTL faengt einen toten Tab ab."""
    body = request.get_json(silent=True) or {}
    cam = body.get("cam")
    if not cam:
        return jsonify({"ok": False, "error": "cam fehlt"}), 400
    if body.get("active"):
        _camera_edit_begin(cam)
    else:
        _camera_edit_end(cam)
    return jsonify({"ok": True})


# ===========================================================================
# Adventure-Engine (Phase 1, 2026-06-07)
# ===========================================================================
# Endpoints fuer die rundenbasierte Spiel-Engine. WICHTIG: KEIN save_history(HISTORY)
# in diesem Block - Adventure-Turns landen ausschliesslich in memory/adventures/<id>.json,
# damit alle Verdichtungs-Gates (facts/episodes/habits/people/decay/heart-suggest)
# sie automatisch nicht sehen. Saubere Real/Fiction-Wand ohne Skip-Flags.
# Frontend kommt Phase 2 - Phase 1 ist via curl spielbar.

def _adventure_finalize_episode(state, manifest):
    """Meta-Episode in yuki_episodes.json haengen, EINMAL pro Adventure.
    Idempotent via state.episode_added-Flag - wird sowohl beim Engine-
    Selbstabschluss (Win/Loss in resolve_user_move) als auch beim manuellen
    /adventure/end-Klick gerufen, und feuert nur das erste Mal.

    Vor 2026-06-07-fix lief der Append nur im /adventure/end-Endpoint - bei
    Engine-Selbstabschluss (Win-Treffer im zahlen_raten) wurde keiner gemacht,
    sodass siegreich beendete Spiele keine Spur in den Episodes-Stack legten.
    Jetzt: in /adventure/move wird der Helper bei status-Flip nach 'closed'
    gerufen, in /adventure/end ebenfalls (idempotent harmlos).

    Liefert (meta_episode_dict, episodes_added_count) - episodes_added=0 wenn
    schon einmal abgehakt. State wird in-place mutiert; Caller muss save_adventure
    rufen damit das Flag persistiert wird."""
    if state.get("episode_added"):
        return None, 0
    # chat_fn=yc.chat_ollama -> Adventure-Engine baut einen LLM-Summary statt
    # nur den deterministischen 'gespielt, ~Xmin'-Memo. Anti-Spoiler-Prompt
    # im Builder, Fallback auf den deterministischen Text wenn LLM kaputt.
    meta = adventure_engine.build_meta_episode(state, manifest, chat_fn=yc.chat_ollama)
    try:
        added = yc.append_episodes([meta])
    except Exception as e:
        print(f"  [Adventure-Meta-Episode-Anhang fehlgeschlagen: {e}]")
        added = 0
    state["episode_added"] = True
    return meta, added


def _adventure_tts_payload(yuki_text):
    """TTS-Payload fuer Yuki-Adventure-Bubble. Engine-Bubbles bleiben bewusst
    lautlos (System-Stimme, kein TTS). Pattern wie /respond:
      - TTS_STREAM_MOBILE=True   -> Frontend zieht das WAV via /tts_stream selber
      - TTS_STREAM_MOBILE=False  -> WAV inline als base64 mit der Response
    Crash-Resistent: TTS-Fehler droppen das Audio still, Spiel laeuft weiter.

    persona=None: synthesize waehlt die Sprache textbasiert (pick_tts_language:
    Umlaute -> Deutsch, Kana/Kanji -> Japanisch, sonst Englisch). Adventure ist
    DE/EN-Companion-Mode - kein tutor/kyoto-Sonderfall. Frontend's playReply(j)
    handelt den Rest exakt wie bei /respond,
    sobald reply/tts_text/audio_b64/has_audio/stream-Felder in der Form
    drinstehen."""
    if not yuki_text:
        return {"reply": "", "tts_text": "", "audio_b64": "",
                "has_audio": False, "stream": False}
    audio_b64 = ""
    if not TTS_STREAM_MOBILE:
        try:
            wav = yc.synthesize(yc.clean_for_tts(yuki_text))
            if wav:
                yc.LAST_REPLY_WAV.write_bytes(wav)
                audio_b64 = base64.b64encode(wav).decode("ascii")
        except Exception as e:
            print(f"  [Adventure-TTS-Fehler: {e}]")
    return {"reply": yuki_text, "tts_text": yuki_text,
            "audio_b64": audio_b64, "has_audio": bool(audio_b64),
            "stream": TTS_STREAM_MOBILE}


def _adventure_dm_tts_payload(dm_text):
    """Phase 7 (2026-06-07): TTS-Payload fuer DM-Bubble. Gleiche Pipeline wie
    _adventure_tts_payload (Sprache textbasiert via pick_tts_language), nur mit
    dm_-prefixed Feldern damit Frontend die Reihenfolge DM->Yuki(->DM-Wrap)
    sequentiell durchspielen kann. LAST_REPLY_WAV bewusst NICHT ueberschreiben -
    das ist Yukis Slot (Replay-Knopf spielt Yuki, nicht DM)."""
    if not dm_text:
        return {"dm_tts_text": "", "dm_audio_b64": "",
                "dm_has_audio": False, "dm_stream": False}
    audio_b64 = ""
    if not TTS_STREAM_MOBILE:
        try:
            wav = yc.synthesize(yc.clean_for_tts(dm_text))
            if wav:
                audio_b64 = base64.b64encode(wav).decode("ascii")
        except Exception as e:
            print(f"  [Adventure-DM-TTS-Fehler: {e}]")
    return {"dm_tts_text": dm_text, "dm_audio_b64": audio_b64,
            "dm_has_audio": bool(audio_b64),
            "dm_stream": TTS_STREAM_MOBILE}


def _adventure_dm_wrap_tts_payload(wrap_text):
    """Phase 7 Combat-Klammer: separater TTS-Payload fuer den zweiten DM-Call
    (combat_cleared-Wrap-Up). dm_wrap_*-prefixed damit Frontend ihn NACH Yukis
    Reply spielen kann (Reihenfolge: DM-Move -> Yuki -> Engine-Resolves -> DM-Wrap).
    """
    if not wrap_text:
        return {"dm_wrap_tts_text": "", "dm_wrap_audio_b64": "",
                "dm_wrap_has_audio": False, "dm_wrap_stream": False}
    audio_b64 = ""
    if not TTS_STREAM_MOBILE:
        try:
            wav = yc.synthesize(yc.clean_for_tts(wrap_text))
            if wav:
                audio_b64 = base64.b64encode(wav).decode("ascii")
        except Exception as e:
            print(f"  [Adventure-DM-Wrap-TTS-Fehler: {e}]")
    return {"dm_wrap_tts_text": wrap_text, "dm_wrap_audio_b64": audio_b64,
            "dm_wrap_has_audio": bool(audio_b64),
            "dm_wrap_stream": TTS_STREAM_MOBILE}


def _last_yuki_text(state):
    """Letzten Yuki-Turn-Text aus state.turns ziehen (oder leer wenn der letzte
    Turn von Engine/User stammt). Wird vom TTS-Payload-Bau genutzt - es wird
    immer NUR der frischeste Yuki-Reply gesprochen."""
    for t in reversed(state.get("turns") or []):
        if t.get("role") == "yuki":
            return t.get("content") or ""
        if t.get("role") in ("engine", "user"):
            return ""
    return ""


def _adventure_response(state, *, manifest=None, extra=None):
    """Einheitliche JSON-Form fuer /adventure/start + /move + /state. extra fuer
    Endpoint-spezifische Felder (rolls/mutations/choices/engine_meta)."""
    out = {"ok": True,
           "id": state["id"],
           "manifest": state["manifest"],
           "yuki_role": state.get("yuki_role"),
           "status": state.get("status", "active"),
           "state": state.get("state", {}),
           "turns": state.get("turns", []),
           "peaceful_mode": bool(state.get("peaceful_mode", False)),
           "last_touched_at": state.get("last_touched_at")}
    if manifest:
        out["manifest_display"] = manifest.get("display_name", manifest.get("name"))
        # Phase 4: PvP-Manifest-Public-Daten mitsenden, damit Frontend HUD-Namen
        # + Move-Buttons rendern kann (auch auf Resume-Pfad, wo das Frontend
        # ueber list_manifests u.U. nicht refresh'd hat).
        chars = manifest.get("characters")
        if isinstance(chars, dict) and chars:
            out["characters"] = chars
    if extra:
        out.update(extra)
    return out


@app.route("/adventure/manifests", methods=["GET"])
def adventure_manifests():
    """Verfuegbare Spiel-Manifests auflisten (fuer Setup-Wizard, Phase 2)."""
    return jsonify({"ok": True, "manifests": adventure_engine.list_manifests()})


@app.route("/adventure/list", methods=["GET"])
def adventure_list():
    """Aktive/abgeschlossene Abenteuer auflisten (Phase 2 Resume-UI)."""
    status = request.args.get("status") or None
    return jsonify({"ok": True, "items": adventure_engine.list_adventures(status)})


@app.route("/adventure/start", methods=["POST"])
def adventure_start():
    """Neues Abenteuer starten. Body: {manifest, yuki_role?, tone?, setup?}.
    Erzeugt State-File, schreibt Initial-Engine-Bubble + ersten Yuki-Turn."""
    payload = request.get_json(silent=True) or {}
    manifest_name = (payload.get("manifest") or "").strip()
    yuki_role = (payload.get("yuki_role") or "").strip() or None
    tone = (payload.get("tone") or "").strip()
    setup = payload.get("setup") or {}
    # Peaceful-Mode (Adventure-Overlay-Toggle): User schaltet Kaempfe vor Start
    # aus. Wird in setup.peaceful_mode durchgereicht, initial_state liest es und
    # persistiert auf top-level state.peaceful_mode (siehe adventure_engine.py).
    if "peaceful" in payload:
        setup["peaceful_mode"] = bool(payload.get("peaceful"))
    if not manifest_name:
        return jsonify({"ok": False, "error": "manifest fehlt"}), 400
    try:
        manifest = adventure_engine.load_manifest(manifest_name)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": f"manifest '{manifest_name}' nicht gefunden"}), 404
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"ok": False, "error": f"manifest-fehler: {e}"}), 400
    if yuki_role and yuki_role not in (manifest.get("yuki_role_allowed") or [manifest.get("yuki_role_default", "narrator")]):
        return jsonify({"ok": False, "error": f"yuki_role '{yuki_role}' nicht erlaubt fuer manifest '{manifest_name}'"}), 400
    # Phase 4: Sparring-Manifest mit characters{} - falls michael_character im
    # setup steht, muss er im Pool sein. Defensive validation, sonst kriegt
    # Michael stillschweigend Yukis Char.
    chars_pool = manifest.get("characters") or {}
    mc = setup.get("michael_character")
    if chars_pool and mc and mc not in chars_pool:
        return jsonify({"ok": False,
                        "error": f"michael_character '{mc}' nicht in pool {list(chars_pool.keys())}"}), 400

    state = adventure_engine.initial_state(manifest,
                                            yuki_role=yuki_role or manifest.get("yuki_role_default", "narrator"),
                                            tone=tone, setup=setup)
    state["id"] = adventure_engine.new_adventure_id(manifest_name)

    # Phase 7: Dual-LLM-Toggle - bei dm_llm_enabled fuehrt der DM die Welt
    # und uebernimmt auch den Story-Auftakt selbst. Template-Bubble entfaellt.
    dm_on = bool(manifest.get("dm_llm_enabled"))

    dm_reply_clean = ""
    dm_rolls: list = []
    dm_mutations: list = []
    dm_choices: list = []

    if dm_on:
        # DM erzaehlt den Story-Auftakt - kein initial_engine_msg als Template-
        # Bubble. user_action=None weil Michael noch nichts getan hat. DM weiss
        # vom System-Prompt "im ersten Reply kein Spoiler, kein Encounter, nur
        # 1-2 Saetze Welt-Stimmung + offene Frage + 2-4 [choice:...]-Cards".
        try:
            dm_raw = yc.generate_dm_reply(state, manifest)
        except Exception as e:
            return jsonify({"ok": False, "error": f"DM-LLM-Fehler: {e}"}), 502
        if dm_raw:
            d, dm_rolls = adventure_engine.expand_roll_markers(dm_raw)
            d, dm_mutations = adventure_engine.expand_state_markers(d, state)
            # Encounter-Marker im Opening STRIPPEN, nicht spawnen (analog Yuki-
            # Opening-Schutz). DM darf im 1. Reply keinen Kampf ausloesen, sonst
            # hat Michael noch keine Chance reagiert.
            d = adventure_engine.strip_encounter_markers(d)
            d, dm_choices = adventure_engine.extract_choices(d)
            d = yc.strip_all_markers(d)
            dm_reply_clean = d
            adventure_engine.apply_dm_turn(state, d,
                                            rolls=dm_rolls or None,
                                            mutations=dm_mutations or None,
                                            choices=dm_choices or None,
                                            meta={"event": "setup"})
    else:
        # Single-LLM-Pfad: System-Stimme schildert das Setup mittig.
        initial_engine = adventure_engine.format_initial_engine_msg(manifest, state)
        if initial_engine:
            adventure_engine.apply_engine_turn(state, initial_engine,
                                                meta={"event": "setup"})

    # Erster Yuki-Turn: Slim-Pfad analog /respond, aber gegen state.turns.
    # Im Dual-LLM-Pfad sieht Yuki den DM-Auftakt schon im Kontext und reagiert
    # ohne State-Marker. Im Single-LLM-Pfad ist sie selbst der Erzaehler.
    try:
        reply_raw = yc.generate_adventure_reply(state, manifest)
    except Exception as e:
        return jsonify({"ok": False, "error": f"LLM-Fehler: {e}"}), 502
    reply, rolls = adventure_engine.expand_roll_markers(reply_raw)
    mutations: list = []
    choices: list = []
    if dm_on:
        # Slim-Yuki: kein expand_state/encounter/choice - das ist DM-Slot.
        # Welt-Marker (state/encounter/choice/move) werden via strip_world_markers
        # stillschweigend entfernt (Yuki sollte sie laut System-Prompt nicht
        # schreiben, aber wir gehen auf Nummer sicher gegen Few-Shot-Leak aus
        # der _adventure-Persona). strip_all_markers macht den Rest (mood/note/etc.).
        reply, _opening_move_discarded = adventure_engine.extract_move_marker(reply)
        reply = adventure_engine.strip_world_markers(reply)
        reply = yc.strip_all_markers(reply)
    else:
        reply, mutations = adventure_engine.expand_state_markers(reply, state)
        # Phase 6: Encounter-Marker im Eroeffnungs-Turn STRIPPEN, nicht spawnen.
        # Analog Stolperfalle 8 (Move-Marker im Opening) - Yuki darf nicht aus dem
        # Nichts einen Kampf ausloesen bevor Michael ueberhaupt einen Turn hatte.
        reply = adventure_engine.strip_encounter_markers(reply)
        reply, choices = adventure_engine.extract_choices(reply)
        # Phase 4: Yuki kennt aus den Sparring-Few-Shots das Pattern "[ENGINE] -> [move:...]"
        # und schreibt im Eroeffnungs-Turn manchmal trotzdem einen Angriffs-Marker.
        # Hier nur RAUS-STRIPPEN (kein resolve) - Michael hat noch nicht zugeschlagen,
        # Yuki darf nicht aus dem Nichts angreifen. Im /adventure/move wird der Marker
        # dann regulaer resolved.
        reply, _opening_move_discarded = adventure_engine.extract_move_marker(reply)
        # Sicherheitsnetz: alle nicht-Adventure-Marker raus (Yuki "vergisst" mal kurz
        # die Regel und schreibt [mood:...] oder [note:...] - landet dann nicht in
        # die echte Welt). KEIN strip_all_markers, das frisst die [roll:...]-Spuren
        # die im History-Display als '(skill 14)' bleiben sollen.
        reply = yc.strip_all_markers(reply)
    adventure_engine.apply_yuki_turn(state, reply,
                                      rolls=rolls or None,
                                      mutations=mutations or None,
                                      choices=choices or None)
    adventure_engine.save_adventure(state)
    tts = _adventure_tts_payload(_last_yuki_text(state))
    # Phase 7: DM-Setup-Bubble vertonen (gleiche Pipeline wie Yuki). Frontend
    # spielt DM-Audio VOR Yuki-Audio sequentiell ab. Bei Single-LLM-Pfad bleibt
    # dm_reply_clean leer -> dm_tts_text leer -> Frontend skipt diesen Schritt.
    dm_tts = _adventure_dm_tts_payload(dm_reply_clean)
    # Frontend bekommt DM- UND Yuki-Spawn-Mutations zusammen damit Inventar/
    # Choices schon beim Setup gerendert werden. dm_choices wird separat ausgegeben
    # damit der Frontend sie unter der DM-Bubble klemmt (nicht unter Yuki).
    combined_mutations = (dm_mutations or []) + (mutations or [])
    combined_rolls = (dm_rolls or []) + (rolls or [])
    return jsonify(_adventure_response(state, manifest=manifest,
                                        extra={"rolls": combined_rolls,
                                               "mutations": combined_mutations,
                                               "choices": choices,
                                               "dm_choices": dm_choices,
                                               "dm_pipeline": dm_on,
                                               **tts, **dm_tts}))


@app.route("/adventure/move", methods=["POST"])
def adventure_move():
    """User-Move im laufenden Abenteuer. Body: {id, content}. Engine loest auf,
    Yuki kommentiert, alle Marker werden ausgewertet."""
    payload = request.get_json(silent=True) or {}
    adv_id = (payload.get("id") or "").strip()
    content = (payload.get("content") or "").strip()
    # Phase 5: optionales UI-Pre-Select aus Threat-HUD-Klick. Wird nur in
    # resolve_user_move ausgewertet wenn der Freitext kein Substring/Stem-
    # Match liefert. Manifest-agnostisch durchgereicht.
    explicit_target_threat_id = (payload.get("target_threat_id") or "").strip() or None
    # Phase 7 (2026-06-07): One-shot-Schalter "DM jetzt mal stumm". Frontend setzt
    # das Flag pro Send, snappt nach dem Send selbst zurueck. Wenn True: DM-Call
    # komplett uebersprungen (spart Latenz), Yuki kriegt die Runde alleine. Welt-
    # Mutation findet in DIESEM Move nicht statt - User muss bewusst sein dass
    # loc/items/etc. nicht greifen.
    dm_silent_request = bool(payload.get("dm_silent"))
    if not adv_id:
        return jsonify({"ok": False, "error": "id fehlt"}), 400
    if not content:
        return jsonify({"ok": False, "error": "content leer"}), 400
    try:
        state = adventure_engine.load_adventure(adv_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": f"adventure '{adv_id}' nicht gefunden"}), 404
    if adventure_engine.is_closed(state):
        return jsonify({"ok": False, "error": "adventure ist geschlossen"}), 409
    try:
        manifest = adventure_engine.load_manifest(state["manifest"])
    except (FileNotFoundError, ValueError) as e:
        return jsonify({"ok": False, "error": f"manifest-fehler: {e}"}), 500

    # Phase 7 (2026-06-07): Dual-LLM-Toggle. Bei dm_llm_enabled fuehrt der DM
    # die Welt (state/encounter/choice-Marker auf DM-Output), Yuki ist Slim-
    # Mitspielerin (nur [move:...] + [roll:...]). Bei False bleibt der Phase-1-6-
    # Pfad mit Single-LLM (Yuki erzaehlt + spielt in einem).
    dm_on = bool(manifest.get("dm_llm_enabled"))

    # 1) User-Move loggen
    adventure_engine.apply_user_turn(state, content)

    # 1b) Phase 4 Sparring: passive SP-Regeneration am Rundenanfang + Round-
    #     Counter inkrementieren. Sonst brennen die teuren Moves nach 2-3
    #     Runden aus. Skipt automatisch bei Solo-Manifests (keine actors{}).
    adventure_engine.apply_round_start_regen(state, regen_per_round=1)

    # 2) Engine-Aufloesung (manifest-spezifisch, Phase 1: zahlen_raten).
    # Wenn die Engine durch diesen Move das Spiel schliesst (Win/Loss), wird
    # die Meta-Episode unten am Ende noch in diesem Call angehaengt - sonst
    # bliebe ein siegreich beendetes Spiel ohne Spur in den Episodes.
    was_active = (state.get("status") != "closed")
    engine_text, engine_meta = adventure_engine.resolve_user_move(
        state, manifest, content,
        explicit_target_threat_id=explicit_target_threat_id)
    if engine_text:
        adventure_engine.apply_engine_turn(state, engine_text, meta=engine_meta)

    # Shared marker-state accumulators - werden je nach Pfad vom DM oder
    # von Yuki befuellt. mutations werden zusammengefuehrt damit das Frontend
    # alle Chips in einem mut-row sieht; choices/rolls bleiben pro Bubble.
    rolls: list = []
    mutations: list = []
    choices: list = []
    dm_rolls: list = []
    dm_mutations: list = []
    dm_choices: list = []
    dm_reply_clean = ""
    encounter_spawned_this_turn = False

    # 2b) Phase 7 Dual-LLM: DM-Call ZWISCHEN Engine-Resolve und Yuki-Call.
    #     DM bekommt user_action als Anker damit er auf DIESEN Move reagiert.
    #     Marker-Pipeline auf DM-Output: roll -> state -> encounter -> choices.
    #     KEIN move-Marker auf DM-Output (move ist Yukis Slot).
    if dm_on and state.get("status") != "closed" and not dm_silent_request:
        try:
            dm_raw = yc.generate_dm_reply(state, manifest, user_action=content)
        except Exception as e:
            print(f"  [Adventure-DM-LLM-Fehler: {e}]")
            dm_raw = ""
        if dm_raw:
            d, dm_rolls = adventure_engine.expand_roll_markers(dm_raw)
            d, dm_mutations = adventure_engine.expand_state_markers(d, state)
            # Encounter-Spawn ist DM-Slot in Phase 7. Helper-Logik (cleared/active-
            # Gate, mode-Flip, location_state=active) bleibt identisch.
            d, dm_enc_mutations = adventure_engine.expand_encounter_markers(d, state)
            if dm_enc_mutations:
                dm_mutations = (dm_mutations or []) + dm_enc_mutations
                encounter_spawned_this_turn = any(
                    m.get("key") == "encounter_spawn" for m in dm_enc_mutations)
            d, dm_choices = adventure_engine.extract_choices(d)
            # Sicherheitsnetz: alle nicht-DM-Marker (move/mood/note/etc.) raus.
            d = yc.strip_all_markers(d)
            dm_reply_clean = d
            adventure_engine.apply_dm_turn(state, d,
                                            rolls=dm_rolls or None,
                                            mutations=dm_mutations or None,
                                            choices=dm_choices or None)
            mutations = (mutations or []) + (dm_mutations or [])

    # 3) Yuki kommentiert - SLIM-Pfad, isoliert von HISTORY. Im Dual-LLM-Pfad
    # sieht sie den frischen DM-Turn schon im state.turns-Kontext und reagiert
    # nur darauf (kein Welt-Mutations-Slot mehr - nur [move:...] + [roll:...]).
    reply_raw = ""
    yuki_text_for_tts = ""   # Single-Source-of-Truth fuer TTS, siehe unten
    # Wenn das Spiel jetzt vorbei ist (Engine hat status=closed gesetzt), darf
    # Yuki nochmal kurz kommentieren, aber wir machen keinen Folge-Turn-Loop.
    try:
        reply_raw = yc.generate_adventure_reply(state, manifest)
    except Exception as e:
        # Yuki-Reply optional - der Engine-Turn alleine reicht zur State-Mutation.
        print(f"  [Adventure-LLM-Fehler: {e}]")
    yuki_move_info = None
    yuki_engine_text = None
    yuki_engine_meta = None
    yuki_rolls: list = []
    yuki_mutations: list = []
    yuki_choices: list = []
    if reply_raw:
        r, yuki_rolls = adventure_engine.expand_roll_markers(reply_raw)
        rolls = (rolls or []) + (yuki_rolls or [])
        if dm_on:
            # Slim-Yuki: KEINE state/encounter/choice-Marker (DM-Slot). System-
            # Prompt verbietet sie, hier strip_world_markers als hartes Sicherheitsnetz
            # (falls Yuki versucht "Yakuza" via item_add zu schmuggeln, fliegt es raus).
            # extract_move_marker zieht legitimen Move-Marker raus fuer Resolve,
            # strip_world_markers entfernt alle uebrigen Welt-Marker.
            r, yuki_move_info = adventure_engine.extract_move_marker(r)
            r = adventure_engine.strip_world_markers(r)
            r = yc.strip_all_markers(r)
        else:
            # Bestand Single-LLM-Pfad: Yuki ist Erzaehler+Mitspielerin, alle Marker
            # erlaubt. Mutations werden hier in state geschrieben.
            r, yuki_mutations = adventure_engine.expand_state_markers(r, state)
            mutations = (mutations or []) + (yuki_mutations or [])
            r, enc_mutations = adventure_engine.expand_encounter_markers(r, state)
            if enc_mutations:
                mutations = (mutations or []) + enc_mutations
                encounter_spawned_this_turn = encounter_spawned_this_turn or any(
                    m.get("key") == "encounter_spawn" for m in enc_mutations)
            r, yuki_choices = adventure_engine.extract_choices(r)
            choices = (choices or []) + (yuki_choices or [])
            r, yuki_move_info = adventure_engine.extract_move_marker(r)
            r = yc.strip_all_markers(r)
        yuki_text_for_tts = r
        adventure_engine.apply_yuki_turn(state, r,
                                          rolls=yuki_rolls or None,
                                          mutations=yuki_mutations or None,
                                          choices=yuki_choices or None)

    # 3a) Yukis Move resolven (Sparring Phase 4 ODER Co-Op Phase 5). Bei Solo-
    #     Manifests ohne characters{} / actors{} / Yuki-Move-Marker skippt das
    #     selbst. Wenn das Spiel bereits geschlossen ist (Michael war schon KO),
    #     schlaegt Yuki auch nicht mehr nach.
    is_coop = adventure_engine.is_coop_state(state)
    # Phase 6: Im Story-Mode darf Yuki KEIN [move:...] gegen jemanden resolven -
    # die Sparring/Co-Op-Branches wuerden sonst gegen Michael bzw. einen Threat
    # gehen. Wenn Yuki den Marker in der Story-Bubble schreibt, wird er beim
    # extract_move_marker zwar gezogen, aber hier ignoriert. Stolperfalle 22.
    cur_mode = (state.get("state") or {}).get("mode")
    if yuki_move_info and state.get("status") != "closed" \
            and isinstance(manifest.get("characters"), dict) \
            and cur_mode != "story":
        yuki_move = adventure_engine.move_from_marker_info(
            state, manifest, "yuki", yuki_move_info)
        if yuki_move:
            if is_coop:
                # Co-Op: Yuki greift Threat an (Auto-Pick = schwaechster Threat,
                # Substring-Hint aus letztem Yuki-Text fuer ausdruckliches Targeting).
                hint_text = reply_raw or ""
                yuki_engine_text, yuki_engine_meta = adventure_engine.resolve_coop_player_move(
                    state, manifest, attacker="yuki",
                    move=yuki_move, user_content=hint_text)
            else:
                # Sparring (PvP): Yuki greift Michael an.
                yuki_engine_text, yuki_engine_meta = adventure_engine.resolve_sparring_move(
                    state, manifest, attacker="yuki", move=yuki_move)
            if yuki_engine_text:
                adventure_engine.apply_engine_turn(state, yuki_engine_text,
                                                    meta=yuki_engine_meta)

    # Phase 6: is_coop neu auswerten - Encounter-Spawn weiter oben hat den State
    # gerade frisch auf Co-Op gekippt, die alte is_coop-Variable spiegelt das nicht.
    is_coop = adventure_engine.is_coop_state(state)

    # 3b) Phase 5 Co-Op: Threats-Phase - alle lebenden Threats greifen jeweils
    #     einen random lebenden Spieler an. Skipt automatisch bei Solo/PvP-
    #     Manifests (run_threats_phase liefert leere Liste). Wenn das Spiel
    #     schon vorbei ist (Threats besiegt durch Yukis Move), greift kein
    #     Threat mehr nach (alive_threats == []).
    # Phase 6: Auf dem SPAWN-Turn schlagen die frisch gespawnten Threats noch
    # NICHT zu - das fuehlt sich narrativ unfair an ("Raeuber stuermen rein und
    # treffen sofort"). Michael kriegt die naechste Runde, um zu reagieren,
    # DANN greifen die Threats. Pendant zu coop_kyoto_cafe's initial_engine_msg-
    # Beat "Michael ist zuerst dran".
    threat_results: list = []
    if is_coop and state.get("status") != "closed" \
            and not encounter_spawned_this_turn:
        threat_results = adventure_engine.run_threats_phase(state, manifest)
        for engine_text, meta in threat_results:
            adventure_engine.apply_engine_turn(state, engine_text, meta=meta)

    # 3c) Outcome-Check: Co-Op (Michael KO -> loss, alle Threats KO -> win)
    #     ODER Sparring (Multi-Aktor-KO, Phase 4) ODER Solo (kein Check).
    #     check_coop_outcome ist no-op bei state ohne threats[], check_ko_auto_close
    #     no-op bei state ohne actors{}. Beides idempotent gegen status=closed.
    if is_coop:
        # Phase 6: manifest mitgeben - bei Hybrid-Manifests (win_condition !=
        # all_threats_down) fluepft alle-Threats-KO nicht das Spiel sondern
        # geht zurueck in Story-Modus (combat_cleared, state.status bleibt active).
        ko_text = adventure_engine.check_coop_outcome(state, manifest)
        # Event-Label folgt dem Status-Flip: combat_cleared wenn nur Modus-Flip,
        # sonst der finale Outcome. Frontend kann darauf konditionieren (z.B.
        # andere Bubble-Tonung).
        ko_event = ("coop_outcome" if state.get("status") == "closed"
                    else "combat_cleared")
    else:
        ko_text = adventure_engine.check_ko_auto_close(state)
        ko_event = "ko"
    # Phase 7 Combat-Klammer: bei dm_on UND combat_cleared (alle Threats KO,
    # mode flippt story zurueck, Spiel laeuft weiter) ersetzt ein zweiter DM-
    # Call die hardcoded "Stille kehrt zurueck"-Engine-Bubble. DM erzaehlt das
    # Wrap-Up narrativ + setzt Ball-zurueck (Frage + Choices) damit die Story
    # weiterlaeuft. Bei Spiel-Ende (coop_outcome -> status=closed) bleibt die
    # Engine-Bubble - das ist ein finaler Beat, nicht eine Story-Fortsetzung.
    dm_wrap_reply_clean = ""
    dm_wrap_rolls: list = []
    dm_wrap_mutations: list = []
    dm_wrap_choices: list = []
    if ko_text:
        if dm_on and ko_event == "combat_cleared":
            try:
                wrap_raw = yc.generate_dm_reply(state, manifest,
                                                signal="combat_cleared")
            except Exception as e:
                print(f"  [Adventure-DM-Wrap-LLM-Fehler: {e}]")
                wrap_raw = ""
            if wrap_raw:
                w, dm_wrap_rolls = adventure_engine.expand_roll_markers(wrap_raw)
                w, dm_wrap_mutations = adventure_engine.expand_state_markers(w, state)
                # KEIN expand_encounter_markers im Wrap - cleared-Location wuerde
                # ihn ohnehin strippen, aber gegen Few-Shot-Leak hartes Strip.
                w = adventure_engine.strip_encounter_markers(w)
                w, dm_wrap_choices = adventure_engine.extract_choices(w)
                w = yc.strip_all_markers(w)
                dm_wrap_reply_clean = w
                adventure_engine.apply_dm_turn(state, w,
                                                rolls=dm_wrap_rolls or None,
                                                mutations=dm_wrap_mutations or None,
                                                choices=dm_wrap_choices or None,
                                                meta={"event": "combat_wrap"})
                # DM-Wrap-Mutations + Rolls in die Top-Level-Buckets mergen damit
                # Frontend-Chips ankommen (analog Move-Pfad).
                mutations = (mutations or []) + (dm_wrap_mutations or [])
                rolls = (rolls or []) + (dm_wrap_rolls or [])
            else:
                # DM-Fallback: leere Wrap-Reply -> Standard-Engine-Bubble zeigen
                # damit der Spieler den Mode-Flip ueberhaupt mitbekommt.
                adventure_engine.apply_engine_turn(state, ko_text,
                                                    meta={"event": ko_event})
        else:
            adventure_engine.apply_engine_turn(state, ko_text,
                                                meta={"event": ko_event})

    # 4) Engine-Selbstabschluss: wenn das Spiel mit DIESEM Move erstmals
    #    auf 'closed' kippt, gleich die Meta-Episode anhaengen. Beim
    #    manuellen /adventure/end ist der Helper idempotent (no-op wenn
    #    schon abgehakt).
    meta_episode = None
    episodes_added = 0
    if was_active and state.get("status") == "closed":
        meta_episode, episodes_added = _adventure_finalize_episode(state, manifest)

    adventure_engine.save_adventure(state)
    # TTS direkt aus yuki_text_for_tts (cleaned, post-marker) statt ueber
    # _last_yuki_text - seit Phase 4 schliesst der Move-Cycle mit einer engine-
    # Bubble (Sparring-Resolve), wodurch _last_yuki_text leer zurueckkommt
    # (bricht bei engine ab). Direct-Pass ist die einzige Wahrheit fuer
    # "was Yuki gerade gesagt hat".
    tts = _adventure_tts_payload(yuki_text_for_tts)
    # Phase 7 Dual-LLM: DM-Mutations stecken bereits in mutations (zusammengefuehrt),
    # DM-Choices kommen separat raus damit Frontend sie unter der DM-Bubble
    # rendert. dm_reply_clean ist die UI-fertige DM-Bubble fuer Live-Render.
    extra = {"rolls": rolls, "mutations": mutations, "choices": choices,
             "engine_meta": engine_meta, **tts}
    if dm_on:
        # dm_pipeline-Flag damit Frontend den Stumm-Toggle nur bei Phase-7-
        # Manifests anzeigt (Single-LLM-Manifests kennen den DM-Pfad nicht).
        extra["dm_pipeline"] = True
        extra["dm_silent_used"] = dm_silent_request
        extra["dm_reply"] = dm_reply_clean
        extra["dm_rolls"] = dm_rolls
        extra["dm_mutations"] = dm_mutations
        extra["dm_choices"] = dm_choices
        # Phase 7: DM-Bubble vertonen (gleiche Pipeline wie Yuki). Frontend
        # spielt sequentiell: DM -> Yuki -> ggf. DM-Wrap. Bei leerer dm_reply
        # bleiben die Felder leer -> Frontend skipt.
        extra.update(_adventure_dm_tts_payload(dm_reply_clean))
        # Phase 7 Combat-Klammer: zweiter DM-Call bei combat_cleared liefert
        # eigene Wrap-Up-Bubble. Frontend rendert sie via state.turns automatisch
        # (role="dm"), aber die expliziten Felder helfen Live-Streaming/UI-Auto-
        # Updates beim Aufdecken (z.B. Choices die als Cards unter dem Wrap-Up
        # erscheinen).
        if dm_wrap_reply_clean:
            extra["dm_wrap_reply"] = dm_wrap_reply_clean
            extra["dm_wrap_rolls"] = dm_wrap_rolls
            extra["dm_wrap_mutations"] = dm_wrap_mutations
            extra["dm_wrap_choices"] = dm_wrap_choices
            # Wrap-Audio: Frontend spielt das NACH Yukis Reply ab (Reihenfolge
            # DM-Move -> Yuki -> Engine-Resolves -> DM-Wrap).
            extra.update(_adventure_dm_wrap_tts_payload(dm_wrap_reply_clean))
    if meta_episode:
        extra["meta_episode"] = meta_episode
        extra["episodes_added"] = episodes_added
    return jsonify(_adventure_response(state, manifest=manifest, extra=extra))


@app.route("/adventure/peaceful", methods=["POST"])
def adventure_peaceful():
    """Peaceful-Mode-Toggle fuer ein laufendes Adventure. Body: {id, enabled:bool}.
    Persistiert state.peaceful_mode + speichert. Engine-Effekte greifen ab dem
    naechsten /adventure/move: expand_encounter_markers strippt Encounter-Marker
    (siehe block_reason 'peaceful'), build_dm_system_msg laesst encounter_hints
    weg + setzt explizite 'kein Kampf'-Regel. Mode-Flip story->combat wird damit
    unmoeglich solange das Flag True ist.

    Nur sinnvoll fuer story_hybrid-Manifests (dm_llm_enabled+mode_default=story).
    Bei Sparring/Combat-First-Manifests rein technisch erlaubt, hat aber keinen
    Effekt - die spawnen Threats ohne [encounter:...]-Marker direkt aus dem
    Manifest. Frontend versteckt den Toggle daher hinter adventure-dm-pipeline.
    """
    payload = request.get_json(silent=True) or {}
    adv_id = (payload.get("id") or "").strip()
    enabled = bool(payload.get("enabled"))
    if not adv_id:
        return jsonify({"ok": False, "error": "id fehlt"}), 400
    try:
        state = adventure_engine.load_adventure(adv_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": f"adventure '{adv_id}' nicht gefunden"}), 404
    state["peaceful_mode"] = enabled
    adventure_engine.save_adventure(state)
    try:
        manifest = adventure_engine.load_manifest(state["manifest"])
    except (FileNotFoundError, ValueError):
        manifest = None
    return jsonify(_adventure_response(state, manifest=manifest))


@app.route("/adventure/delete", methods=["POST"])
def adventure_delete():
    """State-File eines Adventures dauerhaft loeschen. Body: {id}.
    Default-Policy: nur Closed-Adventures duerfen ueber diesen Endpoint weg -
    laufende oder pausierte Spiele sollten ueber /adventure/end gehen, sonst
    sind sie still aus der Active-Liste verschwunden ohne ordentlichen
    Wrap-Up. Meta-Episoden in yuki_episodes.json bleiben unangetastet.
    """
    payload = request.get_json(silent=True) or {}
    adv_id = (payload.get("id") or "").strip()
    if not adv_id:
        return jsonify({"ok": False, "error": "id fehlt"}), 400
    try:
        state = adventure_engine.load_adventure(adv_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": f"adventure '{adv_id}' nicht gefunden"}), 404
    if state.get("status") != "closed":
        return jsonify({"ok": False,
                        "error": "nur abgeschlossene Adventures koennen geloescht werden - laufende bitte erst beenden"}), 409
    ok = adventure_engine.delete_adventure(adv_id)
    if not ok:
        return jsonify({"ok": False, "error": "Loeschen fehlgeschlagen"}), 500
    return jsonify({"ok": True, "id": adv_id})


@app.route("/adventure/state", methods=["GET"])
def adventure_state():
    """State-File fuer Resume ausliefern. Query: ?id=..."""
    adv_id = (request.args.get("id") or "").strip()
    if not adv_id:
        return jsonify({"ok": False, "error": "id fehlt"}), 400
    try:
        state = adventure_engine.load_adventure(adv_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": f"adventure '{adv_id}' nicht gefunden"}), 404
    try:
        manifest = adventure_engine.load_manifest(state["manifest"])
    except (FileNotFoundError, ValueError):
        manifest = None
    return jsonify(_adventure_response(state, manifest=manifest))


@app.route("/adventure/end", methods=["POST"])
def adventure_end():
    """Abenteuer beenden. Body: {id}. Setzt status=closed, generiert EINEN
    inhalt-leeren Meta-Episode-Eintrag ('mit Michael X gespielt, ~Ymin, Outcome')
    und haengt ihn an yuki_episodes.json - das ist die EINZIGE Spur, die in den
    realen Memory-Stack rueberwandert. KEIN Story-Inhalt, KEINE Personen-/
    Heart-/Facts-Updates."""
    payload = request.get_json(silent=True) or {}
    adv_id = (payload.get("id") or "").strip()
    if not adv_id:
        return jsonify({"ok": False, "error": "id fehlt"}), 400
    try:
        state = adventure_engine.load_adventure(adv_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": f"adventure '{adv_id}' nicht gefunden"}), 404
    try:
        manifest = adventure_engine.load_manifest(state["manifest"])
    except (FileNotFoundError, ValueError) as e:
        return jsonify({"ok": False, "error": f"manifest-fehler: {e}"}), 500

    # Unterscheidung zwischen natuerlichem Spiel-Ende und manuellem Abbruch:
    # - Wenn die Engine das Spiel selbst geschlossen hat (status war schon
    #   "closed" bevor /end ueberhaupt aufgerufen wurde), ist die Meta-Episode
    #   entweder schon angehaengt (idempotent geschuetzt) oder wird hier
    #   nachgezogen falls /move sie verpasst hat. Wert: ja, merken.
    # - Wenn der User mitten im Spiel auf "Beenden" klickt (status==active),
    #   ist das eine Abbruch-Geste - keine merkwuerdige Erinnerung wert.
    #   User-Wunsch 2026-06-07: kein Eintrag bei Abbruch. Wir setzen das
    #   episode_added-Flag trotzdem damit ein spaeterer /end-Klick nichts
    #   nachreicht.
    was_natural_close = (state.get("status") == "closed")
    state["status"] = "closed"
    if was_natural_close:
        meta_episode, added = _adventure_finalize_episode(state, manifest)
    else:
        state.setdefault("state", {})["outcome"] = \
            state.get("state", {}).get("outcome") or "aborted"
        meta_episode, added = None, 0
        state["episode_added"] = True
    adventure_engine.save_adventure(state)
    return jsonify({"ok": True, "id": adv_id, "status": "closed",
                    "meta_episode": meta_episode, "episodes_added": added})


# ===========================================================================
# Adventure-Generator (neues Manifest via Wizard, Multi-Pass-LLM)
# ===========================================================================
@app.route("/adventure/generate", methods=["POST"])
def adventure_generate_start():
    """Generation-Job starten. Body: {mode, setting, pitch, tone, yuki_role?,
    duration?, display_name?}. mode = 'cozy_story' | 'story_hybrid'.

    Antwort: {ok, job_id} - Frontend abonniert dann den SSE-Stream unter
    /adventure/generate/<job_id>/events um Pass-by-Pass-Progress zu sehen."""
    payload = request.get_json(silent=True) or {}
    try:
        job_id = adventure_generator.start_generation(payload)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/adventure/generate/<job_id>/events", methods=["GET"])
def adventure_generate_events(job_id):
    """SSE-Stream fuer einen laufenden Generation-Job. Yieldt jeden Event aus
    job.events ab last_idx, schliesst bei status in (done, error, cancelled).
    Heartbeat alle ~15s damit Browser/Proxies die Connection halten.

    Re-Connect-safe: liefert ab Start ALLE Events aus dem Buffer aus, dann
    schaltet auf Live-Modus - falls Frontend kurz die Verbindung verliert
    bekommt es beim Reconnect alles nachgeliefert."""
    job = adventure_generator.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": f"job '{job_id}' nicht gefunden"}), 404

    def gen():
        last_idx = 0
        last_heartbeat = time.time()
        yield ": connected\n\n"
        while True:
            with job.lock:
                events = job.events[last_idx:]
                status = job.status
            for ev in events:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            last_idx += len(events)
            if status in ("done", "error", "cancelled"):
                break
            if time.time() - last_heartbeat > 15:
                yield ": keep-alive\n\n"
                last_heartbeat = time.time()
            time.sleep(0.4)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store",
                             "X-Accel-Buffering": "no"})


@app.route("/adventure/generate/<job_id>/state", methods=["GET"])
def adventure_generate_state(job_id):
    """Snapshot des Job-States. Polling-Fallback falls SSE nicht funktioniert
    + Frontend nutzt es nach 'done' um Preview-Felder neu zu laden."""
    job = adventure_generator.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": f"job '{job_id}' nicht gefunden"}), 404
    with job.lock:
        return jsonify({"ok": True,
                        "job_id": job.id,
                        "status": job.status,
                        "error": job.error,
                        "preview": job.preview,
                        "event_count": len(job.events)})


@app.route("/adventure/generate/<job_id>/save", methods=["POST"])
def adventure_generate_save(job_id):
    """Draft als config/adventures/<slug>.json speichern. Returns endgueltigen
    slug + name (display_name). load_manifest-Dry-Run als Sanity-Check."""
    job = adventure_generator.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": f"job '{job_id}' nicht gefunden"}), 404
    try:
        slug = adventure_generator.save_job_manifest(job_id)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "slug": slug,
                    "display_name": (job.preview or {}).get("display_name")})


@app.route("/adventure/generate/<job_id>/cancel", methods=["POST"])
def adventure_generate_cancel(job_id):
    """Laufenden Job abbrechen. Worker prueft cancel_event zwischen jedem Pass
    und beendet beim naechsten Check sauber. Draft bleibt nicht gespeichert."""
    ok = adventure_generator.cancel_job(job_id)
    if not ok:
        return jsonify({"ok": False, "error": f"job '{job_id}' nicht gefunden"}), 404
    return jsonify({"ok": True, "job_id": job_id, "status": "cancel_requested"})


# ===========================================================================
# Startup
# ===========================================================================
def ollama_upgrade_loop():
    """Periodischer Hochschalt-Job (config llm.auto_upgrade_*). select_ollama_server
    ist 'klebrig': nach einem Failover auf ein schwaecheres Modell bleibt das System
    dort haengen, weil der laufende Server ja funktioniert (kein Fehler -> kein Re-
    Probe). Dieser Loop holt es alle N Sekunden wieder aufs HOECHSTE erreichbare
    Modell zurueck.

    Latenz-frei by design: probe_best_server() (die Netz-Checks, bis 2s pro totem
    Server) laeuft AUSSERHALB des LOCK; nur der instantane Swap der Globals liegt
    drin. Ein laufender Turn wird also nie verzoegert. Deckt als Bonus pre-emptiven
    ABSTIEG ab - stirbt der aktive Server, schalten wir um, BEVOR der naechste Turn
    in den Failover-Haenger laeuft."""
    interval = int(_cfg("llm", "auto_upgrade_interval_sec", 300))
    if not _cfg("llm", "auto_upgrade_enabled", True) or interval <= 0:
        print("  [Ollama-Auto-Upgrade: aus (config)]", flush=True)
        return
    print(f"  [Ollama-Auto-Upgrade: an, alle {interval}s]", flush=True)
    while True:
        time.sleep(interval)
        try:
            name, url, model = yc.probe_best_server()
            if not url:
                continue                      # keiner erreichbar -> aktiven lassen
            if url == yc.OLLAMA_URL and model == yc.OLLAMA_MODEL:
                continue                      # schon das hoechste erreichbare
            with LOCK:
                # Re-Check unter Lock: zwischen Probe und Lock-Erwerb kann ein Turn
                # selbst failovert haben. Nur swappen, wenn es weiterhin ein echter
                # Wechsel ist (sonst ueberschreiben wir eine frische Failover-Wahl).
                if url != yc.OLLAMA_URL or model != yc.OLLAMA_MODEL:
                    old = yc.OLLAMA_MODEL
                    yc.OLLAMA_URL, yc.OLLAMA_MODEL = url, model
                    arrow = "⬆" if yc._model_size_b(model) >= yc._model_size_b(old) else "⬇"
                    print(f"  [{arrow} Ollama-Auto-Switch: {old} -> {model} ({name})]",
                          flush=True)
        except Exception as e:
            print(f"  [Ollama-Upgrade-Loop-Fehler: {e}]", flush=True)


def init_pipeline():
    """Modelle laden + Services pruefen + rollendes Gedaechtnis (wie in main.py)."""
    global MODEL, HISTORY, MEMORY, SYSTEM_MSG
    print("\nService-Check:")
    yc.preflight()
    if yc.select_ollama_server() is None:
        print("  [!!] Kein Ollama-Server verfuegbar! Lokal starten (ollama app.exe)"
              " oder Zweitrechner/IP in OLLAMA_SERVERS pruefen.")
    MODEL = yc.load_whisper()
    yc.warmup()
    HISTORY, MEMORY = yc.start_session()
    SYSTEM_MSG = yc.build_system_msg(MEMORY, CURRENT_PERSONA)
    if MEMORY:
        print(f"\nYukis Erinnerung:\n{MEMORY}")
    if yc.HEART_ENABLED:
        heart = yc.load_heart()
        if heart:
            print(f"\nYukis Herz ({len(heart)}):\n{yc._heart_block(heart)}")
    # Auto-Vision-Thread starten (laeuft passiv, bis im UI eingeschaltet wird).
    # last_comment auf JETZT stempeln: sonst startet es auf 0.0 (Epoch) und das
    # Impuls-Gate bekaeme "~29 Mio Min seit letztem Kommentar" gefuettert (56 Jahre),
    # bis Yuki das erste Mal reagiert. Jetzt = "die Beobachtungs-Uhr laeuft ab Start".
    _auto_web["last_comment"] = time.time()
    threading.Thread(target=auto_vision_loop_web, daemon=True,
                     name="auto-vision-web").start()
    # Proactive-Thread (Spontan-Aussagen) - analog Default AUS.
    threading.Thread(target=proactive_loop_web, daemon=True,
                     name="proactive-web").start()
    # Ollama-Auto-Upgrade: holt das System nach einem Failover periodisch wieder
    # aufs hoechste erreichbare Modell zurueck (config llm.auto_upgrade_*).
    threading.Thread(target=ollama_upgrade_loop, daemon=True,
                     name="ollama-upgrade").start()
    _proactive_init_clock()                               # initialer Cooldown, fresh-Pool bis erster Turn
    # Steward-Thread (autonome Sehnsucht) - sticky State aus memory/yuki_steward.json.
    _st = yc.load_steward_state()
    _steward["enabled"] = bool(_st.get("enabled", False))
    _steward["notstop"] = bool(_st.get("notstop", False))
    _steward["last_sehnsucht_reach_out_ts"] = float(_st.get("last_sehnsucht_reach_out_ts", 0.0) or 0.0)
    _steward["start_ts"] = time.time()                    # Idle zaehlt ab Start, nicht ab ts=0
    _steward["day_stamp"] = datetime.datetime.now().strftime("%Y-%m-%d")
    threading.Thread(target=steward_loop_web, daemon=True, name="steward-web").start()
    # Gaming Screen Companion-Thread (laeuft passiv; macht nichts solange enabled=False).
    threading.Thread(target=gaming_loop_web, daemon=True, name="gaming-web").start()
    threading.Thread(target=gaming_knowledge_loop_web, daemon=True, name="gaming-knowledge").start()
    print(f"\n(Auto-Vision + Proactive + Steward + Gaming-Thread laufen; Defaults AUS, im UI einschaltbar)")


def main():
    print("=" * 64)
    print("  Yuki – Web/Handy-Server (BFF)")
    print("=" * 64)

    lan_ip = get_lan_ip()
    certfile, keyfile = ensure_cert(lan_ip)

    init_pipeline()

    print("\n" + "=" * 64)
    print("  Bereit. Am Handy im selben WLAN oeffnen:")
    print(f"      https://{lan_ip}:{PORT}")
    print("  (Beim ersten Mal: 'Erweitert' -> 'Trotzdem fortfahren' wegen des")
    print("   self-signed Zertifikats. Danach Mikrofon erlauben.)")
    print("=" * 64 + "\n")

    # use_reloader=False: sonst wuerde Flask alles doppelt starten (Whisper 2x laden!)
    app.run(host="0.0.0.0", port=PORT, ssl_context=(certfile, keyfile),
            threaded=True, use_reloader=False, debug=False)


if __name__ == "__main__":
    main()
