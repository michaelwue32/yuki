"""yuki_stories.py - Persistenter Speicher fuer die "ganzen Geschichten" der Erzaehlerin.

Orthogonal zum 6-Tier-Gedaechtnis und zur conversation.json - genau wie die Adventures
(memory/adventures/<id>.json). Eine im "Ganze Geschichte"-Modus erzeugte Geschichte landet
hier als reiner TEXT (Titel + Absaetze), NIE im Chat-Verlauf. Dadurch:

  * Der volle Wortlaut leakt nie in den Canon (die Verdichtung sieht ihn nicht; sie sieht
    nur den kurzen Hinweis, den der Chat-Turn traegt - und die Erzaehlerin ist ohnehin
    no_canon, [[yuki-personas]]).
  * Die Library (📚) kann Geschichten spaeter wieder anhoeren, weitererzaehlen oder
    loeschen - auch wenn der Chat laengst verdichtet wurde.

Audio wird BEWUSST nicht gespeichert: F5-TTS ist lokal/billig, die Wiedergabe synthetisiert
beim Anhoeren neu. Das haelt die Datei winzig und die Stimme frisch.

Schema (memory/yuki_stories.json):
  { "stories": [ {
        "id": "s_<unix>_<hex>",
        "title": "Der Karpfen und der alte Mann",
        "paragraphs": ["Absatz 1 ...", "Absatz 2 ...", ...],
        "persona": "storyteller",
        "created": "2026-06-21 14:30",
        "updated": "2026-06-21 14:30",
        "continued_from": null | "<id der Vorgaenger-Geschichte>"
    }, ... ],
    "updated": "2026-06-21 14:30" }
"""

import json
import time
import uuid
import threading
from pathlib import Path

_ROOT = Path(__file__).parent
STORIES_FILE = _ROOT / "memory" / "yuki_stories.json"

_LOCK = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _new_id():
    # Zeit-Prefix haelt die Datei grob chronologisch lesbar; hex-Suffix gegen Kollision
    # bei zwei Geschichten in derselben Sekunde.
    return f"s_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def load_stories():
    """Liste aller Story-Dicts (robust gegen fehlende/kaputte Datei)."""
    if STORIES_FILE.exists():
        try:
            data = json.loads(STORIES_FILE.read_text(encoding="utf-8"))
            stories = data.get("stories", [])
            return stories if isinstance(stories, list) else []
        except Exception:
            return []
    return []


