# Versionierung & Updates: Testen, Einfrieren, Zurückfallen

Yukis Modelle/Runtimes (TTS, STT, Vision, LLM, Web-Libs) liegen alle in festen
Versionen. Diese Doku ist die Strategie, wie man **sicher updatet** und vor allem
**ohne Netz-Abhängigkeit zurückfällt** — der Fall „die Version ist aus dem
Internet verschwunden" ist real und nicht rückwirkend reparierbar.

Verwandt: `docs/backup.md` (Yuki-State-Backup), `config/versions.lock.jsonc`
(was läuft gerade), die `docs/setup-*.md` (Neu-Aufsetzen pro Komponente).

---

## Grundhaltung

**Aktuell-Sein ist hier kein Wert an sich.** Yuki läuft offline, nur Michael greift
zu, keine Sicherheits-SLA. Ein Update lohnt nur bei konkretem Schmerz („hört sich
besser an", „ist fehlerfreier"). Die beste Update-Strategie ist deshalb meistens:
**nicht updaten, aber die Option besitzen** — die laufende Version einfrieren und
in Ruhe lassen.

Drei Prinzipien:

1. **Besitze deine Artefakte.** Die einzige robuste Antwort auf „nicht mehr im
   Netz" ist, nicht mehr am Netz zu hängen. → `tools/freeze_artifacts.ps1`.
2. **Side-by-Side statt In-Place.** Eine neue Version ersetzt nie die alte am
   gleichen Ort. Wechsel = Config-Flip + Service-Restart, kein Reinstall.
3. **Kein Wechsel ohne Vorher/Nachher-Vergleich.** → `tools/reference_check.py`.

---

## Die vier Update-Typen (unterschiedliches Risiko)

| Typ | Beispiele | Verschwindet aus dem Netz? | Absicherung |
|---|---|---|---|
| **Modell-Gewichte** | SoVITS-Pretrained, large-v3, LFM2-VL-GGUF, F5-ckpt, Ollama-Modelle | **Ja, ständig** (HF-Repos ersetzt/gelöscht) | `freeze_artifacts.ps1` |
| **Runtime/Binaries** | llama.cpp-Build, GPT-SoVITS-Code, ffmpeg | Release-Assets ja, Tags eher nicht | `freeze -VisionRuntime`, Code mit-gefroren |
| **Python-Deps** | faster-whisper, torch+cu124, pyarrow | selten, CUDA-Wheels schon | `requirements.lock` / `f5_requirements.lock` |
| **Web/CDN** | three, three-vrm, marked | unpkg kann 404en | self-hosted `/vendor/` (s.u.) |

Der „Version ist weg"-Schmerz trifft fast nur **Modell-Gewichte** und **CDN**.

---

## Punkt 1 — Artefakte einfrieren (Cold Storage)

`tools/freeze_artifacts.ps1` kopiert die aktuell laufenden Upstream-Brocken
byte-genau in ein Lager + schreibt `SHA256SUMS.txt` pro Komponente. Byte-genau
heißt: etwaige **lokale Patches am Fremd-Code sind automatisch mit drin** (darum
wird auch der Code-Baum von SoVITS und das `f5_tts`-Package gefroren, nicht nur
die Gewichte).

```powershell
# Default (HF-vanish-Risiko: Vision-GGUF + SoVITS-Pretrained+Code + F5-ckpt+Package):
.\tools\freeze_artifacts.ps1 -Dest \\nas\yuki\artifacts

# Alles inkl. llama.cpp-Binaries + Whisper-Cache + web/vendor:
.\tools\freeze_artifacts.ps1 -All -Dest \\nas\yuki\artifacts

# Einzeln: -Vision -VisionRuntime -Sovits -F5 -Whisper -WebVendor
# Trockenlauf (zeigt nur, schreibt nichts):
.\tools\freeze_artifacts.ps1 -DryRun
```

> **Wichtig:** `-Dest` auf eine **andere Platte / NAS** legen. Ein Frost auf
> derselben Platte schützt nicht vor Plattentod.

**Patch-Detektor / Integritäts-Check** — prüft die *laufende* Installation gegen
den letzten Frost (zeigt, ob jemand am Live-Code geschraubt hat oder ob ein
Update durchgelaufen ist):

```powershell
.\tools\freeze_artifacts.ps1 -Verify -Dest \\nas\yuki\artifacts
```

**Pflege-Regel:** *Vor* jedem Update einmal frieren — danach ist die alte Version
weg. `config/versions.lock.jsonc` ist die Landkarte „was läuft gerade + woher
wieder bekommen"; bei jedem Update dort den Eintrag hochziehen.

