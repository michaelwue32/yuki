"""Async Action-Pipeline (Decider fuehrt alle ACTION-Marker aus).

D Action-Decider (nativer Tool-Calling-Call, urteilt was Yuki TUT) + E Executor
(fuehrt die bestehenden yc.*-Funktionen aus, meldet ok/fail). F Feedback-Emitter
lebt in server.py (LOCK + SSE). Siehe docs/superpowers/specs/2026-07-13-marker-
reliability-async-actions-design.md + 2026-07-14-marker-prompt-diet-design.md.

Umfang (ACTION_TOOLS_SPEC): mark_routine_done, ha_control, create_note,
create_event, start_timer, create_list, activate_list, check_list_item,
create_routine. Diese Marker sind aus Yukis Prosa-Prompt raus (Prompt-Diaet);
der Decider macht ihre Absicht wahr. RENDER/UI-Marker (mood/gesture/calc/keepsake/
furigana/quiz/... + vocab) bleiben im synchronen Inline-Pfad
(_handle_marker_side_effects).
"""
import datetime as _dt
import json as _json
import re as _re
from difflib import SequenceMatcher as _SequenceMatcher
import imagegen
import yuki_core as yc

try:
    import homeassistant as _ha
except Exception:            # Adapter/Import fehlt -> Geraete-Pool bleibt leer
    _ha = None


# --- Tool-Definitionen (Ollama native tool-calling, Shape wie RESEARCH_TOOLS_SPEC) ---
_TOOL_DEF_ROUTINE_DONE = {
    "type": "function",
    "function": {
        "name": "mark_routine_done",
        "description": (
            "Mark one of Michael's recurring routines (e.g. medication, teeth, a daily "
            "habit) as DONE for today. Call this when Michael reports having done it, or "
            "asks you to check it off, EVEN IF you only acknowledged it in words without "
            "a marker. Use the routine's label as written in the routines list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "routine": {"type": "string",
                            "description": "Label of the routine to mark done, as shown in the routines list."}
            },
            "required": ["routine"]
        }
    }
}

_TOOL_DEF_HA_CONTROL = {
    "type": "function",
    "function": {
        "name": "ha_control",
        "description": (
            "Control one physical Home-Assistant device from the device pool. ONLY call "
            "this on an EXPLICIT request by Michael to switch or set a device. Do NOT act "
            "on mere mentions of a situation (e.g. 'let’s watch a film' is NOT a request "
            "to change the lights). Use the friendly device name from the device pool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string",
                           "description": "Friendly device name from the device pool."},
                "action": {"type": "string", "enum": ["on", "off", "toggle", "set"],
                           "description": "on/off/toggle for switches; 'set' for adjustable devices (needs value)."},
                "value": {"type": "string",
                          "description": "Only for action='set' (e.g. a temperature). Omit otherwise."}
            },
            "required": ["device", "action"]
        }
    }
}

_TOOL_DEF_CREATE_NOTE = {
    "type": "function",
    "function": {
        "name": "create_note",
        "description": (
            "Save a short note when Michael asks you to remember/note something, or clearly "
            "states a fact/todo he wants kept, OR when Yuki's OWN reply says SHE wants to "
            "remember/keep something for herself. Do NOT note idle chit-chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note content, concise."},
                "owner": {
                    "type": "string",
                    "enum": ["michael", "yuki"],
                    "description": (
                        "Whose note this is. 'yuki' ONLY when Yuki's own reply says SHE wants "
                        "to remember/keep it for herself ('das merk ich mir', 'das behalte ich'). "
                        "'michael' (default) when Michael asks to note something or states his "
                        "own fact/todo. Omit if unsure -> defaults to michael."
                    )
                }
            },
            "required": ["text"]
        }
    }
}

_TOOL_DEF_CREATE_EVENT = {
    "type": "function",
    "function": {
        "name": "create_event",
        "description": (
            "Create a calendar appointment when Michael asks to schedule something with a "
            "concrete date/time. Only on an explicit scheduling request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "datetime": {"type": "string",
                             "description": "ISO 8601 local start, e.g. 2026-07-20T15:00."},
                "title": {"type": "string", "description": "Short event title."},
                "duration_min": {"type": "integer", "minimum": 1, "maximum": 1440,
                                 "description": "Duration in minutes (default 60)."}
            },
            "required": ["datetime", "title"]
        }
    }
}

