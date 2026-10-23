"""FedEx Rates API - account (negotiated) rate quotes.

Uses the OAuth client-credentials flow; the account number on the token's
associated project is what makes returned rates negotiated rather than list.

Env/secrets required:
    FEDEX_CLIENT_ID, FEDEX_CLIENT_SECRET, FEDEX_ACCOUNT_NUMBER
Optional:
    FEDEX_BASE_URL          default https://apis.fedex.com
    FEDEX_SHIPPER_NAME/STREET/CITY/STATE/ZIP  - Ratech ship-from address
"""
import os
import time
import requests


def _base():
    return os.getenv('FEDEX_BASE_URL', 'https://apis.fedex.com').rstrip('/')


_tok = {'value': None, 'exp': 0}


def is_configured():
    return all(os.getenv(k) for k in
               ('FEDEX_CLIENT_ID', 'FEDEX_CLIENT_SECRET', 'FEDEX_ACCOUNT_NUMBER'))


def _token():
    if _tok['value'] and time.time() < _tok['exp'] - 60:
        return _tok['value']
    r = requests.post(f"{_base()}/oauth/token", data={
        'grant_type': 'client_credentials',
        'client_id': os.getenv('FEDEX_CLIENT_ID'),
        'client_secret': os.getenv('FEDEX_CLIENT_SECRET'),
    }, timeout=20)
    r.raise_for_status()
    j = r.json()
    _tok['value'] = j['access_token']
    _tok['exp'] = time.time() + int(j.get('expires_in', 3600))
    return _tok['value']


def _shipper():
    return {
        'streetLines': [os.getenv('FEDEX_SHIPPER_STREET', '1110 Adwood Drive')],
        'city': os.getenv('FEDEX_SHIPPER_CITY', 'Cincinnati'),
        'stateOrProvinceCode': os.getenv('FEDEX_SHIPPER_STATE', 'OH'),
        'postalCode': os.getenv('FEDEX_SHIPPER_ZIP', '45240'),
        'countryCode': 'US',
        'residential': False,
    }


SERVICE_NAMES = {
    'FEDEX_GROUND': 'FedEx Ground',
    'GROUND_HOME_DELIVERY': 'FedEx Home Delivery',
    'FEDEX_EXPRESS_SAVER': 'FedEx Express Saver',
    'FEDEX_2_DAY': 'FedEx 2Day',
    'FEDEX_2_DAY_AM': 'FedEx 2Day A.M.',
    'STANDARD_OVERNIGHT': 'Standard Overnight',
    'PRIORITY_OVERNIGHT': 'Priority Overnight',
    'FIRST_OVERNIGHT': 'First Overnight',
    'FEDEX_FREIGHT_PRIORITY': 'FedEx Freight Priority',
    'FEDEX_FREIGHT_ECONOMY': 'FedEx Freight Economy',
    'INTERNATIONAL_PRIORITY': 'International Priority',
    'INTERNATIONAL_ECONOMY': 'International Economy',
}

_TRANSIT_LABELS = {
    'ONE_DAY': '1 business day',
    'TWO_DAYS': '2 business days',
    'THREE_DAYS': '3 business days',
    'FOUR_DAYS': '4 business days',
    'FIVE_DAYS': '5 business days',
    'SIX_DAYS': '6 business days',
    'SEVEN_DAYS': '7 business days',
}


def get_rates(ship_to, weight_lbs, residential=False):
    """Quote every service for a lane at negotiated (ACCOUNT) rates.

    ship_to: dict with name/line1/line2/city/state/zip (as stored in the
    queue payload). weight_lbs: total package weight. Returns a list of
    {'service','name','amount','currency','transit'} sorted cheapest first.
    """
    streets = [s for s in (ship_to.get('line1'), ship_to.get('line2')) if s]
    body = {
        'accountNumber': {'value': os.getenv('FEDEX_ACCOUNT_NUMBER')},
        'requestedShipment': {
            'shipper': {'address': _shipper()},
            'recipient': {
                'address': {
                    'streetLines': streets,
                    'city': ship_to.get('city', ''),
                    'stateOrProvinceCode': ship_to.get('state', ''),
                    'postalCode': ship_to.get('zip', ''),
                    'countryCode': ship_to.get('country', 'US') or 'US',
                    'residential': bool(residential),
                }
            },
            'pickupType': 'USE_SCHEDULED_PICKUP',
            'packagingType': 'YOUR_PACKAGING',
            'rateRequestType': ['ACCOUNT'],
            'requestedPackageLineItems': [{
                'weight': {'units': 'LB', 'value': float(weight_lbs)},
            }],
        },
    }
    r = requests.post(
        f"{_base()}/rate/v1/rates/quotes",
        headers={'Authorization': f"Bearer {_token()}",
                 'Content-Type': 'application/json'},
        json=body, timeout=30)
    r.raise_for_status()

    out = []
    for d in (r.json().get('output', {}).get('rateReplyDetails') or []):
        svc = d.get('serviceType', '')
        amount = currency = transit = None
        for rs in (d.get('ratedShipmentDetails') or []):
            if rs.get('rateType') == 'ACCOUNT':
                amount = rs.get('totalNetCharge')
                currency = rs.get('currency', 'USD')
                break
        if amount is None:  # no account rate returned for this service
            continue
        op = d.get('operationalDetail') or {}
        tt = (op.get('transitTime') or '')
        transit = _TRANSIT_LABELS.get(tt)
        if not transit:
            dd = (d.get('commitDetail') or {}).get('dateTimestamp')
            if dd:
                transit = f"delivers {str(dd)[:10]}"
        out.append({
            'service': svc,
            'name': SERVICE_NAMES.get(svc, svc.replace('_', ' ').title()),
            'amount': float(amount),
            'currency': currency or 'USD',
            'transit': transit or '',
        })
    out.sort(key=lambda q: q['amount'])
    return out
