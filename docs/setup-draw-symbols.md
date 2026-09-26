# Setup: Draw-Stempel-Bibliothek (OpenMoji) wieder aufsetzen

Die Künstlerin-Persona komponiert ihre Doodles seit 2026-06-19 zusätzlich aus fertigen
**Stempel-Motiven** (OpenMoji), die sie mit `<use href='#slug' .../>` ins SVG setzt, einfärbt
und mit eigenen Freihand-Strichen mischt ([[yuki-drawing-feature]]). Die Bibliothek ist
**gitignored** (~11 MB Bundles, jederzeit re-downloadbar) – nach einem frischen Clone oder
Disaster-Recovery einmal neu holen, sonst malt die Künstlerin nur reines Freihand (degradiert
sauber, kein Fehler).

## Holen / Neu bauen

```powershell
cd D:\Projects\yuki
.\.venv\Scripts\python.exe tools\fetch_draw_symbols.py
```

Das Skript (stdlib-only, kein Extra-Dependency):

1. lädt `openmoji.json` (Metadaten: annotation/tags/group je Motiv),
2. lädt die beiden OpenMoji-**Release-Zips** (`openmoji-svg-color.zip` + `openmoji-svg-black.zip`)
   von GitHub und entpackt sie im Speicher,
3. normalisiert je Motiv **zwei** `<symbol>`-Varianten:
   - `line` (OpenMoji-black): Schwarz → `currentColor`, damit Yuki pro `<use color='#hex'>` tintet,
   - `color` (OpenMoji-color): flach bunt, unverändert,
4. schreibt nach `data/draw_symbols/`:
   - `symbols_line.json`  – `{slug: "<symbol id='slug' …>…</symbol>"}`
   - `symbols_color.json` – `{slug: "<symbol id='slug-color' …>…</symbol>"}`
   - `index.json`         – Suchindex `[{slug, hexcode, annotation, tags, group, subgroups, has_color}]`
   - `CREDITS.md`         – OpenMoji-Attribution (CC BY-SA 4.0)

Skintone-Varianten werden übersprungen (Basis-Motiv genügt). Ergebnis: ~2500 Motive.
Flags `--limit N` (Teilmenge zum Testen) und `--force` (vorhandenen Output neu bauen).

## Wie es zur Laufzeit benutzt wird

- **Kern-Satz**: `CORE_STAMPS` in `yuki_core.py` (162 validierte Slugs, nach Kategorie) wird
  über `core_stamps_block()` immer in den Künstlerin-Prompt gehängt → die meisten Doodles
  brauchen keine Suche.
- **On-Demand-Suche**: schreibt Yuki `[stamps:english keywords]`, durchsucht
  `generate_kuenstlerin_reply` (server.py-Pfad) die lokale `index.json` und ruft mit den
  Treffern erneut (bis `stamp_search_max_rounds`). Rein lokal, kein Netz/GPU.
- **Defs-Injektion**: `inject_symbol_defs()` setzt nur die tatsächlich referenzierten
  `<symbol>`-Defs ins SVG, bevor es gerendert/angezeigt/ins Album gespeichert wird
  (Browser + resvg lösen `#id` nur im selben Dokument). Die **Leinwand (WIP)** speichert
  bewusst die kompakte `<use>`-Fassung – kein Token-Stau im Prompt.

## Abschalten

`config/settings.jsonc → drawing.symbols_enabled = false` (Restart) → Künstlerin malt wieder
reines Freihand-SVG wie vor 2026-06-19. Weitere Tunables dort: `symbol_inject_cap`,
`stamp_search_max_rounds`, `stamp_search_result_limit`.

## Lizenz

Motive aus **OpenMoji** (https://openmoji.org), **CC BY-SA 4.0**. Attribution liegt nach dem
Fetch in `data/draw_symbols/CREDITS.md`. Für das private Offline-Setup unkritisch; bei einer
etwaigen Veröffentlichung von Bildern Namensnennung + Share-Alike beachten.
