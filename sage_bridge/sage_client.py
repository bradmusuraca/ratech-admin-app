"""Sage 50 US COM client — creates sales orders via the PeachwServer
ActiveX API exposed by the Sage 50 SDK (Sage.Peachtree / PTOW objects).

Requires:
  - Sage 50 US 2019 installed on this machine
  - Sage 50 SDK installed (COM samples + PeachwServer interop)
  - pywin32
  - The Sage 50 company must have been opened at least once

NOTE: The PTOW object/property names below are based on the Sage 50
COM API user's manual. If your SDK build names them differently, run
probe.py to dump the real member list and adjust FIELD_MAP.
"""
import win32com.client
import pythoncom

# PTOWSalesOrder field names (calibrate with probe.py on the Sage PC)
FIELD_MAP = {
    'customer_id': 'CustomerID',
    'customer_po': 'CustomerPONumber',
    'order_date': 'TransactionDate',
    'ship_by': 'ShipByDate',
    'ship_to_name': 'ShipToName',
    'ship_via': 'ShipViaCode',
    'internal_note': 'InternalNote',
    'lines_collection': 'LineItems',   # .Add() returns a line object
    'line_item_id': 'ItemID',
    'line_description': 'Description',
    'line_quantity': 'Quantity',
    'line_unit_price': 'UnitPrice',
    'line_gl_account': 'GLAccount',
    'line_tax_type': 'TaxType',
    'line_amount': 'Amount',
}


class SageComClient:
    """Context manager wrapping a PeachwServer session."""

    def __init__(self, company_path, app_id='RatechOrderBridge'):
        self.company_path = company_path
        self.app_id = app_id
        self._server = None
        self._company = None

    def __enter__(self):
        pythoncom.CoInitialize()
        self._server = win32com.client.Dispatch('PeachwServer')
        self._server.Begin(self.app_id)
        self._company = self._server.OpenCompany(self.company_path)
        return self

    def __exit__(self, *exc):
        try:
            if self._server:
                self._server.End()
        finally:
            pythoncom.CoUninitialize()

    def create_sales_order(self, payload):
        """Create one sales order from a queue payload dict.
        Returns the Sage-assigned sales order number."""
        f = FIELD_MAP
        so = self._company.CreateObject('PTOWSalesOrder')
        setattr(so, f['customer_id'], payload['customer_id'])
        setattr(so, f['customer_po'], payload['customer_po'][:20])
        if payload.get('order_date'):
            setattr(so, f['order_date'], payload['order_date'])
        if payload.get('ship_by'):
            setattr(so, f['ship_by'], payload['ship_by'])
        if payload.get('ship_to_name'):
            setattr(so, f['ship_to_name'], payload['ship_to_name'][:30])
        if payload.get('ship_via'):
            setattr(so, f['ship_via'], payload['ship_via'][:12])
        if payload.get('internal_note'):
            setattr(so, f['internal_note'], payload['internal_note'][:2000])

        lines = getattr(so, f['lines_collection'])
        for line in payload['lines']:
            ln = lines.Add()
            setattr(ln, f['line_item_id'], str(line['item_id'])[:20])
            setattr(ln, f['line_description'], str(line['description'])[:160])
            setattr(ln, f['line_quantity'], float(line['quantity']))
            setattr(ln, f['line_unit_price'], float(line['unit_price']))
            if line.get('gl_account'):
                setattr(ln, f['line_gl_account'], line['gl_account'])
            if line.get('tax_type'):
                setattr(ln, f['line_tax_type'], line['tax_type'])

        so.Save()
        return getattr(so, f.get('order_number', 'SalesOrderNumber'), '')

    def update_sales_order(self, so_number, lines):
        """Open an existing sales order and rewrite line quantities
        (short-pack adjustments from the floor). Lines is a list of
        {'item_id', 'quantity'}; matched by item_id.

        NOTE: existing-record open/load semantics vary by SDK build -
        run probe.py and adjust (Load() vs. setting the key then Read).
        """
        f = FIELD_MAP
        so = self._company.CreateObject('PTOWSalesOrder')
        # Load existing order - candidate patterns, first that works wins.
        loaded = False
        for meth in ('Load', 'Open', 'Read'):
            if hasattr(so, meth):
                try:
                    getattr(so, meth)(so_number)
                    loaded = True
                    break
                except Exception:
                    continue
        if not loaded:
            setattr(so, f.get('order_number', 'SalesOrderNumber'), so_number)
            if hasattr(so, 'Load'):
                so.Load()
        want = {str(l['item_id']): float(l['quantity']) for l in lines}
        for ln in getattr(so, f['lines_collection']):
            iid = str(getattr(ln, f['line_item_id']))
            if iid in want:
                setattr(ln, f['line_quantity'], want[iid])
        so.Save()
