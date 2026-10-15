# Ratech -> Sage 50 bridge (PowerShell — no Python install needed).
#
# Polls the Supabase `sage_order_queue` table for pending orders,
# creates them as sales orders in Sage 50 via the PeachwServer COM API,
# and writes the result (done/error + Sage order number) back.
#
# Run on the Windows PC where Sage 50 is installed:
#
#   copy .env.example .env   (fill in values)
#   powershell -ExecutionPolicy Bypass -File .\probe.ps1
#   powershell -ExecutionPolicy Bypass -File .\bridge.ps1
#
# Loops forever; Ctrl+C to stop. For always-on use, register as a
# Scheduled Task at logon.

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- config ----
$envFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$SUPABASE_URL  = $env:SUPABASE_URL.TrimEnd('/')
$SUPABASE_KEY  = $env:SUPABASE_KEY
$COMPANY_PATH  = $env:SAGE_COMPANY_PATH
$POLL_SECONDS  = 60
if ($env:POLL_SECONDS) { $POLL_SECONDS = [int]$env:POLL_SECONDS }
$TABLE         = 'sage_order_queue'

if (-not $SUPABASE_URL -or -not $SUPABASE_KEY -or -not $COMPANY_PATH) {
    Write-Host 'Missing SUPABASE_URL / SUPABASE_KEY / SAGE_COMPANY_PATH in .env' -ForegroundColor Red
    exit 1
}

# Sage 50 COM field names — calibrate with probe.ps1 (see sage_client.py
# for the Python equivalent). Rename values here if your SDK differs.
$FM = @{
    customer_id       = 'CustomerID'
    customer_po       = 'CustomerPONumber'
    order_date        = 'TransactionDate'
    ship_by           = 'ShipByDate'
    ship_to_name      = 'ShipToName'
    ship_via          = 'ShipViaCode'
    internal_note     = 'InternalNote'
    lines_collection  = 'LineItems'
    line_item_id      = 'ItemID'
    line_description  = 'Description'
    line_quantity     = 'Quantity'
    line_unit_price   = 'UnitPrice'
    line_gl_account   = 'GLAccount'
    line_tax_type     = 'TaxType'
    order_number      = 'SalesOrderNumber'
}

function Set-Prop($obj, [string]$name, $value) { $obj.$name = $value }
function Get-Prop($obj, [string]$name) { return $obj.$name }
function Trim-To([string]$s, [int]$n) { if ($s.Length -gt $n) { $s.Substring(0, $n) } else { $s } }

function Headers {
    @{
        apikey        = $SUPABASE_KEY
        Authorization = "Bearer $SUPABASE_KEY"
        'Content-Type' = 'application/json'
    }
}

function Get-Pending {
    $uri = "$SUPABASE_URL/rest/v1/$TABLE`?status=eq.pending&order=created_at.asc&select=order_key,job_type,payload"
    Invoke-RestMethod -Uri $uri -Headers (Headers) -TimeoutSec 20
}

function Claim-Order([string]$key) {
    $uri = "$SUPABASE_URL/rest/v1/$TABLE`?order_key=eq.$key&status=eq.pending"
    $h = Headers; $h['Prefer'] = 'return=representation'
    $body = @{ status = 'processing'; updated_at = 'now()' } | ConvertTo-Json
    $res = Invoke-RestMethod -Uri $uri -Method Patch -Headers $h -Body $body -TimeoutSec 20
    return $res.Count -gt 0
}

function Finish-Order([string]$key, [string]$status, $soNo = $null, $err = $null) {
    $uri = "$SUPABASE_URL/rest/v1/$TABLE`?order_key=eq.$key"
    $body = @{ status = $status; sage_order_no = $soNo; error = $err; updated_at = 'now()' } | ConvertTo-Json
    Invoke-RestMethod -Uri $uri -Method Patch -Headers (Headers) -Body $body -TimeoutSec 20 | Out-Null
}

function Record-SoNumber([string]$key, [string]$soNo) {
    $uri = "$SUPABASE_URL/rest/v1/open_orders`?order_key=eq.$key"
    $body = @{ sage_order_no = $soNo; updated_at = 'now()' } | ConvertTo-Json
    try { Invoke-RestMethod -Uri $uri -Method Patch -Headers (Headers) -Body $body -TimeoutSec 20 | Out-Null }
    catch { Write-Host "  [!] open_orders update failed: $_" -ForegroundColor Yellow }
}

