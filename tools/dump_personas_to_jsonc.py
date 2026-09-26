"""
dump_personas_to_jsonc.py
============================================================
Einmal-Skript zur Migration: nimmt die aktuelle PERSONAS-Dict aus
yuki_core.py + PERSONA_LIGHTS / PERSONA_GRADIENTS aus web/index.html
und schreibt sie als config/personas.jsonc raus.

Zweck: vermeidet Hand-Escape-Fehler bei multi-zeiligen system-Strings
und Few-Shot-Listen. Das Ergebnis ist die Basis-Datei, die der User
danach editieren kann (enabled-Flags, neue Personas wie Sekretaerin
manuell hinzufuegen).

Nutzung:
    .\\.venv\\Scripts\\python.exe tools\\dump_personas_to_jsonc.py
    -> schreibt config/personas.jsonc.generated (sicherer Default,
       damit nichts ueberschrieben wird; manuell umbenennen).

Wenn das Skript spaeter nochmal genutzt werden soll (z.B. weil
Personas-Schema sich aendert): einfach laufen lassen, danach diff
gegen das Live-personas.jsonc machen.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from yuki_core import PERSONAS, GERMAN_PERSONAS, PERSONA_AUTO_BLOCKLIST  # noqa: E402


INDEX_HTML = ROOT / "web" / "index.html"


def parse_persona_lights():
    """PERSONA_LIGHTS aus web/index.html ziehen (per Regex - JS-Parser-Overkill).
    Format: persona_key: { hemi: {...}, dir: {...}, rim: {...} }
    Hex-Farben (0xRRGGBB) werden als Hex-Strings im JSONC abgelegt - das ist
    lesbarer und das Frontend kann sie weiter mit THREE.Color(0xRRGGBB) lesen
    (parseInt-fallback bekommt es hin)."""
    txt = INDEX_HTML.read_text(encoding="utf-8")
    # Block zwischen "const PERSONA_LIGHTS = {" und "};"
    m = re.search(r"const PERSONA_LIGHTS\s*=\s*\{(.+?)\n\};", txt, re.DOTALL)
    if not m:
        raise RuntimeError("PERSONA_LIGHTS-Block in web/index.html nicht gefunden")
    block = m.group(1)
    lights = {}
    # Pro Persona-Zeile: key: { hemi: {...}, dir: {...}, rim: {...} },
    # Wir matchen "key:" und ziehen die folgenden drei Sub-Objekte.
    for pm in re.finditer(r"(\w+):\s*\{\s*hemi:\s*\{([^}]+)\},\s*dir:\s*\{([^}]+)\},\s*rim:\s*\{([^}]+)\}", block):
        key = pm.group(1)
        hemi = parse_light_subobj(pm.group(2))
        d = parse_light_subobj(pm.group(3))
        rim = parse_light_subobj(pm.group(4))
        lights[key] = {"hemi": hemi, "dir": d, "rim": rim}
    return lights


def parse_light_subobj(s):
    """Inhalt zwischen den geschweiften Klammern eines hemi/dir/rim-Sub-Objekts
    zerlegen. Felder: sky/ground (hemi), color (dir/rim), intensity, pos (dir/rim).
    Werte: Hex 0x.. -> "#RRGGBB"-String, Zahlen 1:1, Arrays als Liste."""
    out = {}
    # Hex-Werte als 0x... -> "#..."
    for cm in re.finditer(r"(\w+):\s*0x([0-9a-fA-F]+)", s):
        out[cm.group(1)] = "#" + cm.group(2).lower().rjust(6, "0")
    # intensity / numerische Werte
    for cm in re.finditer(r"(\w+):\s*(-?\d+(?:\.\d+)?)\b", s):
        if cm.group(1) not in out:
            out[cm.group(1)] = float(cm.group(2))
    # pos: [ a, b, c ]
    pm = re.search(r"pos:\s*\[([^\]]+)\]", s)
    if pm:
        nums = [float(x.strip()) for x in pm.group(1).split(",") if x.strip()]
        out["pos"] = nums
    return out


def parse_persona_gradients():
    """PERSONA_GRADIENTS aus web/index.html ziehen."""
    txt = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"const PERSONA_GRADIENTS\s*=\s*\{(.+?)\n\};", txt, re.DOTALL)
    if not m:
        raise RuntimeError("PERSONA_GRADIENTS-Block in web/index.html nicht gefunden")
    block = m.group(1)
    gradients = {}
    # key: "string",  (string kann Doppel-Anfuehrungszeichen drin haben wenn escaped - hier nicht)
    for gm in re.finditer(r'(\w+):\s*"([^"]+)"\s*,', block):
        gradients[gm.group(1)] = gm.group(2)
    return gradients


def language_for(key, persona):
    """Persona-Sprache aus den existierenden Sets ableiten."""
    if key == "tutor":
        return "tutor"           # EN + JP-Lehr-Format
    if key == "kyoto":
        return "kyoto"           # JP only + [de:...]-Untertitel
    if key in GERMAN_PERSONAS:
        return "de"              # Companion - folgt companion_lang DE/EN
    if key.startswith("_"):
        return "internal"
    return "de"


def build_persona_entry(key, persona, lights, gradients):
    """Eine Persona aus dem Code-Dict in JSONC-Form bringen."""
    entry = {
        "enabled": True,
        "name": persona["name"],
        "language": language_for(key, persona),
        "auto_switch_block": key in PERSONA_AUTO_BLOCKLIST,
        "force_research": False,
        "no_canon": bool(persona.get("no_canon", False)),
        "system": persona["system"],
        "scene": persona.get("scene", ""),
        "lighting": persona.get("lighting", ""),
        "fewshot": persona.get("fewshot", []),
        "render": {
            "gradient": gradients.get(key, ""),
            "lights": lights.get(key, {}),
        },
    }
    return entry


def jsonc_header():
    return """// ============================================================================
