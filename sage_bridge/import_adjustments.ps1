# import_adjustments.ps1 - stage pending inventory adjustments and run the
# headless Sage import (assembly_import.au3, parameterized) via AutoIt.
#
# Flow:
#   inventory_adjustments.csv (bridge appends adjustments here)
#     -> if it has data rows, move it aside to inventory_adjustments_pending.csv
#        (the fixed filename the Sage import template is configured to read)
#     -> run AutoIt driver against the Sage UI (needle 'Adjustment')
#     -> on success archive to imported\, on failure keep pending + log
#
# The Sage "Inventory Adjustments Journal" import template must be configured
# once: fields Item ID, Reference, Date, GL Acct-Cost, Quantity, Unit Cost,
# Reason to Adjust - in that order - and the Options-tab filename pointed at
# inventory_adjustments_pending.csv in this folder.
#
# Safe to run on a schedule every few minutes - exits fast when nothing
# is pending or when Sage isn't open.

$ErrorActionPreference = 'Stop'
$envFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$DIR      = $PSScriptRoot
$BUILDS   = if ($env:INV_ADJ_CSV)         { $env:INV_ADJ_CSV }         else { Join-Path $DIR 'inventory_adjustments.csv' }
$PENDING  = if ($env:INV_ADJ_PENDING_CSV) { $env:INV_ADJ_PENDING_CSV } else { Join-Path $DIR 'inventory_adjustments_pending.csv' }
$ARCHIVE  = Join-Path $DIR 'imported'
$LOG      = Join-Path $DIR 'adjustment_import.log'
$AU3      = Join-Path $DIR 'assembly_import.au3'
$HEADER   = 'Item ID,Reference,Date,GL Acct-Cost,Quantity,Unit Cost,Reason to Adjust'

function Log([string]$m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content $LOG
}

if (-not (Test-Path $BUILDS)) { exit 0 }

# Anything beyond the header line?
$data = (Get-Content $BUILDS | Select-Object -Skip 1 | Where-Object { $_.Trim() })
if (-not $data) { exit 0 }

# The bridge launches us on-change too - never run two importers, the
# second would clobber the pending file mid-import.
if (Get-Process AutoIt3 -ErrorAction SilentlyContinue) {
    Log 'SKIP: another import is already running - rows stay pending'
    exit 0
}

# Sage must be running for the UI drive to work
if (-not (Get-Process peachw -ErrorAction SilentlyContinue)) {
    Log 'SKIP: Sage (peachw.exe) not running - adjustments stay pending'
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
Log "staged $($data.Count) adjustment row(s) to inventory_adjustments_pending.csv"

$proc = Start-Process $autoit -ArgumentList "`"$AU3`" Adjustment adjustment_import.log" -Wait -PassThru `
        -WorkingDirectory $DIR -WindowStyle Hidden
Log "autoit exit code $($proc.ExitCode)"

if ($proc.ExitCode -eq 0) {
    New-Item -ItemType Directory -Path $ARCHIVE -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Move-Item $PENDING (Join-Path $ARCHIVE "adjustments_$stamp.csv") -Force
    Log "OK: imported and archived adjustments_$stamp.csv"
} else {
    # Rejected/partial import: archive for review - do NOT restore to the
    # builds file, since rows before an error may have imported and
    # re-adding them would duplicate the adjustment.
    $failDir = Join-Path $DIR 'failed'
    New-Item -ItemType Directory -Path $failDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Move-Item $PENDING (Join-Path $failDir "adjustments_$stamp.csv") -Force
    Log "FAIL: import exit $($proc.ExitCode) - rows archived to failed\adjustments_$stamp.csv"
}
