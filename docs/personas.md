# Persönlichkeits-Prompt & Personas (umschaltbar)

Lade diese Datei, wenn du eine Persona anlegst/ändert, Tonalität-Mismatch debuggst,
oder die JP-Disziplin (welche Persona darf Japanisch) anfasst.

## Single-Source-of-Truth: `config/personas.jsonc`

Seit 2026-06-10 leben alle **User-wählbaren** Personas in `config/personas.jsonc`.
Dort liegen `name`, `system`-Prompt, `fewshot`, `scene`, `lighting` UND die
Frontend-Darstellung (`render.gradient`, `render.lights`). Neue Persona =
einen Eintrag im JSONC hinzufügen, Yuki neu starten. Personas an-/ausschalten
ohne Code-Edit: `"enabled": false`.

**Interne Personas** (`_research`, `_adventure`, `_dm`) bleiben in
`yuki_core.py` — die haben kein User-Tuning, das schadet nur. Sie werden
beim Merge nicht überschrieben.

**Code-Fallback:** wenn `personas.jsonc` fehlt oder kaputt ist, fällt Yuki
auf das hardcoded `PERSONAS`-Dict in `yuki_core.py` zurück. Yuki bleibt
lauffähig, die Migration ist also reversibel.

**Kein Live-Reload:** Änderungen brauchen Server-Restart (analog
`settings.jsonc`).

## Schema pro Persona

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `enabled` | bool | nein (true) | `false`: Persona ist im Picker versteckt |
| `name` | str | **ja** | Anzeige-Label im Picker |
| `language` | str | nein | `"tutor"` (EN+JP), `"kyoto"` (JP+[de:]), `"de"` (Companion, folgt companion_lang DE/EN), `"internal"` |
| `auto_switch_block` | bool | nein (false) | War: Ausnahme vom Persona-Auto-Switch. **Auto-Switch + `[persona:]`-Marker sind seit 2026-07-14 entfernt** – das Flag speist nur noch die Konstante `PERSONA_AUTO_BLOCKLIST` (ohne Wirkung); Persona wechselt jetzt ausschließlich der User. |
| `force_research` | bool | nein (false) | `true`: Tools-Modus jeden Turn (Sekretärin); 🧠-Button im UI gepinnt-aktiv |
| `system` | str | **ja** | System-Prompt (steht NACH BASE_RULES). `LANGUAGE:`-Zeile als Anker drin lassen |
| `scene` | str | nein | Visuelle Szenenbeschreibung (für Atmosphäre, fließt in den Prompt) |
| `lighting` | str | nein | Beleuchtungs-Hinweis (rein dokumentarisch) |
| `fewshot` | list | **ja** | 2-3 Beispiel-Turns. Alle Action-Marker die die Persona nutzen soll mindestens 1x demonstrieren ([[action-marker-sycophancy]]) |
| `render.gradient` | str | nein | CSS-Gradient für Persona-Hintergrund |
| `render.lights` | obj | nein | `{hemi, dir, rim}` mit Farben (`#rrggbb`) + Intensität + Position |

## BASE_RULES

= die invarianten Regeln für ALLE Personas: Yuki-Identität (35, Kyoto), Voice-Format
(kurz, keine Emojis, kein Markdown), Marker-Definitionen (nur noch Render-/Tutor-/Kyoto-
Marker; die Action-Marker sind seit 2026-07-14 raus – siehe Historie), das
Kana+Übersetzungs-Format *falls* JP genutzt wird. Statt der Action-Marker-Blöcke hängt
`build_system_msg` einen kurzen `_CAPABILITY_HINT` an (Yuki kündigt Aktionen in Ich-Form
an, ein async Action-Decider führt sie aus). **Seit 2026-05-30 enthält BASE_RULES KEINE Sprach-Wahl mehr** –
die steht pro Persona im `LANGUAGE:`-Anker. Yuki bleibt dieselbe Person mit GETEILTER
Erinnerung – Personas sind nur Rollen/Stimmungen.

BASE_RULES lebt weiterhin in `yuki_core.py` (Konstante `BASE_RULES`), wird zur
Laufzeit mit `{{AGE}}`, `{{MOODS_LIST}}`, `{{PERSONAS_LIST}}`, `{{GESTURES_LIST}}`
gefüllt.

## Aktuelle Personas (16 + 3 interne)

Stand `config/personas.jsonc`: **11 aktiv, 5 deaktiviert** (`enabled:false`) + 3 interne im Code.

