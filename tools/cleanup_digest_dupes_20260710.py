# -*- coding: utf-8 -*-
"""Einmaliger Digest-Aufraeumer (2026-07-10).

Der Steward-Digest (memory/yuki_steward_digest.json) hatte durch den Seen-Churn-
Bug denselben Artikel mehrfach drin (gleiche URL, mehrfach als 'neu' eingekippt).
Root-Cause ist in yuki_core.steward_new_items + add_steward_digest gefixt; dieses
Skript raeumt den BESTANDS-Muell weg: pro Link (sonst Titel) bleibt EIN Eintrag.

Survivor-Wahl je Duplikat-Gruppe:
  - ein gemerkter (saved=True) Eintrag gewinnt (bewusst per Stern behalten),
  - sonst der aelteste (erste) Eintrag,
  - Flags werden konservativ zusammengefuehrt: saved=True wenn IRGENDEINER gemerkt
    war; read bleibt nur True wenn ALLE gelesen waren (ein ungelesener bleibt
    sichtbar - resurfacet aber nichts, was schon weg war).

Default = Dry-Run (nur Vorschau). Mit '--apply' schreiben (Backup davor).
"""
import json, shutil, sys, time
from collections import OrderedDict
from pathlib import Path

MEM = Path(__file__).resolve().parent.parent / "memory"
DIGEST = MEM / "yuki_steward_digest.json"
APPLY = "--apply" in sys.argv


def dedup_key(it):
    link = (it.get("link") or "").strip().lower()
    return link if link else (it.get("title") or "").strip().lower()


data = json.loads(DIGEST.read_text(encoding="utf-8"))
items = data.get("digest", [])

groups = OrderedDict()
for it in items:
    groups.setdefault(dedup_key(it), []).append(it)

kept, dropped = [], []
for key, grp in groups.items():
    if not key or len(grp) == 1:
        kept.append(grp[0])
        continue
    # Survivor: gemerkter gewinnt, sonst der erste (aelteste)
    survivor = next((g for g in grp if g.get("saved")), grp[0])
    merged = dict(survivor)
    merged["saved"] = any(g.get("saved") for g in grp)
    merged["read"] = all(g.get("read") for g in grp)
    kept.append(merged)
    dropped.extend(g for g in grp if g is not survivor)

print(f"Digest: {len(items)} Eintraege -> {len(kept)} nach Dedup "
      f"({len(dropped)} Dubletten entfernt)")
by_title = {}
for d in dropped:
    t = (d.get("title") or d.get("link") or "?")[:70]
    by_title[t] = by_title.get(t, 0) + 1
for t, n in sorted(by_title.items(), key=lambda x: -x[1]):
    print(f"  -{n:>2}x  {t}")

if not APPLY:
    print("\n(Dry-Run - nichts geschrieben. Mit '--apply' anwenden.)")
    sys.exit(0)

ts = time.strftime("%Y%m%d_%H%M%S")
bak = DIGEST.with_suffix(f".bak.{ts}.json")
shutil.copy2(DIGEST, bak)
data["digest"] = kept
data["updated"] = time.strftime("%Y-%m-%d %H:%M")
DIGEST.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nGeschrieben. Backup -> {bak.name}")
