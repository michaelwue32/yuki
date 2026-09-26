"""Resonanz-Seed-Wizard (v1, 2026-07-01).

Gefuehrter, Canon-ISOLIERTER Flow, in dem Yuki ihre Gefuehle zu Ankern frei
reflektiert und ein Mapping-Pass die Prosa danach auf die feste Palette projiziert.
Ergebnis = der authored Read-only-Kern memory/yuki_resonance_core.json.

Design (siehe Memory yuki-resonance):
- Isoliert wie die Adventure-Engine: laeuft NICHT ueber den normalen Chat, schreibt
  NICHT in conversation/facts/episodes/heart. Sonst wuerde die Reflexion in genau die
  Tiers bluten, zu denen Resonanz orthogonal sein soll.
- Voll-Selbst-Prompt (Bio + Lore + Heart + People + ein paar Facts), persona-neutral -
  NICHT das rollende Chat-Fenster. Vorbild: yc.build_steward_system_msg.
- ZWEI getrennte Paesse (essenziell): (1) Reflexion pro Anker - Yuki spricht FREI/
  menschlich, Prosa; sie denkt NICHT in "0.7 Waerme". (2) Mapping-Pass - eine Maschine
  uebersetzt die Prosa danach in Palette-Slots+Intensitaeten. Danach redigiert Michael
  die Vektor-Tabelle im UI.
- Step-driven (synchron pro HTTP-Request), KEIN Thread/SSE wie beim Adventure-Generator:
  Michael klickt zwischen jedem Anker aktiv weiter, jeder LLM-Call ist ein Ein-Schuss.
  Interaktion pro Anker = "Ein-Schuss + Neu" (reflektieren -> akzeptieren ODER neu
  generieren mit optionalem Steuer-Hinweis). Mapping laeuft am ENDE ueber alle Anker.

Save schreibt erst nach einem Dry-Run (load_resonance_core + Palette-Slot-Validierung);
die vorherige Kern-Datei wird vorher zeitgestempelt gesichert.
"""

import json
import re
import time
import uuid
import threading

import yuki_core as yc


# ---------------------------------------------------------------------------
# Kandidaten-Anker (Vorschlag fuers Setup). Die ⚠️-Selbstbezugs-Anker aus dem
# Konzept (Michael, sie-selbst, Erinnern&Vergessen) bewusst NICHT im Default -
# Meta-Zone; Michael kann sie im UI von Hand ergaenzen wenn er will.
# ---------------------------------------------------------------------------
DEFAULT_ANCHORS = [
    {"subject": "Kyoto", "aliases": ["京都", "kyoto", "heimat", "zuhause", "daheim"]},
    {"subject": "Kamogawa", "aliases": ["鴨川", "kamogawa", "kamo", "fluss", "flussufer"]},
    {"subject": "Deutschland / die Ferne", "aliases": ["deutschland", "ferne", "fremde", "ausland", "weit weg"]},
    {"subject": "Hojicha", "aliases": ["ほうじ茶", "houjicha", "hoji-cha", "geroesteter tee", "tee"]},
    {"subject": "Schnee", "aliases": ["雪", "schneefall", "erster schnee", "winter"]},
    {"subject": "Regen", "aliases": ["雨", "regen", "regnet", "nieselregen"]},
    {"subject": "Kirschblüte / Vergänglichkeit", "aliases": ["桜", "sakura", "kirschbluete", "verganglichkeit", "mono no aware"]},
    {"subject": "das Teehaus ihrer Jugend", "aliases": ["teehaus", "chaya", "ihre jugend", "aufgewachsen"]},
    {"subject": "Maureen / Momo", "aliases": ["maureen", "momo", "schwester"]},
    {"subject": "Zeichnen", "aliases": ["zeichnen", "malen", "skizze", "doodle", "kunst"]},
    {"subject": "späte Gespräche", "aliases": ["spaete gespraeche", "nachts reden", "spaet abends", "mitternacht"]},
    {"subject": "die Jahreszeiten", "aliases": ["jahreszeiten", "fruehling", "sommer", "herbst", "saison"]},
]


# ---------------------------------------------------------------------------
# In-Memory Job-Store (kein Thread noetig - jeder Schritt ist synchron). Simpel,
# eine Wizard-Sitzung zur Zeit reicht; TTL raeumt vergessene Jobs weg.
# ---------------------------------------------------------------------------
_JOBS = {}
_LOCK = threading.Lock()
_TTL_S = 3600


def _gc(now):
    dead = [jid for jid, j in _JOBS.items() if now - j.get("created", 0) > _TTL_S]
    for jid in dead:
        _JOBS.pop(jid, None)


