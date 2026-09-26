"""Adventure-Engine - generische rundenbasierte Spiel-Engine mit Yuki als Sprecherin.

PHASE 1 LIVE (2026-06-07).

ARCHITEKTUR-EINORDNUNG:
- Adventure-Mode ist orthogonal zum normalen Chat-Pfad. Adventure-Turns landen
  AUSSCHLIESSLICH in memory/adventures/<id>.json, NIE in conversation.json -
  dadurch sehen alle Verdichtungs-Gates (facts/episodes/habits/people/decay/
  heart-suggest) die Adventure-Turns nicht. Saubere Memory-Isolation ohne
  Skip-Flags.
- Die `_adventure`-Persona (yuki_core.PERSONAS) ist intern (Unterstrich-Prefix).
  User kann sie nicht manuell waehlen. Server schaltet sie pro Adventure-Turn
  ein via generate_adventure_reply (analog _research/generate_research_reply).
- Slim-System-Builder build_adventure_system_msg() in yuki_core gibt Yuki nur
  Bio + Game-State - kein Heart, kein Facts/Episodes/People-Recall. Bewusste
  Real/Fiction-Wand.

MARKER (alle nur im Adventure-Kontext aktiv):
- [roll:skill|adv/normal/dis]    - PnP-Wuerfel, Code wuerfelt 2d20 high/low oder 1d20
- [adv_state:KEY:VALUE]           - State-Mutation (item_add/hp/loc/...)
- [adv_state:actor:NAME:KEY:VAL]  - Multi-Aktor-Mutation fuer PvP/Co-Op (Phase 4)
- [adv_state:threat:ID:KEY:VAL]   - Threat-Mutation fuer Co-Op (Phase 5, KEY=hp)
- [choice:LABEL|TEXT]             - Frontend-Click-Cards unter Yukis Bubble

PHASE 5 - Co-Op (2026-06-07):
Threats sind die NPC-Gegner-Seite in Co-Op-Manifests. Sie haben keine eigene
Stimme (keine Char-Personas/Move-Pools), sondern simple Stat-Bloecke:
{id, name, hp, max_hp, ac, dmg, description}. Engine wuerfelt fuer sie 1d20 vs
target.AC (Default 12), Schaden nach dmg-Spec. Tot bei hp<=0. Yuki/Michael
greifen Threats mit [move:ID] an, Engine pickt schwaechsten Threat wenn kein
Target im Text steht. Heal-Moves haben kind='heal' im Move-Dict und resolven
ueber resolve_heal_move (target=ally, immer hit). Asymmetrische KO-Mechanik:
Michael KO = sofort user_lost; Yuki KO bleibt offen, Michael kann sie via
Heal-Move wiederbeleben (kein Spieler-Solo-Modus).

State-File-Schema und Manifest-Schema siehe docs/adventure-engine-walkthrough.md.
"""

from __future__ import annotations

import datetime
import json
import os
import random
import re
import tempfile
from pathlib import Path
from typing import Any

# ===========================================================================
# Pfade
# ===========================================================================
_ROOT = Path(__file__).resolve().parent
ADVENTURES_DIR = _ROOT / "memory" / "adventures"
MANIFESTS_DIR = _ROOT / "config" / "adventures"


def _ensure_dirs():
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_text(path, text, encoding="utf-8"):
    """Atomar schreiben (temp-Datei + os.replace). Verhindert, dass ein Crash/
    Kill mitten im Schreiben das Adventure-State-File truncieren laesst - der
    State wird jeden Zug neu geschrieben. Eigene Kopie statt Import aus yuki_core,
    weil yuki_core dieses Modul importiert (sonst zirkulaer). newline=None erhaelt
    die Windows-CRLF-Zeilenenden wie das fruehere Path.write_text."""
    path = os.fspath(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=None) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ===========================================================================
# State-File I/O
# ===========================================================================
def load_adventure(adv_id: str) -> dict:
    """State-File laden. Wirft FileNotFoundError wenn id unbekannt."""
    if not adv_id or "/" in adv_id or "\\" in adv_id or ".." in adv_id:
        raise FileNotFoundError(f"ungueltige adventure id: {adv_id!r}")
    path = ADVENTURES_DIR / f"{adv_id}.json"
    if not path.exists():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def save_adventure(state: dict) -> None:
    """State-File schreiben. state['id'] bestimmt Pfad."""
    adv_id = state.get("id")
    if not adv_id:
        raise ValueError("state.id fehlt")
    _ensure_dirs()
    path = ADVENTURES_DIR / f"{adv_id}.json"
    state["last_touched_at"] = _now_iso()
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def delete_adventure(adv_id: str) -> bool:
    """State-File eines Adventures loeschen (Cleanup-Operation aus dem Modal).
    Die zugehoerige Meta-Episode in yuki_episodes.json bleibt absichtlich
    stehen - das ist Yukis 1-Satz-Erinnerung an die Sitzung, nicht die
    Transkript-Datei. Returns True wenn geloescht, False wenn nicht da.
    """
    if not adv_id:
        return False
    path = ADVENTURES_DIR / f"{adv_id}.json"
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError as e:
        print(f"[adventure delete] {adv_id}: {e}", flush=True)
        return False
    return True


def list_adventures(status: str | None = None) -> list[dict]:
    """Aktive Abenteuer auflisten (fuer Resume-UI in Phase 2). status-Filter
    optional ('active'/'closed')."""
    _ensure_dirs()
    out = []
    for path in sorted(ADVENTURES_DIR.glob("*.json")):
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if status and st.get("status") != status:
            continue
        # Phase 6: mode aus state.state mit-exponieren, damit das Modal-
        # Listing eine Mode-Pille rendern kann ("Story" vs "Kampf").
        inner = st.get("state") or {}
        out.append({"id": st.get("id"), "manifest": st.get("manifest"),
                    "status": st.get("status"),
                    "yuki_role": st.get("yuki_role"),
                    "started_at": st.get("started_at"),
                    "last_touched_at": st.get("last_touched_at"),
                    "turns": len(st.get("turns", [])),
                    "mode": inner.get("mode")})
    return out


def new_adventure_id(manifest_name: str) -> str:
    """Eindeutige ID: YYYYMMDD-<manifest>-NNN. Inkrementiert bei Konflikten."""
    _ensure_dirs()
    day = datetime.date.today().strftime("%Y%m%d")
    safe = re.sub(r"[^a-z0-9_]+", "_", manifest_name.lower()) or "adv"
    n = 1
    while True:
        cand = f"{day}-{safe}-{n:03d}"
        if not (ADVENTURES_DIR / f"{cand}.json").exists():
            return cand
        n += 1


# ===========================================================================
# Manifest-Loader
# ===========================================================================
_MANIFEST_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def load_manifest(name: str) -> dict:
    """Spiel-Manifest aus config/adventures/<name>.json laden + minimal validieren.
    Wirft FileNotFoundError / ValueError bei Problemen."""
    if not name or not _MANIFEST_NAME_RE.match(name):
        raise ValueError(f"ungueltiger manifest-name: {name!r}")
    path = MANIFESTS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError(f"manifest {name}: 'name'-Feld fehlt")
    return data


