"""KANJIDIC2 XML-Dump -> SQLite-Importer.

Liest data/kanjidic2.xml (von EDRDG, ~16 MB) ein und schreibt eine indizierte
SQLite-Datenbank nach data/kanjidic2.sqlite. Die DB liefert pro Kanji die
Metadaten (Strichzahl, JLPT, Schulstufe, Frequenz, Radikal, On-/Kun-Lesungen,
englische Bedeutungen) fuer das erweiterte Wadoku-Popup.

Schema:
  kanji(
    literal TEXT PRIMARY KEY,        -- das Kanji-Zeichen selbst (1 char)
    codepoint INTEGER,               -- UCS-Codepoint (dezimal, fuer KanjiVG-Filename-Lookup)
    stroke_count INTEGER,
    jlpt INTEGER,                    -- Original JLPT-Skala 1..4 (4 = einfachstes)
    grade INTEGER,                   -- Schul-Klasse 1..6, 8 = Joyo nicht-Pflicht, 9 = Jinmeiyo, 10 = Jinmeiyo-Varianten
    freq INTEGER,                    -- Zeitungs-Frequenz-Rang (1 = haeufigst), NULL wenn nicht in top 2500
    radical INTEGER,                 -- klassische Radikal-Nummer (1..214)
    on_readings TEXT NOT NULL,       -- JSON-Liste Katakana On-Lesungen
    kun_readings TEXT NOT NULL,      -- JSON-Liste Hiragana Kun-Lesungen (mit . fuer Stem-Endung)
    meanings_en TEXT NOT NULL        -- JSON-Liste englische Bedeutungen
  )
  INDEX idx_kanji_codepoint ON kanji(codepoint)
  (keine weiteren Indizes - literal ist PK, das reicht fuer alle Lookups)

Aufruf:
    D:\\Projects\\yuki\\.venv\\Scripts\\python.exe D:\\Projects\\yuki\\tools\\import_kanjidic2.py

Idempotent: vorhandene kanjidic2.sqlite wird vor dem Import geloescht.
Laufzeit ~5-10s (~13k Eintraege). Resultierende DB-Groesse ~3-5 MB.

Source-Datei:
  http://www.edrdg.org/kanjidic/kanjidic2.xml.gz  (~5 MB gepackt)
  Entpacken nach: D:\\Projects\\yuki\\data\\kanjidic2.xml

License-Hinweis: KANJIDIC2 ist EDRDG-lizenziert (Quellenangabe noetig bei
Weitergabe). Fuer lokale Yuki-Nutzung passt. Details:
http://www.edrdg.org/edrdg/licence.html
"""
from __future__ import annotations
import json
import sqlite3
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC_XML = DATA / "kanjidic2.xml"
DB_PATH = DATA / "kanjidic2.sqlite"


def _parse_character(elem):
    """Ein <character>-Element auf das Schema reduzieren. Liefert dict oder
    None wenn Pflichtfelder fehlen."""
    literal_el = elem.find("literal")
    if literal_el is None or not (literal_el.text or "").strip():
        return None
    literal = literal_el.text.strip()
    if len(literal) != 1:        # Defensive: KANJIDIC2 ist single-char per <character>
        return None

    # UCS-Codepoint (Dezimal). KanjiVG-Filenames sind 5-stellige Hex-Codepoints.
    codepoint = ord(literal)

    misc = elem.find("misc")
    stroke_count = jlpt = grade = freq = None
    if misc is not None:
        sc_el = misc.find("stroke_count")
        if sc_el is not None and (sc_el.text or "").strip():
            try: stroke_count = int(sc_el.text.strip())
            except ValueError: pass
        jlpt_el = misc.find("jlpt")
        if jlpt_el is not None and (jlpt_el.text or "").strip():
            try: jlpt = int(jlpt_el.text.strip())
            except ValueError: pass
        grade_el = misc.find("grade")
        if grade_el is not None and (grade_el.text or "").strip():
            try: grade = int(grade_el.text.strip())
            except ValueError: pass
        freq_el = misc.find("freq")
        if freq_el is not None and (freq_el.text or "").strip():
            try: freq = int(freq_el.text.strip())
            except ValueError: pass

    # Radikal (classical). Es gibt optional auch nelson-radical; classical reicht.
    radical = None
    for rv in elem.findall(".//radical/rad_value"):
        if rv.get("rad_type") == "classical":
            try: radical = int((rv.text or "").strip())
            except ValueError: pass
            break

    on_readings = []
    kun_readings = []
    meanings_en = []
    rm = elem.find("reading_meaning")
    if rm is not None:
        # rmgroup: gruppiert Lesungen+Bedeutungen pro Kanji-"Identitaet". Bei
        # Mehrfach-rmgroups (selten) mergen wir alle - im Popup reicht die
        # Gesamtliste, fein-feiner Sliced-by-Sense bringt visuell nichts.
        for rmg in rm.findall("rmgroup"):
            for r in rmg.findall("reading"):
                rtype = r.get("r_type")
                txt = (r.text or "").strip()
                if not txt:
                    continue
                if rtype == "ja_on":
                    on_readings.append(txt)
                elif rtype == "ja_kun":
                    kun_readings.append(txt)
            for m in rmg.findall("meaning"):
                # m ohne m_lang-Attribut = Englisch (Default). DE existiert in
                # KANJIDIC2 nicht, FR/PT/ES schon. Wir nehmen nur EN.
                if not m.get("m_lang") and (m.text or "").strip():
                    meanings_en.append(m.text.strip())

    return {
        "literal": literal,
        "codepoint": codepoint,
        "stroke_count": stroke_count,
        "jlpt": jlpt,
        "grade": grade,
        "freq": freq,
        "radical": radical,
        "on_readings": on_readings,
        "kun_readings": kun_readings,
        "meanings_en": meanings_en,
    }


