# Read-only reflection probe for the Sales Invoice API surface.
# Pure metadata - opens NO Sage session.
#
#   powershell -ExecutionPolicy Bypass -File .\probe_invoice.ps1

$ErrorActionPreference = 'Continue'
$SAGE_BASE = if ($env:SAGE_BASE_DIR) { $env:SAGE_BASE_DIR } else { 'C:\Program Files (x86)\Sage' }

# No resolver handler - preload every Sage dll instead (two passes so
# assemblies whose deps load late still succeed).
$dlls = Get-ChildItem $SAGE_BASE -Recurse -Filter "*.dll" -ErrorAction SilentlyContinue
1..2 | ForEach-Object {
    $dlls | ForEach-Object {
        try { [Reflection.Assembly]::LoadFrom($_.FullName) | Out-Null } catch { }
    }
}
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

$types = @()
try { $types = $asm.GetTypes() }
catch { $types = $_.Exception.Types | Where-Object { $_ } }

Write-Host "=== types matching Factory|Filter ===" -ForegroundColor Cyan
$types | Where-Object { $_.Name -match 'Factory|Filter' } |
    ForEach-Object { Write-Host "  $($_.FullName)" }

foreach ($n in @(
    'Sage.Peachtree.API.SalesOrderFactory',
    'Sage.Peachtree.API.Filter'
)) {
    Dump-Type ($asm.GetType($n))
}