def list_manifests() -> list[dict]:
    """Verfuegbare Manifests fuer Setup-Wizard (Phase 2).

    SPOILER-SCHUTZ: system_rules, win_hint, initial_engine_msg bleiben absichtlich
    raus - die enthalten Loesungs-Details (z.B. Cozy-Mystery: 'Schluessel liegt im
    Cafe'). Public-Game-Daten wie Move-Pools und Charaktere fuer PvP-Manifests
    (characters) werden 1:1 durchgereicht, weil sie zum Spielen sichtbar sein muessen.
    """
    if not MANIFESTS_DIR.exists():
        return []
    out = []
    for path in sorted(MANIFESTS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entry = {"name": data.get("name", path.stem),
                 "display_name": data.get("display_name", path.stem),
                 "description": data.get("description", ""),
                 "yuki_role_default": data.get("yuki_role_default", "narrator"),
                 "yuki_role_allowed": data.get("yuki_role_allowed", ["narrator"]),
                 "tones_allowed": data.get("tones_allowed", []),
                 # Strukturelle Flags - kein Spoiler, das Frontend braucht sie
                 # um Setup-Optionen sichtbar/unsichtbar zu schalten (z.B.
                 # Peaceful-Toggle nur bei story_hybrid).
                 "dm_llm_enabled": bool(data.get("dm_llm_enabled", False)),
                 "mode_default": data.get("mode_default") or ""}
        # Phase 4: Sparring/Co-Op-Manifests deklarieren characters[] mit Move-Pools.
        # Diese sind oeffentlich (Setup-Wizard zeigt sie zur Char-Wahl).
        if isinstance(data.get("characters"), dict):
            entry["characters"] = data["characters"]
        if isinstance(data.get("starting_hp"), int):
            entry["starting_hp"] = data["starting_hp"]
        if isinstance(data.get("starting_sp"), int):
            entry["starting_sp"] = data["starting_sp"]
        out.append(entry)
    return out


# ===========================================================================
# Marker-Expander
# ===========================================================================
# Pattern analog yuki_core._CALC_MARKER_RE + expand_calc_markers. Roll laeuft VOR
# State-Mutation, sonst sehen State-Marker einen unaufgeloesten Roll-Wert.
_ROLL_MARKER_RE = re.compile(
    r"\[roll:\s*([a-z_]+)\s*\|\s*(adv|normal|dis)\s*\]", re.IGNORECASE)
_STATE_MARKER_RE = re.compile(
    r"\[adv_state:\s*([a-z_]+)\s*:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_CHOICE_MARKER_RE = re.compile(
    r"\[choice:\s*([^\]|]+?)\s*\|\s*([^\]]+?)\s*\]", re.IGNORECASE)
# Phase 4: Move-Marker fuer Sparring/PvP. Zwei Varianten:
# - [move:hayate]                      -> Pool-Pick by ID (case-insensitive)
# - [move:freestyle|Beschreibung]      -> Improvisation, Engine wuerfelt mit Default-Stats
_MOVE_MARKER_RE = re.compile(
    r"\[move:\s*([a-z_]+)\s*(?:\|\s*([^\]]+?))?\s*\]", re.IGNORECASE)
# Phase 6: Encounter-Marker fuer Story-Hybrid-Manifests. Yuki schreibt ihn
# narrativ wenn ein Kampf passt: [encounter:Name|HP|AC|dmg].
# Beispiel: [encounter:Magerer Raeuber|12|11|1d4]
# Name darf Leerzeichen + Umlaute haben, HP+AC sind Integers, dmg ist Damage-Spec.
# Phase 6 Fix 2026-06-07: dmg-Feld OPTIONAL (Yuki vergisst es regelmaessig)-
# Default 1d4. Match scheitert sonst still und der Marker leakt als Text +
# kein Combat triggert.
_ENCOUNTER_MARKER_RE = re.compile(
    r"\[encounter:\s*([^\]|]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)"
    r"(?:\s*\|\s*([^\]]+?))?\s*\]",
    re.IGNORECASE)
_ENCOUNTER_DMG_DEFAULT = "1d4"
# Loose-Match fuer fehlformatierte Encounter-Marker (DM-Halluzination im Kampf).
# Beobachtet 2026-06-07: DM schrieb "[encounter:Name: HP 12/18, 14/14]" um Threat-
# Status anzuzeigen - nutzt KEIN |-Separator, matched die strikte Regex also nicht
# und leakte als Roh-Text. Loose-Variante faengt JEDEN [encounter:...]-Marker
# (egal welches Format) als Cleanup-Pass NACH dem strikten Match. Aufrufer:
# expand_encounter_markers, strip_encounter_markers, strip_world_markers.
_ENCOUNTER_LOOSE_RE = re.compile(r"\[encounter:[^\]]*\]", re.IGNORECASE)
# Damage-Spec: '1d6+2', '2d4', '1d8', oder einfach '5' (fester Damage).
_DAMAGE_SPEC_RE = re.compile(r"^\s*(\d+)d(\d+)\s*(?:([+-])\s*(\d+))?\s*$", re.IGNORECASE)


# Phase 6: Location-Outcome-Hierarchie. Hoehere Stufe ueberschreibt niedrigere
# NIE - "cleared" bleibt cleared auch wenn loc neu betreten wird.
_LOC_OUTCOME_RANK = {None: 0, "empty": 1, "active": 2, "cleared": 3}


# Phase 7 (2026-06-07): Inventar wird zum "Detektiv-Notizbuch". Eintraege sind
# dicts {name, desc, found_at}, item_add-Marker erlauben optionalen |Beschreibung-
# Suffix ([adv_state:item_add:Quittung|Quittung mit 柳-Schriftzeichen]). Alte
# string-Eintraege aus Vor-Phase-7-States werden beim Lesen lazy normalisiert
# (defensiver Read-Pfad in allen Rendern + remove-Lookup).
def _normalize_inventory_entry(entry, now_iso: str | None = None) -> dict:
    """Wandelt einen Inventar-Eintrag (string ODER dict) in das Phase-7-Schema
    {name, desc, found_at}. Backward-compat: nackte strings werden zu
    {name=str, desc='', found_at=None}. now_iso ist optional fuers found_at
    (ISO-Timestamp string), bei None bleibt vorhandener Wert bestehen bzw. None.
    Idempotent - normalisierte dicts gehen unveraendert raus, fehlende Felder
    werden aufgefuellt."""
    if isinstance(entry, dict):
        name = str(entry.get("name") or "").strip()
        desc = str(entry.get("desc") or "")
        found = entry.get("found_at") or now_iso
        return {"name": name, "desc": desc, "found_at": found}
    name = str(entry or "").strip()
    return {"name": name, "desc": "", "found_at": now_iso}


def _inventory_find_index(inv: list, name: str) -> int:
    """Sucht einen Eintrag im Inventar by name (case-insensitive). -1 wenn nicht
    gefunden. Akzeptiert sowohl string- als auch dict-Eintraege (Migration-
    Friendly), damit item_remove auf einem ungemischten State funktioniert."""
    if not isinstance(inv, list) or not name:
        return -1
    needle = name.strip().lower()
    for i, e in enumerate(inv):
        if isinstance(e, dict):
            if str(e.get("name") or "").strip().lower() == needle:
                return i
        else:
            if str(e or "").strip().lower() == needle:
                return i
    return -1


def _touch_location_state(target: dict, loc: str, outcome: str) -> None:
    """Pflegt target['location_states'][loc] idempotent. outcome wird nur
    angehoben (nie downgegradet). target = state['state']-Sub-Dict.
    Aufrufer: loc-Marker (outcome='empty'), encounter-Spawn (outcome='active'),
    combat_cleared (outcome='cleared').
    """
    if not loc:
        return
    key = str(loc).strip()
    if not key:
        return
    ls = target.setdefault("location_states", {})
    cur = ls.get(key) or {}
    cur_outcome = cur.get("outcome")
    if _LOC_OUTCOME_RANK.get(outcome, 0) > _LOC_OUTCOME_RANK.get(cur_outcome, 0):
        cur["outcome"] = outcome
    cur["visited"] = True
    ls[key] = cur


def _do_roll(mode: str, rng=None) -> dict:
    """Wuerfler. mode='adv' -> 2d20 take higher, 'dis' -> take lower, 'normal' -> 1d20.
    Liefert {'value': int, 'rolls': [int, ...], 'mode': str}. rng = optional eigener
    Random (z.B. fuer reproduzierbare Tests)."""
    r = rng or random
    mode = (mode or "normal").lower()
    if mode == "adv":
        rolls = [r.randint(1, 20), r.randint(1, 20)]
        value = max(rolls)
    elif mode == "dis":
        rolls = [r.randint(1, 20), r.randint(1, 20)]
        value = min(rolls)
    else:
        rolls = [r.randint(1, 20)]
        value = rolls[0]
        mode = "normal"
    return {"value": value, "rolls": rolls, "mode": mode}


def expand_roll_markers(text: str, rng=None) -> tuple[str, list[dict]]:
    """Wertet alle [roll:skill|mode]-Marker aus und ENTFERNT sie aus dem Text.
    Liefert (cleaned_text, rolls_list).

    Bis 2026-06-07 wurden Marker durch ein Inline-Platzhalter '(skill 14)'
    ersetzt - User-Feedback war aber, dass das narrativ stoert
    ("eine Schramme am Stein (listen 7)" liest sich clunky). Die Roll-Karte
    unter dem Reply zeigt die Daten ohnehin (Skill + Wert + Mode + Wuerfel),
    der Inline-Text war redundant. Jetzt: nur Strip + Whitespace-Cleanup,
    Daten reisen via rolls_list weiter ans Frontend.
    """
    if not text or "[roll:" not in text.lower():
        return text, []
    rolls: list[dict] = []

    def _repl(m):
        skill = m.group(1).lower()
        mode = m.group(2).lower()
        result = _do_roll(mode, rng)
        rolls.append({"skill": skill, **result})
        return ""

    new_text = _ROLL_MARKER_RE.sub(_repl, text)
    # Doppelte Leerzeichen / Leerzeichen-vor-Punkt aufraeumen, die durch
    # entfernte Marker mitten im Satz entstehen ('Schramme  am Stein .').
    new_text = re.sub(r"\s+([.,!?;:])", r"\1", new_text)
    new_text = re.sub(r"[ \t]{2,}", " ", new_text)
    new_text = re.sub(r"[ \t]+\n", "\n", new_text)
    return new_text.strip(), rolls


def expand_state_markers(text: str, state: dict) -> tuple[str, list[dict]]:
    """Mutiert state in-place anhand [adv_state:KEY:VALUE]-Markern. Liefert
    (cleaned_text, mutations_list).

    Unterstuetzte KEYs:
    - item_add:NAME[|DESC] -> state['inventory'].append({name, desc, found_at}).
                              DESC ist optional (Phase 7 Detektiv-Notizbuch); ohne
                              Pipe wird desc=''. Backward-compat: alte string-Items
                              im State werden bei item_remove via Name-Match gefunden.
    - item_remove:NAME   -> entfernt den Eintrag mit name=NAME (idempotent; case-
                              insensitive Match via _inventory_find_index).
    - hp:DELTA           -> state['hp'] += int(DELTA) (negative Werte erlaubt)
    - loc:VALUE          -> state['location'] = VALUE
    - guesses:DELTA      -> state['guesses'] += int(DELTA)  (fuer zahlen_raten + analoge Spiele)
    - status:VALUE       -> state['status'] = VALUE (z.B. 'closed')
    - actor:NAME:KEY:VAL -> Multi-Aktor-Mutation (Phase 4, Sparring/Co-Op).
                            KEY = hp|sp (Delta), state['actors'][NAME][KEY] += int(VAL).
                            Auto-Clamp hp ans 0 (kein Negativ-HP), sp ans 0.
                            KO-Auto-Close laeuft NICHT hier - siehe check_ko_auto_close.

    Unbekannte KEYs: Marker bleibt im Text stehen + wird geloggt - so sieht Yuki
    in past turns dass etwas nicht griff und kann sich beim naechsten Versuch
    selbst korrigieren (analog expand_calc_markers)."""
    if not text or "[adv_state:" not in text.lower():
        return text, []
    s = state.setdefault("state", {}) if "state" in state else state
    # Adventures haben 'state' als sub-dict (siehe Schema). Wenn ein nacktes
    # state-dict reinkommt (z.B. Test), arbeiten wir direkt drauf.
    target = state.get("state") if isinstance(state.get("state"), dict) else state
    mutations: list[dict] = []

    def _repl(m):
        key = m.group(1).lower()
        val_raw = m.group(2).strip()
        try:
            if key == "item_add":
                # Phase 7: optionaler |Beschreibung-Suffix. Pipe trennt name+desc;
                # weitere Pipes bleiben in der Beschreibung (split=1). Ohne Pipe ->
                # desc=''. mutations enthaelt 'value' (Name fuer Chip-Kompat) UND
                # 'desc' (fuer Frontend-Erweiterung im Inventar-Panel).
                if "|" in val_raw:
                    name, desc = val_raw.split("|", 1)
                    name = name.strip()
                    desc = desc.strip()
                else:
                    name = val_raw.strip()
                    desc = ""
                inv = target.setdefault("inventory", [])
                # Phase 7 Dedup (2026-06-07): DM erwaehnt gefundene Items oft in
                # mehreren Bubbles ("die Quittung im Schmutz", "wir haben die
                # Quittung schon"). Ohne Dedup landet "Quittung" doppelt im
                # Notizbuch + zweite mut-chip-Reihe. Name-Match case-insensitive,
                # silent skip - Marker im Text entfernen (return ""), aber KEINE
                # neue mutation (sonst doppelte Chip-Anzeige).
                existing_idx = _inventory_find_index(inv, name)
                if existing_idx >= 0:
                    print(f"  [Inventar-Dedup: '{name}' bereits vorhanden, skip]",
                          flush=True)
                    return ""
                entry = _normalize_inventory_entry(
                    {"name": name, "desc": desc},
                    now_iso=datetime.datetime.now(
                        datetime.timezone.utc).isoformat(timespec="seconds"))
                inv.append(entry)
                mutations.append({"key": key, "value": name, "desc": desc})
                return ""
            if key == "item_remove":
                inv = target.setdefault("inventory", [])
                idx = _inventory_find_index(inv, val_raw)
                removed_name = val_raw.strip()
                if idx >= 0:
                    removed = inv.pop(idx)
                    if isinstance(removed, dict) and removed.get("name"):
                        removed_name = str(removed["name"]).strip() or removed_name
                mutations.append({"key": key, "value": removed_name})
                return ""
            if key == "hp":
                delta = int(val_raw)
                cur = target.get("hp")
                if cur is None:
                    cur = 0
                target["hp"] = int(cur) + delta
                mutations.append({"key": key, "delta": delta, "new": target["hp"]})
                return ""
            if key == "loc":
                target["location"] = val_raw
                # Phase 6: Location-Memory pflegen (idempotent, kein Downgrade).
                _touch_location_state(target, val_raw, "empty")
                mutations.append({"key": key, "value": val_raw})
                return ""
            if key == "guesses":
                delta = int(val_raw)
                target["guesses"] = int(target.get("guesses", 0)) + delta
                mutations.append({"key": key, "delta": delta, "new": target["guesses"]})
                return ""
            if key == "status":
                target["status"] = val_raw
                # Status liegt auf TOP-Level laut Schema - bewusst doppelt setzen,
                # damit /adventure/end auch greift wenn Yuki status-Marker schreibt.
                state["status"] = val_raw
                mutations.append({"key": key, "value": val_raw})
                return ""
            if key == "actor":
                # val_raw: 'NAME:KEY:VALUE' (z.B. 'michael:hp:-3'). Doppelpunkte
                # in NAME/KEY sind nicht erlaubt - splitn=3 ergibt (name, sub_key, sub_val).
                parts = [p.strip() for p in val_raw.split(":", 2)]
                if len(parts) != 3:
                    print(f"  [Adventure-Actor-Marker malformed: {val_raw!r}]",
                          flush=True)
                    return m.group(0)
                actor_name, sub_key, sub_val = parts
                actor_name = actor_name.lower()
                sub_key = sub_key.lower()
                actors = target.setdefault("actors", {})
                actor = actors.get(actor_name)
                if not isinstance(actor, dict):
                    # Wir legen keinen Actor neu an - das Manifest deklariert wer mitspielt.
                    # Ein Marker auf einen unbekannten Actor wird geloggt + bleibt stehen.
                    print(f"  [Adventure-Actor unbekannt: {actor_name!r} "
                          f"(bekannt: {list(actors.keys())})]", flush=True)
                    return m.group(0)
                if sub_key in ("hp", "sp"):
                    delta = int(sub_val)
                    cur = int(actor.get(sub_key, 0))
                    new_val = max(0, cur + delta)
                    actor[sub_key] = new_val
                    mutations.append({"key": "actor_" + sub_key,
                                      "actor": actor_name,
                                      "delta": delta, "new": new_val,
                                      "max": actor.get("max_" + sub_key)})
                    return ""
                print(f"  [Adventure-Actor-Key unbekannt: {actor_name}:{sub_key}]",
                      flush=True)
                return m.group(0)
            if key == "threat":
                # Phase 5 Co-Op: 'ID:KEY:VALUE' (z.B. 'rauber_1:hp:-5').
                # Threats leben in state.threats (Liste). Unbekannte ID -> Marker
                # bleibt stehen + Log (Pendant zu actor).
                parts = [p.strip() for p in val_raw.split(":", 2)]
                if len(parts) != 3:
                    print(f"  [Adventure-Threat-Marker malformed: {val_raw!r}]",
                          flush=True)
                    return m.group(0)
                threat_id, sub_key, sub_val = parts
                threat_id = threat_id.lower()
                sub_key = sub_key.lower()
                threats = target.setdefault("threats", [])
                hit = None
                for thr in threats:
                    if isinstance(thr, dict) and (thr.get("id") or "").lower() == threat_id:
                        hit = thr
                        break
                if hit is None:
                    print(f"  [Adventure-Threat unbekannt: {threat_id!r} "
                          f"(bekannt: {[t.get('id') for t in threats if isinstance(t, dict)]})]",
                          flush=True)
                    return m.group(0)
                if sub_key == "hp":
                    delta = int(sub_val)
                    cur = int(hit.get("hp", 0))
                    new_hp = max(0, cur + delta)
                    hit["hp"] = new_hp
                    if new_hp <= 0:
                        hit["alive"] = False
                    mutations.append({"key": "threat_hp",
                                      "threat": threat_id,
                                      "delta": delta, "new": new_hp,
                                      "max": hit.get("max_hp")})
                    return ""
                print(f"  [Adventure-Threat-Key unbekannt: {threat_id}:{sub_key}]",
                      flush=True)
                return m.group(0)
        except (TypeError, ValueError) as e:
            print(f"  [Adventure-State-Marker-Fehler: {key}:{val_raw} -> {e}]",
                  flush=True)
            return m.group(0)
        print(f"  [Adventure-State-Marker unbekannt: {key}:{val_raw}]", flush=True)
        return m.group(0)

    new_text = _STATE_MARKER_RE.sub(_repl, text)
    # Doppel-Leerzeichen einsammeln, die durch Marker mitten im Satz entstehen
    new_text = re.sub(r"[ \t]{2,}", " ", new_text)
    new_text = re.sub(r"[ \t]+\n", "\n", new_text)
    return new_text.strip(), mutations


def expand_encounter_markers(text: str, state: dict) -> tuple[str, list[dict]]:
    """Phase 6: Yuki-driven Encounter-Spawn fuer Story-Hybrid-Manifests.

    Parst alle [encounter:Name|HP|AC|dmg]-Marker. Drei Faelle:
    1. Aktuelle Location ist 'cleared' oder 'active' -> alle Marker STRIPPED
       + geloggt, kein Spawn. Verhindert Re-Spawn an besuchten Kampf-Orten.
    2. state.state.mode ist bereits 'combat' -> alle Marker STRIPPED. Kein
       nested Combat.
    3. Sonst: jeder Marker -> Threat-Dict (id='enc_<n>', name/hp/ac/dmg,
       max_hp=hp, alive=True), append zu state.state.threats[]. Beim ersten
       Spawn flippt state.state.mode auf 'combat', aktuelle Location bekommt
       outcome='active', combat_started_at wird gesetzt.

    Aufruf-Reihenfolge in der Pipeline: NACH expand_state_markers (loc-Update
    ist dann schon drin), VOR extract_move_marker. Siehe Stolperfalle.

    Liefert (cleaned_text, mutations). mutations: pro Spawn ein Dict
    {key:'encounter_spawn', name, hp, ac, dmg, id}, oder {key:'encounter_blocked',
     reason: 'cleared'|'active'|'combat_mode', name} wenn gestripped.
    """
    if not text or "[encounter:" not in text.lower():
        return text, []
    target = state.get("state") if isinstance(state.get("state"), dict) else state
    cur_loc = (target.get("location") or "").strip()
    loc_state = (target.get("location_states") or {}).get(cur_loc, {}) if cur_loc else {}
    cur_outcome = loc_state.get("outcome")
    cur_mode = target.get("mode") or "story"
    # Blocker-Reason: Peaceful-Mode (User-Toggle, ueberlagert alles), Location
    # bereits ausgekaempft/Kampf laeuft, oder schon combat-Mode.
    block_reason = None
    if state.get("peaceful_mode"):
        block_reason = "peaceful"
    elif cur_outcome in ("cleared", "active"):
        block_reason = cur_outcome
    elif cur_mode == "combat":
        block_reason = "combat_mode"

    mutations: list[dict] = []
    threats = target.setdefault("threats", [])
    # Eindeutige Encounter-ID pro Spawn - laufender Counter ueber bestehende
    # enc_N + spawn-Position. Verhindert ID-Kollisionen bei Mehrfach-Markern
    # in einem Reply.
    existing_ids = {t.get("id") for t in threats if isinstance(t, dict)}
    counter = 1
    spawn_started = False  # erst beim ersten erfolgreichen Spawn Mode flippen

    def _next_id():
        nonlocal counter
        while True:
            candidate = f"enc_{counter}"
            counter += 1
            if candidate not in existing_ids:
                existing_ids.add(candidate)
                return candidate

    def _repl(m):
        nonlocal spawn_started
        name = m.group(1).strip()
        try:
            hp = int(m.group(2))
            ac = int(m.group(3))
        except (TypeError, ValueError):
            print(f"  [Encounter-Marker malformed: {m.group(0)!r}]", flush=True)
            return ""
        # Phase 6 Fix: dmg-Feld optional, Default 1d4. Yuki vergisst es
        # regelmaessig, ohne Fallback wuerde Marker still scheitern.
        dmg_raw = m.group(4)
        dmg = dmg_raw.strip() if dmg_raw else _ENCOUNTER_DMG_DEFAULT
        if block_reason:
            print(f"  [Encounter geblockt ({block_reason}): {name!r} @ loc={cur_loc!r}]",
                  flush=True)
            mutations.append({"key": "encounter_blocked",
                              "reason": block_reason, "name": name})
            return ""
        tid = _next_id()
        threats.append({
            "id": tid,
            "name": name,
            "hp": hp, "max_hp": hp,
            "ac": ac,
            "dmg": dmg,
            "description": "",
            "alive": True,
        })
        # Beim ALLERERSTEN Spawn pro Marker-Run: Mode + Location-Outcome +
        # combat_started_at setzen.
        if not spawn_started:
            spawn_started = True
            target["mode"] = "combat"
            if cur_loc:
                _touch_location_state(target, cur_loc, "active")
            target.setdefault("combat_started_at", _now_iso())
        mutations.append({"key": "encounter_spawn", "id": tid, "name": name,
                          "hp": hp, "ac": ac, "dmg": dmg})
        return ""

    new_text = _ENCOUNTER_MARKER_RE.sub(_repl, text)
    # Phase 7 (2026-06-07): Loose-Cleanup fuer fehlformatierte Encounter-Marker
    # die das strikte Format nicht treffen ([encounter:Name: HP X/Y] o.ae.).
    # Strikter Match hat sich oben um echte Spawns gekuemmert; was uebrig bleibt
    # ist DM/Yuki-Halluzination + sollte stillschweigend raus statt im Text
    # zu leaken.
    new_text = _ENCOUNTER_LOOSE_RE.sub("", new_text)
    new_text = re.sub(r"[ \t]{2,}", " ", new_text)
    new_text = re.sub(r"[ \t]+\n", "\n", new_text)
    return new_text.strip(), mutations


def strip_encounter_markers(text: str) -> str:
    """Phase 6: nur strippen, kein Spawn. Genutzt im /adventure/start-Endpoint
    (analog extract_move_marker im Opening-Turn) damit Yuki nicht im ersten
    Reply schon einen Combat ausloesen kann - das ist die selbe Klasse von
    Few-Shot-Carryover wie Stolperfalle 8 (Yuki schreibt [move:...] im
    Eroeffnungs-Turn weil Sparring-Few-Shots das Pattern trainieren).
    Phase 7 (2026-06-07): zusaetzlich Loose-Strip fuer fehlformatierte Marker
    (z.B. HP-Anzeige-Halluzinationen wie [encounter:Name: HP 12/18]).
    """
    if not text or "[encounter:" not in text.lower():
        return text
    out = _ENCOUNTER_MARKER_RE.sub("", text)
    out = _ENCOUNTER_LOOSE_RE.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def strip_world_markers(text: str) -> str:
    """Phase 7 (2026-06-07): ALLE Welt-Marker stillschweigend strippen ohne
    State zu mutieren. Genutzt im Dual-LLM-Slim-Yuki-Pfad als hartes Sicherheits-
    netz - Yuki darf laut System-Prompt keine [adv_state:...], [encounter:...],
    [choice:...], [move:...] schreiben, aber Few-Shot-Carryover aus der
    _adventure-Persona kann sie trotzdem leaken. Hier fliegen sie raus ohne
    Welt-Wirkung. roll-Marker BLEIBEN - die werden vorher separat ausgewertet
    und sind auch im Slim-Mode legitim (Yuki darf wuerfeln).
    """
    if not text:
        return text
    out = _STATE_MARKER_RE.sub("", text)
    out = _ENCOUNTER_MARKER_RE.sub("", out)
    # Phase 7 (2026-06-07): Loose-Strip auch fuer Yuki im Slim-Pfad - DM-
    # Halluzinations-Pattern koennte aus Few-Shot-Carryover auch bei Yuki landen.
    out = _ENCOUNTER_LOOSE_RE.sub("", out)
    out = _CHOICE_MARKER_RE.sub("", out)
    out = _MOVE_MARKER_RE.sub("", out)
    out = re.sub(r"\s+([.,!?;:])", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return out.strip()


def extract_choices(text: str) -> tuple[str, list[dict]]:
    """Sammelt alle [choice:LABEL|TEXT]-Marker und entfernt sie aus dem Text.
    Liefert (cleaned_text, [{'label': 'A', 'text': 'Tuer oeffnen'}, ...]).
    Frontend rendert die Liste als Click-Cards unter Yukis Bubble."""
    if not text or "[choice:" not in text.lower():
        return text, []
    choices: list[dict] = []

    def _repl(m):
        label = m.group(1).strip()
        ctext = m.group(2).strip()
        choices.append({"label": label, "text": ctext})
        return ""

    new_text = _CHOICE_MARKER_RE.sub(_repl, text)
    new_text = re.sub(r"[ \t]{2,}", " ", new_text)
    new_text = re.sub(r"[ \t]+\n", "\n", new_text)
    return new_text.strip(), choices


def strip_adventure_markers(text: str) -> str:
    """Sicherheitsnetz fuer den /history-Reload-Pfad (Phase 2). Strippt Reste,
    falls etwas durch die Expander gerutscht ist."""
    if not text:
        return text
    text = _ROLL_MARKER_RE.sub("", text)
    text = _STATE_MARKER_RE.sub("", text)
    text = _CHOICE_MARKER_RE.sub("", text)
    text = _MOVE_MARKER_RE.sub("", text)
    text = _ENCOUNTER_MARKER_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_move_marker(text: str) -> tuple[str, dict | None]:
    """Sucht den ersten [move:ID] oder [move:freestyle|TEXT] Marker (Phase 4).
    Liefert (cleaned_text, move_info_dict | None).

    move_info: {'kind': 'pool', 'id': 'hayate'}
            oder {'kind': 'freestyle', 'description': 'Ich gleite seitwaerts'}

    Mehrere Move-Marker in einer Antwort: nur der ERSTE zaehlt, weitere werden
    als Text-Reste belassen (= durch strip_all_markers spaeter entfernt). Sinn:
    Yuki soll EINEN Move pro Runde wahlen, kein Spam.
    """
    if not text or "[move:" not in text.lower():
        return text, None
    m = _MOVE_MARKER_RE.search(text)
    if not m:
        return text, None
    move_id = m.group(1).strip().lower()
    freetext = (m.group(2) or "").strip()
    if move_id == "freestyle":
        info: dict = {"kind": "freestyle", "description": freetext or "improvisierter Move"}
    else:
        info = {"kind": "pool", "id": move_id}
    # Marker raus aus dem Text (nur den gematchten, nicht alle).
    cleaned = text[:m.start()] + text[m.end():]
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned.strip(), info


def _roll_damage(spec: str, rng=None) -> tuple[int, str]:
    """Wuerfelt einen Damage-Wert nach 'NdM+K' / 'NdM-K' / 'NdM' / festem Int.
    Liefert (value, detail_str) wobei detail_str z.B. '4+3+2 = 9' ist (fuer
    Engine-Bubble-Render). Bei ungueltigem Spec: (1, '1 (default)') als
    Fallback - Engine darf nicht crashen weil ein Manifest schludrig ist.
    """
    r = rng or random
    if spec is None:
        return 1, "1 (default)"
    s = str(spec).strip()
    if not s:
        return 1, "1 (default)"
    # Fester Int?
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        v = int(s)
        return max(0, v), str(v)
    m = _DAMAGE_SPEC_RE.match(s)
    if not m:
        return 1, f"1 (spec invalid: {s!r})"
    n_dice = int(m.group(1))
    die = int(m.group(2))
    sign = m.group(3) or ""
    mod = int(m.group(4)) if m.group(4) else 0
    if sign == "-":
        mod = -mod
    n_dice = max(1, min(n_dice, 10))   # safety
    die = max(2, min(die, 20))
    rolls = [r.randint(1, die) for _ in range(n_dice)]
    total = sum(rolls) + mod
    detail = "+".join(str(x) for x in rolls)
    if mod:
        detail += (f"+{mod}" if mod > 0 else f"{mod}")
    detail += f" = {max(0, total)}"
    return max(0, total), detail


# --- Sparring-Move-Resolver (Phase 4) ----------------------------------------
_FREESTYLE_DEFAULT_MOVE = {"id": "freestyle", "name": "Freestyle",
                            "sp_cost": 1, "damage": "1d4+1", "accuracy": 12,
                            "description": "freie Aktion"}


def _find_move_by_id_or_name(character: dict, key: str) -> dict | None:
    """Findet einen Move in character['moves'] anhand id ODER name (case-insens).
    Hilfreich weil Michaels Freitext oft 'Hadouken' (Name) statt 'hadouken' (ID)
    schreibt - Engine soll beides verstehen."""
    if not isinstance(character, dict):
        return None
    key_norm = (key or "").strip().lower()
    if not key_norm:
        return None
    for mv in character.get("moves") or []:
        if not isinstance(mv, dict):
            continue
        if (mv.get("id", "") or "").lower() == key_norm:
            return mv
        if (mv.get("name", "") or "").lower() == key_norm:
            return mv
    return None


def _parse_user_move(state: dict, manifest: dict,
                      user_content: str) -> dict | None:
    """Parst Michaels Freitext-Eingabe zu einem Move-Dict (oder freestyle-Fallback).

    Strategie:
    - Erst exact-match auf id/name in seinem Char-Pool
    - Dann substring-match (User schreibt 'mach mal Hadouken' -> 'hadouken' im Text)
    - Sonst: freestyle (Engine wuerfelt mit Default-Stats)
    """
    chars = manifest.get("characters") or {}
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    michael = actors.get("michael") or {}
    char = chars.get(michael.get("character"))
    if not isinstance(char, dict):
        return None
    text_low = (user_content or "").lower()
    # Exact match
    hit = _find_move_by_id_or_name(char, text_low)
    if hit:
        return dict(hit)
    # Substring (laengste Treffer zuerst, sonst 'kick' matched 'lightkick')
    moves = sorted(char.get("moves") or [],
                    key=lambda mv: -len((mv.get("name") or "")))
    for mv in moves:
        if not isinstance(mv, dict):
            continue
        for needle in (mv.get("id"), mv.get("name")):
            if needle and needle.lower() in text_low:
                return dict(mv)
    # Freestyle
    fs = dict(_FREESTYLE_DEFAULT_MOVE)
    fs["freestyle_text"] = user_content
    return fs


def move_from_marker_info(state: dict, manifest: dict, actor_id: str,
                            move_info: dict) -> dict | None:
    """Uebersetzt das Ergebnis von extract_move_marker() in ein konkretes
    Move-Dict aus dem Actor-Char-Pool (oder freestyle-Fallback). Liefert None
    wenn der Actor keinen Character oder kein Char-Pool existiert.

    actor_id: 'michael' oder 'yuki'.
    """
    if not isinstance(move_info, dict):
        return None
    chars = manifest.get("characters") or {}
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    actor = actors.get(actor_id) or {}
    char = chars.get(actor.get("character"))
    if move_info.get("kind") == "freestyle":
        fs = dict(_FREESTYLE_DEFAULT_MOVE)
        fs["freestyle_text"] = move_info.get("description") or ""
        return fs
    move_id = (move_info.get("id") or "").lower()
    if not move_id:
        return None
    hit = _find_move_by_id_or_name(char, move_id)
    if hit:
        return dict(hit)
    # Yuki hat einen Move benannt der NICHT im Pool ist - als freestyle abfangen
    # statt "stumm zu schlucken". So sieht der User den Versuch und der Engine-
    # Bubble erklaert was passierte.
    fs = dict(_FREESTYLE_DEFAULT_MOVE)
    fs["freestyle_text"] = f"versuchter Move '{move_id}' (nicht im Pool)"
    return fs


def resolve_sparring_move(state: dict, manifest: dict, attacker: str,
                           move: dict, rng=None) -> tuple[str, dict]:
    """Resolvt EINEN Sparring-Angriff. Mutiert state.actors[*] direkt.

    attacker: 'michael' oder 'yuki' - bestimmt SP-Abzug + Damage-Ziel.
    move: dict mit id/name/sp_cost/damage/accuracy/description (aus char.moves
          oder aus _FREESTYLE_DEFAULT_MOVE). Bei freestyle: enthaelt zusaetzlich
          'freestyle_text' fuer den Engine-Bubble-Text.

    Liefert (engine_text, engine_meta). meta: {event, attacker, move, ...}.
    Engine-Text-Format:
      'Michaels Hadouken -> 1d20=15 vs acc 12: Treffer. Schaden 1d6+1 = 5. (Yuki: HP 25/30)'
      oder bei Miss:
      'Yukis Iai-Nuki -> 1d20=8 vs acc 12: daneben. (Michael: HP 30/30)'
    """
    r = rng or random
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    a_actor = actors.get(attacker) or {}
    defender = "yuki" if attacker == "michael" else "michael"
    d_actor = actors.get(defender) or {}
    if not a_actor or not d_actor:
        return ("(Sparring-Engine-Fehler: actor missing)",
                {"event": "error", "reason": "missing_actor",
                 "attacker": attacker})

    move_name = move.get("name") or move.get("id") or "Move"
    sp_cost = int(move.get("sp_cost") or 0)
    accuracy = int(move.get("accuracy") or 12)
    damage_spec = move.get("damage") or "1d4"
    sp_cur = int(a_actor.get("sp", 0))

    # SP-Pruefung: nicht genug SP -> Engine senkt den Move auf Notfall-Schlag
    # (1d2 dmg, acc 12) und vermerkt das. Sonst koennten Yukis Move-Picks ins
    # Leere laufen wenn sie versehentlich einen teuren Move waehlt.
    if sp_cost > sp_cur:
        original = move_name
        move_name = f"{original} (zu erschoepft)"
        sp_cost = 0
        damage_spec = "1d2"
        accuracy = 12
        downgraded = True
    else:
        downgraded = False

    # SP abziehen, dann ggf. sp_regen-Bonus drauf (Block-Moves, max-clamped).
    new_sp = max(0, sp_cur - sp_cost)
    sp_regen = int(move.get("sp_regen") or 0)
    if sp_regen > 0:
        msp = int(a_actor.get("max_sp", new_sp + sp_regen))
        new_sp = min(msp, new_sp + sp_regen)
    a_actor["sp"] = new_sp

    # Accuracy-Roll: 1d20 >= accuracy = Hit
    roll = r.randint(1, 20)
    crit = roll == 20
    fumble = roll == 1
    hit = (roll >= accuracy) and not fumble
    dmg = 0
    dmg_detail = ""
    if hit:
        dmg, dmg_detail = _roll_damage(damage_spec, rng=r)
        if crit:
            dmg = int(dmg * 1.5)
            dmg_detail += " ×1.5 crit"
    # Defender HP-Mutation
    d_hp_before = int(d_actor.get("hp", 0))
    d_hp_after = max(0, d_hp_before - dmg)
    d_actor["hp"] = d_hp_after

    # Build Engine-Text
    a_display = "Michaels" if attacker == "michael" else "Yukis"
    d_display = "Yuki" if defender == "yuki" else "Michael"
    parts = [f"{a_display} {move_name}: 1d20={roll} vs acc {accuracy}"]
    if fumble:
        parts.append("Patzer (1)")
    elif crit:
        parts.append("kritischer Treffer (20)")
    elif hit:
        parts.append("Treffer")
    else:
        parts.append("daneben")
    if hit:
        parts.append(f"Schaden {damage_spec} = {dmg}")
    if downgraded:
        parts.append("(SP-Reduktion)")
    parts.append(f"-> {d_display}: HP {d_hp_after}/"
                 f"{d_actor.get('max_hp', d_hp_after)}")
    engine_text = " | ".join(parts)

    meta = {
        "event": "sparring_resolve",
        "attacker": attacker,
        "defender": defender,
        "move_id": move.get("id"),
        "move_name": move_name,
        "roll": roll,
        "accuracy": accuracy,
        "hit": hit,
        "crit": crit,
        "fumble": fumble,
        "damage": dmg,
        "damage_spec": damage_spec if hit else None,
        "sp_cost": sp_cost,
        "sp_regen": sp_regen if sp_regen > 0 else 0,
        "sp_left": a_actor["sp"],
        "defender_hp_before": d_hp_before,
        "defender_hp_after": d_hp_after,
    }
    if move.get("freestyle_text"):
        meta["freestyle_text"] = move["freestyle_text"]
    if downgraded:
        meta["downgraded"] = True
    return engine_text, meta


# ===========================================================================
# Turn-Logik
# ===========================================================================
def apply_user_turn(state: dict, user_content: str) -> dict:
    """User-Turn an state.turns anhaengen. last_touched_at setzt save_adventure."""
    user_content = (user_content or "").strip()
    if not user_content:
        return state
    state.setdefault("turns", []).append({"role": "user", "content": user_content})
    return state


def apply_engine_turn(state: dict, engine_content: str,
                      meta: dict | None = None) -> dict:
    """Engine-Turn (System-Stimme) anhaengen. Frontend rendert mittig/weiss.
    meta optional fuer Roll-/Mutation-Spuren die als Tooltip anzeigbar sind."""
    engine_content = (engine_content or "").strip()
    if not engine_content:
        return state
    entry: dict[str, Any] = {"role": "engine", "content": engine_content}
    if meta:
        entry["meta"] = meta
    state.setdefault("turns", []).append(entry)
    return state


def apply_yuki_turn(state: dict, yuki_content: str,
                    rolls: list[dict] | None = None,
                    mutations: list[dict] | None = None,
                    choices: list[dict] | None = None) -> dict:
    """Yuki-Turn anhaengen. yuki_content kommt bereits cleaned (Roll-/State-/
    Choice-Marker schon entfernt). rolls/mutations/choices als Frontend-Meta
    fuer separate Engine-Bubbles / Click-Cards."""
    yuki_content = (yuki_content or "").strip()
    if not yuki_content:
        return state
    entry: dict[str, Any] = {"role": "yuki", "content": yuki_content}
    if rolls:
        entry["rolls"] = rolls
    if mutations:
        entry["mutations"] = mutations
    if choices:
        entry["choices"] = choices
    state.setdefault("turns", []).append(entry)
    return state


def apply_dm_turn(state: dict, dm_content: str,
                  rolls: list[dict] | None = None,
                  mutations: list[dict] | None = None,
                  choices: list[dict] | None = None,
                  meta: dict | None = None) -> dict:
    """Phase 7 (2026-06-07): DM-Turn anhaengen. dm_content kommt bereits cleaned
    (Roll-/State-/Encounter-/Choice-Marker schon expandiert/extrahiert). Frontend
    rendert ihn als eigener .dm-Bubble-Style (oder fallback .engine - bewusst
    keine .yuki). Die DM-Stimme ist die Erzaehler-Stimme der Welt - parallel zu
    Yukis Mitspieler-Stimme.

    rolls/mutations/choices analog apply_yuki_turn: Frontend nutzt sie fuer
    Mutation-Chips + Choice-Cards unter der DM-Bubble.
    meta optional fuer Tags wie {"event": "combat_wrap"} damit der Frontend
    den Combat-Wrap visuell unterscheiden kann.
    """
    dm_content = (dm_content or "").strip()
    if not dm_content:
        return state
    entry: dict[str, Any] = {"role": "dm", "content": dm_content}
    if rolls:
        entry["rolls"] = rolls
    if mutations:
        entry["mutations"] = mutations
    if choices:
        entry["choices"] = choices
    if meta:
        entry["meta"] = meta
    state.setdefault("turns", []).append(entry)
    return state


# ===========================================================================
# Spiel-Initialisierung
# ===========================================================================
def initial_state(manifest: dict, yuki_role: str = "narrator",
                  tone: str = "", setup: dict | None = None,
                  rng=None) -> dict:
    """Baut den Initial-State fuer ein neues Abenteuer aus dem Manifest.

    Manifest-spezifisches Setup (z.B. 'zahlen_raten' -> geheime Zahl ziehen)
    wird hier verdrahtet. Phase 1 hat genau einen Spiel-Typ: zahlen_raten.
    Spaetere Manifests erweitern den Block analog.
    """
    r = rng or random
    name = manifest["name"]
    setup = setup or {}
    today = datetime.date.today().isoformat()
    state: dict[str, Any] = {
        "id": "",                            # wird vom Server gesetzt (new_adventure_id)
        "manifest": name,
        "yuki_role": yuki_role or manifest.get("yuki_role_default", "narrator"),
        "tone": tone or "",
        "started_at": _now_iso(),
        "last_touched_at": _now_iso(),
        "status": "active",
        # User-Toggle (Adventure-Overlay): wenn True, sind Kaempfe komplett aus.
        # Encounter-Marker werden gestrippt + DM-Prompt bekommt explizites Verbot.
        # Sinnvoll nur fuer story_hybrid-Manifests (dm_llm_enabled+mode_default=story);
        # Sparring/Combat-First-Manifests ignorieren das Flag (UI versteckt es).
        "peaceful_mode": bool(setup.get("peaceful_mode", False)),
        "setup": setup,
        "state": {
            "inventory": [],
            "location": None,
            "hp": None,
            "stats": {},
            "yuki_notes": [],
            # Phase 6: Story-Hybrid (Story-Modus + Combat per Yuki-Trigger)
            "mode": (manifest.get("mode_default") or "combat"
                     if (manifest.get("threats") or manifest.get("characters"))
                     else "story"),
            "location_states": {},
        },
        "turns": [],
    }
    # Spiel-spezifischer State-Init
    if name == "zahlen_raten":
        lo, hi = manifest.get("range", [1, 100])
        state["state"]["secret"] = r.randint(int(lo), int(hi))
        state["state"]["guesses"] = 0
        state["state"]["max_guesses"] = int(manifest.get("max_guesses", 7))
        state["state"]["range_lo"] = int(lo)
        state["state"]["range_hi"] = int(hi)
    # Phase 4: Multi-Aktor-Init fuer Sparring/Co-Op-Manifests.
    # Manifest deklariert characters{} - setup.michael_character waehlt aus.
    # Yuki ist fest auf manifest.yuki_character_id (sonst erster char).
    chars = manifest.get("characters") or {}
    if isinstance(chars, dict) and chars:
        starting_hp = int(manifest.get("starting_hp", 30))
        starting_sp = int(manifest.get("starting_sp", 5))
        yuki_cid = manifest.get("yuki_character_id") or next(iter(chars))
        michael_cid = setup.get("michael_character") or manifest.get(
            "michael_character_default") or yuki_cid
        # Validierung gegen Char-Pool, sonst fallback auf yuki_cid (defensive).
        if michael_cid not in chars:
            michael_cid = yuki_cid
        state["state"]["actors"] = {
            "michael": {"character": michael_cid,
                        "hp": starting_hp, "max_hp": starting_hp,
                        "sp": starting_sp, "max_sp": starting_sp},
            "yuki":    {"character": yuki_cid,
                        "hp": starting_hp, "max_hp": starting_hp,
                        "sp": starting_sp, "max_sp": starting_sp},
        }
        state["state"]["round"] = 1
    # Phase 5 Co-Op: Threats-Liste aus dem Manifest klonen. Manifest deklariert
    # threats[] als Vorlage; jeder Eintrag bekommt hp = max_hp und alive=True.
    # Wenn das Manifest keine Threats hat (Solo, PvP-Sparring), bleibt die
    # Liste leer und der Co-Op-Pfad in resolve_user_move triggert nicht.
    threats_tpl = manifest.get("threats") or []
    if isinstance(threats_tpl, list) and threats_tpl:
        threats_state = []
        for i, t in enumerate(threats_tpl):
            if not isinstance(t, dict):
                continue
            tid = (t.get("id") or f"threat_{i+1}").lower()
            mhp = int(t.get("hp") or 10)
            threats_state.append({
                "id": tid,
                "name": t.get("name") or tid,
                "hp": mhp,
                "max_hp": mhp,
                "ac": int(t.get("ac") or 12),
                "dmg": t.get("dmg") or "1d4",
                "description": (t.get("description") or "").strip(),
                "alive": True,
            })
        state["state"]["threats"] = threats_state
    return state


def format_initial_engine_msg(manifest: dict, state: dict) -> str:
    """Manifest's initial_engine_msg mit State-Placeholdern fuellen."""
    tmpl = (manifest.get("initial_engine_msg") or "").strip()
    if not tmpl:
        return ""
    st = state.get("state", {})
    try:
        return tmpl.format(
            range_lo=st.get("range_lo", ""),
            range_hi=st.get("range_hi", ""),
            max_guesses=st.get("max_guesses", ""),
            guesses=st.get("guesses", 0),
            location=st.get("location", ""),
        )
    except (KeyError, IndexError) as e:
        print(f"  [Initial-Engine-Msg-Template-Fehler: {e}]", flush=True)
        return tmpl


# ===========================================================================
# Win/Loss-Logik (Phase 1: nur zahlen_raten)
# ===========================================================================
_INT_IN_TEXT_RE = re.compile(r"-?\d+")


def resolve_user_move(state: dict, manifest: dict,
                       user_content: str,
                       explicit_target_threat_id: str | None = None) -> tuple[str, dict | None]:
    """Engine-seitige Aufloesung des User-Moves. Liefert (engine_text, result_meta).
    engine_text wandert als engine-Turn ans Frontend, result_meta wird zusaetzlich
    der Yuki-Generation als Kontext angeboten (in build_adventure_system_msg).

    explicit_target_threat_id: optionaler UI-Pre-Select (User klickt auf Threat-
    HUD-Card im Frontend). Wird in resolve_coop_player_move durchgereicht und
    greift NUR wenn kein Substring/Stem-Match im Freitext sitzt (Freitext-Override).

    Phase 1: zahlen_raten. Phase 4: alle Manifests mit state.actors{} laufen
    durch den Sparring-Pfad - der Move wird aus dem Pool gepickt oder auf
    Freestyle gefallback'd. Sonstige Manifests liefern (None, None) und ueberlassen
    die Aufloesung Yuki + den Markern."""
    name = manifest.get("name")
    if name == "zahlen_raten":
        return _resolve_zahlen_raten(state, manifest, user_content)
    # Phase 6: Story-Modus (Hybrid-Manifest gerade nicht im Kampf) -> KEIN
    # Move-Resolve. Hybrid-Manifests haben actors{} + characters{} aber im
    # Story-Mode sind Michael+Yuki nicht in rundenbasierter Auseinandersetzung;
    # die Sparring/Co-Op-Zweige unten wuerden sonst Michaels Freitext-Aktion
    # ("ich gehe zum Markt") als Move gegen Yuki resolven. Stolperfalle 22.
    cur_mode = (state.get("state") or {}).get("mode")
    if cur_mode == "story":
        return "", None
    # Phase 5: Co-Op-Manifest (characters + threats) -> Threat-Pfad statt PvP.
    # is_coop_state checkt das state-File, das robuster ist als manifest-only.
    if is_coop_state(state) and isinstance(manifest.get("characters"), dict):
        move = _parse_user_move(state, manifest, user_content)
        if not move:
            return "", None
        engine_text, meta = resolve_coop_player_move(state, manifest,
                                                      attacker="michael",
                                                      move=move,
                                                      user_content=user_content,
                                                      explicit_target_id=explicit_target_threat_id)
        return engine_text, meta
    # Phase 4: Sparring-PvP (characters{} ohne threats[]).
    if isinstance(manifest.get("characters"), dict) and \
       (state.get("state") or {}).get("actors"):
        move = _parse_user_move(state, manifest, user_content)
        if not move:
            return "", None
        engine_text, meta = resolve_sparring_move(state, manifest,
                                                    attacker="michael",
                                                    move=move)
        return engine_text, meta
    return "", None


def _resolve_zahlen_raten(state: dict, manifest: dict,
                           user_content: str) -> tuple[str, dict | None]:
    st = state["state"]
    m = _INT_IN_TEXT_RE.search(user_content or "")
    if not m:
        return ("Engine: das war keine Zahl. Tipp doch eine Zahl zwischen "
                f"{st['range_lo']} und {st['range_hi']}.",
                {"event": "invalid_guess"})
    guess = int(m.group(0))
    secret = int(st["secret"])
    st["guesses"] = int(st.get("guesses", 0)) + 1
    remaining = int(st["max_guesses"]) - int(st["guesses"])
    if guess == secret:
        state["status"] = "closed"
        st["outcome"] = "user_won"
        return (f"Getroffen! Die Zahl war {secret}. "
                f"({st['guesses']} Versuche)",
                {"event": "win", "guess": guess, "secret": secret,
                 "guesses": st["guesses"]})
    direction = "hoeher" if guess < secret else "tiefer"
    if remaining <= 0:
        state["status"] = "closed"
        st["outcome"] = "user_lost"
        return (f"Aus! Die Zahl war {secret}. "
                f"Du hattest {st['max_guesses']} Versuche.",
                {"event": "loss", "guess": guess, "secret": secret})
    return (f"{direction}. ({remaining} Versuche uebrig)",
            {"event": "hint", "guess": guess, "direction": direction,
             "remaining": remaining})


def is_closed(state: dict) -> bool:
    return state.get("status") == "closed"


# ===========================================================================
# Multi-Aktor-Helfer (Phase 4 - Sparring/Co-Op)
# ===========================================================================
def apply_round_start_regen(state: dict, regen_per_round: int = 1) -> dict | None:
    """Wird vom /move-Endpoint VOR resolve_user_move aufgerufen. Inkrementiert
    state.state.round und gibt beiden Actors +regen_per_round SP (clamped auf
    max_sp). Skipt wenn keine actors{} (= Solo-Manifest). Liefert ein Mutations-
    Meta-Dict fuer optionale Engine-Bubble oder None wenn nichts passierte.

    Begruendung: ohne passive Regen brennen die Specials nach 2-3 Runden aus
    und nur die kostenlosen Jab-/Block-Moves bleiben. Mit +1 SP pro Runde
    laeuft sich der Pool langsam wieder auf und Taktik wird moeglich."""
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    if not actors:
        return None
    # Phase 6: Hybrid-Manifests in Story-Modus haben actors{} aber sind nicht
    # im Kampf - kein Runden-Tick, kein SP-Regen. Erst wenn ein Encounter
    # spawnt (mode flippt auf 'combat') zaehlen wieder Runden.
    if (st.get("mode") or "combat") == "story":
        return None
    st["round"] = int(st.get("round", 1)) + 1
    regen = max(0, int(regen_per_round))
    if regen <= 0:
        return None
    out = {"event": "round_regen", "round": st["round"], "regen": regen, "actors": {}}
    for who, a in actors.items():
        if not isinstance(a, dict):
            continue
        cur = int(a.get("sp", 0))
        msp = int(a.get("max_sp", cur + regen))
        new_sp = min(msp, cur + regen)
        if new_sp != cur:
            a["sp"] = new_sp
            out["actors"][who] = {"sp": new_sp, "max_sp": msp, "delta": new_sp - cur}
    return out if out["actors"] else None


def is_coop_manifest(manifest: dict) -> bool:
    """Phase 5: Co-Op-Manifest ist eines mit deklarierter threats-Liste +
    characters-Pool. PvP-Sparring hat characters aber keine threats - der einzige
    Unterscheider zur Pfad-Wahl in resolve_user_move."""
    if not isinstance(manifest, dict):
        return False
    if not isinstance(manifest.get("characters"), dict):
        return False
    return bool(manifest.get("threats"))


def is_coop_state(state: dict) -> bool:
    """State-seitige Variante - hat das State-File schon Threats? Robuster als
    is_coop_manifest weil das Manifest mid-game nicht geaendert werden sollte,
    aber wenn doch (Hot-Edit), zaehlt der State."""
    st = state.get("state", {}) or {}
    return bool(st.get("threats"))


def alive_threats(state: dict) -> list[dict]:
    """Lebendige Threats. Convenience fuer Pipeline-Steps."""
    st = state.get("state", {}) or {}
    return [t for t in (st.get("threats") or [])
            if isinstance(t, dict) and int(t.get("hp", 0)) > 0]


def alive_player_actors(state: dict) -> list[str]:
    """Liste lebender Spieler ('michael'/'yuki'). Tot = hp<=0."""
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    out = []
    for who in ("michael", "yuki"):
        a = actors.get(who) or {}
        if int(a.get("hp", 0)) > 0:
            out.append(who)
    return out


def _pick_target_threat(state: dict, user_content: str = "",
                          explicit_id: str | None = None) -> dict | None:
    """Pickt einen Threat als Angriffsziel. Strategie:
    1. Direkter Substring-Match auf id ODER name ODER ersten Namens-Token.
    2. Stem-Match: id/Tail-Token kommt als WORT-PREFIX im Text vor.
       Bsp: id 'rauber_links' -> Tail-Token 'links' -> matched 'linken' im Text.
       Das ist wichtig fuer deutsche Deklinationen (linker/linken/linkem).
    3. explicit_id (z.B. UI-Pre-Select via Threat-Klick), wenn ein lebender
       Threat mit dieser id existiert. Bewusst NACH 1+2 - Freitext-Override
       gewinnt immer, weil Yuki/User die UI-Auswahl im Satz korrigieren
       koennen sollen.
    4. Fallback: schwaechster lebender Threat (kleinster hp-Wert).
    Liefert None wenn keine Threats mehr leben."""
    alive = alive_threats(state)
    if not alive:
        return None
    txt = (user_content or "").lower()
    if txt:
        # Tokens fuer Stem-Match isolieren (nur Buchstaben).
        words = re.findall(r"[a-zäöüß]+", txt)
        # Laengste Treffer zuerst (sonst matched 'raeuber' alle drei).
        sorted_alive = sorted(alive, key=lambda t: -len((t.get("name") or "")))
        # Pass 1: direkter Substring auf id/name/Namens-erster-Token.
        for thr in sorted_alive:
            for needle in (thr.get("id"), thr.get("name")):
                if needle and needle.lower() in txt:
                    return thr
            nm = (thr.get("name") or "").lower().strip()
            if nm:
                first = nm.split()[0]
                if first and first in txt:
                    return thr
        # Pass 2: Stem-Match - id-Tail-Token UND erstes Namens-Token teilen
        # einen gemeinsamen Prefix von mind. (len(cand)-2) Zeichen mit einem
        # Text-Wort. Deckt deutsche Deklinationen ab:
        # - 'links' (id-Tail) ↔ 'linken' (Wort) ueber 'link'.
        # - 'schneller' (Name-Token) ↔ 'schnellen' (Wort) ueber 'schnelle'.
        def _common_prefix_len(a: str, b: str) -> int:
            n = min(len(a), len(b))
            i = 0
            while i < n and a[i] == b[i]:
                i += 1
            return i
        for thr in sorted_alive:
            stems = []
            tid = (thr.get("id") or "").lower()
            tail = tid.rsplit("_", 1)[-1] if "_" in tid else tid
            if len(tail) >= 4:
                stems.append(tail)
            nm = (thr.get("name") or "").lower().strip()
            if nm:
                first_name_token = nm.split()[0]
                if len(first_name_token) >= 4 and first_name_token not in stems:
                    stems.append(first_name_token)
            for cand in stems:
                threshold = len(cand) - 2
                for w in words:
                    if _common_prefix_len(cand, w) >= threshold and len(w) >= threshold:
                        return thr
    # Pass 3: explizites Pre-Select aus UI - greift nur wenn weder Substring
    # noch Stem-Match angeschlagen haben (Freitext-Override > UI-Auswahl).
    if explicit_id:
        eid = explicit_id.lower()
        for thr in alive:
            if (thr.get("id") or "").lower() == eid:
                return thr
    # Schwaechster zuerst (= mit niedrigster HP). Bei Gleichstand: erster in Liste.
    return min(alive, key=lambda t: int(t.get("hp", 0)))


def _resolve_attack_on_threat(state: dict, attacker: str, move: dict,
                                threat: dict, rng=None) -> tuple[str, dict]:
    """Resolvt einen Spieler-Angriff auf einen Threat. Mutiert state in-place:
    SP-Abzug + threat.hp-Mutation.
    Liefert (engine_text, meta).
    """
    r = rng or random
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    a_actor = actors.get(attacker) or {}
    if not threat:
        return "", {"event": "attack_no_target", "attacker": attacker}

    move_name = move.get("name") or move.get("id") or "Move"
    sp_cost = int(move.get("sp_cost") or 0)
    accuracy = int(move.get("accuracy") or 12)
    damage_spec = move.get("damage") or "1d4"
    sp_cur = int(a_actor.get("sp", 0))
    downgraded = False
    if sp_cost > sp_cur:
        move_name = f"{move_name} (zu erschoepft)"
        sp_cost = 0
        damage_spec = "1d2"
        accuracy = 12
        downgraded = True

    new_sp = max(0, sp_cur - sp_cost)
    sp_regen = int(move.get("sp_regen") or 0)
    if sp_regen > 0:
        msp = int(a_actor.get("max_sp", new_sp + sp_regen))
        new_sp = min(msp, new_sp + sp_regen)
    a_actor["sp"] = new_sp

    target_ac = int(threat.get("ac", 12))
    roll = r.randint(1, 20)
    crit = roll == 20
    fumble = roll == 1
    hit = (roll >= target_ac) and not fumble
    dmg = 0
    if hit:
        dmg, _ = _roll_damage(damage_spec, rng=r)
        if crit:
            dmg = int(dmg * 1.5)
    t_hp_before = int(threat.get("hp", 0))
    t_hp_after = max(0, t_hp_before - dmg)
    threat["hp"] = t_hp_after
    if t_hp_after <= 0:
        threat["alive"] = False

    a_display = "Michaels" if attacker == "michael" else "Yukis"
    tname = threat.get("name") or threat.get("id") or "Threat"
    parts = [f"{a_display} {move_name}: 1d20={roll} vs AC {target_ac}"]
    if fumble:
        parts.append("Patzer (1)")
    elif crit:
        parts.append("kritischer Treffer (20)")
    elif hit:
        parts.append("Treffer")
    else:
        parts.append("daneben")
    if hit:
        parts.append(f"Schaden {damage_spec} = {dmg}")
    if downgraded:
        parts.append("(SP-Reduktion)")
    if t_hp_after <= 0:
        parts.append(f"-> {tname} faellt!")
    else:
        parts.append(f"-> {tname}: HP {t_hp_after}/{threat.get('max_hp', t_hp_after)}")
    engine_text = " | ".join(parts)

    meta = {
        "event": "coop_attack",
        "attacker": attacker,
        "target_threat": threat.get("id"),
        "target_name": tname,
        "move_id": move.get("id"),
        "move_name": move_name,
        "roll": roll, "accuracy": target_ac,
        "hit": hit, "crit": crit, "fumble": fumble,
        "damage": dmg,
        "damage_spec": damage_spec if hit else None,
        "sp_cost": sp_cost,
        "sp_left": a_actor["sp"],
        "threat_hp_before": t_hp_before,
        "threat_hp_after": t_hp_after,
        "threat_dead": t_hp_after <= 0,
    }
    if downgraded:
        meta["downgraded"] = True
    return engine_text, meta


def _resolve_heal_move(state: dict, healer: str, move: dict,
                        rng=None) -> tuple[str, dict]:
    """Resolvt einen Heal-Move. Target abhaengig von move.target:
    - 'self': healer heilt sich selbst (Move-Type 'Heilen')
    - 'ally': healer heilt den ANDEREN Spieler (Move-Type 'Erste Hilfe')
    - Default: 'ally' (Rueckkompatibilitaet zu Pre-2026-06-07-Pools).

    Wenn der Target bei voller HP ist, wird der Move trotzdem 'ausgefuehrt'
    (SP wird abgezogen, Heal-Wuerfel rollt) - das ist Yukis/Michaels
    Verantwortung, klug zu casten. Engine-Text macht das klar.
    Liefert (engine_text, meta).
    """
    r = rng or random
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    healer_actor = actors.get(healer) or {}
    target_kind = (move.get("target") or "ally").lower()
    if target_kind == "self":
        ally_name = healer
        ally = healer_actor
    else:
        ally_name = "yuki" if healer == "michael" else "michael"
        ally = actors.get(ally_name) or {}

    move_name = move.get("name") or move.get("id") or "Heal"
    sp_cost = int(move.get("sp_cost") or 0)
    heal_spec = move.get("damage") or "1d6+2"
    sp_cur = int(healer_actor.get("sp", 0))
    insufficient = sp_cost > sp_cur
    if insufficient:
        move_name = f"{move_name} (zu erschoepft)"
        sp_cost = 0
        heal_spec = "1d2"
    healer_actor["sp"] = max(0, sp_cur - sp_cost)

    heal_amount, _ = _roll_damage(heal_spec, rng=r)
    ally_hp_before = int(ally.get("hp", 0))
    ally_max = int(ally.get("max_hp", ally_hp_before + heal_amount))
    ally_hp_after = min(ally_max, ally_hp_before + heal_amount)
    revived = ally_hp_before <= 0 < ally_hp_after
    ally["hp"] = ally_hp_after

    a_display = "Michael" if healer == "michael" else "Yuki"
    ally_display = "Yuki" if ally_name == "yuki" else "Michael"
    is_self = (target_kind == "self")
    target_display = "sich" if is_self else ally_display
    parts = [f"{a_display} setzt {move_name} ein"]
    if revived:
        parts.append(f"{target_display if is_self else ally_display} "
                     f"kommt zurueck (+{heal_amount} HP)")
    elif ally_hp_after >= ally_max:
        parts.append(f"{target_display if is_self else ally_display} "
                     f"bereits voll - Heilung verpufft (+0 HP)")
    else:
        actual_gain = ally_hp_after - ally_hp_before
        parts.append(f"+{actual_gain} HP fuer {target_display}")
    parts.append(f"-> {ally_display}: HP {ally_hp_after}/{ally_max}")
    engine_text = " | ".join(parts)

    meta = {
        "event": "coop_heal",
        "healer": healer,
        "ally": ally_name,
        "target_kind": target_kind,        # 'self' oder 'ally' fuer Frontend-Hint
        "move_id": move.get("id"),
        "move_name": move_name,
        "heal_spec": heal_spec,
        "heal_amount_rolled": heal_amount,
        "ally_hp_before": ally_hp_before,
        "ally_hp_after": ally_hp_after,
        "revived": revived,
        "sp_cost": sp_cost,
        "sp_left": healer_actor["sp"],
    }
    return engine_text, meta


def resolve_coop_player_move(state: dict, manifest: dict, attacker: str,
                              move: dict, user_content: str = "",
                              explicit_target_id: str | None = None,
                              rng=None) -> tuple[str, dict]:
    """Dispatcher fuer Co-Op-Player-Moves. attack -> Threat-Angriff, heal ->
    Ally-Heilung. Pickt das Threat-Target aus user_content (Substring/Stem),
    dann explicit_target_id (UI-Pre-Select), sonst Fallback auf den
    schwaechsten lebenden Threat. Freitext gewinnt immer gegen UI-Auswahl.

    Defensiv: wenn der Attacker selbst KO ist (HP<=0), wird der Move geskippt
    (leerer engine_text). Das fangen die Pipeline-Callers ab: Yuki KO -> ihr
    [move:...] aus dem LLM-Reply landet hier und wird stillschweigend ignoriert.
    """
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    a_actor = actors.get(attacker) or {}
    if int(a_actor.get("hp", 0)) <= 0:
        return "", {"event": "attacker_down", "attacker": attacker}
    kind = (move.get("kind") or "attack").lower()
    if kind == "heal":
        return _resolve_heal_move(state, attacker, move, rng=rng)
    threat = _pick_target_threat(state, user_content, explicit_target_id)
    if threat is None:
        return ("Engine: kein Threat mehr am Leben - der Angriff laeuft ins Leere.",
                {"event": "attack_no_target", "attacker": attacker})
    return _resolve_attack_on_threat(state, attacker, move, threat, rng=rng)


def _pick_threat_target_actor(state: dict, rng=None) -> str | None:
    """Threats greifen einen RANDOM lebenden Spieler an. Bei einem lebenden:
    nur der. Bei keinem: None."""
    r = rng or random
    alive = alive_player_actors(state)
    if not alive:
        return None
    return r.choice(alive)


def _resolve_threat_attack(state: dict, threat: dict, target_actor: str,
                            rng=None) -> tuple[str, dict]:
    """Ein Threat greift einen Spieler an. Mutiert state.actors[target].hp.
    Liefert (engine_text, meta).
    """
    r = rng or random
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    target = actors.get(target_actor) or {}
    target_ac = int(target.get("ac", 12))  # Spieler-AC default 12; Manifest darf override
    dmg_spec = threat.get("dmg") or "1d4"
    roll = r.randint(1, 20)
    crit = roll == 20
    fumble = roll == 1
    hit = (roll >= target_ac) and not fumble
    dmg = 0
    if hit:
        dmg, _ = _roll_damage(dmg_spec, rng=r)
        if crit:
            dmg = int(dmg * 1.5)
    hp_before = int(target.get("hp", 0))
    hp_after = max(0, hp_before - dmg)
    target["hp"] = hp_after

    tname = threat.get("name") or threat.get("id") or "Threat"
    a_display = "Michael" if target_actor == "michael" else "Yuki"
    parts = [f"{tname} greift {a_display} an: 1d20={roll} vs AC {target_ac}"]
    if fumble:
        parts.append("Patzer")
    elif crit:
        parts.append("kritisch")
    elif hit:
        parts.append("Treffer")
    else:
        parts.append("daneben")
    if hit:
        parts.append(f"Schaden {dmg_spec} = {dmg}")
    if hp_after <= 0:
        parts.append(f"-> {a_display} geht zu Boden (HP 0)")
    else:
        parts.append(f"-> {a_display}: HP {hp_after}/{target.get('max_hp', hp_after)}")
    engine_text = " | ".join(parts)

    meta = {
        "event": "threat_attack",
        "threat_id": threat.get("id"),
        "threat_name": tname,
        "target_actor": target_actor,
        "roll": roll, "accuracy": target_ac,
        "hit": hit, "crit": crit, "fumble": fumble,
        "damage": dmg,
        "damage_spec": dmg_spec if hit else None,
        "target_hp_before": hp_before,
        "target_hp_after": hp_after,
        "target_down": hp_after <= 0,
    }
    return engine_text, meta


def run_threats_phase(state: dict, manifest: dict,
                       rng=None) -> list[tuple[str, dict]]:
    """Alle lebenden Threats greifen jeweils einen lebenden Spieler an
    (random-Pick pro Threat). Skipt einzelne Threats wenn kein lebender
    Spieler mehr existiert.

    Abbruchbedingung: sobald Michael KO geht, brechen wir ab - das Spiel
    ist mit dem naechsten check_coop_outcome eh vorbei und weitere Threat-
    Schlaege auf bereits-am-Boden-Yuki waeren narrativ unsinnig.

    Liefert Liste von (engine_text, meta)-Tupeln. Caller (server) haengt
    jedes als eigene engine-Bubble an. KEIN State-Mutating-Marker hier;
    Mutationen passieren direkt in _resolve_threat_attack."""
    out = []
    for thr in alive_threats(state):
        # Abbruch wenn Michael KO -> Spiel ist eh vorbei.
        st = state.get("state", {}) or {}
        michael = (st.get("actors") or {}).get("michael") or {}
        if int(michael.get("hp", 0)) <= 0:
            break
        target = _pick_threat_target_actor(state, rng=rng)
        if target is None:
            # Keine lebenden Spieler mehr - check_coop_outcome wird greifen.
            break
        engine_text, meta = _resolve_threat_attack(state, thr, target, rng=rng)
        out.append((engine_text, meta))
    return out


def check_coop_outcome(state: dict, manifest: dict | None = None) -> str | None:
    """Phase 5+6: Co-Op-Outcome-Check.

    Asymmetrische KO-Mechanik (beide Modi):
    - Michael HP <= 0    -> user_lost (sofort, schliesst Spiel)
    - Yuki KO            -> nicht-final, Michael kann sie heilen.

    Phase-6-Verzweigung bei alle-Threats-KO:
    - manifest.win_condition == 'all_threats_down' (reines Co-Op wie
      coop_kyoto_cafe) ODER manifest is None (Test-Pfad, Legacy):
      -> user_won, status:closed (alte Phase-5-Logik).
    - sonst (Hybrid-Manifest wie kyoto_hojicha_mystery): COMBAT_CLEARED.
      Spiel laeuft weiter in Story-Modus. Threats-Liste wird geleert,
      state.state.mode -> 'story', aktuelle Location -> outcome:'cleared',
      SP-Reset fuer beide Actors (HP bleibt - Verletzung traegt sich rueber
      als Story-Stake). State.status bleibt 'active' - der Caller sieht das
      und triggert KEINE Episode-Finalisierung.

    Idempotent: wenn state schon closed, None zurueck.
    """
    if state.get("status") == "closed":
        return None
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    threats = st.get("threats") or []
    # Co-Op-Pfad nur wenn Threats existieren - sonst fallback auf
    # check_ko_auto_close (Sparring) bzw. gar nichts (Solo).
    if not threats:
        return None
    michael = actors.get("michael", {}) or {}
    m_down = int(michael.get("hp", 1)) <= 0
    if m_down:
        state["status"] = "closed"
        st["outcome"] = "user_lost"
        return "💥 Michael geht zu Boden. Yuki kniet neben ihm - das Match ist vorbei."
    threats_alive = [t for t in threats
                     if isinstance(t, dict) and int(t.get("hp", 0)) > 0]
    if threats_alive:
        return None
    # Alle Threats KO. Verzweigung nach Manifest-Typ.
    win_condition = (manifest or {}).get("win_condition") if manifest else None
    is_hybrid = bool(manifest) and win_condition != "all_threats_down"
    if not is_hybrid:
        # Reines Co-Op (oder Legacy-Test-Pfad ohne manifest).
        state["status"] = "closed"
        st["outcome"] = "user_won"
        return "🎉 Alle Angreifer am Boden. Stille im Cafe - ihr habt es geschafft."
    # Phase 6 Hybrid: COMBAT_CLEARED. Aufraeumen + zurueck in Story-Modus.
    st["mode"] = "story"
    cur_loc = (st.get("location") or "").strip()
    if cur_loc:
        _touch_location_state(st, cur_loc, "cleared")
    # Threats wegraeumen - Frontend body.adventure-coop haengt an
    # state.threats.length>0 und fliegt damit automatisch ab.
    st["threats"] = []
    st.pop("combat_started_at", None)
    # SP-Reset (HP bleibt absichtlich, traegt sich als Story-Stake rueber).
    for who in ("michael", "yuki"):
        a = actors.get(who) or {}
        if isinstance(a, dict) and "max_sp" in a:
            a["sp"] = int(a["max_sp"])
    return ("🕊 Stille kehrt zurueck. Die Angreifer sind am Boden - "
            "ihr koennt durchatmen und weiter eurer Spur folgen.")


def _build_yuki_story_block(state: dict, manifest: dict) -> str:
    """Phase 6: YUKI-STATE-Block fuer Story-Modus (Hybrid-Manifests).
    Yuki sieht die Location-Memory + Encounter-Marker-Doku + die explizite
    Cleared-Sperre. Default-Output fuer mode=story bei Hybrid-Manifests.
    """
    st = state.get("state", {}) or {}
    cur_loc = (st.get("location") or "").strip()
    ls = st.get("location_states") or {}
    visited_empty = sorted([k for k, v in ls.items()
                            if (v or {}).get("outcome") == "empty"])
    cleared = sorted([k for k, v in ls.items()
                      if (v or {}).get("outcome") == "cleared"])
    peaceful = bool(state.get("peaceful_mode"))
    lines = ["YUKI-STATE (Story-Modus, Pflichtlektuere):"]
    lines.append(f"- Aktuelle Location: {cur_loc or '(noch nirgends)'}")
    if visited_empty:
        lines.append(f"- Besucht, kein Kampf: {', '.join(visited_empty)}")
    if cleared:
        lines.append(f"- Gecleared (DORT KEIN [encounter:...] mehr - Engine "
                     f"strippt ihn ohnehin): {', '.join(cleared)}")
    if peaceful:
        lines.append("- FRIEDLICHER MODUS aktiv: User hat Kaempfe ausgeschaltet. "
                     "KEINE [encounter:...]-Marker schreiben - Engine strippt "
                     "sie ohnehin. Spiel bleibt komplett Story/Detektiv ohne "
                     "Kampfszenen. [move:...] ebenfalls nicht relevant.")
        lines.append("- [move:...]-Marker machen in der Story KEINEN Sinn.")
        return "\n".join(lines)
    lines.append("- Wenn die Geschichte einen Kampf rechtfertigt, schreibe "
                 "[encounter:Name|HP|AC|dmg] pro Gegner. ALLE VIER FELDER "
                 "PFLICHT mit | getrennt, sonst spawnt der Encounter nicht "
                 "(Engine matched strict). Format-Beispiel: "
                 "[encounter:Magerer Raeuber|12|11|1d4][encounter:Stiernackiger "
                 "Raeuber|16|12|1d4+1]. HP 8-20, AC 10-14, dmg 1d3 / 1d4 / "
                 "1d4+1 / 1d6 / 1d6+1.")
    lines.append("- WARNUNG: '[encounter:Name|HP|AC]' (nur 3 Felder, kein dmg) "
                 "gilt als Fehler - die Engine nimmt Default 1d4. Lieber das "
                 "vierte Feld direkt mitschreiben.")
    lines.append("- KEINE [encounter:...]-Marker an cleared/active Locations "
                 "oder wenn ihr schon im Kampf seid. Lieber Narration.")
    lines.append("- [move:...]-Marker machen in der Story KEINEN Sinn - "
                 "nur waehrend Kampf.")
    encounter_hints = manifest.get("encounter_hints") if manifest else None
    if isinstance(encounter_hints, list) and encounter_hints:
        lines.append("- Vorschlaege fuers Manifest (kannst du frei abwandeln):")
        for h in encounter_hints[:5]:
            lines.append(f"  * {str(h).strip()}")
    return "\n".join(lines)


def build_yuki_state_block(state: dict, manifest: dict) -> str:
    """Phase 5+6: Code-Hint-Block fuer Yuki - Move-Pick (Combat) oder
    Encounter-Disziplin (Story).

    PHASE 5: Combat-Pfad, jede Runde ein [move:...] ist Pflicht analog
    Sparring, AUSSER sie ist KO (HP 0).
    - 'attacked_last_round': Konter-Move + Bewertung des Treffers
    - 'michael_low': first_aid + Sorge-Tonung
    - 'yuki_low': heal (Self) + kurz innehalten
    - 'yuki_down': KEIN Move (passiv)
    - 'all_clear': Angriff + Beobachtung der Lage (welcher Threat schwankt etc.)

    PHASE 6: Story-Pfad (Hybrid-Manifests). Yuki sieht Location-Memory +
    Encounter-Marker-Doku und kriegt einen Hint zu cleared-Locations damit
    sie nicht versucht, dort erneut zu spawnen (Engine wuerde es ohnehin
    strippen, der Prompt-Hint spart Frust + Token).
    """
    st = state.get("state", {}) or {}
    cur_mode = st.get("mode") or ("combat" if st.get("threats") else "story")
    # Story-Modus: Hybrid-Manifest mit actors{} aber gerade NICHT im Kampf.
    if cur_mode == "story":
        # Solo-Manifests (verschwundener_schluessel, zahlen_raten) brauchen
        # diesen Block nicht - sie haben keine actors{}/threats[]-Mechanik.
        if not (manifest or {}).get("characters"):
            return ""
        return _build_yuki_story_block(state, manifest)
    if not is_coop_state(state):
        return ""
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    yuki = actors.get("yuki") or {}
    michael = actors.get("michael") or {}
    yhp = int(yuki.get("hp", 0))
    ymax = int(yuki.get("max_hp", 1)) or 1
    mhp = int(michael.get("hp", 0))
    mmax = int(michael.get("max_hp", 1)) or 1

    yuki_down = yhp <= 0
    yuki_low = (yhp / ymax) < 0.4 and yhp > 0
    michael_low = (mhp / mmax) < 0.4 and mhp > 0

    # Letzte Runde: schau die letzten ~5 engine-Turns ob threat_attack auf
    # yuki kam und getroffen hat (dmg>0). Wichtig: nur den ALLERLETZTEN
    # threat_attack-Block vor dem aktuellen User-Move beruecksichtigen -
    # NICHT die ganze vorherige Runde aufzaehlen, sonst tendiert Yuki dazu,
    # die alte Runde nochmal aufzurollen statt Michael's aktuellen Move
    # zu kommentieren.
    attacked_last_round = False
    last_attacker_name = None
    for t in reversed(state.get("turns", [])[-8:]):
        if t.get("role") != "engine":
            continue
        meta = t.get("meta") or {}
        if meta.get("event") == "threat_attack" \
                and meta.get("target_actor") == "yuki" \
                and meta.get("hit"):
            attacked_last_round = True
            last_attacker_name = meta.get("threat_name")
            break

    # Phase 6 (Fix 2026-06-07): Michaels AKTUELLEN Move heraussuchen - der
    # letzte coop_attack-Engine-Turn mit attacker=michael in den letzten
    # 4 turns. Yuki bekommt damit den Fokus: "kommentiere DIESEN Move,
    # nicht was vor Runden passierte". Vorher fehlte dieser Anker komplett
    # und Yuki bezog sich auf das was am Ende ihres Contexts stand -
    # meistens die threat_attack-Bubbles vom Ende der VORHERIGEN Runde.
    michael_just_did = None
    for t in reversed(state.get("turns", [])[-6:]):
        if t.get("role") != "engine":
            continue
        meta = t.get("meta") or {}
        if meta.get("event") == "coop_attack" \
                and meta.get("attacker") == "michael":
            michael_just_did = meta
            break

    # Schwaechster lebender Threat (fuer Beobachtungs-Hint im all_clear-Fall)
    alive_list = alive_threats(state)
    weakest_hint = ""
    if alive_list:
        weakest = min(alive_list, key=lambda t: int(t.get("hp", 0)))
        wname = weakest.get("name") or weakest.get("id")
        whp = int(weakest.get("hp", 0))
        wmhp = int(weakest.get("max_hp", whp)) or 1
        if whp / wmhp < 0.4:
            weakest_hint = f" Beobachtung: {wname} schwankt schon (HP {whp}/{wmhp})."

    threats_left = len(alive_list)
    lines = ["YUKI-STATE (Pflichtlektuere fuer deinen Move-Pick):"]

    # Phase 6 (Fix 2026-06-07): Michaels aktueller Move ist der Anker fuer
    # deinen Kommentar. Auch wenn andere Hinweise unten kommen (du wurdest
    # getroffen / Michael ist knapp / Lage ruhig) - du beziehst dich PRIMAER
    # auf das was Michael GERADE gemacht hat, nicht auf vorherige Runden.
    if michael_just_did:
        move_name = michael_just_did.get("move_name") \
                    or michael_just_did.get("move_id", "ein Move")
        hit = michael_just_did.get("hit")
        threat_name = michael_just_did.get("target_name") \
                      or michael_just_did.get("target_threat", "den Threat")
        if hit:
            dmg = michael_just_did.get("damage", "?")
            outcome = f"TRAF {threat_name} (-{dmg} HP)"
        else:
            outcome = f"daneben gegen {threat_name}"
        lines.append(f"- WICHTIGSTE LAGE-INFO: Michael hat GERADE {move_name} "
                     f"gemacht -> {outcome}. Dein Kommentar bezieht sich PRIMAER "
                     f"auf diesen frischen Move. NICHT auf das was am Ende der "
                     f"vorigen Runde passiert ist - das ist Geschichte.")

    if yuki_down:
        lines.append("- Du liegst am Boden (HP 0). Du kannst diesen Turn KEINEN "
                     "[move:...] schreiben. Bleib stumm oder gib max. einen Halbsatz "
                     "Antwort - Michael muss dich erst heilen.")
    elif attacked_last_round:
        lines.append(f"- Nebeninfo: du wurdest in der letzten Runde angegriffen "
                     f"({last_attacker_name or 'ein Threat'}) - das ist die LAGE "
                     f"jetzt (du hast eine Schramme), aber NICHT dein Haupt-Thema. "
                     f"Kommentiere zuerst Michaels frischen Move (s.o.), dann "
                     f"kontere mit [move:hayate/iai_nuki/...].")
    elif michael_low:
        lines.append(f"- Michael ist knapp (HP {mhp}/{mmax}). [move:first_aid] "
                     f"heilt ihn - kurze Sorge zu seinem Stand + dein Move.")
    elif yuki_low:
        lines.append(f"- Du bist selbst knapp (HP {yhp}/{ymax}). [move:heal] "
                     f"heilt dich selbst (target=self) - kurz innehalten + dein Move.")
    else:
        lines.append("- Lage ruhig (niemand auf 0 HP, beide weitgehend voll). "
                     "Du greifst trotzdem an - ein [move:...] gegen einen Threat "
                     "ist auch hier PFLICHT (du bist im Kampf, nicht im Cafe-"
                     "Plaudermodus). 1-2 Saetze Beobachtung der Lage + dein Move."
                     + weakest_hint)
    lines.append(f"- Threats noch am Leben: {threats_left}.")
    return "\n".join(lines)


def threats_block_for_prompt(state: dict, manifest: dict) -> str:
    """Phase 5: kompakte Threats-Liste fuer den System-Prompt. Lebende oben,
    tote unten ausgegraut. Yuki sieht damit was sie/Michael noch erwartet."""
    if not is_coop_state(state):
        return ""
    st = state.get("state", {}) or {}
    threats = st.get("threats") or []
    if not threats:
        return ""
    lines = []
    alive = [t for t in threats if isinstance(t, dict) and int(t.get("hp", 0)) > 0]
    dead = [t for t in threats if isinstance(t, dict) and int(t.get("hp", 0)) <= 0]
    for t in alive:
        nm = t.get("name") or t.get("id")
        hp = int(t.get("hp", 0))
        mhp = int(t.get("max_hp", hp))
        ac = int(t.get("ac", 12))
        desc = (t.get("description") or "").strip()
        seg = f"- {t.get('id')} | {nm}: HP {hp}/{mhp}, AC {ac}"
        if desc:
            seg += f" - {desc}"
        lines.append(seg)
    for t in dead:
        nm = t.get("name") or t.get("id")
        lines.append(f"- {t.get('id')} | {nm}: am Boden (besiegt)")
    return "\n".join(lines)


def check_ko_auto_close(state: dict) -> str | None:
    """Prueft nach State-Marker-Mutationen ob ein Actor.hp <= 0 ist und schliesst
    das Spiel automatisch. Liefert den Engine-Text fuer eine zusaetzliche engine-Bubble
    ('KO! Michael geht zu Boden. Yuki gewinnt das Match.') oder None wenn niemand
    am Boden ist.

    Konvention fuer outcome: 'user_won' wenn yuki KO, 'user_lost' wenn michael KO.
    Bei beiden gleichzeitig KO (Draw) -> 'draw'. Der Server-Endpoint haengt den
    zurueckgegebenen Text als engine-turn an und triggert _adventure_finalize_episode.
    Idempotent: wenn state schon closed ist, wird nichts mehr aenderungswuerdig
    angefasst und None zurueckgegeben."""
    if state.get("status") == "closed":
        return None
    st = state.get("state", {}) or {}
    actors = st.get("actors") or {}
    if not actors:
        return None
    michael = actors.get("michael", {}) or {}
    yuki = actors.get("yuki", {}) or {}
    m_down = int(michael.get("hp", 1)) <= 0
    y_down = int(yuki.get("hp", 1)) <= 0
    if not (m_down or y_down):
        return None
    state["status"] = "closed"
    if m_down and y_down:
        st["outcome"] = "draw"
        return "💥 Beide am Boden. Doppel-KO - das Match endet unentschieden."
    if y_down:
        st["outcome"] = "user_won"
        return "💥 KO! Yuki geht zu Boden. Michael gewinnt das Match."
    st["outcome"] = "user_lost"
    return "💥 KO! Michael geht zu Boden. Yuki gewinnt das Match."


# ===========================================================================
# Spiel-Ende: Meta-Episode-Eintrag (KEIN Story-Inhalt)
# ===========================================================================
def _extract_story_turns_for_summary(state: dict, max_turns: int = 12,
                                       per_turn_chars: int = 180) -> list[str]:
    """Pickt die letzten ~max_turns Story-Turns aus state.turns fuer den LLM-
    Summary-Prompt. Engine-Bubbles (Wuerfel/Resolve) sind mechanisches Rauschen
    und werden geskippt - nur user/yuki/dm zaehlen als 'Story-Inhalt'.

    per_turn_chars trunciert lange Bubbles damit der Prompt handlich bleibt -
    Yukis Adventure-Replies sind oft 200+ Worte, wir wollen den Token-Footprint
    klein halten weil der Summary-Call mit Thinking gemacht wird."""
    role_label = {"user": "Michael", "yuki": "Yuki", "dm": "DM"}
    out: list[str] = []
    for turn in state.get("turns", []):
        role = turn.get("role")
        if role not in role_label:
            continue
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        snippet = content[:per_turn_chars]
        if len(content) > per_turn_chars:
            snippet += "..."
        out.append(f"{role_label[role]}: {snippet}")
    # Letzte max_turns Eintraege - die Wrap-Up-Szene ist immer wichtiger als
    # die Eroeffnung fuer die Erinnerung
    return out[-max_turns:]


def build_meta_episode(state: dict, manifest: dict, chat_fn=None) -> dict:
    """Erzeugt EINEN 1-2-Satz-Meta-Eintrag fuer yuki_episodes.json. Caller
    (server.py) haengt das via yc.append_episodes an die Episoden-Datei.
    Liefert {date, text}-Dict (gleiches Schema wie der LLM-Pfad in
    yc.append_episodes).

    Modi:
    - chat_fn=None (Default): deterministische Variante 'mit Michael X gespielt,
      ~Ymin, Z Turns, gewonnen'. Phase-1-Verhalten, rueckwaerts-kompatibel.
    - chat_fn=callable: LLM baut einen organischen 1-2-Satz-Tagebuch-Memo aus
      den letzten ~12 Story-Turns ('Mit Michael durch Night City Yukis Tagebuch
      gesucht - Hana hatte es im Salon, Wrap-Up bei Jasmin-Tee.'). Bei
      LLM-Fehler oder zu kurzer/zu langer Antwort faellt es auf den
      deterministischen Text zurueck.

    chat_fn-Signatur: (messages: list[dict], temperature: float, purpose: str)
    -> str (passt zu yuki_core.chat_ollama). Reingereicht weil adventure_engine
    bewusst KEINE yuki_core-Abhaengigkeit haben soll - Caller wrappt."""
    today = datetime.date.today().isoformat()
    display = manifest.get("display_name") or manifest.get("name") or "Adventure"
    n_turns = len(state.get("turns", []))
    # Dauer aus started_at (ISO mit tz) ableiten - knapp, Minuten-Granularitaet.
    duration_min = None
    try:
        t0 = datetime.datetime.fromisoformat(state.get("started_at", ""))
        t1 = datetime.datetime.now(t0.tzinfo) if t0.tzinfo else datetime.datetime.now()
        duration_min = max(1, int((t1 - t0).total_seconds() // 60))
    except (ValueError, TypeError):
        pass
    outcome = state.get("state", {}).get("outcome")
    outcome_word = {"user_won": "gewonnen", "user_lost": "verloren",
                     "draw": "unentschieden"}.get(outcome, "")
    parts = [f"mit Michael {display} gespielt"]
    if duration_min:
        parts.append(f"~{duration_min}min")
    parts.append(f"{n_turns} Turns")
    if outcome_word:
        parts.append(outcome_word)
    fallback_text = ", ".join(parts)

    if chat_fn is None:
        return {"date": today, "text": fallback_text}

    # --- LLM-Summary-Pfad ---
    story_lines = _extract_story_turns_for_summary(state)
    if not story_lines:
        return {"date": today, "text": fallback_text}

    duration_str = f"~{duration_min} Minuten" if duration_min else "unbekannte Dauer"
    outcome_str = outcome_word or "(natuerlicher Abschluss, kein Win/Loss-Outcome)"
    sys_prompt = (
        "Du schreibst EINEN Tagebuch-Eintrag fuer Yuki (japanische Begleiterin "
        "von Michael) ueber ein gerade beendetes Pen-and-Paper-Spiel. Format: "
        "1-2 dichte Saetze, 1. Person Plural ('mit Michael ... gespielt'), "
        "Vergangenheit. Erinnerungsform - WAS hat sich zugetragen + EIN "
        "atmosphaerisches Detail. KEINE Schritt-fuer-Schritt-Loesungs-"
        "Choreografie, KEINE Inventar-Auflistung, KEIN Engine-Jargon "
        "('encounter', 'roll', 'mutation'). Maximal ~35 Worte. "
        "Antworte AUSSCHLIESSLICH mit dem Memo-Text, kein Vorwort, kein "
        "Markdown-Fence, keine Anfuehrungszeichen drumherum."
    )
    user_prompt = (
        f"Spiel: {display}\n"
        f"Dauer: {duration_str}, {n_turns} Turns\n"
        f"Outcome: {outcome_str}\n\n"
        f"Verlauf (Auszug der letzten Turns):\n"
        + "\n".join(story_lines)
        + "\n\nSchreib jetzt 1-2 Saetze als Tagebuch-Erinnerung.\n"
        "Beispiel-Stil: 'Mit Michael durch Night City Yukis Tagebuch gesucht - "
        "Hana hatte es im Massage-Salon, Wrap-Up bei Jasmin-Tee daheim.'"
    )
    try:
        raw = chat_fn(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": user_prompt}],
            temperature=0.55,
            purpose="adventure_meta",
        )
    except Exception:
        return {"date": today, "text": fallback_text}

    text = (raw or "").strip()
    # Markdown-Fence + umschliessende Anfuehrungszeichen entfernen falls Modell
    # die Anweisung ignoriert
    text = re.sub(r"^```[a-z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip().strip('"').strip("'").strip("«»„").strip()
    # Sanity: muss zwischen 20-280 Zeichen liegen, sonst Fallback
    # (zu kurz = LLM hat versagt, zu lang = LLM hat sich verzettelt)
    if 20 <= len(text) <= 280:
        return {"date": today, "text": text}
    return {"date": today, "text": fallback_text}
