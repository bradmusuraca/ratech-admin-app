# Toggle the bridge's PAUSE flag: when C:\sage_bridge\PAUSE exists, the
# bridge releases the Sage company session and idles (safe for backups and
# Sage maintenance). Run again to resume. The flag is also cleared if the
# bridge is not running, so it can't get stuck paused after a reboot.
$f = Join-Path $PSScriptRoot 'PAUSE'
$running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -match 'sage_bridge.*\.ps1' -and $_.CommandLine -notmatch 'start_bridge|pause_bridge' }

if (Test-Path $f) {
    Remove-Item $f -Force
    Write-Host "Bridge RESUMED - it will reopen Sage on its next poll."
} else {
    if (-not $running) { Write-Host "Note: no bridge process is currently running." }
    New-Item $f -ItemType File -Force | Out-Null
    Write-Host "Bridge PAUSING - Sage session releases within a few seconds."
    Write-Host "Run this script again (or delete $f) to resume."
}
