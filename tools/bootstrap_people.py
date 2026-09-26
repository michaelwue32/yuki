"""
bootstrap_people.py - yuki_people.json einmalig aus facts+episodes seeden.
========================================================================
Scant yuki_facts.json + yuki_episodes.json, schlaegt Personen-Kandidaten vor,
laesst dich interaktiv akzeptieren/aliasen/relationshippen. Schreibt am Ende
nach memory/yuki_people.json (mit Backup wenn schon was da ist).

Heuristiken fuer Kandidaten:
  1) Facts mit Pattern "has X named Y" / "X nickname is Y" / "(sister|brother|...) Y"
     -> Y ist STARKER Kandidat samt vorgeschlagener relationship aus Kontext
  2) Episodes: Capitalized-Tokens die >=2x vorkommen, gefiltert gegen eine
     Liste typischer False-Positives (Locations, Brands, Game-Charaktere).
  3) Pro Kandidat werden alle Belegstellen aus facts+episodes gezeigt (max 5).

Per Kandidat hast du 4 Wege:
  [Y] akzeptieren -> es folgen Promps fuer Aliases, relationship, of
  [N] verwerfen (False Positive)
  [S] skippen (spaeter ueber Auto-Gate)
  [Q] Abbruch (alle bisherigen Akzeptierten landen trotzdem in der Datei)

Aufruf:  .\\.venv\\Scripts\\python.exe tools\\bootstrap_people.py
"""
import sys
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yuki_core as yc


# ---------------------------------------------------------------------------
# Filter-Listen gegen offensichtliche False-Positives. Lieber zu konservativ
# als zu generisch - wenn Yuki spaeter eine echte Person mit dem Namen kennt,
# kann sie sich via Gate trotzdem rueberbauen.
# ---------------------------------------------------------------------------
_OBVIOUS_NON_PERSONS = {
    # Eigennamen / Marken / Software / Hardware / Spiele / Geographie
    "michael", "yuki", "yourname", "michi",                  # die zwei + Kosenamen kommen NICHT in den Graph
    "claude", "anthropic", "openai", "chatgpt", "google",
    "vroid", "vrm", "vrma", "mixamo", "blender", "unity",
    "bambulab", "nintendo", "snes", "twitch", "youtube",
    "raid", "nas", "linux", "windows", "android",
    "kyoto", "tokio", "tokyo", "japan", "deutschland", "germany",
    "kamogawa", "kamo", "kyoto", "shanghai", "sapporo", "napoli", "fujiyama",
    "kiyomizu", "gion", "wutachschlucht", "philosophenweg", "brezelberg",
    "Musterstadt", "bayern", "rewe", "europa",
    # Stoffe / Konzepte / Essen
    "matcha", "hojicha", "onigiri", "gyudon", "miso", "mirin", "nori",
    "tatami", "tofu", "yuzu", "sake", "ramen", "sushi", "udon", "soba",
    "fall", "spring", "winter", "summer", "regen", "sommer",
    # Game-Charaktere / Anime
    "tifa", "ryu", "chun", "hung", "ken", "guile", "akuma", "blanka",
    "tron", "bowser", "mario", "pikachu", "sonic", "geralt", "kratos",
    "stellar", "blade", "tomb", "raider", "lara", "octopath", "persona",
    # Streamer/Channels
    "gronkh", "tobinator", "phunkroyal", "geoff", "keighley",
    # Filme/Serien (Eigennamen)
    "fable", "wing", "commander", "lemmings", "forza", "horizon",
    "fighter", "street", "victory", "guild", "wars",
    "shame", "metal", "gear", "solid",
    "jackie", "sammo",
    # Zeitliche / sonstige Worte die capitalized in Episodes vorkommen
    "morgen", "abend", "nacht", "tag", "tage", "tagen", "stunde", "stunden",
    "uhr", "jahr", "jahren", "wochen", "wochenende", "samstag", "freitag",
    "ich", "der", "die", "das", "ein", "und", "oder", "aber", "doch",
    "meine", "deine", "seine", "ihre", "unsere",
    "japanisch", "japanischlernen", "deutsch", "deutscher",
}

# Beziehungs-Begriffe die in Fact-Texten als Trigger funktionieren.
_RELATIONSHIP_TRIGGERS = {
    "sister": "sister",  "schwester": "sister",  "sis": "sister",
    "brother": "brother","bruder": "brother",
    "mother": "mother",  "mutter": "mother",  "mom": "mother", "mama": "mother", "mum": "mother",
    "father": "father",  "vater": "father",  "dad": "father", "papa": "father",
    "wife": "wife",      "frau": "wife",
    "husband": "husband","mann": "husband",
    "partner": "partner",
    "friend": "friend",  "freund": "friend",  "freundin": "friend",
    "kollege": "colleague","colleague": "colleague","kollegin": "colleague",
    "nephew": "nephew",  "neffe": "nephew",
    "niece": "niece",    "nichte": "niece",
    "uncle": "uncle",    "onkel": "uncle",
    "aunt": "aunt",      "tante": "aunt",
    "cousin": "cousin",  "cousine": "cousin",
    "katze": "cat",      "cat": "cat",
    "hund": "dog",       "dog": "dog",
}


