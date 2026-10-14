# Ratech → Sage 50 Bridge

Runs on the Windows PC where **Sage 50 US 2019** is installed. Polls a
Supabase queue table and creates sales orders through the Sage 50 COM
API — no CSV import wizard needed.

## One-time setup

1. **Install the Sage 50 2019.2 SDK** on this PC (Sage KB 96384 — includes
   the PeachwServer COM interop and docs).
2. **Supabase**: create a free project, run `supabase_setup.sql` in the
   SQL Editor, copy the project URL + service-role key.
3. `pip install -r requirements.txt` (Python 3.8+; install pywin32 then run
   `python -m pywin32_postinstall` if COM registration complains).
4. `copy .env.example .env` and fill in `SUPABASE_URL`, `SUPABASE_KEY`,
   `SAGE_COMPANY_PATH` (the company folder path Sage uses to open it).
5. `python probe.py` — dumps the actual `PTOWSalesOrder` members. Compare
   against `FIELD_MAP` in `sage_client.py` and rename any that differ.
6. `python bridge.py` — leave running, or register as a Scheduled Task.

## Flow

App pushes a validated order → queue row `status=pending` → bridge claims
it (`processing`), posts the sales order via COM, writes `done` + the Sage
order number — or `error` + the exception. `order_key` is a hash of the
order contents, so re-sending the same batch can never double-post.

Sage does not need to be open, but the company must have been opened at
least once. The bridge opens its own session each sweep.
