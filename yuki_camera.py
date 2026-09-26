"""yuki_camera.py - Kamera-Abstraktion fuer Yukis Augen (autonomes Beobachten).

Mehrere Quellen nebeneinander: BRIO via dshow (lokal), Netzwerk-Cams via
HTTP-Snapshot oder RTSP, optional mit PTZ (schwenk-/neigbar mit Presets). Der
Rest der Vision-Pipeline (LFM2.5-VL, Auto-Vision, Keepsakes) haengt nur an den
zurueckgegebenen JPEG-Bytes und merkt vom Quellwechsel nichts.

Config: config/cameras.json (gitignored, Credentials im Klartext - analog
yuki_calendar.json); Template als config/cameras.template.json. Code-Defaults
(BRIO) greifen, wenn Datei/Keys fehlen -> Yuki laeuft auch ohne Config wie vor
dem Umbau.

PTZ aktuell nur HiSilicon Hi3510 (native CGI preset.cgi). ONVIF bewusst NICHT:
der ONVIF-Layer der getesteten upCam uebersetzt Preset-Tokens fehlerhaft und
quittiert auch ungueltige Tokens mit "OK" (siehe tools/test_ptz.py + Memory
yuki-ptz-camera-observation). Das Ende der Anfahrt wird per Bewegungs-Diff
erkannt (Snapshots pollen bis das Bild still steht), mit Hard-Cap als Safety.
"""
import io
import os
import json
import time
import tempfile
import threading
import subprocess
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config" / "cameras.json"
RUNTIME_DIR = HERE / "runtime"

# Code-Defaults (greifen wenn cameras.json fehlt) - BRIO wie vor dem Umbau.
_DEFAULTS = {
    "default_source": "brio",
    "sources": {
        "brio": {"type": "dshow", "device": "Logitech BRIO",
                 "warmup_frames": 45, "video_size": "1280x720", "timeout": 20},
    },
}

_cfg = None
_cfg_mtime = None                # mtime der zuletzt gelesenen cameras.json (Live-Reload)
_cfg_lock = threading.Lock()
_ptz_lock = threading.Lock()     # serialisiert Kamera-Bewegungen (eine Cam, ein Mover)


# ---- Config -----------------------------------------------------------------

def _load():
    """Liest config/cameras.json gecached, aber mit LIVE-RELOAD: aendert sich die
    Datei (mtime), wird sie beim naechsten Zugriff neu eingelesen - kein Server-
    Neustart noetig (Intervall/Presets/Park-Verhalten sofort wirksam, spaetestens
    im naechsten Rotations-Zyklus). Reads sind nur ein paar pro Zyklus, der stat()
    ist vernachlaessigbar."""
    global _cfg, _cfg_mtime
    with _cfg_lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
        except OSError:
            mtime = None
        if _cfg is not None and mtime == _cfg_mtime:
            return _cfg
        cfg = json.loads(json.dumps(_DEFAULTS))   # tiefe Kopie
        if mtime is not None:
            try:
                disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if disk.get("default_source"):
                    cfg["default_source"] = disk["default_source"]
                # sources mergen statt ersetzen, damit der BRIO-Default erhalten bleibt
                cfg["sources"].update(disk.get("sources", {}) or {})
            except Exception as e:
                print(f"  [camera] config-load Fehler: {e} -> Defaults", flush=True)
                # kaputte Datei: alten gueltigen Cache behalten, falls vorhanden
                if _cfg is not None:
                    return _cfg
        _cfg = cfg
        _cfg_mtime = mtime
        return _cfg


def reload_config():
    """Cache hart invalidieren (config/cameras.json wird beim naechsten Zugriff neu
    gelesen). Wird i.d.R. nicht gebraucht - _load() reloaded selbst per mtime."""
    global _cfg, _cfg_mtime
    with _cfg_lock:
        _cfg = None
        _cfg_mtime = None
    return _load()


def _source(name=None):
    cfg = _load()
    name = name or cfg.get("default_source")
    return name, (cfg.get("sources", {}).get(name) or {})


def default_source_name():
    return _load().get("default_source")


