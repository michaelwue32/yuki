"""
Yuki-Habits: Pattern-Erkennung ueber wiederkehrende Tätigkeiten + emotionale
States. Design 2026-06-04 (siehe memory/yuki-habits.md).

Zwei Tabellen in memory/yuki_habits.sqlite:
  - habit_occurrences: append-only Roh-Ereignisse, UNIQUE pro (subject, key, Tag)
  - habit_summary: vorberechneter Lese-Cache (category, concern_score,
      pattern_note, last_seen, count_30d/90d) - taeglich neu gerechnet, damit
      der Prompt-Bau in build_system_msg ohne Aggregations-SQL pro Turn auskommt

Erkennung: LLM-Gate (extract_habits in yuki_core.py) wird beim 30-Turn-
Komprimieren aufgerufen und schreibt Occurrences hier rein. Recompute laeuft
direkt danach, plus einmal taeglich (Faulheits-Trigger 'wenn last_computed_at
< today').

Concern-Score-Logik VORLAEUFIG ohne config/habit_profiles.json (kommt mit
Schritt D). Aktuell heuristisch:
  - count_30d==0 und count_90d>=3 -> 'beunruhigend' (Habit ist weggebrochen)
  - count_30d>=20                  -> 'sehr_oft'
  - count_30d>=8 + Gap-Stddev klein -> 'fester_rhythmus'
  - count_30d>=5                   -> 'regelmaessig'
  - count_30d>=2                   -> 'unregelmaessig'
  - count_30d==1                   -> 'selten'
concern_score sind moderate Defaults; habit_profiles.json hebt sie gezielt
(alkohol-taeglich = hoch, fahrrad-taeglich = niedrig).
"""
from __future__ import annotations

import datetime
import json
import statistics
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional


_ROOT = Path(__file__).parent
_MEMORY_DIR = _ROOT / "memory"
DB_PATH = _MEMORY_DIR / "yuki_habits.sqlite"
PROFILES_PATH = _ROOT / "config" / "habit_profiles.json"