---

## Punkt 2 — Side-by-Side & Rollback per Config-Flip

Die meisten Pfade sind schon Tunables in `config/settings.jsonc`. Rollback =
Wert zurücksetzen + Restart. Beispiele:

| Komponente | Stellschraube | Rollback |
|---|---|---|
| Whisper-Modell | `settings.jsonc: stt.whisper_model` | Wert zurück + Server-Restart |
| LLM-Modell | `settings.jsonc: llm.ollama_servers` (Tag) | Tag zurück + Restart |
| SoVITS-Ref/URL | `settings.jsonc: tts.*` | Wert zurück; altes Bundle daneben, `:9880` umbiegen |
| F5-Checkpoint | `settings.jsonc: f5_server.ckpt` | auf alte `.pt` zeigen + F5-Restart |
| Vision-GGUF | `serve-lfm2vl.ps1: -m` / `settings.jsonc: vision.url` | altes GGUF parallel auf `:8082`, URL umbiegen |

**Side-by-Side testen** (Vision-Beispiel): neues GGUF nach `models\lfm2-vl-NEU.gguf`,
zweite `llama-server`-Instanz auf `:8082` starten, `settings.jsonc: vision.url`
temporär auf `:8082` → testen → bei Mist eine Zeile zurück. Die alte Instanz lief
die ganze Zeit unangetastet auf `:8081`.

> Die *externen* Versionen (SoVITS-Modellwahl in `GPT-SoVITS/weight.json`, GGUF-
> Pfad in `serve-lfm2vl.ps1`) leben außerhalb von `settings.jsonc`, weil sie von
> den Fremd-Apps kontrolliert werden. Sie stehen in `config/versions.lock.jsonc`.

---

## Punkt 3 — Referenz-Smoke-Gate (Vorher/Nachher)

`tools/reference_check.py` jagt eine fixe Eingabe-Batterie
(`tests/reference/manifest.json`) durch TTS/STT/Vision/LLM und vergleicht gegen
eine einmal abgenommene Baseline. Macht „testen" ehrlich statt Bauchgefühl.

```powershell
# 1) Auf der ALTEN (bekannt-guten) Version Baseline abnehmen:
.\.venv\Scripts\python.exe tools\reference_check.py --capture
# 2) Neue Version daneben hochziehen, dann vergleichen:
.\.venv\Scripts\python.exe tools\reference_check.py
# Filter: --only tts,stt
```

PASS → promoten. DRIFT/FAIL → die abgelegten WAVs/Replies in
`tests/reference/baselines/` anschauen/anhören. Details:
`tests/reference/README.md`.

---

## Punkt 4 — Web-Libs self-hosten (kein CDN-404)

three.js, three-vrm und marked liefen früher von unpkg. Jetzt self-hosted unter
`web/vendor/` (server.py → `/vendor/`), genau wie die MediaPipe-Assets. Damit
läuft der Avatar auch offline und überlebt unpkg-404s.

```powershell
# Nach frischem Clone (web/vendor/ ist .gitignore-d, ~2.7 MB):
.\.venv\Scripts\python.exe tools\fetch_web_vendor.py
```

Versionen sind an **drei** Stellen gepinnt und müssen übereinstimmen:
`tools/fetch_web_vendor.py` (Konstanten), `web/index.html` (`<importmap>`),
`config/versions.lock.jsonc` (`web_vendor`). Bei Update: alle drei hochziehen,
neu fetchen, im Browser **hart neu laden** (Cache; `/vendor/` ist `immutable`).

Das Skript prüft nach dem Download, ob alle relativen Imports der gefetchten
Module lokal auflösbar sind (sonst bricht der Avatar erst zur Laufzeit).

---

## Disaster-Recovery-Reihenfolge (mit Frost)

1. `docs/backup.md` → Yuki-Repo + State zurück.
2. `config/versions.lock.jsonc` lesen → welche Versionen müssen es sein.
3. Wenn Upstream noch da: `docs/setup-*.md` Schritt für Schritt.
4. Wenn Upstream **weg**: aus dem Frost-Lager zurückkopieren (die SHA256SUMS
   bestätigen Integrität), dann `settings.jsonc`/`serve-*.ps1`-Pfade draufzeigen.
5. `tools/fetch_web_vendor.py` + `tools/fetch_mediapipe.py` für `web/vendor/`.
6. `tools/reference_check.py` (gegen die mitgesicherte Baseline) als Abnahme.
