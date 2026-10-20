# Read-only probe for the Sage.Peachtree.API managed API.
# Verifies: assembly loads, session begins, companies are visible,
# app access is authorized, and entity factories list correctly.
# Creates NOTHING in Sage.
#
# MUST run in 32-bit PowerShell:
#   C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File .\probe.ps1

$ErrorActionPreference = 'Continue'
$envFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$COMPANY_NAME = $env:SAGE_COMPANY_NAME
$APP_ID       = if ($env:SAGE_APP_ID) { $env:SAGE_APP_ID } else { 'Ratech.OrderBridge.1.0' }
$SAGE_BASE    = if ($env:SAGE_BASE_DIR) { $env:SAGE_BASE_DIR } else { 'C:\Program Files (x86)\Sage' }

$script:resolveHandler = {
    param($sender, $e)
    $name = ($e.Name -split ',')[0]
    $hit = Get-ChildItem $SAGE_BASE -Recurse -Filter "$name.dll" -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { return [Reflection.Assembly]::LoadFrom($hit.FullName) }
    return $null
}
[AppDomain]::CurrentDomain.add_AssemblyResolve($script:resolveHandler)

Write-Host 'Loading Sage.Peachtree.API ...'
[Reflection.Assembly]::LoadFrom("$SAGE_BASE\Peachtree\API\Sage.Peachtree.API.dll") | Out-Null

$session = New-Object Sage.Peachtree.API.PeachtreeSession
Write-Host "Begin('$APP_ID') ..."
try {
    $session.Begin($APP_ID)
    Write-Host '  OK' -ForegroundColor Green
} catch {
    Write-Host "  FAILED: $_" -ForegroundColor Red
    Write-Host '  If this says invalid application identifier, the app ID may need to be registered in Sage.'
    exit 1
}

Write-Host "`nCompanies visible to the API:"
$ids = $session.CompanyList()
$ids | ForEach-Object { Write-Host "  - $($_.CompanyName)  [db=$($_.DatabaseName) path=$($_.Path)]" }

$cid = $ids | Where-Object {
    $_.CompanyName -match [regex]::Escape($COMPANY_NAME) -or
    $_.DatabaseName -match [regex]::Escape($COMPANY_NAME) -or
    $_.Path -match [regex]::Escape($COMPANY_NAME)
} | Select-Object -First 1
if (-not $cid) {
    Write-Host "`nSAGE_COMPANY_NAME='$COMPANY_NAME' matched nothing — set it to part of a name above." -ForegroundColor Yellow
    $session.End()
    exit 1
}
Write-Host "`nUsing company: $($cid.CompanyName)"

Write-Host 'VerifyAccess ...'
try { Write-Host "  $($session.VerifyAccess($cid))" } catch { Write-Host "  $_" -ForegroundColor Yellow }
Write-Host 'RequestAccess ...'
try { Write-Host "  $($session.RequestAccess($cid))" } catch { Write-Host "  $_" -ForegroundColor Yellow }

Write-Host "`nOpen ..."
try {
    $company = $session.Open($cid)
    Write-Host '  OK' -ForegroundColor Green
} catch {
    Write-Host "  FAILED: $_" -ForegroundColor Red
    $session.End()
    exit 1
}

Write-Host "`nFirst 5 customers (ID -> Name):"
try {
    $cl = $company.Factories.CustomerFactory.List(); $cl.Load()
    $cl | Select-Object -First 5 |
        ForEach-Object { Write-Host "  $($_.ID) -> $($_.Name)" }
    Write-Host "  ($($cl.Count) customers total)"
} catch { Write-Host "  $_" -ForegroundColor Yellow }

Write-Host "`nFirst 5 inventory items (ID -> Description):"
try {
    $il = $company.Factories.InventoryItemFactory.List(); $il.Load()
    $il | Select-Object -First 5 |
        ForEach-Object { Write-Host "  $($_.ID) -> $($_.Description)" }
    Write-Host "  ($($il.Count) items total)"
} catch { Write-Host "  $_" -ForegroundColor Yellow }

Write-Host "`nNext sales order number (peek only):"
try { Write-Host "  $($company.Services.ReferenceNumber.TakeNextSalesOrderNumber())" }
catch { Write-Host "  $_" -ForegroundColor Yellow }

$session.Close($company)
$session.End()
Write-Host "`nProbe complete." -ForegroundColor Green
