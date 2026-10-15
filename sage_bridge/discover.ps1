# Discovery: enumerate the Sage.Peachtree.API .NET assembly WITHOUT the SDK.
# Resolves dependencies from anywhere under C:\Program Files (x86)\Sage.
#
#   powershell -ExecutionPolicy Bypass -File .\discover.ps1
# (must be 32-bit PowerShell: C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe)

$ErrorActionPreference = 'Continue'
$base = 'C:\Program Files (x86)\Sage'
$apiDll = Join-Path $base 'Peachtree\API\Sage.Peachtree.API.dll'

# Resolve any Sage dependency by filename from anywhere under the Sage dir
$resolve = {
    param($sender, $e)
    $name = ($e.Name -split ',')[0]
    $hit = Get-ChildItem $base -Recurse -Filter "$name.dll" -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { return [Reflection.Assembly]::LoadFrom($hit.FullName) }
    return $null
}
[AppDomain]::CurrentDomain.add_AssemblyResolve($resolve)

Write-Host "Loading $apiDll ..."
$asm = [Reflection.Assembly]::LoadFrom($apiDll)

Write-Host "`n=== Exported types (names only) ==="
try {
    $asm.GetExportedTypes() | ForEach-Object { $_.FullName } | Sort-Object
} catch {
    Write-Host "Full type scan failed: $_" -ForegroundColor Yellow
    Write-Host "Retrying with partial enumeration..."
    try { $types = $asm.GetTypes() } catch [Reflection.ReflectionTypeLoadException] { $types = $_.Exception.Types }
    $types | Where-Object { $_ -ne $null } | ForEach-Object { $_.FullName } | Sort-Object
}

Write-Host "`n=== Looking for server/company/order classes ==="
try { $types = $asm.GetExportedTypes() } catch { $types = $asm.GetTypes() }
$interesting = $types | Where-Object {
    $_.FullName -match 'Server|Company|SalesOrder|Order|Journal|Invoice'
}
foreach ($t in $interesting) {
    Write-Host "`n--- $($t.FullName) ---"
    $t.GetMembers() | ForEach-Object { "  $($_.MemberType): $($_.Name)" } | Sort-Object -Unique
}
