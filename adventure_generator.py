"""Adventure-Generator - Multi-Pass-LLM-Pipeline fuer neue Manifests.

Generiert ein neues `config/adventures/<slug>.json` aus User-Eckdaten (Setting,
Pitch, Tone, Yuki-Rolle). Statt einem grossen One-Shot-Call (der bei 12B/8B-
Failover-Modellen praktisch sicher kaputt-JSON oder vergessene Felder liefert)
laeuft die Generation in 4-7 sequentiellen LLM-Calls mit Thinking, jeder Pass
bekommt die Outputs der Vorgaenger als Input - so kann auch ein 12B-Modell
konsistente 20KB-Manifests im Stil von `kyoto_hojicha_mystery` bauen.

PIPELINE pro Mode:

* cozy_story (kein DM-LLM, kein Combat, wie `verschwundener_schluessel`):
    1. world_skeleton       - display_name + description + Locations + NPCs
    2. solution_architecture - Loesungs-Faecher + Hinweis-Spuren + 2-Schritt-Wrap
    3. cozy_system_rules    - system_rules-Prose (analog verschwundener_schluessel)
    4. opener_view_hint     - initial_engine_msg + win_condition + win_hint
    5. self_critique        - Konsistenz-Check (LLM-driven)

* story_hybrid (mit DM-LLM, Encountern, Char-Pool, wie `kyoto_hojicha_mystery`):
    1. world_skeleton
    2. solution_architecture
    3. dm_voice             - dm_system_rules-Prose (anspruchsvollster Pass)
    4. encounter_hints      - 3-5 Encounter-Vorschlaege mit HP/AC/dmg
    5. opener_view_hint     - initial_engine_msg + yuki_initial_view + win_hint
    6. self_critique

SPOILER-ISOLATION: Das `solution_architecture`-Output bleibt im Server (Job-State),
wird NIE ans Frontend exponiert. User sieht beim Save-Preview nur display_name,
description, Locations-Count, NPC-Count - nichts ueber das eigentliche Raetsel.
Beim spaeteren Spielen ist die Loesung dadurch wirklich neu.

JOB-LIFECYCLE: Background-Thread (daemon=True), in-memory Job-Registry, SSE-Stream
fuer Progress-Updates, Cancel-Endpoint zum Abbrechen, TTL 1h fuer abgelaufene Jobs.
"""

from __future__ import annotations

import datetime
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yuki_core as yc
import adventure_engine

_ROOT = Path(__file__).resolve().parent
MANIFESTS_DIR = _ROOT / "config" / "adventures"

# ===========================================================================
# Job-Registry (modul-level, lebt im Server-Prozess)
# ===========================================================================
_JOBS: dict[str, "GenerationJob"] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_S = 3600  # 1h - alte Jobs werden beim naechsten Start aufgeraeumt


# ===========================================================================
# Char-Pool 1:1 aus kyoto_hojicha_mystery
# ===========================================================================
# Hybrid-Modus liefert IMMER diesen Pool mit. Bewusst NICHT pro Generation neu
# erfinden - die 5 Templates sind balanced, getestet, und Yuki's Iaido-Style
# passt zu ihrer Bio. Lieber spaeter manuell tunen falls noetig. Spart pro
# Generation einen ganzen LLM-Schritt + verhindert Move-Balance-Fehler.
_HYBRID_STARTING_HP = 30
_HYBRID_STARTING_SP = 5
_HYBRID_YUKI_CHAR_ID = "yuki_iaido"
_HYBRID_MICHAEL_DEFAULT = "shotokan_brawler"

_HYBRID_CHAR_POOL: dict[str, dict] = {
    "yuki_iaido": {
        "name": "Yuki (Kyoto-Iaido)",
        "description": "Schwertzieh-Kunst aus dem Sanjusangendo-Dojo. Schnell, praezise, mit Augen wie Stille.",
        "moves": [
            {"id": "hayate", "name": "Hayate", "sp_cost": 0, "damage": "1d4+1", "accuracy": 14, "description": "Schneller Schnitt nach vorn"},
            {"id": "iai_nuki", "name": "Iai-Nuki", "sp_cost": 2, "damage": "1d6+2", "accuracy": 12, "description": "Blitz-Ziehschnitt aus der Scheide"},
            {"id": "mawashi_uke", "name": "Mawashi-Uke", "sp_cost": 0, "sp_regen": 1, "damage": "1d3", "accuracy": 16, "description": "Kreis-Block mit kontrolliertem Konter, sammelt 1 SP"},
            {"id": "suiheigiri", "name": "Suiheigiri", "sp_cost": 2, "damage": "1d6+1", "accuracy": 13, "description": "Horizontaler Schwung in Hueftehoehe"},
            {"id": "tsubame_gaeshi", "name": "Tsubame-Gaeshi", "sp_cost": 3, "damage": "1d8+3", "accuracy": 10, "description": "Schwalben-Rueckschlag - riskant, aber heftig"},
            {"id": "heal", "name": "Heilen", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "self", "description": "Atemruhe + eigener Heiltee, heilt dich selbst"},
            {"id": "first_aid", "name": "Erste Hilfe", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "ally", "description": "Verband + schneller Heiltee, heilt Michael"},
        ],
    },
    "shotokan_brawler": {
        "name": "Shotokan-Brawler",
        "description": "Klassischer Karate-Mix: Geradeaus, hart, mit ikonischen Specials. Inspiriert von Ryu-Style.",
        "moves": [
            {"id": "jab", "name": "Jab", "sp_cost": 0, "damage": "1d4+1", "accuracy": 14, "description": "Schneller gerader Faustschlag"},
            {"id": "shoryuken", "name": "Shoryuken", "sp_cost": 2, "damage": "1d6+2", "accuracy": 12, "description": "Aufsteigender Drachenschlag"},
            {"id": "hadouken", "name": "Hadouken", "sp_cost": 2, "damage": "1d6+1", "accuracy": 13, "description": "Energiestoss aus beiden Handflaechen"},
            {"id": "tatsumaki", "name": "Tatsumaki", "sp_cost": 3, "damage": "1d8+1", "accuracy": 11, "description": "Wirbelnder Tornado-Kick"},
            {"id": "guard", "name": "Guard", "sp_cost": 0, "sp_regen": 1, "damage": "1d2", "accuracy": 16, "description": "Block mit kurzem Konter, regeneriert 1 SP"},
            {"id": "heal", "name": "Heilen", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "self", "description": "Verband + Schluck Wasser fuer dich selbst"},
            {"id": "first_aid", "name": "Erste Hilfe", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "ally", "description": "Erste-Hilfe-Set aus der Stoffstasche, heilt Yuki"},
        ],
    },
    "speed_kicker": {
        "name": "Speed-Kicker",
        "description": "Beinarbeit-Spezialistin mit Tempo-Vorteil. Inspiriert von Chun-Li-Style.",
        "moves": [
            {"id": "lightkick", "name": "Light-Kick", "sp_cost": 0, "damage": "1d4+1", "accuracy": 15, "description": "Schneller niedriger Tritt"},
            {"id": "spinning_bird", "name": "Spinning Bird", "sp_cost": 2, "damage": "1d6+1", "accuracy": 13, "description": "Wirbel-Tritt aus der Drehung"},
            {"id": "lightning_legs", "name": "Lightning Legs", "sp_cost": 2, "damage": "1d6+2", "accuracy": 12, "description": "Schnellfeuer-Trittserie"},
            {"id": "neck_breaker", "name": "Neck-Breaker", "sp_cost": 3, "damage": "1d8+2", "accuracy": 10, "description": "Sprung-Kombination zum Nacken"},
            {"id": "tactical_dodge", "name": "Tactical Dodge", "sp_cost": 0, "sp_regen": 1, "damage": "1d2", "accuracy": 16, "description": "Ausweichen mit Konter, regeneriert 1 SP"},
            {"id": "heal", "name": "Heilen", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "self", "description": "Verband + Schluck Wasser fuer dich selbst"},
            {"id": "first_aid", "name": "Erste Hilfe", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "ally", "description": "Erste-Hilfe-Set aus der Stoffstasche, heilt Yuki"},
        ],
    },
    "grappler": {
        "name": "Grappler",
        "description": "Naher Griff-Kampf mit schweren Wuerfen. Inspiriert von Zangief-Style.",
        "moves": [
            {"id": "headbutt", "name": "Headbutt", "sp_cost": 0, "damage": "1d4+2", "accuracy": 13, "description": "Stirn voraus - simpel und brutal"},
            {"id": "body_slam", "name": "Body-Slam", "sp_cost": 2, "damage": "1d6+3", "accuracy": 11, "description": "Niederwerfen mit voller Wucht"},
            {"id": "spinning_pile", "name": "Spinning Pile", "sp_cost": 3, "damage": "1d8+3", "accuracy": 9, "description": "Drehender Pile-Driver"},
            {"id": "double_lariat", "name": "Double Lariat", "sp_cost": 2, "damage": "1d6+1", "accuracy": 13, "description": "Beidarmiger Schwung - trifft auch durch Block"},
            {"id": "iron_guard", "name": "Iron Guard", "sp_cost": 0, "sp_regen": 1, "damage": "1d2", "accuracy": 17, "description": "Massiger Block, regeneriert 1 SP"},
            {"id": "heal", "name": "Heilen", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "self", "description": "Verband + Schluck Wasser fuer dich selbst"},
            {"id": "first_aid", "name": "Erste Hilfe", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "ally", "description": "Erste-Hilfe-Set aus der Stoffstasche, heilt Yuki"},
        ],
    },
    "charge_fighter": {
        "name": "Charge-Fighter",
        "description": "Militaerischer Stil mit Lade-Specials. Inspiriert von Guile-Style.",
        "moves": [
            {"id": "backhand", "name": "Backhand", "sp_cost": 0, "damage": "1d4+1", "accuracy": 14, "description": "Rueckhand-Schlag im Vorbeigehen"},
            {"id": "sonic_boom", "name": "Sonic Boom", "sp_cost": 2, "damage": "1d6+1", "accuracy": 13, "description": "Schallwellen-Wurf aus dem Stand"},
            {"id": "flash_kick", "name": "Flash-Kick", "sp_cost": 2, "damage": "1d6+2", "accuracy": 12, "description": "Aufsteigender Sturzflug-Tritt"},
            {"id": "knee_smash", "name": "Knee-Smash", "sp_cost": 3, "damage": "1d8+2", "accuracy": 10, "description": "Sprung-Knie mit voller Wucht"},
            {"id": "stance", "name": "Stance", "sp_cost": 0, "sp_regen": 1, "damage": "1d2", "accuracy": 16, "description": "Stehende Verteidigung, regeneriert 1 SP"},
            {"id": "heal", "name": "Heilen", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "self", "description": "Verband + Schluck Wasser fuer dich selbst"},
            {"id": "first_aid", "name": "Erste Hilfe", "sp_cost": 1, "damage": "1d6+2", "accuracy": 99, "kind": "heal", "target": "ally", "description": "Erste-Hilfe-Set aus der Stoffstasche, heilt Yuki"},
        ],
    },
}


