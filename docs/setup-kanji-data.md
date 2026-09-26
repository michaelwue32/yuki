# Kanji-Daten installieren (KANJIDIC2 + KanjiVG)

Wadoku-Popup erweitert sich seit 2026-06-05 automatisch um Kanji-Details
(Strichzahl, Radikal, JLPT, On-/Kun-Lesungen + animierte Strichordnung). Die
Server-Endpoints `/kanji/<char>` und `/kanjivg/<file>.svg` brauchen dafür zwei
einmalig zu installierende Offline-Datensätze.

**Was du am Ende hast:**
- `data/kanjidic2.sqlite` (~3-5 MB) — Metadaten pro Kanji (~13k Einträge)
- `data/kanjivg/*.svg` (~13k Dateien, ~25 MB entpackt) — Strichordnungs-SVGs

Beides ist offline-only, beide Lizenzen passen für Yuki-Eigennutzung
(KANJIDIC2 = EDRDG, KanjiVG = CC BY-SA 3.0).

---

## 1) KANJIDIC2 (Metadaten)

```powershell
# In D:\Projects\yuki\data\ landen
$dst = "D:\Projects\yuki\data\kanjidic2.xml.gz"
Invoke-WebRequest -Uri "http://www.edrdg.org/kanjidic/kanjidic2.xml.gz" `
                  -OutFile $dst

# Entpacken (gzip)
$gz = "D:\Projects\yuki\data\kanjidic2.xml.gz"
$xml = "D:\Projects\yuki\data\kanjidic2.xml"
$in = [System.IO.File]::OpenRead($gz)
$out = [System.IO.File]::Create($xml)
$gzs = New-Object System.IO.Compression.GzipStream($in,
       [System.IO.Compression.CompressionMode]::Decompress)
$gzs.CopyTo($out); $gzs.Close(); $out.Close(); $in.Close()
Remove-Item $gz  # gzip-Datei nicht mehr nötig

# Import-Skript laufen lassen
D:\Projects\yuki\.venv\Scripts\python.exe `
    D:\Projects\yuki\tools\import_kanjidic2.py
```

Resultat: `data/kanjidic2.sqlite` (~3-5 MB, indiziert auf `literal` als PK).
Die rohe `data/kanjidic2.xml` kann nach erfolgreichem Import gelöscht werden,
muss aber nicht (wird nur beim Re-Import gebraucht).

---

## 2) KanjiVG (Strichordnungs-SVGs)

KanjiVG-Releases liegen auf GitHub:
https://github.com/KanjiVG/kanjivg/releases

Das Release-Asset heißt typischerweise `kanjivg-YYYYMMDD-main.zip` und enthält
einen Ordner `kanji/` mit ~13k SVG-Dateien (Filenames = 5-stellige
lowercase-Hex-Codepoints, z.B. `06f22.svg` für 漢).

```powershell
# Aktuellstes Release herunterladen (URL ggf. auf neuestes Datum anpassen,
# Checke https://github.com/KanjiVG/kanjivg/releases vor dem Lauf)
$ver = "20240807"
$url = "https://github.com/KanjiVG/kanjivg/releases/download/r$ver/kanjivg-$ver-main.zip"
$zip = "D:\Projects\yuki\data\kanjivg.zip"
Invoke-WebRequest -Uri $url -OutFile $zip

# Entpacken nach data/kanjivg/ (der ZIP-Top-Level-Folder heißt 'kanji', wir
# verschieben dessen Inhalt direkt nach data/kanjivg/)
$tmp = "D:\Projects\yuki\data\_kanjivg_tmp"
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1   # kanji/
$dst = "D:\Projects\yuki\data\kanjivg"
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Move-Item -Path $src.FullName -Destination $dst
Remove-Item -Recurse -Force $tmp
Remove-Item $zip
```

Resultat: `data/kanjivg/*.svg` (~25 MB entpackt, ~13k Files).

---

## 3) Verifikation

```powershell
D:\Projects\yuki\.venv\Scripts\python.exe -c @"
import kanjidict
print('DB available:', kanjidict.is_available())
print('SVGs available:', kanjidict.has_strokes())
print('Lookup test:')
print(' ', kanjidict.lookup_kanji('漢'))
print('SVG path test:')
print(' ', kanjidict.svg_path_for_char('漢'))
"@
```

Output sollte ungefähr so aussehen:

```
DB available: True
SVGs available: True
Lookup test:
  {'literal': '漢', 'codepoint': 28450, 'stroke_count': 13, 'jlpt': 2, ...}
SVG path test:
  D:\Projects\yuki\data\kanjivg\06f22.svg
```

Wenn beides True ist, ist alles bereit — Server neustarten, dann erscheint
beim Tap auf einen Kanji-Token im Wadoku-Popup zusätzlich die Detail-Sektion
mit animierten Strichen.

---

## Disaster-Recovery / Umzug

Bei Crash oder Neuinstallation reicht dieser Doku-Eintrag — die Quell-XML
(EDRDG) und das KanjiVG-ZIP (GitHub) sind beide stabil + einfach
re-besorgbar. SQLite-DB + SVG-Dir können aus dem Yuki-Backup (siehe
`docs/backup.md`) wiederhergestellt werden, müssen aber nicht — der Re-Import
dauert <30s gesamt.

---

## Lizenz-Hinweise

- **KANJIDIC2**: EDRDG-Lizenz (Jim Breen, http://www.edrdg.org/edrdg/licence.html).
  Quellenangabe nötig bei Weitergabe. Für lokale Yuki-Nutzung unproblematisch.
- **KanjiVG**: CC BY-SA 3.0 (Ulrich Apel). Bei Weitergabe Attribution +
  Share-Alike. Für lokale Yuki-Nutzung unproblematisch.

Keiner der beiden Datensätze verlässt deinen Rechner.
