# Vision – Yukis „Augen" (LFM2.5-VL-1.6B via llama.cpp)

Lade diese Datei, wenn du Vision debuggst (Server tot, Timeouts, falsche Captions),
die Webcam tauschst, am Auto-Vision-Gate schraubst oder den Keepsakes-Flow änderst.

Yuki kann durch die Webcam sehen. Architektur wie bei `world_context`: **VLM = Augen**
(Bild → kurze faktische Beschreibung) → als Wahrnehmung in den Verlauf → **qwen3/Persona
= Stimme** reagiert. Logik in `yuki_core.py` (`describe_image`, `look_and_react`), beide
Frontends teilen sie (Memory bleibt geteilt).

## Modell + Runtime

- **Modell:** LFM2.5-VL-1.6B (LiquidAI), GGUF + mmproj. Winzig (~2,2 GB + 0,56 GB),
  schnell (~1–2 s/Bild auf der 3060), kann OCR (las „example" vom Pulli).
- **Runtime: separater llama.cpp-Server, NICHT Ollama.** Grund: Ollama kann diese
  Vision-Arch (Stand 2026-05) nicht finalisieren – `ollama pull hf.co/LiquidAI/
  LFM2.5-VL-1.6B-GGUF` lädt alle Blobs, scheitert aber beim Manifest mit `Error: 400`
  (upstream nur „Model Request", Ollama-Issue #13637). llama.cpp unterstützt die Arch.
- **Server:** `D:\llama.cpp\` (Build b9357, CUDA 12.4). Start: `D:\llama.cpp\serve-lfm2vl.ps1`
  → OpenAI-API auf `127.0.0.1:8081`. Modelle in `D:\llama.cpp\models\` (aus den Ollama-Blobs
  kopiert → unabhängig vom Ollama-Store; die orphan-Blobs dort könnte man prunen).

## Webcam-Capture (Terminal)

ffmpeg/dshow, Gerät „Logitech BRIO". **Stolperfalle:** der erste Frame ist schwarz
(Auto-Belichtung nicht eingeschwungen) → `capture_frame()` lässt `CAM_WARMUP_FRAMES`
Frames warmlaufen (`-update 1`) und behält den letzten. Default **45 (≈ 1,5 s)**;
höher = sicherer bei wenig Licht, niedriger = schnelleres Schauen. Privacy-Blende muss
auf. (Hinweis: `VISION_TIMEOUT` in yuki_core ist NUR die Max-Wartezeit aufs VLM, nicht
die Kamera; die Kamera-Notaus-Grenze ist `subprocess … timeout=20` in capture_frame.)

## Bedienung

- **Terminal:** **V** im Wartezustand → Yuki schaut + kommentiert in Persona; Beschreibung
  bleibt im Verlauf, Folgefragen per Sprache möglich.
- **Handy/Web:** **📷-Button** → Foto (Browser-Kamera/Galerie) → Upload an den `/see`-Endpoint
  (server.py) → selbe `look_and_react`-Logik. UI zeigt zusätzlich „👁 …" (was das VLM
  gesehen hat) + Yukis Antwort + Sprachausgabe.

## „Genauer hinschauen" – agentische Vision-Rückfrage (2026-06-14)

Bricht den image→text-Flaschenhals auf: qwen3 ist text-only, nur das VLM sieht Pixel —
also bekommt Yuki im Bild-Turn ein **VQA-Werkzeug** statt nur die eine Beschreibung.

- **Marker** `[look: FRAGE]` (Yukis bewusste Aktion). Der Hinweis + 2 Few-Shot-Beispiele
  hängen via `VISION_LOOK_HINT` an der Wahrnehmung (Action-Marker brauchen Beispiele,
  sonst vergessen). Frage bewusst auf Englisch → das kleine VLM antwortet darauf am besten.
- **2-Pass-Schleife in `look_and_react`:** Pass 1 (beschreiben, wie bisher) → wenn Yuki
  `[look:Q]` setzt, re-query `describe_image(..., VISION_LOOK_VQA_PROMPT)` (VQA) → Antwort
  zurück an Yuki → Pass 2 = ihre echte Reaktion. Cap `VISION_LOOK_MAX_ROUNDS` (Default 1).
- **Saubere History:** die „ich schau genauer"-Zwischenschritte laufen NUR auf einer
  Arbeitskopie (`work`); in HISTORY/DB landet EIN Turn (Wahrnehmung ohne Hint + finale
  Antwort). Sonst vermüllt der 30-Turn-Kontext. Feature aus → exakt altes Verhalten.
- **Safety-Net:** setzt sie auf der letzten Runde nur nochmal einen Marker (statt zu
  antworten), wäre die Antwort nach dem Strip leer → einmal forcierte Wort-Antwort.
- **Mic-Status:** `/see` reicht `on_look`-Callback rein → `mic_status`-SSE mit
  `target_client_id` → nur das sendende Gerät zeigt „Yuki schaut genauer hin …" (Handler
  läuft VOR dem busy-Check, weil es der eigene laufende Turn ist).
- **Tunables** (Code-Defaults, optional `config/settings.jsonc` `vision`): `look_enabled`,
  `look_max_rounds`, `look_max_tokens`, `look_vqa_prompt`, `look_hint`.

## Autonom (Terminal, `AUTO_VISION`)

Hintergrund-Thread (`auto_vision_loop` in main.py) schaut im **Leerlauf** alle
~`AUTO_VISION_INTERVAL`s (60) durch die BRIO; ein qwen3-Gate (`vision_worth_commenting`)
vergleicht vorige/aktuelle Szene und lässt Yuki NUR bei echter Änderung/Interessantem
von selbst kommentieren (`react_to_sight` – ohne „Kamera"-Sprech), mit
`AUTO_VISION_COOLDOWN` (150s) gegen Gequassel. Heavy-Work im Thread → PTT bleibt
reaktiv; Reaktion+TTS macht der Main-Loop. `_cam_lock` teilt BRIO zwischen V-Taste &
Thread. `_auto["idle"]` = nur in der Warteschleife True.

**Gate-Tuning (wichtig):** das Gate ignoriert bewusst den WORTLAUT (die Vision-LM
formuliert dieselbe Szene jedes Mal anders → naive Textvergleiche feuern ständig) per
Few-Shot + `temperature=0`, und der **erste Blick etabliert nur die Baseline** (kein
Kommentar aus dem Nichts). Triggert nur bei Substanz: Person weg/neu/anders, Objekt
hochgehalten, Haustier, klar andere Aktivität. Getestet 9/10 (2026-05-27).

## Keepsakes – Bild-Album (alle drei Vision-Quellen, 2026-05-28)

Pro Vision-Reaktion (V-Taste, /see, autonom) entscheidet ein qwen3-Gate
(`keepsake_decide`, `temperature=0`, einzeilig SKIP|KEEP+Caption) ob das Bild ein
„Polaroid" wert ist; bei KEEP schreibt `save_keepsake` JPG + Markdown-Sidecar (Datum,
Quelle, was Yuki sah, was sie sagte) nach `keepsakes/`. Aufruf via
`yc.maybe_archive_keepsake(frame, saw, reply, source=..., on_saved=...)` – **fire-and-forget
im Hintergrund-Thread**, blockt also TTS bzw. den /see-Response nicht. Quellen-Tags:
`"V-Taste"`/`"Handy"`/`"autonom"`.

**Bewusst getrennt vom Fakten-Canon:** Keepsakes wandern NIE in Yukis Kontext
(Verlauf/Memory/Facts) – das Album ist nur für Michael, wie Polaroids in einer
Schublade. Damit das auch beim autonomen Modus klappt, hält `_auto["pending_frame"]`
jetzt die JPEG-Bytes parallel zur `pending`-Beschreibung (vorher verloren). Gate-Prompt
konservativ (`_KEEPSAKE_SYS`, „when in doubt: SKIP") gegen Album-Flut.

Config in yuki_core: `KEEPSAKES_ENABLED`/`KEEPSAKES_DIR`/`KEEPSAKES_MAX_CAPTION_WORDS`.

## Config

- yuki_core: `VISION_ENABLED`/`VISION_URL`/`VISION_DESCRIBE_PROMPT`/`VISION_TIMEOUT`
- main.py: `LOOK_KEY`/`CAM_DEVICE`/`CAM_FRAME`/`CAM_WARMUP_FRAMES`/`AUTO_VISION`/
  `AUTO_VISION_INTERVAL`/`AUTO_VISION_COOLDOWN`