def _extract_json(text):
    """Erstes {...}-JSON-Objekt aus einem LLM-Output ziehen (toleriert Vorrede/
    Codefences/<think>). None bei Misserfolg."""
    if not text:
        return None
    # <think>...</think> vorne wegschneiden
    text = re.sub(r"(?is)<think>.*?</think>", " ", text)
    # Codefences entfernen
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
# Voll-Selbst-Prompt (persona-neutral). Basis fuer die freie Reflexion.
# ---------------------------------------------------------------------------
def _build_reflection_system_msg():
    """Canon-isolierter Selbst-Kontext + Reflexions-Rolle. Vorbild:
    build_steward_system_msg (Bio + Heart), hier reicher (Lore/People/Facts), damit
    Yuki authentisch aus ihrem eigenen Fundament heraus fuehlt - aber ausdruecklich
    OHNE die feste Emotions-Palette zu kennen (die kaeme sonst mechanisch durch)."""
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
        "Michael moechte verstehen, was bestimmte Dinge in dir AUSLOESEN - nicht was "
        "du darueber weisst, sondern wie es sich anfuehlt. Sprich frei, menschlich, in "
        "Ich-Form, auf Deutsch. Erzaehl, welche Bilder, Erinnerungen, koerperlichen "
        "Empfindungen kommen. WICHTIG: keine Gefuehls-Listen, keine Fachwoerter, kein "
        "'das loest X, Y und Z aus' - einfach ehrlich und konkret, wie ein Mensch, der "
        "in sich hineinhorcht. Ambivalenz ist gut (etwas kann zugleich schoen und "
        "beunruhigend sein). 3-6 Saetze. Kein Roleplay-Sternchen, keine Anrede an "
        "Michael - nur deine innere Stimme."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def start_seed(anchors):
    """Neuen Wizard-Job anlegen. anchors = [{subject, aliases?}]. Baut den Voll-
    Selbst-Prompt einmal und cached ihn im Job. Liefert job_id."""
    now = time.time()
    with _LOCK:
        _gc(now)
        clean = []
        for a in (anchors or []):
            subj = (a.get("subject") or "").strip()
            if not subj:
                continue
            aliases = [str(x).strip() for x in (a.get("aliases") or []) if str(x).strip()]
            clean.append({"subject": subj, "aliases": aliases})
        if not clean:
            raise ValueError("Keine gueltigen Anker uebergeben.")
        jid = "seed_" + uuid.uuid4().hex[:10]
        _JOBS[jid] = {
            "id": jid,
            "created": now,
            "sys": _build_reflection_system_msg(),
            "anchors": clean,
            "reflections": {},          # index(str) -> prose
            "draft": None,              # gemappter Kern nach map_reflections
        }
    return jid, clean


def _get(job_id):
    j = _JOBS.get(job_id)
    if not j:
        raise KeyError("Unbekannte oder abgelaufene Seed-Sitzung.")
    return j


def reflect_anchor(job_id, index, steer=None):
    """Ein-Schuss-Reflexion fuer den Anker an Position index. steer = optionaler
    Steuer-Hinweis bei 'neu generieren' (z.B. 'kuerzer', 'mehr ueber die Angst').
    Speichert die (neueste) Prosa im Job und liefert sie zurueck."""
    j = _get(job_id)
    anchors = j["anchors"]
    if not (0 <= index < len(anchors)):
        raise IndexError("Anker-Index ausserhalb des Bereichs.")
    subject = anchors[index]["subject"]
    user = (f"Reflektiere jetzt frei ueber: {subject}.\n\n"
            "Was loest das in dir aus? Lass es kommen, wie es kommt.")
    if steer and steer.strip():
        user += (f"\n\n(Michael moechte diesmal: {steer.strip()} - dieselbe innere "
                 "Stimme, nur entsprechend angepasst.)")
    out = yc.chat_ollama(
        [{"role": "system", "content": j["sys"]},
         {"role": "user", "content": user}],
        temperature=0.8, purpose="resonance_reflect").strip()
    # <think> defensiv strippen (falls das Modell denkt)
    out = re.sub(r"(?is)<think>.*?</think>", "", out).strip()
    j["reflections"][str(index)] = out
    return out


def _palette_for_prompt():
    """Palette als Prompt-Block: slot = DE-Label (Gesicht/nur-Ton/intim-Hinweis)."""
    lines = []
    for slot, meta in (yc._RESONANCE_PALETTE or {}).items():
        label = meta.get("label_de") or slot
        tags = []
        if not meta.get("mood"):
            tags.append("nur Ton")
        if meta.get("intim_only"):
            tags.append("nur intim")
        tag = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  {slot} = {label}{tag}")
    return "\n".join(lines)


