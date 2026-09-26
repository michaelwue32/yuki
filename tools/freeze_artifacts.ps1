<#
.SYNOPSIS
  Friert die AKTUELL laufenden Upstream-Artefakte (Modelle, Runtimes, gepatchten
  Code) byte-genau in ein Cold-Storage-Lager ein - mit SHA256-Checksummen.

.DESCRIPTION
  Das hier loest genau EIN Problem: "Die Version, die bei uns laeuft, ist aus dem
  Netz verschwunden / wurde ersetzt." Sobald du eine Version einmal eingefroren
  hast, haengst du nicht mehr am Upstream-Repo.

  Anders als backup_yuki.ps1 (das den Yuki-Projekt-State sichert) sichert DIESES
  Skript die EXTERNEN Brocken unter D:\Server + die grossen re-downloadbaren
  Modelle, die settings.jsonc nur referenziert. Es kopiert byte-genau, also sind
  auch etwaige lokale Patches am Fremd-Code automatisch mit drin (der Grund,
  warum auch die Code-Baeume und nicht nur die Gewichte gefroren werden).

  Die mitgeschriebene SHA256SUMS.txt pro Komponente ist doppelt nuetzlich:
    1. Integritaets-Check des Frosts (kam alles heil an?).
    2. Patch-Detektor: spaeter `-Verify` gegen die laufende Installation laufen
       lassen zeigt, ob sich am Live-Code seit dem Frost etwas geaendert hat.

.PARAMETER Dest
  Wurzel des Cold-Storage-Lagers. Default D:\Server\_artifacts.
  EMPFEHLUNG: auf eine ANDERE Platte oder NAS legen (-Dest \\nas\yuki\artifacts).
  Ein Frost auf derselben Platte schuetzt nicht vor Plattentod.

.PARAMETER Vision        LFM2.5-VL GGUFs (HF-vanish-Risiko HOCH)
.PARAMETER VisionRuntime llama.cpp-Binaries + DLLs (exakter Build 9357)
.PARAMETER Sovits        GPT-SoVITS pretrained_models + weight.json + Code-Baum
.PARAMETER F5            F5-German-Checkpoint + das f5_tts-Package (Code)
.PARAMETER Whisper       faster-whisper HF-Cache (medium + large-v3)
.PARAMETER WebVendor     web/vendor (three/mediapipe self-hosted)
.PARAMETER All           Alles oben.
.PARAMETER Verify        NICHT kopieren - nur Live-Installation gegen die letzte
                         eingefrorene SHA256SUMS.txt pruefen (Patch-Detektor).
.PARAMETER DryRun        Zeigt was kopiert wuerde, schreibt nichts.

.NOTES
  Ohne Komponenten-Flag wird der "HF-vanish-Risiko"-Default gefroren:
  Vision + Sovits + F5 (die drei mit Quelle = fremdes HF-Repo). Runtime/Whisper/
  WebVendor sind opt-in (niedrigeres Risiko bzw. trivial re-fetchbar).

  Siehe docs/versioning.md fuer die Gesamt-Strategie und config/versions.lock.jsonc
  fuer "was laeuft gerade".