_TOOL_DEF_CREATE_LIST = {
    "type": "function",
    "function": {
        "name": "create_list",
        "description": (
            "Create a new checklist (e.g. shopping/recipe) when Michael asks to start one "
            "or dictates items for a fresh list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "List title."},
                "kind": {"type": "string", "description": "e.g. 'shopping', 'recipe' (optional)."},
                "items": {"type": "array", "items": {"type": "string"},
                          "description": "Initial item names (optional)."},
                "activate": {"type": "boolean",
                             "description": "Make this the active list (optional)."}
            },
            "required": ["title"]
        }
    }
}

_TOOL_DEF_ACTIVATE_LIST = {
    "type": "function",
    "function": {
        "name": "activate_list",
        "description": (
            "Make an existing list the active one when Michael asks to switch to/open it. "
            "If a DIFFERENT list is already active, the switch will be refused (Michael must "
            "confirm) - that's expected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "list": {"type": "string", "description": "List title or id to activate."}
            },
            "required": ["list"]
        }
    }
}

_TOOL_DEF_CHECK_LIST_ITEM = {
    "type": "function",
    "function": {
        "name": "check_list_item",
        "description": (
            "Mark an OPEN item on the currently active list as 'mentioned/got it' when Michael "
            "says he already has it or is done with it. Use the item name from the active list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Name of the open item to mark."}
            },
            "required": ["item"]
        }
    }
}

_TOOL_DEF_CREATE_ROUTINE = {
    "type": "function",
    "function": {
        "name": "create_routine",
        "description": (
            "Create a QUIET RECURRING resolution Michael wants to keep up (daily meds, "
            "brushing teeth at night, a weekly stream). Call this when Michael asks to set up "
            "a recurring habit/reminder, OR when Yuki's reply says she entered/noted it as a "
            "routine - EVEN IF only acknowledged in words. Use ONLY for something RECURRING "
            "that does NOT belong on the calendar: a one-off or dated appointment is an EVENT, "
            "not a routine. Routines start WITHOUT active push (Michael enables that himself)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string",
                          "description": "Short name of the routine, e.g. 'Abend-Medizin'."},
                "recurrence": {"type": "string",
                               "description": "'daily' or weekday(s) like 'fri' or 'mon,thu'. Default daily."},
                "band": {"type": "string", "enum": ["morning", "afternoon", "evening", "night"],
                         "description": "Rough time of day, optional."},
                "time": {"type": "string",
                         "description": "Clock time like '20:00', optional (used for later reminders)."}
            },
            "required": ["label"]
        }
    }
}

_TOOL_DEF_TIMER = {
    "type": "function",
    "function": {
        "name": "start_timer",
        "description": (
            "Start a countdown timer when Michael asks for one (e.g. 'stell einen Timer "
            "auf 5 Minuten', 'weck mich in einer halben Stunde'). Convert the duration to "
            "SECONDS yourself. ONLY call this on a real timer request - a mere mention of "
            "time ('ich hol nur kurz Luft') is NOT a timer request. Do NOT call it twice "
            "for the same request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer",
                            "description": "Duration in seconds, e.g. 300 for 5 minutes."},
                "label": {"type": "string",
                          "description": "Short label for the timer, e.g. 'Tee'. Default 'Timer'."}
            },
            "required": ["seconds"]
        }
    }
}

_TOOL_DEF_GEDANKENBILD = {
    "type": "function",
    "function": {
        "name": "gedankenbild",
        "description": (
            "Render a THOUGHT-IMAGE (Gedankenbild) - a picture of what Yuki imagines, "
            "dreams, or how she pictures something in her mind. Call this ONLY when Michael "
            "asks Yuki to show/imagine how something looks in her head, how she pictures a "
            "scene or person, or how she herself would look if she were real - AND Yuki's reply "
            "agrees to picture it. Do NOT call it for casual talk about images, for a drawing "
            "request (that is the Kuenstlerin's SVG job), or when Yuki merely discusses a topic. "
            "Compose the visual PROMPT yourself, in English, from Yuki's point of view: concrete "
            "subject, scene, mood, lighting (<60 words). Pick the style that fits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "English visual prompt from Yuki's imagination, <60 words."},
                "style": {"type": "string", "enum": ["painterly", "anime", "realistic"],
                          "description": "'painterly' (dreamy/emotional, default), 'anime' "
                                         "(Yuki's own art style), 'realistic' (how it would look real)."}
            },
            "required": ["prompt"]
        }
    }
}

_TOOL_DEF_UPDATE_EVENT = {
    "type": "function",
    "function": {
        "name": "update_event",
        "description": (
            "Change an EXISTING calendar appointment - reschedule (new datetime) and/or "
            "rename (new title). Use the number from the 'Kommende Termine' list as ref "
            "(the N in '#N'). Provide at least one of datetime/title. Only on an explicit "
            "request from Michael."),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer",
                        "description": "The number N from the 'Kommende Termine' list (#N)."},
                "datetime": {"type": "string",
                             "description": "New ISO 8601 local start, e.g. 2026-07-24T15:00."},
                "title": {"type": "string", "description": "New event title."},
                "duration_min": {"type": "integer", "minimum": 1, "maximum": 1440,
                                 "description": "New duration in minutes."},
            },
            "required": ["ref"],
        },
    },
}

