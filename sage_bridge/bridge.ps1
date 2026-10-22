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
$POLL_SECONDS   = 15
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

# Invoke-RestMethod in PS 5.1 can return a JSON array wrapped as ONE
# object instead of one element per row — flatten to real rows.
function Get-Rows($uri) {
    $r = Invoke-RestMethod -Uri $uri -Headers (Headers) -TimeoutSec 20
    $rows = @()
    if ($null -ne $r) { foreach ($x in $r) { $rows += $x } }
    return $rows
}

function Get-Pending {
    $uri = "$SUPABASE_URL/rest/v1/$TABLE`?status=eq.pending&order=created_at.asc&select=order_key,job_type,payload"
    Get-Rows $uri
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
    Write-Host "  [.] begin..." -ForegroundColor DarkGray
    $session.Begin($APP_ID)
    # Exact match first — substring matching can grab the wrong company
    # (e.g. 'Precision Gasket / Ratech' also -matches 'RATECH').
    Write-Host "  [.] company list..." -ForegroundColor DarkGray
    $cid = $session.CompanyList() | Where-Object {
        $_.CompanyName -ieq $COMPANY_NAME -or $_.DatabaseName -ieq $COMPANY_NAME
    } | Select-Object -First 1
    if (-not $cid) {
        $cid = $session.CompanyList() | Where-Object {
            $_.CompanyName -match [regex]::Escape($COMPANY_NAME) -or
            $_.DatabaseName -match [regex]::Escape($COMPANY_NAME) -or
            $_.Path -match [regex]::Escape($COMPANY_NAME)
        } | Select-Object -First 1
    }
    if (-not $cid) {
        $session.End()
        throw "No company matching '$COMPANY_NAME'. Companies seen: " +
              (($session.CompanyList() | ForEach-Object { $_.CompanyName }) -join ', ')
    }
    # RequestAccess activates the grant for this session (VerifyAccess
    # alone only inspects it — Open fails with "Authentication failed").
    Write-Host "  [.] request access ($($cid.CompanyName))..." -ForegroundColor DarkGray
    $auth = $session.RequestAccess($cid)
    if ("$auth" -notmatch 'Grant|Allow|Success|Authorized') {
        $auth = $session.VerifyAccess($cid)
    }
    Write-Host "  [.] open..." -ForegroundColor DarkGray
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
    if ($payload.order_date) {
        try { $so.Date = [datetime]$payload.order_date }
        catch { Write-Host "  [!] bad order_date '$($payload.order_date)' - using today" -ForegroundColor Yellow; $so.Date = Get-Date }
    }
    if ($payload.ship_by) {
        try { $so.ShipByDate = [datetime]$payload.ship_by }
        catch { Write-Host "  [!] bad ship_by '$($payload.ship_by)' - left blank" -ForegroundColor Yellow }
    }
    if ($payload.ship_via)   { $so.ShipVia = Trim-To "$($payload.ship_via)" 12 }
    if ($payload.internal_note) { $so.InternalNote = Trim-To "$($payload.internal_note)" 2000 }
    if ($payload.ship_to_name)    { $so.ShipToAddress.Name = Trim-To "$($payload.ship_to_name)" 39 }
    if ($payload.ship_to_line1)   { $so.ShipToAddress.Address.Address1 = Trim-To "$($payload.ship_to_line1)" 39 }
    if ($payload.ship_to_line2)   { $so.ShipToAddress.Address.Address2 = Trim-To "$($payload.ship_to_line2)" 39 }
    if ($payload.ship_to_city)    { $so.ShipToAddress.Address.City = Trim-To "$($payload.ship_to_city)" 25 }
    if ($payload.ship_to_state)   { $so.ShipToAddress.Address.State = Trim-To "$($payload.ship_to_state)" 4 }
    if ($payload.ship_to_zip)     { $so.ShipToAddress.Address.Zip = Trim-To "$($payload.ship_to_zip)" 12 }
    if ($payload.ship_to_country) { $so.ShipToAddress.Address.Country = Trim-To "$($payload.ship_to_country)" 15 }

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
    $script:soList = $null   # new SO - invalidate the cached list
    return "$($so.ReferenceNumber)"
}

