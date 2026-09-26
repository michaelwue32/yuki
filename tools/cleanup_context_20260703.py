# -*- coding: utf-8 -*-
"""Einmaliger Kontext-Aufraeumer (2026-07-03).

Entfernt live-widersprochene/stale Fakten, RSS-Feed-Muell aus den Episodes,
den Meta-Leak 'Claude' aus dem People-Graph, eine Alias-Kollision und einen
Tippfehler; korrigiert die Prosa-Wohnsituation. Legt vorher Backups an und
protokolliert jede Aenderung. Reversibel ueber den Backup-Ordner.

Nur Datenhygiene - keine Struktur/Code-Aenderung. Biografische Widersprueche
(Kaffee/Bier/Fahrrad-Farbe) werden BEWUSST NICHT angefasst - die entscheidet
Michael, weil nur er die Wahrheit kennt.
"""
import json, shutil, time, sys
from pathlib import Path

MEM = Path(__file__).resolve().parent.parent / "memory"
FACTS = MEM / "yuki_facts.json"
PEOPLE = MEM / "yuki_people.json"
EPISODES = MEM / "yuki_episodes.json"
MEMORY = MEM / "yuki_memory.json"

ts = time.strftime("%Y%m%d_%H%M%S")
BAK = MEM / f"_cleanup_bak_{ts}"
BAK.mkdir(exist_ok=True)

def load(p):
    return json.loads(p.read_text(encoding="utf-8"))

def save(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def norm(s):
    return " ".join((s or "").lower().split())

report = []

# --- Backups -------------------------------------------------------------
for p in (FACTS, PEOPLE, EPISODES, MEMORY):
    shutil.copy2(p, BAK / p.name)
report.append(f"Backups -> {BAK}")

# --- B) Facts: nur eindeutig stale/live-widersprochene raus --------------
# (subject, text) exakt (normalisiert). Nur Umwelt-/Geraete-Leichen.
FACT_KILL = {
    ("weather", "peak heatwave occurring"),
    ("weather", "storm expected tomorrow"),
    ("air conditioner", "must be moved during storms"),
    ("hallway", "temperature 28.6 degrees"),
    ("second camera", "has six saved positions"),
}
fdata = load(FACTS)
facts = fdata.get("facts", [])
kept, removed = [], []
for f in facts:
    key = (norm(f.get("subject", "")), norm(f.get("text", "")))
    if key in FACT_KILL:
        removed.append(f"{f.get('subject','')}: {f.get('text','')}")
    else:
        kept.append(f)
# Klarstellender Ground-Truth-Fakt gegen die Wohnsituations-Konfabulation
kept.append({
    "text": "lives in his own household, separate from his mother and sister",
    "subject": "Michael",
    "added": time.strftime("%Y-%m-%d"),
    "recall_count": 0,
    "last_recalled_ts": None,
})
fdata["facts"] = kept
fdata["updated"] = time.strftime("%Y-%m-%d %H:%M")
save(FACTS, fdata)
report.append(f"\nFACTS: {len(removed)} entfernt, 1 Klarstellungs-Fakt ergaenzt, {len(kept)} verbleiben")
for r in removed:
    report.append(f"  - {r}")
report.append("  + Michael: lives in his own household, separate from his mother and sister")

# --- A) People: Claude-Leak, Robi-Kollision, Amanada-Tippfehler ----------
pdata = load(PEOPLE)
people = pdata.get("people", [])
before = len(people)
people = [p for p in people if p.get("id") != "claude"]
report.append(f"\nPEOPLE:")
report.append(f"  - Person 'claude' geloescht ({before - len(people)} Eintrag)")
for p in people:
    if p.get("id") == "robin":
        if "Robi" in p.get("aliases", []):
            p["aliases"] = [a for a in p["aliases"] if a != "Robi"]
            report.append("  - Alias 'Robi' von Robin entfernt (Kollision mit Saugroboter)")
    if p.get("id") == "amanada":
        p["name"] = "Amanda"
        report.append("  - 'Amanada' -> 'Amanda' korrigiert")
pdata["people"] = people
pdata["updated"] = time.strftime("%Y-%m-%d %H:%M")
save(PEOPLE, pdata)

# --- C) Episodes: RSS-Feed-Digests + Fable-5/Trump-Muell raus ------------
edata = load(EPISODES)
# Struktur robust ermitteln (Liste oder {"episodes":[...]})
if isinstance(edata, dict):
    eps = edata.get("episodes", edata.get("memos", []))
    ekey = "episodes" if "episodes" in edata else ("memos" if "memos" in edata else None)
else:
    eps, ekey = edata, None

def is_junk(text):
    t = text or ""
    if t.startswith("Aus Michaels Feeds vorgemerkt"):
        return True
    low = t.lower()
    if "fable 5" in low and ("trump" in low or "gesperrte ki" in low):
        return True
    return False

ekept, eremoved = [], 0
for e in eps:
    txt = e.get("text", "") if isinstance(e, dict) else str(e)
    if is_junk(txt):
        eremoved += 1
    else:
        ekept.append(e)
if ekey:
    edata[ekey] = ekept
    edata["updated"] = time.strftime("%Y-%m-%d %H:%M")
    save(EPISODES, edata)
else:
    save(EPISODES, ekept)
report.append(f"\nEPISODES: {eremoved} RSS-/Muell-Memos entfernt, {len(ekept)} verbleiben")

# --- D) Prosa-Wohnsituation korrigieren ----------------------------------
mdata = load(MEMORY)
summ = mdata.get("summary", "")
old = "He works in IT managing GitLab servers and lives with his mother and sister, Momo."
new = "He works in IT managing GitLab servers and lives in his own household, separate from his mother and his sister Momo."
if old in summ:
    mdata["summary"] = summ.replace(old, new)
    save(MEMORY, mdata)
    report.append("\nMEMORY: Wohnsituation korrigiert (getrennt lebend)")
else:
    report.append("\nMEMORY: Ziel-Satz nicht wortgenau gefunden - bitte manuell pruefen")

print("\n".join(report))