# ===========================================================================
# Helpers
# ===========================================================================
def _extract_json(raw: str) -> Any:
    """Defensiv JSON aus LLM-Reply ziehen. Versucht direkt, dann ```json-Fence,
    dann erste {...}/[...]-Substring. None bei Fehlschlag.

    LLMs schreiben oft Vorwort ('Hier ist die JSON:') oder Markdown-Fences trotz
    expliziter Anweisung - der Parser muss tolerant sein, sonst muss bei jedem
    kleinen Stilfehler ein kompletter Re-Run laufen."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Markdown-Fence ```json ... ``` strippen
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Erste balancierte {...}- oder [...]-Substring
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = raw.find(open_ch)
        j = raw.rfind(close_ch)
        if i >= 0 and j > i:
            sub = raw[i:j + 1]
            try:
                return json.loads(sub)
            except json.JSONDecodeError:
                continue
    return None


_SLUG_BAD_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """display_name -> name-Feld (snake_case ASCII). Manifest-Loader erlaubt
    nur [a-z0-9_]+."""
    s = (name or "").lower()
    s = (s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
           .replace("ß", "ss").replace("é", "e").replace("è", "e"))
    s = _SLUG_BAD_RE.sub("_", s).strip("_")
    return s or "adventure"


def _unique_slug(base: str) -> str:
    """Konflikt-freier Slug: <base>, <base>_2, <base>_3, ..."""
    slug = base
    n = 2
    while (MANIFESTS_DIR / f"{slug}.json").exists():
        slug = f"{base}_{n}"
        n += 1
    return slug


def _call_llm(messages: list[dict], purpose: str, temperature: float = 0.85) -> str:
    """Wrapper um yc.chat_ollama mit think=True. Ein Slot fuer einen Pass.

    Setzt thinking explizit auf True (Default-Logik wuerde es nur bei aktiven
    Tools anschalten, aber wir haben hier keine Tools). qwen3-Thinking ist
    fuer die Architektur-Ueberlegung der Generation der entscheidende Hebel."""
    return yc.chat_ollama(
        messages,
        temperature=temperature,
        tools=None,
        purpose=purpose,
        think=True,
    )


# ===========================================================================
# Job-Klasse
# ===========================================================================
class GenerationJob:
    """In-memory State fuer einen Generation-Lauf.

    `events` ist die Event-Liste die der SSE-Stream konsumiert. `solution`
    bleibt im Server, wird nie an Frontend exponiert (das ist der Spoiler-
    Faecher den Yuki braucht aber Michael nicht wissen soll). `draft` enthaelt
    das vollstaendige Manifest fuer Save oder Discard. `preview` ist das
    spoiler-freie Subset das das Frontend im Preview-Schritt rendert."""

    def __init__(self, spec: dict):
        self.id = uuid.uuid4().hex[:12]
        self.spec = spec
        self.status = "pending"  # pending | running | done | error | cancelled
        self.created_at = time.time()
        self.events: list[dict] = []
        self.draft: dict | None = None
        self.preview: dict | None = None
        self.solution: dict | None = None  # Spoiler - nie an FE
        self.error: str | None = None
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()

    def emit(self, kind: str, **payload):
        """Event in die Stream-Queue legen. Wird vom SSE-Reader konsumiert.
        Server-Konsole bekommt auch eine Zeile, damit Verlauf mitlesbar ist."""
        ev = {"kind": kind, "t": round(time.time() - self.created_at, 1), **payload}
        with self.lock:
            self.events.append(ev)
        try:
            print(f"  [adv_gen {self.id} {kind}] "
                  + json.dumps(payload, ensure_ascii=False)[:200], flush=True)
        except Exception:
            pass

    def check_cancel(self):
        """Zwischen jedem Pass aufgerufen. Bricht den Job sauber ab wenn die
        User Cancel geklickt hat."""
        if self.cancel_event.is_set():
            raise _CancelledError()


class _CancelledError(Exception):
    pass


# ===========================================================================
# Pass 1: World-Skeleton (Locations + NPCs)
# ===========================================================================
def _pass_world_skeleton(job: GenerationJob) -> dict:
    spec = job.spec
    mode = spec["mode"]
    setting = spec.get("setting", "").strip()
    pitch = spec.get("pitch", "").strip()
    tone = spec.get("tone", "warm").strip()
    duration = spec.get("duration", "mittel")
    job.emit("pass_start", step="world_skeleton",
             label="Schauplatz und Personen erfinden ...",
             progress=1, total=_total_passes(mode))

    system = (
        "Du bist ein erfahrener Pen-and-Paper-Spielleiter und entwirfst die Welt "
        "fuer ein neues interaktives Erzaehl-Adventure auf Deutsch. Yuki "
        "(japanische Tutorin/Begleiterin von Michael) wird das Adventure spaeter "
        "spielen. Du bekommst Eckdaten und lieferst eine Welt-Skizze. "
        "Antworte AUSSCHLIESSLICH mit gueltigem JSON ohne Markdown-Fence, ohne "
        "Vorwort, ohne Kommentar. Alles auf Deutsch."
    )
    user = f"""Eckdaten:
