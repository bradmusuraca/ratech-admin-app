# start_bridge.ps1 - watchdog launcher for the Sage bridge.
# Scheduled every few minutes: if no powershell process is running
# bridge.ps1, start one hidden in the background. Exits instantly
# when the bridge is already up.
#
# To see live bridge output instead, run bridge.ps1 manually in a
# visible PowerShell window (the watchdog leaves it alone).

$running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match 'bridge\.ps1' }
if ($running) { exit 0 }

Start-Process "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -ArgumentList '-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\John\Desktop\sage_bridge\bridge.ps1' `
    -WorkingDirectory 'C:\Users\John\Desktop\sage_bridge'
