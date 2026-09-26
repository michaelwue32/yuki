"""
Gast-/Personen-Konversationsverlauf in SQLite (memory/yuki_guest_history.sqlite).

Design 2026-06-16 (Gast-Modus Phase 2, [[yuki-guest-identity]]):
Wenn jemand ANDERES als Michael mit Yuki redet (bekannte Person aus dem People-
Graph ODER anonymer Gast), laeuft der Turn am Server ephemer und beruehrt Michaels
Canon (conversation.json/Memory/Facts/Habits/Heart) NICHT. Trotzdem wollen wir den
vollen Roh-Verlauf behalten - fuer spaetere Suche/Auswertung. Das ist diese DB.

Bewusst eine EIGENE Datei (nicht yuki_history.sqlite), damit es sauber getrennt vom
Michael-Archiv bleibt. Gleiche Tabellen-Form wie yuki_history_db.messages, plus:
- person_id   : durable Join-Key auf den People-Graph (yuki_people.json). Bei
                anonymem Gast = 'guest'. Bei Merge zweier Personen via
                reassign_identity() mitgezogen.
- person_name : lesbarer Snapshot ("so hiess sie damals"); ueberlebt auch ein
                Delete der Person im People-Graph.
Speaker-Spalte: human-Zeile = person_id (queryable), Yuki-Zeile = 'yuki'.

guest_sessions-Tabelle traegt den Graduation-State (Phase 2, schmale Memory-
Anbindung): beim 👤->Michael-Zurueckschalten destilliert eine bekannte Person ihren
offenen Scratch in People-Bricks + Episodes. graduated_ts=NULL bedeutet "noch
offen" - DB-gestuetzt, ueberlebt also App-Close (anders als der reine In-Memory-
Puffer im server.py). Anonyme 'guest'-Sessions graduieren NIE (kind='guest'),
bleiben nur als Roh-Archiv liegen.

Defensiv: alle Funktionen catchen interne Fehler und loggen nur, damit ein DB-Lock/
Disk-Voll-Fehler nie die Reply-Pipeline killt - reiner Archiv-Layer.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


_ROOT = Path(__file__).parent
_MEMORY_DIR = _ROOT / "memory"
DB_PATH = _MEMORY_DIR / "yuki_guest_history.sqlite"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  ts          TEXT NOT NULL,
  speaker     TEXT NOT NULL,
  content     TEXT NOT NULL,
  person_id   TEXT,
  person_name TEXT,
  persona     TEXT,
  mood        TEXT,
  tokens      TEXT
);
CREATE INDEX IF NOT EXISTS idx_gmsg_ts       ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_gmsg_session  ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_gmsg_person   ON messages(person_id, ts);

CREATE TABLE IF NOT EXISTS guest_sessions (
  session_id   TEXT PRIMARY KEY,
  person_id    TEXT,
  person_name  TEXT,
  kind         TEXT,          -- 'person' | 'guest'
  started_ts   TEXT,
  graduated_ts TEXT           -- NULL = noch nicht in People/Episodes destilliert
);
CREATE INDEX IF NOT EXISTS idx_gsess_person  ON guest_sessions(person_id, graduated_ts);
"""

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_INIT_FAILED = False


def _new_session_id() -> str:
    """Format 'g_YYYYmmdd_HHMMSS_<6hex>' - chronologisch sortierbar, eigener
    Prefix 'g_' (Gast) zur klaren Abgrenzung vom Michael-Archiv ('s_')."""
    return "g_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def _get_conn() -> Optional[sqlite3.Connection]:
    """Lazy-Init der Connection. Idempotent, Thread-safe via _LOCK. None wenn
    Init dauerhaft fehlgeschlagen ist (Caller skippt dann sauber)."""
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
                isolation_level=None,      # autocommit
            )
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA_SQL)
            _CONN = conn
            return _CONN
        except Exception as e:
            _INIT_FAILED = True
            print(f"  [yuki_guest_db: Init fehlgeschlagen ({e}); Gast-Verlauf-DB deaktiviert]")
            return None


def _iso_now() -> str:
    return datetime.datetime.now().isoformat(timespec="microseconds")


def start_session(person_id: str, person_name: str, kind: str) -> Optional[str]:
    """Neue Gast-Session anlegen (eine Sitzung mit EINER Person/Gast). Liefert die
    session_id, die der Caller fuer alle persist_turn()-Calls dieser Sitzung nutzt.
    kind: 'person' (bekannt, graduiert spaeter) | 'guest' (anonym, nie graduiert).
    None bei DB-Fehler (Caller kann ohne DB weiterlaufen)."""
    conn = _get_conn()
    if conn is None:
        return None
    sid = _new_session_id()
    try:
        with _LOCK:
            conn.execute(
                "INSERT INTO guest_sessions(session_id, person_id, person_name, kind, "
                "started_ts, graduated_ts) VALUES (?, ?, ?, ?, ?, NULL)",
                (sid, person_id, person_name, kind, _iso_now()),
            )
        return sid
    except Exception as e:
        print(f"  [yuki_guest_db.start_session fehlgeschlagen: {e}]")
        return None


