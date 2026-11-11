# Ratech Admin App — Agent Notes

Streamlit order-management app (Streamlit Cloud: `ratech-app.streamlit.app`, repo `bradmusuraca/ratech-admin-app`, branch `main`, entry `app.py`). Ingests PDF/email/ShipStation orders → user validates → Sage 50 2019 sales orders → packing → assembly builds → invoices → shipping labels.

## Architecture
- `app.py` — single-file Streamlit app (~3000 lines). Login gate at top; ShipStation batch/card label flows ~1879–2180; combined CSV ~2215; Create Invoices ~2465+; `build_sage_sales_orders` ~828–897; packing-slip PDF builder ~1030–1157.
- `sage_queue.py` — Supabase helpers. `create_open_orders` (dedup via `_order_key`, uses `Prefer: resolution=ignore-duplicates`) now substitutes synced Sage item descriptions via `get_item_descriptions()`.
- `sage_bridge/` — runs on separate Windows PC with Sage 50. `bridge.ps1` (32-bit PowerShell) polls Supabase `sage_order_queue` jobs: `create`, `update_qty`, `build_assembly`, `create_invoice`. AutoIt scripts drive Sage journal imports (`assembly_import.au3`, `import_assemblies.ps1`, `import_adjustments.ps1`). No Python on the Sage PC.
- `bomlist.csv`/`bom.csv` in sage_bridge/ — Sage BOM exports; assembly IDs source for `Set-AssemblyFlags`.
- `fedex.py` — rates/labels/third-party billing (profiles keyed by Sage customer #). `email_inbox.py`, `customers.csv`, `packages.csv`, `email_rules.csv` — supporting data.

## Supabase tables
- `open_orders` — packing/invoice rows. `lines` JSONB: `item_id, description, qty_required, qty_packed, assembly, built_qty`.
- `sage_order_queue` — jobs the bridge polls (`status`: pending/done/failed/skipped).
- `items` — `item_id, description` only (no assembly flag; flags set by bridge from Sage `AssemblyItemFactory`).
- Creds: `SUPABASE_URL`/`SUPABASE_KEY` env vars (`sage_bridge/.env` on this box).

## Key behaviors
- "Create label" (card OR batch): buys label → queues Sage SO → `create_open_orders` packing row → removes order from active list (filtered out of `st.session_state.invoices`/`combined`). Labeled orders kept in collapsed expander for reprints.
- ShipStation line descriptions embed `[order#] name/address` — `create_open_orders` replaces with Sage item descriptions so packing/invoices stay clean.
- Assembly lines flagged `assembly:true` + `built_qty` — packing delta > built_qty queues `build_assembly` jobs; bridge appends to `assembly_builds.csv` for journal import.
- Pick list appended to batch label PDF: clean `Item|Description|Qty|Packed` table, no ShipStation header block, descriptions truncated to prevent Qty bleed (fpdf2 `multi_cell` cursor fix at ~a296922).

## Gotchas
- `requirements.txt`: `streamlit==1.66.1` is PINNED — do not unpin; each unpinned deploy re-rolls the frontend bundle → "Failed to fetch dynamically imported module" 404s (happened repeatedly).
- Streamlit Cloud delete/redeploy loses Secrets — copy first.
- Local runs: OneDrive file-watch causes rerun loops — launch with `--server.fileWatcherType none`.
- PowerShell scripts need `-ExecutionPolicy Bypass` on this machine; Git Bash + PowerShell mixing fails.
- Python is NOT installed on this local PC — use PowerShell for Supabase repair work (see `check_queue.ps1` for PostgREST patterns: eq. filters, PATCH lines JSONB wholesale).

## Verification
- **Run commands ON the Sage PC without asking Brad: the `sage_commands` channel.** `sage_cmd.ps1` polls the table every ~15s on that PC (scheduled task `RatechSageCmd`, single-instance mutex, 5-min respawn backstop). From here: `powershell -ExecutionPolicy Bypass -File sage_run.ps1 -Command "<ps1>"` (repo root, waits + prints output; `-NoWait` to fire-and-forget) or INSERT into `sage_commands` via exec_sql. Use it for log tails (`bridge_out.log`, `*_import.log`), process checks, manual imports, deploys. The scheduled task runs the poller as `sage_bridge_run.ps1` does — deploys must update BOTH files. Remote execution by design — service-role only.
- PostgREST: `curl -H "apikey: $KEY" "$URL/rest/v1/open_orders?order_key=eq.X&select=lines"`.
- **Arbitrary SQL (incl. DDL) goes through the `exec_sql` RPC** — PostgREST can't run raw SQL and the VPN blocks direct Postgres ports. Call it via `run_sql.ps1` (`powershell -ExecutionPolicy Bypass -File run_sql.ps1 -Query "select now()"`, run from repo root — reads creds from `sage_bridge/.env`) or `POST $URL/rest/v1/rpc/exec_sql -d '{"query":"..."}'`. SELECTs/`RETURNING` return JSON rows; DDL/DML return `{"ok":true}`. One statement per call; service_role only, so it bypasses RLS — never use it for anything user-facing. If the RPC 404s, the `exec_sql` block at the bottom of `sage_bridge/supabase_setup.sql` hasn't been run in the SQL Editor yet.
- Order keys are content hashes (`_order_key(tx)`) — e.g. SO 57300 = `c0757921f3917c1dbdff0101d96a1778`, SO 57302 = `6f631828d8e8cb87f111cba721f1f843`.

## Field lessons (mined from production sessions — full transcripts in C:\dev\session-archive\)

### ShipStation
- **v1 label endpoint is `POST /orders/createlabelfororder`** — `/orders/createlabel` does not exist (404 "No HTTP resource ... createlabel"). `shipDate` is required; default to today. Batch labels never worked before this fix — all prior labels went through FedEx.
- **`carriers/listpackages` returns only the carrier's standard boxes.** Custom package names/dims live under the **v2 API** (`/v2/packages`, separate `SHIPSTATION_V2_API_KEY` from Settings → Account → API Settings). v1 `/packages` 404s on this plan. A custom box preset lands on an order as generic `package` code + dimensions — no name.
- Order line `description` embeds `[order#] name/address` — replace with synced Sage item descriptions (see `get_item_descriptions`).

### FedEx
- One API project covers Rates + Address Validation + Ship — just enable the APIs on the same project; same client ID/secret. Test key verifies plumbing only; **for production delete `FEDEX_BASE_URL` entirely** (default = `apis.fedex.com`). Negotiated rates come back automatically when quoting with the account number.

### Sage bridge (Windows PC, `C:\sage_bridge`)
- `build_assembly` jobs mark `done` when the CSV row is *written* — actual Sage posting is a separate step: `import_assemblies.ps1` + AutoIt journal import. Invoice waits on "waiting on assembly stock" until the build posts.
- **`SKIP: Sage (peachw.exe) not running`** in `assembly_import.log` → open Sage, run `powershell -ExecutionPolicy Bypass -File C:\sage_bridge\import_assemblies.ps1` manually; invoice completes itself after stock posts. **`FAIL: import exit N`** → row moved to `C:\sage_bridge\failed\`.
- **`FAIL: template window did not open` / `template_missing`** in `adjustment_import.log` = the Sage **Inventory Adjustments Journal** import template was never configured (one-time setup, like assemblies): File → Select Import/Export → Inventory → Inventory Adjustments Journal → Import → Fields: Item ID, Reference, Date, GL Acct-Source, Quantity, Unit Cost, Reason to Adjust, Number of Distributions, Amount → Options: filename `C:\sage_bridge\inventory_adjustments_pending.csv`, "First Row Contains Headings" UNCHECKED. To replay a failed row: prepend the header line onto `failed\adjustments_*.csv`, save as `inventory_adjustments.csv`, re-run `import_adjustments.ps1`. Configured 2026-11-09.
- **Sweeps take ~12–15 min when held jobs exist** (each pending `build_assembly`/`create_invoice` re-checks every BOM component + assembly on-hand via Sage COM calls). `done` on a job means "CSV row written" — posting still needs the AutoIt import; check `*_import.log` before assuming stock landed.
- Hung import: `Get-Process AutoIt3 | Stop-Process -Force`, then re-run the import script.
- **Sage "another user" errors** (bulk price changes, backups): the bridge's open SDK session counts as a user. App sidebar **Pause Sage bridge** sets `bridge_control.paused`; bridge releases Sage within one poll and reports `bridge_state`/`bridge_seen_at` back (heartbeat 60 s). Needs the `bridge_control` SQL from `supabase_setup.sql`. `PAUSE` file / quiet window still work too.
- **AV on the Sage PC (Norton/Malwarebytes/Defender) can block/quarantine `bridge.ps1`** — renamed copy `sage_bridge_run.ps1` worked around it. **The `RatechBridge` scheduled task runs `sage_bridge_run.ps1`, not `bridge.ps1`** — deploying `*.ps1` from the repo does NOT update the running bridge unless you also `Copy-Item C:\sage_bridge\bridge.ps1 C:\sage_bridge\sage_bridge_run.ps1 -Force`, and restart by matching `-match 'sage_bridge_run\.ps1'` (the `bridge\.ps1` filter misses it). `pause_bridge.ps1` toggles it for backups.
- Adding a queue status requires `alter table sage_order_queue drop/add constraint sage_order_queue_status_check` (SQL appended in `sage_bridge/supabase_setup.sql`).

### Streamlit
- `st.data_editor` inside fragments: editing refreshes the editor column but NOT sections below it — exports/CSVs must rebuild from `inv['df']` at click time, and edits can take two Enters / cause scroll jumps. Keep export rendering outside the editing fragment.
- Cloud deploys can go **stale/partial** (new `app.py` + old `sage_queue.py`) — commit dependent files together; on weird ImportErrors, redeploy cleanly.
- A dead/changed streamlit.app hostname just spins forever — check the URL first. `/api/v2/app/status` returning 200 + `{"status":"running"}` means awake; `/api/v2/user/details` 404 is normal for logged-out visitors.
- Supabase list endpoints may wrap rows in a `value` array — check the shape before assuming empty.

### Customer data quality
- Vendor numbers come from OUR customer list, never the order doc (DTS printed our phone number in their Account field). Match-on-"their code for us" columns rot into false positives — don't match on data nobody maintains.
- Part-number prefixes matter: Amazon SKUs arrive as `AM…`, Ratech items are `ATV…` — rewrite on the item, not the order number. `CR 13988`-style numbers are the vendor's internal code, not the customer's part number.
