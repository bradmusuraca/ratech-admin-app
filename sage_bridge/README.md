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
