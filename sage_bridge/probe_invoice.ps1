# Read-only reflection probe for the Sales Invoice API.
# Dumps members of SalesInvoiceFactory, SalesInvoice, SalesInvoiceLine
# so the bridge can create invoices from sales orders correctly.
# Creates NOTHING in Sage.
#
# MUST run in 32-bit PowerShell:
#   C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File .\probe_invoice.ps1

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

[Reflection.Assembly]::LoadFrom("$SAGE_BASE\Peachtree\API\Sage.Peachtree.API.dll") | Out-Null

$session = New-Object Sage.Peachtree.API.PeachtreeSession
$session.Begin($APP_ID)
$ids = $session.CompanyList()
$cid = $ids | Where-Object { $_.CompanyName -eq $COMPANY_NAME } | Select-Object -First 1
if (-not $cid) { $cid = $ids | Where-Object { $_.CompanyName -match [regex]::Escape($COMPANY_NAME) } | Select-Object -First 1 }
$session.RequestAccess($cid) | Out-Null
$company = $session.Open($cid)
Write-Host "opened $($cid.CompanyName)" -ForegroundColor Green

function Dump-Type([type]$t) {
    Write-Host "`n=== $($t.Name) ===" -ForegroundColor Cyan
    Write-Host "-- properties --"
    $t.GetProperties() | Sort-Object Name | ForEach-Object {
        $rw = if ($_.CanWrite) { 'set' } else { 'get' }
        Write-Host "  $rw $($_.PropertyType.Name) $($_.Name)"
    }
    Write-Host "-- methods --"
    $t.GetMethods() | Where-Object { -not $_.IsSpecialName } | Sort-Object Name -Unique |
        ForEach-Object { Write-Host "  $($_.Name)($($_.GetParameters() | ForEach-Object { $_.ParameterType.Name } | Join-String -Separator ', '))" }
}

Write-Host "`n=== Factories (invoice-related) ===" -ForegroundColor Cyan
$company.Factories | Get-Member -MemberType Property |
    Where-Object { $_.Name -match 'nvoice|ales|hip' } |
    ForEach-Object { Write-Host "  $($_.Name): $($_.Type -as [type]).Name" }

try { Dump-Type ([Sage.Peachtree.API.SalesInvoice]) } catch { Write-Host "SalesInvoice: $_" -ForegroundColor Yellow }
try { Dump-Type ([Sage.Peachtree.API.SalesInvoiceLine]) } catch { Write-Host "SalesInvoiceLine: $_" -ForegroundColor Yellow }
try { Dump-Type ([Sage.Peachtree.API.SalesInvoiceFactory]) } catch { Write-Host "SalesInvoiceFactory: $_" -ForegroundColor Yellow }

$session.Close($company)
$session.End()
Write-Host "`nProbe complete." -ForegroundColor Green
