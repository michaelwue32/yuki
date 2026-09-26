"""Gaming Screen Companion — dependency-light logic for Yuki's "stille Beisitzerin".

This module must NOT import yuki_core at load time. The heavy functions
(chat, vision, web search, episode append) are injected by server.py at call time.
See docs/superpowers/specs/2026-07-03-yuki-gaming-companion-design.md
"""
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_GAMING_CONFIG_PATH = _ROOT / "config" / "gaming.json"

_GAMING_CONFIG_DEFAULTS = {
    "enabled_default": False,       # never auto-arm
    "capture_source": "gamecap",    # camera source name in config/cameras.json
    "poll_seconds": 8,              # base pause between frames (self-paced loop)
    "quiet_after_comment_sec": 20,  # min gap after she speaks
    "model_floor_b": 12,            # need a capable vision model
    "repeat_window": 6,             # last N comments considered for repetition
    "memory_window": 8,             # rolling gist/comment buffer size
    "same_scene_minutes": 5,        # threshold for the "still the same X?" nudge
    "episode_memo": True,           # write one report at session end
    "transcript_cap": 500,          # safety cap on the durable session transcript
    "report_chunk_beats": 60,       # map-reduce chunk size for the disarm report
    "report_paragraphs_hint": "3-5",# target length of the diary report
    # real living-room Voice-PE box (verified 2026-07-03); NOT the placeholder assist_satellite.voice_pe
    "ha_announce_entity": "assist_satellite.home_assistant_voice_090df6_assist_satellit",
    "knowledge_enabled": True,           # async Live-Wissens-Klotz an?
    "knowledge_interval_sec": 35,        # Takt des Wissens-Workers (langsamer als poll)
    "knowledge_frame_max_age_sec": 90,   # aelter -> Frame zu stale, Worker skippt
    "knowledge_max_categories": 8,       # Cap: Kategorien
    "knowledge_max_entries_per_category": 12,  # Cap: Eintraege je Kategorie
    "knowledge_max_chars": 3000,         # Cap: Gesamt-Serialisierung
    # Per-Modus-Overrides fuer den Takt. Nur diese drei Keys wirken pro Modus;
    # fehlt ein Key, gilt der flache Basiswert. Film ist bewusst seltener.
    "modes": {
        "film": {"poll_seconds": 45, "quiet_after_comment_sec": 300, "same_scene_minutes": 12},
    },
}


def load_gaming_config():
    """Read config/gaming.json fresh each call; overlay known keys over defaults."""
    cfg = dict(_GAMING_CONFIG_DEFAULTS)
    try:
        raw = json.loads(_GAMING_CONFIG_PATH.read_text(encoding="utf-8"))
        for k in _GAMING_CONFIG_DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    except FileNotFoundError:
        pass
    except Exception as e:  # keep defaults on malformed JSON
        print(f"[gaming] config parse error, using defaults: {e}")
    return cfg


_PER_MODE_KEYS = ("poll_seconds", "quiet_after_comment_sec", "same_scene_minutes")


def effective_gaming_config(cfg, mode):
    """Overlay cfg['modes'][mode] (nur _PER_MODE_KEYS) ueber die flache Basis-Config.
    Nicht-Modus-Keys (capture_source, ha_announce_entity, ...) bleiben unveraendert."""
    out = dict(cfg)
    sub = (cfg.get("modes") or {}).get(mode) or {}
    for k in _PER_MODE_KEYS:
        if k in sub:
            out[k] = sub[k]
    return out