- Schauplatz: {setting or '(frei waehlen, gerne in Yukis Heimat Sakyo-ku Kyoto wenn nichts dagegen spricht)'}
- Konflikt-Pitch in 1-2 Saetzen: {pitch}
- Ton: {tone}
- Geplante Spielzeit: {duration}
- Modus: {'Cozy-Story (kein Kampf, reines Detektiv/Ermittler-Spiel)' if mode == 'cozy_story' else 'Story-Hybrid (Story + gelegentliche Zufallskaempfe)'}

Erfinde:
- display_name: kurzer Spiel-Titel (max 8 Wort, deutsch, leicht poetisch)
- description: 1-2 Saetze fuer den Manifest-Picker (was ist der Pitch?)
- locations: 5-7 Orte als Liste. Jeder Ort braucht id (snake_case ascii, kurz), label (deutscher Anzeigename), atmosphere (1 Satz Beschreibung was man sieht/hoert/riecht). Inklusive einem klaren Startpunkt.
- npcs: 3-5 NPCs als Liste. Jeder NPC: name (japanisch oder deutsch, je nach Setting), age (int), role (kurz: "Cafe-Besitzerin", "Lieferant", "Gaertner"), personality (1 Satz).

Schreib so, dass es wie {setting or 'ein lebendiges Viertel'} riecht und klingt. Tone: {tone}.

JSON-Schema STRENG:
{{
  "display_name": "string",
  "description": "string",
  "locations": [
    {{"id": "snake_case_id", "label": "Anzeigename", "atmosphere": "string"}}
  ],
  "npcs": [
    {{"name": "string", "age": int, "role": "string", "personality": "string"}}
  ]
}}"""
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose="adventure_gen_world", temperature=0.9)
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"world_skeleton: kein JSON parsebar (Raw[:200]: {raw[:200]!r})")
    locations = data.get("locations") or []
    npcs = data.get("npcs") or []
    if not isinstance(locations, list) or len(locations) < 3:
        raise RuntimeError(f"world_skeleton: zu wenig Locations ({len(locations) if isinstance(locations, list) else 'kein list'})")
    if not isinstance(npcs, list) or len(npcs) < 2:
        raise RuntimeError(f"world_skeleton: zu wenig NPCs ({len(npcs) if isinstance(npcs, list) else 'kein list'})")
    # Defensive: jede Location braucht id + label
    for loc in locations:
        if not isinstance(loc, dict) or not loc.get("id") or not loc.get("label"):
            raise RuntimeError(f"world_skeleton: Location ohne id/label: {loc}")
    job.emit("pass_done", step="world_skeleton",
             note=f"{len(locations)} Orte / {len(npcs)} NPCs",
             preview_display_name=data.get("display_name", ""))
    return data


# ===========================================================================
# Pass 2: Solution-Architecture (HIDDEN)
# ===========================================================================
def _pass_solution(job: GenerationJob, world: dict) -> dict:
    spec = job.spec
    mode = spec["mode"]
    pitch = spec.get("pitch", "").strip()
    tone = spec.get("tone", "warm").strip()
    job.emit("pass_start", step="solution",
             label="Loesungs-Architektur entwerfen (versteckt) ...",
             progress=2, total=_total_passes(mode))

    loc_lines = "\n".join(f"- {loc['id']}: {loc['label']} ({loc.get('atmosphere','')})"
                          for loc in world["locations"])
    npc_lines = "\n".join(f"- {n['name']} ({n.get('age','?')}, {n.get('role','')}): {n.get('personality','')}"
                          for n in world["npcs"])

    system = (
        "Du bist ein erfahrener Spielleiter und planst die VERSTECKTE Loesungs-"
        "Architektur eines Cozy-Mystery-Adventures. Diese Infos sieht NUR der "
        "Spielleiter (du, im Spielverlauf Yuki). Der Spieler (Michael) darf "
        "die Loesung erst durch konkretes Handeln erfahren. Antworte "
        "AUSSCHLIESSLICH mit gueltigem JSON ohne Markdown, auf Deutsch."
    )
    user = f"""Welt-Skizze:
{world.get('display_name','')} - {world.get('description','')}

Orte:
{loc_lines}

NPCs:
{npc_lines}

Pitch: {pitch}
Ton: {tone}

Plane jetzt die VERSTECKTE Loesung. WICHTIG:
- Solution muss konkret und ortsverankert sein (welcher Ort/NPC haelt was zurueck?)
- 3-4 Hinweis-Spuren, verteilt auf verschiedene Orte/NPCs - Michael muss aktiv fragen damit sie sich ergeben
- 2-Schritt-Aufloesung: zuerst Hauptobjekt/Wissen FINDEN, dann eine Folge-Aktion (zurueckbringen, jemanden konfrontieren, etwas tun) bevor Spielende
- Pacing: wenn Michael 8-10 Runden ohne Spur ist, soll ein NPC einen klareren Hinweis fallen lassen

Schreib pragmatisch und konkret, keine Schwurbel-Phrasen. Schreib in 2.-Person-Spielleiter-Sicht ("Aiko-san weiss X").

JSON-Schema STRENG:
{{
  "solution_summary": "1-2 Saetze: was ist wirklich passiert, wer/was ist die Loesung",
  "key_object_or_truth": "das was Michael finden/erfahren muss (Schluessel, Brief, Aussage, Person)",
  "key_location_id": "id aus der Orte-Liste oben - wo liegt die Loesung",
  "clue_trail": [
    {{"clue": "kurze Beschreibung was Michael lernt", "found_at": "location_id oder npc_name", "trigger": "wodurch findet er es (z.B. 'wenn er Aiko nach dem Morgen fragt')"}}
  ],
  "wrap_up_step1_find": "wie sieht der Find-Moment aus (2-3 Saetze)",
  "wrap_up_step2_resolve": "wie sieht die Auflösung danach aus (3-4 Saetze, mit Reaktion eines NPC)",
  "fallback_hint": "EIN konkreter Hinweis den ein NPC unaufgefordert nach 8-10 Runden ohne Fortschritt fallen lassen kann",
  "win_condition_slug": "snake_case_label fuer das Sieg-Stichwort (z.B. 'find_key', 'return_hojicha')"
}}"""
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose="adventure_gen_solution", temperature=0.8)
    data = _extract_json(raw)
    if not isinstance(data, dict) or not data.get("solution_summary"):
        raise RuntimeError(f"solution: kein gueltiges JSON oder solution_summary fehlt (Raw[:200]: {raw[:200]!r})")
    if not isinstance(data.get("clue_trail"), list) or len(data["clue_trail"]) < 2:
        raise RuntimeError(f"solution: clue_trail braucht mind. 2 Eintraege")
    job.emit("pass_done", step="solution",
             note=f"{len(data['clue_trail'])} Hinweis-Spuren entworfen")
    return data


# ===========================================================================
# Pass 3a: Cozy-System-Rules (Single-LLM-Pfad, analog verschwundener_schluessel)
# ===========================================================================
def _pass_cozy_system_rules(job: GenerationJob, world: dict, solution: dict) -> str:
    spec = job.spec
    tone = spec.get("tone", "warm").strip()
    yuki_role = spec.get("yuki_role", "narrator")
    job.emit("pass_start", step="system_rules",
             label="Erzaehl-Regeln und Spoiler-Faecher fuer Yuki schreiben ...",
             progress=3, total=_total_passes(spec["mode"]))

    loc_block = "\n".join(f"- {loc['id']}: {loc['label']}" for loc in world["locations"])
    npc_block = "\n".join(f"- {n['name']} ({n.get('role','')})" for n in world["npcs"])
    clue_block = "\n".join(
        f"- {c.get('clue','')} (an {c.get('found_at','')}, wenn {c.get('trigger','')})"
        for c in solution.get("clue_trail", []))

    system = (
        "Du schreibst die system_rules-Sektion eines Cozy-Mystery-Manifests "
        "fuer Yuki. Sie ist Erzaehlerin (oder Begleiterin) eines interaktiven "
        "Spiels. Diese Regeln sieht NUR sie im Spielleitungs-Prompt - du gibst "
        "ihr Spoiler-Faecher, Hinweis-Verteilung, Pacing-Disziplin. "
        "Antworte AUSSCHLIESSLICH mit dem reinen Regel-Text auf Deutsch, kein "
        "JSON, kein Vorwort, kein Markdown-Code-Fence. Stil: pragmatisch, "
        "in der Du-Form an Yuki gerichtet, nutzt vorhandene Marker-Begriffe."
    )
    user = f"""Manifest: {world.get('display_name','')}
