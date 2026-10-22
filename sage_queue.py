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
from datetime import datetime
import requests

TABLE = 'sage_order_queue'
OPEN_TABLE = 'open_orders'


def is_configured():
    return bool(os.getenv('SUPABASE_URL') and os.getenv('SUPABASE_KEY'))


def _headers():
    key = os.getenv('SUPABASE_KEY')
    return {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }


def _url(table=TABLE, **params):
    base = os.getenv('SUPABASE_URL', '').rstrip('/')
    q = '&'.join(f'{k}={v}' for k, v in params.items())
    return f'{base}/rest/v1/{table}' + (f'?{q}' if q else '')


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
                'ship_to_line1': str(r['Ship to Address-Line One']),
                'ship_to_line2': str(r['Ship to Address-Line Two']),
                'ship_to_city': str(r['Ship to City']),
                'ship_to_state': str(r['Ship to State']),
                'ship_to_zip': str(r['Ship to Zipcode']),
                'ship_to_country': str(r['Ship to Country']),
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
            'job_type': 'create',
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


# ---- Open sales orders (packing list) ----

def create_open_orders(sage_df):
    """Create packing-list rows for each Sage transaction.
    Returns (inserted, skipped) order_key lists."""
    txs = transactions_from_sage_df(sage_df)
    rows = []
    for tx in txs:
        key = _order_key(tx)
        rows.append({
            'order_key': key,
            'customer_id': tx['customer_id'],
            'customer_po': tx['customer_po'],
            'status': 'open',
            'lines': [{
                'item_id': l['item_id'],
                'description': l['description'],
                'qty_required': l['quantity'],
                'qty_packed': None,
            } for l in tx['lines']],
        })
    if not rows:
        return [], []
    resp = requests.post(
        _url(OPEN_TABLE),
        headers={**_headers(), 'Prefer': 'resolution=ignore-duplicates,return=representation'},
        json=rows, timeout=20)
    resp.raise_for_status()
    inserted = {r['order_key'] for r in resp.json()} if resp.text else set()
    return ([r['order_key'] for r in rows if r['order_key'] in inserted],
            [r['order_key'] for r in rows if r['order_key'] not in inserted])


def list_open_orders(include_done=False, limit=50):
    """Packing-list rows, oldest first. open/short only unless include_done."""
    params = {'order': 'created_at.asc', 'limit': str(limit),
              'select': 'order_key,customer_id,customer_po,status,lines,sage_order_no,packed_by,created_at'}
    if not include_done:
        params['status'] = 'in.(open,short)'
    resp = requests.get(_url(OPEN_TABLE, **params), headers=_headers(), timeout=20)
    resp.raise_for_status()
    return resp.json()


def update_packed(order_key, lines, status, packed_by=''):
    """Write packed quantities + status back to an open order."""
    resp = requests.patch(
        _url(OPEN_TABLE, order_key=f'eq.{order_key}'),
        headers=_headers(),
        json={'lines': lines, 'status': status, 'packed_by': packed_by,
              'updated_at': 'now()'},
        timeout=20)
    resp.raise_for_status()


def note_line_adjustment(order_key, asm_item_id, comp_item_id, qty):
    """Stamp a persistent 'adjustment queued' note on a packing line so the
    action is still visible after the page reruns."""
    resp = requests.get(_url(OPEN_TABLE, order_key=f'eq.{order_key}',
                           select='lines'), headers=_headers(), timeout=20)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return
    lines = rows[0]['lines']
    for l in lines:
        if str(l.get('item_id', '')).strip().upper() == asm_item_id.strip().upper():
            l['adjust_note'] = (f"+{qty:g} queued for {comp_item_id} "
                                f"at {datetime.now().strftime('%H:%M')}")
    requests.patch(_url(OPEN_TABLE, order_key=f'eq.{order_key}'),
                   headers=_headers(),
                   json={'lines': lines, 'updated_at': 'now()'},
                   timeout=20).raise_for_status()


def delete_open_order(order_key):
    """Remove a packing-list row (e.g. test orders or SOs deleted in Sage)."""
    resp = requests.delete(_url(OPEN_TABLE, order_key=f'eq.{order_key}'),
                           headers=_headers(), timeout=20)
    resp.raise_for_status()


