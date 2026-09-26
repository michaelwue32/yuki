# -*- coding: utf-8 -*-
"""Backfill deutscher Such-Keywords fuer Facts + Heart (aktiv + Archiv).

Warum: die Erinnerungen sind auf Englisch gespeichert, Michael redet Deutsch ->
der Substring-Recall verfehlt ('Aengste' trifft 'fear' nicht). Loesung (wie bei
den Lebenserinnerungen): pro Eintrag ein deutsches 'keywords'-Feld, gegen das der
Recall zusaetzlich matcht (_entry_kw_hit in yuki_core). Dieses Skript fuellt den
BESTAND nach - es laesst die englischen Saetze unangetastet und ergaenzt nur die
Keywords (via LLM, batch-weise).

Nutzung:
  python tools/backfill_memory_keywords.py --dry-run           # nur zeigen
  python tools/backfill_memory_keywords.py --dry-run --limit 5 # nur 5 (Probe)
  python tools/backfill_memory_keywords.py                     # schreiben (mit Backup)
  python tools/backfill_memory_keywords.py --store facts       # nur ein Store

Idempotent: Eintraege mit bereits gefuelltem 'keywords' werden uebersprungen ->
mehrfach laufbar, fuellt nur Luecken. [[yuki-context-hygiene]]
"""
import argparse, json, re, shutil, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yuki_core as yc

BATCH = 8   # Eintraege pro LLM-Call. Prompt+Parsing leben in yuki_core
            # (_generate_de_keywords_batch) - Single Source of Truth, gleiche Logik
            # wie die Verdichtung (update_keywords_from_stores).


def _needs(entry):
    return not (entry.get("keywords") or [])


def backfill_store(name, entries, limit_left, dry):
    todo = [(i, e) for i, e in enumerate(entries) if _needs(e)]
    if limit_left is not None:
        todo = todo[:limit_left]
    if not todo:
        print(f"  [{name}] nichts zu tun (alle haben Keywords oder leer)")
        return 0
    print(f"  [{name}] {len(todo)} Eintraege ohne Keywords -> generiere (Batch {BATCH}) ...")
    done = 0
    for b in range(0, len(todo), BATCH):
        chunk = todo[b:b + BATCH]
        rows = [(i, e.get("subject", ""), e.get("text", "")) for i, e in chunk]
        try:
            kmap = yc._generate_de_keywords_batch(rows)
        except Exception as ex:
            print(f"    ! Batch {b//BATCH} fehlgeschlagen: {ex}")
            continue
        for i, e in chunk:
            kws = kmap.get(i)
            if not kws:
                continue
            done += 1
            if dry:
                print(f"    [{name} #{i}] {e.get('subject','')}: {e.get('text','')[:50]}")
                print(f"        -> {', '.join(kws)}")
            else:
                e["keywords"] = kws
        print(f"    ... {min(b+BATCH, len(todo))}/{len(todo)}")
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="max Eintraege gesamt (Probe)")
    ap.add_argument("--store", choices=["facts", "heart", "archive", "all"], default="all")
    args = ap.parse_args()

    mem = Path(__file__).resolve().parent.parent / "memory"
    stores = []
    if args.store in ("facts", "all"):
        stores.append(("facts", yc.FACTS_FILE, "facts", yc.load_facts()))
    if args.store in ("heart", "all"):
        stores.append(("heart", yc.HEART_FILE, "heart", yc.load_heart()))
    if args.store in ("archive", "all"):
        stores.append(("heart_archived", yc.HEART_ARCHIVED_FILE, "heart", yc.load_heart_archived()))

    if not args.dry_run:
        bak = mem / f"_kwbackfill_bak_{time.strftime('%Y%m%d_%H%M%S')}"
        bak.mkdir(exist_ok=True)
        for _n, path, _k, _e in stores:
            if Path(path).exists():
                shutil.copy2(path, bak / Path(path).name)
        print(f"Backup -> {bak}")

    limit_left = args.limit
    total = 0
    for name, path, jkey, entries in stores:
        n = backfill_store(name, entries, limit_left, args.dry_run)
        total += n
        if limit_left is not None:
            limit_left = max(0, limit_left - n)
        if not args.dry_run and n:
            yc._atomic_write_text(
                path, json.dumps({jkey: entries, "updated": time.strftime("%Y-%m-%d %H:%M")},
                                 ensure_ascii=False, indent=2))
            print(f"  [{name}] gespeichert ({n} Eintraege ergaenzt)")
    print(f"\n{'(dry-run) ' if args.dry_run else ''}Fertig: {total} Eintraege mit Keywords versorgt.")


if __name__ == "__main__":
    main()