def source_label(name=None):
    """Menschlicher Anzeigename der Quelle (Feld 'label' in cameras.json), Fallback
    = der Quell-Key. Fuer Prompts/Logs, damit Yuki 'die Raum-Cam' statt 'roomcam'
    referenzieren kann - der Marker selbst nutzt weiter den technischen Key."""
    n, src = _source(name)
    return src.get("label") or n


def watch_sources():
    """Quell-Namen, die am autonomen Beobachtungs-Reigen teilnehmen (Flag
    "watch": true in cameras.json), in Config-Reihenfolge. Fallback wenn KEINE
    Quelle das Flag setzt: nur die default_source -> eine Config ohne watch-Flags
    verhaelt sich wie vor dem Multi-Cam-Umbau (eine Cam)."""
    cfg = _load()
    srcs = cfg.get("sources", {}) or {}
    named = [n for n, s in srcs.items() if s.get("watch")]
    if named:
        return named
    d = cfg.get("default_source")
    return [d] if d and d in srcs else []


def _auth_for(src):
    if src.get("user") is not None:
        return requests.auth.HTTPBasicAuth(src.get("user", ""), src.get("pass", ""))
    return None


# ---- Reolink JSON-API (Login-Token, geteilt von Snap + PTZ) ------------------
# Reolink-Cams (z.B. E1 Pro) haben KEINE Web-UI, sondern eine JSON-API auf
# /cgi-bin/api.cgi. Login liefert ein Token (~1h gueltig), das fuer Snap UND
# PtzCtrl gebraucht wird -> hier zentral pro (api_url, user) gecached, mit
# automatischem Re-Login bei Ablauf (Reolink quittiert ein totes Token NICHT mit
# 401, sondern mit einem JSON-error-Block rspCode -6/-7 bzw. Snap liefert dann
# JSON statt JPEG -> beides faengt der Retry ab). Preset-IDs sind DIREKT
# (op:"ToPos", id:N) - KEIN Off-by-one wie bei der Hi3510-preset.cgi.

_reolink_tokens = {}             # (api_url, user) -> {"token": str, "exp": float}
_reolink_tok_lock = threading.Lock()
_reolink_rs_counter = 0


def _reolink_rs():
    """Cache-Buster-String fuer Snap (Reolink verlangt einen rs-Parameter).
    Zeit + monoton steigender Zaehler, damit zwei Snaps in derselben Sekunde
    nicht kollidieren. Race auf dem Zaehler ist hier folgenlos (nur Busting)."""
    global _reolink_rs_counter
    _reolink_rs_counter += 1
    return f"{int(time.time())}{_reolink_rs_counter}"


def _reolink_token(src, force=False):
    """Holt (gecached) ein Login-Token. Re-Login bei Ablauf oder force=True.
    Serialisiert ueber den Lock -> bei parallelem Ablauf nur EIN Re-Login."""
    api = src["api_url"]
    user = src.get("user", "")
    key = (api, user)
    now = time.time()
    with _reolink_tok_lock:
        ent = _reolink_tokens.get(key)
        if ent and not force and ent["exp"] > now + 30:
            return ent["token"]
        body = [{"cmd": "Login", "param": {"User": {
            "userName": user, "Version": "0", "password": src.get("pass", "")}}}]
        r = requests.post(api, params={"cmd": "Login"}, json=body,
                          timeout=src.get("timeout", 10), verify=src.get("verify", True))
        r.raise_for_status()
        item = r.json()[0]
        if item.get("code") != 0:
            raise RuntimeError(f"Reolink-Login fehlgeschlagen: {item.get('error')}")
        tok = item["value"]["Token"]
        lease = int(tok.get("leaseTime", 3600))
        _reolink_tokens[key] = {"token": tok["name"], "exp": now + lease}
        return tok["name"]


def _reolink_post(src, cmd, param=None):
    """JSON-Command an api.cgi mit gecachetem Token; genau ein Re-Login + Retry,
    falls das Token abgelaufen ist. Gibt das value-Dict des ersten Eintrags."""
    api = src["api_url"]
    last_err = None
    for attempt in (0, 1):
        tok = _reolink_token(src, force=(attempt == 1))
        body = [{"cmd": cmd, "param": param or {}}]
        r = requests.post(api, params={"cmd": cmd, "token": tok}, json=body,
                          timeout=src.get("timeout", 10), verify=src.get("verify", True))
        r.raise_for_status()
        item = r.json()[0]
        if item.get("code") == 0:
            return item.get("value", {})
        last_err = item.get("error", {})
        if attempt == 0 and last_err.get("rspCode") in (-6, -7):
            continue                          # Token tot -> oben Re-Login erzwingen
        break
    raise RuntimeError(f"Reolink {cmd} fehlgeschlagen: {last_err}")