| Key | Sprache | aktiv | force_research | Notiz |
|---|---|---|---|---|
| `tutor` | EN+JP | ✅ | – | JP-Lern-Modus, `[expect_lang:]`/`[vocab:]`/`[furigana:]`/`[quiz:]`/`[conjugate:]`; `auto_switch_block` |
| `kyoto` | JP + [de:] | ✅ | – | Reines JP mit stillem DE-Untertitel; `auto_switch_block` |
| `smalltalk` | DE | ✅ | – | Locker plaudern |
| `sibling` | DE | ❌ | – | Schwester, neckisch |
| `confidante` | DE | ✅ | – | Realistische Vertraute |
| `partner` | DE | ✅ | – | Liebevoll & flirty |
| `party` | DE | ✅ | – | Verspielt, absurd |
| `gamer` | DE | ✅ | – | Videospiel-Fan |
| `comforter` | DE | ❌ | – | Sanfter Support |
| `philosopher` | DE | ❌ | – | Tief-denkend, fragend |
| `coach` | DE | ❌ | – | Direkt, motivierend |
| `storyteller` | DE | ✅ | – | Anekdoten/Geschichten; `no_canon` (Story leakt nicht in den Canon) |
| `kuenstlerin` | DE | ✅ | – | Malt SVG-Doodles (`[draw:]`/`[canvas:]`); `auto_switch_block` |
| `developer` | DE | ❌ | – | Code/Debugging/DevOps; bewusst aus |
| `secretary` | DE | ✅ | ✅ | Persönliche Sekretärin, Tools immer an; Aktionen markerlos via Async-Decider; `auto_switch_block` (NEU 2026-06-10) |
| `berater` | DE | ✅ | – | Einkauf/jap. Küche, Listen-Spezialistin; `auto_switch_block` |
| `_research` | DE/EN | – | (intern) | Per-Turn Tools-Modus (🧠-Toggle), SLIM Kontext |
| `_adventure` | – | – | (intern) | Adventure-Engine-Modus |
| `_dm` | – | – | (intern) | DM für Story-DM-Adventures |

## Sprach-Disziplin

