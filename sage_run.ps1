# sage_run.ps1 - run a PowerShell command on the Sage PC via sage_commands.
# The sage_cmd.ps1 poller on that PC picks it up within ~15s, runs it, and
# posts the output back. Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File sage_run.ps1 -Command "Get-Content C:\sage_bridge\bridge_out.log -Tail 20"
# Add -NoWait to fire-and-forget (prints the command id to check later).
param(
    [Parameter(Mandatory=$true)][string]$Command,
    [int]$TimeoutSeconds = 360,
    [switch]$NoWait
)

$envVars = @{}
Get-Content 'sage_bridge\.env' | ForEach-Object { $k,$v = $_ -split '=',2; $envVars[$k.Trim()] = $v.Trim() }
$h = @{ apikey = $envVars['SUPABASE_KEY']; Authorization = "Bearer $($envVars['SUPABASE_KEY'])"; 'Content-Type'='application/json' }
$base = $envVars['SUPABASE_URL'].TrimEnd('/') + '/rest/v1/sage_commands'

$row = @(Invoke-RestMethod -Method Post -Uri "$base`?select=id" -Headers ($h + @{ Prefer = 'return=representation' }) `
    -Body (@{ command = $Command; created_by = $env:USERNAME } | ConvertTo-Json -Compress))
$id = $row[0].id
"queued command #$id on the Sage PC"
if ($NoWait) { return }

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep 4
    $r = @(Invoke-RestMethod -Uri "$base`?id=eq.$id&select=status,output" -Headers $h)
    if ($r.Count -and $r[0].status -in @('done','error')) {
        if ($r[0].status -eq 'error') { Write-Host "FAILED:" -ForegroundColor Red }
        if ($r[0].output) { $r[0].output }
        return
    }
}
Write-Host "timed out waiting for #$id - check sage_commands table later" -ForegroundColor Yellow