# ---- Grab (Standbild holen) -------------------------------------------------

def _grab_http_snap(src):
    r = requests.get(src["snap_url"], auth=_auth_for(src), timeout=src.get("timeout", 10))
    r.raise_for_status()
    return r.content


def _grab_dshow(src):
    out = RUNTIME_DIR / "_frame_web.jpg"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "dshow",
           "-video_size", src.get("video_size", "1280x720"),
           "-i", f"video={src.get('device')}",
           "-frames:v", str(src.get("warmup_frames", 45)),   # Warmup gegen BRIO-Schwarzbild
           "-update", "1", "-y", str(out)]
    subprocess.run(cmd, check=True, timeout=src.get("timeout", 20),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out.read_bytes()


def _grab_rtsp(src):
    out = RUNTIME_DIR / "_frame_web.jpg"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-rtsp_transport", src.get("rtsp_transport", "tcp"),
           "-i", src["rtsp_url"], "-frames:v", "1", "-update", "1", "-y", str(out)]
    subprocess.run(cmd, check=True, timeout=src.get("timeout", 20),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out.read_bytes()


def _grab_reolink(src):
    """Standbild via Reolink Snap-Command (GET auf api.cgi, liefert JPEG direkt).
    Token gecached; bei Ablauf kommt JSON statt Bild zurueck -> ein Re-Login +
    Retry. ~2880x1616, sauberer als ein RTSP-Frame-Grab."""
    api = src["api_url"]
    ch = int(src.get("channel", 0))
    for attempt in (0, 1):
        tok = _reolink_token(src, force=(attempt == 1))
        params = {"cmd": "Snap", "channel": ch, "rs": _reolink_rs(), "token": tok}
        r = requests.get(api, params=params, timeout=src.get("timeout", 10),
                         verify=src.get("verify", True))
        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/"):
            return r.content
        if attempt == 0:
            continue                          # vermutlich totes Token -> Re-Login
        r.raise_for_status()
        raise RuntimeError(f"Reolink Snap lieferte kein Bild "
                           f"(CT={r.headers.get('Content-Type')!r})")


_GRABBERS = {"http_snap": _grab_http_snap, "dshow": _grab_dshow,
             "rtsp": _grab_rtsp, "reolink": _grab_reolink}


def grab(source=None):
    """Standbild der Quelle als JPEG-Bytes; None bei Fehler (Pipeline degradiert sauber)."""
    name, src = _source(source)
    fn = _GRABBERS.get(src.get("type"))
    if not fn:
        print(f"  [camera] unbekannter Quelltyp fuer '{name}': {src.get('type')!r}")
        return None
    try:
        return fn(src)
    except Exception as e:
        print(f"  [camera] Grab-Fehler ({name}): {e}")
        return None


# ---- PTZ --------------------------------------------------------------------

def ptz_config(source=None):
    _, src = _source(source)
    return src.get("ptz") or {}


def has_ptz(source=None):
    return bool(ptz_config(source).get("kind"))


def presets(source=None):
    """{position(int): label(str)} der konfigurierten Presets, nach Position sortiert."""
    raw = ptz_config(source).get("presets") or {}
    out = {}
    for k, v in raw.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            pass
    return dict(sorted(out.items()))


def home_preset(source=None):
    return int(ptz_config(source).get("home", 1))


def rotate_enabled(source=None):
    return bool(ptz_config(source).get("rotate", True))


def watch_viewpoints():
    """Alle Blickpunkte fuer den Beobachtungs-Reigen ueber ALLE watch-Cams:
    Liste von (source_name, preset_or_None, area_label). PTZ-Cams mit rotate
    steuern je Preset einen Punkt bei (nach Position sortiert), fixe Cams bzw. PTZ
    mit rotate=false genau einen festen Blick (preset None, label ''). Der Server
    rotiert ueber diese flache Liste -> wechselt automatisch Cam UND Position."""
    out = []
    for name in watch_sources():
        if has_ptz(name) and rotate_enabled(name):
            ps = presets(name)
            if ps:
                for n, label in ps.items():
                    out.append((name, n, label))
                continue
        out.append((name, None, ""))            # fixe Cam / rotate=false: ein Blick
    return out


def rotate_interval(source=None):
    """Eigenes Intervall (s) zwischen PTZ-Schwenks dieser Kamera - getrennt vom
    globalen Auto-Vision-Intervall, weil die Anfahrt selbst Sekunden dauert (sonst
    Dauerfeuer). Pro Quelle in cameras.json setzbar; Default grosszuegig."""
    try:
        return max(5, int(ptz_config(source).get("rotate_interval", 90)))
    except (TypeError, ValueError):
        return 90


def park_home_on_stop(source=None):
    """Soll die Cam beim Beenden des Beobachtens auf ihre Home-Position zurueck
    (alte Ueberwachungs-Default-Ansicht)? Default an."""
    return bool(ptz_config(source).get("park_home_on_stop", True))


def go_home(source=None):
    """Faehrt die Cam auf ihre Home-Position (Park-Stellung). Best-effort, schluckt
    Fehler (Cam offline o.ae. soll den Aufrufer nicht stoeren)."""
    if not has_ptz(source):
        return False
    try:
        goto_preset(source, home_preset(source))
        print(f"  [camera] Park: zurueck auf Home (Pos {home_preset(source)})")
        return True
    except Exception as e:
        print(f"  [camera] go_home-Fehler: {e}")
        return False


def _hi3510_goto(ptz, ui_num, auth):
    """Hi3510 preset.cgi goto. UI-Positionen sind 1-basiert, die CGI 0-basiert
    -> -number = ui_num + number_offset (Default -1). Optional -speed."""
    base = ptz["base"].rstrip("/")
    num = int(ui_num) + int(ptz.get("number_offset", -1))
    q = f"{base}/preset.cgi?-act=goto&-number={num}"
    if ptz.get("speed") is not None:
        q += f"&-speed={ptz['speed']}"
    r = requests.get(q, auth=auth, timeout=ptz.get("timeout", 10))
    r.raise_for_status()
    return r.text.strip()


def _reolink_goto(ptz, ui_num, src):
    """Reolink PtzCtrl ToPos. Preset-IDs sind DIREKT adressiert (KEIN Off-by-one
    wie Hi3510) -> number_offset Default 0, aber respektiert falls die UI doch
    versetzt zaehlt. Token-Handling steckt in _reolink_post."""
    pid = int(ui_num) + int(ptz.get("number_offset", 0))
    _reolink_post(src, "PtzCtrl", {"channel": int(src.get("channel", 0)),
                                   "op": "ToPos", "id": pid,
                                   "speed": int(ptz.get("speed", 32))})
    return f"ToPos id={pid} ok"


def _goto(ptz, ui_num, src):
    kind = ptz.get("kind")
    if kind == "hi3510":
        return _hi3510_goto(ptz, ui_num, _auth_for(src))
    if kind == "reolink":
        return _reolink_goto(ptz, ui_num, src)
    raise ValueError(f"unbekannter PTZ-kind: {kind!r}")


def goto_preset(source=None, ui_num=None):
    """Faehrt (blockierend gesetzt, nicht abgewartet) auf eine UI-Position."""
    name, src = _source(source)
    with _ptz_lock:
        return _goto(src.get("ptz") or {}, ui_num, src)


def _wait_until_settled(source_name, ptz):
    """Pollt Snapshots bis die Cam steht (zwei Frames fast identisch). Gemessen:
    Bewegung erzeugt Grauwert-Diff ~40-70, Stillstand <1 -> Schwelle 5 trennt
    robust. Hard-Cap max_s als Safety (dann 'angekommen' annehmen). 'lead' wartet
    erst kurz, damit die Mechanik sicher angelaufen ist (kein False-Positive vor
    Bewegungsbeginn). Gibt die gewartete Zeit zurueck."""
    from PIL import Image, ImageChops
    s = ptz.get("settle", {}) or {}
    max_s = float(s.get("max_s", 15.0))
    interval = float(s.get("interval", 1.0))
    thresh = float(s.get("thresh", 5.0))
    lead = float(s.get("lead", 1.5))

    def gray():
        data = grab(source_name)
        if not data:
            return None
        return Image.open(io.BytesIO(data)).convert("L").resize((160, 90))

    time.sleep(lead)
    t = lead
    prev = gray()
    if prev is None:
        return t
    while t < max_s:
        time.sleep(interval)
        t += interval
        cur = gray()
        if cur is None:
            break
        hist = ImageChops.difference(prev, cur).histogram()
        d = sum(i * c for i, c in enumerate(hist)) / (sum(hist) or 1)
        if d < thresh:
            return t
        prev = cur
    return t


def look_at(source=None, ui_num=None):
    """Faehrt (falls PTZ + ui_num) auf die Position, wartet bis still, holt dann
    ein Standbild. Gibt (jpeg_bytes|None, label) zurueck. Ohne PTZ/ui_num: nur
    Grab, label=''. Die ganze Bewegung+Wartung laeuft unter _ptz_lock, damit sich
    nie zwei Anfahrten ueberholen."""
    name, src = _source(source)
    ptz = src.get("ptz") or {}
    if ptz.get("kind") and ui_num is not None:
        label = presets(name).get(int(ui_num), "")
        with _ptz_lock:
            try:
                _goto(ptz, ui_num, src)
            except Exception as e:
                print(f"  [camera] PTZ-goto-Fehler ({name}, pos {ui_num}): {e}")
            _wait_until_settled(name, ptz)
            return grab(name), label
    return grab(name), ""


# ---- Manuelle Steuerung (Jog + Preset-Verwaltung, nur Reolink) --------------
# Diese Ops sind fuer das Kamera-Steuer-Panel (Optionen->System), damit die
# internet-gesperrte Reolink LOKAL ausgerichtet + verwaltet werden kann. Nur
# kind=="reolink": die Hi3510-upcam hat ihre eigene Weboberflaeche dafuer.

_REOLINK_JOG_OPS = {"left": "Left", "right": "Right", "up": "Up", "down": "Down"}


def _require_reolink(name, src, what):
    kind = (src.get("ptz") or {}).get("kind")
    if kind != "reolink":
        raise NotImplementedError(f"{what} nur fuer reolink (Quelle {name!r}, kind={kind!r})")


def jog(source=None, direction=None, ms=None, speed=None):
    """Step-Nudge: bewegt die Reolink kurz in eine Richtung und stoppt wieder
    (op:<Dir> -> ms warten -> op:Stop), alles serialisiert unter _ptz_lock. Ueber
    LAN vorhersehbarer als kontinuierliches Halten. direction in
    {left,right,up,down}. ms Default aus ptz.jog_ms (350), gedeckelt 50..3000."""
    name, src = _source(source)
    _require_reolink(name, src, "jog")
    op = _REOLINK_JOG_OPS.get(direction)
    if not op:
        raise ValueError(f"unbekannte jog-Richtung: {direction!r}")
    ptz = src.get("ptz") or {}
    ms = int(ms if ms is not None else ptz.get("jog_ms", 350))
    ms = max(50, min(3000, ms))
    spd = int(speed if speed is not None else ptz.get("speed", 32))
    ch = int(src.get("channel", 0))
    with _ptz_lock:
        _reolink_post(src, "PtzCtrl", {"channel": ch, "op": op, "speed": spd})
        time.sleep(ms / 1000.0)
        _reolink_post(src, "PtzCtrl", {"channel": ch, "op": "Stop"})
    return f"jog {op} {ms}ms ok"


def hw_presets(source=None):
    """Belegte Preset-Slots direkt aus der Reolink (GetPtzPreset), nur die mit
    enable==1. Fuer Drift-Abgleich im Panel (falls per Reolink-App physisch was
    angelegt wurde, das im cameras.json-Label-Map fehlt). [] fuer nicht-reolink."""
    name, src = _source(source)
    if (src.get("ptz") or {}).get("kind") != "reolink":
        return []
    val = _reolink_post(src, "GetPtzPreset", {"channel": int(src.get("channel", 0))})
    out = []
    for p in (val.get("PtzPreset") or []):
        try:
            if int(p.get("enable", 0)) == 1:
                out.append({"id": int(p["id"]), "name": p.get("name", "")})
        except (TypeError, ValueError, KeyError):
            pass
    return out


def next_free_preset_id(source=None):
    """Kleinste freie Preset-id 0..63 (weder als Label in cameras.json noch als
    belegter Hardware-Slot). Hardware-Abfrage best-effort (Cam offline -> ignoriert)."""
    used = set(presets(source).keys())
    try:
        used |= {p["id"] for p in hw_presets(source)}
    except Exception:
        pass
    for i in range(64):
        if i not in used:
            return i
    raise RuntimeError("keine freie Preset-id (0..63 alle belegt)")


def source_names():
    """Alle konfigurierten Quell-Namen (Config-Reihenfolge) - fuers Panel-Listing."""
    return list(_load().get("sources", {}).keys())


def _atomic_write_text(path, text, encoding="utf-8"):
    """Lokaler atomic write (temp+fsync+os.replace), gleiche Semantik wie
    yuki_core._atomic_write_text ([[yuki-atomic-write]]) - hier reimplementiert,
    damit der leichte Kamera-Treiber (auch von tools/ genutzt) NICHT das schwere
    yuki_core (faster-whisper/scipy) importieren muss. newline='' -> keine
    Zeilenende-Uebersetzung."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _rewrite_presets(source_name, mutate):
    """Read-modify-write der KOMPLETTEN cameras.json: laedt sie roh, ruft
    mutate(presets_dict) das NUR sources.<name>.ptz.presets aendert, schreibt
    atomar zurueck (alles andere byte-fuer-byte als Daten erhalten) und
    invalidiert den Live-Reload-Cache. presets-Keys sind Strings (JSON)."""
    disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    ptz = disk.setdefault("sources", {}).setdefault(source_name, {}).setdefault("ptz", {})
    presets_map = ptz.setdefault("presets", {})
    mutate(presets_map)
    _atomic_write_text(CONFIG_PATH, json.dumps(disk, ensure_ascii=False, indent=2))
    reload_config()


def save_preset(source=None, ui_num=None, name=None):
    """Speichert die AKTUELLE Blickrichtung als Preset: Reolink SetPtzPreset
    (Hardware) ZUERST, dann Label in cameras.json (kanonisch). ui_num=None ->
    naechste freie id (anlegen); bestehende id -> ersetzen/umbenennen. Gibt die
    id zurueck. Reihenfolge bewusst: schlaegt das JSON-Schreiben fehl, ist die
    Position schon in der Cam -> Caller kann das melden (kein stiller Teilerfolg)."""
    sname, src = _source(source)
    _require_reolink(sname, src, "save_preset")
    name = (name or "").strip()
    if not name:
        raise ValueError("Preset-Name leer")
    ui_num = next_free_preset_id(source) if ui_num is None else int(ui_num)
    ptz = src.get("ptz") or {}
    hw_id = ui_num + int(ptz.get("number_offset", 0))
    ch = int(src.get("channel", 0))
    with _ptz_lock:
        _reolink_post(src, "SetPtzPreset",
                      {"PtzPreset": {"channel": ch, "id": hw_id, "enable": 1, "name": name}})
        _rewrite_presets(sname, lambda pm: pm.__setitem__(str(ui_num), name))
    return ui_num


def delete_preset(source=None, ui_num=None):
    """Loescht ein Preset: Reolink SetPtzPreset enable:0 (Hardware) + Label aus
    cameras.json entfernen."""
    sname, src = _source(source)
    _require_reolink(sname, src, "delete_preset")
    ui_num = int(ui_num)
    ptz = src.get("ptz") or {}
    hw_id = ui_num + int(ptz.get("number_offset", 0))
    ch = int(src.get("channel", 0))
    with _ptz_lock:
        _reolink_post(src, "SetPtzPreset",
                      {"PtzPreset": {"channel": ch, "id": hw_id, "enable": 0}})
        _rewrite_presets(sname, lambda pm: pm.pop(str(ui_num), None))