def _looks_like_person_token(tok):
    """Sehr grobes Vorfilter: capitalized, >=3 Zeichen, kein Komplettzahl."""
    if not tok or len(tok) < 3:
        return False
    if tok.lower() in _OBVIOUS_NON_PERSONS:
        return False
    if not tok[0].isupper():
        return False
    if tok.isupper() and len(tok) <= 4:  # Akronyme rausfischen
        return False
    return True


def _scan_facts(facts):
    """Aus Facts Personen rausziehen. Suche nach Pattern 'named X' / 'nickname is X'
    sowie Erwaehnungen von Relationship-Triggern. Liefert dict: name -> dict mit
    {relationship?, evidence: [strings]}."""
    candidates = defaultdict(lambda: {"relationship": "", "evidence": []})
    # Pattern A: "has <relation> named X"  ->  X = Name
    pat_named = re.compile(
        r"(?:has|hat)\s+(\w+)\s+named\s+([A-Z][a-zA-Z]+)", re.IGNORECASE)
    # Pattern B: "<relation> nickname is X"  ->  X = Spitzname, relation klar
    pat_nick = re.compile(
        r"(\w+)\s+nickname\s+is\s+([A-Z][a-zA-Z]+)", re.IGNORECASE)
    # Pattern C: irgendeine Capitalized-Phrase die nahe einem Trigger steht.
    for f in facts:
        text = f.get("text") or ""
        if not text:
            continue
        m = pat_named.search(text)
        if m:
            rel_raw = m.group(1).lower()
            name = m.group(2)
            rel = _RELATIONSHIP_TRIGGERS.get(rel_raw, rel_raw)
            if _looks_like_person_token(name):
                entry = candidates[name]
                entry["relationship"] = entry["relationship"] or rel
                entry["evidence"].append(f"[fact] {text}")
        m = pat_nick.search(text)
        if m:
            rel_raw = m.group(1).lower()
            name = m.group(2)
            rel = _RELATIONSHIP_TRIGGERS.get(rel_raw, rel_raw)
            if _looks_like_person_token(name):
                entry = candidates[name]
                entry["relationship"] = entry["relationship"] or rel
                entry["evidence"].append(f"[fact, nickname] {text}")
        # Pattern C: jedes capitalized Token in Fact-Text, das nahe einem
        # Relationship-Trigger steht
        toks = re.findall(r"[A-Za-zÄÖÜäöüß]+", text)
        for i, tok in enumerate(toks):
            if not _looks_like_person_token(tok):
                continue
            window = [t.lower() for t in toks[max(0, i-3):i+4]]
            for trig, rel in _RELATIONSHIP_TRIGGERS.items():
                if trig in window and trig != tok.lower():
                    entry = candidates[tok]
                    entry["relationship"] = entry["relationship"] or rel
                    if f"[fact] {text}" not in entry["evidence"]:
                        entry["evidence"].append(f"[fact] {text}")
                    break
    return candidates


def _scan_episodes(episodes, min_count=2):
    """Aus Episodes capitalized Tokens >= min_count zaehlen. Liefert dict: name ->
    dict mit {relationship?, evidence}. Relationship wird erkannt wenn ein Trigger
    in der gleichen Episode vorkommt."""
    counts = Counter()
    contexts = defaultdict(list)
    rels = {}
    for e in episodes:
        text = e.get("text") or ""
        if not text:
            continue
        toks = re.findall(r"[A-Za-zÄÖÜäöüß]+", text)
        lowers = [t.lower() for t in toks]
        present_rels = [(t, _RELATIONSHIP_TRIGGERS[t])
                        for t in lowers if t in _RELATIONSHIP_TRIGGERS]
        for tok in toks:
            if not _looks_like_person_token(tok):
                continue
            counts[tok] += 1
            if len(contexts[tok]) < 5:
                date = e.get("date", "?")
                contexts[tok].append(f"[ep {date}] {text}")
            if present_rels and tok not in rels:
                rels[tok] = present_rels[0][1]
    candidates = {}
    for tok, c in counts.items():
        if c < min_count:
            continue
        candidates[tok] = {
            "count": c,
            "relationship": rels.get(tok, ""),
            "evidence": contexts[tok],
        }
    return candidates


