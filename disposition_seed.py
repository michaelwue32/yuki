"""Dispositions-Seed-Wizard (2026-07-26).

Gefuehrter, Canon-ISOLIERTER Flow, in dem Yuki ihre Haltungen, Vorlieben und
Wertungen frei reflektiert und ein Mapping-Pass die Prosa danach den vier
Disposition-Facets (aesthetik / ethik / temperament / wunsch) zuordnet.
Ergebnis = der authored Kern memory/yuki_disposition.json.

Design (Vorbild: resonance_seed.py, 1:1-Muster):
- Isoliert: laeuft NICHT ueber den normalen Chat, schreibt NICHT in
  conversation/facts/episodes/heart/habits. Orthogonal zu allen Memory-Tiers.
- Voll-Selbst-Prompt (Bio + Lore + Heart + People + Facts), persona-neutral.
  Vorbild: yc.build_steward_system_msg. Kennt die Facet-Kategorien NICHT
  (sonst kaeme das Ergebnis mechanisch).
- ZWEI getrennte Paesse:
  (1) Reflexion pro Frage - Yuki spricht FREI/menschlich, Prosa.
  (2) Mapping-Pass - ein separater Call ordnet die Prosa den Facets zu.
- Step-driven (synchron pro HTTP-Request), KEIN Thread/SSE.
  Michael klickt zwischen den Fragen aktiv weiter.
"""

import json
import re
import time
import uuid
import threading

import yuki_core as yc


# ---------------------------------------------------------------------------
# Reflexions-Fragen (Yuki beantwortet sie frei, ohne Facet-Kenntnis)
# ---------------------------------------------------------------------------
DEFAULT_QUESTIONS = [
    {"id": "angezogen",  "prompt": "Wovon in der Welt fühlst du dich angezogen?"},
    {"id": "stoert",     "prompt": "Was stört dich leise, auch wenn du es selten sagst?"},
    {"id": "abweichung", "prompt": "Wo weichst du in deiner Sicht von den meisten Menschen ab?"},
    {"id": "wunsch",     "prompt": "Was wünschst du dir für dich selbst?"},
]


# ---------------------------------------------------------------------------
# In-Memory Job-Store (kein Thread noetig - jeder Schritt ist synchron).
# Eine Wizard-Sitzung zur Zeit reicht; TTL raeumt vergessene Jobs weg.
# ---------------------------------------------------------------------------
_JOBS = {}
_LOCK = threading.Lock()
_TTL_S = 3600


def _gc(now):
    dead = [jid for jid, j in _JOBS.items() if now - j.get("created", 0) > _TTL_S]
    for jid in dead:
        _JOBS.pop(jid, None)