_TOOL_DEF_DELETE_EVENT = {
    "type": "function",
    "function": {
        "name": "delete_event",
        "description": (
            "Cancel/delete an EXISTING calendar appointment. Use the number from the "
            "'Kommende Termine' list as ref (the N in '#N'). Only on an explicit request "
            "from Michael."),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer",
                        "description": "The number N from the 'Kommende Termine' list (#N)."},
            },
            "required": ["ref"],
        },
    },
}

ACTION_TOOLS_SPEC = [_TOOL_DEF_ROUTINE_DONE, _TOOL_DEF_HA_CONTROL,
                     _TOOL_DEF_CREATE_NOTE, _TOOL_DEF_CREATE_EVENT,
                     _TOOL_DEF_CREATE_LIST, _TOOL_DEF_ACTIVATE_LIST,
                     _TOOL_DEF_CHECK_LIST_ITEM, _TOOL_DEF_CREATE_ROUTINE,
                     _TOOL_DEF_TIMER, _TOOL_DEF_GEDANKENBILD,
                     _TOOL_DEF_UPDATE_EVENT, _TOOL_DEF_DELETE_EVENT]


# --- Kontext-Builder fuer den Decider-Prompt ---

def _routine_lines(now=None):
    """Heute faellige Routinen fuers Decider-Kontext (offen zuerst; erledigte mit
    Vermerk, damit der Decider nicht doppelt abhakt)."""
    lines = []
    for r in yc.routines_view(now):
        if not r.get("enabled", True) or not r.get("due_today"):
            continue
        label = (r.get("label") or "").strip()
        if not label:
            continue
        lines.append(f"- {label}" + (" (heute schon erledigt)" if r.get("done_today") else ""))
    return lines


def _ha_names():
    """Schaltbare + regelbare Geraete-Namen. Leer wenn Adapter fehlt."""
    if _ha is None:
        return []
    names = []
    try:
        names += [d["name"] for d in _ha.devices() if d.get("name")]
        names += [d["name"] for d in _ha.settables() if d.get("name")]
    except Exception as e:
        print(f"  [Action-Kontext: HA-Geräte-Liste fehlgeschlagen (ignoriert): {e}]", flush=True)
        return []
    return names


def _list_context():
    """Aktive Liste (offene Items) + Titel aller aktivierbaren Listen fuer die
    list-Tools. Leer/defensiv wenn nichts da."""
    parts = []
    try:
        cur = yc.active_list()
    except Exception:
        cur = None
    if cur:
        open_items = [ (it.get("text") or "").strip()
                       for it in (cur.get("items") or []) if not it.get("checked") ]
        open_items = [t for t in open_items if t]
        title = (cur.get("title") or "").strip()
        if open_items:
            parts.append(f"Aktive Liste '{title}' - offene Items (Namen so verwenden):\n"
                         + "\n".join(f"- {t}" for t in open_items))
        else:
            parts.append(f"Aktive Liste '{title}': (keine offenen Items)")
    else:
        parts.append("Aktive Liste: (keine)")
    try:
        titles = [ (l.get("title") or "").strip() for l in yc.load_lists()
                   if not l.get("archived") and (l.get("title") or "").strip() ]
    except Exception:
        titles = []
    if titles:
        parts.append("Vorhandene Listen (für activate_list):\n"
                     + "\n".join(f"- {t}" for t in titles))
    return "\n\n".join(parts)


def _resolution_lines():
    """Feste/gesternte aktive Vorsätze für den Decider-Kontext (Action-Sicherheit,
    unabhängig vom Chat-Multiplier RESOLUTIONS_MULTIPLIER). Filter:
    active AND (strength >= firm_threshold OR starred)."""
    if not yc.RESOLUTIONS_ENABLED:
        return []
    try:
        items = yc.load_resolutions()
    except Exception:
        return []
    out = []
    for e in items or []:
        if not e.get("active", True):
            continue
        try:
            strength = int(e.get("strength", 1) or 1)
        except (TypeError, ValueError):
            strength = 1
        if strength >= yc.RESOLUTIONS_FIRM_THRESHOLD or e.get("starred"):
            txt = (e.get("resolution") or "").strip()
            if txt:
                out.append(txt)
    return out


