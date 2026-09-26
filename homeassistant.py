# homeassistant.py - REST-Anbindung an einen lokalen Home-Assistant-Server.
#
# Pattern wie yuki_calendar.py: lazy config, graceful no-op bei Netz-/Auth-Fehler
# (der Voice-Pfad crasht NIE - fehlt HA, faellt alles HA-bezogene still auf no-op),
# kurzer TTL-Cache mit async-Refresh fuer die Zustands-Abfrage (world_context fragt
# pro Turn, soll HA aber nicht mit Requests bombardieren und den Voice-Pfad nicht
# blocken).
#
# Steuerung laeuft ueber die HA-REST-API mit Long-Lived-Token - KEIN Umweg ueber
# Assist/Conversation. Darum brauchen Entitaeten KEINE "fuer Assist freigegeben"-
# Markierung; Yuki spricht sie direkt per entity_id an.
#
# Credentials + Geraete-Allowlist: config/yuki_homeassistant.json (gitignored).
# Template: config/yuki_homeassistant.template.json.

from __future__ import annotations
import json, os, threading, time
from pathlib import Path

import requests

# Tunables (ohne Geheimnisse) aus config/settings.jsonc - alle mit Code-Default.
try:
    from config_loader import settings as _CFG
    _HA_CFG = _CFG.get("homeassistant", None, {}) or {}
except Exception:
    _HA_CFG = {}

CONFIG_PATH = Path(__file__).parent / "config" / "yuki_homeassistant.json"
HTTP_TIMEOUT = _HA_CFG.get("http_timeout_seconds", 5)            # nie laenger blocken als 5s
STATE_CACHE_TTL = _HA_CFG.get("state_cache_ttl_seconds", 20)     # Zustand max alle 20s frisch ziehen
RECONNECT_COOLDOWN = _HA_CFG.get("reconnect_cooldown_seconds", 30)  # nach Fehler 30s Ruhe

# Drei Interaktions-Typen (kind), abgeleitet aus der HA-Domain:
#   switch  = an/aus      (light/switch/fan/input_boolean) -> [ha:Name|on/off]
#   sensor  = nur Wert     (sensor/binary_sensor)          -> nur world_context, read-only
#   climate = Wert+regelbar (climate, dim. Heizung)        -> world_context + [ha:Name|set|18]
#   number  = Wert+regelbar (number/input_number)          -> world_context + [ha:Name|set|X]
DOMAIN_KIND = {
    "light": "switch", "switch": "switch", "fan": "switch", "input_boolean": "switch",
    "sensor": "sensor", "binary_sensor": "sensor",
    "climate": "climate", "number": "number", "input_number": "number",
}
# Domains, die die Discovery einliest (alle bekannten kinds). media_player/cover/lock
# bewusst raus (andere Service-Semantik). Konfigurierbar via settings.jsonc.
DISCOVER_DOMAINS = tuple(_HA_CFG.get("discover_domains", list(DOMAIN_KIND.keys())))
_VALUE_KINDS = ("sensor", "climate", "number")     # gehoeren in den "Werte"-Bereich
_SETTABLE_KINDS = ("climate", "number")            # zusaetzlich per [ha:|set|X] regelbar

def _device_kind(d) -> str:
    """Interaktions-Typ eines Config-Eintrags: explizites kind-Feld, sonst aus der
    Domain abgeleitet (rueckwaertskompatibel fuer Eintraege ohne kind)."""
    k = (d.get("kind") or "").strip().lower()
    if k in ("switch", "sensor", "climate", "number"):
        return k
    eid = (d.get("entity_id") or "").lower()
    dom = eid.split(".", 1)[0] if "." in eid else ""
    return DOMAIN_KIND.get(dom, "switch")

# ---- Config -----------------------------------------------------------------

_config = None
_config_lock = threading.Lock()

def _load_config():
    global _config
    with _config_lock:
        if _config is not None:
            return _config
        cfg = {}
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  [ha] config-load Fehler: {e}", flush=True)
                cfg = {}
        # ENV uebersteuert Datei (Startup-Script kann Secrets injecten)
        for env_key, cfg_key in (("YUKI_HA_URL", "base_url"),
                                 ("YUKI_HA_TOKEN", "token")):
            v = os.environ.get(env_key)
            if v:
                cfg[cfg_key] = v
        _config = cfg
        return _config