Ton: {tone}
Yukis Rolle (default): {yuki_role}

Welt-Orte (id: label):
{loc_block}

NPCs:
{npc_block}

LOESUNG (Spoiler-Faecher fuer Yuki):
- Was ist passiert: {solution.get('solution_summary','')}
- Was Michael finden/erfahren muss: {solution.get('key_object_or_truth','')}
- Wo es liegt: {solution.get('key_location_id','')}

Hinweis-Spuren:
{clue_block}

Fallback-Hinweis nach 8-10 Runden ohne Fortschritt:
{solution.get('fallback_hint','')}

Wrap-Up:
- Schritt 1 (Find-Moment): {solution.get('wrap_up_step1_find','')}
- Schritt 2 (Aufloesung danach): {solution.get('wrap_up_step2_resolve','')}

Schreib jetzt die system_rules. Bau folgende Bausteine ein (in dieser Reihenfolge):

1. SPIELABLAUF (1-2 Saetze): wie spielt sich das Adventure ab, was Yukis Rolle ist
2. SPOILER FUER DICH (Michael darf das NICHT direkt erfahren): die Loesung exakt
3. HINWEISE die Michael finden kann (Liste der Spuren mit Trigger)
4. AUFLOESUNG IN ZWEI SCHRITTEN (wichtig fuers Pacing - nicht in EINEM Turn abreissen!):
   - SCHRITT 1 - FIND-MOMENT
   - SCHRITT 2 - RESOLVE-MOMENT (mit ausdruecklichem "MINDESTENS 3-4 Saetze vor [adv_state:status:closed]")
5. WENN MICHAEL ZU LANGE STECKT: Fallback-Hinweis nach 8-10 Runden
6. MARKER die Yuki nutzen darf:
   - [adv_state:loc:NAME] bei Ortswechsel
   - [adv_state:item_add:NAME] fuer Hinweise/Items
   - [choice:A|...] [choice:B|...] an natuerlichen Verzweigungen
   - KEIN [roll:...] noetig (Detektivarbeit, nicht Kampf)
7. TON: {tone}, Cozy-Mystery - nichts Schlimmes ist passiert, alles geht gut aus

Stil-Vorbild ist die system_rules-Sektion von 'verschwundener_schluessel' (du kennst die aus dem System-Prompt). Halte dich an deren Struktur und Direktheit.

