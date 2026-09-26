"""
config_loader.py - JSONC-Reader fuer config/settings.jsonc
============================================================

Ein duenner Helper, der die zentrale Yuki-Konfiguration laedt. Drei Designziele:

  1. **Nur stdlib** (json + re). Kein json5/PyYAML/toml-Dependency, damit das
     Modul aus JEDER Python-venv heraus funktioniert (auch der separaten
     D:\\Server\\f5tts-venv, die nicht yuki-core's Packages hat).

  2. **JSONC-Support**: erlaubt //-Zeilenkommentare und /* ... */-Bloeke - bei
     reinem JSON sind Kommentare verboten, das macht die Datei aber nutzlos
     fuer Selbst-Doku (Min/Max/Beschreibung direkt am Wert).

  3. **Defensiv**: fehlende Datei, fehlende Sektion, fehlender Key liefern den
     mitgegebenen Default zurueck. Yuki startet damit auch wenn settings.jsonc
     komplett geloescht ist - jede Konstante hat in den importierenden
     Modulen einen hartcodierten Fallback. Die Config ist Komfort, kein Muss.

Verwendung:

    from config_loader import settings
    HISTORY_KEEP_LAST = settings.get("memory", "history_keep_last", 10)

Sektionen sind dicts, Keys sind die Tunable-Namen (snake_case). Werte
werden 1:1 durchgereicht; Typ-Casts (z.B. Date-Strings -> datetime.date)
macht das importierende Modul, weil es weiss was es will.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

# Datei liegt neben diesem Modul (Projektroot/config/settings.jsonc).
_HERE = Path(__file__).resolve().parent
_CONFIG_PATH = _HERE / "config" / "settings.jsonc"

# JSONC -> JSON: //-Kommentare bis Zeilenende UND /* ... */ Bloecke entfernen.
# Achtung: NICHT innerhalb von String-Literalen ersetzen. Wir machen einen
# zweistufigen Pass mit einer Regex, die Strings als Ganzes matcht und
# unveraendert durchreicht; Kommentare werden nur ausserhalb von Strings entfernt.
_JSONC_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'   # 1) String-Literale (inkl. escaped quotes) - bleiben
    r'|//[^\n]*'           # 2) Zeilenkommentar
    r'|/\*.*?\*/',         # 3) Blockkommentar
    re.DOTALL,
)


def _strip_jsonc(text: str) -> str:
    """Kommentare aus JSONC raushauen, Strings unangetastet lassen."""
    def repl(m):
        s = m.group(0)
        # String-Literale starten mit "; alles andere ist Kommentar -> weg.
        return s if s.startswith('"') else ""
    return _JSONC_RE.sub(repl, text)


class _Settings:
    """Lazy-loading Singleton mit Reload-Moeglichkeit (settings.reload())."""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict = {}
        self._lock = threading.Lock()
        self._loaded = False
        self._load_error: str | None = None

    def _ensure_loaded(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load()

    def _load(self):
        self._data = {}
        self._load_error = None
        try:
            if not self._path.exists():
                # Kein Fehler - alle Defaults greifen.
                self._loaded = True
                return
            raw = self._path.read_text(encoding="utf-8")
            stripped = _strip_jsonc(raw)
            self._data = json.loads(stripped) or {}
        except Exception as e:
            # Geparkt als Diagnose - der Aufrufer kriegt trotzdem Defaults zurueck.
            self._load_error = f"{type(e).__name__}: {e}"
            self._data = {}
        finally:
            self._loaded = True

    def reload(self):
        """Datei erneut einlesen (z.B. nach manueller Aenderung).
        Hot-Reload zur Laufzeit greift nur, wenn die Konsumenten ihre
        Werte erneut auslesen - die meisten Yuki-Module lesen am Start
        einmal und legen sie als Modul-Konstante ab. Daher: Server-Restart
        nach Edit bleibt die zuverlaessige Variante."""
        with self._lock:
            self._loaded = False
            self._load()

    def get(self, section: str, key: str | None = None, default=None):
        """Wert aus der Config lesen.
          - settings.get("memory")                   -> ganze Sektion (dict)
          - settings.get("memory", "history_keep_last", 10)  -> einzelner Key mit Default
        Bei jedem nicht gefundenen Pfad wird default zurueckgegeben - so muss
        der Aufrufer nie auf KeyError checken.
        """
        self._ensure_loaded()
        sect = self._data.get(section)
        if key is None:
            return sect if isinstance(sect, dict) else (default if default is not None else {})
        if not isinstance(sect, dict):
            return default
        return sect.get(key, default)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def load_error(self) -> str | None:
        """None wenn Load OK war, sonst die Fehlermeldung. Hilfreich fuer
        einen kurzen Boot-Log: print(settings.load_error) wenn nicht None."""
        self._ensure_loaded()
        return self._load_error


# Modul-globaler Singleton. Importeure greifen via `from config_loader import settings`.
settings = _Settings(_CONFIG_PATH)


# ===========================================================================
# Personas-JSONC (config/personas.jsonc)
# ---------------------------------------------------------------------------
# Eigene Loader-Funktion statt zweiter _Settings-Instanz: das Personas-File
# hat eine andere Struktur (genestetes "personas"-Dict) und braucht
# Validierung der Pflichtfelder. Liefert das geparste dict oder None bei
# fehlender/kaputter Datei - der Aufrufer (yuki_core.py) faellt dann auf
# hardcoded-PERSONAS zurueck, damit Yuki immer lauffaehig bleibt.
# ===========================================================================
_PERSONAS_PATH = _HERE / "config" / "personas.jsonc"

_PERSONA_REQUIRED = ("name", "system", "fewshot")
_PERSONA_DEFAULTS = {
    "enabled": True,
    "language": "de",
    "auto_switch_block": False,
    "force_research": False,
    "scene": "",
    "lighting": "",
    "render": {},
}


def load_personas_jsonc():
    """Personas aus config/personas.jsonc laden.

    Returns:
        dict: {key: persona_dict, ...} - nur valide Personas (mit Pflichtfeldern
              name/system/fewshot). Fehlende optionale Felder werden mit
              _PERSONA_DEFAULTS aufgefuellt.
        None: Datei fehlt, JSONC kaputt, oder kein gueltiges "personas"-Dict drin.

    Bewusst kein Singleton-Cache: yuki_core.py liest beim Start einmal und
    legt die Werte als Modul-Konstante ab - Live-Reload spielt hier keine
    Rolle (Restart-Pattern wie settings.jsonc).
    """
    try:
        if not _PERSONAS_PATH.exists():
            return None
        raw = _PERSONAS_PATH.read_text(encoding="utf-8")
        data = json.loads(_strip_jsonc(raw))
        personas = data.get("personas")
        if not isinstance(personas, dict) or not personas:
            return None
        # Validate + fill defaults. Personas ohne Pflichtfeld werden geskippt
        # mit Log - das macht den Loader robust gegen ein einzelnes kaputtes
        # Entry ohne dass die ganze Datei verworfen wird.
        validated = {}
        for key, p in personas.items():
            if not isinstance(p, dict):
                print(f"  [Personas: '{key}' ist kein Dict, skip]")
                continue
            missing = [f for f in _PERSONA_REQUIRED if not p.get(f)]
            if missing:
                print(f"  [Personas: '{key}' fehlt Pflichtfeld {missing}, skip]")
                continue
            filled = {**_PERSONA_DEFAULTS, **p}
            validated[key] = filled
        return validated or None
    except Exception as e:
        print(f"  [Personas-JSONC-Load fehlgeschlagen: {type(e).__name__}: {e}]")
        return None


def personas_jsonc_path():
    """Pfad zur Personas-Datei (fuer Diagnose/Tests)."""
    return _PERSONAS_PATH
