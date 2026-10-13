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


def list_stores(api_key: str, api_secret: str) -> List[Dict]:
    """Return connected stores (marketplaces) - used to map storeId to a name."""
    resp = requests.get(f'{API_BASE}/stores', auth=(api_key, api_secret),
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_carriers(api_key: str, api_secret: str) -> List[Dict]:
    """Return carriers connected to the account."""
    resp = requests.get(f'{API_BASE}/carriers', auth=(api_key, api_secret),
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_services(api_key: str, api_secret: str, carrier_code: str) -> List[Dict]:
    """Return service codes for a carrier."""
    resp = requests.get(f'{API_BASE}/carriers/listservices',
                        params={'carrierCode': carrier_code},
                        auth=(api_key, api_secret), timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_packages(api_key: str, api_secret: str, carrier_code: str) -> List[Dict]:
    """Return package codes for a carrier."""
    resp = requests.get(f'{API_BASE}/carriers/listpackages',
                        params={'carrierCode': carrier_code},
                        auth=(api_key, api_secret), timeout=30)
    resp.raise_for_status()
    return resp.json()


def create_label(api_key: str, api_secret: str, order_id,
                 carrier_code: str, service_code: str, package_code: str,
                 weight_oz: float, ship_date: Optional[str] = None,
                 dimensions: Optional[Dict] = None,
                 confirmation: str = 'delivery', test: bool = False) -> Dict:
    """Purchase postage and create a shipping label for an existing order.

    WARNING: this is a billable action - postage is charged, the order is
    marked shipped, and the marketplace is notified with tracking.
    test=True produces a non-billable test label where the carrier
    supports it (e.g. USPS via Stamps.com).
    Returns the API response dict (includes base64 'labelData' PDF/PNG).
    """
    body = {
        'orderId': int(order_id),
        'carrierCode': carrier_code,
        'serviceCode': service_code,
        'packageCode': package_code,
        'confirmation': confirmation,
        'weight': {'value': float(weight_oz), 'units': 'ounces'},
        'testLabel': bool(test),
    }
    if ship_date:
        body['shipDate'] = ship_date
    if dimensions:
        body['dimensions'] = dimensions
    resp = requests.post(f'{API_BASE}/orders/createlabel', json=body,
                         auth=(api_key, api_secret), timeout=60)
    resp.raise_for_status()
    return resp.json()


def mark_as_shipped(api_key: str, api_secret: str, order_id,
                    carrier_code: str, tracking_number: str,
                    ship_date: Optional[str] = None,
                    notify_marketplace: bool = True,
                    notify_customer: bool = False) -> Dict:
    """Mark an order shipped with an existing tracking number.

    Used for combined shipments: one label is purchased on the primary
    order, sibling orders get the same tracking number so each
    marketplace still receives ship confirmation.
    """
    body = {
        'orderId': int(order_id),
        'carrierCode': carrier_code,
        'trackingNumber': tracking_number,
        'notifyMarketplace': bool(notify_marketplace),
        'notifyCustomer': bool(notify_customer),
    }
    if ship_date:
        body['shipDate'] = ship_date
    resp = requests.post(f'{API_BASE}/orders/markasshipped', json=body,
                         auth=(api_key, api_secret), timeout=30)
    resp.raise_for_status()
    return resp.json()


def label_error_message(exc) -> str:
    """Extract ShipStation's human-readable error from a failed API call."""
    try:
        data = exc.response.json()
        for key in ('ExceptionMessage', 'Message', 'message'):
            if data.get(key):
                return str(data[key])
        return str(exc.response.text or exc)
    except Exception:
        return str(exc)


def order_web_url(order_id) -> str:
    """Deep link to the order in the ShipStation web app (for fixing
    addresses etc.). Subdomain varies by account; all-orders search page
    is the most stable target."""
    return 'https://ship11.shipstation.com/orders/all-orders'


def label_pdf_bytes(label_response: Dict) -> bytes:
    """Decode the base64 labelData field from a create_label response."""
    import base64
    data = label_response.get('labelData') or ''
    return base64.b64decode(data) if data else b''


def order_channel(order: Dict, store_map: Optional[Dict] = None) -> str:
    """Best-effort marketplace detection for a ShipStation order."""
    import re
    num = str(order.get('orderNumber') or '')
    adv = order.get('advancedOptions') or {}
    src = str(adv.get('source') or '')
    store_name = str((store_map or {}).get(adv.get('storeId')) or '')
    for cand in (src, store_name):
        cl = cand.lower()
        if 'amazon' in cl:
            return 'Amazon'
        if 'ebay' in cl:
            return 'eBay'
    # Amazon IDs look like 114-3315411-3045845; eBay like 17-15192-30111
    if re.match(r'^\d{3}-\d{7}-\d{7}$', num):
        return 'Amazon'
    if re.match(r'^\d{2}-\d{5}-\d{5}$', num):
        return 'eBay'
    return store_name or src or 'ShipStation'