Laenge: ungefaehr 800-1500 Worte. Keine Markdown-Header (# oder ##), nur natuerliche Absaetze und WICHTIG-Aussagen in CAPS am Zeilenanfang."""
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose="adventure_gen_cozyrules", temperature=0.8)
    text = (raw or "").strip()
    # Markdown-Fence entfernen falls Modell trotz Anweisung einen einbaut
    text = re.sub(r"^```[a-z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    if len(text) < 400:
        raise RuntimeError(f"system_rules: Output zu kurz ({len(text)} Zeichen)")
    job.emit("pass_done", step="system_rules", note=f"{len(text)} Zeichen")
    return text


# ===========================================================================
# Pass 3b: DM-Voice (Hybrid-Pfad, anspruchsvollster Pass)
# ===========================================================================
def _pass_dm_voice(job: GenerationJob, world: dict, solution: dict) -> str:
    spec = job.spec
    tone = spec.get("tone", "warm").strip()
    job.emit("pass_start", step="dm_voice",
             label="DM-Erzaehlstimme und Spoiler-Faecher schreiben ...",
             progress=3, total=_total_passes(spec["mode"]))

    loc_block = "\n".join(f"- {loc['id']}: {loc['label']} - {loc.get('atmosphere','')}"
                          for loc in world["locations"])
    npc_block = "\n".join(
        f"- {n['name']} ({n.get('age','?')}, {n.get('role','')}, {n.get('personality','')})"
        for n in world["npcs"])
    clue_block = "\n".join(
        f"- {c.get('clue','')} (an {c.get('found_at','')}, wenn {c.get('trigger','')})"
        for c in solution.get("clue_trail", []))

    system = (
        "Du schreibst die dm_system_rules-Sektion eines Story-Hybrid-Adventures. "
        "Das ist die LAENGSTE und WICHTIGSTE Sektion des Manifests - der "
        "vollstaendige Prompt fuer den DM-LLM, der die Welt fuehrt. Stil-Vorbild "
        "ist die dm_system_rules von 'kyoto_hojicha_mystery' (du kennst die "
        "Struktur). Antworte AUSSCHLIESSLICH mit dem reinen Regel-Text auf "
        "Deutsch, kein JSON, kein Markdown-Fence. Du-Form an den DM."
    )
    user = f"""Manifest: {world.get('display_name','')}
Ton: {tone}

Welt-Orte:
{loc_block}

NPCs:
{npc_block}

LOESUNG (Spoiler-Faecher exklusiv fuer DM):
- Was ist passiert: {solution.get('solution_summary','')}
- Was Michael finden/erfahren muss: {solution.get('key_object_or_truth','')}
- Wo es liegt: {solution.get('key_location_id','')}

Hinweis-Spuren:
{clue_block}

Fallback-Hinweis nach 10-12 Runden ohne Fortschritt:
{solution.get('fallback_hint','')}

Wrap-Up:
- Schritt 1 (Find-Moment): {solution.get('wrap_up_step1_find','')}
- Schritt 2 (Aufloesung): {solution.get('wrap_up_step2_resolve','')}

Schreib jetzt die dm_system_rules. PFLICHT-Bausteine in dieser Reihenfolge:

1. IDENTITAET: "DU BIST DER DM dieses Spiels. Du bist NICHT Yuki, NICHT Michael - du bist die Welt." Yuki spielt in getrenntem LLM-Call, du steuerst sie NICHT.

2. SPOILER-FAECHER (nur DU weisst das, weder Michael noch Yuki haben Zugriff):
   - Die Loesung exakt wie oben
   - Welche Spuren wo zu finden sind (Liste der clue_trail-Eintraege)
   - Welche Encounter (Kampf-Situationen) IRRELEVANT vom Plot sind (z.B. zufaellige Stoerer)
   - Spielziel-Sequenz: Michael findet X, bringt es Y zurueck, [adv_state:status:closed]

3. ERZAEHL-STIMME:
   - 3. Person beobachtend
   - NPC-Dialog in direkter Rede mit Anfuehrungszeichen
   - NIE in 1. Person Yuki - das ist Yukis Slot
   - Du adressierst Michael in 2. Person wenn natuerlich
   - KEINE Meta-Kommentare, keine 4. Wand

4. DEINE MARKER (Werkzeugkasten):
   - [adv_state:loc:NAME] bei Ortswechsel - PFLICHT sonst weiss Engine nicht wo Encounter spawnen
   - [adv_state:item_add:NAME|Beschreibung] - HAUPT-Mechanik fuer Story-Progress. Beschreibung ist Detektiv-Notizbuch-Eintrag, mit Erklaerung was Michael sieht/lernt
   - [encounter:Name|HP|AC|dmg] - Zufallskampf, pro Gegner ein Marker, HP 6-20, AC 10-14, dmg 1d3 bis 1d6+1
   - [choice:LABEL|Text] - 2-4 Click-Cards an narrativen Verzweigungen
   - [roll:skill|adv/normal/dis] - sparsam, story-driven
   - [adv_state:status:closed] - NUR am absoluten Spielende

5. MARKER-VERBOTE:
   - KEIN [move:...] (Yukis Slot)
   - KEIN [adv_state:actor:...] und KEIN [adv_state:threat:...] (Engine macht HP-Mutation automatisch)
   - KEIN [heart:...], [note:...], [timer:...], [event:...], [keepsake:...] (Real-Memory-Marker)

6. BALL-ZURUECK in jedem Story-Reply: solange Story-Modus laeuft, endet JEDER Reply mit offener Frage ODER 2-4 [choice:...]-Markern. Niemals Fakt-Schluss.

7. IM KAMPF (mode=combat): KEINE [choice:...] und KEINE neuen [encounter:...]. KURZ bleiben (1-2 Saetze Atmosphaere). Action gehoert Yuki + Engine. Erst nach combat_cleared wieder als Erzaehler.

8. KRITISCHE PACING-DISZIPLIN:
   - IM ERSTEN REPLY: KEINE Spoiler, KEIN [encounter:...]. NUR 1-2 Saetze Welt-Stimmung + offene Frage + 2-4 [choice:...]-Cards.
   - WAEHREND SPIEL: Hinweise nur durch konkretes Handeln. NIE unaufgefordert in den Weg.
   - Nach 10-12 Runden ohne Fortschritt: EINEN Fallback-Hinweis fallen lassen ({solution.get('fallback_hint','')})

9. KAMPF-EROEFFNUNG: an passenden Orten [encounter:Name|HP|AC|dmg]. KEIN Encounter an sicheren Zonen ({solution.get('key_location_id','')} und Startpunkt sind sicher). KEIN Encounter im 1. Reply. Kurzer Auftakt-Moment, KEIN choice-Block direkt nach Encounter.

10. COMBAT-WRAP (combat_cleared - eigener DM-Call): 2-4 Saetze 'die Stille kehrt zurueck' + Ball zurueck. KEIN [adv_state:status:closed] beim Combat-Wrap.

11. AUFLOESUNG (Story-Win, ZWEI Schritte):
    1. SCHRITT - FINDEN: {solution.get('wrap_up_step1_find','')}. NOCH NICHT [adv_state:status:closed]. Setze [adv_state:item_add:...] mit Beschreibung. Lass Michael selbst entscheiden was als naechstes.
    2. SCHRITT - AUFLOESUNG: in der NAECHSTEN Runde. {solution.get('wrap_up_step2_resolve','')}. MINDESTENS 3-4 Saetze Wrap-Up. ERST DANN [adv_state:status:closed].

12. TON: {tone}, mit Lokalfarbe passend zum Setting.

Schreib pragmatisch, direkt, mit konkreten Beispielen wo hilfreich. Laenge: 1800-3500 Worte. Keine Markdown-Header. Verwende '\\n\\n' fuer Absatztrennung."""
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose="adventure_gen_dmvoice", temperature=0.8)
    text = (raw or "").strip()
    text = re.sub(r"^```[a-z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    if len(text) < 1200:
        raise RuntimeError(f"dm_voice: Output zu kurz ({len(text)} Zeichen, mind. 1200 noetig)")
    job.emit("pass_done", step="dm_voice", note=f"{len(text)} Zeichen")
    return text


# ===========================================================================
# Pass 4: Encounter-Hints (Hybrid only)
# ===========================================================================
def _pass_encounter_hints(job: GenerationJob, world: dict, solution: dict) -> list[str]:
    spec = job.spec
    tone = spec.get("tone", "warm").strip()
    job.emit("pass_start", step="encounter_hints",
             label="Zufalls-Kampf-Vorschlaege schreiben ...",
             progress=4, total=_total_passes(spec["mode"]))

    loc_block = "\n".join(f"- {loc['id']}: {loc['label']} ({loc.get('atmosphere','')})"
                          for loc in world["locations"])

    system = (
        "Du erfindest Encounter-Hints fuer ein Story-Hybrid-Adventure. Das sind "
        "Vorschlaege fuer Zufalls-Kaempfe, die der DM frei abwandeln darf. "
        "Stat-Range: HP 6-20, AC 10-14, dmg 1d3 bis 1d6+1. Antworte mit reiner "
        "JSON-Array (Liste von Strings), kein Markdown, auf Deutsch."
    )
    user = f"""Welt: {world.get('display_name','')}
Ton: {tone}
Orte:
{loc_block}

Sichere Zone (kein Encounter): {solution.get('key_location_id','(Startpunkt)')}
Loesungs-Ort (haerter): {solution.get('key_location_id','')}

Schreib 3-5 Encounter-Hints. Jeder Hint ist EIN String mit Format:
"NAME (HP X / HP Y..., AC Z, dmg WdW) - Kontext wo/warum"

Beispiele aus 'kyoto_hojicha_mystery' (Stil-Vorbild):
- "Raeuber-Trio (Magerer 10 HP / Stiernackiger 16 HP / Schneller 12 HP, AC 11-13, dmg 1d4 bis 1d4+1) - betrunken im Hinterhof oder am Markt"
- "Streunende Hunde (6-8 HP, AC 13, dmg 1d3) - 2-3 Stueck am Lager"

Mind. ein Encounter unabhaengig vom Plot (zufaellige Stoerer), mind. einer plot-relevant (am Loesungs-Ort).

JSON-Schema:
["string", "string", ...]"""
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose="adventure_gen_encounters", temperature=0.85)
    data = _extract_json(raw)
    if not isinstance(data, list) or len(data) < 2:
        raise RuntimeError(f"encounter_hints: kein JSON-Array oder zu wenig ({raw[:200]!r})")
    hints = [str(h).strip() for h in data if str(h).strip()]
    job.emit("pass_done", step="encounter_hints", note=f"{len(hints)} Hints")
    return hints


# ===========================================================================
# Pass 5: Opener + Yuki-View + Win-Hint
# ===========================================================================
def _pass_opener_view(job: GenerationJob, world: dict, solution: dict, mode: str) -> dict:
    spec = job.spec
    tone = spec.get("tone", "warm").strip()
    yuki_role = spec.get("yuki_role", "narrator" if mode == "cozy_story" else "companion")
    pass_idx = 4 if mode == "cozy_story" else 5
    job.emit("pass_start", step="opener_view",
             label="Eroeffnungs-Szene und Yukis Perspektive schreiben ...",
             progress=pass_idx, total=_total_passes(mode))

    loc_block = "\n".join(f"- {loc['id']}: {loc['label']}" for loc in world["locations"])
    npc_block = "\n".join(f"- {n['name']} ({n.get('role','')})" for n in world["npcs"])
    start_loc = world["locations"][0]["id"]

    system = (
        "Du schreibst die Eroeffnungs- und Yuki-Sicht-Sektionen eines neuen "
        "Adventure-Manifests. Antworte AUSSCHLIESSLICH mit gueltigem JSON ohne "
        "Markdown, auf Deutsch."
    )
    yuki_view_clause = (
        "- yuki_initial_view: 2-3 Saetze - was weiss Yuki am Spielstart? "
        "WICHTIG: Yuki hat KEINEN Zugriff auf den Spoiler-Faecher. Sie kennt "
        "Setting und Kontext (z.B. 'Aiko-san hat um Hilfe gebeten, Lieferung "
        "verschwunden, ihr brecht von Yukis Apartment auf'), aber NICHT die "
        "Loesung. KEINE Yakuza-/Schuldige-Erwaehnung, KEINE Hinweise wo "
        "die Loesung liegt - das wuerde den Spoiler-Schutz brechen.\n"
        if mode == "story_hybrid" else ""
    )
    user = f"""Manifest: {world.get('display_name','')}
Ton: {tone}
Yukis Rolle (Default): {yuki_role}
Mode: {mode}

Orte (Startpunkt zuerst):
{loc_block}

NPCs:
{npc_block}

Spoiler (versteckt, NICHT in initial_engine_msg + yuki_initial_view erwaehnen):
- Solution: {solution.get('solution_summary','')}
- Win-Slug: {solution.get('win_condition_slug','')}

Schreib:
- initial_engine_msg: 2-4 Saetze Szene-Auftakt. Setzt die Atmosphaere, fuehrt den Konflikt ein (ohne Spoiler), endet mit dem Startpunkt {start_loc} und einem klaren "Michael, du bist zuerst dran"-Cue.
{yuki_view_clause}- win_condition: kurzer snake_case-Slug fuer das Sieg-Ereignis (z.B. 'find_key', 'return_hojicha', 'expose_thief')
- win_hint: 1-2 Saetze - wann hat Michael gewonnen, was passiert bei Loss

JSON-Schema STRENG:
{{
  "initial_engine_msg": "string",
  {'"yuki_initial_view": "string",' if mode == 'story_hybrid' else ''}
  "win_condition": "snake_case_slug",
  "win_hint": "string"
}}"""
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose="adventure_gen_opener", temperature=0.85)
    data = _extract_json(raw)
    if not isinstance(data, dict) or not data.get("initial_engine_msg"):
        raise RuntimeError(f"opener: kein gueltiges JSON oder initial_engine_msg fehlt")
    if mode == "story_hybrid" and not data.get("yuki_initial_view"):
        # Hybrid braucht yuki_initial_view - falls Modell vergisst, generieren wir was Generisches.
        data["yuki_initial_view"] = (
            f"Ihr seid in {world['locations'][0]['label']} und brecht gleich auf. "
            f"Du kennst das Viertel aus dem Alltag, aber von dem aktuellen "
            f"Problem weisst du nicht mehr als Michael."
        )
        job.emit("note", text="yuki_initial_view fehlte - Fallback eingesetzt")
    job.emit("pass_done", step="opener_view",
             note=f"win_condition={data.get('win_condition','')}")
    return data