def build_action_context(now=None):
    """Kontext-Block fuer den Decider-Prompt: heute faellige Routinen + HA-Geraete-Pool
    + Listen-Kontext + weiche Policy. Wandert bewusst aus dem Prosa-Prompt hierher
    (schlankerer Prosa-Call)."""
    parts = []
    ref = now or _dt.datetime.now()
    _WD = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    parts.append(
        f"Aktuelles Datum: {ref.date().isoformat()} ({_WD[ref.weekday()]}), "
        f"{ref.strftime('%H:%M')} Uhr. Nutze das als Basis fuer relative Zeitangaben "
        f"(heute, morgen, Donnerstag) beim datetime-Feld von create_event/update_event.")
    rl = _routine_lines(now)
    if rl:
        parts.append("Michaels heutige Routinen (Label so verwenden):\n" + "\n".join(rl))
    else:
        parts.append("Michaels heutige Routinen: (keine faellig)")
    names = _ha_names()
    if names:
        parts.append("Schaltbare/regelbare Geraete (Name so verwenden):\n"
                     + "\n".join(f"- {n}" for n in names))
    else:
        parts.append("Schaltbare/regelbare Geraete: (keine verfuegbar)")
    parts.append(_list_context())
    evs = yc.upcoming_events_with_ids()
    if evs:
        _WD_KURZ = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        lines = [f"#{e['id']} {e['title']} · {_WD_KURZ[e['start'].weekday()]} "
                 f"{e['start'].strftime('%d.%m %H:%M')}" for e in evs]
        parts.append("Kommende Termine (Nummer '#N' fuer update_event/delete_event so "
                     "verwenden):\n" + "\n".join(lines))
    else:
        parts.append("Kommende Termine: (keine)")
    rls = _resolution_lines()
    if rls:
        parts.append("Yukis geltende Vorsätze (haben Vorrang, wenn sie einer Aktion "
                     "widersprechen - außer Michael bittet ausdrücklich darum):\n"
                     + "\n".join(f"- {t}" for t in rls))
    parts.append("Policy: Geraete NUR auf eine explizite Ansage von Michael schalten. "
                 "Die blosse Erwaehnung einer Situation (z.B. 'wir schauen einen Film') "
                 "ist KEINE Schalt-Ansage.")
    return "\n\n".join(parts)


# --- Decider-Runner (Task 4) ---

_DECIDER_ROLE = (
    "You are Yuki's ACTION layer. You do NOT talk to Michael. You read the recent "
    "exchange (Michael's words AND Yuki's spoken reply) and decide which side-effect "
    "TOOLS to call. Judge the whole exchange:\n"
    "- If Yuki's reply claims or implies an action happened, MAKE IT TRUE by calling the "
    "tool (Yuki must never merely say she noted/did something without it happening).\n"
    "- If Michael reports doing a routine, or asks to check it off, call mark_routine_done "
    "even if Yuki only acknowledged it in words ('ich merke es mir'). His confirmation is "
    "often ELLIPTICAL and does NOT name the routine ('jep, gerade', 'hab ich', 'schon "
    "erledigt', 'gerade genommen') - resolve WHICH routine he means from the recent "
    "exchange (usually the one Yuki just asked about) and the routines list, then call the "
    "tool with that routine's exact label. This holds for ANY routine (meds, teeth, a "
    "daily habit), not just one kind. Example: earlier Yuki asked whether he'd done a "
    "routine (say 'Zaehne putzen'); now Michael answers 'jep, gerade' and Yuki replies "
    "'dann ist das erledigt' -> call mark_routine_done(routine=\"Zaehne putzen\").\n"
    "- Michael's explicit request is the STRONGEST signal and must always go through.\n"
    "- Yuki has standing resolutions (listed in the context block below). They take "
    "PRIORITY over her own initiative: if her reply only loosely implies an action but a "
    "resolution says not to do it in this situation (e.g. only switch a device on "
    "Michael's explicit request), do NOT call the tool. A resolution constrains Yuki's "
    "own initiative; it never blocks Michael's own explicit request.\n"
    "- Yuki can render a THOUGHT-IMAGE via the gedankenbild tool when Michael asks how she "
    "imagines/pictures something (or how she'd look if real) and her reply agrees to show it. "
    "Only on a genuine imagination request - never on casual image talk or a drawing request.\n"
    "- Distinguish real intent from politeness/figures of speech.\n"
    "- create_note: set owner=\"yuki\" ONLY when Yuki's OWN reply says SHE wants to keep/"
    "remember something for herself ('das merk ich mir', 'das behalte ich fuer mich'). Use "
    "owner=\"michael\" (the default) when Michael asks to note something or states his own "
    "fact/todo. Do NOT open a new note for a mere REWORDING of something just noted - the "
    "same request rephrased is still one note.\n"
    "Call zero, one, or several tools. If nothing is called for, call no tool."
)


