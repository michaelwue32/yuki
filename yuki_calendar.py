# yuki_calendar.py - CalDAV-Anbindung an externen Radicale-Server
#
# Pattern entspricht dem Weather-Pattern in yuki_core: lazy connect (erst beim
# ersten Call), TTL-Cache fuer Listen mit async refresh, graceful no-op bei
# Netz-/Auth-Fehler (is_configured()==False oder Connect-Fehler -> world_context
# ueberspringt den Block, kein Crash im Voice-Pfad).
#
# Credentials: config/yuki_calendar.json (oder ENV YUKI_CALDAV_URL/USER/PASS).
# Connection schreibt naive datetimes mit local-tz versehen weg, damit Termine im
# DAVx5/Samsung-Kalender mit der lokalen Uhrzeit auftauchen (kein UTC-Versatz).

from __future__ import annotations
import datetime, json, os, threading, time, uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import caldav
from icalendar import Calendar as ICal, Event as IEvent

# Tunables (CalDAV-Tuning, ohne Geheimnisse) aus config/settings.jsonc.
# Credentials bleiben in config/yuki_calendar.json (separate Datei).
try:
    from config_loader import settings as _CFG
    _CAL_CFG = _CFG.get("calendar", None, {}) or {}
except Exception:
    _CAL_CFG = {}

CONFIG_PATH = Path(__file__).parent / "config" / "yuki_calendar.json"
CACHE_TTL = _CAL_CFG.get("cache_ttl_seconds", 300)              # 5 min - Voice-Chat-tauglich, kurz genug fuer Live-Updates
HTTP_TIMEOUT = _CAL_CFG.get("http_timeout_seconds", 5)          # nie laenger blocken als 5s
RECONNECT_COOLDOWN = _CAL_CFG.get("reconnect_cooldown_seconds", 30)  # nach Fehler 30s nicht erneut versuchen
DEFAULT_TZ_NAME = _CAL_CFG.get("default_timezone", "Europe/Berlin")

# Local timezone: IANA-Name (z.B. "Europe/Berlin"), nicht datetime.now().astimezone(),
# weil das auf Windows den lokalisierten Anzeigenamen ("Mitteleuropaeische Sommerzeit")
# liefert - icalendar schreibt das als TZID-Header in den VTIMEZONE-Block, DAVx5
# lehnt es als "invalid format" ab. ZoneInfo nutzt die tzdata-Pkg (haben wir).
# Override via "timezone"-Feld in yuki_calendar.json moeglich (falls Umzug).
def _resolve_local_tz():
    name = DEFAULT_TZ_NAME
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            name = (cfg.get("timezone") or "").strip() or DEFAULT_TZ_NAME
        env = os.environ.get("YUKI_CALDAV_TZ")
        if env:
            name = env.strip()
    except Exception:
        pass
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ_NAME)

LOCAL_TZ = _resolve_local_tz()

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
                print(f"  [calendar] config-load Fehler: {e}", flush=True)
                cfg = {}
        # ENV uebersteuert Datei (Startup-Script kann Secrets injecten)
        for env_key, cfg_key in (
            ("YUKI_CALDAV_URL", "url"),
            ("YUKI_CALDAV_USER", "user"),
            ("YUKI_CALDAV_PASS", "pass"),
            ("YUKI_CALDAV_CALENDAR", "calendar_name"),
        ):
            v = os.environ.get(env_key)
            if v:
                cfg[cfg_key] = v
        _config = cfg
        return _config

def is_configured() -> bool:
    cfg = _load_config()
    return bool(cfg.get("url") and cfg.get("user") and cfg.get("pass"))

# ---- Connection (lazy, threadsafe, mit Cooldown nach Fehler) ----------------

_client = None
_calendar = None
_conn_lock = threading.Lock()
_conn_failed_until = 0.0

def _calendar_obj():
    """Liefert das aktive Calendar-Object oder None. None bedeutet 'aktuell nicht
    erreichbar' - Aufrufer machen graceful no-op. 30s-Cooldown nach Fehler verhindert,
    dass jeder Voice-Turn 5s auf einen toten Server wartet."""
    global _client, _calendar, _conn_failed_until
    with _conn_lock:
        if _calendar is not None:
            return _calendar
        if time.time() < _conn_failed_until:
            return None
        cfg = _load_config()
        if not (cfg.get("url") and cfg.get("user") and cfg.get("pass")):
            return None
        try:
            _client = caldav.DAVClient(
                url=cfg["url"],
                username=cfg["user"],
                password=cfg["pass"],
                timeout=HTTP_TIMEOUT,
            )
            principal = _client.principal()
            calendars = principal.calendars()
            if not calendars:
                print("  [calendar] keine Kalender auf dem Server gefunden", flush=True)
                _conn_failed_until = time.time() + RECONNECT_COOLDOWN
                return None
            wanted = (cfg.get("calendar_name") or "").strip().lower()
            picked = None
            if wanted:
                for c in calendars:
                    try:
                        dn = (c.get_display_name() or "").strip().lower()
                    except Exception:
                        dn = ""
                    if dn == wanted:
                        picked = c
                        break
            _calendar = picked or calendars[0]
            try:
                name = _calendar.get_display_name()
            except Exception:
                name = "(?)"
            print(f"  [calendar] verbunden mit '{name}'", flush=True)
            return _calendar
        except Exception as e:
            print(f"  [calendar] connect-Fehler: {e}", flush=True)
            _client = None
            _calendar = None
            _conn_failed_until = time.time() + RECONNECT_COOLDOWN
            return None

