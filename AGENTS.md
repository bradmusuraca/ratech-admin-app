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
- PostgREST: `curl -H "apikey: $KEY" "$URL/rest/v1/open_orders?order_key=eq.X&select=lines"`.
- Order keys are content hashes (`_order_key(tx)`) — e.g. SO 57300 = `c0757921f3917c1dbdff0101d96a1778`, SO 57302 = `6f631828d8e8cb87f111cba721f1f843`.
