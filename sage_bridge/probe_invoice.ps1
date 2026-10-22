# Read-only reflection probe for the Sales Invoice API surface.
# Pure metadata - opens NO Sage session.
#
#   powershell -ExecutionPolicy Bypass -File .\probe_invoice.ps1

$ErrorActionPreference = 'Continue'
$SAGE_BASE = if ($env:SAGE_BASE_DIR) { $env:SAGE_BASE_DIR } else { 'C:\Program Files (x86)\Sage' }

# Precompute the dll map ONCE - doing Get-ChildItem inside the handler
# re-triggers assembly resolution and stack-overflows.
$script:dllMap = @{}
Get-ChildItem $SAGE_BASE -Recurse -Filter "*.dll" -ErrorAction SilentlyContinue |
    ForEach-Object { $script:dllMap[$_.BaseName.ToLower()] = $_.FullName }
$script:resolveHandler = {
    param($sender, $e)
    $name = ($e.Name -split ',')[0].ToLower()
    if ($script:dllMap.ContainsKey($name)) {
        return [Reflection.Assembly]::LoadFrom($script:dllMap[$name])
    }
    return $null
}
[AppDomain]::CurrentDomain.add_AssemblyResolve($script:resolveHandler)

[Reflection.Assembly]::LoadFrom("$SAGE_BASE\Peachtree\API\Sage.Peachtree.API.dll") | Out-Null
$asm = [AppDomain]::CurrentDomain.GetAssemblies() | Where-Object { $_.GetName().Name -eq 'Sage.Peachtree.API' } | Select-Object -First 1
if (-not $asm) { Write-Host "API assembly not found under $SAGE_BASE" -ForegroundColor Red; exit 1 }

function Dump-Type([type]$t) {
    if (-not $t) { Write-Host "  (type not found)" -ForegroundColor Yellow; return }
    Write-Host "`n=== $($t.FullName) ===" -ForegroundColor Cyan
    try {
        $t.GetProperties() | Sort-Object Name | ForEach-Object {
            $rw = if ($_.CanWrite) { 'set' } else { 'get' }
            Write-Host "  $rw $($_.PropertyType.Name) $($_.Name)"
        }
        Write-Host "  -- methods --"
        $t.GetMethods() | Where-Object { -not $_.IsSpecialName } | Sort-Object Name -Unique |
            ForEach-Object { $p = ($_.GetParameters() | ForEach-Object { $_.ParameterType.Name }) -join ', '; Write-Host "    $($_.Name)($p)" }
    } catch { Write-Host "  (member dump failed: $($_.Exception.Message))" -ForegroundColor Yellow }
}

foreach ($n in @(
    'Sage.Peachtree.API.SalesInvoice',
    'Sage.Peachtree.API.SalesInvoiceSalesOrderLineCollection',
    'Sage.Peachtree.API.SalesInvoiceSalesOrderLine',
    'Sage.Peachtree.API.SalesInvoiceSalesLineCollection',
    'Sage.Peachtree.API.SalesInvoiceSalesLine',
    'Sage.Peachtree.API.SalesOrderLine',
    'Sage.Peachtree.API.SalesOrder'
)) {
    Dump-Type ($asm.GetType($n))
}
