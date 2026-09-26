"""
Voller Konversationsverlauf in SQLite (memory/yuki_history.sqlite).

Hintergrund (Design 2026-06-04, siehe memory/yuki-habits.md):
Loest archive/sessions/*.json ab und liefert gleichzeitig die Daten-Basis fuer
das Habits-Auto-Gate (yuki_habits_db) -- pro Message bekommen wir persona/mood/
ts strukturiert in den Griff, was den Habits-Gate sauber per-message-persona
annotieren laesst statt heuristisch raten zu muessen.

Designentscheidungen:
- Speaker = 'michael' | 'yuki' | 'system'. role='user' im LLM-History entspricht
  speaker='michael', role='assistant' entspricht 'yuki'.
- content wird UN-gestripped gespeichert (mit Markern wie [mood:X], [timer:...],
  [heart:...]). Das ist die Archiv-Wahrheit; UI/Recherche kann beim Lesen strippen.
- persona/mood = aktiver State zum Zeitpunkt der Message (Provenance fuer Habits).
- tokens = optionales Wadoku-Token-JSON (nur Yuki-Replies mit JP).
- session_id wird pro Server-Start neu generiert; end_session() rotiert sie
  explizit. Format 's_YYYYmmdd_HHMMSS_<6hex>' fuer Lesbarkeit beim DB-Browsen.

Defensiv: Alle Funktionen catchen interne Fehler und loggen nur einmal, damit
ein DB-Lock/Disk-Voll-Fehler nicht die Reply-Pipeline killt -- das Feature ist
ein Archiv-Layer, kein kritischer Pfad.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


_ROOT = Path(__file__).parent
_MEMORY_DIR = _ROOT / "memory"
DB_PATH = _MEMORY_DIR / "yuki_history.sqlite"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts         TEXT NOT NULL,
  speaker    TEXT NOT NULL,
  content    TEXT NOT NULL,
  persona    TEXT,
  mood       TEXT,
  tokens     TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_ts        ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msg_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_msg_speaker   ON messages(speaker, ts);
"""

_LOCK = threading.Lock()         # serialisiert Schreibzugriffe; sqlite3 ist
                                 # zwar thread-safe, aber Python-seitige
                                 # Connection-Wiederverwendung will sync sein
_CONN: Optional[sqlite3.Connection] = None
_SESSION_ID: Optional[str] = None
_INIT_FAILED = False             # nach Erstfehler nicht stiller Endlos-Spam


def _new_session_id() -> str:
    """Format 's_YYYYmmdd_HHMMSS_<6hex>' -- chronologisch sortierbar +
    Kollisionsschutz bei Server-Restart im selben Sekundenfenster."""
    return "s_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def _get_conn() -> Optional[sqlite3.Connection]:
    """Lazy-Init der Connection. Idempotent, Thread-safe via _LOCK.
    Liefert None wenn Init dauerhaft fehlgeschlagen ist (gibt Caller die
    Chance, sauber zu skippen statt zu crashen)."""
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
                check_same_thread=False,   # wir serialisieren selbst via _LOCK
                isolation_level=None,      # autocommit -- jede Message direkt persistent
            )
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA_SQL)
            _CONN = conn
            return _CONN
        except Exception as e:
            _INIT_FAILED = True
            print(f"  [yuki_history_db: Init fehlgeschlagen ({e}); Verlaufs-DB deaktiviert]")
            return None


def current_session_id() -> str:
    """Aktuelle Server-Session-ID. Bei Erstaufruf generiert + memoized."""
    global _SESSION_ID
    if _SESSION_ID is None:
        _SESSION_ID = _new_session_id()
    return _SESSION_ID


def start_new_session() -> str:
    """Naechste Inserts gehen unter neuer session_id. Aufrufen bei
    end_session()-Button und intern bei Komprimierung wenn die Sitzung
    semantisch gewechselt hat."""
    global _SESSION_ID
    _SESSION_ID = _new_session_id()
    return _SESSION_ID


def _iso_now() -> str:
    """Lokale ISO-Zeit mit Mikrosekunden -- Sortier-Stable, ohne TZ-Suffix
    (Yuki ist eh single-region, und Migration-Eintraege haben kein TZ-Wissen)."""
    return datetime.datetime.now().isoformat(timespec="microseconds")


def persist_message(
    speaker: str,
    content: str,
    *,
    persona: Optional[str] = None,
    mood: Optional[str] = None,
    tokens: Optional[Any] = None,
    session_id: Optional[str] = None,
    ts: Optional[str] = None,
) -> Optional[int]:
    """Eine Message in den Verlauf schreiben.

    speaker: 'michael' | 'yuki' | 'system'
    content: roher Text MIT Markern (Archiv-Wahrheit)
    tokens:  list/dict ODER schon JSON-string ODER None
    session_id: ueberschreibt aktuelle Session (z.B. fuer Migration)
    ts: ueberschreibt _iso_now() (fuer Migration mit synthetischer T12:00:00-Zeit)

    Liefert die eingefuegte id, oder None bei Fehler.
    """
    if not speaker or content is None:
        return None
    conn = _get_conn()
    if conn is None:
        return None
    if isinstance(tokens, (list, dict)):
        tokens_str = json.dumps(tokens, ensure_ascii=False)
    elif isinstance(tokens, str):
        tokens_str = tokens
    else:
        tokens_str = None
    sid = session_id or current_session_id()
    ts_val = ts or _iso_now()
    try:
        with _LOCK:
            cur = conn.execute(
                "INSERT INTO messages(session_id, ts, speaker, content, persona, mood, tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, ts_val, speaker, content, persona, mood, tokens_str),
            )
            return cur.lastrowid
    except Exception as e:
        print(f"  [yuki_history_db.persist_message fehlgeschlagen: {e}]")
        return None


def get_messages(
    *,
    session_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    speaker: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Messages lesen. Alle Filter optional, kombinierbar.
    since/until: ISO-Strings (lexikografisch vergleichbar dank ISO-Format).
    """
    conn = _get_conn()
    if conn is None:
        return []
    where = []
    args: list[Any] = []
    if session_id:
        where.append("session_id = ?")
        args.append(session_id)
    if since:
        where.append("ts >= ?")
        args.append(since)
    if until:
        where.append("ts <= ?")
        args.append(until)
    if speaker:
        where.append("speaker = ?")
        args.append(speaker)
    sql = "SELECT * FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    try:
        with _LOCK:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  [yuki_history_db.get_messages fehlgeschlagen: {e}]")
        return []


def count_messages(session_id: Optional[str] = None) -> int:
    """Gesamt-Anzahl Messages (oder pro Session). Fuer Status/Debug."""
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with _LOCK:
            if session_id:
                r = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                                 (session_id,)).fetchone()
            else:
                r = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0