# Default-Tag-Kurven falls config/habit_profiles.json fehlt - decken die
# 6 Categories aus _categorize() ab, plus einen neutralen "default"-Tag.
_DEFAULT_TAG_CURVES = {
    "default": {
        "sehr_oft": 0.50, "fester_rhythmus": 0.30, "regelmaessig": 0.25,
        "unregelmaessig": 0.20, "selten": 0.10, "beunruhigend": 0.30,
    }
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS habit_occurrences (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  habit_key   TEXT NOT NULL,
  subject     TEXT NOT NULL,
  ts          TEXT NOT NULL,
  context     TEXT,
  persona     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_occ_day
  ON habit_occurrences(subject, habit_key, substr(ts,1,10));
CREATE INDEX IF NOT EXISTS idx_occ_subj_key
  ON habit_occurrences(subject, habit_key);

CREATE TABLE IF NOT EXISTS habit_summary (
  habit_key      TEXT NOT NULL,
  subject        TEXT NOT NULL,
  computed_at    TEXT NOT NULL,
  category       TEXT,
  concern_score  REAL,
  last_seen      TEXT,
  count_30d      INTEGER,
  count_90d      INTEGER,
  pattern_note   TEXT,
  PRIMARY KEY (habit_key, subject)
);

CREATE TABLE IF NOT EXISTS habit_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Disabled-Liste: User-kuratierte Habits die NICHT mehr ins Habit-System
-- sollen. Hat Vorrang vor allem: insert_occurrences skipped, _habits_block
-- (Prompt-Render) skipped, get_summary kann sie zeigen mit Flag. Persistent
-- ueber Bootstrap-Resets hinaus - sonst kaemen weggeworfene Habits wieder rein.
CREATE TABLE IF NOT EXISTS habit_disabled (
  habit_key   TEXT NOT NULL,
  subject     TEXT NOT NULL,
  disabled_at TEXT NOT NULL,
  reason      TEXT,
  PRIMARY KEY (habit_key, subject)
);

-- Starred-Liste: User-angepinnte Habits die IMMER in den Prompt sollen,
-- unabhaengig von concern_score oder Top-N-Schwelle. Optionale Notiz wird
-- an die Prompt-Render-Zeile angehaengt (kontextueller Hinweis fuer Yuki).
-- Disable raeumt einen Star automatisch mit.
CREATE TABLE IF NOT EXISTS habit_starred (
  habit_key  TEXT NOT NULL,
  subject    TEXT NOT NULL,
  starred_at TEXT NOT NULL,
  note       TEXT,
  PRIMARY KEY (habit_key, subject)
);
"""

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_INIT_FAILED = False


# ---------------------------------------------------------------------------
# Key-Normalisierung: deutsche Umlaute -> ASCII, Sonderzeichen -> '_'.
# Hintergrund: das LLM-Gate ist nicht zuverlaessig bei deutscher ASCII-Wandlung
# ('aufraeumen' wird gerne zu 'aerraumen' verstuemmelt). Wir erlauben deshalb
# Umlaute in der LLM-Antwort und normalisieren deterministisch hier. So bleibt
# die DB konsistent, egal was das Modell schreibt - und 'aufräumen' / 'aufraeumen'
# / 'Aufräumen' landen alle als 'aufraeumen'.
# ---------------------------------------------------------------------------
import re as _re_keynorm

_UMLAUT_MAP = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
})


def normalize_habit_key(raw: str) -> str:
    """LLM-Habit-Key zu kanonischer Form: lowercase ASCII snake_case ohne
    Mehrfach-Underscores. Liefert leeren String wenn nichts uebrig bleibt."""
    if not raw:
        return ""
    s = raw.strip().lower().translate(_UMLAUT_MAP)
    # alles ausser a-z 0-9 _ wird zu _
    s = _re_keynorm.sub(r"[^a-z0-9_]+", "_", s)
    # Mehrfach-Underscores zusammenklappen, fuehrend/abschliessend strippen
    s = _re_keynorm.sub(r"_+", "_", s).strip("_")
    return s


def load_profiles() -> dict:
    """config/habit_profiles.json frisch von Disk lesen. Live-Reload pattern:
    jeder Aufruf prueft die Datei neu, kein Caching. Faellt auf hardcoded
    Defaults zurueck wenn Datei fehlt/kaputt."""
    profiles = {"tags": dict(_DEFAULT_TAG_CURVES), "habits": {}, "aliases": {}}
    if not PROFILES_PATH.is_file():
        return profiles
    try:
        data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [habit_profiles.json kaputt, Defaults bleiben: {e}]")
        return profiles
    # _comment-Felder ignorieren
    tags = data.get("tags") or {}
    if isinstance(tags, dict):
        # Merge mit Defaults: profile-Datei darf 'default' ueberschreiben
        merged = dict(_DEFAULT_TAG_CURVES)
        merged.update({k: v for k, v in tags.items() if isinstance(v, dict)})
        profiles["tags"] = merged
    habits = data.get("habits") or {}
    if isinstance(habits, dict):
        # Habit-Keys auch normalisieren, damit eingetragene 'AufRäumen' matchen
        profiles["habits"] = {normalize_habit_key(k): v for k, v in habits.items()
                              if isinstance(v, dict)}
    aliases = data.get("aliases") or {}
    if isinstance(aliases, dict):
        profiles["aliases"] = {normalize_habit_key(k): normalize_habit_key(v)
                                for k, v in aliases.items() if v}
    return profiles


def resolve_alias(key: str, aliases: dict | None = None) -> str:
    """Alias-Aufloesung. aliases=None laedt sie frisch. Mehrstufige Aliase
    werden bis zur 5. Hoffung verfolgt (Zyklus-Safety)."""
    if aliases is None:
        aliases = load_profiles().get("aliases", {})
    seen = set()
    cur = key
    for _ in range(5):
        if cur in seen or cur not in aliases:
            break
        seen.add(cur)
        cur = aliases[cur]
    return cur


def score_for(category: str, habit_key: str, profiles: dict) -> tuple[float, str | None]:
    """Concern-Score + Tag-Label fuer (category, habit_key) aus den Profilen.
    Habit ohne Eintrag faellt auf 'default'-Tag."""
    tag = (profiles.get("habits", {}).get(habit_key) or {}).get("tag", "default")
    curve = profiles.get("tags", {}).get(tag) or _DEFAULT_TAG_CURVES["default"]
    return (float(curve.get(category, 0.0)), tag)


def _get_conn() -> Optional[sqlite3.Connection]:
    """Lazy connect. Auto-Schema. None bei dauerhaftem Init-Fail."""
    global _CONN, _INIT_FAILED
    if _INIT_FAILED:
        return None
    if _CONN is not None:
        return _CONN
    with _LOCK:
        if _CONN is not None:
            return _CONN
        try:
            _MEMORY_DIR.mkdir(exist_ok=True)
            conn = sqlite3.connect(
                str(DB_PATH),
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA_SQL)
            _CONN = conn
            return _CONN
        except Exception as e:
            _INIT_FAILED = True
            print(f"  [yuki_habits_db: Init fehlgeschlagen ({e}); Habits-DB deaktiviert]")
            return None


# ---------------------------------------------------------------------------
# Lookups (vom Gate + Prompt-Bau aufgerufen)
# ---------------------------------------------------------------------------

def known_habit_keys(days: int = 90) -> list[dict]:
    """Distinkte (habit_key, subject) der letzten N Tage mit last_seen.
    Wird dem LLM-Gate als Liste mitgegeben -> Drift-Schutz ('use existing keys')."""
    conn = _get_conn()
    if conn is None:
        return []
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    try:
        with _LOCK:
            rows = conn.execute(
                "SELECT habit_key, subject, MAX(substr(ts,1,10)) AS last_seen, COUNT(*) AS n "
                "FROM habit_occurrences WHERE substr(ts,1,10) >= ? "
                "GROUP BY habit_key, subject "
                "ORDER BY last_seen DESC",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_summary(min_concern: float = 0.0, include_disabled: bool = False) -> list[dict]:
    """Summary-Zeilen sortiert nach concern_score absteigend.
    Vom Prompt-Bau aufgerufen (Top-N). Disabled-Habits werden standardmaessig
    NICHT mitgegeben - sie sammeln im Hintergrund weiter, sollen aber weder im
    Yuki-Prompt noch in der UI-Active-Liste auftauchen (Frontend zeigt sie
    separat ueber /habits/disabled). include_disabled=True nur fuer Debug."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with _LOCK:
            if include_disabled:
                rows = conn.execute(
                    "SELECT * FROM habit_summary WHERE concern_score >= ? "
                    "ORDER BY concern_score DESC, count_30d DESC",
                    (min_concern,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT s.* FROM habit_summary s "
                    "LEFT JOIN habit_disabled d "
                    "  ON d.habit_key = s.habit_key AND d.subject = s.subject "
                    "WHERE s.concern_score >= ? AND d.habit_key IS NULL "
                    "ORDER BY s.concern_score DESC, s.count_30d DESC",
                    (min_concern,),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def all_occurrences_for(habit_key: str, subject: str) -> list[dict]:
    """Voller Verlauf eines Habits (fuer Options-Modal-Anzeige spaeter)."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with _LOCK:
            rows = conn.execute(
                "SELECT * FROM habit_occurrences WHERE habit_key=? AND subject=? "
                "ORDER BY ts ASC",
                (habit_key, subject),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def list_disabled() -> set[tuple[str, str]]:
    """Set von (habit_key, subject) der User-deaktivierten Habits.
    Set-Form fuer schnelles 'in'-Lookup im Insert-Filter."""
    conn = _get_conn()
    if conn is None:
        return set()
    try:
        with _LOCK:
            rows = conn.execute(
                "SELECT habit_key, subject FROM habit_disabled"
            ).fetchall()
        return {(r["habit_key"], r["subject"]) for r in rows}
    except Exception:
        return set()


def get_disabled_meta() -> list[dict]:
    """Volle Disabled-Liste mit Timestamp + Reason fuer UI-Anzeige.
    Sortiert: zuletzt deaktiviert oben."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with _LOCK:
            rows = conn.execute(
                "SELECT habit_key, subject, disabled_at, reason FROM habit_disabled "
                "ORDER BY disabled_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def disable_habit(habit_key: str, subject: str, reason: str | None = None) -> bool:
    """Habit dauerhaft aus Yukis Prompt ausblenden - reiner Prompt-Filter.
    Die Daten (occurrences, summary) bleiben erhalten und werden weiter
    gesammelt; bei spaeterem Re-Enable steht der komplette Verlauf wieder
    zur Verfuegung (mit dem Platz im Concern-Ranking den er sich verdient hat).

    Idempotent. Liefert True bei Erfolg.

    Designwechsel 2026-06-04: vorher hat disable Bestand mitgeloescht und
    insert_occurrences geblockt -> nach Re-Enable war der Habit eine leere
    Huelse und musste sich von Position 1 hochkaempfen. User-Beobachtung
    'statt urlaub ist grillen an die Stelle gerueckt' war Symptom genau
    davon. Jetzt ist disable ein Sicht-Filter; sammeln laeuft transparent
    durch."""
    conn = _get_conn()
    if conn is None:
        return False
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with _LOCK:
            conn.execute(
                "INSERT OR REPLACE INTO habit_disabled"
                "(habit_key, subject, disabled_at, reason) VALUES (?, ?, ?, ?)",
                (habit_key, subject, now, reason),
            )
            # Star automatisch raeumen - sonst Widerspruch im User-Intent
            # (angepinnt = immer im Prompt + ignoriert = nie im Prompt). Disable
            # gewinnt, Star muss weichen. Note geht damit auch verloren - bei
            # spaeterem Re-Enable + Re-Star muss sie neu gesetzt werden.
            conn.execute(
                "DELETE FROM habit_starred WHERE habit_key=? AND subject=?",
                (habit_key, subject),
            )
        return True
    except Exception as e:
        print(f"  [yuki_habits_db.disable_habit fehlgeschlagen: {e}]")
        return False


def enable_habit(habit_key: str, subject: str) -> bool:
    """Disable wieder ruecknehmen - kuenftige Vorkommen koennen wieder
    registriert werden. Der bisherige Verlauf bleibt verloren (war beim
    disable() schon weggeworfen)."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with _LOCK:
            cur = conn.execute(
                "DELETE FROM habit_disabled WHERE habit_key=? AND subject=?",
                (habit_key, subject),
            )
            return (cur.rowcount or 0) > 0
    except Exception:
        return False


def list_starred() -> dict[tuple[str, str], str | None]:
    """Map (habit_key, subject) -> note (oder None). Map-Form fuer schnellen
    Lookup beim Prompt-Render. Note ist optional - Eintrag ohne Notiz
    landet als None-Wert."""
    conn = _get_conn()
    if conn is None:
        return {}
    try:
        with _LOCK:
            rows = conn.execute(
                "SELECT habit_key, subject, note FROM habit_starred"
            ).fetchall()
        return {(r["habit_key"], r["subject"]): r["note"] for r in rows}
    except Exception:
        return {}


def get_starred_meta() -> list[dict]:
    """Volle Starred-Liste mit Timestamp + Note fuer UI/Debug.
    Sortiert: zuletzt angepinnt oben."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with _LOCK:
            rows = conn.execute(
                "SELECT habit_key, subject, starred_at, note FROM habit_starred "
                "ORDER BY starred_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def star_habit(habit_key: str, subject: str, note: str | None = None) -> bool:
    """Habit anpinnen - landet immer im Prompt, ggf. mit Notiz-Anhang.
    Idempotent - bestehender Eintrag wird ueberschrieben (Notiz-Edit per
    erneutem star_habit-Call)."""
    conn = _get_conn()
    if conn is None:
        return False
    now = datetime.datetime.now().isoformat(timespec="seconds")
    # Note sanitize: leerstring -> NULL, max 200 Zeichen
    if note is not None:
        note = note.strip()[:200] or None
    try:
        with _LOCK:
            conn.execute(
                "INSERT OR REPLACE INTO habit_starred"
                "(habit_key, subject, starred_at, note) VALUES (?, ?, ?, ?)",
                (habit_key, subject, now, note),
            )
        return True
    except Exception as e:
        print(f"  [yuki_habits_db.star_habit fehlgeschlagen: {e}]")
        return False


def unstar_habit(habit_key: str, subject: str) -> bool:
    """Stern entfernen. Liefert True wenn vorher gestarred."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with _LOCK:
            cur = conn.execute(
                "DELETE FROM habit_starred WHERE habit_key=? AND subject=?",
                (habit_key, subject),
            )
            return (cur.rowcount or 0) > 0
    except Exception:
        return False


def delete_habit(habit_key: str, subject: str) -> int:
    """Habit komplett loeschen (Options-Modal-Loesch-Button). Liefert
    geloeschte Occ-Anzahl. Raeumt occurrences + summary + star auf - die
    drei "lebenden" Spuren des Habits. Disable-Eintrag bleibt bewusst
    stehen (Anti-Wiederkehr-Mechanik, gewollt persistent). Damit ist
    Loeschen eines gestarrten Habits ein echter Reset - kommt der Habit
    spaeter wieder, faengt er ohne Star und ohne Notiz an."""
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with _LOCK:
            cur = conn.execute(
                "DELETE FROM habit_occurrences WHERE habit_key=? AND subject=?",
                (habit_key, subject),
            )
            deleted = cur.rowcount
            conn.execute(
                "DELETE FROM habit_summary WHERE habit_key=? AND subject=?",
                (habit_key, subject),
            )
            # Star mit-aufräumen, damit Wiederkehr nicht ueberraschend
            # gestarrt + mit alter Notiz daherkommt. Bewusst KEIN DELETE
            # auf habit_disabled - der ist der "kommt nie wieder"-Mechanismus
            # und muss Loeschen ueberleben.
            conn.execute(
                "DELETE FROM habit_starred WHERE habit_key=? AND subject=?",
                (habit_key, subject),
            )
        return deleted
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Inserts (vom Gate aufgerufen)
# ---------------------------------------------------------------------------

def insert_occurrences(occs: Iterable[dict]) -> int:
    """Bulk-Insert von Habit-Eintraegen.
    occs: iterable von {habit_key, subject, date (YYYY-MM-DD), context?, persona?}
    UNIQUE-Index fuegt nichts doppelt am selben Tag ein (INSERT OR IGNORE).
    Liefert die Anzahl tatsaechlich neu eingefuegter Zeilen."""
    conn = _get_conn()
    if conn is None:
        return 0
    inserted = 0
    # Aliases einmal vorne laden statt pro-Occurrence (Drift-Konsolidierung).
    aliases = load_profiles().get("aliases", {})
    # Disabled wird hier bewusst NICHT gefiltert (Designwechsel 2026-06-04):
    # weiter sammeln, damit Re-Enable den vollstaendigen Verlauf zurueckbringt
    # inkl. Counts der Zeit waehrend Disable. Disable ist reiner Prompt-Filter,
    # nicht Daten-Stop. Siehe disable_habit() docstring.
    try:
        with _LOCK:
            for o in occs:
                key = normalize_habit_key(o.get("habit_key") or "")
                key = resolve_alias(key, aliases)
                subj = (o.get("subject") or "").strip().lower()
                date = (o.get("date") or "").strip()
                if not key or subj not in ("michael", "yuki") or not date:
                    continue
                # ts auf Mittag setzen damit die Reihenfolge innerhalb des Tages
                # nicht von der echten Uhrzeit abhaengt (Tag-Granularitaet reicht).
                ts = f"{date}T12:00:00"
                ctx = (o.get("context") or "").strip() or None
                pers = (o.get("persona") or "").strip() or None
                cur = conn.execute(
                    "INSERT OR IGNORE INTO habit_occurrences"
                    "(habit_key, subject, ts, context, persona) VALUES (?, ?, ?, ?, ?)",
                    (key, subj, ts, ctx, pers),
                )
                inserted += cur.rowcount or 0
        return inserted
    except Exception as e:
        print(f"  [yuki_habits_db.insert_occurrences fehlgeschlagen: {e}]")
        return 0


# ---------------------------------------------------------------------------
# Recompute Summary
# ---------------------------------------------------------------------------

def _meta_get(conn, key: str) -> Optional[str]:
    r = conn.execute("SELECT value FROM habit_meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO habit_meta(key, value) VALUES (?, ?)",
        (key, value),
    )


def last_recompute_date() -> Optional[str]:
    """ISO-Datum (YYYY-MM-DD) der letzten Summary-Berechnung, oder None."""
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with _LOCK:
            return _meta_get(conn, "last_recompute")
    except Exception:
        return None


def _categorize(count_30d: int, count_90d: int, gaps_days: list[int]) -> str:
    """Kategorie aus Zaehlern. Der Concern-Score kommt separat via
    score_for(category, habit_key, profiles)."""
    if count_30d == 0 and count_90d >= 3:
        return "beunruhigend"
    if count_30d >= 20:
        return "sehr_oft"
    if count_30d >= 8 and len(gaps_days) >= 4:
        try:
            sd = statistics.pstdev(gaps_days)
        except statistics.StatisticsError:
            sd = 999
        if sd <= 1.5:
            return "fester_rhythmus"
    if count_30d >= 5:
        return "regelmaessig"
    if count_30d >= 2:
        return "unregelmaessig"
    if count_30d == 1:
        return "selten"
    return "selten"


_WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _pattern_note(dates: list[datetime.date]) -> str:
    """Kurzer Hinweis-Text fuers Prompt-Render. dates = ISO-Tagesliste aufsteigend."""
    if not dates:
        return ""
    # Wochentag-Cluster: gibt's einen Wochentag der >=3x in den letzten 30 Tagen vorkommt?
    today = datetime.date.today()
    recent = [d for d in dates if (today - d).days <= 30]
    if len(recent) >= 3:
        wd_counts: dict[int, int] = {}
        for d in recent:
            wd_counts[d.weekday()] = wd_counts.get(d.weekday(), 0) + 1
        top = sorted(wd_counts.items(), key=lambda x: -x[1])
        if top[0][1] >= 3:
            wd_names = [_WEEKDAY_DE[wd] for wd, c in top if c >= 2][:2]
            if wd_names:
                return "haeufig " + "+".join(wd_names)
    # Sonst: "alle ~X Tage" als Frequenz-Hinweis
    if len(recent) >= 2:
        span = (recent[-1] - recent[0]).days
        if span >= 2:
            avg = max(1, round(span / max(1, len(recent) - 1)))
            return f"~alle {avg} Tage"
    if recent:
        gap = (today - recent[-1]).days
        if gap >= 1:
            return f"zuletzt vor {gap} Tag" + ("en" if gap != 1 else "")
    return ""


def recompute_summary() -> int:
    """Alle Habit-Summary-Zeilen neu rechnen. Liefert die Anzahl Habits in der DB."""
    conn = _get_conn()
    if conn is None:
        return 0
    today = datetime.date.today()
    today_iso = today.isoformat()
    since_30 = (today - datetime.timedelta(days=30)).isoformat()
    since_90 = (today - datetime.timedelta(days=90)).isoformat()

    profiles = load_profiles()
    try:
        with _LOCK:
            # Alle distinkten Habits ueber ALLE Zeit holen (auch die ohne Recent-
            # Occurrences -> 'beunruhigend' will weggebrochene Habits sehen).
            habits = conn.execute(
                "SELECT DISTINCT habit_key, subject FROM habit_occurrences"
            ).fetchall()
            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            written = 0
            for hk, subj in [(r["habit_key"], r["subject"]) for r in habits]:
                rows_90 = conn.execute(
                    "SELECT substr(ts,1,10) AS d FROM habit_occurrences "
                    "WHERE habit_key=? AND subject=? AND substr(ts,1,10) >= ? "
                    "ORDER BY ts ASC",
                    (hk, subj, since_90),
                ).fetchall()
                dates_90 = [datetime.date.fromisoformat(r["d"]) for r in rows_90]
                dates_30 = [d for d in dates_90 if d.isoformat() >= since_30]
                count_30d = len(dates_30)
                count_90d = len(dates_90)
                gaps = []
                for a, b in zip(dates_30, dates_30[1:]):
                    gaps.append((b - a).days)
                category = _categorize(count_30d, count_90d, gaps)
                score, _tag = score_for(category, hk, profiles)
                last_seen_row = conn.execute(
                    "SELECT MAX(substr(ts,1,10)) AS d FROM habit_occurrences "
                    "WHERE habit_key=? AND subject=?",
                    (hk, subj),
                ).fetchone()
                last_seen = last_seen_row["d"] if last_seen_row else None
                pnote = _pattern_note(dates_90)
                conn.execute(
                    "INSERT OR REPLACE INTO habit_summary"
                    "(habit_key, subject, computed_at, category, concern_score,"
                    " last_seen, count_30d, count_90d, pattern_note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (hk, subj, now_iso, category, score, last_seen,
                     count_30d, count_90d, pnote),
                )
                written += 1
            _meta_set(conn, "last_recompute", today_iso)
            return written
    except Exception as e:
        print(f"  [yuki_habits_db.recompute_summary fehlgeschlagen: {e}]")
        return 0


def recompute_if_stale() -> int:
    """Nur recomputen wenn die letzte Berechnung NICHT von heute ist.
    Wird bei jeder Memory-Verdichtung aufgerufen -> bei taeglicher Nutzung
    rechnet's max 1x/Tag, bei Server-Restart auch. Liefert betroffene Anzahl
    Habits, oder 0 wenn skipped."""
    today_iso = datetime.date.today().isoformat()
    if last_recompute_date() == today_iso:
        return 0
    return recompute_summary()


def apply_aliases_to_existing() -> int:
    """Bestehende habit_occurrences-Zeilen mit Alias-Schluessel auf den
    kanonischen Key umbiegen. Wird vom Bootstrap-Lauf am Ende gerufen, plus
    bei Bedarf manuell nach Edit von habit_profiles.json. Handlet UNIQUE-
    Kollision (substr(ts,1,10)) per DELETE der Original-Zeile - bewusst, weil
    Aliase semantisch identisch sein sollten und der kanonische Key gewinnt.
    Liefert Anzahl betroffener Zeilen."""
    conn = _get_conn()
    if conn is None:
        return 0
    aliases = load_profiles().get("aliases", {})
    if not aliases:
        return 0
    affected = 0
    try:
        with _LOCK:
            for alias_key, canonical_key in aliases.items():
                if alias_key == canonical_key:
                    continue
                # Versuche UPDATE; bei UNIQUE-Kollision (es gibt schon eine
                # canonical-Zeile am selben Tag) -> DELETE der Alias-Zeile.
                rows = conn.execute(
                    "SELECT id, subject, substr(ts,1,10) AS d FROM habit_occurrences "
                    "WHERE habit_key = ?",
                    (alias_key,),
                ).fetchall()
                for row in rows:
                    try:
                        conn.execute(
                            "UPDATE habit_occurrences SET habit_key = ? WHERE id = ?",
                            (canonical_key, row["id"]),
                        )
                        affected += 1
                    except sqlite3.IntegrityError:
                        # canonical existiert schon an dem Tag - Alias-Zeile droppen
                        conn.execute(
                            "DELETE FROM habit_occurrences WHERE id = ?",
                            (row["id"],),
                        )
                        affected += 1
                # Summary-Zeile fuer den toten Alias auch loeschen
                conn.execute(
                    "DELETE FROM habit_summary WHERE habit_key = ?",
                    (alias_key,),
                )
        return affected
    except Exception as e:
        print(f"  [yuki_habits_db.apply_aliases_to_existing fehlgeschlagen: {e}]")
        return 0


def count_occurrences() -> int:
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with _LOCK:
            r = conn.execute("SELECT COUNT(*) FROM habit_occurrences").fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0
