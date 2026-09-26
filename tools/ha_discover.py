#!/usr/bin/env python
# tools/ha_discover.py - Home-Assistant-Geraete-Discovery (CLI-Wrapper).
#
# Die eigentliche Logik lebt in homeassistant.discover() (gemeinsame Quelle mit dem
# UI-Button "Geraete suchen" im Options-Modal). Dieses Script ist nur die
# Kommandozeilen-Huelle drumherum mit huebscher Ausgabe.
#
# Synchronisiert die devices-Allowlist in config/yuki_homeassistant.json mit HA:
#   * NEUE schaltbare Entitaeten (light/switch/fan/input_boolean) -> enabled:false
#     (opt-in; Yuki sieht/schaltet sie erst, wenn du sie auf enabled:true stellst).
#   * VERSCHWUNDENE Geraete -> enabled:false (mit --prune ganz raus).
#   * BESTEHENDE Eintraege bleiben unangetastet (enabled-Flag, Name, Area).
#
# Aufruf (aus dem Repo-Root, mit der Haupt-venv):
#     .venv\Scripts\python.exe tools\ha_discover.py            # schreibt die Config
#     .venv\Scripts\python.exe tools\ha_discover.py --dry-run  # nur Vorschau
#     .venv\Scripts\python.exe tools\ha_discover.py --prune    # verschwundene raus

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import homeassistant as ha

# Windows-Konsole ist cp1252 -> Umlaute in Geraetenamen wuerden als Mojibake
# erscheinen (Stolperfalle 8). Die JSON-Datei selbst ist davon unabhaengig (utf-8).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    dry = "--dry-run" in sys.argv
    prune = "--prune" in sys.argv

    res = ha.discover(prune=prune, dry_run=dry)
    if not res.get("ok"):
        print(f"FEHLER: {res.get('error')}")
        sys.exit(1)

    for d in res["new"]:
        print(f"  + NEU (disabled): {d['name']}  [{d['entity_id']}]")
    for d in res["vanished"]:
        verb = "entfernt" if prune else "disabled"
        print(f"  {'-' if prune else '~'} VERSCHWUNDEN ({verb}): {d['name']}  [{d['entity_id']}]")

    print(f"\n{len(res['new'])} neu (disabled) · {len(res['vanished'])} verschwunden "
          f"({'entfernt' if prune else 'disabled'}) · {res['total']} in Config "
          f"· {res['enabled']} davon enabled (fuer Yuki aktiv).")
    if dry:
        print("\n--dry-run: nichts geschrieben.")
    elif res["new"]:
        print('\nNeue Geraete sind disabled. Im Options-Modal (🛠 System -> HA-Geraete) '
              'oder in der Config auf "enabled": true setzen, was Yuki steuern soll.')


if __name__ == "__main__":
    main()
