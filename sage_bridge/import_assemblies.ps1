# import_assemblies.ps1 - stage pending assembly builds and run the
# headless Sage import (assembly_import.au3) via AutoIt.
#
# Flow:
#   assembly_builds.csv (bridge appends builds here)
#     -> if it has data rows, move it aside to assembly_pending.csv
#        (the fixed filename the Sage import template is configured to read)
#     -> run AutoIt driver against the Sage UI
#     -> on success archive to imported\, on failure keep pending + log
#
# Safe to run on a schedule every few minutes - exits fast when nothing
# is pending or when Sage isn't open.

$ErrorActionPreference = 'Stop'
$DIR      = $PSScriptRoot
$BUILDS   = Join-Path $DIR 'assembly_builds.csv'
$PENDING  = Join-Path $DIR 'assembly_pending.csv'
$ARCHIVE  = Join-Path $DIR 'imported'
$LOG      = Join-Path $DIR 'assembly_import.log'
$AU3      = Join-Path $DIR 'assembly_import.au3'
$HEADER   = 'Assembly Item ID,Reference,Date,Reason to Build,Quantity Built,Number of distributions,Quantity,Component Item ID,Revision Number Used'

function Log([string]$m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content $LOG
}

if (-not (Test-Path $BUILDS)) { exit 0 }

# Anything beyond the header line?
$data = (Get-Content $BUILDS | Select-Object -Skip 1 | Where-Object { $_.Trim() })
if (-not $data) { exit 0 }

# The bridge launches us on-change too - never run two importers, the
# second would clobber assembly_pending.csv mid-import.
if (Get-Process AutoIt3 -ErrorAction SilentlyContinue) {
    Log 'SKIP: another import is already running - rows stay pending'
    exit 0
}

# Sage must be running for the UI drive to work
if (-not (Get-Process peachw -ErrorAction SilentlyContinue)) {
    Log 'SKIP: Sage (peachw.exe) not running - builds stay pending'
    exit 0
}

# Find AutoIt
$autoit = @(
    "${env:ProgramFiles(x86)}\AutoIt3\AutoIt3.exe",
    "${env:ProgramFiles}\AutoIt3\AutoIt3.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $autoit) {
    Log 'FAIL: AutoIt3.exe not installed - download from autoitscript.com'
    exit 1
}

# Stage data rows only - the Sage template is configured WITHOUT
# "first row contains headings", so it reads every line as data.
$data | Set-Content $PENDING
Set-Content $BUILDS $HEADER   # reset builds file so bridge appends continue
Log "staged $($data.Count) build row(s) to assembly_pending.csv"

$proc = Start-Process $autoit -ArgumentList "`"$AU3`"" -Wait -PassThru `
        -WorkingDirectory $DIR -WindowStyle Hidden
Log "autoit exit code $($proc.ExitCode)"

if ($proc.ExitCode -eq 0) {
    New-Item -ItemType Directory -Path $ARCHIVE -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Move-Item $PENDING (Join-Path $ARCHIVE "assembly_$stamp.csv") -Force
    Log "OK: imported and archived assembly_$stamp.csv"
} else {
    Log "FAIL: import exit $($proc.ExitCode) - rows left in assembly_pending.csv"
    # Restore pending rows to the builds file so nothing is lost even if
    # the pending file gets clobbered later.
    if (Test-Path $BUILDS) {
        Get-Content $PENDING | Add-Content $BUILDS
    } else {
        Move-Item $PENDING $BUILDS -Force
    }
}