function Open-Sage {
    $server = New-Object -ComObject PeachwServer
    $server.Begin('RatechOrderBridge') | Out-Null
    $company = $server.OpenCompany($COMPANY_PATH)
    return @{ Server = $server; Company = $company }
}

function New-SalesOrder($company, $payload) {
    $so = $company.CreateObject('PTOWSalesOrder')
    Set-Prop $so $FM.customer_id "$($payload.customer_id)"
    Set-Prop $so $FM.customer_po (Trim-To "$($payload.customer_po)" 20)
    if ($payload.order_date)    { Set-Prop $so $FM.order_date ([datetime]$payload.order_date) }
    if ($payload.ship_by)       { Set-Prop $so $FM.ship_by ([datetime]$payload.ship_by) }
    if ($payload.ship_to_name)  { Set-Prop $so $FM.ship_to_name (Trim-To "$($payload.ship_to_name)" 30) }
    if ($payload.ship_via)      { Set-Prop $so $FM.ship_via (Trim-To "$($payload.ship_via)" 12) }
    if ($payload.internal_note) { Set-Prop $so $FM.internal_note (Trim-To "$($payload.internal_note)" 2000) }

    $lines = Get-Prop $so $FM.lines_collection
    foreach ($line in $payload.lines) {
        $ln = $lines.Add()
        Set-Prop $ln $FM.line_item_id (Trim-To "$($line.item_id)" 20)
        Set-Prop $ln $FM.line_description (Trim-To "$($line.description)" 160)
        Set-Prop $ln $FM.line_quantity ([double]$line.quantity)
        Set-Prop $ln $FM.line_unit_price ([double]$line.unit_price)
        if ($line.gl_account) { Set-Prop $ln $FM.line_gl_account "$($line.gl_account)" }
        if ($line.tax_type)   { Set-Prop $ln $FM.line_tax_type "$($line.tax_type)" }
    }

    $so.Save()
    return "$(Get-Prop $so $FM.order_number)"
}

function Update-SalesOrder($company, [string]$soNo, $lines) {
    $so = $company.CreateObject('PTOWSalesOrder')
    $loaded = $false
    foreach ($meth in 'Load', 'Open', 'Read') {
        try { $so.$meth($soNo); $loaded = $true; break } catch { }
    }
    if (-not $loaded) {
        Set-Prop $so $FM.order_number $soNo
        $so.Load()
    }
    $want = @{}
    foreach ($l in $lines) { $want["$($l.item_id)"] = [double]$l.quantity }
    foreach ($ln in (Get-Prop $so $FM.lines_collection)) {
        $iid = "$(Get-Prop $ln $FM.line_item_id)"
        if ($want.ContainsKey($iid)) { Set-Prop $ln $FM.line_quantity $want[$iid] }
    }
    $so.Save()
}

Write-Host "Sage bridge polling every $POLL_SECONDS s - Ctrl+C to stop"
while ($true) {
    $sess = $null
    try {
        $sess = Open-Sage
        foreach ($row in (Get-Pending)) {
            $key = $row.order_key
            if (-not (Claim-Order $key)) { continue }
            try {
                if ($row.job_type -eq 'qty_update') {
                    Update-SalesOrder $sess.Company "$($row.payload.sage_order_no)" $row.payload.lines
                    Finish-Order $key 'done'
                    Write-Host "[+] $($key.Substring(0,8)) -> updated SO $($row.payload.sage_order_no) quantities"
                } else {
                    $soNo = New-SalesOrder $sess.Company $row.payload
                    Finish-Order $key 'done' $soNo
                    Record-SoNumber "$($row.payload.order_key)" $soNo
                    Write-Host "[+] $($key.Substring(0,8)) -> Sage sales order $soNo"
                }
            } catch {
                Finish-Order $key 'error' $null "$_"
                Write-Host "[!] $($key.Substring(0,8)) FAILED: $_" -ForegroundColor Red
            }
        }
    } catch {
        Write-Host "[!] sweep failed: $_" -ForegroundColor Red
    } finally {
        if ($sess) { try { $sess.Server.End() | Out-Null } catch { } }
    }
    Start-Sleep -Seconds $POLL_SECONDS
}
