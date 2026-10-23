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
import re
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


def _recipient_addr(ship_to, residential):
    streets = [s for s in (ship_to.get('line1'), ship_to.get('line2')) if s]
    return {
        'streetLines': streets,
        'city': ship_to.get('city', ''),
        'stateOrProvinceCode': ship_to.get('state', ''),
        'postalCode': ship_to.get('zip', ''),
        'countryCode': ship_to.get('country', 'US') or 'US',
        'residential': bool(residential),
    }


def validate_address(ship_to):
    """Address Validation API -> {'ok','residential','address','notes'}.
    Standardizes the address and detects residential vs commercial so the
    rate quote (and Home Delivery vs Ground routing) is right."""
    addr = _recipient_addr(ship_to, False)
    addr.pop('residential', None)
    r = requests.post(
        f"{_base()}/address/v1/addresses/resolve",
        headers={'Authorization': f"Bearer {_token()}",
                 'Content-Type': 'application/json'},
        json={'addressesToValidate': [{'address': addr}]}, timeout=20)
    r.raise_for_status()
    res = (r.json().get('output', {}).get('resolvedAddresses') or [{}])[0]
    cls = (res.get('classification') or '').upper()
    std = res.get('standardizedAddress') or {}
    notes = [m.get('message', '') for m in (res.get('customerMessages') or [])
             if m.get('message')]
    return {
        'ok': bool(res.get('attributes', {}).get('Resolved')),
        'residential': cls == 'RESIDENTIAL',
        'address': {
            'line1': (std.get('streetLines') or [''])[0] if std.get('streetLines') else ship_to.get('line1', ''),
            'line2': (std.get('streetLines') or ['', ''])[1] if len(std.get('streetLines') or []) > 1 else ship_to.get('line2', ''),
            'city': std.get('city') or ship_to.get('city', ''),
            'state': std.get('stateOrProvinceCode') or ship_to.get('state', ''),
            'zip': std.get('postalCode') or ship_to.get('zip', ''),
            'country': 'US',
        },
        'notes': notes,
    }


def create_shipment(ship_to, weight_lbs, service, residential=False, ref=''):
    """Ship API -> {'tracking', 'label_b64'}. Creates a real shipment billed
    to the account; label comes back as base64 PDF."""
    shipper_phone = os.getenv('FEDEX_SHIPPER_PHONE', '5137422111')
    body = {
        'accountNumber': {'value': os.getenv('FEDEX_ACCOUNT_NUMBER')},
        'labelResponseOptions': 'LABEL',
        'requestedShipment': {
            'shipper': {
                'contact': {
                    'personName': os.getenv('FEDEX_SHIPPER_NAME', 'Ratech Manufacturing'),
                    'phoneNumber': re.sub(r'\D', '', shipper_phone)[:10],
                    'companyName': 'Ratech Manufacturing',
                },
                'address': _shipper(),
            },
            'recipients': [{
                'contact': {
                    'personName': (ship_to.get('name') or 'Customer')[:35],
                    'phoneNumber': os.getenv('FEDEX_RECIPIENT_PHONE', re.sub(r'\D', '', shipper_phone)[:10]),
                },
                'address': _recipient_addr(ship_to, residential),
            }],
            'serviceType': service,
            'packagingType': 'YOUR_PACKAGING',
            'pickupType': 'USE_SCHEDULED_PICKUP',
            'shippingChargesPayment': {
                'paymentType': 'SENDER',
                'payor': {},
            },
            'labelSpecification': {
                'labelFormatType': 'COMMON2D',
                'imageType': 'PDF',
                'labelStockType': os.getenv('FEDEX_LABEL_STOCK',
                                            'PAPER_85X11_TOP_HALF_LABEL'),
            },
            'requestedPackageLineItems': [{
                'weight': {'units': 'LB', 'value': float(weight_lbs)},
                'customerReferences': ([{'customerReferenceType': 'CUSTOMER_REFERENCE',
                                        'value': str(ref)[:40]}] if ref else []),
            }],
        },
    }
    r = requests.post(
        f"{_base()}/ship/v1/shipments",
        headers={'Authorization': f"Bearer {_token()}",
                 'Content-Type': 'application/json'},
        json=body, timeout=40)
    if r.status_code >= 400:
        # Surface FedEx's own error text - it names the offending field
        raise RuntimeError(f"FedEx ship API {r.status_code}: {r.text[:500]}")
    ts = (r.json().get('output', {}).get('transactionShipments') or [{}])[0]
    tracking = ts.get('masterTrackingNumber', '')
    docs = ((ts.get('pieceResponses') or [{}])[0].get('packageDocuments') or [])
    label_b64 = next((d.get('encodedLabel') for d in docs if d.get('encodedLabel')), '')
    return {'tracking': tracking, 'label_b64': label_b64}


def get_rates(ship_to, weight_lbs, residential=False):
    """Quote every service for a lane at negotiated (ACCOUNT) rates.

    ship_to: dict with name/line1/line2/city/state/zip (as stored in the
    queue payload). weight_lbs: total package weight. Returns a list of
    {'service','name','amount','currency','transit'} sorted cheapest first.
    """
    body = {
        'accountNumber': {'value': os.getenv('FEDEX_ACCOUNT_NUMBER')},
        'requestedShipment': {
            'shipper': {'address': _shipper()},
            'recipient': {
                'address': _recipient_addr(ship_to, residential),
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