def _save(stories):
    try:
        STORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        STORIES_FILE.write_text(
            json.dumps({"stories": stories, "updated": _now()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        return True
    except Exception as e:
        print(f"  [Stories-Speichern fehlgeschlagen: {e}]", flush=True)
        return False


def _clean_paragraphs(paragraphs):
    """Liste -> getrimmte, nicht-leere Absaetze. Strings werden an Leerzeilen gesplittet,
    falls jemand einen Block statt einer Liste uebergibt (defensiv)."""
    if isinstance(paragraphs, str):
        import re
        paragraphs = re.split(r"\n\s*\n", paragraphs)
    out = []
    for p in (paragraphs or []):
        t = (p or "").strip()
        if t:
            out.append(t)
    return out


def create_story(title, paragraphs, persona="storyteller", continued_from=None):
    """Neue Geschichte anlegen. Gibt das gespeicherte Story-Dict zurueck (mit id),
    oder None wenn keine validen Absaetze drin sind."""
    paras = _clean_paragraphs(paragraphs)
    if not paras:
        return None
    story = {
        "id": _new_id(),
        "title": (title or "").strip() or "Eine Geschichte",
        "paragraphs": paras,
        "persona": persona or "storyteller",
        "created": _now(),
        "updated": _now(),
        "continued_from": continued_from,
    }
    with _LOCK:
        stories = load_stories()
        stories.append(story)
        _save(stories)
    return story


def get_story(story_id):
    """Volles Story-Dict zu einer id (oder None)."""
    if not story_id:
        return None
    for s in load_stories():
        if s.get("id") == story_id:
            return s
    return None


def branch_story(parent_id, cut_index, new_paragraphs, title=None):
    """Verzweigen ("ab Stelle neu weitererzaehlen"): legt eine NEUE Geschichte an aus
    dem Praefix der Eltern-Geschichte (Absaetze [0 .. cut_index]) + der neuen
    Fortsetzung. Die Eltern-Geschichte bleibt voellig unangetastet - `continued_from`
    verlinkt das Kind mit ihr (Abstammung). Gibt das neue Story-Dict zurueck (oder None,
    wenn die Eltern unbekannt sind oder keine validen neuen Absaetze drin stehen).

    Bewusst KEIN In-Place-Mutieren (anders als append_chapter): die alte Fassung soll
    erhalten bleiben, damit man Varianten vergleichen + bei Bedarf einzeln loeschen kann.
    Wird im Endpoint erst NACH erfolgreicher LLM-Generierung gerufen -> ein Fehlschlag
    beschaedigt nie eine bestehende Geschichte."""
    paras_new = _clean_paragraphs(new_paragraphs)
    if not paras_new:
        return None
    with _LOCK:
        stories = load_stories()
        parent = next((s for s in stories if s.get("id") == parent_id), None)
        if parent is None:
            return None
        old = parent.get("paragraphs") or []
        keep = max(0, min(int(cut_index) + 1, len(old)))
        kept = [p for p in old[:keep] if (p or "").strip()]
        child = {
            "id": _new_id(),
            "title": (title or parent.get("title") or "").strip() or "Eine Geschichte",
            "paragraphs": kept + paras_new,
            "persona": parent.get("persona") or "storyteller",
            "created": _now(),
            "updated": _now(),
            "continued_from": parent_id,
            # Anzahl uebernommener Absaetze = ab welchem Absatz neu erzaehlt wurde
            # (rein fuer die Library-Anzeige "Fortsetzung ab Absatz N").
            "branch_point": len(kept),
        }
        stories.append(child)
        _save(stories)
    return child


def append_chapter(story_id, paragraphs):
    """Weitererzaehlen: neue Absaetze an eine bestehende Geschichte haengen. Gibt das
    aktualisierte Story-Dict zurueck (oder None wenn id unbekannt / nichts anzuhaengen)."""
    paras = _clean_paragraphs(paragraphs)
    if not paras:
        return None
    with _LOCK:
        stories = load_stories()
        for s in stories:
            if s.get("id") == story_id:
                s.setdefault("paragraphs", []).extend(paras)
                s["updated"] = _now()
                _save(stories)
                return s
    return None


def set_summary(story_id, summary):
    """Mini-Inhaltsangabe (2-3 Saetze) an eine Geschichte heften - fuer die Library-
    Uebersicht. Wird async nach der Generierung ODER on-demand beim ℹ-Klick gesetzt.
    `updated` bleibt bewusst unangetastet (keine inhaltliche Aenderung, soll die
    Sortierung nicht verschieben). True wenn gespeichert."""
    summary = (summary or "").strip()
    with _LOCK:
        stories = load_stories()
        for s in stories:
            if s.get("id") == story_id:
                s["summary"] = summary
                _save(stories)
                return True
    return False


def set_title(story_id, custom_title):
    """Eigenen Haupttitel setzen. Leer -> custom_title entfernen (zurueck auf den
    Original-Titel). Der Original-`title` (LLM-generiert, bei der Erzeugung gesetzt und
    danach NIE mutiert) bleibt immer erhalten und dient als Untertitel - deshalb
    ueberschreibt erneutes Umbenennen den Untertitel nie. `updated` bleibt unangetastet
    (Metadaten-Aenderung, soll die Library-Sortierung nicht verschieben). True wenn
    gespeichert."""
    ct = (custom_title or "").strip()[:120]
    with _LOCK:
        stories = load_stories()
        for s in stories:
            if s.get("id") == story_id:
                if ct:
                    s["custom_title"] = ct
                else:
                    s.pop("custom_title", None)
                _save(stories)
                return True
    return False


def delete_story(story_id):
    """Geschichte loeschen. Kinder (die per `continued_from` an dieser Geschichte
    haengen) werden auf deren Eltern umgehaengt, damit die Abstammungs-Kette beim
    Loeschen mittendrin / am Anfang nicht reisst. Zeigte das Geloeschte selbst auf
    nichts (Wurzel), werden seine Kinder zu neuen Wurzeln. True wenn etwas entfernt
    wurde."""
    with _LOCK:
        stories = load_stories()
        target = next((s for s in stories if s.get("id") == story_id), None)
        if target is None:
            return False
        new_parent = target.get("continued_from")   # ggf. None -> Kinder werden Wurzeln
        kept = []
        for s in stories:
            if s.get("id") == story_id:
                continue
            if s.get("continued_from") == story_id:
                s = dict(s)
                s["continued_from"] = new_parent
            kept.append(s)
        _save(kept)
        return True


def _summary(s, parent_ids=None):
    """Schlanke Listen-Repraesentation (ohne den vollen Absatz-Text). `parent_ids` ist
    die Menge aller `continued_from`-Werte -> `has_children` markiert eine erweiterte
    (verzweigte) Geschichte fuers Library-Tree-Rendering."""
    paras = s.get("paragraphs") or []
    return {
        "id": s.get("id"),
        "title": s.get("title") or "Eine Geschichte",
        "custom_title": (s.get("custom_title") or ""),
        "created": s.get("created"),
        "updated": s.get("updated") or s.get("created"),
        "paragraph_count": len(paras),
        "continued_from": s.get("continued_from"),
        "branch_point": s.get("branch_point"),
        "has_children": bool(parent_ids and s.get("id") in parent_ids),
        "summary": (s.get("summary") or ""),
        # erster Absatz angeschnitten als Vorschau-Teaser fuer die Library-Karte
        "preview": (paras[0][:140] if paras else ""),
    }


def list_stories():
    """Alle Geschichten als schlanke Summaries, neueste zuerst (nach updated). Das
    Frontend baut daraus per `continued_from` den Abstammungs-Baum (treppenartig)."""
    stories = load_stories()
    parent_ids = {s.get("continued_from") for s in stories if s.get("continued_from")}
    stories = sorted(stories, key=lambda s: (s.get("updated") or s.get("created") or ""),
                     reverse=True)
    return [_summary(s, parent_ids) for s in stories]
