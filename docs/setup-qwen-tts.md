# Setup: Qwen3-TTS (faster-qwen3-tts) neu aufsetzen

**Stand 2026-09-06 (Cutover).** Qwen3-TTS ist **die einzige** TTS-Engine für DE+EN+JA
aus EINER Referenzstimme, mit optionaler instruct-Emotion. Löste beim Cutover den
alten SoVITS+F5-Split ab (es gibt **keinen** `tts.engine`-Umschalter mehr — Qwen ist
der einzige Pfad). Läuft als eigener HTTP-Service `qwen_server.py` auf `:5006` in
einer eigenen venv.

Design/Entscheidungen: `docs/superpowers/specs/2026-09-04-qwen3-tts-integration-design.md`.
Eval/Messwerte: Memory `qwen3-tts-eval`.

## Warum eigene venv

`faster-qwen3-tts` zieht `transformers 5.x` + eigene Torch-Version, die mit Yukis
Hauptumgebung (Whisper/…) kollidiert. Yuki ruft den Service nur via HTTP — kein
Python-Import (wie bei GPT-SoVITS und F5).

## venv + Installation

> **Pfad-Konvention:** Wir nutzen `D:\Server\qwen3-tts\` als Basis. Der Pfad ist frei
> wählbar — passe ihn überall an (auch im Patch-Skript unten via Arg/Env, s.u.). Die
> TTS-venv ist **Python 3.11** (Torch-Wheels) und **getrennt** von der 3.14-Core-venv.

```powershell
# venv (Python 3.11 - NICHT die 3.14-Core-venv; Torch-Wheels brauchen 3.11)
py -3.11 -m venv D:\Server\qwen3-tts\venv
D:\Server\qwen3-tts\venv\Scripts\python.exe -m pip install --upgrade pip

# WICHTIG: Versionen PINNEN. Ohne Pin zieht pip:
#   - faster-qwen3-tts 0.5.2  -> bricht die Patch-Anker (s.u.) + die qwen_server-API
#   - transformers 5.17+      -> entfernt MimiConfig.rope_theta, der Worker crasht in
#                                Endlosschleife (VRAM laedt/entlaedt im Takt).
D:\Server\qwen3-tts\venv\Scripts\python.exe -m pip install "faster-qwen3-tts==0.4.0" "transformers==5.15.1"