# ===========================================================================
# Pass 6: Self-Critique (Konsistenz-Check)
# ===========================================================================
def _pass_self_critique(job: GenerationJob, draft: dict, solution: dict) -> dict:
    mode = job.spec["mode"]
    pass_idx = 5 if mode == "cozy_story" else 6
    job.emit("pass_start", step="critique",
             label="Konsistenz-Check - findet das Adventure Loecher? ...",
             progress=pass_idx, total=_total_passes(mode))

    # Subset des Drafts: lange Prosa kuerzen, sonst wird der Critique-Prompt zu lang
    short_draft = {
        "name": draft.get("name"),
        "display_name": draft.get("display_name"),
        "description": draft.get("description"),
        "win_condition": draft.get("win_condition"),
        "world_brief": (draft.get("world_brief", "") or "")[:800],
        "dm_system_rules_or_system_rules":
            (draft.get("dm_system_rules") or draft.get("system_rules") or "")[:1500],
        "yuki_initial_view": draft.get("yuki_initial_view"),
        "initial_engine_msg": draft.get("initial_engine_msg"),
        "encounter_hints": draft.get("encounter_hints"),
        "win_hint": draft.get("win_hint"),
    }
    system = (
        "Du bist ein erfahrener Lektor und prueft ein Adventure-Manifest auf "
        "innere Konsistenz. Antworte AUSSCHLIESSLICH mit gueltigem JSON."
    )
    user = f"""Pruef-Auftrag: hat das folgende Adventure-Manifest Konsistenz-Loecher?

Manifest (Auszug):
{json.dumps(short_draft, ensure_ascii=False, indent=2)}

Loesungs-Faecher (verstecktes Ground-Truth):
{json.dumps(solution, ensure_ascii=False, indent=2)}

Pruef-Kriterien:
1. Werden alle Locations konsistent referenziert? (kein Ort in dm_rules/system_rules der nicht in der Welt-Skizze existiert)
2. Werden alle Hinweis-NPCs in dm_rules/system_rules erwaehnt?
3. Erwaehnt initial_engine_msg oder yuki_initial_view den Spoiler? (sollte NICHT - das waere ein Leak)
4. Passt die Encounter-Liste zu den Locations?
5. Ist die 2-Schritt-Aufloesung klar formuliert?

Wenn alles OK: {{"ok": true, "issues": []}}
Wenn Probleme: {{"ok": false, "issues": ["kurze Beschreibung pro Problem"]}}

JSON-Schema STRENG:
{{"ok": bool, "issues": ["string", ...]}}"""
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose="adventure_gen_critique", temperature=0.3)
    data = _extract_json(raw)
    if not isinstance(data, dict):
        # Critique-Fehler nicht hart fail - lieber als "ok mit Hinweis" behandeln
        data = {"ok": False, "issues": ["Critique-Pass konnte JSON nicht parsen"]}
    job.emit("pass_done", step="critique",
             ok=bool(data.get("ok")),
             issues=list(data.get("issues") or []))
    return data


# ===========================================================================
# Assemble: Pass-Outputs zu finalem Manifest-Dict mergen
# ===========================================================================
def _build_world_brief(world: dict) -> str:
    """world_brief-Block analog kyoto_hojicha_mystery: Locations + NPCs als
    Strukturierter Text-Block."""
    lines = []
    loc_label = world["locations"][0]["label"] if world.get("locations") else ""
    setting_first = world.get("locations", [{}])[0].get("atmosphere", "")
    if setting_first:
        lines.append(setting_first)
    lines.append("")
    lines.append("Die Locations (alle in Gehweite):")
    for loc in world["locations"]:
        atmo = loc.get("atmosphere", "")
        lines.append(f"- {loc['id']}: {loc['label']}"
                     + (f" - {atmo}" if atmo else ""))
    lines.append("")
    lines.append("NPCs:")
    for n in world["npcs"]:
        lines.append(f"- {n['name']} ({n.get('age','?')}, "
                     f"{n.get('personality','').strip()}) - {n.get('role','')}")
    return "\n".join(lines)


def _assemble_cozy(world: dict, solution: dict, system_rules: str,
                    opener: dict, spec: dict) -> dict:
    """Cozy-Story-Manifest zusammenbauen. Stil-Vorbild: verschwundener_schluessel."""
    display_name = world.get("display_name", "Neues Adventure")
    yuki_role_default = spec.get("yuki_role") or "narrator"
    return {
        "name": _slugify(display_name),
        "display_name": display_name,
        "description": world.get("description", "").strip(),
        "yuki_role_default": yuki_role_default,
        "yuki_role_allowed": ["narrator", "companion"],
        "tones_allowed": ["warm", "humorvoll", "spannend"],
        "win_condition": opener.get("win_condition", solution.get("win_condition_slug", "win")),
        "initial_engine_msg": opener.get("initial_engine_msg", "").strip(),
        "world_brief": _build_world_brief(world),
        "system_rules": system_rules.strip(),
        "win_hint": opener.get("win_hint", "").strip(),
        "stats_layout": {},
        "moves": [],
    }


def _assemble_hybrid(world: dict, solution: dict, dm_rules: str,
                      hints: list[str], opener: dict, spec: dict) -> dict:
    """Story-Hybrid-Manifest zusammenbauen. Stil-Vorbild: kyoto_hojicha_mystery."""
    display_name = world.get("display_name", "Neues Adventure")
    yuki_role_default = spec.get("yuki_role") or "companion"
    return {
        "name": _slugify(display_name),
        "display_name": display_name,
        "description": world.get("description", "").strip(),
        "yuki_role_default": yuki_role_default,
        "yuki_role_allowed": ["companion", "narrator"],
        "tones_allowed": ["warm", "humorvoll", "spannend"],
        "mode_default": "story",
        "win_condition": opener.get("win_condition", solution.get("win_condition_slug", "win")),
        "dm_llm_enabled": True,
        "starting_hp": _HYBRID_STARTING_HP,
        "starting_sp": _HYBRID_STARTING_SP,
        "yuki_character_id": _HYBRID_YUKI_CHAR_ID,
        "michael_character_default": _HYBRID_MICHAEL_DEFAULT,
        "characters": _HYBRID_CHAR_POOL,
        "encounter_hints": hints,
        "initial_engine_msg": opener.get("initial_engine_msg", "").strip(),
        "yuki_initial_view": opener.get("yuki_initial_view", "").strip(),
        "world_brief": _build_world_brief(world),
        "dm_system_rules": dm_rules.strip(),
        "win_hint": opener.get("win_hint", "").strip(),
        "stats_layout": {},
        "moves": [],
    }


