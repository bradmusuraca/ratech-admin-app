"""ShipStation API (v1) client for pulling orders.

Auth is HTTP Basic with an API key/secret pair generated in
ShipStation -> Settings -> Account -> API Settings. API access requires
a Standard plan or higher.
"""
import requests
from typing import List, Dict, Optional

API_BASE = 'https://ssapi.shipstation.com'


def list_orders(api_key: str, api_secret: str,
                status: Optional[str] = None,
                order_date_start: Optional[str] = None,
                page_size: int = 50, max_pages: int = 5) -> List[Dict]:
    """Return orders newest-first. order_date_start is ISO 'YYYY-MM-DDTHH:MM:SS'."""
    params = {
        'sortBy': 'OrderDate',
        'sortDir': 'DESC',
        'pageSize': page_size,
    }
    if status:
        params['orderStatus'] = status
    if order_date_start:
        params['orderDateStart'] = order_date_start

    orders = []
    for page in range(1, max_pages + 1):
        params['page'] = page
        resp = requests.get(f'{API_BASE}/orders', auth=(api_key, api_secret),
                            params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        orders.extend(data.get('orders', []))
        if page >= int(data.get('pages', 1) or 1):
            break
    return orders


def order_channel(order: Dict) -> str:
    """Best-effort marketplace detection for a ShipStation order."""
    src = str((order.get('advancedOptions') or {}).get('source') or '')
    num = str(order.get('orderNumber') or '')
    sl = src.lower()
    if 'amazon' in sl:
        return 'Amazon'
    if 'ebay' in sl:
        return 'eBay'
    # Amazon IDs look like 114-3315411-3045845; eBay like 17-15192-30111
    import re
    if re.match(r'^\d{3}-\d{7}-\d{7}$', num):
        return 'Amazon'
    if re.match(r'^\d{2}-\d{5}-\d{5}$', num):
        return 'eBay'
    return src or 'ShipStation'