def build_decider_system_msg(action_context):
    """Schlanker System-Prompt fuer den Decider (kein BASE_RULES/Persona/Facts -
    reine Urteils-Aufgabe, analog build_research_system_msg)."""
    return _DECIDER_ROLE + "\n\n" + action_context


def run_action_decider(user_text, yuki_prosa, recent_turns, now=None):
    """D: der urteilende zweite LLM-Call (nativer Tool-Calling). Liefert die rohen
    tool_calls oder [] (auch auf <12B-Failover -> stiller Skip)."""
    if not yc._supports_tool_calling():
        return []
    sys_msg = build_decider_system_msg(build_action_context(now))
    exchange = (f"RECENT TURNS:\n{recent_turns}\n\n"
                f"NOW:\nMichael: {user_text}\nYuki (spoken reply): {yuki_prosa}")
    msgs = [{"role": "system", "content": sys_msg},
            {"role": "user", "content": exchange}]
    return yc.chat_ollama(msgs, tools=ACTION_TOOLS_SPEC, think=True,
                          temperature=0, return_tool_calls=True, purpose="action_decider")


# --- Executor (Task 5) ---

_HA_ACT_DE = {"on": "an", "off": "aus", "toggle": "umschalten"}


def _args_of(tc):
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = _json.loads(args)
        except Exception:
            args = {}
    return (fn.get("name", "") or ""), (args if isinstance(args, dict) else {})


def _exec_routine_done(args):
    key = (args.get("routine") or "").strip()
    if not key:
        return None
    rid = yc._match_routine_key(key)
    if not rid:
        return None                          # unaufloesbar -> kein Record (kein Icon-Luege)
    if yc.is_routine_done_today_by_id(rid):
        return None                          # heute schon abgehakt -> No-Op, KEIN erneuter
                                             # Record (sonst Haken-Icon bei jedem Folgesatz,
                                             # solange der Decider das Thema aus recent_turns
                                             # als 'nochmal abhaken' liest)
    ok = bool(yc.mark_routine_done(rid, by="michael"))
    return {"type": "routine_done", "detail": key, "ok": ok}


def _exec_routine_create(args):
    label = (args.get("label") or "").strip()
    if not label:
        return None                              # leeres Label -> kein Record
    # create_routine normalisiert recurrence/band/time robust selbst (sichere Fallbacks)
    # und dedupt auf Label. Yuki ist Autor -> created_by='yuki', proactive=False (Michael
    # schaltet Push selbst im Routinen-Modal scharf).
    rec = yc.create_routine(label,
                            recurrence=(args.get("recurrence") or "daily"),
                            band=(args.get("band") or ""),
                            due_after=(args.get("time") or ""),
                            created_by="yuki", proactive=False)
    if not rec:
        return {"type": "routine", "detail": label, "ok": False}
    when = yc.routine_when_label(rec.get("recurrence", "daily"),
                                 rec.get("band", ""), rec.get("due_after", ""))
    detail = rec.get("label", label) + (f" · {when}" if when else "")
    return {"type": "routine", "detail": detail, "ok": True}


def _exec_ha(args):
    target = (args.get("device") or "").strip()
    action = (args.get("action") or "").strip().lower()
    if not target or action not in ("on", "off", "toggle", "set"):
        return None
    if action == "set":
        value = (args.get("value") or "").strip()
        if not value:
            return None
        ok = bool(yc.ha_set_value(target, value))
        tail = value
    else:
        ok = bool(yc.ha_set(target, action))
        tail = _HA_ACT_DE.get(action, action)
    label = yc.ha_device_name(target) or target
    return {"type": "ha", "detail": f"{label} → {tail}", "ok": ok}


# Executor-Dedup fuer Notizen (2026-09-07): Der async Decider kann dieselbe Notiz bei
# jeder Umformulierung im Gespraech erneut anlegen (er liest recent_turns, das Thema
# haengt noch drin) -> 3x Umformulieren = 3 Dubletten. Statt zu duplizieren heben wir
# eine bestehende, sehr aehnliche Notiz DESSELBEN Besitzers auf die neueste Formulierung.
# Nur die neuesten N aktiven Notizen des Besitzers, nur im Zeitfenster (laufendes
# Gespraech) - sonst wuerde eine zufaellig aehnliche Alt-Notiz ueberschrieben. Schwelle
# bewusst hoch (0.72): lieber eine Dublette zu viel als ein falscher Merge. Alles
# tunebar; embrace-imperfection, nach echtem Gebrauch nachjustieren.
_NOTE_MERGE_RECENT_N = 3       # nur die neuesten 3 Notizen des Besitzers pruefen
_NOTE_MERGE_WINDOW_MIN = 45    # Minuten: nur Notizen aus dem laufenden Gespraech mergen
_NOTE_MERGE_SIM = 0.72         # SequenceMatcher-ratio-Schwelle auf normalisiertem Text
_NOTE_WORD_RE = _re.compile(r"[^\wäöüß]+", _re.UNICODE)


