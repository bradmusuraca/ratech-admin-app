"""Ratech -> Sage 50 bridge daemon.

Polls the Supabase `sage_order_queue` table for pending orders,
creates them as sales orders in Sage 50 via the COM API, and writes
the result (done/error + Sage order number) back to the queue.

Run on the Windows PC where Sage 50 is installed:

    pip install -r requirements.txt
    copy .env.example .env   (fill in values)
    python probe.py          (one-time: verify COM objects/fields)
    python bridge.py         (loops forever; Ctrl+C to stop)

For always-on use, register it as a Scheduled Task at logon.
"""
import os
import time
import traceback
import requests
from dotenv import load_dotenv

from sage_client import SageComClient

load_dotenv()

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY = os.environ['SUPABASE_KEY']
COMPANY_PATH = os.environ['SAGE_COMPANY_PATH']  # e.g. C:\Sage\Peachtree\Company or \\SERVER\...
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '60'))
TABLE = 'sage_order_queue'


def hdrs():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
    }


def fetch_pending():
    r = requests.get(
        f'{SUPABASE_URL}/rest/v1/{TABLE}',
        params={'status': 'eq.pending', 'order': 'created_at.asc',
                'select': 'order_key,job_type,payload'},
        headers=hdrs(), timeout=20)
    r.raise_for_status()
    return r.json()


def claim(order_key):
    """Flip pending->processing, but only if still pending (race guard)."""
    r = requests.patch(
        f'{SUPABASE_URL}/rest/v1/{TABLE}',
        params={'order_key': f'eq.{order_key}', 'status': 'eq.pending'},
        json={'status': 'processing', 'updated_at': 'now()'},
        headers={**hdrs(), 'Prefer': 'return=representation'}, timeout=20)
    r.raise_for_status()
    return len(r.json()) > 0


def finish(order_key, status, sage_order_no=None, error=None):
    r = requests.patch(
        f'{SUPABASE_URL}/rest/v1/{TABLE}',
        params={'order_key': f'eq.{order_key}'},
        json={'status': status, 'sage_order_no': sage_order_no,
              'error': error, 'updated_at': 'now()'},
        headers=hdrs(), timeout=20)
    r.raise_for_status()


def record_so_number(order_key, so_no):
    """Write the Sage SO number onto the matching open_orders row so
    short-pack updates later know which order to edit."""
    requests.patch(
        f'{SUPABASE_URL}/rest/v1/open_orders',
        params={'order_key': f'eq.{order_key}'},
        json={'sage_order_no': str(so_no), 'updated_at': 'now()'},
        headers=hdrs(), timeout=20)


def process_once(sage):
    for row in fetch_pending():
        key = row['order_key']
        if not claim(key):
            continue  # another worker took it
        try:
            if row.get('job_type') == 'qty_update':
                sage.update_sales_order(row['payload']['sage_order_no'],
                                        row['payload']['lines'])
                finish(key, 'done')
                print(f'[+] {key[:8]} -> updated SO '
                      f"{row['payload']['sage_order_no']} quantities")
            else:
                so_no = sage.create_sales_order(row['payload'])
                finish(key, 'done', sage_order_no=str(so_no))
                record_so_number(row['payload'].get('order_key', key), so_no)
                print(f'[+] {key[:8]} -> Sage sales order {so_no}')
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            finish(key, 'error', error=f'{e}\n{tb}')
            print(f'[!] {key[:8]} FAILED: {e}')


def main():
    print(f'Sage bridge polling every {POLL_SECONDS}s — Ctrl+C to stop')
    while True:
        try:
            # Open a fresh Sage session per sweep so a company that is
            # closed/exclusive elsewhere doesn't leave us stuck.
            with SageComClient(COMPANY_PATH) as sage:
                process_once(sage)
        except Exception as e:
            print(f'[!] sweep failed: {e}')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
