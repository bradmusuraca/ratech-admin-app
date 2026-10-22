# Read-only reflection probe for the Sales Invoice API surface.
# Dumps the types involved in creating a sales invoice from a sales
# order. Opens NO Sage session - pure metadata, zero risk.
#
# Run in any PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\probe_invoice.ps1

$ErrorActionPreference = 'Continue'
$SAGE_BASE = if ($env:SAGE_BASE_DIR) { $env:SAGE_BASE_DIR } else { 'C:\Program Files (x86)\Sage' }

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
$asm = [AppDomain]::CurrentDomain.GetAssemblies() | Where-Object { $_.GetName().Name -eq 'Sage.Peachtree.API' }

$allTypes = @()
try { $allTypes = $asm.GetTypes() }
catch { $allTypes = @($_.Exception.Types | Where-Object { $_ }) }

Write-Host "=== invoice-related types ===" -ForegroundColor Cyan
$allTypes | Where-Object { $_.Name -match 'SalesInvoice|InvoiceLine|ApplyTo' } |
    Sort-Object Name | ForEach-Object { Write-Host "  $($_.FullName)" }

function Dump-Type([type]$t) {
    if (-not $t) { Write-Host "  (type not found)" -ForegroundColor Yellow; return }
    Write-Host "`n=== $($t.Name) ===" -ForegroundColor Cyan
    $t.GetProperties() | Sort-Object Name | ForEach-Object {
        $rw = if ($_.CanWrite) { 'set' } else { 'get' }
        Write-Host "  $rw $($_.PropertyType.Name) $($_.Name)"
    }
    Write-Host "  -- methods --"
    $t.GetMethods() | Where-Object { -not $_.IsSpecialName } | Sort-Object Name -Unique |
        ForEach-Object { $p = ($_.GetParameters() | ForEach-Object { $_.ParameterType.Name }) -join ', '; Write-Host "    $($_.Name)($p)" }
}

$get = {
    param([string]$name)
    $allTypes | Where-Object { $_.Name -eq $name } | Select-Object -First 1
}

Dump-Type ($get.Invoke('SalesInvoiceSalesOrderLineCollection'))
Dump-Type ($get.Invoke('SalesInvoiceSalesOrderLine'))
Dump-Type ($get.Invoke('SalesOrderLine'))
Dump-Type ($get.Invoke('SalesInvoiceFactory'))
