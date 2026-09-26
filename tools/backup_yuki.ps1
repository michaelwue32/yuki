# =============================================================================
# backup_yuki.ps1 - Yuki-Backup als ZIP
# =============================================================================
# Packt die unverzichtbaren Teile von Yuki in eine ZIP-Datei mit Zeitstempel.
# Was rein/raus gehoert: docs\backup.md ist die Quelle der Wahrheit.
#
# Aufruf:
#   .\tools\backup_yuki.ps1                         # Standard, ~300 MB
#   .\tools\backup_yuki.ps1 -IncludeF5Model         # zusaetzlich F5-DE-Modell (+1.3 GB)
#   .\tools\backup_yuki.ps1 -IncludeWadoku          # zusaetzlich wadoku.sqlite (+100 MB)
#   .\tools\backup_yuki.ps1 -IncludeAvatarSource    # VRoid-Sources + Mixamo-FBX (+390 MB)
#   .\tools\backup_yuki.ps1 -All                    # alles oben drauf
#   .\tools\backup_yuki.ps1 -OutputDir D:\Backup    # eigenes Ziel
#   .\tools\backup_yuki.ps1 -DryRun                 # nur listen, nicht packen
#
# Standard-Ablageort:  ..\yuki_backup\   (also EINE Ebene ueber dem Repo)
#   - bewusst ausserhalb des Repos, damit alte Backups beim naechsten Lauf
#     nicht versehentlich im neuen ZIP landen
#
# Was IMMER rein geht (Defaults):
#   - Code (.py, .ps1, .md, .toml, .lock, .json, .jsonc) im Repo-Root
#   - config/  (alles - inkl. yuki_calendar.json mit CalDAV-Credentials!)
#   - memory/  (Yukis Gedaechtnis - nicht re-erzeugbar)
#     SQLite-Files (yuki_habits.sqlite, yuki_history.sqlite) werden NICHT direkt
#     kopiert, sondern via sqlite3.Connection.backup() konsistent ge-snapshottet -
#     funktioniert auch wenn Yuki-Server oder HeidiSQL eine offene Connection
#     halten. -wal/-shm Sidecars werden uebersprungen (Inhalt im Snapshot drin).
#   - archive/ (Session-Volltranskripte + Facts-Backups)
#   - keepsakes/ (Bild-Album)
#   - drawings/ (Yukis Doodles) + gedankenbilder/ (KI-Traumbilder)
#   - voices/   (TTS-Referenzen inkl. voices/f5tts/ref_de_yuki.wav; nur das
#                schwere F5-Modell .pt unter f5tts/ ist gated, siehe unten)
#   - avatar/*.vrm, *.vroid, animations/, backgrounds/  (kein source/, kein animations_source/)
#   - data/wadoku-xml-*.tar.xz  (das kleine Original-Archiv, kein entpacktes Dir)
#   - tools/, tests/, web/, docs/  (Code-Verzeichnisse)
#   - certs/  (self-signed, eigentlich re-erzeugbar - mitgenommen weil winzig)
#
# Was NIE rein geht:
#   - .venv/                                  (~2.7 GB, via requirements.lock reproduzierbar)
#   - __pycache__/                            (Python-Bytecode)
#   - runtime/                                (Debug-Spuren)
#   - tests/outputs/                          (WAV-Test-Renders)
#   - data/wadoku-xml-*/  (entpacktes Dir)    (215 MB, re-erzeugbar aus dem .tar.xz)
#   - data/wadoku.sqlite  (außer mit -IncludeWadoku)  (re-erzeugbar via import_wadoku.py)
#   - voices/f5tts/*.pt   (nur das DE-MODELL, außer mit -IncludeF5Model; re-
#                          downloadbar von HF. ref_de_yuki.wav dort geht IMMER mit)
#   - avatar/source/                          (außer mit -IncludeAvatarSource)
#   - avatar/animations_source/               (außer mit -IncludeAvatarSource)
# =============================================================================

