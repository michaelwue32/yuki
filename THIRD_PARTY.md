# Fremd-Komponenten & Lizenzen (Third-Party Notices)

Der **Yuki-Code** steht unter **GPL-3.0** (siehe `LICENSE`). Die folgenden
Fremd-Komponenten haben **eigene** Lizenzen. Sie werden **nicht** in diesem Repo
mitgeliefert, sondern zur Laufzeit von dir nachgeladen (per Skript oder
Download); ihre Lizenzen gelten unabhängig von der Code-Lizenz. Wo Attribution
gefordert ist, nenne die Quelle bei einer Weitergabe.

## Wörterbuch- / Sprachdaten

| Komponente | Lizenz | Quelle | Nutzung in Yuki |
|---|---|---|---|
| **Wadoku** (JP→DE) | CC BY-SA | https://www.wadoku.de | DE-Glossen im JP-Token-Popup (`wadoku.py`, nachgeladen via `tools/import_wadoku.py`) |
| **KANJIDIC2** | Lizenz der EDRDG (CC BY-SA 4.0) | https://www.edrdg.org/wiki/index.php/KANJIDIC_Project | Kanji-Striche/JLPT/Lesungen (`kanjidict.py`, `tools/import_kanjidic2.py`) |
| **KanjiVG** | CC BY-SA 4.0 | https://kanjivg.tagaini.net | Strichordnungs-SVGs im Kanji-Popup |
| **OpenMoji** | CC BY-SA 4.0 | https://openmoji.org | Stempel-Bibliothek der Künstlerin-Persona (`tools/fetch_draw_symbols.py`) |

## Web-Bibliotheken (nachgeladen via `tools/fetch_web_vendor.py`)

| Komponente | Lizenz | Quelle |
|---|---|---|
| **three.js** | MIT | https://github.com/mrdoob/three.js |
| **@pixiv/three-vrm** | MIT | https://github.com/pixiv/three-vrm |
| **marked** | MIT | https://github.com/markedjs/marked |
| **hls.js** | Apache-2.0 | https://github.com/video-dev/hls.js |
| **MediaPipe** (Selfie-Segmentation, optional) | Apache-2.0 | https://github.com/google/mediapipe |

## Modelle / Runtimes (separat zu installieren)

| Komponente | Hinweis |
|---|---|
| **Ollama** + LLM (z.B. Gemma) | Eigene Lizenzen der jeweiligen Modelle beachten (z.B. Gemma Terms of Use). |
| **faster-whisper** (STT) | MIT (Modelle: MIT/Apache je nach Variante). |
| **Qwen3-TTS** (TTS) | Lizenz des Qwen-Modells beachten. |
| **LFM2.5-VL** (Vision) | Lizenz von LiquidAI beachten. |

## Bring-your-own-Medien (bewusst NICHT enthalten)

- **VRoid/VRM-Avatar** — eingebettete VRM-Lizenz des jeweiligen Modells beachten.
- **Mixamo-Animationen** — Adobe-Mixamo-ToS verbietet Weiterverteilung; jeder holt
  sich die Clips selbst (siehe `INSTALL.md`).
- **VOICEVOX / Referenz-Stimmen** — Lizenz pro Charakter beachten; nicht mitgeliefert.

## Inspiration

- **Riko-Projekt** (MIT) — https://github.com/rayenfeng/riko_project — war der
  ursprüngliche Anstoß. Yuki wurde von Grund auf neu geschrieben; es wurde **kein
  Riko-Code übernommen**. Erwähnung als Dank, nicht als Lizenzpflicht.
