# Setup: Vision-Server (LFM2.5-VL-1.6B via llama.cpp)

Yukis „Augen": Webcam-Frame → LFM2.5-VL gibt 1-2 Saetze Bildbeschreibung →
qwen3-Persona reagiert. Liefert OCR mit (las „example" vom Pulli ab).

**Wichtig:** Wir nutzen `llama.cpp` direkt, NICHT Ollama. Ollama kann diese
Vision-Architektur (Stand 2026-05) nicht finalisieren — `ollama pull` laedt
alle Blobs runter, scheitert dann beim Manifest mit HTTP 400 (Ollama-Issue
#13637, upstream-Status „Model Request").

Vom Crash zur laufenden Pipeline: **etwa 15 Minuten** (~3 GB Downloads).

---

## Was du am Ende hast

```
D:\Server\llama.cpp\
  ├─ llama-server.exe + ~40 weitere .exe/.dll  ← pre-built CUDA-Binaries
  ├─ models\
  │   ├─ lfm2-vl-1.6b.gguf   (2.2 GB)  ← das VLM
  │   └─ mmproj.gguf         (556 MB)  ← Multimodal-Projector (Bild-Encoder)
  └─ serve-lfm2vl.ps1                  ← Start-Skript
```

Smoke-Test am Ende: `curl http://127.0.0.1:8081/health` antwortet, Vision-Pipeline
in `server.py` startet ohne 400-Errors.

---

## Voraussetzungen

- **Windows 10/11** (Binaries fuer Linux/Mac aequivalent von gleichem Release ziehen)
- **NVIDIA-GPU + CUDA 12.x-Treiber** (wir nutzen CUDA 12.4-Binaries; 12.x sollte
  abwaertskompatibel sein)
- **~3 GB** freier Plattenplatz fuer Modelle + Binaries
- **~3 GB VRAM** zur Laufzeit (LFM2.5-VL ist klein, +KV-Cache mit `-c 4096`)

---

## Setup Step-by-Step

### 1) llama.cpp Release-Binaries laden

Nimm ein aktuelles **`bNNNNN`-getaggtes** Release (z. B. `b9357`+). **Achtung:**
`/releases/latest` zeigt evtl. einen Versions-Tag (z. B. `v0.5.0`) **ohne** Windows-
Binaries — die echten Builds hängen an den `bNNNNN`-Tags. Die genaue Nummer ist nicht
heilig; bei „komischen Errors" auf einen bekannten guten Build zurückfallen.

**CUDA-Runtime passend zum Treiber wählen:** Es gibt inzwischen `cuda-12.4`- **und**
`cuda-13.4`-Assets. Nimm das, das **nicht neuer** als deine Treiber-CUDA ist — bei
Treiber-CUDA ≤ 13.1 ist **`cuda-12.4`** die richtige Wahl (13.4-Runtime wäre zu neu).

```powershell
# Release-Index: https://github.com/ggml-org/llama.cpp/releases
# Datei:  llama-b<NNNNN>-bin-win-cuda-12.4-x64.zip
# Plus    cudart-llama-bin-win-cuda-12.4-x64.zip (CUDA-Runtime-DLLs, falls separat)

mkdir D:\Server\llama.cpp   # Pfad frei waehlbar - ueberall konsistent anpassen
# ZIPs nach D:\Server\llama.cpp\ entpacken (Inhalt flach, nicht in Unterordner)
```

Nach dem Entpacken sollten `llama-server.exe`, viele `ggml-*.dll`, `cudart64_12.dll`
direkt im Verzeichnis liegen.

Smoke-Test der Binary:

```powershell
D:\Server\llama.cpp\llama-server.exe --version
```

### 2) Modell-GGUFs ziehen

Repo: **`LiquidAI/LFM2.5-VL-1.6B-GGUF`** auf Hugging Face.
Konkrete URL: https://huggingface.co/LiquidAI/LFM2.5-VL-1.6B-GGUF

```powershell
mkdir D:\Server\llama.cpp\models

# Mit der HF-CLI (Yuki-Haupt-venv hat sie). ACHTUNG: ab huggingface_hub 1.x heisst
# der Befehl `hf download` (nicht mehr `huggingface-cli download`). Der neue CLI wirft
# am Ende evtl. einen kosmetischen click-Traceback TROTZ Erfolg - ignorieren.
D:\Projects\yuki\.venv\Scripts\Activate.ps1
hf download LiquidAI/LFM2.5-VL-1.6B-GGUF `
    LFM2.5-VL-1.6B-Q8_0.gguf mmproj-LFM2.5-VL-1.6B-F16.gguf `
    --local-dir D:\Server\llama.cpp\models
# (Groessen ca.: Modell Q8_0 ~1,2 GB, mmproj F16 ~0,8 GB - variiert je Release.)
```

Dann **umbenennen** auf die Namen, die `serve-lfm2vl.ps1` erwartet:

```powershell
cd D:\Server\llama.cpp\models
Rename-Item LFM2.5-VL-1.6B-Q8_0.gguf lfm2-vl-1.6b.gguf
Rename-Item mmproj-LFM2.5-VL-1.6B-F16.gguf mmproj.gguf
```

> Quantisierung: Q8_0 ist die hochwertigste, die wir hier nutzen — 2.2 GB.
> Q4_K_M waere ~900 MB kleiner, aber bei einem so kleinen Modell (1.6B)
> ist das die falsche Stelle zum Sparen. Wenn das System hier in 5 Jahren
> auf Embedded-Hardware laufen soll, dann Q4_K_M.

### 3) serve-lfm2vl.ps1 anlegen

Liegt als Template im Repo: `tools\serve-lfm2vl.ps1.template`. Beim Restore
einfach kopieren:

```powershell
Copy-Item D:\Projects\yuki\tools\serve-lfm2vl.ps1.template `
          D:\Server\llama.cpp\serve-lfm2vl.ps1
```

Inhalt zur Referenz (falls Repo nicht da ist):

```powershell
# Startet LFM2.5-VL-1.6B ("Yukis Augen") als llama.cpp-Server auf 127.0.0.1:8081.
# Konsole offen lassen (wie GPT-SoVITS). Test: http://127.0.0.1:8081/health
# -c 4096: kleines Context-Fenster, weil eine Bildbeschreibung selten >400 Tokens
# braucht (Bild-Embed ~256 + Prompt ~100 + Antwort ~50). Spart ~2-3 GB KV-Cache
# gegenueber dem Modell-Default (16k/32k) -> Platz fuer XTTS-v2 auf der 3060.
& "D:\Server\llama.cpp\llama-server.exe" `
  -m "D:\Server\llama.cpp\models\lfm2-vl-1.6b.gguf" `
  --mmproj "D:\Server\llama.cpp\models\mmproj.gguf" `
  --host 127.0.0.1 --port 8081 -ngl 99 -c 4096
```

### 4) Starten + Smoke-Test

```powershell
D:\Server\llama.cpp\serve-lfm2vl.ps1
```

Cold-Load ~5-10 s. Dann horcht der Server auf `:8081`. Test:

```powershell
curl http://127.0.0.1:8081/health
# {"status":"ok"}
```

Echter Vision-Test (kleines Test-Bild bereitlegen, z.B. `runtime\_frame_web.jpg`):

```powershell
$img = [Convert]::ToBase64String([IO.File]::ReadAllBytes("D:\Projects\yuki\runtime\_frame_web.jpg"))
$body = @{
  messages = @(@{
    role = "user"
    content = @(
      @{ type = "text"; text = "Describe what you see in one sentence." }
      @{ type = "image_url"; image_url = @{ url = "data:image/jpeg;base64,$img" } }
    )
  })
  max_tokens = 50
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Uri http://127.0.0.1:8081/v1/chat/completions `
    -Method POST -ContentType "application/json" -Body $body |
    ForEach-Object { $_.choices[0].message.content }
```

Sollte 1-2 Saetze Bildbeschreibung liefern.

---

## Was im Backup landen muss

- **`D:\Server\llama.cpp\serve-lfm2vl.ps1`** — Start-Skript, KEIN re-download moeglich
  (haben wir selbst geschrieben). Wir koennten es auch ins Projekt-Repo unter
  `tools/serve-lfm2vl.ps1.template` ziehen — dann ist es automatisch im Git-Backup.
- **`D:\Server\llama.cpp\models\*.gguf`** (optional — re-downloadbar von HF.
  Bei LiquidAI haengt's davon ab ob das Repo mal verschwindet)

**NICHT** im Backup: die llama.cpp-Binaries (`*.exe`, `*.dll`) — sind aus dem
upstream-Release reproduzierbar.

---

## Stolperfallen

| Symptom | Ursache | Loesung |
|---|---|---|
| `Ollama: Error 400` beim Pull | Vision-Arch wird von Ollama nicht finalisiert | Bewusst llama.cpp statt Ollama nutzen. Issue #13637 upstream. |
| Vision-Timeout im Yuki-Server | GPU-Konflikt (typisch VSeeFace) | `nvidia-smi` → Schuldigen beenden. VSeeFace kann die 3060 zu 100% auslasten und LFM2 verhungern. |
| `--mmproj` wird nicht erkannt | Zu alte llama.cpp-Binaries (< Aug 2025) | Neueres `bNNNNN`-Release ziehen (z. B. b9357+) |
| Bild wird nicht decodiert (Format-Error) | image_url-Format nicht OpenAI-konform | `data:image/jpeg;base64,...` als URL, NICHT nur den Base64-String |
| Reply ist generisch („I see a person") trotz reichem Bild | LFM2.5-VL ist klein (1.6B), reagiert nicht auf Details | Yuki kompensiert: VLM-Output ist nur Augen, qwen3 macht die Persona. Bei reproduzierbar duenner Beschreibung: groesseres Modell waere `Qwen2.5-VL-3B-Instruct-GGUF` (~3.4 GB, gleicher llama.cpp-Server) |
| Webcam-Frame schwarz | BRIO-Erster-Frame-Bug | `CAM_WARMUP_FRAMES` (in settings.jsonc: `server.cam_warmup_frames`) hoeher (default 45 = 1.5s) |

Siehe auch `docs/stolperfallen.md` (Stolperfalle 18+).

---

## Architektur-Hinweise

- LFM2.5-VL ist OpenAI-API-kompatibel — gleicher Endpoint-Aufbau wie GPT-4o
  Vision (`/v1/chat/completions` mit `image_url` Content-Part).
- `-c 4096` ist absichtlich klein (statt 16k Default). Bild-Embed ~256 Tokens
  + Prompt ~100 + Antwort ~50 reicht; spart ~2-3 GB KV-Cache und macht VRAM
  fuer SoVITS frei.
- `-ngl 99` = alle Layer auf die GPU (klein genug, passt). Bei VRAM-Druck
  könntest du `-ngl 0` (CPU) fahren; LFM2.5-VL ist auf CPU ~5x langsamer
  aber immer noch unter 10 s pro Bild.
- Modell laeuft autonom — kein Token-Streaming noetig, weil Yuki die ganze
  Bildbeschreibung in einem Stueck haben will (geht dann an qwen3).