# ---- assembly support ----
# BOM sources (in order of preference):
#   bomlist.csv = Sage "Bill of Materials" list export — one row per component
#                 per revision; covers every assembly, built or not. The
#                 highest Revision Number wins.
#   bom.csv     = Sage "Inventory Assemblies Journal" export — only assemblies
#                 that were actually built in the export window; per-unit
#                 component qty = Quantity / Quantity Built.
# Returns @{ assemblyId -> @{ componentId -> qty }, "<id>_rev" -> revision }.
function Get-BomMap {
    $map = @{}
    $listPath = Join-Path $PSScriptRoot 'bomlist.csv'
    $jrnlPath = Join-Path $PSScriptRoot 'bom.csv'

    if (Test-Path $listPath) {
        Import-Csv $listPath | ForEach-Object {
            $aid = "$($_.'Assembly ID')".Trim().ToUpper()
            $rev = [int]"$($_.'Revision Number')"
            $cid2 = "$($_.'Component ID')".Trim().ToUpper()
            $cqty = [decimal]"$($_.'Component Qty Needed')"
            if (-not $aid -or -not $cid2) { return }
            if ($map.ContainsKey("${aid}_revnum") -and $map["${aid}_revnum"] -gt $rev) { return }
            if (-not $map.ContainsKey("${aid}_revnum") -or $map["${aid}_revnum"] -lt $rev) {
                $map[$aid] = @{}
                $map["${aid}_revnum"] = $rev
                $map["${aid}_rev"] = "$rev"
            }
            $map[$aid][$cid2] = $cqty
        }
        return $map
    }

    if (-not (Test-Path $jrnlPath)) { return $null }
    Import-Csv $jrnlPath | ForEach-Object {
        $aid = "$($_.'Assembly Item ID')".Trim().ToUpper()
        $built = [decimal]"$($_.'Quantity Built')"
        $cqty = [decimal]"$($_.Quantity)"
        $cid2 = "$($_.'Component Item ID')".Trim().ToUpper()
        if (-not $aid -or $built -eq 0 -or -not $cid2) { return }
        if (-not $map.ContainsKey($aid)) { $map[$aid] = @{} }
        $map[$aid][$cid2] = $cqty / $built
        $map["${aid}_rev"] = "$($_.'Revision Number Used')"
    }
    return $map
}