def _merge_candidates(from_facts, from_episodes):
    """Beide Quellen zusammen, Facts haben Vorrang bei relationship-Konflikt."""
    merged = {}
    for name, data in from_facts.items():
        merged[name] = {
            "relationship": data.get("relationship", ""),
            "evidence":     list(data.get("evidence", [])),
            "from_facts":   True,
            "count":        len(data.get("evidence", [])),
        }
    for name, data in from_episodes.items():
        if name in merged:
            entry = merged[name]
            if not entry["relationship"]:
                entry["relationship"] = data.get("relationship", "")
            for ev in data.get("evidence", []):
                if ev not in entry["evidence"]:
                    entry["evidence"].append(ev)
            entry["count"] += data.get("count", 0)
        else:
            merged[name] = {
                "relationship": data.get("relationship", ""),
                "evidence":     list(data.get("evidence", [])),
                "from_facts":   False,
                "count":        data.get("count", 0),
            }
    # Score: Facts-Funde stark gewichten, Episoden-Counts addieren.
    for name, entry in merged.items():
        entry["score"] = (10 if entry["from_facts"] else 0) + entry["count"]
    # Absteigend sortiert nach Score
    return sorted(merged.items(), key=lambda kv: kv[1]["score"], reverse=True)


def _prompt_str(q, default=""):
    try:
        v = input(f"{q} [{default}]: ").strip()
    except EOFError:
        return default
    return v or default


def _prompt_choice(q, choices, default):
    while True:
        try:
            v = input(f"{q} [{'/'.join(choices)}] (default {default}): ").strip().lower()
        except EOFError:
            return default
        if not v:
            return default
        if v in choices:
            return v
        print(f"  bitte eine der Optionen: {choices}")


def main():
    print("=== Bootstrap People Graph aus facts + episodes ===\n")
    facts = yc.load_facts()
    episodes = yc.load_episodes()
    print(f"Facts geladen: {len(facts)}")
    print(f"Episodes geladen: {len(episodes)}")
    if not facts and not episodes:
        print("Nichts zu scannen."); return

    fact_cand = _scan_facts(facts)
    epi_cand = _scan_episodes(episodes, min_count=2)
    print(f"Kandidaten aus Facts: {len(fact_cand)}")
    print(f"Kandidaten aus Episodes: {len(epi_cand)} (>=2 Erwaehnungen)\n")

    merged = _merge_candidates(fact_cand, epi_cand)
    if not merged:
        print("Keine Personen-Kandidaten gefunden."); return

    print(f"Zu pruefen: {len(merged)} Kandidaten (sortiert nach Score)\n")
    print("Tasten pro Kandidat:  Y = akzeptieren  |  N = verwerfen  |  S = skip  |  Q = Abbruch+schreiben\n")

    accepted = []
    for i, (name, data) in enumerate(merged, 1):
        rel_hint = f"  [Hinweis: relationship -> {data['relationship']}]" if data["relationship"] else ""
        print(f"\n--- {i}/{len(merged)} :: '{name}'  (score={data['score']}, evidence={len(data['evidence'])}){rel_hint}")
        for ev in data["evidence"][:5]:
            print(f"    {ev[:140]}")
        if len(data["evidence"]) > 5:
            print(f"    ... ({len(data['evidence']) - 5} weitere)")
        choice = _prompt_choice("  ", ["y", "n", "s", "q"], "s")
        if choice == "q":
            print("Abbruch - schreibe bisher Akzeptierte ..."); break
        if choice == "n":
            continue
        if choice == "s":
            continue
        # Y -> Details abfragen
        canonical_name = _prompt_str("    canonical Name (Display)", name)
        aliases_raw = _prompt_str("    Aliases (komma-getrennt, leer = keine)", "")
        aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
        relationship = _prompt_str("    Relationship (sister/brother/friend/...)",
                                   data["relationship"])
        of_who = _prompt_str("    of (default Michael)", "Michael")
        bricks_raw = _prompt_str("    Bricks (kurze Stichpunkte, getrennt mit ';;', leer=keine)", "")
        bricks = [b.strip() for b in bricks_raw.split(";;") if b.strip()]
        # Wenn keine Bricks angegeben - aus der ersten Evidence-Zeile vorschlagen?
        if not bricks and data["evidence"]:
            print(f"    (Tipp: spaetere Bricks koennen via Gate automatisch reinwachsen)")
        accepted.append({
            "name": canonical_name,
            "aliases": aliases,
            "relationship": relationship,
            "of": of_who,
            "bricks": bricks,
        })
        print(f"    OK: {canonical_name}" + (f" + {len(bricks)} bricks" if bricks else ""))

    if not accepted:
        print("\nKeine Person akzeptiert, nichts zu schreiben."); return

    # Backup wenn schon eine Datei existiert
    if yc.PEOPLE_FILE.exists():
        bak = str(yc.PEOPLE_FILE) + ".bootstrap.bak"
        shutil.copy(yc.PEOPLE_FILE, bak)
        print(f"\nBackup -> {bak}")

    added_persons, added_bricks = yc.append_people_entries(accepted)
    print(f"\nGeschrieben: {added_persons} Personen, {added_bricks} Bricks")
    print(f"Datei: {yc.PEOPLE_FILE}")


if __name__ == "__main__":
    main()
