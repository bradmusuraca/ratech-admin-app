# export_itemlist.ps1 - refresh itemlist.csv (Item ID + Unit Cost) by
# driving Sage's Item List export through the same AutoIt driver the
# importers use (3rd arg 'export' presses EXPO&RT, 4th arg is the window
# title needle 'Item List' since the template is named app_itemlist).
#
# The bridge calls this before writing inventory-adjustment rows so the
# Amount column uses current last-unit-cost, not a stale export.
#
# Prereq: the 'app_itemlist' export template must exist in Sage with
# fields Item ID + Unit Cost and the Options-tab filename pointed at
# C:\sage_bridge\itemlist.csv.

$ErrorActionPreference = 'Stop'
$envFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$DIR = $PSScriptRoot
$LOG = Join-Path $DIR 'itemlist_export.log'
$AU3 = Join-Path $DIR 'assembly_import.au3'

function Log([string]$m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content $LOG
}

if (Get-Process AutoIt3 -ErrorAction SilentlyContinue) {
    Log 'SKIP: another Sage UI automation is running'
    exit 2
}
if (-not (Get-Process peachw -ErrorAction SilentlyContinue)) {
    Log 'SKIP: Sage (peachw.exe) not running'
    exit 3
}

$autoit = @(
    "${env:ProgramFiles(x86)}\AutoIt3\AutoIt3.exe",
    "${env:ProgramFiles}\AutoIt3\AutoIt3.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $autoit) {
    Log 'FAIL: AutoIt3.exe not installed'
    exit 4
}

$proc = Start-Process $autoit -ArgumentList "`"$AU3`" itemlist itemlist_export.log export `"Item List`"" `
        -Wait -PassThru -WorkingDirectory $DIR -WindowStyle Hidden
Log "autoit exit code $($proc.ExitCode)"
exit $proc.ExitCode