def _norm_note(text):
    """Klein + Satzzeichen/Mehrfach-Space raus - Basis fuer den Aehnlichkeitsvergleich."""
    return _NOTE_WORD_RE.sub(" ", (text or "").lower()).strip()


def _recent_similar_note(text, source, now=None):
    """Die neueste aktive Notiz DESSELBEN Besitzers, die (a) im Zeitfenster liegt und
    (b) dem neuen Text sehr aehnlich ist (Reformulierung). None wenn keine passt."""
    key = _norm_note(text)
    if not key:
        return None
    ref = now or _dt.datetime.now()
    cands = [n for n in yc.load_notes()
             if n.get("active") and (n.get("source") or "michael").strip().lower() == source]
    cands.sort(key=lambda n: n.get("created", ""), reverse=True)
    for n in cands[:_NOTE_MERGE_RECENT_N]:
        created = n.get("created")
        try:
            age_min = (ref - _dt.datetime.fromisoformat(created)).total_seconds() / 60.0
        except (TypeError, ValueError):
            continue                                   # unparsbares Datum -> nicht mergen
        if age_min < 0 or age_min > _NOTE_MERGE_WINDOW_MIN:
            continue
        if _SequenceMatcher(None, key, _norm_note(n.get("text", ""))).ratio() >= _NOTE_MERGE_SIM:
            return n
    return None


def _exec_note(args, note_source="michael", now=None):
    text = (args.get("text") or "").strip()
    if not text:
        return None
    # Besitzer: der Decider darf ihn via owner-Arg setzen (Yukis eigene Merk-Notiz);
    # unbekannt/fehlend -> Pfad-Default (michael im Chat, yuki im Steward-Loop).
    owner = (args.get("owner") or "").strip().lower()
    source = owner if owner in ("michael", "yuki") else note_source
    # Reformulierung im laufenden Gespraech -> bestehende Notiz aktualisieren statt duplizieren.
    dup = _recent_similar_note(text, source, now=now)
    if dup:
        ok = bool(yc.update_note(dup["id"], text=text))
        return {"type": "note", "detail": text, "ok": ok, "owner": source, "updated_id": dup["id"]}
    ok = bool(yc.add_note(text, source=source))
    return {"type": "note", "detail": text, "ok": ok, "owner": source}


def _exec_event(args):
    iso = (args.get("datetime") or "").strip()
    title = (args.get("title") or "").strip()
    if not iso or not title:
        return None
    try:
        start_dt = _dt.datetime.fromisoformat(iso)
    except Exception:
        return None                              # ungueltiges ISO -> kein Record
    if not yc.event_datetime_sane(start_dt):
        return {"type": "event", "detail": f"{title} (ungueltiges Datum)", "ok": False}
    dur = args.get("duration_min")
    try:
        dur = int(dur) if dur is not None else 60
    except Exception:
        dur = 60
    ok = bool(yc.create_event(title, start_dt, duration_min=dur))
    detail = f"{title} · {start_dt.strftime('%d.%m.%Y %H:%M')}"
    return {"type": "event", "detail": detail, "ok": ok}


def _resolve_event_ref(ref):
    """Ordinal-Nummer N (int, aus dem #N-Pool) -> Event-dict aus upcoming_events_with_ids,
    oder None bei ungueltiger/unbekannter Nummer. Der Decider liefert ref laut Tool-Schema
    als int. Zustandslos: die Liste ist deterministisch nach Startzeit sortiert."""
    try:
        n = int(ref)
    except (TypeError, ValueError):
        return None
    for e in yc.upcoming_events_with_ids():
        if e["id"] == n:
            return e
    return None