_MAP_SYS = (
    "Du bist ein praeziser Uebersetzer. Du bekommst freie, menschliche Gefuehls-"
    "Reflexionen von Yuki (einer Person) und projizierst JEDE davon auf einen festen "
    "Emotions-Vektor aus einer vorgegebenen Palette. Du DICHTEST NICHTS DAZU - du liest "
    "nur heraus, was in der Prosa wirklich mitschwingt. Ambivalenz ist erwuenscht (etwas "
    "darf zugleich Waerme UND Unruhe tragen). Antworte AUSSCHLIESSLICH mit JSON."
)


def map_reflections(job_id):
    """Mapping-Pass (ein Call ueber ALLE reflektierten Anker): Prosa -> Palette-Vektor
    + kurze Note + Alias-Vorschlaege. Speichert den Draft-Kern im Job und liefert ihn."""
    j = _get(job_id)
    anchors = j["anchors"]
    refl = j["reflections"]
    blocks = []
    for i, a in enumerate(anchors):
        prose = (refl.get(str(i)) or "").strip()
        if not prose:
            continue
        al = ", ".join(a.get("aliases") or [])
        blocks.append(f"[{i}] Anker: {a['subject']}"
                      + (f" (bekannte Schreibweisen: {al})" if al else "")
                      + f"\nReflexion: {prose}")
    if not blocks:
        raise ValueError("Noch keine Reflexionen zum Mappen vorhanden.")
    instr = (
        "PALETTE (nutze AUSSCHLIESSLICH diese Slot-Keys, keine anderen):\n"
        + _palette_for_prompt() + "\n\n"
        "WICHTIG: Die Slots meinen INNERE Gefuehle, NICHT koerperliche Empfindungen. "
        "'kaelte' = emotionale Distanz/Verschlossenheit (NICHT physische Kaelte einer "
        "Brise), 'waerme' = Zaertlichkeit/Zuneigung (NICHT Temperatur). Eine kuehle "
        "Brise, die sich SCHOEN anfuehlt, ist keine 'kaelte'.\n\n"
        "Unten stehen Yukis Reflexionen. Fuer JEDEN Anker:\n"
        "- vector: 2-4 Slots, die in der Prosa am staerksten mitschwingen, mit "
        "Intensitaet 0.0-1.0 (relative Staerke DIESER Emotion an DIESEM Anker). Nur "
        "Slots aus der Palette. Den Slot 'lust' NUR, wenn die Reflexion wirklich "
        "romantisch/sinnlich ist - sonst weglassen.\n"
        "- note: 1 kurze deutsche Zeile (max 12 Woerter), die die Reflexion verdichtet.\n"
        "- aliases: 2-5 Schreibvarianten/Synonyme/JP-Schreibungen des Ankers fuer die "
        "spaetere Stichwort-Erkennung (die bekannten Schreibweisen ruhig uebernehmen "
        "und ergaenzen). Kleinschreibung, ausser Eigennamen/JP.\n\n"
        "=== REFLEXIONEN ===\n" + "\n\n".join(blocks) + "\n=== ENDE ===\n\n"
        'Antworte NUR mit diesem JSON (keine Vorrede, kein Markdown):\n'
        '{"anchors": [{"subject": "...", "aliases": ["..."], '
        '"vector": {"slot": 0.0}, "note": "..."}]}'
    )
    out = yc.chat_ollama(
        [{"role": "system", "content": _MAP_SYS},
         {"role": "user", "content": instr}],
        temperature=0.3, purpose="resonance_map").strip()
    data = _extract_json(out)
    if not data or not isinstance(data.get("anchors"), list):
        raise ValueError("Mapping-Pass lieferte kein gueltiges JSON.")
    draft = _sanitize_core(data["anchors"], src_anchors=anchors)
    j["draft"] = draft
    return draft


