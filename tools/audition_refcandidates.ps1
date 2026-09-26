# F5-Referenz-Kandidaten (Itako-Bootstrap) - engere Auswahl vergleichen.
# Aufruf:  .\tools\audition_refcandidates.ps1
# Enter = naechster Clip, Ctrl+C = Ende. Merk dir den Dateinamen deines Favoriten.
$ErrorActionPreference = "Stop"
$dir = "D:\Projects\yuki\tests\outputs\refcandidates"

# Michaels engere Auswahl (vollstaendige Clips).
$names = @(
    "yuki_a_sp100_seed01_dur6.1s_r0.82.wav",
    "yuki_b_sp100_seed03_dur6.7s_r0.81.wav",
    "yuki_a_sp090_seed01_dur6.8s_r0.82.wav",
    "yuki_a_sp090_seed03_dur6.8s_r0.82.wav",
    "yuki_b_sp090_seed00_dur7.4s_r0.81.wav",
    "yuki_b_sp090_seed02_dur7.4s_r0.81.wav"
)

Write-Host "yuki_a = 'Hallo, ich bin Yuki. Schoen, dass du da bist. Lass uns heute ganz in Ruhe ein bisschen Japanisch ueben, ja?'" -ForegroundColor Yellow
Write-Host "yuki_b = 'Guten Morgen! Ich freue mich, dass wir uns wiedersehen. Setz dich, nimm dir Zeit, und dann fangen wir gemuetlich an.'" -ForegroundColor Yellow
Write-Host ("{0} Clips. sp100 = Live-Tempo, sp090 = etwas langsamer/ruhiger.`n" -f $names.Count) -ForegroundColor Green

$i = 0
foreach ($n in $names) {
    $i++
    $f = Join-Path $dir $n
    if (-not (Test-Path $f)) { Write-Host "[fehlt] $n" -ForegroundColor DarkGray; continue }
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $i, $names.Count, $n) -ForegroundColor Cyan
    (New-Object Media.SoundPlayer $f).PlaySync()
    Read-Host "      Enter = naechster (Ctrl+C = Ende)"
}
Write-Host "`nDurch. Sag mir den Dateinamen deines Favoriten." -ForegroundColor Green