def _drop_connection():
    """Verbindung verwerfen - naechster Call macht frischen Reconnect.
    Aufrufen wenn ein Request mitten in der Session crasht."""
    global _client, _calendar
    with _conn_lock:
        _client = None
        _calendar = None

# ---- create_event -----------------------------------------------------------

def create_event(title: str, start_dt: datetime.datetime, duration_min: int = 60,
                 description: str | None = None) -> bool:
    """Termin anlegen. Synchron (User erwartet sofortige Sichtbarkeit), aber
    HTTP_TIMEOUT begrenzt auf max 5s. Naive datetime wird als local-tz interpretiert."""
    if not title:
        title = "Termin"
    title = title.strip()
    if duration_min <= 0:
        duration_min = 60
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
    end_dt = start_dt + datetime.timedelta(minutes=duration_min)
    # Als UTC schreiben (DTSTART:...Z) - vermeidet die VTIMEZONE-Pflicht, die
    # icalendar 7.x bei TZID=Europe/Berlin NICHT automatisch mitsendet und an
    # der Radicale mit 400 Bad Request scheitert. DAVx5/Samsung-Kalender
    # konvertieren UTC beim Anzeigen ohnehin in local time. Floating-Time-Verlust
    # nur bei Recurring Events mit DST-Uebergang - kein Use-Case fuer Yuki.
    start_utc = start_dt.astimezone(datetime.timezone.utc)
    end_utc = end_dt.astimezone(datetime.timezone.utc)

    cal = _calendar_obj()
    if cal is None:
        return False

    ical = ICal()
    ical.add("prodid", "-//Yuki//yuki_calendar.py//DE")
    ical.add("version", "2.0")
    ev = IEvent()
    ev.add("uid", f"yuki-{uuid.uuid4()}")
    ev.add("summary", title)
    ev.add("dtstart", start_utc)
    ev.add("dtend", end_utc)
    ev.add("dtstamp", datetime.datetime.now(datetime.timezone.utc))
    if description:
        ev.add("description", description)
    ical.add_component(ev)

    try:
        cal.save_event(ical.to_ical().decode("utf-8"))
        # Synchron refreshen (+1 HTTP-Roundtrip): Yuki wartet auf die Server-Antwort
        # ohnehin schon, dann darf sie auch direkt sehen was sie eben angelegt hat.
        # Ohne sync-Refresh wuerde der naechste Voice-Turn sonst noch den alten Cache
        # sehen (5min TTL), Yuki wuesste nichts von "ihrem" eigenen Termin.
        _refresh_cache_sync()
        return True
    except Exception as e:
        print(f"  [calendar] create_event Fehler: {e}", flush=True)
        _drop_connection()
        return False

# ---- Listen (TTL-Cache + async refresh, wie Weather) ------------------------

_cache_lock = threading.Lock()
_cache = {"today": None, "upcoming": None, "ts": 0.0}
_refresh_thread = None
_refresh_lock = threading.Lock()

def _invalidate_cache():
    with _cache_lock:
        _cache["ts"] = 0.0

def refresh_cache_async():
    """Hintergrund-Refresh anstossen wenn nicht schon einer laeuft."""
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _refresh_thread = threading.Thread(
            target=_refresh_cache_sync, daemon=True, name="yuki-cal-refresh")
        _refresh_thread.start()

def _refresh_cache_sync():
    cal = _calendar_obj()
    if cal is None:
        return
    try:
        now = datetime.datetime.now(LOCAL_TZ)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + datetime.timedelta(days=1)
        upcoming_end = today_start + datetime.timedelta(days=7)
        today = _search_events(cal, today_start, today_end)
        upcoming = _search_events(cal, today_end, upcoming_end)
        with _cache_lock:
            _cache["today"] = today
            _cache["upcoming"] = upcoming
            _cache["ts"] = time.time()
    except Exception as e:
        print(f"  [calendar] refresh Fehler: {e}", flush=True)
        _drop_connection()

