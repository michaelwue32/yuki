# Yuki — Hinweise für Claude Code

Dies ist die **öffentliche Yuki-Codebase** (lokale, cloud-freie DE/JP-Waifu-
Japanisch-Tutorin). Wenn du einem Nutzer beim Aufsetzen hilfst, lies zuerst
`README.md` und `INSTALL.md` — diese Datei ergänzt sie nur um die Reihenfolge.

## Setup-Reihenfolge (Text-Kern zuerst)

1. **Ollama prüfen:** läuft ein Server, ist ein Modell gezogen? (`ollama list`,
   `curl http://localhost:11434/api/tags`). Empfehlung für den Einstieg: ein
   12B-Modell wie `gemma3:12b`.
2. **venv + Deps:** Python 3.14+, `pip install -r requirements.lock`.
3. **Config:** `config/settings.example.jsonc` → `config/settings.jsonc` kopieren,
   `llm.ollama_servers` auf den erreichbaren Ollama + Modell-Tag setzen.
4. **Text-Kern starten:** `python server.py`, dann `https://localhost:8443`
   (self-signed Cert einmal bestätigen). Erst wenn ein Text-Turn zurückkommt,
   weiter zur Kür.
5. **Optionale Dienste nur bei Bedarf:** Web-Assets (`tools/fetch_web_vendor.py`),
   Wörterbücher (`tools/import_wadoku.py`, `tools/import_kanjidic2.py`), Vision
   (llama.cpp + LFM2.5-VL → `docs/setup-vision-lfm2.md`), TTS (`qwen_server.py` →
   `docs/setup-qwen-tts.md`).
   - **Wichtig bei TTS:** eigener Dienst in **separater Python-3.11-venv** (nicht die
     3.14-Core-venv), und die Paket-Versionen **exakt pinnen** (`faster-qwen3-tts` +
     `transformers`) — ungepinnt crasht der Worker. Exakte Pins in
     `docs/setup-qwen-tts.md`; strikt daran halten.
   - **Bei Vision:** llama.cpp-CUDA-Runtime passend zur Treiber-CUDA wählen (nicht
     neuer), Modelle via `hf download`. Details in `docs/setup-vision-lfm2.md`.

## Bring-your-own (absichtlich nicht im Repo)

- **Avatar** (`.vrm` unter `avatar/`, mind. `Yuki_default.vrm`),
- **Animationen** (Mixamo → VRMA, Konverter liegt bei),
- **Stimme** (eigene Referenz-WAV unter `voices/`).

Fehlt eines, degradiert Yuki sauber (Avatar still / nur Text). Blockiere den
Text-Kern nicht daran.

## Nützliche Docs

`docs/setup-qwen-tts.md` (TTS), `docs/setup-vision-lfm2.md` (Vision),
`docs/setup-kanji-data.md` (Kanji-Daten), `docs/stolperfallen.md` (bekannte
Fallen), `docs/cheatsheet.md` (Feature-Übersicht).
