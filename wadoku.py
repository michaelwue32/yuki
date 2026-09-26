"""Wadoku-Lookup + Tokenisierung fuer den Kyoto-Persona-Gloss-Popup.

Was diese Datei macht:
  * Liest data/wadoku.sqlite (vom tools/import_wadoku.py erzeugt) - 440k
    Eintraege, indiziert auf surface + reading.
  * tokenize_jp(text): zerlegt einen JP-Span in Morpheme (via fugashi) und
    gibt pro Token surface/reading/lemma/pos zurueck. Damit kann der Client
    pro Token einen clickable span rendern.
  * lookup(surface, lemma=None, limit=3): sucht surface-first, lemma als
    Fallback. Returnt eine kleine Liste von Treffern mit reading + Top-3
    deutschen Glossen pro Eintrag.

Thread-Sicherheit:
  Flask laeuft multi-thread. SQLite-Connections sind per Default an einen
  Thread gebunden. Wir nutzen check_same_thread=False + Connection-Cache
  pro Process; bei Bulk-Reads ist das OK (kein Lock-Contention, weil keine
  Writes nach dem initialen Import).

Performance:
  Lookup ist <1ms (Indexed); fugashi-Tokenisierung von 5-30 JP-Zeichen
  ist <5ms. Vor allem das wiederholte JSON-Parsing der glosses-Spalte
  bremst noch - bei Bedarf koennte man einen LRU-Cache reinhaengen.
"""
from __future__ import annotations
import json
import re
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "wadoku.sqlite"

# Fugashi-Tagger: lokal, weil wadoku.py nicht von yuki_core abhaengen will
# (klare DAG). Ist beim Modul-Import einmaliger ~100ms-Hit.
try:
    import fugashi
    _tagger = fugashi.Tagger()
except Exception as _e:
    _tagger = None
    print(f"[Hinweis] Wadoku-Tokenizer deaktiviert (fugashi fehlt: {_e})")

# JP-Span-Regex: Hiragana + Katakana + CJK (Kanji) + Iterator + Choon. Identisch
# zu _JP_SPAN in yuki_core (bewusst dupliziert, damit wadoku.py self-contained ist).
JP_SPAN = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ々ー]+")

# Connection-Cache: SQLite-Connection pro Process, lazy. Wir oeffnen mit
# check_same_thread=False, weil Flask Requests aus verschiedenen Threads kommen
# und unsere Workload reine SELECTs sind (Race-Condition irrelevant).
_DB_LOCK = threading.Lock()
_DB = None


def _db():
    global _DB
    if _DB is not None:
        return _DB
    with _DB_LOCK:
        if _DB is not None:
            return _DB
        if not DB_PATH.is_file():
            return None
        con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        con.row_factory = sqlite3.Row
        _DB = con
        return _DB


def is_available():
    """True wenn die SQLite-DB existiert UND fugashi geladen wurde. False
    deaktiviert den Popup-Pfad im Server (Endpoint returnt 503, Frontend
    rendert kein clickable Markup)."""
    return _db() is not None and _tagger is not None


def _exists(surface):
    """Schneller Boolean-Check: gibt es 'surface' als forms-Eintrag in Wadoku?
    Wird von tokenize_jp fuer den Compound-Merge benutzt - dort brauchen wir
    nur 'Treffer ja/nein', keine Glossen. EXISTS-Query ist schneller als
    SELECT-LIMIT-1, weil die Engine bei erstem Match abbricht."""
    con = _db()
    if con is None or not surface:
        return False
    r = con.execute("SELECT 1 FROM forms WHERE surface = ? LIMIT 1",
                    (surface,)).fetchone()
    return r is not None


# Cap fuer Compound-Merge: bis zu wie vielen aufeinanderfolgenden fugashi-
# Tokens probieren wir das Mergen? 4 deckt erfahrungsgemaess alle ueblichen
# Idiome (おかえり=2, 今日は=2, ありがとう=2-3, さようなら=3, 行ってきます=4)
# ohne den Tokenizer-Pass spuerbar zu verlangsamen.
try:
    from config_loader import settings as _CFG
    _COMPOUND_MAX = _CFG.get("wadoku", "compound_max", 4)
except Exception:
    _COMPOUND_MAX = 4