def is_configured() -> bool:
    cfg = _load_config()
    return bool(cfg.get("base_url") and cfg.get("token"))

def converse_token() -> str:
    """Optionales Shared-Secret fuer den /ha/converse-Voice-Endpoint. Steht in der
    Config als `converse_token`; fehlt es, ist der Endpoint offen (LAN-Trust wie der
    Rest des BFF). Gesetzt -> das HA-custom_component muss den Token mitschicken."""
    return (_load_config().get("converse_token") or "").strip()

def _enabled_devices() -> list:
    """Alle AKTIVEN Geraete (enabled) normalisiert inkl. kind. Basis fuer die
    kind-gefilterten Pools unten. `enabled:false` blendet komplett aus (nicht in
    world_context, nicht steuerbar); fehlendes Feld gilt als aktiv (rueckwaertskompat)."""
    cfg = _load_config()
    out = []
    for d in (cfg.get("devices") or []):
        if d.get("enabled", True) is False:
            continue
        eid = (d.get("entity_id") or "").strip().lower()
        if eid:
            out.append({"entity_id": eid,
                        "name": (d.get("name") or eid).strip(),
                        "area": (d.get("area") or "").strip(),
                        "kind": _device_kind(d)})
    return out

def devices() -> list:
    """AKTIVE SCHALTBARE Geraete (kind==switch) - der Pool fuer den on/off-Marker +
    die switchable-Zeile im world_context. Bewusst eng: Yuki schaltet NUR, was hier
    drinsteht (kein Zugriff aufs ganze Haus durch einen halluzinierten entity_id)."""
    return [d for d in _enabled_devices() if d["kind"] == "switch"]

def value_devices() -> list:
    """AKTIVE Wert-Geraete (sensor/climate/number) - fuer die 'Werte'-Zeile im
    world_context (Yuki kennt den Messwert passiv, read-only)."""
    return [d for d in _enabled_devices() if d["kind"] in _VALUE_KINDS]

def settables() -> list:
    """AKTIVE regelbare Geraete (climate/number) - der Pool fuer den [ha:|set|X]-Marker."""
    return [d for d in _enabled_devices() if d["kind"] in _SETTABLE_KINDS]

def _allowed_ids() -> set:
    """Alle aktiven entity_ids (schaltbar + Werte) - States werden fuer beide geholt."""
    return {d["entity_id"] for d in _enabled_devices()}

def all_devices() -> list:
    """ALLE konfigurierten Geraete inkl. enabled-Flag + kind (fuer den UI-Inspector).
    Anders als die Pools oben filtert das weder disabled noch nach kind."""
    cfg = _load_config()
    out = []
    for d in (cfg.get("devices") or []):
        eid = (d.get("entity_id") or "").strip().lower()
        if eid:
            out.append({"entity_id": eid,
                        "name": (d.get("name") or eid).strip(),
                        "area": (d.get("area") or "").strip(),
                        "kind": _device_kind(d),
                        "enabled": d.get("enabled", True) is not False})
    return out

def _save_config(cfg) -> None:
    """Config zurueckschreiben + Modul-Cache invalidieren -> der naechste Turn
    (world_context/devices/resolve) liest die Aenderung OHNE Server-Restart."""
    global _config
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    with _config_lock:
        _config = None

def _update_device(entity_id: str, **fields) -> bool:
    """Felder eines Geraets in der Config setzen + sofort persistieren (Live-Reload,
    kein Restart). Liest die DATEI frisch (nicht den Cache), damit parallele Edits
    nicht verloren gehen. True wenn das Geraet gefunden + geschrieben wurde."""
    if not CONFIG_PATH.exists():
        return False
    eid = (entity_id or "").strip().lower()
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [ha] _update_device config-load Fehler: {e}", flush=True)
        return False
    hit = False
    for d in (cfg.get("devices") or []):
        if (d.get("entity_id") or "").strip().lower() == eid:
            d.update(fields)
            hit = True
            break
    if not hit:
        return False
    _save_config(cfg)
    return True

def set_device_enabled(entity_id: str, enabled: bool) -> bool:
    """enabled-Flag eines Geraets live setzen (kein Restart)."""
    return _update_device(entity_id, enabled=bool(enabled))