def _search_events(cal, start_dt, end_dt):
    """expand=True loest auch wiederkehrende Events in einzelne Instanzen auf."""
    items = cal.search(start=start_dt, end=end_dt, event=True, expand=True)
    out = []
    for it in items:
        try:
            vobj = it.icalendar_instance
            for comp in vobj.walk("VEVENT"):
                dtstart = comp.get("dtstart")
                if not dtstart:
                    continue
                s = dtstart.dt
                if isinstance(s, datetime.datetime):
                    s_local = s.astimezone(LOCAL_TZ).replace(tzinfo=None) if s.tzinfo \
                              else s
                else:
                    # All-Day-Event (date) - als 00:00 darstellen
                    s_local = datetime.datetime.combine(s, datetime.time(0, 0))
                out.append({
                    "title": str(comp.get("summary") or "(ohne Titel)"),
                    "start": s_local,
                    "uid": str(comp.get("uid") or ""),
                })
        except Exception as e:
            print(f"  [calendar] parse-Fehler: {e}", flush=True)
    out.sort(key=lambda d: d["start"])
    return out

def list_today():
    """Heutige Termine. Liefert sofort den Cache (oder [] beim Erst-Call), tritt
    bei Cache-Miss einen async Refresh los - der naechste Aufruf sieht dann frische
    Daten. Kein Voice-Chat-Lag."""
    with _cache_lock:
        events, ts = _cache["today"], _cache["ts"]
    if events is None or (time.time() - ts) >= CACHE_TTL:
        refresh_cache_async()
    return events or []

def list_upcoming():
    """Naechste 7 Tage (ohne heute, das macht list_today)."""
    with _cache_lock:
        events, ts = _cache["upcoming"], _cache["ts"]
    if events is None or (time.time() - ts) >= CACHE_TTL:
        refresh_cache_async()
    return events or []


def upcoming_events_with_ids():
    """Heute + naechste 7 Tage, nach Startzeit sortiert, jedes mit 1-basierter id.
    Deterministische Reihenfolge -> zustandslose #N-Aufloesung im Decider-Executor."""
    evs = sorted(list_today() + list_upcoming(), key=lambda d: d["start"])
    return [{"id": i + 1, "uid": e.get("uid", ""), "title": e["title"], "start": e["start"]}
            for i, e in enumerate(evs)]


def update_event_by_uid(uid, *, start_dt=None, title=None, duration_min=None) -> bool:
    """Termin per UID aendern: alte Felder lesen, gesetzte Felder ueberschreiben, dann
    loeschen + via create_event neu anlegen (robuster als In-Place-ICS-Mutation; die neue
    UID ist ok, Identifikation laeuft ueber Inhalt, nicht UID)."""
    cal = _calendar_obj()
    if cal is None or not uid:
        return False
    try:
        ev = cal.event_by_uid(uid)
        comp = next(c for c in ev.icalendar_instance.walk("VEVENT"))
    except Exception as e:
        print(f"  [calendar] update: lookup Fehler: {e}", flush=True)
        _drop_connection()
        return False
    old_title = str(comp.get("summary") or "Termin")
    dts = comp.get("dtstart").dt if comp.get("dtstart") else None
    dte = comp.get("dtend").dt if comp.get("dtend") else None
    if isinstance(dts, datetime.datetime):
        old_start = dts.astimezone(LOCAL_TZ) if dts.tzinfo else dts.replace(tzinfo=LOCAL_TZ)
    elif dts is not None:
        old_start = datetime.datetime.combine(dts, datetime.time(0, 0), tzinfo=LOCAL_TZ)
    else:
        old_start = None
    old_dur = 60
    if isinstance(dts, datetime.datetime) and isinstance(dte, datetime.datetime):
        try:
            old_dur = max(1, int((dte - dts).total_seconds() // 60))
        except TypeError:
            pass  # gemischt naive/aware (kaputtes Fremd-Event) -> 60-min-Default

    new_title = (title.strip() if title else "") or old_title
    new_start = start_dt or old_start
    if new_start is None:
        return False
    new_dur = duration_min if (duration_min and duration_min > 0) else old_dur
    try:
        ev.delete()
    except Exception as e:
        print(f"  [calendar] update: delete-alt Fehler: {e}", flush=True)
        _drop_connection()
        return False
    return create_event(new_title, new_start, duration_min=new_dur)   # macht _refresh_cache_sync


def delete_event_by_uid(uid) -> bool:
    """Termin per UID loeschen."""
    cal = _calendar_obj()
    if cal is None or not uid:
        return False
    try:
        ev = cal.event_by_uid(uid)
        ev.delete()
        _refresh_cache_sync()
        return True
    except Exception as e:
        print(f"  [calendar] delete_event Fehler: {e}", flush=True)
        _drop_connection()
        return False