def queue_order_void(order_key, sage_order_no):
    """Queue a Sage-side delete for a sales order the user removed from
    the packing list."""
    resp = requests.post(
        _url(),
        headers={**_headers(), 'Prefer': 'resolution=ignore-duplicates,return=representation'},
        json=[{
            'order_key': f"{order_key[:20]}-void",
            'job_type': 'void_order',
            'status': 'pending',
            'payload': {'sage_order_no': sage_order_no,
                        'parent_order': order_key},
        }], timeout=20)
    resp.raise_for_status()


def _supersede_pending(parent_order, job_type):
    """Mark still-pending jobs of this type for the order 'superseded' so a
    stale held/queued job can't fire alongside the fresh one (e.g. build 5
    after the user lowers packed qty to 2 and re-saves)."""
    requests.patch(
        _url(**{'payload->>parent_order': f'eq.{parent_order}',
                'job_type': f'eq.{job_type}', 'status': 'eq.pending'}),
        headers=_headers(), json={'status': 'superseded'}, timeout=20
    ).raise_for_status()


def queue_assembly_build(order_key, lines):
    """Queue assembly build(s): the bridge appends rows to assembly_builds.csv
    in Sage's Inventory Assemblies Journal import format."""
    _supersede_pending(order_key, 'build_assembly')
    upd_key = f"{order_key[:20]}-asm-{hashlib.sha1(str(lines).encode()).hexdigest()[:8]}"
    # merge-duplicates upserts: re-queueing after a done/error run revives
    # the row (ignore-duplicates would silently drop the resend).
    resp = requests.post(
        _url(),
        headers={**_headers(), 'Prefer': 'resolution=merge-duplicates,return=representation'},
        json=[{
            'order_key': upd_key,
            'job_type': 'build_assembly',
            'status': 'pending',
            'payload': {'parent_order': order_key,
                        'lines': [{'item_id': l['item_id'],
                                   'qty': l['qty'],
                                   'description': l.get('description', '')}
                                  for l in lines]},
        }], timeout=20)
    resp.raise_for_status()
    return upd_key


def queue_invoice(order_key, sage_order_no, lines):
    """Queue a Sage sales invoice created from the sales order using the
    packed (shipped) quantities. Invoicing ships the SO - a fully-shipped
    order closes automatically in Sage."""
    _supersede_pending(order_key, 'create_invoice')
    inv_key = f"{order_key[:20]}-inv-{hashlib.sha1(str(lines).encode()).hexdigest()[:8]}"
    resp = requests.post(
        _url(),
        headers={**_headers(), 'Prefer': 'resolution=merge-duplicates,return=representation'},
        json=[{
            'order_key': inv_key,
            'job_type': 'create_invoice',
            'status': 'pending',
            'payload': {'sage_order_no': sage_order_no,
                        'parent_order': order_key,
                        'lines': [{'item_id': l['item_id'],
                                   'quantity': l['qty_packed']} for l in lines]},
        }], timeout=20)
    resp.raise_for_status()
    return inv_key


def queue_inventory_adjustment(item_id, qty_delta, reason):
    """Queue a Sage inventory adjustment journal row for one item
    (+/- on-hand delta). Each request is a distinct job."""
    import uuid
    key = f"adj-{uuid.uuid4().hex[:16]}"
    resp = requests.post(
        _url(),
        headers={**_headers(), 'Prefer': 'resolution=ignore-duplicates,return=representation'},
        json=[{
            'order_key': key,
            'job_type': 'adjust_inventory',
            'status': 'pending',
            'payload': {'lines': [{'item_id': item_id,
                                   'qty': qty_delta,
                                   'reason': reason}]},
        }], timeout=20)
    resp.raise_for_status()
    return key


def queue_qty_update(order_key, sage_order_no, lines):
    """Queue a Sage-side quantity update for a short-packed order."""
    import hashlib as _h
    _supersede_pending(order_key, 'qty_update')
    upd_key = f"{order_key[:20]}-upd-{_h.sha1(str(lines).encode()).hexdigest()[:8]}"
    resp = requests.post(
        _url(),
        headers={**_headers(), 'Prefer': 'resolution=merge-duplicates,return=representation'},
        json=[{
            'order_key': upd_key,
            'job_type': 'qty_update',
            'status': 'pending',
            'payload': {'sage_order_no': sage_order_no,
                        'parent_order': order_key,
                        'lines': [{'item_id': l['item_id'],
                                   'quantity': l['qty_packed']} for l in lines]},
        }], timeout=20)
    resp.raise_for_status()
    return upd_key
