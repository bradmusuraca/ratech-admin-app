# Probe the Sage 50 COM API on this machine (no Python needed).
#
# Run once before starting bridge.ps1. It opens a PeachwServer session,
# creates a PTOWSalesOrder, and prints every property/method the object
# exposes — compare the output against $FM in bridge.ps1 and rename any
# that differ.
#
#   powershell -ExecutionPolicy Bypass -File .\probe.ps1

$envFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$COMPANY_PATH = $env:SAGE_COMPANY_PATH
if (-not $COMPANY_PATH) {
    Write-Host 'Set SAGE_COMPANY_PATH in .env first' -ForegroundColor Red
    exit 1
}

$server = New-Object -ComObject PeachwServer
$server.Begin('RatechProbe') | Out-Null
$company = $server.OpenCompany($COMPANY_PATH)

$so = $company.CreateObject('PTOWSalesOrder')
Write-Host '=== PTOWSalesOrder members ==='
$so | Get-Member | Select-Object -ExpandProperty Name | Sort-Object -Unique

try {
    $lines = $so.LineItems
    $ln = $lines.Add()
    Write-Host '=== Line item members ==='
    $ln | Get-Member | Select-Object -ExpandProperty Name | Sort-Object -Unique
} catch {
    Write-Host "LineItems probe failed: $_"
}

$server.End() | Out-Null