// config/personas.jsonc - Yukis Persona-Definitionen
// ============================================================================
//
// Single-Source-of-Truth fuer alle USER-waehlbaren Personas. Interne Personas
// (_research, _adventure, _dm) bleiben in yuki_core.py - die haben kein
// User-Tuning, das schadet nur.
//
// Felder pro Persona:
//   enabled         bool   - false: Persona ist im Picker und im UI versteckt
//                            (Pflege-Knopf, kein Live-Reload, Restart noetig).
//   name            str    - Anzeige-Label im Picker.
//   language        str    - "tutor" | "kyoto" | "de" | "secretary" | "internal"
//                            "de" = Companion, folgt companion_lang DE/EN.
//                            "secretary" = wie "de" aber mit force_research.
//   auto_switch_block bool - true: [persona:KEY]-Marker darf weder rein noch raus
//                            wechseln (tutor/kyoto sind so). Empfehlung true bei
//                            allen Personas mit harter Sprach-/Modus-Bindung.
//   force_research  bool   - true: jeder Turn laeuft im Tools-Modus mit vollem
//                            Kontext (Heart/Facts/Episodes/People). UI: 🧠 ist
//                            gepinnt-aktiv und nicht klickbar. Nur Sekretaerin.
//   system          str    - mehrzeiliger System-Prompt (steht NACH BASE_RULES).
//                            "LANGUAGE: ..."-Zeile als Anker drin lassen.
//   scene           str    - kurze visuelle Beschreibung der Umgebung. Fliesst
//                            NICHT in den Prompt, aber Frontend liest sie fuer
//                            Persona-Picker-Tooltips (optional).
//   lighting        str    - Beleuchtungs-Hinweis (rein dokumentarisch, sollte
//                            zur render.lights-Vorgabe passen).
//   fewshot         list   - 2-3 Beispiel-Turns. WICHTIG: alle Action-Marker die
//                            die Persona nutzen soll mindestens 1x demonstrieren
//                            (siehe [[action-marker-sycophancy]] - few-shot
//                            > BASE_RULES).
//   render          obj    - Frontend-Visualisierung. gradient = CSS-String,
//                            lights = {hemi,dir,rim} mit Hex-Farben.
//
// Aenderungen brauchen Server-Restart (kein Live-Reload, analog settings.jsonc).
// Bei kaputtem JSONC oder fehlender Datei faellt Yuki auf hardcoded-Personas
// im Code zurueck - sie bleibt lauffaehig.
// ============================================================================
"""


def main():
    lights = parse_persona_lights()
    gradients = parse_persona_gradients()

    out = {"personas": {}}
    for key, persona in PERSONAS.items():
        if key.startswith("_"):
            continue                # interne Personas bleiben im Code
        out["personas"][key] = build_persona_entry(key, persona, lights, gradients)

    target = ROOT / "config" / "personas.jsonc.generated"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = jsonc_header() + json.dumps(out, indent=2, ensure_ascii=False)
    target.write_text(payload, encoding="utf-8")
    print(f"-> {target}")
    print(f"   {len(out['personas'])} Personas geschrieben")
    print(f"   Lights: {sorted(lights.keys())}")
    print(f"   Gradients: {sorted(gradients.keys())}")


if __name__ == "__main__":
    main()
