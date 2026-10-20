# Ratech -> Sage 50 bridge (PowerShell + Sage.Peachtree.API managed .NET API).
# No Python, no SDK install, no COM registration — uses the API assembly
# already installed with Sage 50 at C:\Program Files (x86)\Sage\Peachtree\API.
#
# MUST run in 32-bit PowerShell (Sage is 32-bit):
#   C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File .\bridge.ps1
#
# First run probe.ps1 to verify the company opens and the app is authorized.

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'Stop'

# ---- config ----
$envFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
$SUPABASE_URL   = $env:SUPABASE_URL.TrimEnd('/')
$SUPABASE_KEY   = $env:SUPABASE_KEY
$COMPANY_NAME   = $env:SAGE_COMPANY_NAME   # substring match on CompanyIdentifier CompanyName/DatabaseName/Path
$APP_ID         = if ($env:SAGE_APP_ID) { $env:SAGE_APP_ID } else { 'Ratech.OrderBridge.1.0' }
$POLL_SECONDS   = 60
if ($env:POLL_SECONDS) { $POLL_SECONDS = [int]$env:POLL_SECONDS }
$TABLE          = 'sage_order_queue'
$SAGE_BASE      = if ($env:SAGE_BASE_DIR) { $env:SAGE_BASE_DIR } else { 'C:\Program Files (x86)\Sage' }

if (-not $SUPABASE_URL -or -not $SUPABASE_KEY -or -not $COMPANY_NAME) {
    Write-Host 'Missing SUPABASE_URL / SUPABASE_KEY / SAGE_COMPANY_NAME in .env' -ForegroundColor Red
    exit 1
}

# ---- load Sage.Peachtree.API with dependency resolution from the Sage tree ----
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
    return @($res).Count -gt 0
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

# ---- Sage session ----
function Open-SageCompany {
    $session = New-Object Sage.Peachtree.API.PeachtreeSession
    $session.Begin($APP_ID)
    $cid = $session.CompanyList() | Where-Object {
        $_.CompanyName -match [regex]::Escape($COMPANY_NAME) -or
        $_.DatabaseName -match [regex]::Escape($COMPANY_NAME) -or
        $_.Path -match [regex]::Escape($COMPANY_NAME)
    } | Select-Object -First 1
    if (-not $cid) {
        $session.End()
        throw "No company matching '$COMPANY_NAME'. Companies seen: " +
              (($session.CompanyList() | ForEach-Object { $_.CompanyName }) -join ', ')
    }
    $auth = $session.VerifyAccess($cid)
    if ("$auth" -notmatch 'Grant|Allow|Success|Authorized') {
        $auth = $session.RequestAccess($cid)
    }
    $company = $session.Open($cid)
    return @{ Session = $session; Company = $company }
}

# Resolve an entity's Key (EntityReference) by its ID, cached per sweep.
# NOTE: factory.List() returns an UNLOADED collection — .Load() first.
function Resolve-Ref($factory, [string]$id, $cache) {
    if ($cache.ContainsKey($id)) { return $cache[$id] }
    $list = $factory.List()
    $list.Load()
    $hit = $list | Where-Object { "$($_.ID)" -eq $id } | Select-Object -First 1
    if (-not $hit) {
        $hit = $list | Where-Object { "$($_.Number)" -eq $id } | Select-Object -First 1
    }
    if (-not $hit) { throw "No Sage entity with ID '$id'" }
    $cache[$id] = $hit.Key
    return $cache[$id]
}

function Trim-To([string]$s, [int]$n) { if ($s.Length -gt $n) { $s.Substring(0, $n) } else { $s } }

function New-SalesOrder($company, $payload, $refCache) {
    $so = $company.Factories.SalesOrderFactory.Create()
    $so.CustomerReference = Resolve-Ref $company.Factories.CustomerFactory "$($payload.customer_id)" $refCache.Customer
    $so.CustomerPurchaseOrderNumber = Trim-To "$($payload.customer_po)" 20
    if ($payload.order_date) { $so.Date = [datetime]$payload.order_date }
    if ($payload.ship_by)    { $so.ShipByDate = [datetime]$payload.ship_by }
    if ($payload.ship_via)   { $so.ShipVia = Trim-To "$($payload.ship_via)" 12 }
    if ($payload.internal_note) { $so.InternalNote = Trim-To "$($payload.internal_note)" 2000 }
    if ($payload.ship_to_name)  { $so.ShipToAddress.Name = Trim-To "$($payload.ship_to_name)" 39 }

    foreach ($line in $payload.lines) {
        $ln = $so.AddLine()
        if (-not $ln) { $ln = $so.SalesOrderLines.Add() }
        $ln.InventoryItemReference = Resolve-Ref $company.Factories.InventoryItemFactory "$($line.item_id)" $refCache.Item
        $ln.Description  = Trim-To "$($line.description)" 160
        $ln.Quantity     = [decimal]$line.quantity
        $ln.UnitPrice    = [decimal]$line.unit_price
        $ln.Amount       = [decimal]$line.quantity * [decimal]$line.unit_price
        $ln.SalesTaxType = 1
        try { $ln.Validate() | Out-Null } catch { }
        if ($line.gl_account) {
            try { $ln.AccountReference = Resolve-Ref $company.Factories.AccountFactory "$($line.gl_account)" $refCache.Account }
            catch { Write-Host "  [!] G/L account $($line.gl_account) unresolved, using item default" -ForegroundColor Yellow }
        }
    }
    # Sage requires an explicit reference number — take the next one
    $so.ReferenceNumber = "$($company.Services.ReferenceNumber.TakeNextSalesOrderNumber())"
    $so.Save()
    return "$($so.ReferenceNumber)"
}

function Update-SalesOrder($company, [string]$soNo, $lines) {
    $soList = $company.Factories.SalesOrderFactory.List()
    $soList.Load()
    $hit = $soList | Where-Object { "$($_.ReferenceNumber)" -eq $soNo } | Select-Object -First 1
    if (-not $hit) { throw "Sales order '$soNo' not found" }
    $so = $company.Factories.SalesOrderFactory.Load($hit.Key)
    $want = @{}
    foreach ($l in $lines) { $want["$($l.item_id)"] = [decimal]$l.quantity }
    foreach ($ln in $so.SalesOrderLines) {
        $iid = "$($ln.InventoryItemReference.Key.ID)"
        if (-not $iid) { $iid = "$($ln.InventoryItemReference)" }
        foreach ($k in $want.Keys) {
            if ($iid -match [regex]::Escape($k) -or $k -match [regex]::Escape($iid)) {
                $ln.Quantity = $want[$k]
            }
        }
    }
    $so.Save()
}

Write-Host "Sage bridge polling every $POLL_SECONDS s - Ctrl+C to stop"
while ($true) {
    $sess = $null
    try {
        $sess = Open-SageCompany
        $refCache = @{ Customer = @{}; Item = @{}; Account = @{} }
        foreach ($row in (Get-Pending)) {
            $key = $row.order_key
            if (-not (Claim-Order $key)) { continue }
            try {
                if ($row.job_type -eq 'qty_update') {
                    Update-SalesOrder $sess.Company "$($row.payload.sage_order_no)" $row.payload.lines
                    Finish-Order $key 'done'
                    Write-Host "[+] $($key.Substring(0,8)) -> updated SO $($row.payload.sage_order_no) quantities"
                } else {
                    $soNo = New-SalesOrder $sess.Company $row.payload $refCache
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
        if ($sess) { try { $sess.Session.End() } catch { } }
    }
    Start-Sleep -Seconds $POLL_SECONDS
}