def _exec_update_event(args):
    ev = _resolve_event_ref(args.get("ref"))
    if ev is None:
        return {"type": "event_update",
                "detail": f"Termin #{args.get('ref')} nicht gefunden", "ok": False}
    title = (args.get("title") or "").strip() or None
    iso = (args.get("datetime") or "").strip()
    start_dt = None
    if iso:
        try:
            start_dt = _dt.datetime.fromisoformat(iso)
        except Exception:
            return {"type": "event_update",
                    "detail": f"{ev['title']} (ungueltiges Datum)", "ok": False}
        if not yc.event_datetime_sane(start_dt):
            return {"type": "event_update",
                    "detail": f"{ev['title']} (Datum ausserhalb Bereich)", "ok": False}
    dur = args.get("duration_min")
    try:
        dur = int(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    if start_dt is None and title is None and dur is None:
        return {"type": "event_update",
                "detail": f"{ev['title']} (nichts zu aendern)", "ok": False}
    ok = bool(yc.update_event(ev["uid"], start_dt=start_dt, title=title, duration_min=dur))
    shown = title or ev["title"]
    when = (start_dt or ev["start"]).strftime("%d.%m.%Y %H:%M")
    return {"type": "event_update", "detail": f"{shown} · {when}", "ok": ok}


def _exec_delete_event(args):
    ev = _resolve_event_ref(args.get("ref"))
    if ev is None:
        return {"type": "event_delete",
                "detail": f"Termin #{args.get('ref')} nicht gefunden", "ok": False}
    ok = bool(yc.delete_event(ev["uid"]))
    return {"type": "event_delete",
            "detail": f"{ev['title']} · {ev['start'].strftime('%d.%m.%Y %H:%M')}", "ok": ok}


def _exec_list_create(args):
    title = (args.get("title") or "").strip()
    if not title:
        return None
    kind = (args.get("kind") or "shopping").strip() or "shopping"
    items = args.get("items") if isinstance(args.get("items"), list) else None
    activate = bool(args.get("activate"))
    lst = yc.create_list(title, kind=kind, items=items, activate=activate)
    if not lst:
        return {"type": "list", "detail": title, "ok": False}
    n = len(items or [])
    detail = f"{title}" + (f" · {n} Eintraege" if n else "")
    return {"type": "list", "detail": detail, "ok": True,
            "_sse": [{"kind": "list_changed", "reason": "created", "id": lst["id"]}]}


def _exec_list_activate(args):
    ref = (args.get("list") or "").strip()
    if not ref:
        return None
    target = yc.find_list_by_ref(ref)
    if not target:
        return None                              # unaufloesbar -> kein Record
    cur = yc.active_list()
    if cur and cur.get("id") != target.get("id"):
        # Invariante: kein stiller Wechsel bei anderer aktiver Liste
        return {"type": "list", "detail": f"{target.get('title', '')} (Wechsel bestaetigen)",
                "ok": False,
                "_sse": [{"kind": "list_changed", "reason": "activate_blocked", "id": target["id"]}]}
    ok = bool(yc.set_list_active(target["id"], True))
    return {"type": "list", "detail": f"{target.get('title', '')} aktiviert", "ok": ok,
            "_sse": [{"kind": "list_changed", "reason": "active", "id": target["id"]}]}


def _exec_list_check(args):
    ref = (args.get("item") or "").strip()
    if not ref:
        return None
    cur = yc.active_list()
    if not cur:
        return None
    idx = yc.find_open_item_by_ref(cur, ref)
    if idx is None:
        return None                              # kein offenes Item -> kein Record
    ok = bool(yc.set_list_item_voice_hint(cur["id"], idx, "erwaehnt"))
    return {"type": "list", "detail": f"✓ {ref}", "ok": ok,
            "_sse": [{"kind": "list_changed", "reason": "hint", "id": cur["id"]}]}


def _exec_timer(args, target_client_id=None, allow_timer=True):
    if not allow_timer:
        return None                              # Watch/Voice-PE koennen nicht wecken -> kein Orphan-Timer
    try:
        sec = int(args.get("seconds"))
    except (TypeError, ValueError):
        return None                              # kein/kaputtes seconds -> kein Record
    if sec < yc.TIMER_MIN_SEC or sec > yc.TIMER_MAX_SEC:
        return None                              # ausserhalb Range -> kein Record (18h-Schutz)
    label = (args.get("label") or "").strip() or "Timer"
    tmr = yc.start_timer(sec, label, target_client_id=target_client_id)
    if not tmr:
        return {"type": "timer", "detail": label, "ok": False}
    m, s = divmod(sec, 60)
    dur = f"{m}:{s:02d}" if m else f"{s}s"
    ev = {"kind": "timer_started", "id": tmr["id"], "end_ts": int(tmr["ends_at"]),
          "duration_sec": sec, "label": label}
    if target_client_id:
        ev["target_client_id"] = target_client_id
    return {"type": "timer", "detail": f"{label} · {dur}", "ok": True, "_sse": [ev]}


def _exec_gedankenbild(args):
    """Traum-/Vorstellungs-Bild via ComfyUI. Reachability-Recheck ZUERST (schneller
    Fail bei Dienst-aus), sonst blocking ~60s. Speichert wie ein Keepsake + pinnt in
    die Galerie. _sse traegt entweder das fertige Bild oder ein 'offline'-Signal."""
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return None
    style = (args.get("style") or "").strip().lower() or None
    if not imagegen.is_comfyui_reachable():
        return {"type": "gedankenbild", "detail": prompt[:60], "ok": False,
                "_sse": [{"kind": "gedankenbild_unavailable"}]}
    img = imagegen.generate(prompt, style=style)
    if not img:
        return {"type": "gedankenbild", "detail": prompt[:60], "ok": False,
                "_sse": [{"kind": "gedankenbild_unavailable"}]}
    path = yc.save_gedankenbild(img, prompt, prompt=prompt, style=style or "")
    if not path:
        return {"type": "gedankenbild", "detail": prompt[:60], "ok": False,
                "_sse": [{"kind": "gedankenbild_unavailable"}]}
    fname = path.name
    # Volle Prompt als Galerie-Caption: die Kachel clampt visuell auf 2 Zeilen,
    # ein Tap klappt den ganzen Text auf (.galCap.expanded). Ein hartes prompt[:120]
    # hier hat den Rest PERMANENT verworfen -> Text sah mitten im Wort abgeschnitten
    # aus und liess sich nicht mehr aufklappen. (Der volle Prompt steht ohnehin im
    # .md-Sidecar.) 'detail' bleibt kurz - das ist nur das Log-/Action-Icon-Label.
    yc.add_to_gallery("gedankenbild", fname, origin="yuki", caption=prompt)
    return {"type": "gedankenbild", "detail": prompt[:60], "ok": True, "file": fname,
            "prompt": prompt, "style": style or "",
            "_sse": [{"kind": "gedankenbild", "image": fname, "caption": prompt,
                      "can_regen": True}]}


# Ausfuehrungs-Phasen: Listen-Setup (anlegen/aktivieren) MUSS vor dem Item-Abhaken
# laufen, sonst scheitert "aktivier Liste X und hak Item Y ab" in EINEM Turn -
# check_list_item liest active_list(), das erst nach dem activate/create stimmt.
# Alles andere ist unabhaengig -> Phase 1. Stable-sort haelt die Emissions-Reihenfolge
# innerhalb einer Phase.
# create_routine ebenfalls Phase 0: falls Michael eine Routine anlegt UND im selben
# Turn abhakt, muss sie existieren, bevor mark_routine_done (Phase 1) sie sucht.
_EXEC_PHASE = {"create_list": 0, "activate_list": 0, "create_routine": 0}


def execute_action_decisions(tool_calls, now=None, target_client_id=None, allow_timer=True,
                             note_source="michael"):
    """E: fuehrt die vom Decider vorgeschlagenen Tool-Calls aus (dieselben yc.*-
    Funktionen wie der alte Inline-Pfad) und liefert Records {type, detail, ok}.
    Reihenfolge: Listen-Setup vor Item-Check (stable-sort nach _EXEC_PHASE). Dedup
    pro (type, detail) - laesst verschiedene Listen-Ops (aktivieren + abhaken) und
    mehrere Items durch, filtert nur ECHTE Duplikate. Unbekannte/ungueltige uebersprungen."""
    parsed = [_args_of(tc) for tc in (tool_calls or [])]
    order = sorted(range(len(parsed)),
                   key=lambda i: (_EXEC_PHASE.get(parsed[i][0], 1), i))
    records, seen = [], set()
    for i in order:
        name, args = parsed[i]
        if name == "mark_routine_done":
            rec = _exec_routine_done(args)
        elif name == "ha_control":
            rec = _exec_ha(args)
        elif name == "create_note":
            rec = _exec_note(args, note_source, now=now)
        elif name == "create_event":
            rec = _exec_event(args)
        elif name == "create_list":
            rec = _exec_list_create(args)
        elif name == "activate_list":
            rec = _exec_list_activate(args)
        elif name == "check_list_item":
            rec = _exec_list_check(args)
        elif name == "create_routine":
            rec = _exec_routine_create(args)
        elif name == "start_timer":
            rec = _exec_timer(args, target_client_id, allow_timer)
        elif name == "update_event":
            rec = _exec_update_event(args)
        elif name == "delete_event":
            rec = _exec_delete_event(args)
        elif name == "gedankenbild":
            rec = _exec_gedankenbild(args)
        else:
            rec = None
        if not rec:
            continue
        key = (rec["type"], rec.get("detail", ""))
        if key in seen:
            continue
        seen.add(key)
        records.append(rec)
    return records
