# sage_cmd.ps1 - remote command runner for the Sage PC.
#
# Polls the Supabase `sage_commands` table every POLL seconds. Agents enqueue
# a row {command}; this script runs it in a child powershell process and
# writes stdout+stderr back to the row. Lets Devin sessions run PowerShell
# on this PC without anyone pasting commands manually.
#
# Deploy: copy to C:\sage_bridge and register a Scheduled Task (see
# AGENTS.md / README). Same trust domain as the bridge: whoever holds the
# service-role key can already post sales orders - treat it accordingly.
#
# Safety rails: commands run one at a time, in this folder, with a timeout;
# output is truncated so a runaway print can't bloat the row.

$ErrorActionPreference = 'Continue'
$DIR = $PSScriptRoot

$envFile = Join-Path $DIR '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$SUPABASE_URL = $env:SUPABASE_URL
$SUPABASE_KEY = $env:SUPABASE_KEY
if (-not $SUPABASE_URL -or -not $SUPABASE_KEY) {
    Write-Host 'Missing SUPABASE_URL / SUPABASE_KEY in .env' -ForegroundColor Red
    exit 1
}
$SUPABASE_URL = $SUPABASE_URL.TrimEnd('/')

# Single instance: the scheduled task respawns every few minutes as a
# crash-recovery backstop - a second copy would double-run commands.
$script:cmdMutex = New-Object Threading.Mutex($false, 'RatechSageCmd')
if (-not $script:cmdMutex.WaitOne(0)) { exit 0 }

$TABLE = 'sage_commands'
$POLL  = if ($env:CMD_POLL_SECONDS) { [int]$env:CMD_POLL_SECONDS } else { 15 }
$CMD_TIMEOUT = if ($env:CMD_TIMEOUT_SECONDS) { [int]$env:CMD_TIMEOUT_SECONDS } else { 300 }
$MAX_OUT = 12000

function Headers {
    @{ apikey = $SUPABASE_KEY; Authorization = "Bearer $SUPABASE_KEY"; 'Content-Type' = 'application/json' }
}
function Log([string]$m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content (Join-Path $DIR 'sage_cmd.log')
    Write-Host $m
}

# Run a command in an isolated child process; capture stdout+stderr.
# Returns @{output; timedOut}
function Invoke-Cmd([string]$cmd) {
    $outFile = Join-Path $env:TEMP "sagecmd_$(Get-Random).txt"
    try {
        $p = Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command', $cmd `
            -WorkingDirectory $DIR -RedirectStandardOutput $outFile -RedirectStandardError "$outFile.err" `
            -NoNewWindow -PassThru
        $timedOut = -not $p.WaitForExit($CMD_TIMEOUT * 1000)
        if ($timedOut) { try { $p.Kill() } catch {} }
        $out = ''
        foreach ($f in @($outFile, "$outFile.err")) {
            if (Test-Path $f) { $out += (Get-Content $f -Raw -ErrorAction SilentlyContinue) + "`n" }
        }
        if ($timedOut) { $out += "`n[TIMEOUT after ${CMD_TIMEOUT}s - process killed]" }
        if ($out.Length -gt $MAX_OUT) { $out = $out.Substring(0, $MAX_OUT) + "`n[TRUNCATED]" }
        return @{ output = $out.Trim(); timedOut = $timedOut }
    } finally {
        Remove-Item $outFile, "$outFile.err" -Force -ErrorAction SilentlyContinue
    }
}

function Finish($id, [string]$status, [string]$output) {
    $body = @{ status = $status; output = $output; finished_at = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json -Compress
    Invoke-RestMethod -Uri "$SUPABASE_URL/rest/v1/$TABLE`?id=eq.$id" -Method Patch `
        -Headers (Headers) -Body $body -TimeoutSec 20 | Out-Null
}

Log "sage_cmd starting - poll every ${POLL}s"
while ($true) {
    $id = $null
    try {
        $rows = Invoke-RestMethod -Uri "$SUPABASE_URL/rest/v1/$TABLE`?status=eq.pending&order=id.asc&limit=1" `
            -Headers (Headers) -TimeoutSec 20
        $rows = @($rows)
        if ($rows.Count -eq 0) { Start-Sleep $POLL; continue }
        $row  = $rows[0]
        $id   = $row.id
        $cmd  = "$($row.command)"
        Log "[.] claim #$id : $cmd"
        Invoke-RestMethod -Uri "$SUPABASE_URL/rest/v1/$TABLE`?id=eq.$id" -Method Patch `
            -Headers (Headers) -TimeoutSec 20 `
            -Body (@{ status = 'running'; started_at = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json -Compress) | Out-Null
        $res = Invoke-Cmd $cmd
        Finish $id ($(if ($res.timedOut) { 'error' } else { 'done' })) $res.output
        Log "[+] #$id done ($(if ($res.timedOut) {'timeout'} else {'ok'}), $($res.output.Length) chars)"
    } catch {
        Log "[!] sweep error: $_"
        if ($id) { try { Finish $id 'error' "$_" } catch {} }
        Start-Sleep $POLL
    }
}
