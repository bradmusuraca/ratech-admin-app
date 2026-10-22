# Ratech → Sage 50 Bridge

Runs on the Windows PC where **Sage 50 US** is installed. Polls a
Supabase queue table and creates sales orders through the
**Sage.Peachtree.API managed .NET assembly** that ships with Sage —
no SDK install, no COM registration, no Python.

## One-time setup

1. **Supabase**: create a free project, run `supabase_setup.sql` in the
   SQL Editor, copy the project URL + service-role key.
2. `copy .env.example .env` and fill in `SUPABASE_URL`, `SUPABASE_KEY`,
   and `SAGE_COMPANY_NAME` (a substring of the company name — the probe
   lists what's available).
3. Run the probe in **32-bit PowerShell** (Sage is 32-bit):

   ```
   C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File .\probe.ps1
   ```

   It lists companies, checks app authorization (Sage may prompt inside
   the app to allow "Ratech.OrderBridge" the first time), and samples
   customers/items — all read-only.
4. Run the bridge the same way:

   ```
   C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File .\bridge.ps1
   ```

   Leave running, or register as a Scheduled Task at logon.

## Flow

App pushes a validated order → queue row `status=pending` → bridge claims
it (`processing`), posts the sales order via `SalesOrderFactory.Create()`,
writes `done` + the Sage order number — or `error` + the exception.
`order_key` is a hash of the order contents, so re-sending the same batch
can never double-post.

Sage does not need to be open, but the company must have been opened at
least once. The bridge opens its own session each sweep.

## Assembly builds (headless import)

Packed quantities on assembly lines queue `build_assembly` jobs; the
bridge appends them to `assembly_builds.csv` in the Inventory Assemblies
Journal format, using `bom.csv` (a Sage BOM export) for components.

To import them without manual File → Import:

1. Install **AutoIt** on the Sage PC (autoitscript.com).
2. In Sage UI, open File → Select Import/Export → Inventory →
   **Inventory Assemblies Journal** → Import. On the Fields tab check the
   nine fields matching `assembly_pending.csv`; on the Options tab set the
   filename to `...\sage_bridge\assembly_pending.csv`. Sage remembers this.
3. Schedule `import_assemblies.ps1` (Task Scheduler, every 5–15 min).
   It stages new build rows, drives the Sage import via
   `assembly_import.au3`, and archives to `imported\` on success.
   Requires Sage to be running with the company open; safe to run
   when nothing is pending. Log: `assembly_import.log`.

## Inventory adjustments (headless import)

Adjustments queued from the app's Inventory page are written to
`inventory_adjustments.csv` and imported via `import_adjustments.ps1`
(same AutoIt driver, `Adjustment` template needle).

Two extra pieces:

- The adjustments import template needs the fields in
  `inventory_adjustments_pending.csv` order, including the required
  `Number of Distributions` (always 1) and `Amount` columns.
- `Amount` is filled from `itemlist.csv` — a Sage **Item List** export
  (File → Select Import/Export → Inventory Items → Item List → Export,
  fields Item ID + Unit Cost, saved to `C:\sage_bridge\itemlist.csv`).
  Blank Amount posts as 0, so re-export it when costs change.
- Set `INV_ADJ_GL_ACCT` in `.env` to the GL source account.

## Python variant (optional)

`sage_client.py` / `bridge.py` / `probe.py` target the older COM
(`PeachwServer`) interface and are kept for reference; the PowerShell
managed-API scripts above are the supported path.


## Flow

App pushes a validated order → queue row `status=pending` → bridge claims
it (`processing`), posts the sales order via COM, writes `done` + the Sage
order number — or `error` + the exception. `order_key` is a hash of the
order contents, so re-sending the same batch can never double-post.

Sage does not need to be open, but the company must have been opened at
least once. The bridge opens its own session each sweep.
