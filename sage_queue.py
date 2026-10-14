"""Push validated orders to a Supabase queue table for the desktop
Sage bridge to post into Sage 50 via the COM API.

Uses Supabase's PostgREST endpoint directly (requests only — no
supabase-py dependency). Requires two env vars:

    SUPABASE_URL       e.g. https://xyzcompany.supabase.co
    SUPABASE_KEY       service-role key (bypasses RLS; keep secret)

Table: see sage_bridge/supabase_setup.sql
"""
import os
import hashlib
import json
import requests

TABLE = 'sage_order_queue'


def is_configured():
    return bool(os.getenv('SUPABASE_URL') and os.getenv('SUPABASE_KEY'))


def _headers():
    key = os.getenv('SUPABASE_KEY')
    return {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }


def _url(**params):
    base = os.getenv('SUPABASE_URL', '').rstrip('/')
    q = '&'.join(f'{k}={v}' for k, v in params.items())
    return f'{base}/rest/v1/{TABLE}' + (f'?{q}' if q else '')


def _order_key(tx):
    """Deterministic idempotency key: identical payloads hash identically,
    so re-sending the same validated batch can never double-post."""
    canonical = json.dumps({
        'cust': tx['customer_id'], 'po': tx['customer_po'],
        'date': tx['order_date'],
        'lines': [(l['item_id'], str(l['quantity']), str(l['unit_price']))
                  for l in tx['lines']],
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def transactions_from_sage_df(sage_df):
    """Group the SO.CSV-shaped dataframe into per-transaction payloads.
    A new transaction starts each time SO/Proposal Distribution resets to 1."""
    txs = []
    cur = None
    for _, r in sage_df.iterrows():
        if int(r['SO/Proposal Distribution']) == 1 or cur is None:
            cur = {
                'customer_id': str(r['Customer ID']),
                'customer_po': str(r['Customer PO']),
                'order_date': str(r['Date']),
                'ship_by': str(r['Ship By']),
                'ship_to_name': str(r['Ship to Name']),
                'ship_via': str(r['Ship Via']),
                'terms': str(r['Displayed Terms']),
                'ar_account': str(r['Accounts Receivable Account']),
                'tax_id': str(r['Sales Tax ID']),
                'internal_note': str(r['Internal Note']),
                'lines': [],
            }
            txs.append(cur)
        cur['lines'].append({
            'item_id': str(r['Item ID']),
            'description': str(r['Description']),
            'quantity': r['Quantity'],
            'unit_price': r['Unit Price'],
            'gl_account': str(r['G/L Account']),
            'tax_type': str(r['Tax Type']),
            'amount': r['Amount'],
        })
    return txs


def queue_orders(sage_df):
    """Insert each Sage transaction as a pending queue row.
    Returns (inserted, skipped) order_key lists — skipped rows already
    exist (idempotent re-push)."""
    txs = transactions_from_sage_df(sage_df)
    rows = []
    for tx in txs:
        tx['order_key'] = _order_key(tx)
        rows.append({
            'order_key': tx['order_key'],
            'status': 'pending',
            'payload': tx,
        })
    if not rows:
        return [], []
    resp = requests.post(
        _url(), headers={**_headers(), 'Prefer': 'resolution=ignore-duplicates,return=representation'},
        json=rows, timeout=20)
    resp.raise_for_status()
    inserted = {r['order_key'] for r in resp.json()} if resp.text else set()
    skipped = [r['order_key'] for r in rows if r['order_key'] not in inserted]
    return [r['order_key'] for r in rows if r['order_key'] in inserted], skipped


def queue_status(limit=25):
    """Most recent queue rows for the status display."""
    resp = requests.get(
        _url(select='order_key,status,sage_order_no,error,created_at,payload',
             order='created_at.desc', limit=str(limit)),
        headers=_headers(), timeout=20)
    resp.raise_for_status()
    return resp.json()