def lookup(surface, lemma=None, limit=3):
    """Eine surface-form (oder ihr lemma) gegen Wadoku schlagen. Reihenfolge:
      1) exakt 'surface'
      2) wenn 0 Treffer und lemma != surface: lemma versuchen
    Liefert eine Liste {id, pos, reading, glosses: [{domain?, text}, ...]} -
    leere Liste wenn nichts gefunden oder DB nicht verfuegbar.

    glosses pro Eintrag werden auf die ersten 3 reduziert - mehr macht den
    Popup unleserlich. Die Anzahl Eintraege ist via `limit` gedeckelt."""
    if not surface:
        return []
    con = _db()
    if con is None:
        return []

    def _query(s):
        return con.execute(
            "SELECT e.id, e.pos, e.reading, e.glosses_json "
            "FROM forms f JOIN entries e ON e.id = f.entry_id "
            "WHERE f.surface = ? "
            "ORDER BY f.is_primary DESC, e.id "
            "LIMIT ?", (s, limit)).fetchall()

    rows = _query(surface)
    if not rows and lemma and lemma != surface:
        rows = _query(lemma)

    out = []
    for r in rows:
        glosses_all = json.loads(r["glosses_json"])
        out.append({
            "id": r["id"],
            "pos": r["pos"],
            "reading": r["reading"],
            "glosses": glosses_all[:3],     # Top-3 Bedeutungen
        })
    return out


def tokenize_jp(text):
    """Einen Text (kann gemischt DE/JP/EN sein) tokenisieren und nur die JP-
    Morpheme zurueckgeben. Pro Token: surface (wie im Text), lemma (Wadoku-
    Suchform), reading (Hiragana wenn verfuegbar), pos (kurzes Label).

    Wir filtern Tokens ohne JP-Zeichen raus (Whitespace, Satzzeichen, lateinische
    Buchstaben aus dem Mixed-Text) - der Client wrappt nur die JP-Tokens als
    clickable spans, deutsche Worte bleiben Plaintext.

    Compound-Merge-Pass (2026-06-01):
        fugashi spaltet idiomatische Wörter oft in Morpheme: おかえり -> お+かえり,
        今日は -> 今日+は, さようなら -> さよう+なら usw. Fuer Lernende ist das
        verwirrend - sie wollen das idiomatische GANZE Wort sehen ("Heimkehr",
        "Hallo", "Auf Wiedersehen"), nicht Praefix+Verbstamm o.ae. Daher ein
        Greedy-Pass nach der Tokenisierung: fuer jede Position pruefen wir, ob
        die naechsten 2-4 Tokens zusammengeklebt einen Wadoku-Eintrag bilden.
        Wenn ja, mergen wir sie zu einem Token mit combined surface -> der
        spaetere /lookup findet die richtige idiomatische Bedeutung.
        Greedy laengstes-zuerst (k=4..2) verhindert Unter-Merge.

    Reihenfolge im Output entspricht der Reihenfolge im Eingangs-Text - das
    Frontend kann sie damit positionell durchlaufen und wrappen."""
    if _tagger is None or not text:
        return []

    # Phase 1: fugashi roh, nur JP-Tokens behalten.
    raw = []
    for w in _tagger(text):
        surface = w.surface
        if not surface or not JP_SPAN.search(surface):
            continue                           # nicht-JP-Token (DE-Wort, Satzzeichen)
        # Lemma + Reading aus fugashi-Features ziehen. Format-Felder koennen
        # '*' sein (fugashi-Konvention fuer 'fehlt') - dann fallback auf surface.
        lemma = getattr(w.feature, "lemma", None)
        if not lemma or lemma == "*":
            lemma = getattr(w.feature, "orth", None) or surface
        reading = getattr(w.feature, "kana", None)
        if not reading or reading == "*":
            reading = getattr(w.feature, "pron", None)
        if not reading or reading == "*":
            reading = ""
        pos = getattr(w.feature, "pos1", None)
        raw.append({
            "surface": surface,
            "lemma": lemma,
            "reading": reading,
            "pos": pos,
        })

    # Phase 2: Compound-Merge gegen Wadoku. Greedy, laengste Compound zuerst.
    # Wenn die DB nicht verfuegbar ist, raw zurueckgeben (graceful degrade).
    if _db() is None:
        return raw
    out = []
    i = 0
    while i < len(raw):
        merged = False
        # Versuche k=4,3,2 Tokens am Stueck zu mergen. k=1 ist Default (kein Merge).
        max_k = min(_COMPOUND_MAX, len(raw) - i)
        for k in range(max_k, 1, -1):
            combined = "".join(t["surface"] for t in raw[i:i+k])
            if _exists(combined):
                # Treffer: die k Tokens werden EIN Token mit combined surface.
                # lemma==surface ist OK, lookup() macht eh surface-first.
                # reading + pos lassen wir leer; der /lookup-Aufruf liefert
                # die korrekte Wadoku-Reading frisch im Popup.
                out.append({
                    "surface": combined,
                    "lemma": combined,
                    "reading": "",
                    "pos": None,
                })
                i += k
                merged = True
                break
        if not merged:
            out.append(raw[i])
            i += 1
    return out
