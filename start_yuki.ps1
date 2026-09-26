# =============================================================
#  Yuki starten - Textual-Dashboard.
#  Diese Box (.101) ist seit dem Core-Umzug (2026-09-10) der GPU-Dienst-Host:
#  startet Ollama + Vision (0.0.0.0) + Qwen3-TTS + Whisper-STT (:5007).
#  Der Yuki-Core selbst laeuft via systemd auf dem Notebook .103, SearXNG als
#  Docker ebenfalls auf .103 - beide erscheinen im Dashboard nur als Status
#  (display_only), werden hier NICHT gestartet.
#
#  Aufruf:  .\start_yuki.ps1
#  Layout-Testen ohne Services: .\start_yuki.ps1 --dry-run
#
#  Hotkeys im Dashboard:
#    q = Quit       r = Restart Yuki    c = Tab leeren    1-7 = Tab-Switch
#
#  Das alte Verhalten (PowerShell-Skript mit sequentiellem Start +
#  Zombie-Kill in eigenen Fenstern) liegt in der Git-History, falls mal
#  noetig: `git show HEAD~3:start_yuki.ps1` (oder Commit vor 2026-06-09).
# =============================================================
$proj      = $PSScriptRoot
$python    = Join-Path $proj ".venv\Scripts\python.exe"
$dashboard = Join-Path $proj "tools\yuki_dashboard.py"

if (-not (Test-Path $python)) {
    Write-Host "FEHLER: $python nicht gefunden." -ForegroundColor Red
    Write-Host "Erst venv anlegen: docs/setup-* oder requirements.lock" -ForegroundColor DarkGray
    exit 1
}
if (-not (Test-Path $dashboard)) {
    Write-Host "FEHLER: $dashboard nicht gefunden." -ForegroundColor Red
    exit 1
}

# WSL2-Keep-Alive (frueher: lokalen SearXNG-Docker warm halten) ersatzlos
# entfernt mit dem Core-Umzug 2026-09-10: SearXNG laeuft jetzt als Docker auf
# dem Core .103, kein Dienst auf dieser Box (.101) nutzt mehr WSL. Block liegt
# in der Git-History, falls je wieder ein lokaler WSL-Dienst dazukommt.

& $python $dashboard @args