# Torch als CUDA-Build passend zum TREIBER. faster-qwen3-tts 0.4.0 zieht torch 2.14.x,
# das es NUR auf dem cu130-Index gibt (nicht cu124). Waehle den cuXXX-Index passend zu
# deiner Treiber-CUDA-Version - hier Treiber-CUDA 13.1 -> cu130 laeuft sauber:
D:\Server\qwen3-tts\venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu130
```

Modell-Gewichte (`Qwen/Qwen3-TTS-12Hz-0.6B-Base`) werden beim ersten Start automatisch
nach `HF_HOME` (`D:\Server\qwen3-tts\hf-cache`) geladen (~1–2 GB).

## Start

```powershell
$env:HF_HOME = "D:\Server\qwen3-tts\hf-cache"
D:\Server\qwen3-tts\venv\Scripts\python.exe D:\Projects\yuki\qwen_server.py
```

Beim Start: Modell laden (~10 s) + CUDAGraph-Warmup (ein Dummy-Render, nimmt beide
Graphen auf). Danach RTF ~0,42 / TTFA ~0,47 s auf der 3060. VRAM-Peak ~3,4–3,8 GB.
Das **Dashboard** (`tools/yuki_dashboard.py`) startet den Service als `qwen`-ServiceConfig
mit den richtigen env-Vars.

**flash-attn ist NICHT nötig** — der Speed kommt aus `torch.cuda.CUDAGraph` (CUDA-only;
auf AMD/ROCm wäre stattdessen flash-attn der Hebel).

## Konfiguration im Core

Qwen ist die **einzige** Engine — es gibt **keinen** `tts.engine`-Schalter (der wurde
mit dem Cutover entfernt). Der Core spricht den Service einfach über die URLs in
`config/settings.jsonc` unter `qwen_tts` an (`url`/`stream_url`/`health`). Ist der
Service nicht erreichbar, degradiert Yuki auf Text-only. Frontend engine-neutral
(24 kHz PCM + `X-Sample-Rate`-Header).

## Endpoints

- `GET /health` → `{"status":"ok","engine":"Qwen3-TTS","model":...}`
- `POST /tts` `{text, language, instruct?}` → WAV (24 kHz mono int16) + `X-Sample-Rate`
- `POST /tts_stream` `{text, language, instruct?}` → rohe int16-PCM-Chunks (nativer Stream)

`language` ∈ `{"German","English","Japanese"}`. `instruct` = englische Stil-/Emotions-
Anweisung (z. B. „Speak in a warm, cheerful and lively way."), optional.

## Referenzstimme

EINE cross-linguale Referenz für alle Sprachen — in `config/settings.jsonc` unter
`qwen_tts.ref_audio` + `qwen_tts.ref_text` (ref_text MUSS wortgenau zum Clip passen).
Kandidaten-A/B via `D:\Server\qwen3-tts\render_ref_ab.py`.

## Emotion (Mood → instruct)

- Phrasen: `config/moods.json` top-level `voice_instruct` (je Mood eine englische Phrase;
  Fallback-Map in `yuki_core._DEFAULT_VOICE_INSTRUCT`). Live-Reload.
- Stärke-Regler: `memory/yuki_voice_runtime.json` `{"multiplier": 0.0..1.0}` (Live-Reload).
  0 = kein Ausdruck (wie Legacy), 0.3 = leicht, 0.6 = normal, 0.9 = deutlich.
- Nur Companion-Personas; `kyoto`/`tutor` + interne Modi bleiben neutral.

## Smoke-Test

```powershell
D:\Projects\yuki\.venv\Scripts\python.exe D:\Projects\yuki\tests\test_qwen_tts.py
```
(skip-if-down; prüft Health + DE/EN/JA + instruct + leeren Text → `tests/outputs/qwen_*.wav`)

## Stabilität: CUDAGraph-Crash-Patch (WICHTIG nach jeder (Neu-)Installation!)

**Problem:** `faster-qwen3-tts` 0.4.0 emittiert im CUDAGraph-Schnellpfad (`fast_generate`)
**intermittierend** eine out-of-range Token-/Codebook-ID → CUDA `index out of bounds`
device-side assert. Der vergiftet den CUDA-Kontext → danach schlägt **jeder** Request fehl
(500), nur ein Prozess-Neustart hilft. Trat bei ~40 % der Story-Absätze auf (nicht
längenabhängig — bis 2859 Zeichen sonst sauber). 0.4.0 ist die neueste Version, kein
Upstream-Fix.

**Fix (zwei Ebenen, beide gebaut):**
1. **Token-Clamp** in `faster_qwen3_tts/generate.py`: die vorhergesagten IDs werden vor
   dem Embedding auf den gültigen Bereich geklemmt → garbage-ID wird zu gültiger ID
   (winziger Audio-Glitch statt Crash), CUDAGraph-Speed bleibt. Verifiziert: 24/24 der
   vorher-crashenden Absätze sauber. **Dieser Patch sitzt in der venv und geht bei einer
   Neuinstallation von faster-qwen3-tts VERLOREN** → danach unbedingt neu anwenden:
   ```powershell
   # Default-Ziel ist D:\Server\qwen3-tts\venv. Liegt deine venv woanders, gib sie als
   # Argument mit (oder setze $env:QWEN_TTS_VENV) - sonst bricht das Skript mit klarer
   # Fehlermeldung ab (es patcht NIE die falsche Datei):
   D:\Server\qwen3-tts\venv\Scripts\python.exe D:\Projects\yuki\tools\patch_qwen_cudagraph.py D:\Server\qwen3-tts\venv
   ```
   (idempotent; Original-Backup `generate.py.bak-yuki`). Danach Qwen-Service neu starten.
2. **Supervisor-Auto-Restart** (in `qwen_server.py`, s.o.): fängt jeden Rest-Crash ab
   (Worker stirbt → Neustart in ~8 s). Input-Log `runtime/qwen_inputs.log` protokolliert
   jeden Request vor dem Rendern (Crash-Auslöser nachvollziehbar).

## Stand

Cutover erfolgt (2026-09-06): SoVITS/F5 sind raus, Qwen ist die einzige Engine. Die
Referenz-Wahl (A/B) + Mood→instruct-Emotion sind live. Bei Änderung der
`faster-qwen3-tts`-Version die Patch-Anker in `tools/patch_qwen_cudagraph.py` prüfen.
