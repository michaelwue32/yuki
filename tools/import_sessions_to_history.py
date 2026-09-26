"""
Einmaliges Migrationsskript: archive/sessions/*.json -> memory/yuki_history.sqlite

Hintergrund: Die alten Session-Archive haben keine pro-Message-Zeitstempel,
nur den Dateinamen (session_YYYYMMDD_HHMMSS.json) als Sitzungs-Start. Wir
nutzen den als Anker und verteilen die Messages linear in 1-Sekunden-Schritten
ab Start -> jede Message hat eine eindeutige ts, Reihenfolge bleibt erhalten,
und Tag-Granularitaet (was das Habit-Gate braucht) ist verlustfrei.

Idempotent: pro Quell-Datei eine session_id 's_archive_<basename>'. Beim
Re-Import wird der Bucket vorher geleert (DELETE WHERE session_id=...).

Overlap-Dedupe: yuki_core sichert bei der 30-Turn-Verdichtung das volle
Snapshot und kuerzt conversation.json danach auf die letzten HISTORY_KEEP_LAST
Turns -> aufeinanderfolgende Archive-Files teilen ~10 redundante Lead-Turns.
Beim sequenziellen Import erkennen wir das pro Datei: passen die ersten K
Messages exakt zu den letzten K der direkt vorigen Datei, werden sie
uebersprungen. (Echte Wiederholungen wie feste Spontan-Prompts ueber mehrere
Tage bleiben drin, weil sie nicht als zusammenhaengender Lead-in einer
direkt anschliessenden Datei auftreten.)

Aufruf:
  .venv\\Scripts\\python.exe tools\\import_sessions_to_history.py
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

# Pfad-Setup, damit das Script auch ohne -m laeuft
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import yuki_history_db  # noqa: E402


_SESSIONS_DIR = _ROOT / "archive" / "sessions"
_CONVERSATION_FILE = _ROOT / "memory" / "conversation.json"
_FNAME_RE = re.compile(r"^session_(\d{8})_(\d{6})\.json$")


def _parse_session_start(fname: str) -> datetime.datetime | None:
    """'session_20260526_201302.json' -> datetime(2026,5,26,20,13,2). None bei Mist."""
    m = _FNAME_RE.match(fname)
    if not m:
        return None
    try:
        date_part, time_part = m.groups()
        return datetime.datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _role_to_speaker(role: str) -> str:
    if role == "user":
        return "michael"
    if role == "assistant":
        return "yuki"
    return "system"


_OVERLAP_MAX = 10   # entspricht yuki_core HISTORY_KEEP_LAST


def _file_entries(path: Path) -> list[dict]:
    """Datei laden und auf valide {role, content}-Eintraege normalisieren.
    Liefert leere Liste bei Mist - so kann der Caller einfach skippen."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [SKIP {path.name}: {e}]")
        return []
    if not isinstance(data, list):
        return []
    out = []
    for ent in data:
        if isinstance(ent, dict) and ent.get("role") and ent.get("content") is not None:
            out.append(ent)
    return out


def _overlap_lead(entries: list[dict], prev_tail: list[tuple[str, str]]) -> int:
    """Wie viele Lead-Eintraege der neuen Datei matchen die letzten K der
    vorigen exakt? Liefert das groesste K (<= _OVERLAP_MAX) sodass
    entries[0:K] == prev_tail[-K:] in (role, content)."""
    if not prev_tail or not entries:
        return 0
    max_k = min(_OVERLAP_MAX, len(prev_tail), len(entries))
    best = 0
    for k in range(1, max_k + 1):
        head = [(e["role"], e["content"]) for e in entries[:k]]
        if head == prev_tail[-k:]:
            best = k
    return best


def _import_file(conn, path: Path, prev_tail: list[tuple[str, str]]) -> tuple[int, int, list[tuple[str, str]]]:
    """Eine Session-Datei importieren. Liefert (imported, skipped_overlap, new_tail)."""
    start_dt = _parse_session_start(path.name)
    if start_dt is None:
        return (0, 0, prev_tail)
    entries = _file_entries(path)
    if not entries:
        return (0, 0, prev_tail)

    session_id = "s_archive_" + path.stem  # eindeutig pro Quell-Datei

    # Idempotenz: vorigen Import dieses Buckets entfernen
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))

    # Overlap zur vorigen Datei erkennen und Lead-in skippen
    skip = _overlap_lead(entries, prev_tail)
    to_import = entries[skip:]

    imported = 0
    for i, ent in enumerate(to_import):
        speaker = _role_to_speaker(ent["role"])
        # +i Sekunden ab Sitzungs-Start + Skip-Offset, damit auch nach Overlap-
        # Drop die ts monoton mit der Original-Reihenfolge laufen.
        ts = (start_dt + datetime.timedelta(seconds=skip + i)).isoformat(timespec="microseconds")

        tokens = ent.get("tokens")
        if tokens is not None and not isinstance(tokens, str):
            try:
                tokens = json.dumps(tokens, ensure_ascii=False)
            except Exception:
                tokens = None

        conn.execute(
            "INSERT INTO messages(session_id, ts, speaker, content, persona, mood, tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, ts, speaker, ent["content"], None, None, tokens),
        )
        imported += 1

    new_tail = [(e["role"], e["content"]) for e in entries[-_OVERLAP_MAX:]]
    return (imported, skip, new_tail)


def main() -> int:
    if not _SESSIONS_DIR.exists():
        print(f"Kein Verzeichnis {_SESSIONS_DIR} - nichts zu importieren.")
        return 0
    files = sorted(_SESSIONS_DIR.glob("session_*.json"))
    if not files:
        print(f"Keine session_*.json in {_SESSIONS_DIR}.")
        return 0

    conn = yuki_history_db._get_conn()
    if conn is None:
        print("Verlaufs-DB konnte nicht geoeffnet werden - Abbruch.")
        return 1

    print(f"Importiere {len(files)} Sitzungs-Archive nach {yuki_history_db.DB_PATH.name} ...")
    total_imported = 0
    total_overlap = 0
    prev_tail: list[tuple[str, str]] = []
    for f in files:
        imp, ov, prev_tail = _import_file(conn, f, prev_tail)
        total_imported += imp
        total_overlap += ov
        if imp or ov:
            print(f"  {f.name}: +{imp} Messages"
                  + (f", {ov} Overlap-Lead-in geskippt" if ov else ""))

    print()
    print(f"Fertig. {total_imported} Messages importiert"
          + (f", {total_overlap} Overlap-Eintraege ausgefiltert" if total_overlap else "")
          + f". Gesamt in DB: {yuki_history_db.count_messages()}.")
    print()
    print("Hinweis: archive/sessions/ kann nun geloescht werden (Backup-Strategie liegt beim User).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