def _build_schema(con):
    cur = con.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS kanji;
        CREATE TABLE kanji (
            literal       TEXT PRIMARY KEY,
            codepoint     INTEGER NOT NULL,
            stroke_count  INTEGER,
            jlpt          INTEGER,
            grade         INTEGER,
            freq          INTEGER,
            radical       INTEGER,
            on_readings   TEXT NOT NULL,
            kun_readings  TEXT NOT NULL,
            meanings_en   TEXT NOT NULL
        );
    """)
    con.commit()


def _build_indices(con):
    cur = con.cursor()
    cur.executescript("""
        CREATE INDEX idx_kanji_codepoint ON kanji(codepoint);
    """)
    con.commit()


def main():
    if not SRC_XML.is_file():
        print(f"FEHLER: {SRC_XML} nicht gefunden.")
        print()
        print("Download-Schritte:")
        print("  1) http://www.edrdg.org/kanjidic/kanjidic2.xml.gz herunterladen")
        print("  2) Entpacken nach D:\\Projects\\yuki\\data\\kanjidic2.xml")
        print("  3) Dieses Skript erneut starten")
        sys.exit(1)

    print(f"Quelle:  {SRC_XML}")
    print(f"Ziel:    {DB_PATH}")
    print()

    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    _build_schema(con)
    cur = con.cursor()

    t0 = time.time()
    n_in = n_out = 0
    batch = []
    BATCH = 2000

    for event, elem in ET.iterparse(str(SRC_XML), events=("end",)):
        if elem.tag != "character":
            continue
        n_in += 1
        try:
            parsed = _parse_character(elem)
        except Exception as e:
            print(f"  [Parse-Fehler bei character n={n_in}: {e}]")
            parsed = None
        if parsed:
            batch.append((
                parsed["literal"], parsed["codepoint"],
                parsed["stroke_count"], parsed["jlpt"], parsed["grade"],
                parsed["freq"], parsed["radical"],
                json.dumps(parsed["on_readings"], ensure_ascii=False),
                json.dumps(parsed["kun_readings"], ensure_ascii=False),
                json.dumps(parsed["meanings_en"], ensure_ascii=False),
            ))
            n_out += 1
        elem.clear()

        if len(batch) >= BATCH:
            cur.executemany(
                "INSERT INTO kanji(literal,codepoint,stroke_count,jlpt,grade,"
                "freq,radical,on_readings,kun_readings,meanings_en) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()

    if batch:
        cur.executemany(
            "INSERT INTO kanji(literal,codepoint,stroke_count,jlpt,grade,"
            "freq,radical,on_readings,kun_readings,meanings_en) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", batch)

    con.commit()
    print(f"Parsing fertig: {n_in} Eintraege gelesen, {n_out} geschrieben.")
    print("Baue Indizes ...")
    _build_indices(con)

    cur.execute("SELECT COUNT(*) FROM kanji")
    n_total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM kanji WHERE jlpt IS NOT NULL")
    n_jlpt = cur.fetchone()[0]
    con.close()

    db_size_mb = DB_PATH.stat().st_size / (1024 * 1024)
    print()
    print(f"Fertig in {time.time()-t0:.1f}s.")
    print(f"  Eintraege:    {n_total}")
    print(f"  mit JLPT:     {n_jlpt}")
    print(f"  DB-Groesse:   {db_size_mb:.1f} MB")


if __name__ == "__main__":
    main()