[CmdletBinding()]
param(
    [string]$OutputDir = "..\yuki_backup",
    [switch]$IncludeF5Model,
    [switch]$IncludeWadoku,
    [switch]$IncludeAvatarSource,
    [switch]$All,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# -All ist ein Shortcut fuer alle drei Include-Schalter
if ($All) {
    $IncludeF5Model      = $true
    $IncludeWadoku       = $true
    $IncludeAvatarSource = $true
}

# ---------------------------------------------------------------------------
# Pfade und Output-Datei
# ---------------------------------------------------------------------------
# Wir starten aus dem Repo-Root oder aus tools/ heraus. Wir ankern relativ
# zu __file__ (Skript-Pfad), damit ".\tools\backup_yuki.ps1" UND ein direkter
# Aufruf aus dem tools-Verzeichnis funktionieren.
$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = Split-Path -Parent $ToolsDir
$RepoName = Split-Path -Leaf $RepoRoot

# OutputDir relativ zum Repo-Root aufloesen
if (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = Join-Path $RepoRoot $OutputDir
}
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)

$Stamp   = Get-Date -Format "yyyy-MM-dd_HHmmss"
$ZipPath = Join-Path $OutputDir "yuki_$Stamp.zip"

Write-Host ""
Write-Host "Repo-Root  : $RepoRoot" -ForegroundColor Cyan
Write-Host "Output-Dir : $OutputDir" -ForegroundColor Cyan
Write-Host "Ziel-ZIP   : $ZipPath" -ForegroundColor Cyan
Write-Host "Flags      : F5-Modell=$IncludeF5Model | Wadoku=$IncludeWadoku | Avatar-Source=$IncludeAvatarSource"
Write-Host ""

# ---------------------------------------------------------------------------
# Sammeln, was rein soll
# ---------------------------------------------------------------------------
# Wir bauen eine flache Liste absoluter Pfade zu Dateien (kein Recurse-Trick),
# dann packt der Loop weiter unten relativ zum Repo-Root. So muessen wir das
# Pfad-Layout im ZIP nicht aus Compress-Archive-Args raten.

$FileList = New-Object System.Collections.Generic.List[string]
$Skipped  = New-Object System.Collections.Generic.List[string]

# Map: physischer Pfad (FileList-Eintrag) -> Pfad-im-ZIP unter $RepoName/.
# Standard ist "strip RepoRoot"; fuer SQLite-Snapshots (live im Temp) wird
# das hier auf den Original-Relpfad gesetzt, damit der Snapshot beim
# Entpacken am richtigen Ort landet.
$ArcPathOverride = @{}

# Top-Level-Dateien: alles wichtige nach Extension
$RootGlob = @("*.py", "*.ps1", "*.md", "*.toml", "*.lock", "*.json", "*.jsonc", "*.txt")
# WICHTIG: -Include greift nur mit Wildcard-Leaf im -Path (oder -Recurse). Ohne das
# ".\*" matchte -Include NICHTS -> alle Root-Code-Dateien (server.py, yuki_core.py,
# requirements.lock, ...) fielen still aus JEDEM Backup raus (Bug bis 2026-09-14).
Get-ChildItem -Path (Join-Path $RepoRoot '*') -File -Include $RootGlob -ErrorAction SilentlyContinue |
    ForEach-Object { $FileList.Add($_.FullName) | Out-Null }

# Verzeichnisse, die KOMPLETT rein gehen (rekursiv, ohne Filter)
$AlwaysDirs = @(
    "config",
    "memory",
    "archive",
    "keepsakes",
    "drawings",         # Yukis Doodles (2026-09-10 nachgetragen - fehlte, war out-of-backup)
    "gedankenbilder",   # KI-Traumbilder  (2026-09-10 nachgetragen - fehlte, war out-of-backup)
    "tools",
    "tests",
    "web",
    "docs",
    "certs"
)

foreach ($d in $AlwaysDirs) {
    $full = Join-Path $RepoRoot $d
    if (Test-Path $full) {
        Get-ChildItem -Path $full -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object {
                $_.FullName -notmatch '\\__pycache__\\' -and
                $_.FullName -notmatch '\\tests\\outputs\\'
            } |
            ForEach-Object { $FileList.Add($_.FullName) | Out-Null }
    }
}

# voices/  - alles. Unter voices\f5tts\ wird nur das SCHWERE Modell (.pt & Co,
# ~1.3 GB, re-downloadbar von HF) hinter -IncludeF5Model gehalten. Kleine Dateien
# dort (v.a. ref_de_yuki.wav - die handverlesene DE-Self-Reference, NICHT
# re-downloadbar, gitignored) gehen IMMER mit ins Backup. Vorher fiel die
# Referenz durch die pfad-basierte Regel mit raus (2026-06-18 gefixt).
$F5ModelExts = @('.pt', '.bin', '.safetensors', '.gguf', '.ckpt', '.pth')
$voicesDir = Join-Path $RepoRoot "voices"
if (Test-Path $voicesDir) {
    Get-ChildItem -Path $voicesDir -File -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object {
            $isF5Model = ($_.FullName -match '\\voices\\f5tts\\') -and
                         ($F5ModelExts -contains $_.Extension.ToLowerInvariant())
            if ($isF5Model -and -not $IncludeF5Model) {
                $Skipped.Add($_.FullName) | Out-Null
            } else {
                $FileList.Add($_.FullName) | Out-Null
            }
        }
}