def set_device_name(entity_id: str, name: str) -> bool:
    """Anzeige-/Steuer-Namen setzen - den Namen, den Yuki im [ha:NAME|...]-Marker
    nutzt. Live persistiert. Leerer Name wird abgelehnt."""
    name = (name or "").strip()
    if not name:
        return False
    return _update_device(entity_id, name=name)

def discover(prune: bool = False, dry_run: bool = False) -> dict:
    """Geraete-Discovery: HA-States holen, NEUE schaltbare Entitaeten
    (TOGGLEABLE_DOMAINS) mit enabled:false eintragen, VERSCHWUNDENE disablen (oder
    mit prune entfernen), BESTEHENDE unangetastet lassen. Schreibt die Config +
    invalidiert den Cache (ausser dry_run). Liest base_url/token aus der Datei bzw.
    ENV. Returns {ok, new:[{entity_id,name}], vanished:[...], total, enabled, error?}."""
    if not CONFIG_PATH.exists():
        return {"ok": False, "error": "config/yuki_homeassistant.json fehlt"}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"config-load: {e}"}
    base = (cfg.get("base_url") or os.environ.get("YUKI_HA_URL") or "").rstrip("/")
    token = cfg.get("token") or os.environ.get("YUKI_HA_TOKEN") or ""
    if not base or not token:
        return {"ok": False, "error": "base_url/token fehlen"}
    try:
        r = requests.get(f"{base}/api/states",
                        headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        states = r.json()
    except Exception as e:
        return {"ok": False, "error": f"HA ({base}) nicht erreichbar: {e}"}

    ha_devs = {}
    for s in states:
        eid = (s.get("entity_id") or "").lower()
        dom = eid.split(".", 1)[0] if "." in eid else ""
        if dom in DISCOVER_DOMAINS:
            ha_devs[eid] = {
                "name": (s.get("attributes") or {}).get("friendly_name") or eid,
                "kind": DOMAIN_KIND.get(dom, "switch"),
            }
    ha_ids = set(ha_devs)

    existing = cfg.get("devices") or []
    existing_ids = {(d.get("entity_id") or "").strip().lower() for d in existing}
    new = []
    for eid, info in sorted(ha_devs.items()):
        if eid not in existing_ids:
            existing.append({"entity_id": eid, "name": info["name"], "area": "",
                            "kind": info["kind"], "enabled": False})
            existing_ids.add(eid)
            new.append({"entity_id": eid, "name": info["name"], "kind": info["kind"]})

    kept, vanished = [], []
    for d in existing:
        eid = (d.get("entity_id") or "").strip().lower()
        if eid and eid not in ha_ids:
            vanished.append({"entity_id": d.get("entity_id"), "name": d.get("name")})
            if prune:
                continue
            d["enabled"] = False
        kept.append(d)
    cfg["devices"] = kept

    if not dry_run:
        _save_config(cfg)
    enabled_n = sum(1 for d in kept if d.get("enabled", True) is not False)
    return {"ok": True, "new": new, "vanished": vanished, "pruned": bool(prune),
            "total": len(kept), "enabled": enabled_n, "dry_run": bool(dry_run)}

# ---- HTTP (mit Cooldown nach Fehler) ----------------------------------------

_failed_until = 0.0
_fail_lock = threading.Lock()

def _down() -> bool:
    with _fail_lock:
        return time.time() < _failed_until

def _mark_down():
    global _failed_until
    with _fail_lock:
        _failed_until = time.time() + RECONNECT_COOLDOWN

def _headers(cfg):
    return {"Authorization": f"Bearer {cfg['token']}",
            "Content-Type": "application/json"}

def call_service(domain: str, service: str, entity_id: str, extra: dict = None) -> bool:
    """Ruft einen HA-Service (z.B. homeassistant.turn_on oder climate.set_temperature).
    `extra` liefert zusaetzliche Service-Daten (z.B. {'temperature': 18}). True bei
    HTTP 2xx, graceful False bei Timeout/Auth-Fehler/nicht-konfiguriert."""
    cfg = _load_config()
    if not (cfg.get("base_url") and cfg.get("token")):
        return False
    if _down():
        return False
    url = f"{cfg['base_url'].rstrip('/')}/api/services/{domain}/{service}"
    payload = {"entity_id": entity_id}
    if extra:
        payload.update(extra)
    try:
        r = requests.post(url, headers=_headers(cfg), json=payload, timeout=HTTP_TIMEOUT)
        if 200 <= r.status_code < 300:
            _invalidate_state_cache()      # Zustand hat sich gerade geaendert
            return True
        print(f"  [ha] {domain}.{service} {entity_id} -> HTTP {r.status_code}", flush=True)
        return False
    except Exception as e:
        print(f"  [ha] call_service Fehler: {e}", flush=True)
        _mark_down()
        return False

def _resolve_in(target: str, pool: list):
    """target = entity_id ODER freundlicher Name (case-insensitive) -> Geraet-Dict aus
    `pool` oder None. Robust gegen LLM-Tippfehler in langen entity_ids: Yuki darf
    einfach 'Sofalicht' schreiben (genau diese Truncation-Fehler kamen live vor)."""
    if not target:
        return None
    t = target.strip().lower()
    for d in pool:                       # exakte entity_id zuerst
        if d["entity_id"] == t:
            return d
    for d in pool:                       # dann exakter Name (case-insensitive)
        if d["name"].strip().lower() == t:
            return d
    return None

def resolve(target: str):
    """Aufloesung im SCHALTBAR-Pool (fuer on/off)."""
    return _resolve_in(target, devices())

def resolve_any(target: str):
    """Aufloesung ueber ALLE aktiven Geraete (schaltbar + Werte) - fuer Anzeige-Name
    (Bubble-Icon), unabhaengig vom kind."""
    return _resolve_in(target, _enabled_devices())

def set_entity(target: str, action: str) -> bool:
    """Schaltet ein schaltbares Allowlist-Geraet on/off/toggle. `target` ist entity_id
    ODER Name. Generische homeassistant-Domain (turn_on/off/toggle domainuebergreifend
    fuer light/switch/fan/...), darum kein Domain-Sniffing."""
    action = (action or "").strip().lower()
    d = resolve(target)
    if d is None:
        print(f"  [ha] target '{target}' nicht schaltbar / nicht in Allowlist - verweigert",
              flush=True)
        return False
    svc = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action)
    if not svc:
        print(f"  [ha] unbekannte Aktion '{action}'", flush=True)
        return False
    return call_service("homeassistant", svc, d["entity_id"])

