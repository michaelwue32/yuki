# Yuki — lokale, zweisprachige Waifu-Japanisch-Tutorin

Yuki ist eine **komplett lokal laufende, gesprochene KI-Begleiterin** und
Japanisch-Tutorin — eine japanische Person, die fließend Deutsch und Japanisch
spricht, Fehler beiläufig korrigiert und nachfragt. **Kein Cloud-Dienst** in der
Pipeline (einzige Ausnahme: optionaler Wetter-Abruf via Open-Meteo).

Sie läuft im Browser (PC + Handy): Push-to-Talk, ein 3D-Avatar mit Lipsync,
mehrere Personas, optionales Sehen über Webcam, ein mehrstufiges Gedächtnis und
Tutor-Werkzeuge (Vokabel-Pool, SRS, Wörterbuch-Popups).

## Was du bekommst — und was du selbst mitbringst

Dies ist das **Konstrukt um Yuki herum** — der Code, mit dem du deine *eigene*
Yuki aufsetzt. Aus Lizenzgründen sind einige Medien **bewusst nicht enthalten**;
`INSTALL.md` erklärt Schritt für Schritt, wie du sie selbst besorgst:

- **3D-Avatar (`.vrm`)** — bring deinen eigenen (z.B. via [VRoid Studio](https://vroid.com/)).
- **Animationen** — lade Clips bei [Mixamo](https://www.mixamo.com) und konvertiere sie (Anleitung liegt bei).
- **Stimme (TTS-Referenz)** — lege eine eigene 3–10 s Referenz-WAV an.
- **Wörterbücher** (Wadoku/KANJIDIC2/KanjiVG) — per Skript nachladbar.

## Was ohne Zusatzdienste läuft

Der **Text-Kern läuft mit nur [Ollama](https://ollama.com)** (ein lokales LLM).
TTS (Sprache), STT (Spracherkennung), Vision (Kamera) und der Avatar sind
**optionale GPU-Kür** — fehlt einer, degradiert Yuki sauber (z.B. Text statt
Sprache). Windows-fokussiert; Linux ist grundsätzlich machbar.

## Schnellstart

Siehe **[INSTALL.md](INSTALL.md)** für die vollständige Anleitung. Kurzform:

```bash
py -3.14 -m venv .venv && .\.venv\Scripts\Activate.ps1   # Windows
pip install -r requirements.lock
cp config/settings.example.jsonc config/settings.jsonc   # Ollama-URL/Modell eintragen
python server.py                                         # https://localhost:8443
```

**Mit Claude Code aufsetzen:** Wenn du [Claude Code](https://claude.com/claude-code)
nutzt, führt dich die beiliegende `CLAUDE.md` durch das Setup — sie verweist auf
README und INSTALL.

## Lizenz

Der **Code** steht unter **GPL-3.0** (siehe [LICENSE](LICENSE)) — er bleibt offen.
Fremd-Assets (Wörterbücher, Web-Libs) haben eigene Lizenzen, siehe
[THIRD_PARTY.md](THIRD_PARTY.md). Diese werden **nicht** mitgeliefert, sondern von
dir nachgeladen — ihre Lizenzen gelten unabhängig von der Code-Lizenz.

## Inspiration

Der ursprüngliche Anstoß war das [Riko-Projekt](https://github.com/rayenfeng/riko_project)
(MIT). Yuki wurde von Grund auf neu geschrieben; es wurde kein Riko-Code
übernommen — der Dank gilt trotzdem für den Funken.