def persist_turn(
    session_id: str,
    speaker: str,
    content: str,
    *,
    person_id: Optional[str] = None,
    person_name: Optional[str] = None,
    persona: Optional[str] = None,
    mood: Optional[str] = None,
    tokens: Optional[Any] = None,
) -> Optional[int]:
    """Eine Zeile (human ODER yuki) der Gast-Sitzung schreiben.
    speaker: person_id fuer die human-Zeile, 'yuki' fuer Yukis Antwort.
    person_id/person_name: traegt JEDE Zeile (auch die Yuki-Zeile), damit Suche
    nach "alles mit Maureen" ohne Session-Join geht.
    content: roher Text MIT Markern (Archiv-Wahrheit). None bei Fehler."""
    if not session_id or not speaker or content is None:
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
    try:
        with _LOCK:
            cur = conn.execute(
                "INSERT INTO messages(session_id, ts, speaker, content, person_id, "
                "person_name, persona, mood, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, _iso_now(), speaker, content, person_id, person_name,
                 persona, mood, tokens_str),
            )
            return cur.lastrowid
    except Exception as e:
        print(f"  [yuki_guest_db.persist_turn fehlgeschlagen: {e}]")
        return None


def get_messages(
    *,
    session_id: Optional[str] = None,
    person_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Roh-Zeilen lesen. Alle Filter optional, kombinierbar. Fuer spaetere Suche/
    Auswertung + als Graduation-Quelle."""
    conn = _get_conn()
    if conn is None:
        return []
    where, args = [], []
    if session_id:
        where.append("session_id = ?"); args.append(session_id)
    if person_id:
        where.append("person_id = ?"); args.append(person_id)
    if since:
        where.append("ts >= ?"); args.append(since)
    if until:
        where.append("ts <= ?"); args.append(until)
    sql = "SELECT * FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"; args.append(limit)
    try:
        with _LOCK:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  [yuki_guest_db.get_messages fehlgeschlagen: {e}]")
        return []


def pending_sessions(person_id: str) -> list[dict]:
    """Noch nicht graduierte BEKANNTE-Personen-Sessions dieser person_id
    (kind='person', graduated_ts IS NULL), aelteste zuerst. Anonyme 'guest'-
    Sessions tauchen hier NIE auf - die graduieren bewusst nicht."""
    conn = _get_conn()
    if conn is None or not person_id:
        return []
    try:
        with _LOCK:
            rows = conn.execute(
                "SELECT * FROM guest_sessions WHERE person_id=? AND kind='person' "
                "AND graduated_ts IS NULL ORDER BY started_ts ASC",
                (person_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  [yuki_guest_db.pending_sessions fehlgeschlagen: {e}]")
        return []


def mark_graduated(session_id: str) -> None:
    """Session als destilliert markieren (graduated_ts setzen), damit sie nicht
    erneut in People/Episodes eingeschmolzen wird."""
    conn = _get_conn()
    if conn is None or not session_id:
        return
    try:
        with _LOCK:
            conn.execute("UPDATE guest_sessions SET graduated_ts=? WHERE session_id=?",
                         (_iso_now(), session_id))
    except Exception as e:
        print(f"  [yuki_guest_db.mark_graduated fehlgeschlagen: {e}]")


def reassign_identity(old_id: str, new_id: str, new_name: Optional[str] = None) -> int:
    """person_id-Aenderung mitziehen (z.B. People-Graph-Merge: old_id wird in
    new_id gefaltet). Aktualisiert messages.person_id + messages.speaker (human-
    Zeilen tragen person_id als speaker) + guest_sessions.person_id. person_name
    bleibt Snapshot, ausser new_name ist explizit gesetzt. Liefert betroffene
    Zeilenzahl (Best-Effort)."""
    conn = _get_conn()
    if conn is None or not old_id or not new_id or old_id == new_id:
        return 0
    try:
        with _LOCK:
            c1 = conn.execute("UPDATE messages SET person_id=? WHERE person_id=?",
                              (new_id, old_id)).rowcount
            conn.execute("UPDATE messages SET speaker=? WHERE speaker=?", (new_id, old_id))
            conn.execute("UPDATE guest_sessions SET person_id=? WHERE person_id=?",
                         (new_id, old_id))
            if new_name:
                conn.execute("UPDATE messages SET person_name=? WHERE person_id=?",
                             (new_name, new_id))
                conn.execute("UPDATE guest_sessions SET person_name=? WHERE person_id=?",
                             (new_name, new_id))
        return int(c1 or 0)
    except Exception as e:
        print(f"  [yuki_guest_db.reassign_identity fehlgeschlagen: {e}]")
        return 0