# Tag open_orders lines whose item is a Sage assembly item.
function Set-AssemblyFlags([string]$orderKey, $asmIds) {
    if (-not $asmIds.Count) { return }
    $uri = "$SUPABASE_URL/rest/v1/open_orders`?order_key=eq.$orderKey&select=lines"
    $row = (Invoke-RestMethod -Uri $uri -Headers (Headers) -TimeoutSec 20) | Select-Object -First 1
    if (-not $row) { return }
    $changed = $false
    foreach ($l in $row.lines) {
        if ($asmIds.ContainsKey("$($l.item_id)".Trim().ToUpper())) {
            $l | Add-Member -NotePropertyName assembly -NotePropertyValue $true -Force
            $changed = $true
        }
    }
    if ($changed) {
        $body = @{ lines = @($row.lines) } | ConvertTo-Json -Depth 8 -Compress
        Invoke-RestMethod -Uri "$SUPABASE_URL/rest/v1/open_orders`?order_key=eq.$orderKey" `
            -Method Patch -Headers (Headers) -Body $body -TimeoutSec 20 | Out-Null
    }
}

# Append a build to assembly_builds.csv in Sage's Assemblies Journal import
# layout (importable fields only, export order). Returns a warning string or $null.
# File writes can hit transient locks (OneDrive sync, AV scans) - retry.
function Write-WithRetry($path, $lines, [switch]$Overwrite) {
    for ($i = 0; $i -lt 5; $i++) {
        try {
            if ($Overwrite) { Set-Content $path $lines }
            else { Add-Content $path $lines }
            return
        } catch {
            Write-Host "  [.] write retry $($i+1): $($_.Exception.GetType().Name): $($_.Exception.Message)" -ForegroundColor DarkYellow
            if ($i -eq 4) { throw }
            Start-Sleep -Seconds 2
        }
    }
}

# Read an item's on-hand quantity. It's a METHOD - QuantityOnHand(asOf) -
# not a property (confirmed via type probe: StockItem/AssemblyItem both
# expose QuantityOnHand(DateTime) and QuantityAvailable(DateTime)).
function Get-ItemOnHand($company, [string]$itemId, $refCache) {
    $key = Resolve-Ref $company.Factories.InventoryItemFactory $itemId $refCache.Item
    $item = $company.Factories.InventoryItemFactory.Load($key)
    try { return [decimal]$item.QuantityOnHand((Get-Date)) } catch { }
    try { return [decimal]$item.QuantityAvailable((Get-Date)) } catch { }
    return $null
}

# Flip a packing row's status (e.g. back to 'open' when a build/invoice
# is blocked, or 'complete' once the invoice posts).
function Set-OrderStatus($orderKey, [string]$status) {
    $body = @{ status = $status; updated_at = 'now()' } | ConvertTo-Json
    Invoke-RestMethod -Uri "$SUPABASE_URL/rest/v1/open_orders`?order_key=eq.$orderKey" `
        -Method Patch -Headers (Headers) -Body $body -TimeoutSec 20 | Out-Null
}

# Write/clear a stock warning on an open_orders line so the app can
# show it next to the packed quantity.
function Set-LineWarning($orderKey, [string]$itemId, [string]$msg) {
    $uri = "$SUPABASE_URL/rest/v1/open_orders`?order_key=eq.$orderKey&select=lines"
    $row = (Invoke-RestMethod -Uri $uri -Headers (Headers) -TimeoutSec 20) | Select-Object -First 1
    if (-not $row) { return }
    $changed = $false
    foreach ($l in $row.lines) {
        if ("$($l.item_id)".Trim().ToUpper() -eq $itemId.Trim().ToUpper()) {
            if ($msg) { $l | Add-Member -NotePropertyName stock_warning -NotePropertyValue $msg -Force }
            elseif ($l.PSObject.Properties['stock_warning']) { $l.PSObject.Properties.Remove('stock_warning') }
            $changed = $true
        }
    }
    if ($changed) {
        $body = @{ lines = @($row.lines) } | ConvertTo-Json -Depth 8 -Compress
        Invoke-RestMethod -Uri "$SUPABASE_URL/rest/v1/open_orders`?order_key=eq.$orderKey" `
            -Method Patch -Headers (Headers) -Body $body -TimeoutSec 20 | Out-Null
    }
}

# Lazily-loaded set of assembly item IDs (per sweep).
$script:asmIds = $null
function Get-AssemblyIds($company) {
    if (-not $script:asmIds) {
        $script:asmIds = @{}
        $al = $company.Factories.AssemblyItemFactory.List()
        $al.Load()
        $al | ForEach-Object { $script:asmIds["$($_.ID)".Trim().ToUpper()] = $true }
    }
    return $script:asmIds
}

# Check packed ASSEMBLY lines against the item's on-hand. If a build hasn't
# posted yet (import still running, or skipped for components) the assembly
# item won't cover the shipped qty - the caller holds the invoice for the
# next sweep instead of letting Sage drive it negative.
function Test-InvoiceStock($company, $payload, $refCache) {
    $asm = Get-AssemblyIds $company
    $short = @()
    foreach ($l in $payload.lines) {
        $id = "$($l.item_id)".Trim().ToUpper()
        if (-not $asm.ContainsKey($id)) { continue }
        $need = [decimal]$l.quantity
        if ($need -le 0) { continue }
        try { $onh = Get-ItemOnHand $company $id $refCache } catch { $onh = $null }
        if ($null -ne $onh -and $onh -lt $need) {
            $short += "${id}: need $($need.ToString('0.##')), on-hand $($onh.ToString('0.##'))"
        }
    }
    return $short
}

# Check BOM components against on-hand stock BEFORE writing build rows.
# Returns a list of shortfall descriptions ('item: need X, have Y').
function Test-AssemblyStock($company, $lines, $bom, $refCache) {
    $short = @()
    foreach ($l in $lines) {
        $aid = "$($l.item_id)".Trim().ToUpper()
        $comps = if ($bom) { $bom[$aid] } else { $null }
        if (-not $comps -or [decimal]$l.qty -le 0) { continue }
        foreach ($c in $comps.GetEnumerator()) {
            $need = $c.Value * [decimal]$l.qty
            try { $onh = Get-ItemOnHand $company $c.Key $refCache } catch { $onh = $null }
            if ($null -ne $onh -and $onh -lt $need) {
                $short += "$($c.Key): need $($need.ToString('0.##')), have $($onh.ToString('0.##'))"
            }
        }
    }
    return $short
}

function Write-AssemblyBuild($lines, $bom, $parentKey = '') {
    # ASM_BUILDS_CSV in .env can relocate the staging file (e.g. off the
    # Desktop if a security product keeps blocking writes there).
    $csv = if ($env:ASM_BUILDS_CSV) { $env:ASM_BUILDS_CSV } else { Join-Path $PSScriptRoot 'assembly_builds.csv' }
    $header = 'Assembly Item ID,Reference,Date,Reason to Build,Quantity Built,Number of distributions,Quantity,Component Item ID,Revision Number Used'
    if (-not (Test-Path $csv)) { Write-WithRetry $csv $header -Overwrite }
    $rows = @()
    $missing = @()
    foreach ($l in $lines) {
        $aid = "$($l.item_id)".Trim().ToUpper()
        $qty = [decimal]$l.qty
        $comps = if ($bom) { $bom[$aid] } else { $null }
        if (-not $comps -or $qty -le 0) { $missing += $aid; continue }
        $rev = "$($bom["${aid}_rev"])"
        $ref = if ($parentKey) { 'SOB-' + $parentKey.Substring(0, [Math]::Min(8, $parentKey.Length)) } else { 'SO-BUILD' }
        foreach ($c in $comps.GetEnumerator()) {
            $cq = $c.Value * $qty
            $rows += "$aid,$ref,$(Get-Date -Format 'M/d/yyyy'),Packed SO build,$qty,$($comps.Count),$cq,$($c.Key),$rev"
        }
    }
    if ($rows) { Write-WithRetry $csv $rows }
    if ($missing) { return "no BOM for: $($missing -join ', ') - build manually" }
    return $null
}

# Queue +/- on-hand adjustments for the Inventory Adjustments Journal import.
# Field order must match the Sage template's checked fields exactly.
function Write-InventoryAdjust($lines) {
    # INV_ADJ_CSV in .env can relocate the staging file.
    $csv = if ($env:INV_ADJ_CSV) { $env:INV_ADJ_CSV } else { Join-Path $PSScriptRoot 'inventory_adjustments.csv' }
    $header = 'Item ID,Reference,Date,GL Acct-Cost,Quantity,Unit Cost,Reason to Adjust'
    if (-not (Test-Path $csv)) { Write-WithRetry $csv $header -Overwrite }
    $rows = @()
    foreach ($l in $lines) {
        $qty = [decimal]$l.qty
        if ($qty -eq 0) { continue }
        $reason = ("$($l.reason)" -replace ',', ';')
        $rows += "$($l.item_id),ADJ,$(Get-Date -Format 'M/d/yyyy'),,$qty,,$reason"
    }
    if ($rows) { Write-WithRetry $csv $rows }
    return $rows.Count
}

# Delete a sales order in Sage (app-side delete button queues this).
# Returns a warning string or $null.
# Sales-order list is 40k+ rows - load once per sweep, lazily.
# Callers that create or delete an SO must set $script:soList = $null
# afterwards so the cache doesn't serve stale data to later jobs.
$script:soList = $null

# Line references stringify as EntityReference<...>:Key(N) - not the item ID.
# Keep the loaded item list and resolve each line's reference via GetItemByKey.
$script:itemList = $null
function Get-ItemKeyMap($company) {
    if ($null -eq $script:itemList) {
        Write-Host "  [.] loading item list..." -ForegroundColor DarkGray
        $script:itemList = $company.Factories.InventoryItemFactory.List()
        $script:itemList.Load()
        Write-Host "  [.] item list loaded ($($script:itemList.Count))" -ForegroundColor DarkGray
    }
    return $script:itemList
}

function Get-LineItemId($line, $company, $list) {
    try { $iid = "$($line.InventoryItemReference.Key.ID)"; if ($iid) { return $iid } } catch { }
    try { $iid = "$($line.InventoryItemReference.ID)";     if ($iid) { return $iid } } catch { }
    try {
        $item = $company.Factories.InventoryItemFactory.Load($line.InventoryItemReference)
        if ($item -and $item.ID) { return "$($item.ID)" }
    } catch { }
    try {
        $hit = $list.GetItemByKey($line.InventoryItemReference)
        if ($hit) { return "$($hit.ID)" }
    } catch { }
    return "$($line.InventoryItemReference)"
}

function Get-SoList($company) {
    if ($null -eq $script:soList) {
        Write-Host "  [.] loading SO list..." -ForegroundColor DarkGray
        $script:soList = $company.Factories.SalesOrderFactory.List()
        try {
            # only open SOs: closed/invoiced ones read as "missing", which is
            # what every consumer (delete/update/invoice/close-missing) wants
            $fe   = [Sage.Peachtree.API.Collections.Generic.FilterExpression]
            $flt  = $fe::Equal($fe::Property('SalesOrder.IsClosed'), $fe::Constant([bool]$false))
            $mods = [Sage.Peachtree.API.Collections.Generic.LoadModifiers]::Create()
            $mods.Filters = $flt
            $script:soList.Load($mods)
            Write-Host "  [.] SO list loaded ($($script:soList.Count) open)" -ForegroundColor DarkGray
        } catch {
            Write-Host "  [!] filtered load failed ($_); loading all" -ForegroundColor Yellow
            $script:soList = $company.Factories.SalesOrderFactory.List()
            $script:soList.Load()
            Write-Host "  [.] SO list loaded ($($script:soList.Count))" -ForegroundColor DarkGray
        }
    }
    return $script:soList
}

function Remove-SalesOrder($company, [string]$soNo) {
    $hit = (Get-SoList $company) | Where-Object { "$($_.ReferenceNumber)" -eq $soNo } | Select-Object -First 1
    if (-not $hit) { return "SO $soNo not in Sage (already deleted or invoiced)" }
    $so = $company.Factories.SalesOrderFactory.Load($hit.Key)
    if (-not $so.IsDeleteAllowed) { throw "Sales order $soNo cannot be deleted" }
    $so.Delete()
    $script:soList = $null   # list changed - force reload
    return $null
}

# Auto-close packing rows whose SO disappeared from Sage (deleted or invoiced).
function Close-MissingOrders($company) {
    $uri = "$SUPABASE_URL/rest/v1/open_orders`?status=in.(open,short)&sage_order_no=not.is.null&select=order_key,sage_order_no"
    $rows = Get-Rows $uri
    if (-not $rows.Count) { return }
    $existing = @{}
    (Get-SoList $company) | ForEach-Object { $existing["$($_.ReferenceNumber)"] = $true }
    foreach ($r in $rows) {
        if (-not $existing.ContainsKey("$($r.sage_order_no)")) {
            $body = @{ status = 'complete'; updated_at = 'now()' } | ConvertTo-Json
            Invoke-RestMethod -Uri "$SUPABASE_URL/rest/v1/open_orders`?order_key=eq.$($r.order_key)" `
                -Method Patch -Headers (Headers) -Body $body -TimeoutSec 20 | Out-Null
            Write-Host "[~] open order closed - SO $($r.sage_order_no) no longer in Sage" -ForegroundColor DarkYellow
        }
    }
}

# Create a sales invoice from a sales order using the packed (shipped)
# quantities. AddSalesOrderLine() links each invoice line back to its SO
# line - Sage ships that quantity and closes the SO when fully shipped.
# Freight is left 0: shipping costs are added in Sage later.
function New-SalesInvoice($company, [string]$soNo, $packLines) {
    $hit = (Get-SoList $company) | Where-Object { "$($_.ReferenceNumber)" -eq $soNo } | Select-Object -First 1
    if (-not $hit) { throw "Sales order '$soNo' not found (already invoiced or deleted)" }
    $so = $company.Factories.SalesOrderFactory.Load($hit.Key)

    $want = @{}
    foreach ($l in $packLines) { $want["$($l.item_id)".Trim().ToUpper()] = [decimal]$l.quantity }

    $inv = $company.Factories.SalesInvoiceFactory.Create()
    $inv.CustomerReference = $so.CustomerReference
    $inv.CustomerPurchaseOrderNumber = $so.CustomerPurchaseOrderNumber
    $inv.Date = Get-Date
    try { $inv.ShipDate = Get-Date } catch { }
    if ($so.ShipVia) { $inv.ShipVia = $so.ShipVia }
    try { $inv.TermsDescription = $so.TermsDescription } catch { }
    try { $inv.FreightAmount = 0 } catch { }
    try { $inv.SalesTaxCodeReference = $so.SalesTaxCodeReference } catch { }
    try {
        $inv.ShipToAddress.Name = $so.ShipToAddress.Name
        $inv.ShipToAddress.Address.Address1 = $so.ShipToAddress.Address.Address1
        $inv.ShipToAddress.Address.Address2 = $so.ShipToAddress.Address.Address2
        $inv.ShipToAddress.Address.City = $so.ShipToAddress.Address.City
        $inv.ShipToAddress.Address.State = $so.ShipToAddress.Address.State
        $inv.ShipToAddress.Address.Zip = $so.ShipToAddress.Address.Zip
        $inv.ShipToAddress.Address.Country = $so.ShipToAddress.Address.Country
    } catch { }

    $shipped = 0
    $seen = @()
    $map = Get-ItemKeyMap $company
    foreach ($soLn in $so.SalesOrderLines) {
        $iid = Get-LineItemId $soLn $company $map
        $seen += $iid
        $qty = $null
        foreach ($k in $want.Keys) {
            if ($iid.Trim().ToUpper() -match [regex]::Escape($k) -or $k -match [regex]::Escape($iid.Trim().ToUpper())) {
                $qty = $want[$k]
            }
        }
        if ($null -eq $qty -or $qty -le 0) { continue }
        $invLn = $inv.AddSalesOrderLine($soLn)
        $invLn.Quantity = [decimal]$qty
        $invLn.Amount = [decimal]$qty * [decimal]$invLn.UnitPrice
        $shipped++
    }
    if (-not $shipped) { throw "No invoice lines matched the packed items (SO lines: $($seen -join ', '); wanted: $($want.Keys -join ', '))" }
    try { $inv.ReferenceNumber = "$($company.Services.ReferenceNumber.TakeNextInvoiceNumber())" } catch { }
    $inv.Save()
    $script:soList = $null   # SO may have closed - invalidate the list
    $refNo = "$($inv.ReferenceNumber)"
    if (-not $refNo) {
        try { $refNo = "$($company.Factories.SalesInvoiceFactory.Load($inv.Key).ReferenceNumber)" } catch { }
    }
    return $refNo
}

function Update-SalesOrder($company, [string]$soNo, $lines) {
    $hit = (Get-SoList $company) | Where-Object { "$($_.ReferenceNumber)" -eq $soNo } | Select-Object -First 1
    if (-not $hit) { throw "Sales order '$soNo' not found" }
    $so = $company.Factories.SalesOrderFactory.Load($hit.Key)
    $want = @{}
    foreach ($l in $lines) { $want["$($l.item_id)"] = [decimal]$l.quantity }
    $seen = @()
    $matched = 0
    $map = Get-ItemKeyMap $company
    foreach ($ln in $so.SalesOrderLines) {
        $iid = Get-LineItemId $ln $company $map
        $seen += $iid
        foreach ($k in $want.Keys) {
            if ($iid -match [regex]::Escape($k) -or $k -match [regex]::Escape($iid)) {
                $ln.Quantity = $want[$k]
                $matched++
            }
        }
    }
    $so.Save()
    if (-not $matched) { return "no SO lines matched (SO lines: $($seen -join ', '); wanted: $($want.Keys -join ', '))" }
    return $null
}

Write-Host "Sage bridge v3 polling every $POLL_SECONDS s - Ctrl+C to stop"
$sess = $null
$idleSweeps = 0
while ($true) {
    # Keep the Sage session open across sweeps - the begin/request/open
    # handshake is the slow part. Reopen only after a failure.
    try {
        if (-not $sess) {
            $sess = Open-SageCompany
            Write-Host "  [.] sage open OK" -ForegroundColor DarkGray
        }
        $refCache = @{ Customer = @{}; Item = @{}; Account = @{} }
        $bom = Get-BomMap
        $script:asmIds = $null
        $script:soList = $null   # fresh view per sweep - drop stale cached list
        $script:itemList = $null
        $pendingRows = @(Get-Pending)
        if ($pendingRows.Count) {
            Write-Host "[.] sweep $(Get-Date -Format 'HH:mm:ss') - pending rows: $($pendingRows.Count)" -ForegroundColor DarkGray
            $idleSweeps = 0
        } else {
            $idleSweeps++
            if ($idleSweeps % 40 -eq 1) { Write-Host "[.] idle heartbeat $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor DarkGray }
        }
        foreach ($row in $pendingRows) {
            $key = $row.order_key
            Write-Host "  [.] claim $key ($($row.job_type))..." -ForegroundColor DarkGray
            if (-not (Claim-Order $key)) { Write-Host "  [.] claim skipped (0 rows updated)" -ForegroundColor DarkGray; continue }
            try {
                if ($row.job_type -eq 'qty_update') {
                    $warn = Update-SalesOrder $sess.Company "$($row.payload.sage_order_no)" $row.payload.lines
                    Finish-Order $key 'done' $null $warn
                    Write-Host "[+] $($key.Substring(0,8)) -> updated SO $($row.payload.sage_order_no) quantities"
                } elseif ($row.job_type -eq 'build_assembly') {
                    $short = Test-AssemblyStock $sess.Company $row.payload.lines $bom $refCache
                    if ($short) {
                        $msg = "insufficient components - lower Packed qty or build in Sage: $($short -join '; ')"
                        Finish-Order $key 'done' $null $msg
                        foreach ($l in $row.payload.lines) {
                            Set-LineWarning $row.payload.parent_order "$($l.item_id)" $msg
                        }
                        try { Set-OrderStatus $row.payload.parent_order 'open' } catch { }
                        Write-Host "[!] $($key.Substring(0,8)) skipped - $msg" -ForegroundColor Yellow
                    } else {
                        $warn = Write-AssemblyBuild $row.payload.lines $bom "$($row.payload.parent_order)"
                        Finish-Order $key 'done' $null $warn
                        foreach ($l in $row.payload.lines) {
                            Set-LineWarning $row.payload.parent_order "$($l.item_id)" $null
                        }
                        $script:asmNeedsImport = $true
                        Write-Host "[+] $($key.Substring(0,8)) -> assembly build row(s) added to assembly_builds.csv$(if($warn){" ($warn)"})"
                    }
                } elseif ($row.job_type -eq 'create_invoice') {
                    $short = Test-InvoiceStock $sess.Company $row.payload $refCache
                    if ($short) {
                        # build not posted yet (or skipped) - hold for next sweep
                        Finish-Order $key 'pending' $null "waiting on assembly stock: $($short -join '; ')"
                        try { Set-OrderStatus $row.payload.parent_order 'open' } catch { }
                        Write-Host "[~] $($key.Substring(0,8)) held - $($short -join '; ')" -ForegroundColor DarkYellow
                    } else {
                        $invNo = New-SalesInvoice $sess.Company "$($row.payload.sage_order_no)" $row.payload.lines
                        Finish-Order $key 'done' $invNo
                        try { Set-OrderStatus $row.payload.parent_order 'complete' } catch { }
                        Write-Host "[+] $($key.Substring(0,8)) -> Sage invoice $invNo (from SO $($row.payload.sage_order_no))"
                    }
                } elseif ($row.job_type -eq 'adjust_inventory') {
                    # Guard: a negative delta can't remove more than on-hand.
                    $short = @()
                    foreach ($l in $row.payload.lines) {
                        $need = -[decimal]$l.qty
                        if ($need -le 0) { continue }
                        try { $onh = Get-ItemOnHand $sess.Company "$($l.item_id)" $refCache } catch { $onh = $null }
                        if ($null -ne $onh -and $onh -lt $need) { $short += "$($l.item_id): removing $need, on-hand $onh" }
                    }
                    if ($short) { throw "insufficient on-hand: $($short -join '; ')" }
                    Write-InventoryAdjust $row.payload.lines | Out-Null
                    Finish-Order $key 'done'
                    $script:adjNeedsImport = $true
                    Write-Host "[+] $($key.Substring(0,8)) -> inventory adjustment row(s) queued"
                } elseif ($row.job_type -eq 'void_order') {
                    $warn = Remove-SalesOrder $sess.Company "$($row.payload.sage_order_no)"
                    Finish-Order $key 'done' $null $warn
                    Write-Host "[+] $($key.Substring(0,8)) -> deleted Sage SO $($row.payload.sage_order_no)$(if($warn){" ($warn)"})"
                } else {
                    $soNo = New-SalesOrder $sess.Company $row.payload $refCache
                    Finish-Order $key 'done' $soNo
                    Record-SoNumber "$($row.payload.order_key)" $soNo
                    if (-not $script:asmIds) {
                        $script:asmIds = @{}
                        $al = $sess.Company.Factories.AssemblyItemFactory.List()
                        $al.Load()
                        $al | ForEach-Object { $script:asmIds["$($_.ID)".Trim().ToUpper()] = $true }
                    }
                    try {
                        Set-AssemblyFlags "$($row.payload.order_key)" $script:asmIds
                    } catch { Write-Host "  [!] assembly flag failed: $_" -ForegroundColor Yellow }
                    Write-Host "[+] $($key.Substring(0,8)) -> Sage sales order $soNo"
                }
            } catch {
                Finish-Order $key 'error' $null "$_"
                Write-Host "[!] $($key.Substring(0,8)) FAILED: $_" -ForegroundColor Red
                # Failed job on a known packing order - put it back in the list
                if ($row.payload.parent_order) {
                    try { Set-OrderStatus $row.payload.parent_order 'open' } catch { }
                }
            }
        }
        try { Close-MissingOrders $sess.Company } catch {
            Write-Host "  [!] missing-order check failed: $_" -ForegroundColor Yellow
        }
        # On-change import: launch the AutoIt importer once per sweep if any
        # build rows were written. The 5-min scheduled task remains as a
        # backstop for runs that fail (Sage busy, dialogs, etc).
        if ($script:asmNeedsImport) {
            $script:asmNeedsImport = $false
            $imp = Join-Path $PSScriptRoot 'import_assemblies.ps1'
            if (Test-Path $imp) {
                Write-Host "  [.] launching assembly import" -ForegroundColor DarkGray
                Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$imp`"" -WorkingDirectory $PSScriptRoot
            }
        }
        if ($script:adjNeedsImport) {
            $script:adjNeedsImport = $false
            $imp = Join-Path $PSScriptRoot 'import_adjustments.ps1'
            if (Test-Path $imp) {
                Write-Host "  [.] launching adjustments import" -ForegroundColor DarkGray
                Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$imp`"" -WorkingDirectory $PSScriptRoot
            }
        }
    } catch {
        Write-Host "[!] sweep failed: $_" -ForegroundColor Red
        # Session may be broken - reopen fresh next sweep
        try { if ($sess) { $sess.Session.End() } } catch { }
        $sess = $null
    }
    Start-Sleep -Seconds $POLL_SECONDS
}
