"""Wadoku XML-Dump -> SQLite-Importer.

Liest data/wadoku-xml-YYYYMMDD/wadoku.xml ein (Streaming via iterparse) und
schreibt eine indizierte SQLite-Datenbank nach data/wadoku.sqlite. Die DB ist
die Source-of-Truth fuer den Wort-fuer-Wort-Gloss (Kyoto-Persona-Popup).

Schema:
  entries(id INTEGER PK, pos TEXT, reading TEXT NOT NULL, glosses_json TEXT)
  forms (entry_id INTEGER, surface TEXT NOT NULL, is_primary INTEGER)
  INDEX idx_forms_surface ON forms(surface)
  INDEX idx_entries_reading ON entries(reading)

surface ist die Such-Achse: alle <orth>-Varianten eines Entries landen einzeln
in forms (eine pro Variante), damit Kanji- UND Kana-Schreibweisen funktionieren.
glosses_json ist eine JSON-Liste: [{"domain": "Med.", "text": "Insulin"}, ...].

Aufruf:
    D:\\Projects\\yuki\\.venv\\Scripts\\python.exe D:\\Projects\\yuki\\tools\\import_wadoku.py

Idempotent: vorhandene wadoku.sqlite wird vor dem Import geloescht (sonst wuerden
PK-Konflikte beim Re-Import knallen). Laufzeit ~5-10 Min auf NVMe (441k Eintraege).
"""
from __future__ import annotations
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "wadoku.sqlite"

# Wadoku-Namespace fuer alle Eintrags-Elemente (steht im xmlns-Attribut von <entry>).
NS = "{http://www.wadoku.de/xml/entry}"

# POS-Tags aus dem Wadoku-Schema -> kurze Labels fuer das Popup.
POS_LABEL = {
    "meishi":       "Subst.",
    "doushi":       "Verb",
    "keiyoushi":    "i-Adj.",
    "keiyoudoushi": "na-Adj.",
    "fukushi":      "Adv.",
    "rentaishi":    "Rentaishi",
    "setsuzokushi": "Konj.",
    "kandoushi":    "Interj.",
    "jodoushi":     "Hilfsverb",
    "joshi":        "Partikel",
}


def _orth_text(orth_el):
    """orth kann Sondermarken haben (z.B. fuehrende △ bei midashigo). Wir nehmen
    den Klartext und strippen die Doku-Marker, damit der Tokenizer-Output
    matchen kann."""
    t = "".join(orth_el.itertext()).strip()
    # △ und ◇ markieren in Wadoku Sonder-Lesungen/Schreibungen - Tokenizer-Output
    # hat sie nicht, also raus.
    return t.lstrip("△◇▲▼")


def _tr_text(tr_el):
    """<tr> kann reinen Text haben oder gemischt mit <token>-Subelementen
    ('friedliche <token>Nutzung</token> der Atomkraft'). itertext() klebt alles
    zusammen."""
    return re.sub(r"\s+", " ", "".join(tr_el.itertext())).strip()


def _pos_label(entry):
    """Erstes child-Element in <gramGrp> als POS, gemapped auf Kurzlabel.
    Bei Kombinationen (z.B. meishi + suru-doushi) nehmen wir den ersten -
    fuer die Popup-Anzeige reicht das."""
    gram = entry.find(NS + "gramGrp")
    if gram is None:
        return None
    for child in gram:
        # Namespace strippen, child.tag ist '{NS}meishi' o.ae.
        tag = child.tag.split("}", 1)[-1]
        if tag in POS_LABEL:
            return POS_LABEL[tag]
    return None


def _parse_entry(entry):
    """Einen <entry> auf das Schema {id, pos, reading, forms, glosses} reduzieren.
    Gibt None zurueck, wenn die Pflichtfelder (orth + reading) fehlen - der Eintrag
    wird dann uebersprungen (passiert bei Cross-Reference-Stub-Entries)."""
    eid = int(entry.get("id", "0"))
    if eid <= 0:
        return None

    form = entry.find(NS + "form")
    if form is None:
        return None
    orth_els = form.findall(NS + "orth")
    if not orth_els:
        return None
    # Alle Schreibvarianten - doppelte raus, Reihenfolge behalten (erstes = primary).
    seen = set()
    forms = []
    for o in orth_els:
        s = _orth_text(o)
        if s and s not in seen:
            seen.add(s)
            forms.append(s)
    if not forms:
        return None

    reading_el = form.find(NS + "reading")
    if reading_el is None:
        return None
    hira_el = reading_el.find(NS + "hira")
    if hira_el is None or not (hira_el.text or "").strip():
        return None
    reading = hira_el.text.strip()

    pos = _pos_label(entry)

    glosses = []
    for sense in entry.findall(NS + "sense"):
        # Domain-Tag (z.B. 'Med.', 'Phys.') - optional, hilft im Popup zu sehen
        # dass eine Bedeutung fachsprachlich ist.
        domain = None
        for usg in sense.findall(NS + "usg"):
            if usg.get("type") == "dom" and usg.text:
                domain = usg.text.strip()
                break
        for trans in sense.findall(NS + "trans"):
            for tr in trans.findall(NS + "tr"):
                txt = _tr_text(tr)
                if not txt:
                    continue
                glosses.append({"domain": domain, "text": txt})

    if not glosses:
        return None

    return {
        "id": eid,
        "pos": pos,
        "reading": reading,
        "forms": forms,
        "glosses": glosses,
    }


