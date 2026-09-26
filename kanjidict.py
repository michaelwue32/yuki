"""Kanji-Detail-Lookup + KanjiVG-Strichordnungs-Pfade.

Datenquellen (beide einmalig manuell installiert - siehe docs/setup-kanji-data.md):
  * data/kanjidic2.sqlite     - via tools/import_kanjidic2.py erzeugt aus
                                 dem KANJIDIC2-XML-Dump (EDRDG). Eine Zeile pro
                                 Kanji mit Strichzahl, JLPT, Schul-Klasse, Frequenz,
                                 Radikal, On-/Kun-Lesungen, englischen Bedeutungen.
  * data/kanjivg/<hex>.svg    - KanjiVG-Strichordnungs-SVGs (CC BY-SA), 5-stellige
                                 lowercase-Hex-Codepoints. Server serviert die
                                 SVGs als-ist; das Frontend animiert die Striche.

Verwendet im Wadoku-Gloss-Popup (web/index.html): beim Tap auf einen JP-Token
wird pro Kanji im Surface zusaetzlich /kanji/<char> abgeholt und die Detail-
Sektion (Lesungen, Strichzahl, JLPT, animiertes SVG) unter den deutschen
Glossen gerendert.

Thread-Sicherheit + Performance: analog wadoku.py - eine Process-weite SQLite-
Connection mit check_same_thread=False, reine Read-Workload.
"""
from __future__ import annotations
import json
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "kanjidic2.sqlite"
SVG_DIR = ROOT / "data" / "kanjivg"

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
    """True wenn DB UND SVG-Verzeichnis existieren. Wenn nur die DB da ist
    (Metadaten ohne Striche), liefert lookup_kanji trotzdem - das Frontend
    rendert dann das Detail ohne SVG. svg_path_for_char liefert None."""
    return _db() is not None


def has_strokes():
    """True wenn KanjiVG-SVGs verfuegbar sind. Frontend kann das Strich-
    animation-Element weglassen wenn False."""
    return SVG_DIR.is_dir() and any(SVG_DIR.glob("*.svg"))


def lookup_kanji(ch):
    """Ein Kanji-Zeichen -> dict mit Metadaten, oder None wenn nicht in DB.
    Returnt eine bereits geparste Form (Lesungen/Bedeutungen als Listen, keine
    JSON-Strings) - das Frontend kriegt direkt usable JSON."""
    if not ch or len(ch) != 1:
        return None
    con = _db()
    if con is None:
        return None
    r = con.execute(
        "SELECT literal, codepoint, stroke_count, jlpt, grade, freq, radical, "
        "       on_readings, kun_readings, meanings_en "
        "FROM kanji WHERE literal = ?", (ch,)).fetchone()
    if r is None:
        return None
    return {
        "literal": r["literal"],
        "codepoint": r["codepoint"],
        "stroke_count": r["stroke_count"],
        "jlpt": r["jlpt"],
        "grade": r["grade"],
        "freq": r["freq"],
        "radical": r["radical"],
        "on_readings": json.loads(r["on_readings"]),
        "kun_readings": json.loads(r["kun_readings"]),
        "meanings_en": json.loads(r["meanings_en"]),
        "has_svg": svg_path_for_char(ch) is not None,
    }


def svg_path_for_char(ch):
    """KanjiVG-SVG-Pfad fuer ein Kanji-Zeichen. Filename = 5-stelliger lowercase-
    Hex-Codepoint (z.B. 06f22.svg fuer 漢, U+6F22). Returnt None wenn die Datei
    fehlt - das Frontend rendert dann die Detail-Sektion ohne Strichordnung."""
    if not ch or len(ch) != 1:
        return None
    cp = ord(ch)
    fname = f"{cp:05x}.svg"
    path = SVG_DIR / fname
    return path if path.is_file() else None


# Heuristik fuer "ist das ein Kanji?" - ohne fugashi-Abhaengigkeit hier,
# weil kanjidict.py mit minimalen Imports klarkommen soll. Reicht fuer
# Single-Char-Checks im Endpoint.
def is_kanji_char(ch):
    if not ch or len(ch) != 1:
        return False
    cp = ord(ch)
    # CJK Unified Ideographs (Hauptblock) + Extension A. Erweiterungen B-G
    # liegen ausserhalb der BMP und sind in KANJIDIC2 ohnehin nicht abgedeckt.
    return (0x3400 <= cp <= 0x4DBF) or (0x4E00 <= cp <= 0x9FFF) or cp == 0x3005  # 々


def kana_kind(ch):
    """Returnt 'hiragana', 'katakana' oder None. Choon (ー), Mittelpunkt (・),
    Iterationsmarken und Sondermarken liegen ausserhalb der Letter-Bereiche
    und werden None - dafuer gibt es keine sinnvollen Strichanimationen."""
    if not ch or len(ch) != 1:
        return None
    cp = ord(ch)
    if 0x3041 <= cp <= 0x309F:
        return "hiragana"
    if 0x30A1 <= cp <= 0x30FA:
        return "katakana"
    return None


def lookup_kana(ch):
    """Schlanker Kana-Lookup: keine DB (kein KANJIDIC2-Pendant fuer Kana),
    nur Codepoint + SVG-Existenz. Returnt dict mit kind/codepoint/has_svg
    wenn ch ein Kana ist UND eine SVG existiert; sonst None. Letzteres deckt
    Sondermarken (ー, ・, Dakuten) sauber ab - ohne SVG keine Karte."""
    kind = kana_kind(ch)
    if kind is None:
        return None
    if svg_path_for_char(ch) is None:
        return None
    return {
        "kind": kind,
        "literal": ch,
        "codepoint": ord(ch),
        "has_svg": True,
    }