- `tutor` → `LANG_REMINDER_TUTOR` (EN + JP-Lehr-Format, „Never German")
- `kyoto` → `LANG_REMINDER_KYOTO` (JP only + `[de:]`-Untertitel)
- `secretary` → `LANG_REMINDER_SECRETARY_DE/_EN` (4-10 Sätze, Tools an, Action-Marker erlaubt)
- alle anderen `language="de"` → `LANG_REMINDER_GERMAN` / `LANG_REMINDER_EN_COMPANION`
  je `companion_lang` (`de`/`en`)

TTS-Routing macht `pick_tts_engine(text)` automatisch: Umlaute/dt. Funktionsworte → F5
(DE), sonst SoVITS (EN/JA). Sprachunabhängig vom Persona-Pin.

## Umschalten zur Laufzeit

- Web/Handy: **Dropdown** oben (`/personas` GET, `/persona` POST)
- Memory & Verlauf laufen beim Wechsel weiter (`system_msg`+`fewshot`+`reminder` ändern sich)
- Persona-Wechsel passiert nur durch den User (Dropdown / Hotkeys 1–9). Der frühere autonome Persona-Auto-Switch (`[persona:]`-Marker) wurde 2026-07-14 entfernt; `auto_switch_block` speist nur noch `PERSONA_AUTO_BLOCKLIST` (ohne Wirkung)

## Sekretärin (force_research)

Eine eigene Persona mit dauer-aktiven Tools. Anders als der interne `_research`
(SLIM, kein Beziehungs-Kontext, nur per-Turn als Werkzeug-Klammer):

- **Voller Yuki-Kontext** (Heart/Facts/Episodes/People/Habits/Affinities) — sie weiß
  dass Maureen Michaels Schwester ist, wenn er „mail an Schwester" sagt.
- **Aktionen markerlos** (seit 2026-07-14): Termine/Notizen/Timer löst sie über den async
  Action-Decider (`yuki_actions.py`) aus — sie sagt in Ich-Form an, dass sie es tut, das
  System führt es aus. Kein `[event:]`/`[note:]`/`[timer:]`-Marker mehr im Prompt.
- **Volle Initiative** (testweise): wenn aus dem Kontext klar, macht sie direkt
  was getan werden muss („Hab dir das eingetragen"). Bei Unsicherheit fragt sie nach.
- **Kein Beziehungs-State-Skip** (anders als `_research`): conversation.json +
  alle Verdichtungs-Gates laufen normal.
- **UI**: 🧠-Button ist gepinnt-aktiv (CSS `.forced`-Class), Klicks werden ignoriert.
- **Backend-Pfad**: `generate_secretary_reply(HISTORY, MEMORY)` in `yuki_core.py`
  → `build_secretary_system_msg(memory)` (voller Yuki-Stack + Datum-Anker)
  → `chat_ollama` mit `RESEARCH_TOOLS_SPEC` + `purpose="secretary"`.

**Test-Hinweis:** Volle Initiative ist ein bewusstes Experiment. Wenn sie zu viel
eigenmächtig einträgt (falsche Termine, zu viele Notizen), kann der `system`-Prompt
in `personas.jsonc` gedrosselt werden auf „frage IMMER nach, bevor du einen Termin
einträgst". Beobachten dann zähmen, nicht prophylaktisch (siehe
[[memory-features-need-anchor-case]]).

## API in `yuki_core.py`

- `build_system_msg(memory, persona)` — normaler Pfad
- `build_secretary_system_msg(memory)` — Sekretärin-Pfad (Vollkontext + Datum)
- `build_research_system_msg(persona_before)` — interner Tools-Modus (SLIM)
- `persona_fewshot(persona)`
- `persona_reminder(persona)`
- `persona_list()` → `[(key, name)]` (gefiltert nach `enabled:true` und ohne `_`-Prefix)
- `generate_reply(history, system_msg, fewshot, reminder)` — normaler Pfad
- `generate_secretary_reply(history, memory)` — Sekretärin-Pfad
- `generate_research_reply(history, persona_before)` — interner Tools-Modus
- `FORCE_RESEARCH_PERSONAS` — Set der Personas mit `force_research:true`
- `PERSONA_AUTO_BLOCKLIST` — abgeleitet aus `auto_switch_block`-Flag + alle internen
- `GERMAN_PERSONAS` — abgeleitet aus `language=="de"`

## Persona-Persistenz

Zuletzt gewählte Persona überlebt Neustarts (`memory/yuki_persona.json`,
`load_persona`/`save_persona` in yuki_core; Web speichert beim Dropdown-`/persona`).
Fallback auf `tutor` bei fehlender/kaputter Datei oder unbekanntem Key.

## Migration / Wie eine neue Persona anlegen

1. `config/personas.jsonc` öffnen
2. Neuen Block nach dem Schema oben anlegen (kopiere ein bestehendes und passe an)
3. **Wichtig**: `name`, `system`, `fewshot` sind Pflicht; ohne diese wird die Persona
   beim Laden mit einem Log geskippt
4. Server-Restart
5. Im UI auswählen, testen
6. Wenn nicht zufrieden: `enabled:false` setzen (statt löschen), Restart

Wenn du das Persona-Schema selbst änderst (z.B. neues Feld), kannst du auch das
Migrations-Skript `tools/dump_personas_to_jsonc.py` als Vorlage nehmen — es nimmt
die aktuelle Code-Dict-Struktur und dumpt sie als JSONC.

## Historie

- **2026-05-27:** JP-Disziplin invertiert (vorher 4 Personas mit JP + 1 EN-only;
  jetzt 1 tutor mit JP + 4 EN-only).
- **2026-05-30:** Companion-Personas auf Deutsch umgestellt, nachdem F5-TTS-German
  als zweite Engine live ging. Vorher waren alle non-tutor-Personas auf Englisch
  gepinnt, weil GPT-SoVITS kein Deutsch konnte (Stolperfalle 7). Tutor bleibt EN+JP.
- **2026-06-10:** Personas in `config/personas.jsonc` ausgelagert. `GERMAN_PERSONAS`
  und `PERSONA_AUTO_BLOCKLIST` jetzt aus Persona-Flags abgeleitet. Neue
  `secretary`-Persona mit `force_research:true` und vollem Yuki-Kontext + Tools-Spec.
  Frontend zieht `PERSONA_LIGHTS` und `PERSONA_GRADIENTS` aus `/personas`-Response
  (Cross-Block-Bridge via `window.setPersonaLightsConfig`).
- **2026-07-14:** Marker-Async-Migration. Die Action-Marker (`[timer:]`, `[note:]`,
  `[event:]`, `[ha:]`, `[list:]`/`[list_activate:]`/`[list_check:]`, `[routine:]`/
  `[routine_done:]`) sowie `[persona:]`, `[affinity:]` und `[heart:]` sind aus den
  Persona-Prompts + BASE_RULES raus. Aktionen laufen über einen async **Action-Decider**
  (`yuki_actions.py`, nativer gemma4-Tool-Call im TTS-Schatten); statt der Marker-Blöcke
  trägt BASE_RULES den kurzen `_CAPABILITY_HINT`. **Persona-Auto-Switch komplett entfernt**
  (Endpoint `/persona/auto_switch`, `autoSwitchBtn`, `extract_persona_marker` weg). Affinity/
  Heart werden weiter über ihre Verdichtungs-Gates gepflegt, nur der Marker-Fast-Path fiel weg.