def _build_schema(con):
    cur = con.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS forms;
        DROP TABLE IF EXISTS entries;
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            pos TEXT,
            reading TEXT NOT NULL,
            glosses_json TEXT NOT NULL
        );
        CREATE TABLE forms (
            entry_id INTEGER NOT NULL,
            surface TEXT NOT NULL,
            is_primary INTEGER NOT NULL
        );
    """)
    con.commit()


def _build_indices(con):
    """Nach dem Bulk-Insert: Indizes anlegen. Vorher waere jeder Insert teuer,
    nachher ist das ein einmaliger Build (Sekunden)."""
    cur = con.cursor()
    cur.executescript("""
        CREATE INDEX idx_forms_surface ON forms(surface);
        CREATE INDEX idx_entries_reading ON entries(reading);
    """)
    con.commit()


def main():
    # Source-Datei auswaehlen (neueste data/wadoku-xml-YYYYMMDD/wadoku.xml).
    candidates = sorted(DATA.glob("wadoku-xml-*/wadoku.xml"), reverse=True)
    if not candidates:
        print(f"FEHLER: keine wadoku.xml unter {DATA}\\wadoku-xml-*/ gefunden.")
        print("Lade vorher den XML-Dump von https://www.wadoku.de/wiki/display/WAD/Downloads+und+Links")
        sys.exit(1)
    src = candidates[0]
    print(f"Quelle:   {src}")
    print(f"Ziel:     {DB_PATH}")
    print()

    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=OFF")        # Bulk-Import, kein Crash-Recovery noetig
    con.execute("PRAGMA synchronous=OFF")
    _build_schema(con)
    cur = con.cursor()

    t0 = time.time()
    n_in = n_out = 0
    batch_entries = []
    batch_forms = []
    BATCH = 5000

    # iterparse mit clear(): Memory bleibt konstant trotz 215MB XML.
    for event, elem in ET.iterparse(str(src), events=("end",)):
        if elem.tag != NS + "entry":
            continue
        n_in += 1
        try:
            parsed = _parse_entry(elem)
        except Exception as e:
            print(f"  [Parse-Fehler bei entry id={elem.get('id')}: {e}]")
            parsed = None
        if parsed:
            batch_entries.append((
                parsed["id"], parsed["pos"], parsed["reading"],
                json.dumps(parsed["glosses"], ensure_ascii=False),
            ))
            for i, s in enumerate(parsed["forms"]):
                batch_forms.append((parsed["id"], s, 1 if i == 0 else 0))
            n_out += 1

        # Element freigeben (sonst frisst das XML-Tree alles)
        elem.clear()

        if len(batch_entries) >= BATCH:
            cur.executemany(
                "INSERT INTO entries(id,pos,reading,glosses_json) VALUES (?,?,?,?)",
                batch_entries)
            cur.executemany(
                "INSERT INTO forms(entry_id,surface,is_primary) VALUES (?,?,?)",
                batch_forms)
            batch_entries.clear()
            batch_forms.clear()
            if n_in % 50000 == 0:
                dt = time.time() - t0
                print(f"  {n_in:>7d} eingelesen, {n_out:>7d} geschrieben "
                      f"({dt:.1f}s, {n_in/dt:.0f} Eintr/s)")

    if batch_entries:
        cur.executemany(
            "INSERT INTO entries(id,pos,reading,glosses_json) VALUES (?,?,?,?)",
            batch_entries)
        cur.executemany(
            "INSERT INTO forms(entry_id,surface,is_primary) VALUES (?,?,?)",
            batch_forms)

    con.commit()
    print()
    print(f"Parsing fertig: {n_in} XML-Eintraege, {n_out} in DB (Diff = Stub-Entries ohne Glossen).")
    print("Baue Indizes ...")
    _build_indices(con)
    # Statistik
    cur.execute("SELECT COUNT(*) FROM entries"); n_entries = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM forms");   n_forms   = cur.fetchone()[0]
    con.close()

    db_size_mb = DB_PATH.stat().st_size / (1024*1024)
    print()
    print(f"Fertig in {time.time()-t0:.1f}s.")
    print(f"  Eintraege: {n_entries}")
    print(f"  Formen:    {n_forms}")
    print(f"  DB-Groesse: {db_size_mb:.1f} MB")


if __name__ == "__main__":
    main()