# avatar/  - VRMs, VRoid, animations/, backgrounds/.
#   source/ und animations_source/ NUR mit -IncludeAvatarSource.
$avatarDir = Join-Path $RepoRoot "avatar"
if (Test-Path $avatarDir) {
    Get-ChildItem -Path $avatarDir -File -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object {
            $isHeavy = $_.FullName -match '\\avatar\\(source|animations_source)\\'
            if ($isHeavy -and -not $IncludeAvatarSource) {
                $Skipped.Add($_.FullName) | Out-Null
            } else {
                $FileList.Add($_.FullName) | Out-Null
            }
        }
}

# data/  - das .tar.xz immer rein (klein, originaler XML-Dump),
#   die entpackten Verzeichnisse NIE (riesig, re-erzeugbar),
#   wadoku.sqlite nur mit -IncludeWadoku.
$dataDir = Join-Path $RepoRoot "data"
if (Test-Path $dataDir) {
    Get-ChildItem -Path $dataDir -File -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object {
            $rel = $_.FullName.Substring($RepoRoot.Length).TrimStart('\')
            $isXmlDump  = $rel -match '^data\\wadoku-xml-[^\\]+\\'  # entpackter Ordner
            $isSqlite   = $_.Name -eq 'wadoku.sqlite'
            $isOriginal = $_.Name -match '\.tar\.xz$'

            if ($isXmlDump) {
                $Skipped.Add($_.FullName) | Out-Null
            } elseif ($isSqlite -and -not $IncludeWadoku) {
                $Skipped.Add($_.FullName) | Out-Null
            } elseif ($isSqlite -or $isOriginal) {
                $FileList.Add($_.FullName) | Out-Null
            } else {
                # alles andere in data/ root (selten was) mitnehmen
                $FileList.Add($_.FullName) | Out-Null
            }
        }
}

# ---------------------------------------------------------------------------
# SQLite-Snapshots
# ---------------------------------------------------------------------------
# SQLite-Dateien koennen (a) durch den Yuki-Server selbst, (b) durch HeidiSQL,
# (c) durch andere Tools mit offener Connection gelockt sein. Compress-Archive
# wuerde dann scheitern ODER bei WAL-Mode mitten in einer Transaktion einen
# inkonsistenten Snapshot ziehen.
#
# Saubere Loesung: SQLite Online-Backup-API. sqlite3.Connection.backup()
# kopiert Page-fuer-Page mit Reader-Lock - andere Connections koennen
# parallel schreiben, der Snapshot bleibt konsistent. -wal/-shm Sidecars
# werden dabei in den Snapshot reingeflusht und sind im Backup ueberfluessig.
#
# Wir nutzen die venv-Python, weil sqlite3.exe nicht garantiert installiert
# ist und Python sowieso schon in .venv liegt (Stand 2026-06-04: Py 3.14.5).
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SnapshotDir = Join-Path $env:TEMP "yuki_sqlite_snap_$Stamp"

$sqliteEntries = @($FileList | Where-Object { $_ -match '\.(sqlite|db)$' })
$sidecarEntries = @($FileList | Where-Object { $_ -match '\.(sqlite|db)(-wal|-shm|-journal)$' })

if ($sqliteEntries.Count -gt 0) {
    if (-not (Test-Path $VenvPython)) {
        Write-Warning "venv-Python nicht gefunden: $VenvPython"
        Write-Warning "SQLite-Files werden direkt kopiert - Backup kann inkonsistent sein wenn DB gerade offen ist."
    } else {
        New-Item -Path $SnapshotDir -ItemType Directory -Force | Out-Null

        # Sidecars (-wal/-shm/-journal) raus aus FileList: deren Inhalt landet
        # ohnehin im Snapshot der Haupt-DB, separate Sicherung waere redundant
        # und potentiell widerspruechlich (verschiedene Timestamps).
        foreach ($side in $sidecarEntries) {
            $FileList.Remove($side) | Out-Null
            $Skipped.Add($side) | Out-Null
        }

        $snapCount = 0
        foreach ($src in $sqliteEntries) {
            $snapFile = Join-Path $SnapshotDir ([IO.Path]::GetFileName($src))
            # Python-Einzeiler: src lesend oeffnen, in dst-Tempfile snapshotten.
            # WAL-Sidecars werden automatisch reingemerged.
            $pyCode = @'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
try:
    with dst:
        src.backup(dst)
finally:
    src.close()
    dst.close()
'@
            try {
                & $VenvPython -c $pyCode $src $snapFile 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0 -and (Test-Path $snapFile)) {
                    # Original-Relpfad merken, damit der Snapshot beim Packen
                    # unter dem richtigen Namen ins ZIP wandert.
                    $origRel = $src.Substring($RepoRoot.Length).TrimStart('\')
                    $ArcPathOverride[$snapFile] = $origRel
                    # Original aus FileList raus, Snapshot rein
                    $idx = $FileList.IndexOf($src)
                    if ($idx -ge 0) { $FileList[$idx] = $snapFile }
                    $snapCount++
                } else {
                    Write-Warning "SQLite-Snapshot fehlgeschlagen ($src) - Datei wird direkt mitgenommen (eventuell inkonsistent)"
                }
            } catch {
                Write-Warning "SQLite-Snapshot Exception ($src): $_"
            }
        }
        Write-Host "SQLite-Snapshots: $snapCount / $($sqliteEntries.Count)  (Sidecars uebersprungen: $($sidecarEntries.Count))" -ForegroundColor DarkCyan
    }
}

# ---------------------------------------------------------------------------
# Statistik + Sicherheits-Warnung
# ---------------------------------------------------------------------------
$totalBytes = 0
$FileList | ForEach-Object {
    $totalBytes += (Get-Item $_ -ErrorAction SilentlyContinue).Length
}
$totalMB = [math]::Round($totalBytes / 1MB, 1)

$skippedBytes = 0
$Skipped | ForEach-Object {
    $skippedBytes += (Get-Item $_ -ErrorAction SilentlyContinue).Length
}
$skippedMB = [math]::Round($skippedBytes / 1MB, 1)

Write-Host "Dateien zu packen: $($FileList.Count)  ($totalMB MB unkomprimiert)"
Write-Host "Dateien skipped : $($Skipped.Count)  ($skippedMB MB, mit -All inkludieren)"
Write-Host ""

# CalDAV-Credentials-Warnung
$secret = Join-Path $RepoRoot "config\yuki_calendar.json"
if (Test-Path $secret) {
    Write-Host "Hinweis: config\yuki_calendar.json enthaelt CalDAV-Klartext-Credentials." -ForegroundColor Yellow
    Write-Host "         Backup-ZIP entsprechend privat halten (nicht in oeffentliche Cloud)." -ForegroundColor Yellow
    Write-Host ""
}

if ($DryRun) {
    Write-Host "[DryRun] kein ZIP geschrieben." -ForegroundColor Magenta
    Write-Host "Erste 20 Pfade die gepackt wuerden:" -ForegroundColor DarkGray
    $FileList | Select-Object -First 20 | ForEach-Object {
        $shown = if ($ArcPathOverride.ContainsKey($_)) { $ArcPathOverride[$_] + "  [SQLite-Snapshot]" }
                 else { $_.Substring($RepoRoot.Length).TrimStart('\') }
        Write-Host "  $shown" -ForegroundColor DarkGray
    }
    if ($FileList.Count -gt 20) { Write-Host "  ... +$($FileList.Count - 20) weitere" -ForegroundColor DarkGray }
    # SnapshotDir auch im DryRun aufraeumen (Snapshot wurde echt erzeugt um Lock-Verhalten zu testen)
    if ($SnapshotDir -and (Test-Path $SnapshotDir)) {
        Remove-Item $SnapshotDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    return
}

# ---------------------------------------------------------------------------
# Manifest schreiben (landet als _BACKUP_MANIFEST.txt im ZIP)
# ---------------------------------------------------------------------------
$manifest = @"
Yuki Backup Manifest
====================
Erstellt    : $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Maschine    : $env:COMPUTERNAME ($env:USERNAME)
Repo-Pfad   : $RepoRoot
Optionen    : F5-Modell=$IncludeF5Model | Wadoku=$IncludeWadoku | Avatar-Source=$IncludeAvatarSource

Dateien     : $($FileList.Count)
Groesse roh : $totalMB MB
Skipped     : $($Skipped.Count)  ($skippedMB MB)

Wiederherstellen
----------------
1) Diese ZIP nach <neuer-pfad>\yuki entpacken
2) Nach 'docs\backup.md' vorgehen (Disaster-Recovery-Reihenfolge)
3) Setup der externen Dienste: 'docs\setup-*.md'
4) 'pip install -r requirements.lock' fuer die Haupt-venv
"@