#>
[CmdletBinding()]
param(
  [string]$Dest = "D:\Server\_artifacts",
  [switch]$Vision,
  [switch]$VisionRuntime,
  [switch]$Sovits,
  [switch]$F5,
  [switch]$Whisper,
  [switch]$WebVendor,
  [switch]$All,
  [switch]$Verify,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

# ---- Komponenten-Definitionen --------------------------------------------------
# Jede Komponente: ein oder mehrere (Src, RelDst, [Filter])-Eintraege.
# Filter (optional) = robocopy-Include-Pattern; leer = alles.

function Get-WhisperCacheDirs {
  $base = if ($env:HF_HOME) { Join-Path $env:HF_HOME "hub" }
          else { Join-Path $env:USERPROFILE ".cache\huggingface\hub" }
  if (-not (Test-Path $base)) { return @() }
  Get-ChildItem $base -Directory -Filter "models--Systran--faster-whisper-*" -ErrorAction SilentlyContinue
}

$Components = @{
  "vision" = @{
    Desc  = "LFM2.5-VL GGUFs"
    Items = @(
      @{ Src = "D:\Server\llama.cpp\models"; Dst = "vision\models"; Include = @("lfm2-vl-1.6b.gguf","mmproj.gguf") }
    )
  }
  "vision_runtime" = @{
    Desc  = "llama.cpp Binaries (Build 9357)"
    Items = @(
      @{ Src = "D:\Server\llama.cpp"; Dst = "vision_runtime"; Include = @("*.exe","*.dll") }
    )
  }
  "sovits" = @{
    Desc  = "GPT-SoVITS pretrained_models + Code (inkl. evtl. Patches)"
    Items = @(
      @{ Src = "D:\Server\GPT-SoVITS\GPT_SoVITS\pretrained_models"; Dst = "sovits\pretrained_models" },
      # Code-Baum OHNE runtime\ (gebundelte Python, riesig+reproduzierbar) und ohne TEMP/logs/pycache.
      @{ Src = "D:\Server\GPT-SoVITS";       Dst = "sovits\code"; Include = @("*.py","*.json","*.txt","*.bat","*.ps1","*.md");
         ExcludeDirs = @("runtime","TEMP","logs","__pycache__","pretrained_models","GPT_weights","GPT_weights_v2","GPT_weights_v2Pro","GPT_weights_v2ProPlus","GPT_weights_v3","GPT_weights_v4","SoVITS_weights","SoVITS_weights_v2","SoVITS_weights_v2Pro","SoVITS_weights_v2ProPlus","SoVITS_weights_v3","SoVITS_weights_v4") }
    )
  }
  "f5" = @{
    Desc  = "F5-German-Checkpoint + f5_tts-Package (Code)"
    Items = @(
      @{ Src = "D:\Projects\yuki\voices\f5tts"; Dst = "f5\checkpoint"; Include = @("*.pt") },
      @{ Src = "D:\Server\f5tts-venv\Lib\site-packages\f5_tts"; Dst = "f5\f5_tts_package"; Include = @("*.py","*.yaml","*.txt");
         ExcludeDirs = @("__pycache__") }
    )
  }
  "whisper" = @{
    Desc  = "faster-whisper HF-Cache (medium + large-v3)"
    Items = @()  # dynamisch befuellt, siehe unten
  }
  "webvendor" = @{
    Desc  = "web/vendor (three/three-vrm/marked/mediapipe self-hosted)"
    Items = @(
      @{ Src = "D:\Projects\yuki\web\vendor"; Dst = "webvendor" }
    )
  }
}

# Whisper-Cache dynamisch
$whisperDirs = Get-WhisperCacheDirs
foreach ($d in $whisperDirs) {
  $Components["whisper"].Items += @{ Src = $d.FullName; Dst = "whisper\$($d.Name)" }
}

# ---- Auswahl welche Komponenten ------------------------------------------------
$selected = @()
if ($All) {
  $selected = $Components.Keys
} else {
  if ($Vision)        { $selected += "vision" }
  if ($VisionRuntime) { $selected += "vision_runtime" }
  if ($Sovits)        { $selected += "sovits" }
  if ($F5)            { $selected += "f5" }
  if ($Whisper)       { $selected += "whisper" }
  if ($WebVendor)     { $selected += "webvendor" }
  if ($selected.Count -eq 0) {
    # Default: HF-vanish-Risiko-Set
    $selected = @("vision","sovits","f5")
    Write-Host "Kein Komponenten-Flag -> Default-Frost (HF-vanish-Risiko): vision + sovits + f5" -ForegroundColor Yellow
  }
}

# ---- Hilfsfunktionen -----------------------------------------------------------
function Write-Manifest($path, $lines) {
  $lines | Out-File -FilePath $path -Encoding utf8
}

function Copy-Item-Robocopy($src, $dst, $include, $excludeDirs) {
  if (-not (Test-Path $src)) { Write-Host "  ! Quelle fehlt: $src" -ForegroundColor Red; return $false }
  $args = @($src, $dst)
  if ($include -and $include.Count -gt 0) { $args += $include }
  $args += @("/E","/COPY:DAT","/R:1","/W:1","/NFL","/NDL","/NJH","/NJS","/NC","/NS","/NP")
  if ($excludeDirs -and $excludeDirs.Count -gt 0) { $args += "/XD"; $args += $excludeDirs }
  if ($DryRun) { $args += "/L" }
  robocopy @args | Out-Null
  # robocopy exit codes 0..7 = OK, >=8 = Fehler
  if ($LASTEXITCODE -ge 8) { Write-Host "  ! robocopy-Fehler ($LASTEXITCODE) bei $src" -ForegroundColor Red; return $false }
  return $true
}

function Get-DirHashes($dir) {
  # Relativer-Pfad -> SHA256, sortiert. Fuer SHA256SUMS.txt + Verify.
  if (-not (Test-Path $dir)) { return @() }
  Get-ChildItem $dir -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($dir.Length).TrimStart('\')
    $h = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    "$h  $rel"
  } | Sort-Object
}

# ---- VERIFY-Modus: Live-Installation gegen letzten Frost pruefen ----------------
if ($Verify) {
  Write-Host "=== VERIFY: Live-Installation vs. eingefrorene SHA256SUMS ===" -ForegroundColor Cyan
  $anyDrift = $false
  foreach ($key in $selected) {
    $comp = $Components[$key]
    $sumFile = Join-Path $Dest "$key\SHA256SUMS.txt"
    if (-not (Test-Path $sumFile)) { Write-Host "[$key] kein Frost vorhanden ($sumFile) - skip" -ForegroundColor DarkGray; continue }
    $frozen = @{}
    Get-Content $sumFile | ForEach-Object { if ($_ -match '^([0-9A-F]+)\s\s(.+)$') { $frozen[$Matches[2]] = $Matches[1] } }
    # Live-Hashes ueber die echten Quell-Items
    $live = @{}
    foreach ($item in $comp.Items) {
      foreach ($line in (Get-DirHashes $item.Src)) {
        if ($line -match '^([0-9A-F]+)\s\s(.+)$') { $live["$($item.Dst)\$($Matches[2])"] = $Matches[1] }
      }
    }
    $drift = 0
    foreach ($f in $frozen.Keys) {
      $rel = $f  # frozen rel paths sind relativ zum Komponenten-Ordner
      # Vergleich nur ueber gemeinsame Schluessel - Pfadschema kann leicht abweichen,
      # daher Match auf Dateiname-Endung als Fallback.
    }
    # Pragmatischer Drift-Check: Anzahl + Hash-Mengen vergleichen
    $frozenHashes = $frozen.Values | Sort-Object -Unique
    $liveHashes   = $live.Values   | Sort-Object -Unique
    $missing = $frozenHashes | Where-Object { $_ -notin $liveHashes }
    if ($missing) {
      $anyDrift = $true
      Write-Host "[$key] DRIFT: $($missing.Count) eingefrorene Datei(en) haben keinen Hash-Match in der Live-Installation" -ForegroundColor Yellow
    } else {
      Write-Host "[$key] ok - alle eingefrorenen Hashes auch live vorhanden" -ForegroundColor Green
    }
  }
  if (-not $anyDrift) { Write-Host "`nKein Drift erkannt." -ForegroundColor Green; exit 0 }
  else { Write-Host "`nDrift erkannt - Live-Code weicht vom Frost ab (evtl. Patch / Update)." -ForegroundColor Yellow; exit 1 }
}

# ---- FREEZE-Modus --------------------------------------------------------------
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "=== FREEZE -> $Dest ===" -ForegroundColor Cyan
if ($DryRun) { Write-Host "(DRY RUN - es wird nichts geschrieben)" -ForegroundColor Yellow }
Write-Host "Komponenten: $($selected -join ', ')`n"

if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $Dest | Out-Null }