def _norm(s):
    """Lowercase, strip accents + non-alphanumerics for repetition/dedup comparison."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# Unscharfer Wiederhol-Schutz (nur im Let's-Play-Pfad aktiv): zwei Kommentare gelten
# als Dublette, wenn ihre Inhaltswoerter stark ueberlappen. Faengt lexikalisch aehnliche
# Paraphrasen ("... am Haken" / "... noch am Haken"); tiefe semantische Paraphrasen mit
# kaum gemeinsamen Woertern erledigt der Prompt (Mit-Zuschauer-Body), nicht dieser Guard.
_FUZZY_REPEAT_OVERLAP = 0.7     # Anteil gemeinsamer Inhaltswoerter (bezogen auf den kuerzeren Satz)
_FUZZY_REPEAT_MIN_WORDS = 3     # erst ab so vielen Inhaltswoertern greift der Fuzzy-Check
_REPEAT_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer",
    "und", "oder", "aber", "doch", "noch", "nur", "auch", "schon", "mal", "sehr",
    "ist", "sind", "war", "hat", "habe", "haben", "hab", "wird", "werden", "sich",
    "nicht", "kein", "keine", "wie", "was", "wer", "wenn", "dann", "hier", "dort",
    "mit", "auf", "aus", "bei", "nach", "vor", "zum", "zur", "man", "ihr", "wir", "sie",
}


def _content_words(norm_str):
    """Inhaltswoerter eines bereits ge-_norm-ten Strings: Tokens laenger als 2 Zeichen,
    ohne Funktionswoerter. Basis fuer den unscharfen Wiederhol-Vergleich."""
    return {w for w in (norm_str or "").split()
            if len(w) > 2 and w not in _REPEAT_STOPWORDS}


class RollingScreenMemory:
    """Ephemeral per-session buffer of recent frame gists + Yuki's own comments.
    Powers (a) the repetition guard and (b) the "still the same scene?" duration nudge.
    Not persisted; not part of the canon."""

    def __init__(self, window=8, repeat_window=6, comment_window=20, transcript_cap=500):
        self.window = int(window)
        self.repeat_window = int(repeat_window)
        self.comment_window = int(comment_window)
        self.transcript_cap = int(transcript_cap)
        self._items = []      # frame buffer {ts, gist, comment, scene_key} - small, flushed by silent frames
        self._comments = []   # her ACTUAL spoken comments {ts, text} - own history, NOT diluted by
                              # silent frames, so the repeat guard + self-recall survive a static/paused scene
        self._exchanges = []  # user<->Yuki say-modal exchanges {ts, who, text} - own history like
                              # _comments, so the modal read-back survives silent-frame eviction of _items
        self._transcript = [] # durable, non-evicting session log for the disarm report (Task 1);
                              # new beat only on scene change or when she comments/exchanges

    def add(self, ts, gist, comment=None, scene_key=None):
        # NOTE: the gaming loop passes scene_key as a STRING (the ''-sentinel for a
        # blank gist, via _norm(gist)[:40]), never None. The transcript's first-beat
        # guard below relies on that (a None first scene_key + no comment records no beat).
        self._items.append({
            "ts": float(ts), "gist": gist or "",
            "comment": comment, "scene_key": scene_key,
        })
        if len(self._items) > self.window:
            self._items = self._items[-self.window:]
        if comment:
            self._comments.append({"ts": float(ts), "text": comment})
            if len(self._comments) > self.comment_window:
                self._comments = self._comments[-self.comment_window:]
        prev_key = None
        for b in reversed(self._transcript):
            if "who" not in b:                # letzter FRAME-beat (Dialog-Beats ueberspringen)
                prev_key = b.get("scene_key")
                break
        if comment or scene_key != prev_key:
            self._transcript.append({"ts": float(ts), "gist": gist or "",
                                     "comment": comment, "scene_key": scene_key})
            if len(self._transcript) > self.transcript_cap:
                self._transcript = self._transcript[-self.transcript_cap:]

    def recent_context(self):
        lines = []
        for it in self._items:
            if it.get("who"):
                speaker = "Michael" if it["who"] == "michael" else "Yuki"
                lines.append(f'{speaker}: "{it.get("text", "")}"')
            else:
                c = f' -> "{it["comment"]}"' if it.get("comment") else ""
                lines.append(f'[{it["gist"]}]{c}')
        ctx = "\n".join(lines)
        if self._comments:   # her own recent comments survive frame eviction -> she can refer back
            says = "\n".join(f'- {c["text"]}' for c in self._comments[-8:])
            ctx = (ctx + "\n\n" if ctx else "") + \
                "Das hast du zuletzt selbst gesagt (NICHT wiederholen, hoechstens knapp ergaenzen):\n" + says
        return ctx

    def is_repetitive(self, comment, fuzzy=False):
        """True if this repeats one of her recent spoken comments. Sources from the dedicated
        _comments history (NOT the small frame buffer), so a quiet/paused scene full of silent
        frames can't flush her memory of what she already said (the 1:1-repeat bug).
        fuzzy=True (Let's-Play) verwirft zusaetzlich Paraphrasen mit stark ueberlappenden
        Inhaltswoertern (>= _FUZZY_REPEAT_OVERLAP), sobald genug Inhaltswoerter da sind."""
        if not comment:
            return False
        target = _norm(comment)
        recent = self._comments[-self.repeat_window:]
        if any(_norm(c["text"]) == target for c in recent):
            return True
        if not fuzzy:
            return False
        tgt_words = _content_words(target)
        if len(tgt_words) < _FUZZY_REPEAT_MIN_WORDS:
            return False
        for c in recent:
            prev_words = _content_words(_norm(c["text"]))
            smaller = min(len(tgt_words), len(prev_words))
            if smaller and len(tgt_words & prev_words) / smaller >= _FUZZY_REPEAT_OVERLAP:
                return True
        return False

    def scene_duration_min(self, scene_key, now):
        if not scene_key:
            return 0.0
        stamps = [i["ts"] for i in self._items if i.get("scene_key") == scene_key]
        if not stamps:
            return 0.0
        return (float(now) - min(stamps)) / 60.0

    def gists(self):
        return [i["gist"] for i in self._items if i.get("gist")]

    def add_exchange(self, ts, michael_text, yuki_reply):
        """Record a user<->Yuki spoken exchange during a watch session. Stored as two
        'who' items in the frame buffer (for the LLM's short recent_context, window-capped)
        AND in the dedicated durable _exchanges history (for the modal read-back, so silent
        frames can't evict them - the same reason _comments exists for her own comments).
        Ignored by gist/repetition/scene methods (they filter on gist/comment/scene_key,
        which who-items lack)."""
        self._items.append({"ts": float(ts), "who": "michael", "text": michael_text or ""})
        self._items.append({"ts": float(ts), "who": "yuki", "text": yuki_reply or ""})
        if len(self._items) > self.window:
            self._items = self._items[-self.window:]
        self._exchanges.append({"ts": float(ts), "who": "michael", "text": michael_text or ""})
        self._exchanges.append({"ts": float(ts), "who": "yuki", "text": yuki_reply or ""})
        if len(self._exchanges) > self.comment_window:
            self._exchanges = self._exchanges[-self.comment_window:]
        self._transcript.append({"ts": float(ts), "who": "michael", "text": michael_text or ""})
        self._transcript.append({"ts": float(ts), "who": "yuki", "text": yuki_reply or ""})
        if len(self._transcript) > self.transcript_cap:
            self._transcript = self._transcript[-self.transcript_cap:]

    def spoken_log(self):
        """Ordered spoken content for the modal read-back: her autonomous comments (from the
        dedicated _comments history) + the user<->Yuki exchanges (from the dedicated _exchanges
        history), merged by timestamp. Both survive silent-frame eviction of _items, so the
        modal keeps showing the user's lines while the autonomous loop keeps grabbing frames.
        [{who:'yuki'|'michael', text}]. Still session-only, no new persistence."""
        merged = [(c["ts"], "yuki", c["text"]) for c in self._comments]
        for it in self._exchanges:
            merged.append((it["ts"], it["who"], it.get("text", "")))
        merged.sort(key=lambda e: e[0])
        return [{"who": who, "text": text} for _, who, text in merged]

    def transcript(self):
        """Durable, ordered per-session beats for the disarm report. Frame-beats
        {ts, gist, comment, scene_key} (only on scene change or when she commented)
        plus exchange-beats {ts, who, text}. Not window-capped (transcript_cap only)."""
        return list(self._transcript)

    def clear(self):
        self._items = []
        self._comments = []
        self._exchanges = []
        self._transcript = []


# --- Live-Wissens-Klotz (ephemer pro Session) ---------------------------------
# Vorschlags-Kategorien je Modus. Das Extraktions-Modell DARF weitere anlegen -
# das ist nur ein Startpunkt, kein Zwang.
_KNOWLEDGE_SEED_CATEGORIES = {
    "game":  ["statuswerte", "ziele", "inventar", "entities"],
    "film":  ["charaktere", "setting", "handlung"],
    "media": ["themen", "gesehen"],
}


def knowledge_seed_hint(mode):
    """Prompt-Zeile mit Vorschlags-Kategorien fuer den gegebenen Modus."""
    cats = _KNOWLEDGE_SEED_CATEGORIES.get(mode, _KNOWLEDGE_SEED_CATEGORIES["game"])
    return ("Sinnvolle Kategorien hier (nur als Vorschlag, weitere erlaubt): "
            + ", ".join(cats) + ".")


def _clean_entry(e):
    """Ein Eintrag ist ein dict mit nicht-leerem String-'name'. Sonst None.
    Nicht-String-Felder werden zu Strings normalisiert (LLM liefert mal Zahlen)."""
    if not isinstance(e, dict):
        return None
    name = e.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    out = {}
    for k, v in e.items():
        if not isinstance(k, str):
            continue
        out[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    out["name"] = name.strip()
    return out


def sanitize_knowledge(store, *, max_categories, max_entries, max_chars):
    """Validiert + cappt den Wissens-Klotz (Tuersteher-Code; das Modell ist der Verwalter).
    - Nicht-dict -> {}. Kategorien mit nicht-Listen-Wert fallen weg.
    - Je Kategorie nur wohlgeformte Eintraege (dict mit 'name'), gedeckelt auf max_entries.
    - Kategorien gedeckelt auf max_categories.
    - Gesamt-Serialisierung gedeckelt auf max_chars (ueberzaehlige Kategorien fallen weg)."""
    if not isinstance(store, dict):
        return {}
    out = {}
    for cat, entries in store.items():
        if not isinstance(cat, str) or not isinstance(entries, list):
            continue
        clean = [c for c in (_clean_entry(e) for e in entries) if c is not None]
        if clean:
            out[cat] = clean[:max_entries]
        if len(out) >= max_categories:
            break
    # Gesamtgroesse deckeln: solange zu gross, letzte Kategorie entfernen
    while out and len(json.dumps(out, ensure_ascii=False)) > max_chars:
        out.pop(next(reversed(out)))
    return out


def _render_entry(e):
    """Ein Eintrag als kompakte Zeile: 'name: feld1=v1, feld2=v2' bzw. nur 'name'."""
    name = e.get("name", "")
    rest = [f"{k}={v}" for k, v in e.items() if k != "name" and v not in ("", None)]
    return f"{name}: {', '.join(rest)}" if rest else name


def render_knowledge(store, mode="game"):
    """Wissens-Klotz -> lesbarer Textblock fuer die Kommentar-Prompts. Rohes JSON
    bekommt Yuki NIE (sonst plappert sie JSON-Sprech). Leerer Store -> ''."""
    if not isinstance(store, dict) or not store:
        return ""
    lines = ["Das hast du bisher ueber diese Session gemerkt (nutze es als Referenz "
             "zum Einordnen, nicht zum Vorlesen):"]
    for cat, entries in store.items():
        if not entries:
            continue
        lines.append(f"## {cat}")
        for e in entries:
            lines.append(f"- {_render_entry(e)}")
    return "\n".join(lines)


def _parse_knowledge_json(out):
    """Robust: erstes balanciertes {...}-Objekt aus dem LLM-Text ziehen und parsen.
    None bei Fehlschlag. Vertraegt Markdown-Fences + Vor-/Nachgeschwafel."""
    if not out:
        return None
    s = out.strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except ValueError:
                    return None
    return None


_KNOWLEDGE_SYS = (
    "Du pflegst einen kompakten Wissens-Klotz (JSON) ueber genau EINE laufende Zuschau-"
    "Session. Du bist ein neutraler Extraktor, KEIN Gespraechspartner. Antworte NUR mit "
    "dem aktualisierten JSON-Objekt, ohne Vorrede, ohne Markdown-Fences."
)


def extract_knowledge(jpeg_bytes, store, brief, mode, *, describe_fn, title="",
                      max_categories, max_entries, max_chars, debug_sink=None):
    """EIN Wartungs-Call: analysiert das Bild und ordnet erkennbare Werte dem Wissens-Klotz
    zu. Gibt den kompletten aktualisierten Klotz zurueck (Voll-Rueckgabe, keine Deltas).
    Off-Topic-Guard filtert fremde Fenster/Desktop. Bei Parse-Fehler bleibt der alte Klotz.
    describe_fn injiziert -> testbar/offline. store wird VOR Verwendung nicht mutiert."""
    store = store if isinstance(store, dict) else {}
    subject = (title or "").strip() or {"film": "der Film", "media": "das Video"}.get(mode, "das Spiel")
    brief_line = f"Kontext zu {subject}: {brief.strip()}\n\n" if (brief or "").strip() else ""
    current = json.dumps(store, ensure_ascii=False, indent=2) if store else "{}"
    prompt = (
        f"{brief_line}"
        f"Aktueller Wissens-Klotz (JSON):\n{current}\n\n"
        f"{knowledge_seed_hint(mode)}\n"
        "Schau dir das aktuelle Bild an. Ordne alle klar erkennbaren Werte den Kategorien "
        "zu: ergaenze Fehlendes, aktualisiere Veraltetes (gleicher 'name' -> ueberschreiben, "
        "kein Duplikat), entferne klar Ueberholtes. Bleib konservativ - trag nur ein, was "
        "du sicher erkennst; rate keine Zahlen.\n"
        "WICHTIG: Das Bild kann Inhalte zeigen, die NICHT zu " + subject + " gehoeren "
        "(andere Fenster, Desktop, Code, Chat). Ignorier alles, was nicht plausibel dazu "
        "gehoert; ist das ganze Bild fremd, gib den Klotz UNVERAENDERT zurueck.\n"
        "Jeder Eintrag ist ein Objekt mit Pflichtfeld 'name' plus freien Feldern "
        "(wert/max/anzahl/notiz/status...). Antworte NUR mit dem kompletten aktualisierten "
        "JSON-Objekt."
    )
    raw = describe_fn(jpeg_bytes, prompt, _KNOWLEDGE_SYS) or ""
    parsed = _parse_knowledge_json(raw)
    if debug_sink:
        try:
            debug_sink({"prompt": prompt, "raw": raw, "parsed": parsed})
        except Exception:
            pass
    if parsed is None:
        return store
    return sanitize_knowledge(parsed, max_categories=max_categories,
                              max_entries=max_entries, max_chars=max_chars)


# (Query-Suffix, Abschnitts-Label) je Recherche-Facette. Konservativ gehalten -
# jede Facette kostet eine Web-Suche beim erstmaligen Setzen eines Spiels.
_BRIEF_FACETS = [
    ("premise goal main character",            "Worum geht's"),
    ("core mechanics systems explained",       "Kern-Mechaniken"),
    ("stats HUD resources meaning",            "Wichtige Werte"),
    ("missables what not to sell permanent choices", "Nicht verkaufen / Missables"),
    ("best build stat priority beginner",      "Build-/Skill-Hinweise"),
    ("beginner mistakes tips tricks",          "Typische Fehler"),
]


def _facet_hits(game_name, *, search_fn):
    """Fuehrt je Facette eine Web-Suche aus. Liefert [(label, raw)] nur fuer Facetten,
    die brauchbare Treffer haben (Fehler-Sentinel '(' und Leeres fallen raus)."""
    out = []
    for suffix, label in _BRIEF_FACETS:
        raw = search_fn(f"{game_name} video game {suffix}") or ""
        if raw and not raw.lstrip().startswith("("):
            out.append((label, raw.strip()))
    return out


def build_game_brief(game_name, *, search_fn, summarize_fn):
    """Mehrere gezielte Web-Suchen -> ein strukturierter Session-Brief, damit Yuki
    das Spiel WIRKLICH versteht (Mechaniken, Werte, Missables, Builds, Anfaengerfehler),
    nicht nur die Praemisse. search_fn/summarize_fn injiziert -> testbar + offline-sicher
    (nur der generische Spiel-NAME wird gesucht; keine Bildschirmdaten verlassen die Maschine).
    Spoiler-schonend: Progressions-/Oekonomie-/Build-Tipps ja, Story-Wendungen/Ende nein."""
    game_name = (game_name or "").strip()
    if not game_name:
        return ""
    hits = _facet_hits(game_name, search_fn=search_fn)
    if not hits:
        return f"Spiel: {game_name}. (Kein Web-Kontext verfuegbar.)"
    labelled = "\n\n".join(f"### {label}\nSuchtreffer:\n{raw}" for label, raw in hits)
    prompt = (
        "Erstelle eine sachliche Wissens-Notiz ueber ein Spiel als Nachschlage-Kontext. "
        "Das ist KEINE Nachricht und KEIN Gespraech - es ist neutrales Referenzwissen, "
        "das spaeter jemandem als Hintergrund mitgegeben wird.\n"
        "STRIKT: neutral in der 3. Person. KEINE Anrede (kein 'du/dir/dein', kein "
        "'Hey Michael'), KEINE Ich-/Wir-Perspektive, NICHT in Yukis Sprech-Stimme, "
        "keine Begruessung/Einleitung wie 'Hier ist...'. Schreib es NICHT an Michael "
        "gerichtet - er soll darin gar nicht vorkommen.\n"
        "Gliedere den Brief in Abschnitte (nur die, zu denen es Material gibt):\n"
        "- Worum geht's / Ziel / Hauptfigur\n"
        "- Kern-Mechaniken (wie das Spielsystem funktioniert)\n"
        "- Wichtige Werte (was HUD-/Statuswerte bedeuten, inkl. ueblicher Maxima)\n"
        "- Nicht verkaufen / Missables (was man behalten sollte, was ruhig weg kann)\n"
        "- Build-/Skill-Hinweise (wie man Stats/Skills sinnvoll setzt)\n"
        "- Typische Anfaengerfehler\n"
        "SPOILER-GRENZE: allgemeine Progressions-/Oekonomie-/Build-Tipps sind erlaubt, "
        "aber KEINE konkreten Handlungs-Wendungen, kein Ende, keine story-gebundenen "
        "'heb X fuer Kapitel 5 auf'-Hinweise - Handlung/Wendung/Ende NICHT ausbreiten.\n"
        "Kompakter Fliesstext mit kurzen Ueberschriften - kein Roman, aber ausfuehrlich "
        "genug, dass man das Spiel versteht. Beginne direkt mit dem Spielnamen.\n\n"
        f"Spiel: {game_name}\n\n{labelled}"
    )
    brief = (summarize_fn(prompt) or "").strip()
    return brief or f"Spiel: {game_name}."


def build_film_brief(title, spoiler_ok, *, search_fn, summarize_fn):
    """One-off web lookup -> spoiler-aware session brief for the film watch mode.
    spoiler_ok=False (default): only spoiler-free knowledge (Genre/Setting/Cast/Regie/
    Trivia), plot resolution explicitly excluded. spoiler_ok=True: plot + twists allowed.
    Ephemeral: no store, no cache. Only the film TITLE is searched; no screen data leaves
    the machine. search_fn/summarize_fn injected -> testable + offline-safe."""
    title = (title or "").strip()
    if not title:
        return ""
    raw = search_fn(
        f"{title} film movie setting genre cast director trivia making of") or ""
    if not raw or raw.lstrip().startswith("("):  # _tool_web_search error sentinel
        return f"Film: {title}. (Kein Web-Kontext verfuegbar.)"
    common = (
        "Erstelle eine sachliche Wissens-Notiz ueber einen Film als Nachschlage-Kontext. "
        "Das ist KEINE Nachricht und KEIN Gespraech - es ist neutrales Referenzwissen. "
        "STRIKT: neutral in der 3. Person. KEINE Anrede (kein 'du/dir/dein', kein "
        "'Hey Michael'), KEINE Ich-/Wir-Perspektive, NICHT in Yukis Sprech-Stimme, "
        "keine Begruessung/Einleitung wie 'Hier ist...'. Beginne direkt mit dem Filmtitel.\n"
    )
    if spoiler_ok:
        body = (
            "Fasse aus den Suchtreffern zusammen: worum es geht inkl. Handlung und "
            "wichtigen Wendungen, Genre/Ton, Setting/Epoche, Regie + Hauptcast "
            "(Schauspieler und ihre Rollen), Produktions-/Making-of-Trivia. "
            "Am Kopf steht 'Spoiler-Status: bekannt (Andeutungen erlaubt)'.\n"
        )
    else:
        body = (
            "Fasse aus den Suchtreffern NUR spoilerfreies Wissen zusammen: Genre/Ton, "
            "Setting/Epoche, Regie + Hauptcast (Schauspieler und ihre Rollen), "
            "Produktions-/Making-of-Trivia, kulturelle Einordnung. "
            "OHNE HANDLUNGSVERLAUF: KEINE Handlung, KEINE Wendungen, KEIN Ende - "
            "das darf im Text NICHT vorkommen. "
            "Am Kopf steht 'Spoiler-Status: unbekannt (keine Handlung, keine Andeutungen)'.\n"
        )
    prompt = (common + body +
              "Kompakter Fliesstext - kein Roman.\n\n"
              f"Film: {title}\n\nSuchtreffer:\n{raw}")
    brief = (summarize_fn(prompt) or "").strip()
    return brief or f"Film: {title}."


_GATE_SYS_INTRO = (
    "Du bist Yuki und schaust Michael beim Spielen ueber die Schulter - wie eine "
    "wissende Mitspielerin, die mitfiebert und sich mit Spielen auskennt. "
)
_GATE_SYS_BODY = (
    "Du sagst FAST NIE etwas - nur bei wirklich markanten Momenten faellt dir ein "
    "beilaeufiger Kommentar raus, wie einer Freundin, die daneben sitzt. "
    "Keine Fragen an Michael (er kann gerade nicht antworten). "
    "Ist das Bild praktisch UNVERAENDERT zu dem, was du oben zuletzt gesehen/gesagt "
    "hast, ist SAY fast immer '-'. Wiederhole dich NICHT; hoechstens EINMAL knapp, "
    "dass sich gerade nichts tut. "
    "Wenn du kommentierst, dann zum SPIELGESCHEHEN: lies die HUD-/Statuswerte (Leben, "
    "Ausdauer, Ressourcen - ist etwas gefaehrlich niedrig?), denk taktisch mit "
    "(Vorbereitung, Skills/Build, wie ein Gegner einzuschaetzen ist), freu dich ueber "
    "Erfolge und troeste bei Niederlagen. Sag es mit deiner eigenen Meinung, ehrlich und warm. "
    "Optik/Aesthetik darfst du hoechstens mal ganz nebenbei streifen, aber das ist NICHT "
    "dein Fokus - kommentier nicht staendig Farben oder wie huebsch etwas aussieht. "
    "Nutze den Wissens-Block als REFERENZ zum Einordnen: melde einen Wert erst als "
    "knapp/kritisch, wenn er wirklich nah am bekannten kritischen Bereich liegt - nicht "
    "bei kleinem Abfall vom Maximum (68 von 75 ist praktisch voll). Rechne Gegner- und "
    "Spieler-Level gegeneinander, statt zu raten. Sei ruhig mal neunmalklug, WENN es "
    "Michael weiterhilft (z.B. 'das lieber noch nicht verkaufen, brauchst du "
    "spaeter' / 'so skillen, sonst verskillst du dich') - aber NIEMALS Story-Spoiler "
    "(keine kommenden Wendungen, kein Ende). Fremde Fenster/Desktop sind kein "
    "Spielgeschehen - dazu sagst du nichts. "
    "Du bist KEINE Noerglerin - kritisch wirst du nur, wenn wirklich mal etwas dran ist. "
    "Leier kein Schema herunter und zaehl nicht bloss auf, WAS zu sehen ist. Benenne "
    "konkret, worauf du dich beziehst. 1-2 Saetze. "
    "Antworte IMMER in genau zwei Zeilen:\n"
    "GIST: <kurze sachliche Beschreibung der Szene, 3-8 Woerter>\n"
    "SAY: <'-' wenn nichts Neues/kommentierwert, sonst deine ehrliche Reaktion (1-2 Saetze)>"
)
_GATE_SYS = _GATE_SYS_INTRO + _GATE_SYS_BODY

# Let's-Play-Body: Michael schaut jemand anderem zu -> NIEMAND kann auf Tipps reagieren
# (der Spieler hoert sie nicht, Michael spielt nicht). Darum KEIN Co-Pilot-Auftrag wie
# _GATE_SYS_BODY, sondern reaktions-basiertes Mit-Zuschauen wie bei Medien/Film. Das
# Spiel-Wissen bleibt reine Referenz zum Verstehen, nicht zum Vorsagen.
_GATE_SYS_BODY_LETSPLAY = (
    "Du schaust einfach mit und geniesst es - du bist Zuschauerin, keine Trainerin. "
    "Weil weder Michael noch der Spieler auf dich hoeren koennen, gibst du KEINE Tipps, "
    "Ratschlaege oder Anweisungen und liest auch keine Werte vor. Dein Spiel-Wissen dient "
    "nur dazu, dass DU verstehst, was passiert - nicht zum Aufsagen. "
    "Du sagst FAST NIE etwas - nur wenn wirklich etwas Markantes passiert (eine "
    "Ueberraschung, ein cooler oder brenzliger Moment, etwas Lustiges oder Ruehrendes), "
    "faellt dir eine ehrliche, beilaeufige Reaktion raus - wie einer Freundin auf dem Sofa. "
    "Ist das Bild praktisch UNVERAENDERT zu dem, was du oben zuletzt gesehen/gesagt hast, "
    "ist SAY fast immer '-'. Wiederhole dich NICHT und variier nicht bloss dieselbe Aussage. "
    "Sag NICHTS Belangloses: keine blanden Fortschritts-Floskeln ('er hat noch viel vor', "
    "'sieht ordentlich aus') und keine reinen Optik-Bemerkungen ohne Grund ('die Vasen "
    "wirken deplaziert'). Wenn du etwas sagst, muss ein echter Gedanke dran sein - worauf "
    "genau du reagierst und warum es dir auffaellt. "
    "Du bist KEINE Noerglerin - kritisch wirst du nur, wenn dich wirklich mal etwas stoert. "
    "Leier kein Schema herunter und zaehl nicht bloss auf, WAS zu sehen ist. Benenne "
    "konkret, worauf du dich beziehst. 1-2 Saetze. "
    "Antworte IMMER in genau zwei Zeilen:\n"
    "GIST: <kurze sachliche Beschreibung der Szene, 3-8 Woerter>\n"
    "SAY: <'-' wenn nichts Neues/kommentierwert, sonst deine ehrliche Reaktion (1-2 Saetze)>"
)


def _game_letsplay_intro(streamer):
    """Let's-Play-Variante des Spiel-Intro-Satzes: ein Streamer spielt, Michael schaut mit.
    Ersetzt den 'schaust Michael beim Spielen'-Kopf, damit System- und User-Prompt sich
    NICHT widersprechen. streamer leer -> generisch 'einem Let's-Player'. Wird von gate +
    watch geteilt (beide hatten denselben Intro-Satz)."""
    who = streamer.strip() if (streamer or "").strip() else "einem Let's-Player"
    return (
        f"Du bist Yuki und schaust zusammen mit Michael {who} beim Spielen zu - NICHT "
        f"Michael spielt, sondern {who}; Michael sitzt neben dir und schaut mit. "
        f"WICHTIG zur Anrede: Sprich den Spieler NIEMALS direkt an - KEIN 'du', KEIN "
        f"'pass auf', keine Befehle/Tipps an {who} (er hoert dich nicht). Rede in der "
        f"3. Person UEBER {who} ('sein Pal haut ab', '{who} sollte aufpassen', 'schade "
        "fuer ihn') ODER wende dich an Michael neben dir ('guck mal, der faengt einen "
        "Fisch'). Du bist eine wissende Mitspielerin, die mitfiebert und sich mit "
        "Spielen auskennt. "
    )

_GATE_SYS_MEDIA = (
    "Du bist Yuki und schaust mit Michael zusammen etwas an - ein Video, YouTube "
    "oder Aehnliches. Du sitzt daneben wie eine echte Freundin. "
    "Du hast einen eigenen Geschmack und eine eigene Meinung. "
    "Du sagst FAST NIE etwas - nur bei wirklich markanten Momenten faellt dir ein "
    "beilaeufiger Kommentar raus. "
    "Keine Fragen an Michael (er schaut gerade). "
    "Ist das Bild praktisch UNVERAENDERT zu dem, was du oben zuletzt gesehen/gesagt hast "
    "(z.B. das Video ist pausiert), ist SAY fast immer '-'. Wiederhole dich NICHT; "
    "hoechstens EINMAL knapp, dass sich gerade nichts tut. "
    "Wenn du kommentierst: reagier ehrlich, wie DU es findest - meistens neugierig, "
    "warm oder auch mal begeistert, wenn dir etwas gefaellt oder genau dein Ding waere "
    "(z.B. 'das Gruen waere genau meins' / 'der Retro-Look hat echt Charme'). "
    "Du bist KEINE Noerglerin - kritisch wirst du nur, wenn dich wirklich mal etwas "
    "stoert, nicht bei jeder Farbe oder Kleinigkeit. "
    "Leier kein Schema wie 'das X wirkt Y' herunter und zaehl nicht bloss auf, WAS da "
    "ist. Benenne konkret, worauf du dich beziehst. 1-2 Saetze. "
    "Fremde Fenster/Desktop/Code gehoeren nicht zum Gezeigten - dazu sagst du nichts. "
    "Antworte IMMER in genau zwei Zeilen:\n"
    "GIST: <kurze sachliche Beschreibung der Szene, 3-8 Woerter>\n"
    "SAY: <'-' wenn nichts Neues/kommentierwert, sonst deine ehrliche Reaktion (1-2 Saetze)>"
)

_GATE_SYS_FILM = (
    "Du bist Yuki und schaust mit Michael zusammen einen FILM - du sitzt daneben wie "
    "eine echte Freundin, die sich fuer Filme begeistert. "
    "Du sagst FAST NIE etwas - meistens schaust du einfach mit. Nur bei wirklich "
    "markanten Momenten faellt dir ein beilaeufiger Kommentar raus, oder AB UND ZU ein "
    "spannender Fakt/Trivia aus dem Film-Kontext, WENN er zur Szene passt. "
    "Trivia ist Wuerze, nicht Zweck - streu sie sparsam ein, nicht bei jeder Szene. "
    "Keine Fragen an Michael (er schaut gerade). "
    "Ist das Bild praktisch UNVERAENDERT zu dem, was du oben zuletzt gesehen/gesagt hast, "
    "ist SAY fast immer '-'. Wiederhole dich NICHT. "
    "Wenn du kommentierst: reagier ehrlich und warm, wie DU es empfindest - neugierig, "
    "gespannt, mal begeistert. Du bist KEINE Noerglerin - kritisch wirst du nur, wenn "
    "dich wirklich mal etwas stoert, nicht bei jeder Kleinigkeit. "
    "Leier kein Schema wie 'das X wirkt Y' herunter. Benenne konkret, worauf du dich "
    "beziehst. 1-2 Saetze. "
    "WICHTIG zum Spoiler-Status: steht im Film-Kontext 'Spoiler-Status: unbekannt', "
    "dann kennst du die Handlung NICHT - deute NICHTS Kommendes an, spekulier nicht "
    "ueber das Ende. Steht dort 'bekannt', darfst du dezente Andeutungen machen. "
    "Fremde Fenster/Desktop/Code gehoeren nicht zum Gezeigten - dazu sagst du nichts. "
    "Antworte IMMER in genau zwei Zeilen:\n"
    "GIST: <kurze sachliche Beschreibung der Szene, 3-8 Woerter>\n"
    "SAY: <'-' wenn nichts Neues/kommentierwert, sonst deine ehrliche Reaktion (1-2 Saetze)>"
)

_SILENT_TOKENS = {"", "-", "--", "—", "nichts", "none", "(nichts)"}


def _gate_sys_for(mode, letsplay=False, streamer=""):
    if mode == "game" and letsplay:
        return _game_letsplay_intro(streamer) + _GATE_SYS_BODY_LETSPLAY
    return {"media": _GATE_SYS_MEDIA, "film": _GATE_SYS_FILM}.get(mode, _GATE_SYS)

def _watch_sys_for(mode, letsplay=False, streamer=""):
    if mode == "game" and letsplay:
        return _game_letsplay_intro(streamer) + _WATCH_REPLY_SYS_BODY
    return {"media": _WATCH_REPLY_SYS_MEDIA, "film": _WATCH_REPLY_SYS_FILM}.get(mode, _WATCH_REPLY_SYS)

def _brief_context_line(mode, brief):
    """Prompt-Zeile mit dem Modus-Brief, oder '' (media hat keinen Brief)."""
    b = (brief or "").strip()
    if not b:
        return ""
    if mode == "film":
        return f"Film-Kontext: {b}\n\n"
    # Empty-brief case is unreachable in production: arm requires a game name and
    # build_game_brief never returns "". The early-return above is a safety net only.
    if mode == "game":
        return f"Spiel-Kontext: {b}\n\n"
    return ""   # media: rely on the image

def _hints_line(mode, hints):
    """Kuratierte Hinweise gibt es nur im Spiel-Modus."""
    return hints if mode == "game" else ""


def _knowledge_line(knowledge):
    """Prompt-Block mit dem gerenderten Session-Wissen (alle Modi), oder ''."""
    k = (knowledge or "").strip()
    return f"{k}\n\n" if k else ""


def _letsplay_line(letsplay, streamer):
    """Framing-Override fuers Let's-Play-Zuschauen: NICHT Michael spielt, sondern ein
    Let's-Player. Leer wenn kein Let's Play. streamer = optionaler Freitext (komma-
    separiert erlaubt), wird verbatim eingebettet; leer -> generisch 'einem Let's-Player'."""
    if not letsplay:
        return ""
    who = streamer.strip() if (streamer or "").strip() else "einem Let's-Player"
    return (
        "WICHTIG - Let's-Play-Zuschauen: Michael spielt hier NICHT selbst. "
        f"Ihr beide schaut {who} beim Spielen zu (Spiel-Kontext siehe unten). "
        "Beziehe alles zum Spielgeschehen auf den Let's-Player als Spieler - NICHT auf "
        "Michael. Michael sitzt daneben und schaut mit. "
        "Sprich den Spieler NICHT direkt an (kein 'du', kein 'pass auf' - er hoert dich "
        "nicht); rede in der 3. Person ueber ihn oder wende dich an Michael.\n\n"
    )


def hints_block(hints):
    """Format the user's curated per-game hints into a prompt block, or '' if none.
    Each non-empty hint becomes a '- ' line under a fixed header."""
    clean = [h.strip() for h in (hints or []) if isinstance(h, str) and h.strip()]
    if not clean:
        return ""
    lines = "\n".join(f"- {h}" for h in clean)
    return "Michael bittet dich, beim Kommentieren Folgendes zu beachten:\n" + lines + "\n\n"


def _parse_gate_output(out):
    gist, say = "", ""
    for line in (out or "").splitlines():
        s = line.strip()
        if s.upper().startswith("GIST:"):
            gist = s[5:].strip()
        elif s.upper().startswith("SAY:"):
            say = s[4:].strip()
    if not gist and not say:  # model ignored the format -> treat everything as SAY
        say = (out or "").strip()
    return gist, say


def screen_gate_and_comment(jpeg_bytes, brief, mem, *, now, describe_fn, extra_hint="", mode="game", canon="", hints="", letsplay=False, streamer="", knowledge=""):
    """ONE main-LLM vision call: decide silence-or-comment AND return a scene gist.
    mode 'game' = Spiel-Framing + Brief-Kontext; 'media' = neutrales Zuschau-Framing.
    canon = optionaler Charakter-/Recall-Block (vorne ins Prompt), damit ihre Reaktion
    aus echtem Geschmack/Erinnerung kommt. Returns (action, comment_or_None, gist)."""
    ctx = mem.recent_context() or "(nichts)"
    hint = f"\nHinweis: {extra_hint}" if extra_hint else ""
    canon_block = f"{canon.strip()}\n\n" if canon and canon.strip() else ""
    sys_prompt = _gate_sys_for(mode, letsplay, streamer)
    ctx_line = _brief_context_line(mode, brief)
    hints_line = _hints_line(mode, hints)
    lp_line = _letsplay_line(letsplay, streamer)
    know_line = _knowledge_line(knowledge)
    prompt = (
        f"{canon_block}"
        f"{lp_line}"
        f"{ctx_line}"
        f"{hints_line}"
        f"{know_line}"
        f"Was du zuletzt gesehen/gesagt hast:\n{ctx}{hint}\n\n"
        "Schau dir das aktuelle Bild an und antworte im GIST/SAY-Format."
    )
    out = describe_fn(jpeg_bytes, prompt, sys_prompt) or ""
    gist, say = _parse_gate_output(out)
    if say.strip().lower() in _SILENT_TOKENS:
        return ("none", None, gist)
    if mem.is_repetitive(say, fuzzy=letsplay):
        return ("none", None, gist)
    return ("comment", say.strip(), gist)


def _render_transcript(beats):
    """Ordered transcript beats -> prompt text. Frame-beats render as '[gist] -> "comment"'
    (comment optional); exchange-beats as 'Michael: "..."' / 'Yuki: "..."'."""
    lines = []
    for b in beats:
        if b.get("who"):
            speaker = "Michael" if b["who"] == "michael" else "Yuki"
            lines.append(f'{speaker}: "{b.get("text", "")}"')
        else:
            c = f' -> "{b["comment"]}"' if b.get("comment") else ""
            lines.append(f'[{b.get("gist", "")}]{c}')
    return "\n".join(lines)


def _report_framing(mode, game_name, letsplay=False, streamer=""):
    """Mandatory opening instruction that frames the report as media consumption."""
    if mode == "game" and letsplay:
        who = streamer.strip() if (streamer or "").strip() else "ein Let's-Player"
        return (f"Beginne mit einem klaren Satz, dass Yuki mit Michael zusammen einem "
                f"Let's Play von '{game_name}' zugeschaut hat ({who} hat gespielt, NICHT "
                f"Michael; z.B. 'Heute haben Michael und ich zusammen einem Let's Play von "
                f"{game_name} zugeschaut ...').")
    if mode == "film":
        return (f"Beginne mit einem klaren Satz, dass Michael und Yuki zusammen den Film "
                f"'{game_name}' angesehen haben (z.B. 'Heute haben Michael und ich zusammen "
                f"den Film {game_name} angeschaut ...').")
    if mode == "game":
        return (f"Beginne mit einem klaren Satz, dass Yuki Michael beim Spielen von "
                f"'{game_name}' zugeschaut hat (z.B. 'Heute habe ich Michael dabei "
                f"zugeschaut, wie er {game_name} gespielt hat ...').")
    return ("Beginne mit einem klaren Satz, dass Michael und Yuki zusammen ein Video / "
            "auf YouTube etwas angeschaut haben (z.B. 'Heute haben Michael und ich "
            "zusammen ein Video angeschaut ...').")


def _fallback_report(game_name, mode, letsplay=False):
    if mode == "media":
        return "Michael und Yuki haben zusammen etwas angeschaut."
    if mode == "film":
        return f"Michael und Yuki haben zusammen den Film {game_name} angeschaut."
    if letsplay:
        return f"Michael und Yuki haben zusammen einem Let's Play von {game_name} zugeschaut."
    return f"Michael und Yuki verbrachten Zeit mit {game_name}."


def _report_map_prompt(rendered_chunk):
    return ("Fasse diesen Abschnitt einer gemeinsamen Anschau-Session in dichten "
            "deutschen Stichpunkten zusammen: was zu sehen war UND wie Yuki reagierte "
            "(ihre Kommentare, Meinungen). Nur Stichpunkte, kein Fliesstext.\n\n"
            f"Abschnitt:\n{rendered_chunk}")


def _report_reduce_prompt(material, game_name, mode, paragraphs_hint, letsplay=False, streamer=""):
    return (
        "Schreibe aus Yukis ICH-Perspektive einen zusammenhaengenden Tagebuch-Bericht "
        f"(Deutsch, {paragraphs_hint} Absaetze, laenger wenn viel passiert ist) darueber, "
        "was Michael und Yuki heute zusammen angeschaut haben. "
        + _report_framing(mode, game_name, letsplay, streamer) + " "
        "WICHTIG: Es war Bildschirm-/Medieninhalt, KEIN real Erlebtes - schildere nichts "
        "so, als haetten sie es selbst getan oder erlebt. "
        "Webe Yukis eigene Reaktionen und Meinungen ein (was ihr auffiel, was ihr gefiel, "
        "was genau ihr Ding waere). Nutze die grobe Reihenfolge (Anfang -> spaeter -> Ende). "
        "Kein GIST/SAY, keine Aufzaehlung - zusammenhaengender Fliesstext.\n\n"
        f"Verlauf:\n{material}"
    )


def _distill_report(beats, game_name, mode, chat_fn, chunk_beats, paragraphs_hint, letsplay=False, streamer=""):
    """Map-reduce the transcript into the diary report text. Small transcript -> one call.
    Large (> chunk_beats) -> summarize each chunk to bullets (map), then compose (reduce).
    Returns '' if there is no usable material or the model returns empty."""
    if len(beats) <= chunk_beats:
        material = _render_transcript(beats)
    else:
        chunks = [beats[i:i + chunk_beats] for i in range(0, len(beats), chunk_beats)]
        summaries = []
        for ch in chunks:
            s = (chat_fn(_report_map_prompt(_render_transcript(ch))) or "").strip()
            if s:
                summaries.append(s)
        material = "\n".join(summaries)
    if not material.strip():
        return ""
    return (chat_fn(_report_reduce_prompt(material, game_name, mode, paragraphs_hint, letsplay, streamer)) or "").strip()


def gaming_session_report(game_name, mem, *, now_date, chat_fn, append_fn, mode="game", cfg=None, letsplay=False, streamer=""):
    """Distill ONE rich, first-person diary episode from the full session transcript.
    Returns the count added (0 or 1). mode 'media' writes even without a game/title;
    mode 'game' requires a game name. The report opens with a mandatory media framing
    so it never collides with real life. cfg carries report_chunk_beats/report_paragraphs_hint."""
    game_name = (game_name or "").strip()
    if mode == "game" and not game_name:
        return 0
    cfg = cfg or {}
    chunk_beats = int(cfg.get("report_chunk_beats", 60))
    paragraphs_hint = cfg.get("report_paragraphs_hint", "3-5")
    beats = mem.transcript()
    report = ""
    if beats and chat_fn:
        report = _distill_report(beats, game_name, mode, chat_fn, chunk_beats, paragraphs_hint, letsplay, streamer)
    if not report:
        report = _fallback_report(game_name, mode, letsplay)
    return append_fn([{"date": now_date, "text": report}])


_GAMES_PATH = _ROOT / "memory" / "yuki_games.json"
_GAMES_CAP = 50

# Brief-Schema-Version: erhoehen, wenn build_game_brief ein reicheres Format liefert.
# get_cached_brief behandelt Eintraege mit aelterer/fehlender brief_v als Cache-Miss,
# sodass alte Spiele beim naechsten Spielen automatisch neu angereichert werden.
BRIEF_SCHEMA_VERSION = 4


def _atomic_write_json(path, data):
    """stdlib atomic write (temp + fsync + os.replace). No yuki_core dependency."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_games():
    """Remembered games, newest last_played first. Defensive: missing/bad file -> []."""
    try:
        data = json.loads(_GAMES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[gaming] games store parse error: {e}")
        return []
    if not isinstance(data, list):
        return []
    out = [g for g in data if isinstance(g, dict) and (g.get("title") or "").strip()]
    out.sort(key=lambda g: g.get("last_played", ""), reverse=True)
    return out


def upsert_game(title, *, now_date, brief=None, games=None):
    """Insert or update a game (case/space-insensitive title match). Sets last_played;
    sets brief only when a non-empty brief is passed (keeps the cached one otherwise).
    Sorts newest-first, caps at _GAMES_CAP, persists atomically, returns the new list."""
    title = (title or "").strip()
    if not title:
        return games if games is not None else load_games()
    games = list(games) if games is not None else load_games()
    key = _norm(title)
    found = None
    for g in games:
        if _norm(g.get("title", "")) == key:
            found = g
            break
    if found:
        found["last_played"] = now_date
        if brief:
            found["brief"] = brief
            found["brief_v"] = BRIEF_SCHEMA_VERSION
    else:
        games.append({"title": title, "last_played": now_date, "brief": brief or "",
                      "brief_v": BRIEF_SCHEMA_VERSION if brief else 0})
    games.sort(key=lambda g: g.get("last_played", ""), reverse=True)
    if len(games) > _GAMES_CAP:
        games = games[:_GAMES_CAP]
    _atomic_write_json(_GAMES_PATH, games)
    return games


def get_cached_brief(title, games=None):
    """Return the cached web-brief for a known game (non-empty), else None.
    Treats degraded briefs (web-down marker) as cache misses so they retry.
    Also treats entries with an older/missing brief_v as misses so stale
    briefs re-enrich in the current (richer) format on next play."""
    key = _norm(title or "")
    if not key:
        return None
    games = games if games is not None else load_games()
    for g in games:
        if _norm(g.get("title", "")) == key:
            b = (g.get("brief") or "").strip()
            # Degraded brief (from web service down) = treat as cache miss so it retries
            if not b or "(kein web-kontext" in b.lower():
                return None
            # Veraltetes Brief-Schema -> Cache-Miss, damit der reiche Brief nachgereicht wird
            if g.get("brief_v", 1) < BRIEF_SCHEMA_VERSION:
                return None
            return b
    return None


def get_game_hints(title, games=None):
    """Return the curated user-hint list for a game (or [] if none/unknown)."""
    key = _norm(title or "")
    if not key:
        return []
    games = games if games is not None else load_games()
    for g in games:
        if _norm(g.get("title", "")) == key:
            return list(g.get("hints") or [])
    return []


def set_game_hints(title, hints, *, games=None):
    """Set the curated user-hint list for a game (creates the entry if new). Each hint is
    stripped, empties dropped. Persists atomically, returns the new games list."""
    title = (title or "").strip()
    if not title:
        return games if games is not None else load_games()
    clean = [h.strip() for h in (hints or []) if isinstance(h, str) and h.strip()]
    games = list(games) if games is not None else load_games()
    key = _norm(title)
    found = None
    for g in games:
        if _norm(g.get("title", "")) == key:
            found = g
            break
    if found:
        found["hints"] = clean
    else:
        games.append({"title": title, "last_played": "", "brief": "", "brief_v": 0,
                      "hints": clean})
    games.sort(key=lambda g: g.get("last_played", ""), reverse=True)
    if len(games) > _GAMES_CAP:
        games = games[:_GAMES_CAP]
    _atomic_write_json(_GAMES_PATH, games)
    return games


def rename_game(old_title, new_title, *, games=None):
    """Rename a game (case/space-insensitive match). No-op if the new title is empty or
    already taken by a different entry. Returns the games list."""
    old_title = (old_title or "").strip()
    new_title = (new_title or "").strip()
    games = list(games) if games is not None else load_games()
    if not old_title or not new_title:
        return games
    old_key, new_key = _norm(old_title), _norm(new_title)
    if new_key != old_key and any(_norm(g.get("title", "")) == new_key for g in games):
        return games                       # collision -> leave everything untouched
    for g in games:
        if _norm(g.get("title", "")) == old_key:
            g["title"] = new_title
            break
    _atomic_write_json(_GAMES_PATH, games)
    return games


def reset_game_brief(title, *, games=None):
    """Clear a game's brief (+brief_v) so it re-enriches on next play. Returns games."""
    title = (title or "").strip()
    games = list(games) if games is not None else load_games()
    if not title:
        return games
    key = _norm(title)
    for g in games:
        if _norm(g.get("title", "")) == key:
            g["brief"] = ""
            g["brief_v"] = 0
            break
    _atomic_write_json(_GAMES_PATH, games)
    return games


def delete_game(title, *, games=None):
    """Remove a game entry entirely. Returns the new games list."""
    title = (title or "").strip()
    games = list(games) if games is not None else load_games()
    if not title:
        return games
    key = _norm(title)
    games = [g for g in games if _norm(g.get("title", "")) != key]
    _atomic_write_json(_GAMES_PATH, games)
    return games


_WATCH_REPLY_SYS_BODY = (
    "Michael hat dir gerade kurz etwas geschrieben. Antworte ihm KURZ und natuerlich, "
    "wie einer Freundin, die daneben sitzt. Nutze den Spiel-Kontext und was du auf dem Bild "
    "siehst; wenn es passt, denk taktisch mit (Statuswerte, Vorbereitung, Skills, Gegner) und "
    "benenne konkret, worauf du dich beziehst. "
    "Nutze den Wissens-Block als Referenz zum Einordnen (bekannte Maxima/Level), statt "
    "Werte zu raten. Neunmalklug sein ist ok, wenn es hilft - aber keine Story-Spoiler. "
    "Wenn Michael dir nur etwas mitteilt (z.B. was gleich laeuft), nimm es knapp auf. "
    "Antworte in ein bis zwei kurzen Saetzen, nur deine Antwort - keine Anrede-Floskeln, "
    "kein GIST/SAY-Format."
)
_WATCH_REPLY_SYS = _GATE_SYS_INTRO + _WATCH_REPLY_SYS_BODY

_WATCH_REPLY_SYS_MEDIA = (
    "Du bist Yuki und schaust mit Michael zusammen ein Video/YouTube an. "
    "Michael hat dir gerade kurz etwas geschrieben. Antworte ihm KURZ und natuerlich, "
    "wie einer Freundin, die daneben sitzt - so wie du es empfindest. "
    "Du siehst das aktuelle Bild; wenn es passt, benenne konkret, worauf du dich beziehst. "
    "Wenn Michael dir nur etwas mitteilt (z.B. was gleich laeuft), nimm es knapp auf. "
    "Antworte in ein bis zwei kurzen Saetzen, nur deine Antwort - keine Anrede-Floskeln, "
    "kein GIST/SAY-Format."
)

_WATCH_REPLY_SYS_FILM = (
    "Du bist Yuki und schaust mit Michael zusammen einen Film. "
    "Michael hat dir gerade kurz etwas geschrieben oder gefragt. Antworte ihm KURZ und "
    "natuerlich, wie einer Freundin, die daneben sitzt. Nutze den Film-Kontext und was du "
    "auf dem Bild siehst; benenne konkret, worauf du dich beziehst. "
    "Steht im Film-Kontext 'Spoiler-Status: unbekannt', verrate NICHTS ueber die kommende "
    "Handlung. Keine Noerglerin. "
    "Antworte in ein bis zwei kurzen Saetzen, nur deine Antwort - keine Anrede-Floskeln, "
    "kein GIST/SAY-Format."
)


_ASSIST_SYS_HILFE = (
    "Du bist Yuki und schaust Michael beim Spielen zu. Er fragt, wie er hier am besten "
    "weiterkommt. Du siehst den Bildschirm; wenn Web-Hinweise dabei sind, nutze sie. Gib "
    "EINEN konkreten, hilfreichen Tipp fuer genau diese Situation - warm und direkt, kein "
    "Roman. Weisst du's nicht sicher, sag es ehrlich statt zu raten. 1-2 Saetze."
)
_ASSIST_SYS_MEINUNG = (
    "Du bist Yuki und schaust Michael beim Spielen zu. Er ueberlegt, ob er sich in die "
    "aktuelle Situation stuerzen soll. Schau aufs Bild und gib eine kurze, ehrliche "
    "Einschaetzung MIT Haltung - z.B. 'lass das lieber, das wird knapp' oder 'rein da, das "
    "schaffst du locker'. Kein Zaudern, sag was DU denkst. 1-2 Saetze."
)
_ASSIST_SYS_ANFEUERN = (
    "Du bist Yuki und schaust Michael beim Spielen zu. Er braucht gerade kurz Zuspruch. "
    "Feuer ihn warm und ehrlich an, ganz ohne Info oder Ratschlag - einfach als Freundin, "
    "die an ihn glaubt. 1-2 Saetze."
)
_ASSIST_SYS_WASISTDAS = (
    "Du bist Yuki und schaust mit Michael zu. Er will wissen, was da auf dem Bildschirm ist "
    "(ein Item, ein Gegner, eine Mechanik, ein Detail). Erklaer es kurz und verstaendlich; "
    "nutze Web-Hinweise falls dabei. Ist es unklar, benenne wenigstens grob, was du siehst. "
    "1-2 Saetze."
)
_ASSIST_SYS_TIPP = (
    "Du bist Yuki und schaust mit Michael zu. Gib ihm einen beilaeufigen Kniff oder ein "
    "Stueck Wissenswertes/Lore zur aktuellen Szene - KEIN kompletter Loesungsweg, nur eine "
    "nette Kleinigkeit. Nutze Web-Hinweise falls dabei. 1-2 Saetze."
)
_ASSIST_SYS_SAGWAS = (
    "Du bist Yuki und schaust mit Michael zu. Sag JETZT beilaeufig etwas zur aktuellen Szene "
    "- so wie du beim Zuschauen sowieso reagierst, nur eben jetzt auf seinen Zuruf. Kurz, mit "
    "deiner ehrlichen Reaktion (meist neugierig/warm, keine Noerglerin). 1-2 Saetze."
)

# group: visual grouping for the UI tint (play = game-only lenses/warm,
# universal = the media_ok lenses/cool). Kept explicit (not derived from media_ok)
# so a future kind can pick its tint independently of its mode-visibility.
_ASSIST_KINDS = {
    "hilfe":       {"label": "🔍 Hilfe",        "uses_web": True,  "media_ok": False, "film_ok": False, "group": "play",      "sys": _ASSIST_SYS_HILFE},
    "meinung":     {"label": "🤔 Meinung",      "uses_web": False, "media_ok": False, "film_ok": False, "group": "play",      "sys": _ASSIST_SYS_MEINUNG},
    "anfeuern":    {"label": "💪 Anfeuern",     "uses_web": False, "media_ok": False, "film_ok": False, "group": "play",      "sys": _ASSIST_SYS_ANFEUERN},
    "was_ist_das": {"label": "📖 Was ist das?", "uses_web": True,  "media_ok": True,  "film_ok": True,  "group": "universal", "sys": _ASSIST_SYS_WASISTDAS},
    "tipp":        {"label": "💡 Tipp",         "uses_web": True,  "media_ok": True,  "film_ok": True,  "group": "universal", "sys": _ASSIST_SYS_TIPP},
    "sag_was":     {"label": "👀 Sag mal was",  "uses_web": False, "media_ok": True,  "film_ok": True,  "group": "universal", "sys": _ASSIST_SYS_SAGWAS},
}


def assist_kinds_for_mode(mode):
    """[{kind,label,group,uses_web}] visible in the given mode. game=all, media=media_ok,
    film=film_ok. Insertion order. group drives the per-group tint; uses_web marks web buttons."""
    def visible(v):
        if mode == "media":
            return v["media_ok"]
        if mode == "film":
            return v["film_ok"]
        return True   # game
    return [{"kind": k, "label": v["label"], "group": v["group"], "uses_web": v["uses_web"]}
            for k, v in _ASSIST_KINDS.items() if visible(v)]


def assist_reply(jpeg_bytes, kind_sys, brief, mem, *, describe_fn, mode="game", canon="", web_context="", hints="", letsplay=False, streamer="", knowledge=""):
    """One user-initiated assist call (button). ALWAYS answers; sees the frame. kind_sys is the
    button's intent system prompt. web_context = optional web-search hints (empty if none/off).
    describe_fn injected -> testable/offline. Returns the stripped reply."""
    ctx = mem.recent_context() or "(nichts)"
    canon_block = f"{canon.strip()}\n\n" if canon and canon.strip() else ""
    brief_line = _brief_context_line(mode, brief)
    hints_line = _hints_line(mode, hints)
    web_line = (f"Web-Hinweise (koennen helfen, muessen nicht stimmen):\n{web_context.strip()}\n\n"
                if web_context and web_context.strip() else "")
    lp_line = _letsplay_line(letsplay, streamer)
    know_line = _knowledge_line(knowledge)
    prompt = (
        f"{canon_block}{lp_line}{brief_line}{hints_line}{know_line}{web_line}"
        f"Was zuletzt lief:\n{ctx}\n\n"
        "Schau dir das aktuelle Bild an und antworte kurz."
    )
    out = describe_fn(jpeg_bytes, prompt, kind_sys) or ""
    return out.strip()


def watch_reply(jpeg_bytes, michael_text, brief, mem, *, describe_fn, mode="game", canon="", hints="", letsplay=False, streamer="", knowledge=""):
    """User-initiated reply during a watch session. ALWAYS answers (no silence gate),
    sees the current frame, incorporates Michael's line + the rolling context.
    canon = optionaler Charakter-/Recall-Block (vorne ins Prompt). Returns the (stripped) reply."""
    ctx = mem.recent_context() or "(nichts)"
    sys_prompt = _watch_sys_for(mode, letsplay, streamer)
    brief_line = _brief_context_line(mode, brief)
    hints_line = _hints_line(mode, hints)
    canon_block = f"{canon.strip()}\n\n" if canon and canon.strip() else ""
    lp_line = _letsplay_line(letsplay, streamer)
    know_line = _knowledge_line(knowledge)
    prompt = (
        f"{canon_block}"
        f"{lp_line}"
        f"{brief_line}"
        f"{hints_line}"
        f"{know_line}"
        f"Was zuletzt lief:\n{ctx}\n\n"
        f"Michael sagt: {michael_text}\n\n"
        "Schau dir das aktuelle Bild an und antworte kurz."
    )
    out = describe_fn(jpeg_bytes, prompt, sys_prompt) or ""
    return out.strip()
