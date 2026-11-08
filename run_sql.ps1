# Run arbitrary SQL against Supabase via the exec_sql RPC (see
# sage_bridge/supabase_setup.sql). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File run_sql.ps1 -Query "select now()"
# SELECTs / INSERT..RETURNING come back as JSON rows; DDL/DML return {"ok":true}.
param([Parameter(Mandatory=$true)][string]$Query)

$envVars = @{}
Get-Content 'sage_bridge\.env' | ForEach-Object { $k,$v = $_ -split '=',2; $envVars[$k.Trim()] = $v.Trim() }
$h = @{ apikey = $envVars['SUPABASE_KEY']; Authorization = "Bearer $($envVars['SUPABASE_KEY'])"; 'Content-Type'='application/json' }
$base = $envVars['SUPABASE_URL'].TrimEnd('/') + '/rest/v1'

try {
    Invoke-RestMethod -Method Post -Uri "$base/rpc/exec_sql" -Headers $h `
        -Body (@{ query = $Query } | ConvertTo-Json) | ConvertTo-Json -Depth 10
} catch {
    $resp = $_.ErrorDetails.Message
    if ($resp) { Write-Error $resp } else { throw }
}
