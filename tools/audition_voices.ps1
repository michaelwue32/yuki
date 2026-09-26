# Yuki-Stimmkandidaten durchhoeren (A/B: neuer Favorit vs. aktuelle Stimme).
# Aufruf:  .\tools\audition_voices.ps1
# Enter = naechster Clip, Ctrl+C = Ende.
$ErrorActionPreference = "Stop"
$voices = "D:\Projects\yuki\voices"

# A/B: 109 (neuer Favorit) vs. 20 (aktuelle Stimme).
$list = @(
    @("vv_109_東北イタコ.wav",  "Tohoku Itako - NEUER Favorit, reife erwachsene Frau"),
    @("vv_20_もち子さん.wav",   "Mochiko / Normal - AKTUELLE Stimme (zu quietschig)")
)

$i = 0
foreach ($item in $list) {
    $i++
    $file = Join-Path $voices $item[0]
    if (-not (Test-Path $file)) { Write-Host "[fehlt] $($item[0])" -ForegroundColor DarkGray; continue }
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $i, $list.Count, $item[1]) -ForegroundColor Cyan
    Write-Host ("      Datei: {0}" -f $item[0]) -ForegroundColor DarkGray
    (New-Object Media.SoundPlayer $file).PlaySync()
    Read-Host "      Enter = naechster (Ctrl+C = Ende)"
}
Write-Host "`nDurch. Wenn 109 gewinnt: sag Bescheid, dann stelle ich SoVITS+F5 um." -ForegroundColor Green
