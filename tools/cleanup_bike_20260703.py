# -*- coding: utf-8 -*-
"""Heimtrainer-Farbe korrigieren (2026-07-03). Beide Farb-Fakten (schwarz/weiss)
sind Kamera-Konfabulation - real: silberner Rahmen, schwarzer Sitz+Griffe."""
import json, shutil, time
from pathlib import Path

MEM = Path(__file__).resolve().parent.parent / "memory"
FACTS = MEM / "yuki_facts.json"
BAK = MEM / f"_cleanup_bak_bike_{time.strftime('%Y%m%d_%H%M%S')}"
BAK.mkdir(exist_ok=True)
shutil.copy2(FACTS, BAK / FACTS.name)

def norm(s): return " ".join((s or "").lower().split())

KILL = {
    ("michael's room", "black exercise bike with mat"),
    ("michael's room", "contains white exercise bike"),
}
data = json.loads(FACTS.read_text(encoding="utf-8"))
facts = data["facts"]
kept, removed, fixed = [], [], []
for f in facts:
    sub, txt = norm(f.get("subject", "")), norm(f.get("text", ""))
    if (sub, txt) in KILL:
        removed.append(f"{f['subject']}: {f['text']}")
        continue
    if sub == "black exercise bike":               # falsche Farbe im Subject
        f["subject"] = "exercise bike"
        fixed.append(f"subject 'black exercise bike' -> 'exercise bike' (text: {f['text']})")
    kept.append(f)

kept.append({
    "text": "has a silver frame with black seat and handgrips",
    "subject": "exercise bike",
    "added": time.strftime("%Y-%m-%d"),
    "recall_count": 0,
    "last_recalled_ts": None,
})
data["facts"] = kept
data["updated"] = time.strftime("%Y-%m-%d %H:%M")
FACTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"Backup -> {BAK}")
print(f"Entfernt ({len(removed)}):")
for r in removed: print("  -", r)
print("Korrigiert:")
for r in fixed: print("  ~", r)
print("  + exercise bike: has a silver frame with black seat and handgrips")
print(f"Facts jetzt: {len(kept)}")