$tempManifest = Join-Path $env:TEMP "_BACKUP_MANIFEST_$Stamp.txt"
$manifest | Set-Content -Path $tempManifest -Encoding utf8

# ---------------------------------------------------------------------------
# Packen
# ---------------------------------------------------------------------------
if (-not (Test-Path $OutputDir)) {
    New-Item -Path $OutputDir -ItemType Directory -Force | Out-Null
}

# Compress-Archive auf einer Liste >> 1000 Dateien wird PROGRESSIV langsamer
# (interne O(n^2)-Implementierung). Daher direkt .NET ZipArchive nutzen -
# spuerbar schneller bei vielen kleinen Dateien (Yuki: ~2000-5000 Files).
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$startMs = (Get-Date).Ticks
$stream  = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::Create)
$zip     = New-Object System.IO.Compression.ZipArchive($stream, [System.IO.Compression.ZipArchiveMode]::Create)

try {
    # Manifest zuerst (steht oben in der ZIP-Liste, beim Doppelklick zuerst sichtbar)
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, $tempManifest, "_BACKUP_MANIFEST.txt",
        [System.IO.Compression.CompressionLevel]::Optimal
    ) | Out-Null

    $i = 0
    foreach ($file in $FileList) {
        $i++
        # Relativen Pfad ermitteln: alles UNTER dem Repo-Root, unter einem
        # 'yuki/'-Praefix - so entpackt sich das ZIP in einen 'yuki'-Ordner
        # und ueberschreibt nicht versehentlich was anderes.
        # SQLite-Snapshots liegen physisch im $env:TEMP, sollen aber unter
        # ihrem ORIGINAL-Relpfad ins ZIP -> Override-Map konsultieren.
        if ($ArcPathOverride.ContainsKey($file)) {
            $rel = $ArcPathOverride[$file]
        } else {
            $rel = $file.Substring($RepoRoot.Length).TrimStart('\')
        }
        $inZip  = "$RepoName/$($rel -replace '\\', '/')"

        # Komprimierung fein-graduieren: bereits-komprimierte Formate auf
        # NoCompression - VRM, WAV, PT, SQLITE, XZ, JPG sind binaer und
        # ZIP wuerde sie nur kopieren mit Overhead.
        $ext = [System.IO.Path]::GetExtension($file).ToLowerInvariant()
        $level = if ($ext -in @('.vrm', '.vroid', '.wav', '.mp3', '.flac', '.ogg',
                                '.pt', '.bin', '.gguf', '.safetensors',
                                '.sqlite', '.db',
                                '.xz', '.gz', '.bz2', '.zip', '.7z',
                                '.jpg', '.jpeg', '.png', '.webp')) {
            [System.IO.Compression.CompressionLevel]::NoCompression
        } else {
            [System.IO.Compression.CompressionLevel]::Optimal
        }

        try {
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $file, $inZip, $level
            ) | Out-Null
        } catch {
            Write-Warning "Skip (Fehler beim Lesen): $file - $_"
        }

        if ($i % 500 -eq 0 -or $i -eq $FileList.Count) {
            Write-Host "  $i / $($FileList.Count) ..."
        }
    }
} finally {
    $zip.Dispose()
    $stream.Dispose()
}

Remove-Item $tempManifest -ErrorAction SilentlyContinue
# SQLite-Snapshot-Tempdir aufraeumen (egal ob Pack erfolgreich oder nicht)
if ($SnapshotDir -and (Test-Path $SnapshotDir)) {
    Remove-Item $SnapshotDir -Recurse -Force -ErrorAction SilentlyContinue
}

$durationS = [math]::Round(((Get-Date).Ticks - $startMs) / 10000000, 1)
$zipMB     = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "Fertig!" -ForegroundColor Green
Write-Host "ZIP        : $ZipPath" -ForegroundColor Green
Write-Host "Groesse    : $zipMB MB"
Write-Host "Dauer      : $durationS s"
Write-Host "Komprimiert: $totalMB MB -> $zipMB MB (ca. $([math]::Round(($zipMB/$totalMB)*100,0))%)"
