"""
Einmaliger Bootstrap-Lauf: Habits aus der migrierten yuki_history.sqlite ziehen.

Was es macht:
  1. Liest alle Messages aus memory/yuki_history.sqlite chronologisch
  2. Chunked sie in 30-Message-Bloecke (identisch zur Live-Behavior)
  3. Ruft pro Chunk yuki_core.extract_habits(chunk, anchor_date=median_day)
  4. INSERT OR IGNORE in memory/yuki_habits.sqlite (UNIQUE pro Tag faengt Doppel)
  5. Am Ende einmal recompute_summary() -> Concern-Scores + Categories sind frisch

Anker-Datum-Strategie: der Median-Tag der Chunk-Messages. Robust gegen Ausreisser
(ein Spontan-Prompt aus einer anderen Session faellt nicht ins Gewicht). Damit
kriegen die Occurrences plausible Daten OHNE dass das LLM raten muss.

Persona-Strategie: haeufigste Persona im Chunk wird als persona_default
genommen (migrierte Messages haben oft NULL -> bleibt NULL).

Voraussetzungen:
  - Ollama laeuft + Failover-Stack erreichbar (qwen3.6:27b primaer)
  - tools/import_sessions_to_history.py wurde vorher gelaufen

Aufwand: bei ~2700 Messages und 30-Chunks-Granularitaet ~90 LLM-Calls. Bei
2-4s pro Call also ~5-10 Min. Laeuft offline parallel zum Live-Server.

Aufruf:
  .venv\\Scripts\\python.exe tools\\extract_habits_from_history.py
  .venv\\Scripts\\python.exe tools\\extract_habits_from_history.py --sample 5
      (nur die letzten 5 Chunks - schneller Smoke-Test des Gate-Outputs)
  .venv\\Scripts\\python.exe tools\\extract_habits_from_history.py --dry
      (Gate laufen lassen aber NICHT in DB schreiben)
"""
from __future__ import annotations

import datetime
import sys
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import yuki_core as yc       # noqa: E402
import yuki_history_db        # noqa: E402
import yuki_habits_db         # noqa: E402


CHUNK_SIZE = 30


def _row_to_session_msg(row: dict) -> dict:
    speaker = row.get("speaker") or ""
    role = ("user" if speaker == "michael"
            else "assistant" if speaker == "yuki"
            else "system")
    return {"role": role, "content": row.get("content") or ""}


def _median_day(chunk: list[dict]) -> str:
    """Median-Tag der ts-Werte im Chunk als ISO-Datum."""
    days = sorted(
        datetime.date.fromisoformat((r["ts"] or "")[:10])
        for r in chunk
        if r.get("ts")
    )
    if not days:
        return datetime.date.today().isoformat()
    return days[len(days) // 2].isoformat()


def _dominant_persona(chunk: list[dict]) -> str | None:
    personas = [r.get("persona") for r in chunk if r.get("persona")]
    if not personas:
        return None
    return Counter(personas).most_common(1)[0][0]


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=0,
                        help="Nur die letzten N Chunks verarbeiten (Smoke-Test)")
    parser.add_argument("--dry", action="store_true",
                        help="Gate laufen lassen, aber NICHT in DB schreiben")
    args = parser.parse_args()

    rows = yuki_history_db.get_messages()
    if not rows:
        print("yuki_history.sqlite ist leer - nichts zu extrahieren.")
        print("Erst tools/import_sessions_to_history.py laufen lassen.")
        return 1

    total_msgs = len(rows)
    chunks = [rows[i:i + CHUNK_SIZE] for i in range(0, total_msgs, CHUNK_SIZE)]
    if args.sample > 0:
        chunks = chunks[-args.sample:]
        print(f"--sample {args.sample}: nur die letzten {len(chunks)} Chunks (von urspr. {total_msgs//CHUNK_SIZE+1}).")
    if args.dry:
        print("--dry: KEINE DB-Schreibvorgaenge, nur Gate-Output zeigen.")
    print(f"Bootstrap-Habit-Extraktion ueber {total_msgs} Messages in {len(chunks)} Chunks ...")
    print(f"Geschaetzte Dauer: ~{len(chunks) * 3 // 60} bis ~{len(chunks) * 6 // 60} Min")
    print()

    started = time.time()
    total_inserted = 0
    skipped_empty = 0

    for idx, chunk in enumerate(chunks, start=1):
        anchor = _median_day(chunk)
        persona_default = _dominant_persona(chunk)
        session_msgs = [_row_to_session_msg(r) for r in chunk]

        t0 = time.time()
        try:
            occs = yc.extract_habits(session_msgs, today_iso=anchor)
        except Exception as e:
            print(f"  [Chunk {idx}/{len(chunks)} @ {anchor}: Gate-Fehler: {e}]")
            continue
        dt = time.time() - t0

        if not occs:
            skipped_empty += 1
            print(f"  Chunk {idx:>3}/{len(chunks)} @ {anchor}  (none, {dt:.1f}s)")
            continue

        if persona_default:
            for o in occs:
                o.setdefault("persona", persona_default)
        if args.dry:
            print(f"  Chunk {idx:>3}/{len(chunks)} @ {anchor}  DRY: {len(occs)} extracted ({dt:.1f}s)")
            for o in occs:
                print(f"     - {o['habit_key']:<20} / {o['subject']:<7} | {o['date']} | {o.get('context','')}")
            continue
        n = yuki_habits_db.insert_occurrences(occs)
        total_inserted += n
        # Was wurde erkannt - sichtbar fuer User-Sanity
        keys = ", ".join(sorted({f"{o['habit_key']}/{o['subject']}" for o in occs}))
        print(f"  Chunk {idx:>3}/{len(chunks)} @ {anchor}  +{n} occ ({len(occs)} extracted) [{keys}] ({dt:.1f}s)")

    total_dt = time.time() - started
    print()
    print(f"Fertig in {total_dt/60:.1f} Min.")
    if args.dry:
        print("  (DRY-RUN - DB unangetastet)")
        return 0
    print(f"  {total_inserted} Occurrences eingefuegt")
    print(f"  {skipped_empty}/{len(chunks)} Chunks ohne Pattern")
    print()
    print("Recompute Summary ...")
    n_habits = yuki_habits_db.recompute_summary()
    print(f"  -> {n_habits} distinkte Habits aggregiert.")
    print()
    # Top-10-Vorschau
    summary = yuki_habits_db.get_summary()
    if summary:
        print("Top-10 nach concern_score:")
        for row in summary[:10]:
            note = f" - {row['pattern_note']}" if row['pattern_note'] else ""
            print(f"  {row['concern_score']:.2f}  {row['subject']:>7} / {row['habit_key']:<20} "
                  f"  ({row['category']}, count_30d={row['count_30d']}{note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
