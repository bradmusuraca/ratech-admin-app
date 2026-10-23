# start_bridge.ps1 - watchdog launcher for the Sage bridge.
# Scheduled every few minutes: if no powershell process is running
# bridge.ps1, start one hidden in the background. Exits instantly
# when the bridge is already up.
#
# To see live bridge output instead, run bridge.ps1 manually in a
# visible PowerShell window (the watchdog leaves it alone).

# Match '\bridge.ps1' with a leading backslash - plain 'bridge\.ps1' also
# matches THIS script's own name (start_bridge.ps1) and the watchdog would
# think a bridge is already running. Excluding our own PID too for safety.
$running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '\\bridge\.ps1' }
if ($running) { exit 0 }

$bridge = Join-Path $PSScriptRoot 'bridge.ps1'
$log    = Join-Path $PSScriptRoot 'bridge_out.log'
# 32-bit PowerShell - the Sage managed API won't load in a 64-bit process.
# All output streams append to bridge_out.log since no window exists.
Start-Process "C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe" `
    -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"& '$bridge' *>> '$log'`"" `
    -WorkingDirectory $PSScriptRoot