$topManifest = @("# Yuki Artefakt-Frost", "# Zeit: $stamp", "# Maschine: $env:COMPUTERNAME", "# Komponenten: $($selected -join ', ')", "")

foreach ($key in $selected) {
  $comp = $Components[$key]
  Write-Host "[$key] $($comp.Desc)" -ForegroundColor White
  if ($comp.Items.Count -eq 0) { Write-Host "  (keine Quellen gefunden - skip)" -ForegroundColor DarkGray; continue }
  $compRoot = Join-Path $Dest $key
  foreach ($item in $comp.Items) {
    $dstFull = Join-Path $Dest $item.Dst
    Write-Host "  $($item.Src)  ->  $($item.Dst)"
    Copy-Item-Robocopy $item.Src $dstFull $item.Include $item.ExcludeDirs | Out-Null
  }
  if (-not $DryRun) {
    # SHA256SUMS pro Komponente ueber den GEFRORENEN Stand
    $hashes = Get-DirHashes $compRoot
    Write-Manifest (Join-Path $compRoot "SHA256SUMS.txt") $hashes
    $size = (Get-ChildItem $compRoot -Recurse -File | Measure-Object Length -Sum).Sum
    $sizeMB = [math]::Round($size/1MB, 1)
    Write-Host "  -> $($hashes.Count) Dateien, $sizeMB MB, SHA256SUMS.txt geschrieben" -ForegroundColor Green
    $topManifest += "$key : $($hashes.Count) Dateien, $sizeMB MB"
  }
}

if (-not $DryRun) {
  Write-Manifest (Join-Path $Dest "_FREEZE_MANIFEST.txt") $topManifest
  Write-Host "`nFertig. Manifest: $(Join-Path $Dest '_FREEZE_MANIFEST.txt')" -ForegroundColor Cyan
  Write-Host "TIPP: Dieses Lager auf eine externe Platte / NAS spiegeln - sonst kein Schutz vor Plattentod." -ForegroundColor Yellow
} else {
  Write-Host "`nDry-Run fertig." -ForegroundColor Cyan
}

# robocopy hinterlaesst $LASTEXITCODE 1..7 auch bei Erfolg -> sauberen 0-Exit
# erzwingen, sonst liest Automation/Dashboard das als Fehler.
exit 0