# ===========================================================================
# Validation
# ===========================================================================
_REQ_COMMON = ("name", "display_name", "description", "yuki_role_default",
               "win_condition", "initial_engine_msg", "world_brief", "win_hint")
_REQ_COZY = _REQ_COMMON + ("system_rules",)
_REQ_HYBRID = _REQ_COMMON + ("dm_system_rules", "yuki_initial_view",
                              "encounter_hints", "characters",
                              "yuki_character_id", "michael_character_default")

# Japanische Honorifics die im Generation-Output regelmaessig auftauchen.
# Treffer auf "<Name>-<honorific>" werden gegen das NPC-Set abgeglichen -
# wenn der Name dort fehlt, ist es vermutlich ein Few-Shot-Leak aus einem
# anderen Manifest (z.B. Aiko-san aus kyoto_hojicha_mystery der in einem
# Night-City-Adventure NIX zu suchen hat).
_NPC_HONORIFIC_RE = re.compile(
    r"\b([A-ZÄÖÜ][a-zäöüß]+)-(?:san|chan|kun|sama|sensei|oba|obaa|oji|ojii|tan)\b"
)
# Snake-case-IDs in safe-zone-Passagen ausfindig machen.
_SNAKE_ID_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
# Trigger-Worte fuer "X ist Safe-Zone"-Passagen im dm_system_rules.
_SAFE_ZONE_TRIGGER_RE = re.compile(
    r"(?:sichere?\s+Zone[ns]?|KEIN\s+Encounter|KEIN\s+Kampf|keine?\s+Begegnung|sicheren?\s+Zone)",
    re.IGNORECASE,
)


def _npc_base_name(full_name: str) -> str:
    """'Aiko-san' -> 'aiko'; 'Hatsue-Oba' -> 'hatsue'; 'Tanaka' -> 'tanaka'.
    Aliasing fuer den NPC-Cross-Reference-Check."""
    return re.split(r"[-\s]", (full_name or "").strip())[0].lower()


def _extract_safe_zone_ids(dm_text: str) -> set[str]:
    """Holt snake_case-Location-IDs aus Passagen rund um Safe-Zone-Triggerworte.
    Window: 60 Zeichen vor und 220 nach dem Match, das deckt 'X und Y sind
    sichere Zonen' und 'KEIN Encounter im X' beide ab."""
    safe: set[str] = set()
    for m in _SAFE_ZONE_TRIGGER_RE.finditer(dm_text or ""):
        start = max(0, m.start() - 60)
        end = min(len(dm_text), m.end() + 220)
        passage = dm_text[start:end]
        for loc_m in _SNAKE_ID_RE.finditer(passage):
            safe.add(loc_m.group(1).lower())
    return safe


def _validate_draft(draft: dict, mode: str,
                     world: dict | None = None,
                     solution: dict | None = None) -> list[str]:
    """Regelbasierte Validation. Issues zurueck, leere Liste = OK.

    Wird VOR dem Save laufen plus auch im Preview angezeigt. world+solution
    werden vom _run_job durchgereicht damit Cross-Reference-Checks
    (NPC-Konsistenz, Safe-Zone-Encounter) moeglich sind. Beides optional -
    ohne world werden die Cross-Checks gskippt (Tests koennen so simple
    drafts ohne world-Kontext bauen)."""
    issues: list[str] = []
    req = _REQ_COZY if mode == "cozy_story" else _REQ_HYBRID
    for key in req:
        if not draft.get(key):
            issues.append(f"Pflichtfeld fehlt: {key}")
    # Slug-Convention
    name = draft.get("name", "")
    if name and not re.match(r"^[a-z0-9_]+$", name):
        issues.append(f"name '{name}' enthaelt nicht-erlaubte Zeichen (nur a-z0-9_)")
    # Hybrid-spezifisch: yuki_character_id muss im pool sein
    if mode == "story_hybrid":
        pool = draft.get("characters") or {}
        yc_id = draft.get("yuki_character_id")
        if yc_id and yc_id not in pool:
            issues.append(f"yuki_character_id '{yc_id}' nicht im characters-Pool")
        mc_def = draft.get("michael_character_default")
        if mc_def and mc_def not in pool:
            issues.append(f"michael_character_default '{mc_def}' nicht im characters-Pool")

    # --- Cross-Reference-Checks (brauchen world + solution) ---------------
    if world:
        npc_list = world.get("npcs") or []

        # (1) Yuki / Michael duerfen nicht in der NPC-Liste auftauchen -
        # sie sind Player-Chars. Anker-Case: Cyberpunk-Generation hatte
        # 'Yuki (24, Sanftmuetig...)' als NPC dabei.
        for n in npc_list:
            base = _npc_base_name(n.get("name", ""))
            if base in ("yuki", "michael"):
                issues.append(
                    f"'{n.get('name')}' ist Player-Char, sollte nicht in der NPC-Liste sein"
                )

        # (2) NPC-Cross-Reference: jeder '<Name>-san' (oder andere Honorifics)
        # in den User-/DM-facing-Texten muss in der NPC-Liste vorhanden sein.
        # Anker-Case: Aiko-san leakte aus kyoto_hojicha_mystery-Few-Shot in
        # eine Night-City-Generation in der Aiko-san nicht existiert.
        canonical = {_npc_base_name(n.get("name", "")) for n in npc_list}
        canonical.discard("")
        canonical |= {"yuki", "michael"}  # immer als bekannt zaehlen
        check_text = " ".join(filter(None, [
            draft.get("dm_system_rules"),
            draft.get("system_rules"),
            draft.get("yuki_initial_view"),
            draft.get("initial_engine_msg"),
            draft.get("description"),
            draft.get("win_hint"),
        ]))
        seen_leaks: set[str] = set()
        for m in _NPC_HONORIFIC_RE.finditer(check_text):
            base = m.group(1).lower()
            if base not in canonical and base not in seen_leaks:
                seen_leaks.add(base)
                issues.append(
                    f"NPC '{m.group(0)}' wird im Text erwaehnt, existiert "
                    f"aber nicht im world_brief NPC-Set "
                    f"(vermutlich Few-Shot-Leak aus anderem Manifest)"
                )

    # (3) Safe-Zone-Violation: encounter_hints duerfen keine Locations
    # nennen die im dm_system_rules als sichere Zone deklariert sind.
    # Anker-Case: Cyberpunk-Generation hatte Schattenwaechter-Encounter
    # im massage_salon (= Solution-Location und Safe-Zone laut DM-Rules).
    if mode == "story_hybrid":
        dm = draft.get("dm_system_rules", "") or ""
        hints = draft.get("encounter_hints") or []
        if dm and hints:
            safe_ids = _extract_safe_zone_ids(dm)
            # Label-Map zum cross-Matchen, weil Hints meist mit Labels arbeiten
            id_to_label: dict[str, str] = {}
            for loc in (world or {}).get("locations", []) or []:
                lid = (loc.get("id") or "").lower()
                llabel = (loc.get("label") or "").lower()
                if lid:
                    id_to_label[lid] = llabel
            for hint in hints:
                hint_lower = (hint or "").lower()
                matched: list[str] = []
                for sid in safe_ids:
                    if sid in hint_lower:
                        matched.append(sid)
                        continue
                    label = id_to_label.get(sid, "")
                    if label and label in hint_lower:
                        matched.append(sid)
                if matched:
                    issues.append(
                        f"Encounter-Hint nennt Safe-Zone {sorted(set(matched))}: "
                        f"'{(hint or '')[:80]}'"
                    )

    return issues