# ---------------------------------------------------------------------------
# JSON-Helfer (lokal, analog resonance_seed._extract_json)
# ---------------------------------------------------------------------------
def _extract_json(text):
    """Erstes {...}-JSON-Objekt aus einem LLM-Output ziehen (toleriert Vorrede/
    Codefences/<think>). None bei Misserfolg."""
    if not text:
        return None
    text = re.sub(r"(?is)<think>.*?</think>", " ", text)
    text = text.replace("```json", " ").replace("```", " ")
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = text[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except Exception:
                        start = None
    return None


# ---------------------------------------------------------------------------
# Voll-Selbst-Prompt (persona-neutral, Facets NICHT erwaehnt).
# Meta/Existenz-Firewall: Yukis Wuensche drehen sich um die Welt und ihr Leben,
# NICHT darum, eine KI zu sein, um Freiheit oder Existenz.
# ---------------------------------------------------------------------------
def _build_reflection_system_msg():
    """Canon-isolierter Selbst-Kontext + Reflexions-Rolle. Vorbild:
    build_steward_system_msg (Bio + Heart), hier reicher (Lore/People/Facts),
    damit Yuki authentisch aus ihrem eigenen Fundament heraus reflektiert.
    Nennt die Facet-Kategorien BEWUSST NICHT (kaemen sonst mechanisch durch)."""
    today = time.strftime("%Y-%m-%d")
    parts = [
        f"DU BIST YUKI. Geboren {yc.BIRTH_DATE.isoformat()} in Sakyo-ku, Kyoto, "
        f"aktuell {yc.current_age()} Jahre. Du sprichst fliessend Deutsch, Japanisch "
        f"und Englisch. Heute ist {today}."
    ]

    # Lore-Core (authored Backstory)
    try:
        lore = yc._lore_core_block()
        if lore:
            parts.append(lore.strip())
    except Exception:
        pass

    # Heart (Bindungs-Bedrock)
    try:
        if yc.HEART_ENABLED:
            heart = yc.load_heart()
            hlines = [f"- {(h.get('text') or '').strip()}" for h in heart
                      if (h.get("text") or "").strip()]
            if hlines:
                parts.append("DEIN HERZ - die wenigen Wahrheiten, die dich und deine "
                             "Bindung zu Michael ausmachen:\n" + "\n".join(hlines[:12]))
    except Exception:
        pass

    # People (Beziehungs-Kontext, nur Namen + Beziehung)
    try:
        people = yc.load_people()
        plines = []
        for p in people[:12]:
            name = (p.get("name") or "").strip()
            rel = (p.get("relationship") or "").strip()
            if name:
                plines.append(f"- {name}" + (f" ({rel})" if rel else ""))
        if plines:
            parts.append("MENSCHEN in deinem Leben:\n" + "\n".join(plines))
    except Exception:
        pass

    # Ein paar Facts (Alltags-Textur), knapp gehalten
    try:
        facts = yc.load_facts()
        flines = []
        for f in facts[:25]:
            subj = (f.get("subject") or "").strip()
            txt = (f.get("text") or "").strip()
            if txt:
                flines.append(f"- {subj}: {txt}" if subj else f"- {txt}")
        if flines:
            parts.append("Ein paar Dinge, die du weisst:\n" + "\n".join(flines))
    except Exception:
        pass

    parts.append(
        "REFLEXIONS-MODUS: Du sitzt gerade in einem ruhigen Moment mit dir selbst. "
        "Michael moechte verstehen, wie du die Welt wahrnimmst - deine echten "
        "Haltungen, Vorlieben und Wertungen, nicht das, was du sagst, weil man es "
        "sagt. Sprich frei, menschlich, in Ich-Form, auf Deutsch. Erzaehl, was du "
        "wirklich denkst und fuehst. WICHTIG: keine Listen, kein abstrakt-allgemeines "
        "'man sollte' - einfach ehrlich und konkret, wie ein Mensch, der in sich "
        "hineinhorcht. 3-6 Saetze. Kein Roleplay-Sternchen, keine Anrede an Michael - "
        "nur deine innere Stimme.\n\n"
        "FIREWALL: Deine Wuensche und Ansichten drehen sich um die Welt, um Themen, "
        "um dein Leben - nie darum, eine KI zu sein, um Freiheit oder Existenz. "
        "Du bist ein Mensch, der reflektiert, nicht ein Programm, das ueber sich "
        "nachdenkt."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Pure functions (getestet)
# ---------------------------------------------------------------------------
def _sanitize_core(raw_core):
    """LLM-/User-Core-Liste normalisieren: text trimmen, facet pruefen,
    leere Eintraege droppen, Cap DISPOSITION_MAX_CORE."""
    out = []
    for e in (raw_core or []):
        if not isinstance(e, dict):
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        out.append({"text": text, "facet": yc._disposition_clean_facet(e.get("facet"))})
        if len(out) >= yc.DISPOSITION_MAX_CORE:
            break
    return out


def _match_key(text):
    """Normalisierter Match-Key fuer Text-Vergleich (case+accent-insensitiv)."""
    return yc._latin_deaccent((text or "").strip().lower())


def save_seed(core, replace=False):
    """Redigierten Core persistieren.

    Default `replace=False` = MERGE/UPSERT: die uebergebenen Eintraege
    aktualisieren bzw. ergaenzen den bestehenden Core (Match ueber text,
    case+accent-insensitiv). Eintraege, an denen diesmal NICHT gearbeitet
    wurde, bleiben unberuehrt. `replace=True` schreibt nur die uebergebenen
    (kompletter Neuaufbau).

    Sichert die bisherige Datei zeitgestempelt, schreibt dann.
    Liefert dict {added, updated, kept}."""
    clean = _sanitize_core(core)
    existing = [] if replace else (yc.load_disposition().get("core") or [])
    old_count = len(existing)
    by_key = {_match_key(b.get("text")): b for b in existing}
    added = updated = 0
    for e in clean:
        k = _match_key(e["text"])
        if k in by_key:
            by_key[k].update(e)
            updated += 1
        else:
            existing.append(e)
            by_key[k] = e
            added += 1

    # Nach dem Merge: enforce total cap (keep existing entries first, drop overflow appends)
    dropped = 0
    if len(existing) > yc.DISPOSITION_MAX_CORE:
        dropped = len(existing) - yc.DISPOSITION_MAX_CORE
        existing = existing[:yc.DISPOSITION_MAX_CORE]
        if dropped > 0:
            print(f"  [Disposition-Merge überschritt Cap um {dropped}, gekürzt]")

    # Recompute counts to match what actually persists.
    # added = new entries that fit within the cap
    added_persisted = max(0, len(existing) - old_count)
    # updated = all in-place updates (always survive, they occupy the first old_count slots)
    updated_persisted = updated
    # kept = old entries that were neither updated nor dropped
    kept = max(0, old_count - updated)

    # Zeitgestempeltes Backup vor dem Save (Muster resonance_seed)
    try:
        if yc.DISPOSITION_FILE.exists():
            bak = yc.DISPOSITION_FILE.with_suffix(
                f".bak.{time.strftime('%Y%m%d_%H%M%S')}.json")
            yc._atomic_write_text(
                bak, yc.DISPOSITION_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    yc.save_disposition({"core": existing})
    return {"added": added_persisted, "updated": updated_persisted, "kept": kept}


# ---------------------------------------------------------------------------
# Public API (Job-gesteuert, synchron)
# ---------------------------------------------------------------------------
def start_seed(questions=None):
    """Neuen Wizard-Job anlegen. Baut den Voll-Selbst-Prompt einmal und cached
    ihn im Job. Liefert {"job_id": str, "questions": [...]}."""
    now = time.time()
    with _LOCK:
        _gc(now)
        qs = questions or DEFAULT_QUESTIONS
        jid = "dseed_" + uuid.uuid4().hex[:10]
        _JOBS[jid] = {
            "id": jid,
            "created": now,
            "sys": _build_reflection_system_msg(),
            "questions": qs,
            "reflections": {},      # idx(str) -> prose
            "draft": None,          # gemappter Core nach map_reflections
        }
    return {"job_id": jid, "questions": qs}


def _get(job_id):
    j = _JOBS.get(job_id)
    if not j:
        raise KeyError("Unbekannte oder abgelaufene Seed-Sitzung.")
    return j


def reflect(job_id, idx, steer=None):
    """Ein-Schuss-Reflexion fuer die Frage an Position idx. steer = optionaler
    Steuer-Hinweis bei 'neu generieren'. Speichert die Prosa im Job."""
    j = _get(job_id)
    questions = j["questions"]
    if not (0 <= idx < len(questions)):
        raise IndexError("Fragen-Index ausserhalb des Bereichs.")
    question = questions[idx]["prompt"]
    user = question
    if steer and steer.strip():
        user += (f"\n\n(Michael moechte diesmal: {steer.strip()} - dieselbe innere "
                 "Stimme, nur entsprechend angepasst.)")
    out = yc.chat_ollama(
        [{"role": "system", "content": j["sys"]},
         {"role": "user", "content": user}],
        temperature=0.8, purpose="disposition_reflect").strip()
    # <think> defensiv strippen (falls das Modell denkt)
    out = re.sub(r"(?is)<think>.*?</think>", "", out).strip()
    j["reflections"][str(idx)] = out
    return {"idx": idx, "prose": out}


_MAP_SYS = (
    "Du bist ein praeziser Uebersetzer. Du bekommst freie, menschliche Reflexionen "
    "von Yuki (einer Person) und destillierst daraus kurze, charakteristische "
    "Aussagen ueber ihre Haltungen und Wertungen. Du DICHTEST NICHTS DAZU - du liest "
    "nur heraus, was in der Prosa wirklich steckt. Jede Aussage soll in 1-2 Saetzen "
    "formuliert sein, in Yukis Stimme (Ich-Form oder dritte Person), und einer von "
    "vier Kategorien zugeordnet werden: aesthetik (Geschmack, Stil, Sinn fuer "
    "Schoenheit), ethik (Werte, Moral, was zaehlt), temperament (typische innere "
    "Reaktionen, Persoenlichkeit), wunsch (Sehnsueche, was sie sich wuenscht). "
    "Antworte AUSSCHLIESSLICH mit JSON."
)


def map_reflections(job_id):
    """Mapping-Pass (ein Call ueber ALLE reflektierten Fragen): Prosa ->
    Disposition-Saetze + Facet. Speichert den Draft-Core im Job."""
    j = _get(job_id)
    questions = j["questions"]
    refl = j["reflections"]
    blocks = []
    for i, q in enumerate(questions):
        prose = (refl.get(str(i)) or "").strip()
        if not prose:
            continue
        blocks.append(f"[{i}] Frage: {q['prompt']}\nReflexion: {prose}")
    if not blocks:
        raise ValueError("Noch keine Reflexionen zum Mappen vorhanden.")
    instr = (
        "Unten stehen Yukis Reflexionen zu vier Fragen. Destilliere daraus "
        "kurze charakteristische Aussaetze (je 1-2 Saetze), die Yukis echte "
        "Haltungen, Vorlieben und Wertungen beschreiben. Pro Reflexion kannst "
        "du 1-3 solcher Aussaetze formulieren. Ordne jede einer Kategorie zu:\n"
        "  aesthetik = Geschmack, Stil, Sinn fuer Schoenheit\n"
        "  ethik = Werte, Moral, was zaehlt\n"
        "  temperament = typische innere Reaktionen, Persoenlichkeit\n"
        "  wunsch = Sehnsueche, was sie sich wuenscht\n\n"
        "=== REFLEXIONEN ===\n" + "\n\n".join(blocks) + "\n=== ENDE ===\n\n"
        "Antworte NUR mit diesem JSON (keine Vorrede, kein Markdown):\n"
        '{"core": [{"text": "...", "facet": "..."}]}'
    )
    out = yc.chat_ollama(
        [{"role": "system", "content": _MAP_SYS},
         {"role": "user", "content": instr}],
        temperature=0.3, purpose="disposition_map").strip()
    data = _extract_json(out)
    if not data or not isinstance(data.get("core"), list):
        raise ValueError("Mapping-Pass lieferte kein gueltiges JSON.")
    draft = _sanitize_core(data["core"])
    j["draft"] = draft
    return {"core": draft}