def set_value(target: str, value) -> bool:
    """Setzt einen Zielwert auf einem regelbaren Geraet (climate -> set_temperature,
    number -> set_value). `value` darf '18', '18,5', '18°C' o.ae. sein - der numerische
    Teil wird extrahiert. target = entity_id ODER Name aus dem settables-Pool."""
    d = _resolve_in(target, settables())
    if d is None:
        print(f"  [ha] set: target '{target}' nicht regelbar / nicht in Allowlist", flush=True)
        return False
    import re as _re
    m = _re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not m:
        print(f"  [ha] set: kein numerischer Wert in '{value}'", flush=True)
        return False
    num = float(m.group(0).replace(",", "."))
    if d["kind"] == "climate":
        return call_service("climate", "set_temperature", d["entity_id"],
                           extra={"temperature": num})
    if d["kind"] == "number":
        return call_service("number", "set_value", d["entity_id"], extra={"value": num})
    return False

def announce(message, entity_id, *, timeout=30):
    """Push a spoken announcement to an assist_satellite (Voice PE) via HA.
    Uses the satellite's pipeline TTS (Wyoming-Yuki bridge) -> Yuki's own voice.
    Unsolicited push, no wake-word.

    BLOCKING: HA returns only after the satellite finished speaking (~9s for two
    sentences). Therefore this uses its OWN long timeout and, unlike call_service,
    NEVER triggers _mark_down() on a slow/failed call -- that would arm the 30s HA
    cooldown and lame [ha:] light control. preannounce:false drops HA's default
    'pling' before beilaeufige comments. Returns True on HTTP 2xx."""
    message = (message or "").strip()
    if not message or not entity_id:
        return False
    cfg = _load_config()
    if not (cfg.get("base_url") and cfg.get("token")):
        return False
    url = f"{cfg['base_url'].rstrip('/')}/api/services/assist_satellite/announce"
    payload = {"entity_id": entity_id, "message": message, "preannounce": False}
    try:
        r = requests.post(url, headers=_headers(cfg), json=payload, timeout=timeout)
        if not (200 <= r.status_code < 300):
            print(f"  [ha] announce non-2xx: HTTP {r.status_code} {r.text[:200]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"  [ha] announce Fehler: {e}", flush=True)   # deliberately NO _mark_down()
        return False

# ---- world_context-Bausteine (vom yuki_core._ha_context formatiert) ---------

def _format_reading(kind: str, state: str, attrs: dict) -> str:
    """Menschenlesbarer Messwert fuer die world_context-'Werte'-Zeile."""
    unit = (attrs.get("unit_of_measurement") or "").strip()
    if str(state).strip().lower() in ("unavailable", "unknown", "none", ""):
        return "nicht verfügbar"
    if kind == "climate":
        cur, tgt = attrs.get("current_temperature"), attrs.get("temperature")
        s = f"{cur}°C" if cur is not None else (state or "?")
        if tgt is not None:
            s += f" (Ziel {tgt}°C)"
        if state and state not in ("", "unknown"):
            s += f", Modus {state}"
        return s
    return f"{state} {unit}".strip()

def switchable_states() -> list:
    """[{name, area, state}] fuer die schaltbare world_context-Zeile."""
    st = states()
    out = []
    for d in devices():
        rec = st.get(d["entity_id"]) or {}
        out.append({"name": d["name"], "area": d["area"], "state": rec.get("state")})
    return out

def value_readings() -> list:
    """[{name, area, kind, text, settable}] fuer die 'Werte'-world_context-Zeile."""
    st = states()
    out = []
    for d in value_devices():
        rec = st.get(d["entity_id"]) or {}
        out.append({"name": d["name"], "area": d["area"], "kind": d["kind"],
                    "text": _format_reading(d["kind"], rec.get("state", "?"),
                                            rec.get("attrs") or {}),
                    "settable": d["kind"] in _SETTABLE_KINDS})
    return out

# ---- Zustand (TTL-Cache + async refresh, wie der Kalender) ------------------

_state_lock = threading.Lock()
_state_cache = {"states": None, "ts": 0.0}
_refresh_thread = None
_refresh_lock = threading.Lock()

def _invalidate_state_cache():
    with _state_lock:
        _state_cache["ts"] = 0.0

def _fetch_states() -> dict:
    """Zustaende aller Allowlist-Entitaeten holen. {entity_id: {state, attrs}}.
    attrs traegt Einheit + Ist-/Ziel-Temperatur fuer die 'Werte'-Zeile. Bricht beim
    ersten Netz-Fehler ab (markiert down) - Teil-Daten sind okay."""
    cfg = _load_config()
    if not (cfg.get("base_url") and cfg.get("token")) or _down():
        return {}
    base = cfg["base_url"].rstrip("/")
    out = {}
    for eid in _allowed_ids():
        try:
            r = requests.get(f"{base}/api/states/{eid}", headers=_headers(cfg),
                            timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                j = r.json()
                out[eid] = {"state": j.get("state") or "unknown",
                            "attrs": j.get("attributes") or {}}
        except Exception as e:
            print(f"  [ha] state {eid} Fehler: {e}", flush=True)
            _mark_down()
            break
    return out

def _refresh_states_sync():
    fresh = _fetch_states()
    with _state_lock:
        _state_cache["states"] = fresh
        _state_cache["ts"] = time.time()

def _refresh_states_async():
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _refresh_thread = threading.Thread(target=_refresh_states_sync,
                                          daemon=True, name="yuki-ha-refresh")
        _refresh_thread.start()

def states() -> dict:
    """Gecachte Zustaende der Allowlist-Geraete. Liefert sofort den Cache (oder {}
    beim Erst-Call), tritt bei Cache-Miss einen async Refresh los - der naechste
    Turn sieht dann frische Daten. Kein Voice-Chat-Lag."""
    with _state_lock:
        cached, ts = _state_cache["states"], _state_cache["ts"]
    if cached is None or (time.time() - ts) >= STATE_CACHE_TTL:
        _refresh_states_async()
    return cached or {}