# ===========================================================================
# Worker-Thread + Pipeline-Orchestration
# ===========================================================================
def _total_passes(mode: str) -> int:
    return 5 if mode == "cozy_story" else 6


def _run_job(job: GenerationJob):
    """Worker-Funktion fuer den Background-Thread. Sequentielle Pass-Aufrufe
    mit Cancel-Check zwischen jedem Pass."""
    try:
        with job.lock:
            job.status = "running"
        mode = job.spec["mode"]

        world = _pass_world_skeleton(job)
        job.check_cancel()

        solution = _pass_solution(job, world)
        with job.lock:
            job.solution = solution
        job.check_cancel()

        if mode == "story_hybrid":
            dm_rules = _pass_dm_voice(job, world, solution)
            job.check_cancel()
            hints = _pass_encounter_hints(job, world, solution)
            job.check_cancel()
            opener = _pass_opener_view(job, world, solution, mode)
            job.check_cancel()
            draft = _assemble_hybrid(world, solution, dm_rules, hints, opener, job.spec)
        else:  # cozy_story
            sys_rules = _pass_cozy_system_rules(job, world, solution)
            job.check_cancel()
            opener = _pass_opener_view(job, world, solution, mode)
            job.check_cancel()
            draft = _assemble_cozy(world, solution, sys_rules, opener, job.spec)

        # Slug konfliktfrei machen (vor Critique - dann zeigt Critique echten Namen)
        base_slug = _slugify(draft.get("display_name", "adventure"))
        draft["name"] = _unique_slug(base_slug)

        # Self-Critique (failt nicht hart, nur informativ)
        try:
            critique = _pass_self_critique(job, draft, solution)
        except Exception as e:
            critique = {"ok": False, "issues": [f"Critique-Crash: {e}"]}
            job.emit("note", text=f"Critique-Pass ist gecrasht: {e}")

        # Regelbasierte Validation (mit world+solution fuer Cross-Reference-Checks)
        rule_issues = _validate_draft(draft, mode, world=world, solution=solution)

        preview = {
            "display_name": draft.get("display_name"),
            "description": draft.get("description"),
            "name_slug": draft.get("name"),
            "mode": mode,
            "yuki_role_default": draft.get("yuki_role_default"),
            "win_condition": draft.get("win_condition"),
            "location_count": len(world.get("locations", [])),
            "npc_count": len(world.get("npcs", [])),
            "encounter_count": len(draft.get("encounter_hints", [])),
            "has_dm_llm": bool(draft.get("dm_llm_enabled")),
            "has_combat": bool(draft.get("characters")),
            "critique_ok": bool(critique.get("ok")),
            "critique_issues": list(critique.get("issues") or []),
            "validation_issues": rule_issues,
            # Vorschau-Auszuege: kurze Cues, KEIN dm_system_rules / system_rules
            "initial_engine_msg": (draft.get("initial_engine_msg") or "")[:600],
            "yuki_initial_view": (draft.get("yuki_initial_view") or "")[:600],
            "encounter_hints": list(draft.get("encounter_hints", []))[:6],
            "locations_preview": [
                {"id": loc["id"], "label": loc["label"]}
                for loc in world.get("locations", [])
            ],
            "npcs_preview": [
                {"name": n["name"], "role": n.get("role", "")}
                for n in world.get("npcs", [])
            ],
        }

        with job.lock:
            job.draft = draft
            job.preview = preview
            job.status = "done"
        job.emit("done", preview=preview)

    except _CancelledError:
        with job.lock:
            job.status = "cancelled"
        job.emit("cancelled")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        with job.lock:
            job.status = "error"
            job.error = msg
        job.emit("error", error=msg)


# ===========================================================================
# Public-API: Job starten, abfragen, abbrechen, speichern
# ===========================================================================
def start_generation(spec: dict) -> str:
    """Spec validieren, Thread starten, job_id zurueck.

    spec-Struktur:
      mode: "cozy_story" | "story_hybrid"
      setting: str (Schauplatz, frei)
      pitch: str (Konflikt-Pitch, 1-2 Saetze, KEINE Loesung)
      tone: str ("warm" / "humorvoll" / "spannend" / ...)
      yuki_role: "narrator" | "companion" (optional, mode-spezifischer Default)
      duration: "kurz" | "mittel" | "lang" (optional)
      display_name: str (optional, sonst wird LLM einen erfinden)
    """
    if not isinstance(spec, dict):
        raise ValueError("spec muss dict sein")
    mode = spec.get("mode")
    if mode not in ("cozy_story", "story_hybrid"):
        raise ValueError(f"unbekannter mode: {mode!r} (cozy_story | story_hybrid)")
    pitch = (spec.get("pitch") or "").strip()
    if len(pitch) < 8:
        raise ValueError("pitch zu kurz (mind. 8 Zeichen, was ist der Konflikt?)")
    spec = dict(spec)
    spec["pitch"] = pitch
    spec["setting"] = (spec.get("setting") or "").strip()
    spec["tone"] = (spec.get("tone") or "warm").strip()
    if not spec.get("yuki_role"):
        spec["yuki_role"] = "narrator" if mode == "cozy_story" else "companion"
    if not spec.get("duration"):
        spec["duration"] = "mittel"

    _gc_stale_jobs()
    job = GenerationJob(spec)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    thread = threading.Thread(target=_run_job, args=(job,),
                              daemon=True, name=f"AdvGen-{job.id}")
    thread.start()
    job.emit("queued", spec={k: v for k, v in spec.items() if k != "spec"})
    return job.id


def get_job(job_id: str) -> GenerationJob | None:
    return _JOBS.get(job_id)


def cancel_job(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job:
        return False
    job.cancel_event.set()
    return True


def save_job_manifest(job_id: str) -> str:
    """Draft als config/adventures/<slug>.json schreiben. Dry-Run via
    adventure_engine.load_manifest. Returns endgueltigen slug."""
    job = _JOBS.get(job_id)
    if not job:
        raise ValueError(f"job '{job_id}' nicht gefunden")
    with job.lock:
        if job.status != "done" or not job.draft:
            raise ValueError(f"job '{job_id}' nicht fertig (status={job.status})")
        draft = dict(job.draft)

    # Slug nochmal pruefen - waehrend Generation koennte parallel ein anderes
    # Manifest mit gleichem Namen gespeichert worden sein
    if (MANIFESTS_DIR / f"{draft['name']}.json").exists():
        base_slug = _slugify(draft.get("display_name", "adventure"))
        draft["name"] = _unique_slug(base_slug)
    slug = draft["name"]
    path = MANIFESTS_DIR / f"{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    yc._atomic_write_text(path, json.dumps(draft, ensure_ascii=False, indent=2))

    # Dry-Run: load_manifest darf nicht crashen
    try:
        adventure_engine.load_manifest(slug)
    except Exception as e:
        # Geschriebenes Manifest lassen wir bewusst liegen damit User reviewen
        # kann - aber Save-Endpoint meldet Fehler.
        raise RuntimeError(f"Manifest geschrieben aber load_manifest crashed: {e}")

    job.emit("saved", slug=slug, path=str(path))
    return slug


def list_jobs() -> list[dict]:
    """Diagnose-Helper - Job-Stati ueberblicken. Frontend nutzt das aktuell nicht."""
    with _JOBS_LOCK:
        return [{"id": j.id, "status": j.status,
                  "mode": j.spec.get("mode"),
                  "display_name": (j.draft or {}).get("display_name") or (j.preview or {}).get("display_name"),
                  "age_s": round(time.time() - j.created_at, 0)}
                 for j in _JOBS.values()]


def _gc_stale_jobs():
    """Abgelaufene Jobs aus dem Dict raeumen. Wird beim Start jedes neuen Jobs
    aufgerufen, keine separate Background-Aufgabe."""
    now = time.time()
    with _JOBS_LOCK:
        stale = [jid for jid, job in _JOBS.items()
                 if now - job.created_at > _JOB_TTL_S]
        for jid in stale:
            _JOBS.pop(jid, None)