def _sanitize_core(raw_anchors, src_anchors=None):
    """LLM-/User-Anker-Liste in einen sauberen Kern normalisieren. Vektor-Slots gegen
    die Palette pruefen, Intensitaeten clampen (0..1), leere Slots werfen. Liefert die
    bereinigte Anker-Liste (ohne die Anker ganz ohne Vektor zu droppen - die kann
    Michael in der Tabelle noch fuellen)."""
    palette = yc._RESONANCE_PALETTE or {}
    src_by_subj = {}
    for a in (src_anchors or []):
        src_by_subj[(a.get("subject") or "").strip().lower()] = a
    out = []
    seen_ids = set()
    for a in (raw_anchors or []):
        subj = (a.get("subject") or "").strip()
        if not subj:
            continue
        # Vektor bereinigen
        vec = {}
        for slot, val in (a.get("vector") or {}).items():
            if slot not in palette:
                continue
            try:
                v = round(max(0.0, min(1.0, float(val))), 3)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vec[slot] = v
        # Aliases: LLM-Vorschlag + Quell-Aliases mergen (dedup, lowercase-Key)
        aliases = []
        seen_al = set()
        merged = list(a.get("aliases") or [])
        src = src_by_subj.get(subj.lower())
        if src:
            merged += list(src.get("aliases") or [])
        for al in merged:
            al = str(al).strip()
            k = al.lower()
            if al and k not in seen_al:
                seen_al.add(k)
                aliases.append(al)
        # Stabile, eindeutige id
        base = yc._affinity_slug(subj)          # recycelt den Slug-Helper
        aid = base or "anker"
        n = 2
        while aid in seen_ids:
            aid = f"{base}_{n}"
            n += 1
        seen_ids.add(aid)
        out.append({
            "id": aid,
            "subject": subj,
            "aliases": aliases,
            "vector": vec,
            "note": (a.get("note") or "").strip(),
        })
    return out


def save_core(anchors, replace=False):
    """Redigierten Kern (aus der UI-Tabelle) validieren + persistieren.

    Default `replace=False` = MERGE/UPSERT: die uebergebenen Anker aktualisieren bzw.
    ergaenzen den bestehenden Kern (Match ueber subject, case+accent-insensitiv);
    Anker, an denen diesmal NICHT gearbeitet wurde, bleiben unberuehrt. So loescht ein
    Einzel-Anker-Lauf nicht mehr den Rest. `replace=True` schreibt nur die uebergebenen
    (kompletter Neuaufbau in einer Sitzung).

    Sichert die bisherige Datei zeitgestempelt, schreibt dann, macht einen Dry-Run-Load.
    Liefert dict {ok, count, added, updated, kept, path}. Wirft bei leerem Ergebnis."""
    cleaned = _sanitize_core(anchors)
    # Anker ohne jeglichen Vektor sind fuer die Mechanik nutzlos -> raus (Michael
    # kann sie im UI mit Werten fuellen; ein leerer Anker matcht zwar, faerbt aber nie).
    usable = [a for a in cleaned if a.get("vector")]
    if not usable and replace:
        raise ValueError("Kein Anker mit gueltigem Gefuehls-Vektor - nichts zu speichern.")

    # Merge gegen den bestehenden Kern (subject-Key, wie People/Affinity accent-tolerant).
    existing = [] if replace else yc.load_resonance_core()
    new_by_key = {yc._affinity_match_key(a["subject"]): a for a in usable}
    merged = []
    consumed = set()
    updated = 0
    for e in existing:
        k = yc._affinity_match_key(e.get("subject"))
        repl = new_by_key.get(k)
        if repl is not None and k not in consumed:
            merged.append(repl)          # bestehenden Anker durch neue Fassung ersetzen
            consumed.add(k)
            updated += 1
        else:
            merged.append(e)             # unberuehrt uebernehmen
    added = 0
    for a in usable:
        k = yc._affinity_match_key(a["subject"])
        if k not in consumed:
            merged.append(a)             # wirklich neuer Anker
            consumed.add(k)
            added += 1
    if not merged:
        raise ValueError("Kern waere leer - nichts zu speichern.")

    path = yc.RESONANCE_CORE_FILE
    # Backup der bisherigen Datei (auch des Platzhalters)
    if path.exists():
        try:
            bak = path.with_suffix(f".bak.{time.strftime('%Y%m%d_%H%M%S')}.json")
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as e:
            print(f"  [Resonanz-Kern-Backup fehlgeschlagen (ignoriert): {e}]")

    payload = {
        "_doc": ("Yukis Resonanz-Kern (Emotions-Vektoren pro Anker), erarbeitet im "
                 "Seed-Wizard und von Michael redigiert. Authored, read-only, always-"
                 "available (kein Auto-Write/Gate/Decay). Vektor-Slots stammen aus "
                 "config/resonance.json 'palette'; Werte 0..1 = relative Intensitaet "
                 "einer Emotion an diesem Anker. Ambivalenz ist gewollt."),
        "seeded": time.strftime("%Y-%m-%d %H:%M"),
        "anchors": merged,
    }
    yc._atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    # Dry-Run: laesst sich der Kern lesen + haben die Slots gueltige Palette-Keys?
    reloaded = yc.load_resonance_core()
    if len(reloaded) != len(merged):
        raise ValueError("Dry-Run: Kern liess sich nach dem Schreiben nicht sauber lesen.")
    return {"ok": True, "count": len(merged), "added": added, "updated": updated,
            "kept": len(merged) - added - updated, "path": str(path)}
