# Yuki aufsetzen (INSTALL)

Diese Anleitung bringt Yuki von null zum Laufen. Der **Text-Kern** braucht nur
Python + Ollama; alles andere (Stimme, Sehen, Avatar, Wörterbücher) ist optional
und „bring your own". Reihenfolge grob: erst Text-Kern zum Laufen bringen, dann
die Kür nachziehen.

## 1. Voraussetzungen

- **Python 3.14+** (bewusst frisch — ggf. eine Hürde). Prüfen: `py -3.14 --version`.
- **[Ollama](https://ollama.com)** installiert und ein Modell gezogen, z.B.:
  ```bash
  ollama pull gemma3:12b
  ```
- **Windows** wird primär unterstützt (CUDA-DLL-Pfade, PowerShell-Bootstrap).
  Linux ist machbar (Kamera-Pfad dshow→v4l2 anpassen), aber nicht der Fokus.
- **GPU** ist nur für die optionalen Dienste (TTS/STT/Vision) nötig, nicht für
  den Text-Kern.

## 2. Projekt aufsetzen

```bash
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows;  Linux/macOS: source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.lock       # volle Reproduzierbarkeit
```

Config anlegen und die Ollama-URL/das Modell eintragen:

```bash
cp config/settings.example.jsonc config/settings.jsonc
```

In `config/settings.jsonc` unter `llm.ollama_servers` deinen Ollama-Host und den
Modell-Tag setzen (Default ist bereits `http://localhost:11434`). Secret-Configs
(Kalender/HA/Kamera/Push) sind optional — nur bei Bedarf aus den
`*.template.json` kopieren.

## 3. Web-Assets holen (nachladbar)

Die JS-Libraries werden nicht mitgeliefert, sondern lokal nachgeladen:

```bash
python tools/fetch_web_vendor.py       # three / three-vrm / marked / hls
python tools/fetch_mediapipe.py        # optional: Kamera-Hintergrund-Nebel
```

## 4. Wörterbücher (optional, für JP-Popups/Kanji)

```bash
python tools/import_wadoku.py          # Wadoku (DE-Glossen)
python tools/import_kanjidic2.py       # KANJIDIC2 (Striche/JLPT/Lesungen)
python tools/fetch_draw_symbols.py     # optional: Stempel-Bibliothek (Künstlerin)
```

KanjiVG-Strichdaten: siehe `docs/setup-kanji-data.md`.

## 5. Avatar (bring your own)

Yuki erwartet mindestens ein VRM-Modell unter `avatar/`:

- Lege eine eigene `.vrm` ab (z.B. mit [VRoid Studio](https://vroid.com/) erstellt
  oder ein frei lizenziertes Modell).
- Outfit-Varianten folgen der Namenskonvention `Yuki_<name>.vrm`
  (z.B. `Yuki_default.vrm`). Mindestens `Yuki_default.vrm` sollte existieren.
- Prüfe die eingebettete VRM-Lizenz deines Modells, wenn du es weitergibst.

## 6. Animationen (Mixamo → VRMA)

Die Bewegungs-Clips stammen aus [Mixamo](https://www.mixamo.com) und dürfen nicht
weiterverteilt werden — hol sie dir selbst:

1. Bei Mixamo mit eigenem (kostenlosem) Account die gewünschten Clips als **FBX**
   herunterladen.
2. **Konvertierung FBX → VRMA** über Blender + VRM-Addon +
   `avatar/animations_source/convert_mixamo_to_vrma.py`.
   - ⚠ **Stolperfalle:** Die Scene-Frame-Range in Blender **explizit** setzen,
     sonst werden alle Clips exakt 8,33 s lang (Blender-Default 1..250 @ 30 fps).
3. Ergebnis nach `avatar/animations/vrma/<bucket>/…` legen (Buckets: `idle`,
   `listening`, `reaktive_gesten`, `stimmungs_idles`, `persona-spezifisch`, …).

Ohne Animationen steht der Avatar still — der Rest funktioniert.

## 7. Stimme / TTS (bring your own)

- Der `voices/`-Ordner ist **nicht im Repo** (gitignored) — in einem frischen Klon
  ist er leer. Lege ihn an und stell deine Referenz-WAVs hinein.
- Nimm eine **eigene 3–10 s Referenz-WAV** auf und trag den Pfad in
  `config/settings.jsonc` unter `qwen_tts.ref_audio` (EN/JA) bzw. `ref_audio_de`
  (Deutsch) ein. Die **Dateinamen sind frei** — sie müssen nur zu deinen Pfaden in
  `settings.jsonc` passen (die Beispiel-Config nutzt `voices/ref.wav` +
  `voices/ref_de.wav`).
- Der `ref_text` / `ref_text_de` muss **wortgenau** zum jeweiligen Clip passen.
- **Eigene Python-Umgebung:** Der TTS-Dienst läuft in einer **separaten venv mit
  Python 3.11** (Torch-Wheels) — getrennt von der 3.14-Core-venv. Dabei die
  Paket-Versionen **pinnen** (sonst crasht der Worker); alle Details + exakte Pins
  in `docs/setup-qwen-tts.md`.
- **Ohne TTS-Dienst läuft Yuki als reiner Text-Chat** — völlig okay zum Starten.

## 8. Spracherkennung (STT / faster-whisper)

`faster-whisper` wird über `requirements.lock` (Schritt 2) mitinstalliert. Es gibt
zwei Betriebsarten:

- **Standard (Einzel-Box): in-process.** Bei leerem `stt.remote_url` in
  `config/settings.jsonc` lädt der Server das Whisper-Modell selbst — kein
  separater Dienst nötig.
- **Optional (Split): eigener GPU-Dienst.** Läuft dein Kern auf einer Box ohne
  brauchbare GPU, kannst du `stt_server.py` auf einer GPU-Box starten
  (`python stt_server.py` → `:5007`) und `stt.remote_url` des Kerns darauf zeigen
  lassen (z.B. `http://<gpu-box>:5007/stt`). Auf der GPU-Box selbst bleibt
  `remote_url` leer (sonst ruft der Dienst sich rekursiv auf).

Weitere Hinweise:

- **GPU (empfohlen):** Die CUDA-Runtime-DLLs kommen über `nvidia-cublas-cu12` /
  `nvidia-cudnn-cu12` mit (nicht Torch — ctranslate2 ist ein eigenes Backend). Die
  DLL-Pfade müssen **vor** dem Import gesetzt sein (`docs/stolperfallen.md` #1 +
  `docs/code-snippets.md`).
- **Ohne GPU:** automatischer Fallback auf CPU (langsamer, läuft aber).
- Modell wählbar unter `stt.whisper_model` (`large-v3` = beste Qualität;
  `small`/`base`/`tiny` = weniger VRAM). Beim ersten Lauf wird es von Hugging Face
  geladen.

Ohne funktionierendes STT tippst du einfach statt zu sprechen — der Rest läuft.

## 9. Starten & Zertifikat

```bash
python server.py
```

- Beim ersten Start wird ein **self-signed HTTPS-Zertifikat** erzeugt.
- Browser: `https://localhost:8443` öffnen, einmal „Erweitert → fortfahren".
- Erwartung: Ohne TTS/Vision/Whisper ist es ein **Text-Chat** — genau so gedacht.

## 10. Troubleshooting

- **Erster Turn crasht / keine Antwort** → Ollama nicht erreichbar oder falsche
  URL/Modell in `config/settings.jsonc` (`llm.ollama_servers`). Prüfen:
  `curl http://localhost:11434/api/tags`.
- **Kleine Modelle liefern Müll** → `<12B`-Modelle brauchen genug Kontext; ein
  12B-Modell (z.B. `gemma3:12b`) ist der empfohlene Einstieg.
- Weitere bekannte Fallen: `docs/stolperfallen.md`.
