import streamlit as st
import os
import re
import json
import hashlib
from dotenv import load_dotenv
import pandas as pd
from io import StringIO
from vision_processor import VisionProcessor, parse_order_text, parse_packing_slip_text
from email_inbox import (list_unseen_orders, fetch_pdf_from_message,
                         fetch_body_from_message, send_email, extract_email_address,
                         mark_as_read)
from email.utils import parsedate_to_datetime
from shipstation import (list_orders as ss_list_orders,
                         list_stores as ss_list_stores,
                         list_carriers as ss_list_carriers,
                         list_services as ss_list_services,
                         list_packages as ss_list_packages,
                         create_label as ss_create_label,
                         mark_as_shipped as ss_mark_as_shipped,
                         label_pdf_bytes, label_error_message, order_web_url,
                         order_channel as ss_order_channel)
from sage_queue import (is_configured as sage_queue_configured,
                        queue_orders as sage_queue_push,
                        queue_status as sage_queue_status,
                        create_open_orders, list_open_orders,
                        update_packed, queue_qty_update, queue_invoice,
                        queue_assembly_build, delete_open_order,
                        queue_order_void, queue_inventory_adjustment,
                        note_line_adjustment, transactions_from_sage_df)
import streamlit.components.v1 as components

load_dotenv()

# Streamlit Cloud dashboard secrets live in st.secrets but are not reliably
# injected into os.environ - copy them in so every os.getenv() works.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str) and _k not in os.environ:
            os.environ[_k] = _v
except Exception:
    pass

st.set_page_config(
    page_title="Ratech Order Management",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ---- App-wide styling ----
st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .ratech-header {
        padding: 1.1rem 1.5rem;
        background: linear-gradient(135deg, #16324F 0%, #2460A7 100%);
        border-radius: 12px;
        margin-bottom: 1.25rem;
    }
    .ratech-header h1 {
        color: #ffffff; margin: 0; font-size: 1.8rem; font-weight: 700;
    }
    .ratech-header p {
        color: #bcd4ea; margin: 0.2rem 0 0 0; font-size: 0.95rem;
    }
    .stMetric {
        background: #F5F7FA;
        border-radius: 10px;
        padding: 0.6rem 1rem;
    }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="ratech-header">
    <h1>📦 Ratech Order Management</h1>
    <p>PDF &amp; ShipStation orders &rarr; validate &rarr; Sage 50 sales orders</p>
</div>
""", unsafe_allow_html=True)

# Session state defaults
for _k, _v in (('invoices', []), ('inbox_items', []), ('loaded_uids', set()),
               ('ss_orders', []), ('loaded_ss', set()), ('ss_store_map', {}),
               ('ss_carriers', []), ('ss_labels', {}), ('ss_opts_cache', {}),
               ('ss_selected', set()), ('ss_batch_pdf', None), ('ss_batch_results', []),
               ('upload_version', 0), ('user', None)):
    if _k not in st.session_state:
        st.session_state[_k] = _v

ALL_PAGES = ["Order Processing", "Create Invoices", "Inventory", "Customers",
             "Inventory Management", "Settings"]


def get_users():
    """Configured user accounts: {username: {password|password_hash, name, pages}}.

    Sources (first match wins):
      - Streamlit secrets: [users.username] table
      - USERS_JSON env var (local dev)
    If none configured, the app runs open (no login screen).
    """
    try:
        users = st.secrets.get('users')
        if users:
            result = {}
            for u, v in dict(users).items():
                # Support both [users.name] tables and `name = "password"` shorthand
                if isinstance(v, str):
                    result[u] = {'password': v}
                else:
                    result[u] = dict(v)
            return result
    except Exception:
        pass
    raw = os.getenv('USERS_JSON')
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def check_login(username, password):
    """Return a user dict on success, else None. Supports plaintext
    'password' or sha256 'password_hash' in the user record."""
    u = get_users().get(username)
    if not u:
        return None
    stored_hash = str(u.get('password_hash', '')).strip().lower()
    if stored_hash:
        ok = hashlib.sha256(password.encode()).hexdigest() == stored_hash
    else:
        ok = str(u.get('password', '')) == password and password != ''
    if not ok:
        return None
    return {
        'username': username,
        'name': u.get('name', username),
        'pages': [p for p in (
            {'Open Sales Orders': 'Create Invoices'}.get(p, p)
            for p in u.get('pages', ALL_PAGES))
            if p in ALL_PAGES] or ALL_PAGES,
    }


def render_pdf_images(pdf_bytes):
    """Render PDF pages to PNG images using PyMuPDF (already used for OCR)."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        imgs = []
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            imgs.append(pix.tobytes('png'))
        doc.close()
        return imgs
    except Exception as e:
        print(f"PDF preview render failed: {e}")
        return []


CUSTOMERS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'customers.csv')
CUSTOMER_COLS = ['Customer Number', 'Customer Name', 'Aliases', 'Their Code For Us']


def load_customers():
    """Load the customer-name -> Sage customer number mapping table.

    Prefers the CUSTOMERS_CSV secret (survives redeploys, not committed to
    the repo); falls back to a local customers.csv file for development."""
    text = os.getenv('CUSTOMERS_CSV')
    try:
        if text:
            df = pd.read_csv(StringIO(text), dtype=str).fillna('')
        elif os.path.exists(CUSTOMERS_CSV):
            df = pd.read_csv(CUSTOMERS_CSV, dtype=str).fillna('')
        else:
            return pd.DataFrame(columns=CUSTOMER_COLS)
        for c in CUSTOMER_COLS:
            if c not in df.columns:
                df[c] = ''
        return df[CUSTOMER_COLS]
    except Exception:
        pass
    return pd.DataFrame(columns=CUSTOMER_COLS)


def guess_customer(text, channel=''):
    """Return the Sage customer number whose alias OR name appears in the
    given text. Channel (filename + sender) is checked first — a PO's
    bill-to block can print a different customer's name (e.g. a Ford PO
    that says 'RMS' should resolve to the sender's customer, not the
    bill-to text). The full text is trimmed to the header: a line item
    like 'Ford Racing ...' deep in the document would otherwise match
    Ford's alias on a competitor's PO."""
    for src in (channel, (text or '')[:2000]):
        tl = (src or '').lower()
        if not tl:
            continue
        for _, row in load_customers().iterrows():
            terms = str(row.get('Aliases', '')).split(';')
            terms.append(str(row.get('Customer Name', '')))
            for term in terms:
                term = term.strip().lower()
                # Word boundaries: bare substring matching lets a short alias
                # like "rms" hit inside "terms"/"forms" and pick the wrong customer.
                if term and re.search(r'\b' + re.escape(term) + r'\b', tl):
                    return str(row.get('Customer Number', '')).strip()
    return ''


def cred_source(name):
    """Where a credential is actually coming from: 'secrets' (permanent),
    'env' (.env file - wiped on redeploy), or None."""
    try:
        if name in st.secrets:
            return 'secrets'
    except Exception:
        pass
    return 'env' if os.getenv(name) else None


def cred_status_caption(name, label):
    src = cred_source(name)
    if src == 'secrets':
        st.success(f"✅ {label} configured (Streamlit secrets - permanent)")
    elif src == 'env':
        st.warning(f"⚠️ {label} configured via .env only - will be lost on reboot. "
                   "Add it to Streamlit secrets.")
    else:
        st.info(f"ℹ️ {label} not configured")


def resolve_vendor(inv):
    """Ensure the order's Vendor column holds a Sage customer number.
    Orders loaded before a number was entered keep the raw parsed vendor
    text (e.g. 'RAT RATECH'); re-run alias matching on the document text
    so the number fills in without re-loading the order."""
    df = inv.get('df')
    if df is None or df.empty or 'Vendor' not in df.columns:
        return
    cust_numbers = {str(n) for n in load_customers()['Customer Number']
                    if str(n).strip()}
    vend = [v for v in df['Vendor'].astype(str).unique() if v.strip()]
    if vend and all(v in cust_numbers for v in vend):
        return
    gc = guess_customer(inv.get('extracted_text') or '',
                        f"{inv.get('name', '')}\n{(inv.get('email') or {}).get('sender', '')}")
    if gc:
        df['Vendor'] = gc
        inv['customer'] = gc
        # Bump the editor key: data_editor caches widget state per key, so
        # the stale vendor string would otherwise overwrite this fix.
        inv['editor_version'] = inv.get('editor_version', 0) + 1


EMAIL_RULES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'email_rules.csv')


def load_email_rules():
    """Sender address/domain -> 'order' or 'ignore', learned from user triage.

    Baseline rules can be seeded via the EMAIL_RULES_CSV secret; learned
    rules are appended to a local file (not committed to the repo)."""
    text = os.getenv('EMAIL_RULES_CSV')
    try:
        if text:
            df = pd.read_csv(StringIO(text), dtype=str).fillna('')
        elif os.path.exists(EMAIL_RULES_CSV):
            df = pd.read_csv(EMAIL_RULES_CSV, dtype=str).fillna('')
        else:
            return {}
        return {str(r['Sender']).strip().lower(): str(r['Classification']).strip().lower()
                for _, r in df.iterrows() if str(r.get('Sender', '')).strip()}
    except Exception:
        pass
    return {}


def save_email_rule(sender_header, classification):
    """Remember how a sender was triaged so future mail is pre-classified."""
    addr = extract_email_address(sender_header).lower()
    if not addr:
        return
    rules = load_email_rules()
    rules[addr] = classification
    try:
        pd.DataFrame([{'Sender': k, 'Classification': v}
                      for k, v in sorted(rules.items())]).to_csv(EMAIL_RULES_CSV, index=False)
    except Exception as e:
        print(f"Could not save email rules: {e}")


def classify_email(item, rules):
    """Return 'order', 'ignore' or 'unknown' for an inbox item."""
    addr = extract_email_address(item.get('sender', '')).lower()
    domain = addr.split('@')[-1]
    if addr in rules:
        return rules[addr]
    if domain in rules:
        return rules[domain]
    subj = item.get('subject', '')
    if re.search(r'(?i)\border\b|purchase order|p\.?\s?o\.?\s*#', subj):
        return 'order'
    if guess_customer(item.get('sender', '') + ' ' + subj):
        return 'order'
    return 'unknown'


def save_env_vars(pairs):
    """Update or add keys in the .env file."""
    env_file = ".env"
    lines = []
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            lines = f.readlines()
    for key, value in pairs.items():
        updated = False
        new_lines = []
        for line in lines:
            if line.startswith(f'{key}='):
                new_lines.append(f'{key}={value}\n')
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f'{key}={value}\n')
        lines = new_lines
    with open(env_file, 'w') as f:
        f.writelines(lines)
    load_dotenv(override=True)


def build_invoice(pdf_bytes, pdf_name, doc_key, email_meta=None):
    """Extract text and parse a PDF into an invoice dict for the queue."""
    inv = {
        'key': doc_key,
        'name': pdf_name,
        'pdf_bytes': pdf_bytes,
        'pdf_images': render_pdf_images(pdf_bytes),
        'raw_csv_df': None,
        'extracted_text': None,
        'df': None,
        'customer': '',
        'email': email_meta,      # {'uid','subject','sender','date'} when loaded from inbox
        'confirmed': False,
    }
    st.success(f"Loaded: {pdf_name}")

    # Get Google credentials from environment
    google_credentials = os.getenv('GOOGLE_CREDENTIALS')

    if not google_credentials:
        st.warning("Google Cloud credentials not configured. Please add them in Settings.")
        st.info("Using placeholder mode for demo purposes")
        extracted_text = "Sample extracted text from PDF\nItem 1: Widget A - $10.00\nItem 2: Widget B - $20.00"
    else:
        with st.spinner(f"Extracting text from {pdf_name}..."):
            try:
                processor = VisionProcessor(credentials_json=google_credentials)
                extracted_text = processor.extract_text_from_bytes(pdf_bytes, file_type='pdf')
            except Exception as e:
                st.error(f"Error processing {pdf_name}: {str(e)}")
                extracted_text = ""
    inv['extracted_text'] = extracted_text

    # Parse order data
    with st.spinner(f"Parsing {pdf_name}..."):
        gemini_key = os.getenv('GEMINI_API_KEY')
        order_items = parse_order_text(extracted_text, gemini_api_key=gemini_key)

        if order_items:
            df = pd.DataFrame(order_items)
            # Reorder columns for better display
            cols = ['Purchase Order Number', 'Date of Order', 'Ship By Date', 'Ship To', 'Invoice Address', 'Vendor', 'Shipping Instructions', 'Comments', 'Inventory Item', 'Description', 'Quantity', 'Unit Price', 'Total']
            cols = [col for col in cols if col in df.columns]
            df = df[cols]
            st.success(f"✅ {pdf_name}: parsed {len(order_items)} item(s)")
        else:
            st.warning(f"⚠️ {pdf_name}: could not automatically parse - enter data manually below.")
            df = pd.DataFrame(columns=["Purchase Order Number", "Date of Order", "Ship By Date", "Ship To", "Invoice Address", "Vendor", "Shipping Instructions", "Comments", "Inventory Item", "Description", "Quantity", "Unit Price", "Total"])
            df.loc[0] = [""] * len(df.columns)

    inv['df'] = df
    # Auto-assign Sage customer number when a known customer is detected
    gc = guess_customer(extracted_text, pdf_name)
    if gc and 'Vendor' in inv['df'].columns:
        inv['df']['Vendor'] = gc
        inv['customer'] = gc
    return inv


def _apply_email_date(df, email_meta):
    """Fill empty Date of Order from the email's sent date."""
    if not email_meta or not email_meta.get('date'):
        return df
    if 'Date of Order' not in df.columns:
        return df
    if not (df['Date of Order'].astype(str).str.strip() == '').all():
        return df
    try:
        df['Date of Order'] = parsedate_to_datetime(email_meta['date']).strftime('%m/%d/%Y')
    except Exception:
        pass
    return df


def build_text_invoice(text, name, doc_key, email_meta=None):
    """Build a queue entry from plain email body text (no PDF attachment)."""
    inv = {
        'key': doc_key,
        'name': name,
        'pdf_bytes': None,
        'pdf_images': [],
        'raw_csv_df': None,
        'extracted_text': text,
        'df': None,
        'customer': '',
        'email': email_meta,
        'confirmed': False,
    }
    with st.spinner(f"Parsing {name}..."):
        order_items = parse_order_text(text, gemini_api_key=os.getenv('GEMINI_API_KEY'))
        if order_items:
            df = pd.DataFrame(order_items)
            cols = ['Purchase Order Number', 'Date of Order', 'Ship By Date', 'Ship To', 'Invoice Address', 'Vendor', 'Shipping Instructions', 'Comments', 'Inventory Item', 'Description', 'Quantity', 'Unit Price', 'Total']
            cols = [col for col in cols if col in df.columns]
            df = df[cols]
            st.success(f"✅ {name}: parsed {len(order_items)} item(s)")
        else:
            st.warning(f"⚠️ {name}: could not automatically parse - enter data manually below.")
            df = pd.DataFrame(columns=["Purchase Order Number", "Date of Order", "Ship By Date", "Ship To", "Invoice Address", "Vendor", "Shipping Instructions", "Comments", "Inventory Item", "Description", "Quantity", "Unit Price", "Total"])
            df.loc[0] = [""] * len(df.columns)
    inv['df'] = _apply_email_date(df, email_meta)
    gc = guess_customer(text, f"{name}\n{(email_meta or {}).get('sender', '')}")
    if gc and 'Vendor' in inv['df'].columns:
        inv['df']['Vendor'] = gc
        inv['customer'] = gc
    return inv


def parse_shipstation_csv(raw_bytes):
    """Map a ShipStation order-items CSV export to Sage 50 sales order rows."""
    src = pd.read_csv(StringIO(raw_bytes.decode('utf-8-sig', errors='replace')),
                      dtype=str).fillna('')
    order_col = 'Order #' if 'Order #' in src.columns else src.columns[0]
    order_counts = src[order_col].astype(str).str.strip().value_counts()
    rows = []
    for _, r in src.iterrows():
        order_num = str(r.get(order_col, '')).strip()
        multi_line = order_counts.get(order_num, 0) > 1
        # Amazon order IDs look like 114-3315411-3045845; eBay like 17-15192-30111
        if re.match(r'^\d{3}-\d{7}-\d{7}$', order_num):
            channel = 'Amazon'
        elif re.match(r'^\d{2}-\d{5}-\d{5}$', order_num):
            channel = 'eBay'
        else:
            channel = 'ShipStation'
        qty_str = str(r.get('Quantity', '')).strip()
        total_str = str(r.get('Order Total', '')).strip()
        # Amazon listings carry an 'AM' SKU prefix; Sage items use 'ATV'
        item_sku = str(r.get('Item SKU', '')).strip()
        if channel == 'Amazon':
            item_sku = re.sub(r'^AM', 'ATV', item_sku)
        # Column H (Order Total) is the line amount; derive unit price so
        # Quantity x Unit Price = Total stays consistent
        unit_price = ''
        try:
            q = float(qty_str)
            t = float(total_str)
            if q:
                unit_price = round(t / q, 4)
        except (ValueError, TypeError):
            pass
        rows.append({
            'Purchase Order Number': order_num,
            'Date of Order': str(r.get('Order Date', '')).strip(),
            'Ship By Date': str(r.get('Ship Date', '')).strip(),
            'Ship To': str(r.get('Recipient', '')).strip(),
            'Invoice Address': '',
            'Vendor': guess_customer(channel),
            'Shipping Instructions': str(r.get('Service', '')).strip(),
            'Comments': (f"{channel} order via ShipStation | Batch: {r.get('Batch', '')}"
                         + (" | NOTE: multi-line order - Order Total may be the order's grand total, verify line pricing"
                            if multi_line else "")),
            'Inventory Item': item_sku,
            'Description': str(r.get('Item Name', '')).strip(),
            'Quantity': qty_str,
            'Unit Price': unit_price,
            'Total': total_str,
        })
    return pd.DataFrame(rows), src


def build_csv_invoice(file_bytes, file_name, doc_key):
    """Build a queue entry from a ShipStation (or other) CSV export."""
    inv = {
        'key': doc_key,
        'name': file_name,
        'pdf_bytes': None,
        'pdf_images': [],
        'raw_csv_df': None,
        'extracted_text': file_bytes.decode('utf-8-sig', errors='replace'),
        'df': None,
        'customer': '',
        'email': None,
        'confirmed': False,
    }
    with st.spinner(f"Parsing {file_name}..."):
        try:
            df, src = parse_shipstation_csv(file_bytes)
            inv['raw_csv_df'] = src
            inv['df'] = df
            # If every row resolved to the same Sage customer, show it in the selector
            vendors = [v for v in df.get('Vendor', pd.Series(dtype=str)).unique() if str(v).strip()]
            if len(vendors) == 1:
                inv['customer'] = vendors[0]
            st.success(f"✅ {file_name}: {len(df)} line item(s) imported")
        except Exception as e:
            st.error(f"Error parsing {file_name}: {e}")
            inv['df'] = pd.DataFrame(columns=["Purchase Order Number", "Date of Order", "Ship By Date", "Ship To", "Invoice Address", "Vendor", "Shipping Instructions", "Comments", "Inventory Item", "Description", "Quantity", "Unit Price", "Total"])
    return inv

def _fmt_ss_date(value):
    """ShipStation ISO date '2026-10-12T13:00:00.0000000' -> '10/12/2026'."""
    if not value:
        return ''
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', str(value))
    return f"{int(m.group(2))}/{int(m.group(3))}/{int(m.group(1))}" if m else str(value)


def build_shipstation_order_invoice(order, doc_key, store_map=None):
    """Map a ShipStation API order to a queue invoice dict."""
    channel = ss_order_channel(order, store_map)
    num = str(order.get('orderNumber') or '')
    ship_to = order.get('shipTo') or {}
    ship_str = ', '.join(x for x in [
        ship_to.get('name'),
        ship_to.get('street1'),
        ship_to.get('street2'),
        ' '.join(p for p in [ship_to.get('city'), ship_to.get('state'),
                             ship_to.get('postalCode')] if p),
    ] if x)
    rows = []
    for it in (order.get('items') or []):
        qty = it.get('quantity') or 0
        try:
            qty_f = float(qty)
        except (TypeError, ValueError):
            qty_f = 0.0
        try:
            price_f = float(it.get('unitPrice'))
        except (TypeError, ValueError):
            price_f = None
        # Amazon listings carry an 'AM' SKU prefix; Sage items use 'ATV'
        sku = str(it.get('sku') or '').strip()
        if channel == 'Amazon':
            sku = re.sub(r'^AM', 'ATV', sku)
        rows.append({
            'Purchase Order Number': num,
            'Date of Order': _fmt_ss_date(order.get('orderDate')),
            'Ship By Date': _fmt_ss_date(order.get('shipByDate') or order.get('shipDate')),
            'Ship To': ship_str,
            'Invoice Address': '',
            'Vendor': guess_customer(channel),
            'Shipping Instructions': order.get('requestedShippingService')
                                     or order.get('serviceCode') or '',
            'Comments': f"{channel} order via ShipStation API | status: {order.get('orderStatus', '')}",
            'Inventory Item': sku,
            'Description': str(it.get('name') or '').strip(),
            'Quantity': qty,
            'Unit Price': price_f if price_f is not None else '',
            'Total': round(qty_f * price_f, 2) if price_f is not None else '',
        })
    df = pd.DataFrame(rows, columns=[
        "Purchase Order Number", "Date of Order", "Ship By Date", "Ship To",
        "Invoice Address", "Vendor", "Shipping Instructions", "Comments",
        "Inventory Item", "Description", "Quantity", "Unit Price", "Total"])
    vendors = [v for v in df['Vendor'].unique() if str(v).strip()] if not df.empty else []
    return {
        'key': doc_key,
        'name': f"ShipStation order {num}",
        'pdf_bytes': None,
        'pdf_images': [],
        'raw_csv_df': None,
        'extracted_text': json.dumps(order, indent=2, default=str)[:20000],
        'df': df,
        'customer': vendors[0] if len(vendors) == 1 else '',
        'email': None,
        'confirmed': False,
    }


# Sage 50 2019 US "Sales Order Journal" import template - column layout matches
# the company's own SO.CSV export exactly (verified against a real export).
SAGE_SO_COLUMNS = [
    'Customer ID', 'Customer Name', 'Sales Order/Proposal #', 'Date', 'Ship By', 'Proposal',
    'Proposal Accepted', 'Closed', 'Quote #', 'Drop Ship', 'Ship to Name',
    'Ship to Address-Line One', 'Ship to Address-Line Two', 'Ship to City',
    'Ship to State', 'Ship to Zipcode', 'Ship to Country', 'Customer PO',
    'Ship Via', 'Discount Amount', 'Displayed Terms', 'Sales Representative ID',
    'Accounts Receivable Account', 'Sales Tax ID', 'Invoice Note',
    'Note Prints After Line Items', 'Statement Note',
    'Stmt Note Prints Before Ref', 'Internal Note', 'Number of Distributions',
    'SO/Proposal Distribution', 'Quantity', 'Item ID', 'Description',
    'G/L Account', 'Unit Price', 'Tax Type', 'UPC / SKU', 'Weight', 'U/M ID',
    'U/M No. of Stocking Units', 'Stocking Quantity', 'Stocking Unit Price',
    'Amount', 'Job ID', 'Sales Tax Agency ID',
]


def build_pick_list_pdf(orders):
    """Printable pick list - all orders on one continuous sheet. Each order
    is a bold header row followed by its item lines with a blank 'Packed'
    column the packager writes on. orders = [(name, df), ...]"""
    from fpdf import FPDF

    def t(v):
        return str(v if v is not None else '').encode('latin-1', 'replace').decode('latin-1')

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    widths = [42, 92, 20, 22]

    def header_row():
        pdf.set_font('helvetica', 'B', 9)
        for h, w in zip(['Item', 'Description', 'Qty', 'Packed'], widths):
            pdf.cell(w, 7, h, border=1)
        pdf.ln()

    header_row()
    for name, df in orders:
        if df is None or df.empty:
            continue
        r0 = df.iloc[0]
        bits = [t(name)]
        for label, col in [('PO', 'Purchase Order Number'),
                           ('Ship By', 'Ship By Date'),
                           ('Cust', 'Vendor'),
                           ('To', 'Ship To')]:
            val = ' '.join(t(r0.get(col, '')).split())
            if val and val.lower() != 'nan':
                bits.append(f'{label}: {val}')
        pdf.set_font('helvetica', 'B', 9)
        pdf.set_fill_color(220, 230, 245)
        pdf.multi_cell(sum(widths), 7, t('  |  '.join(bits)),
                       border=1, fill=True)
        pdf.set_font('helvetica', '', 9)
        for _, r in df.iterrows():
            if pdf.get_y() > pdf.page_break_trigger - 10:
                pdf.add_page()
                header_row()
            pdf.cell(widths[0], 7, t(r.get('Inventory Item', ''))[:24], border=1)
            pdf.cell(widths[1], 7, t(r.get('Description', ''))[:60], border=1)
            pdf.cell(widths[2], 7, t(r.get('Quantity', '')), border=1)
            pdf.cell(widths[3], 7, '', border=1)
            pdf.ln()
    return bytes(pdf.output())


def _csv_safe(v):
    """Make a field safe for Sage's line-based CSV import: collapse
    newlines and neutralize embedded double quotes (9" -> 9in)."""
    s = ' '.join(str(v).split())
    s = re.sub(r'(\d)"', r'\1in', s)
    return s.replace('"', "'")


def _split_ship_to(text):
    """Split a multiline ship-to blob into Sage's address fields.
    Layout: name / address line(s) / 'City, ST ZIP' (last line)."""
    lines = [l.strip() for l in str(text or '').split('\n') if l.strip()]
    name = l1 = l2 = city = state = zipc = ''
    if lines:
        name = lines[0]
        m = re.match(r'^(.*?),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', lines[-1])
        addr = lines[1:-1] if m and len(lines) >= 2 else lines[1:]
        if m and len(lines) >= 2:
            city, state, zipc = m.group(1).strip(' ,'), m.group(2), m.group(3)
        l1 = addr[0] if addr else ''
        l2 = addr[1] if len(addr) > 1 else ''
    return name, l1, l2, city, state, zipc


def build_sage_sales_orders(df):
    """Transform the internal order table into the Sage 50 Sales Order
    Journal (SO.CSV) layout, matching the company's export template.

    - SO/Proposal # is left blank so Sage auto-assigns order numbers;
      the source order number goes in Customer PO
    - Amount is negative (sales order lines credit income)
    - Rows are grouped by order so each transaction's distribution
      lines stay contiguous
    """
    ar_acct = os.getenv('SAGE_AR_ACCOUNT', '12000')
    gl_acct = os.getenv('SAGE_GL_ACCOUNT', '30500')
    mkt_cust = os.getenv('SAGE_MARKETPLACE_CUSTOMER', '100B')
    cust_names = dict(zip(load_customers()['Customer Number'].astype(str),
                          load_customers()['Customer Name'].astype(str)))
    tax_id = os.getenv('SAGE_TAX_ID', '')
    tax_type = os.getenv('SAGE_TAX_TYPE', '1')
    terms = os.getenv('SAGE_TERMS', 'Net Due')

    # All ShipStation-sourced lines merge into ONE sales order (customer 100B
    # is a marketplace clearing account). Source order number and buyer ship-to
    # are preserved per-line in the Description: "[order#] ship-to - item".
    work = df.copy()
    comments = work['Comments'].astype(str).str.lower() if 'Comments' in work.columns else ''
    is_ss = comments.str.contains('via shipstation') if isinstance(comments, pd.Series) \
        else pd.Series(False, index=work.index)
    work['_group'] = work['Purchase Order Number'].astype(str)
    work.loc[is_ss, '_group'] = '__SHIPSTATION_BATCH__'

    out = []
    for grp_key, grp in work.groupby('_group', sort=False):
        is_batch = grp_key == '__SHIPSTATION_BATCH__'
        cust_po = 'ShipStation Orders' if is_batch else grp_key
        n = len(grp)
        for i, (_, r) in enumerate(grp.iterrows(), 1):
            qty = pd.to_numeric(r.get('Quantity'), errors='coerce')
            price = pd.to_numeric(r.get('Unit Price'), errors='coerce')
            total = pd.to_numeric(r.get('Total'), errors='coerce')
            if pd.notna(total) and total:
                amount = total
            elif pd.notna(qty) and pd.notna(price):
                amount = qty * price
            else:
                amount = ''
            if amount != '':
                amount = -abs(float(amount))  # sales order lines are credits
            desc = str(r.get('Description', ''))
            if is_batch:
                desc = (f"[{r.get('Purchase Order Number', '')}] "
                        f"{r.get('Ship To', '')} - {desc}")
            s_name, s_l1, s_l2, s_city, s_state, s_zip = _split_ship_to(r.get('Ship To', ''))
            cust_id = (r.get('Vendor', '') or mkt_cust) if is_batch else r.get('Vendor', '')
            out.append({
                'Customer ID': cust_id,
                'Customer Name': cust_names.get(str(cust_id), ''),
                'Sales Order/Proposal #': '',
                'Date': r.get('Date of Order', ''),
                'Ship By': r.get('Ship By Date', ''),
                'Proposal': 'FALSE',
                'Proposal Accepted': 'FALSE',
                'Closed': 'FALSE',
                'Quote #': '',
                'Drop Ship': 'FALSE',
                'Ship to Name': _csv_safe(s_name)[:30],
                'Ship to Address-Line One': _csv_safe(s_l1)[:30],
                'Ship to Address-Line Two': _csv_safe(s_l2)[:30],
                'Ship to City': _csv_safe(s_city)[:20],
                'Ship to State': _csv_safe(s_state)[:2],
                'Ship to Zipcode': _csv_safe(s_zip)[:10],
                'Ship to Country': 'USA' if s_zip else '',
                'Customer PO': _csv_safe(cust_po),
                'Ship Via': _csv_safe(str(r.get('Shipping Instructions', '')))[:12],
                'Discount Amount': '0',
                'Displayed Terms': terms,
                'Sales Representative ID': '',
                'Accounts Receivable Account': ar_acct,
                'Sales Tax ID': tax_id,
                'Invoice Note': '',
                'Note Prints After Line Items': 'FALSE',
                'Statement Note': '',
                'Stmt Note Prints Before Ref': 'FALSE',
                'Internal Note': _csv_safe(str(r.get('Comments', '')))[:2000],
                'Number of Distributions': n,
                'SO/Proposal Distribution': i,
                'Quantity': qty if pd.notna(qty) else r.get('Quantity', ''),
                'Item ID': str(r.get('Inventory Item', ''))[:20],
                'Description': _csv_safe(desc)[:160],
                'G/L Account': gl_acct,
                'Unit Price': price if pd.notna(price) else r.get('Unit Price', ''),
                'Tax Type': tax_type,
                'UPC / SKU': '',
                'Weight': '0.00',
                'U/M ID': '<Each>',
                'U/M No. of Stocking Units': '1.00',
                'Stocking Quantity': qty if pd.notna(qty) else r.get('Quantity', ''),
                'Stocking Unit Price': price if pd.notna(price) else r.get('Unit Price', ''),
                'Amount': amount,
                'Job ID': '',
                'Sales Tax Agency ID': '',
            })
    return pd.DataFrame(out, columns=SAGE_SO_COLUMNS)


def _orders_print_html(sage_df):
    """Print-ready pick sheet — one page per Sage transaction, with an
    in-page Print button (opens the browser print dialog)."""
    import html as _h
    pages = []
    for tx in transactions_from_sage_df(sage_df):
        lines = ''.join(
            "<tr>"
            f"<td>{_h.escape(str(l['item_id']))}</td>"
            f"<td>{_h.escape(str(l['description']))}</td>"
            f"<td class='num'>{_h.escape(str(l['quantity']))}</td>"
            "<td class='packed'></td></tr>"
            for l in tx['lines'])
        city_state_zip = ' '.join(p for p in [tx.get('ship_to_city', ''),
                                              tx.get('ship_to_state', ''),
                                              tx.get('ship_to_zip', '')] if p)
        ship_addr = '<br>'.join(
            _h.escape(x) for x in [tx['ship_to_name'],
                                   tx.get('ship_to_line1', ''),
                                   tx.get('ship_to_line2', ''),
                                   city_state_zip] if x)
        pages.append(
            "<div class='page'>"
            "<h2>RATECH — Sales Order Pick Sheet</h2>"
            "<table class='hdr'><tr>"
            f"<td><b>Customer:</b> {_h.escape(tx['customer_id'])}</td>"
            f"<td><b>PO:</b> {_h.escape(tx['customer_po'])}</td>"
            f"<td><b>Date:</b> {_h.escape(tx['order_date'])}</td>"
            f"<td><b>Ship by:</b> {_h.escape(tx['ship_by'])}</td>"
            f"<td><b>Via:</b> {_h.escape(tx['ship_via'])}</td></tr></table>"
            f"<p><b>Ship to:</b><br>{ship_addr}</p>"
            "<table class='lines'><tr><th>Item</th><th>Description</th>"
            "<th>Qty</th><th>Packed</th></tr>"
            f"{lines}</table></div>")
    return ("<!doctype html><html><head><style>"
            "body{font-family:Arial,sans-serif;margin:8px}"
            ".page{page-break-after:always;padding:12px}"
            ".hdr{width:100%;border-collapse:collapse}"
            ".hdr td{padding:2px 10px 2px 0;font-size:13px;white-space:nowrap}"
            ".lines{width:100%;border-collapse:collapse;margin-top:10px}"
            ".lines th,.lines td{border:1px solid #999;padding:5px;font-size:13px;text-align:left}"
            ".num{text-align:right}.packed{width:70px}"
            "#printbtn{position:fixed;top:8px;right:14px;padding:8px 18px;font-size:15px}"
            "@media print{#printbtn{display:none}}"
            "</style></head><body>"
            "<button id='printbtn' onclick='window.print()'>Print</button>"
            + ''.join(pages) + "</body></html>")


def _addr_key(order):
    """Normalized ship-to key for grouping orders going to the same address."""
    s = order.get('shipTo') or {}
    parts = [s.get('name'), s.get('street1'), s.get('city'),
             s.get('state'), s.get('postalCode')]
    key = '|'.join(str(p or '').strip().lower() for p in parts)
    return key if key.strip('|') else ''


def _merge_pdfs(pdf_blobs):
    """Merge a list of PDF byte-strings into a single PDF."""
    from pypdf import PdfReader, PdfWriter
    from io import BytesIO
    writer = PdfWriter()
    for b in pdf_blobs:
        for pg in PdfReader(BytesIO(b)).pages:
            writer.add_page(pg)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---- Login gate (only active when users are configured) ----
_users = get_users()
if _users and st.session_state.user is None:
    _c1, _c2, _c3 = st.columns([1, 1.1, 1])
    with _c2:
        st.markdown("### 🔐 Sign In")
        st.caption("Enter your credentials to continue")
        _uname = st.text_input("Username", key="login_user")
        _pw = st.text_input("Password", type="password", key="login_pass")
        if st.button("Sign In", type="primary", use_container_width=True):
            _u = check_login(_uname.strip(), _pw)
            if _u:
                st.session_state.user = _u
                st.rerun()
            else:
                st.error("Invalid username or password")
                st.caption(f"Configured users: {', '.join(get_users()) or 'none found'}")
        st.stop()

_allowed_pages = st.session_state.user['pages'] if (_users and st.session_state.user) else ALL_PAGES

# Sidebar navigation
st.sidebar.markdown("""
<div style="padding: 0.2rem 0.2rem 0.4rem;">
    <div style="font-size:1.5rem; font-weight:700; color:#2460A7;">📦 Ratech</div>
    <div style="font-size:0.8rem; color:#8a93a0;">Order Management</div>
</div>
""", unsafe_allow_html=True)
_nav_labels = [
    f"Order Processing :red[({len(st.session_state.invoices)})]"
    if p == "Order Processing" else p
    for p in _allowed_pages
]
_choice = st.sidebar.radio("Select Page", _nav_labels)
page = _allowed_pages[_nav_labels.index(_choice)]

st.sidebar.divider()
st.sidebar.caption("Connections")
for _label, _ok in [
    ("Vision OCR", os.getenv('GOOGLE_CREDENTIALS')),
    ("Gemini parser", os.getenv('GEMINI_API_KEY')),
    ("Email inbox", os.getenv('EMAIL_USER') and os.getenv('EMAIL_PASSWORD')),
    ("ShipStation", os.getenv('SHIPSTATION_API_KEY') and os.getenv('SHIPSTATION_API_SECRET')),
    ("Sage queue", sage_queue_configured()),
]:
    st.sidebar.markdown(f"{'🟢' if _ok else '⚪'} {_label}")
st.sidebar.caption(f"🗂 {len(st.session_state.invoices)} order(s) in queue")

if _users and st.session_state.user:
    st.sidebar.divider()
    st.sidebar.caption(f"Signed in as **{st.session_state.user['name']}**")
    if st.sidebar.button("🚪 Sign Out", use_container_width=True):
        st.session_state.user = None
        st.rerun()

if page == "Order Processing":
    st.header("📥 Order Processing")

    # Queue metrics
    _n_lines = sum(len(inv['df']) for inv in st.session_state.invoices
                   if inv.get('df') is not None)
    _n_matched = sum(1 for inv in st.session_state.invoices if inv.get('customer'))
    _m1, _m2, _m3 = st.columns(3)
    _m1.metric("Orders in queue", len(st.session_state.invoices))
    _m2.metric("Line items", _n_lines)
    _m3.metric("Customers assigned", _n_matched)

    # --- Inbox orders ---
    # Reads stay read-only; an email is marked read only after it has been
    # loaded into the queue or explicitly ignored. Emails received before
    # EMAIL_START_DATE are never shown (go-live cutoff).
    st.subheader("📥 Inbox Orders")
    email_host = os.getenv('EMAIL_HOST', 'imap.mail.yahoo.com')
    email_port = os.getenv('EMAIL_PORT', '993')
    email_user = os.getenv('EMAIL_USER')
    email_pass = os.getenv('EMAIL_PASSWORD')
    email_since = os.getenv('EMAIL_START_DATE', '12-Oct-2026')
    st.caption(f"Showing unread emails received on/after {email_since}. "
               "Load or Ignore marks an email read; untouched mail stays unread.")

    if st.button("🔍 Check Inbox for New Orders"):
        if not email_user or not email_pass:
            st.warning("Email credentials not configured. Add them in the Settings page.")
        else:
            with st.spinner("Checking inbox for unread emails..."):
                try:
                    items = list_unseen_orders(email_host, email_port, email_user,
                                               email_pass, since=email_since)
                    rules = load_email_rules()
                    for i in items:
                        i['classification'] = classify_email(i, rules)
                    items = [i for i in items if i['uid'] not in st.session_state.loaded_uids]
                    st.session_state.inbox_items = items
                    if items:
                        st.success(f"Found {len(items)} unread email(s)")
                    else:
                        st.info("No unread emails in the inbox.")
                except Exception as e:
                    st.error(f"Inbox check failed: {e}")

    for item in st.session_state.inbox_items:
        try:
            item_card = st.container(border=True)
        except TypeError:
            item_card = st.container()
        with item_card:
            item_cols = st.columns([5, 1, 1])
            with item_cols[0]:
                _cls = item.get('classification', 'unknown')
                _badge = {'order': '📦 order', 'ignore': '🚫 ignore'}.get(_cls, '❓ unknown')
                st.write(f"**{item['subject']}**")
                st.caption(f"{item['sender']} • {item['date']} • {_badge}")
            with item_cols[1]:
                if st.button("Load", key=f"load_{item['uid']}"):
                    if not email_user or not email_pass:
                        st.warning("Email credentials not configured.")
                    else:
                        with st.spinner("Loading PDF attachment(s)..."):
                            try:
                                pdfs = fetch_pdf_from_message(email_host, email_port, email_user, email_pass, item['uid'])
                            except Exception as e:
                                st.error(f"Failed to load attachment: {e}")
                                pdfs = []
                        if not pdfs:
                            # No PDF - the email body itself may be the order
                            try:
                                body = fetch_body_from_message(email_host, email_port,
                                                               email_user, email_pass, item['uid'])
                            except Exception as e:
                                st.error(f"Failed to read email body: {e}")
                                body = ''
                            if body.strip():
                                meta = {'uid': item['uid'], 'subject': item['subject'],
                                        'sender': item['sender'], 'date': item['date']}
                                inv_key = ('inbox', item['uid'], 'body')
                                if inv_key not in {inv['key'] for inv in st.session_state.invoices}:
                                    st.session_state.invoices.append(
                                        build_text_invoice(body, f"Email: {item['subject']}",
                                                           inv_key, email_meta=meta))
                                save_email_rule(item['sender'], 'order')
                                try:
                                    mark_as_read(email_host, email_port, email_user,
                                                 email_pass, item['uid'])
                                except Exception as e:
                                    st.warning(f"Order loaded but could not mark email read: {e}")
                                st.session_state.loaded_uids.add(item['uid'])
                                st.session_state.inbox_items = [i for i in st.session_state.inbox_items if i['uid'] != item['uid']]
                                st.rerun()
                            else:
                                st.warning("No PDF attachment and no readable email body.")
                        else:
                            existing = {inv['key'] for inv in st.session_state.invoices}
                            loaded = 0
                            for pdf in pdfs:
                                inv_key = ('inbox', item['uid'], pdf['filename'])
                                if inv_key not in existing:
                                    meta = {'uid': item['uid'], 'subject': item['subject'],
                                            'sender': item['sender'], 'date': item['date']}
                                    st.session_state.invoices.append(
                                        build_invoice(pdf['bytes'], pdf['filename'], inv_key,
                                                      email_meta=meta))
                                    existing.add(inv_key)
                                    loaded += 1
                            save_email_rule(item['sender'], 'order')
                            try:
                                mark_as_read(email_host, email_port, email_user,
                                             email_pass, item['uid'])
                            except Exception as e:
                                st.warning(f"Order loaded but could not mark email read: {e}")
                            st.session_state.loaded_uids.add(item['uid'])
                            st.session_state.inbox_items = [i for i in st.session_state.inbox_items if i['uid'] != item['uid']]
                            if loaded:
                                st.rerun()
            with item_cols[2]:
                if st.button("Ignore", key=f"ignore_{item['uid']}"):
                    save_email_rule(item['sender'], 'ignore')
                    if email_user and email_pass:
                        try:
                            mark_as_read(email_host, email_port, email_user,
                                         email_pass, item['uid'])
                        except Exception as e:
                            st.warning(f"Could not mark email read: {e}")
                    st.session_state.loaded_uids.add(item['uid'])
                    st.session_state.inbox_items = [i for i in st.session_state.inbox_items if i['uid'] != item['uid']]
                    st.rerun()

    st.divider()
    st.subheader("🛒 ShipStation Orders")
    ss_key = os.getenv('SHIPSTATION_API_KEY')
    ss_secret = os.getenv('SHIPSTATION_API_SECRET')
    if not ss_key or not ss_secret:
        st.caption("Add SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET in Settings or secrets to pull orders from the API.")
    else:
        ss1, ss2, ss3 = st.columns([2, 1, 1])
        with ss1:
            ss_status = st.selectbox(
                "Status",
                ["awaiting_shipment", "shipped", "awaiting_payment", "on_hold", "(all)"],
                key='ss_status')
        with ss2:
            ss_days = st.number_input("Days back", min_value=1, max_value=90,
                                      value=7, key='ss_days')
        with ss3:
            st.write("")  # align button
            fetch_clicked = st.button("🔄 Fetch Orders", use_container_width=True)
        if fetch_clicked:
            from datetime import datetime, timedelta
            since = (datetime.now() - timedelta(days=int(ss_days))
                     ).strftime('%Y-%m-%dT%H:%M:%S')
            with st.spinner("Fetching ShipStation orders..."):
                try:
                    try:
                        stores = ss_list_stores(ss_key, ss_secret)
                        st.session_state.ss_store_map = {
                            s.get('storeId'): s.get('marketplaceName') or s.get('storeName')
                            for s in stores}
                    except Exception:
                        pass  # store names are a nicety, not required
                    st.session_state.ss_orders = ss_list_orders(
                        ss_key, ss_secret,
                        status=None if ss_status == "(all)" else ss_status,
                        order_date_start=since)
                    if st.session_state.ss_orders:
                        st.success(f"Found {len(st.session_state.ss_orders)} order(s)")
                    else:
                        st.info("No ShipStation orders matched.")
                except Exception as e:
                    st.error(f"ShipStation fetch failed: {e}")

        _addr_counts = {}
        for _o in st.session_state.ss_orders:
            _k = _addr_key(_o)
            if _k:
                _addr_counts[_k] = _addr_counts.get(_k, 0) + 1

        for order in st.session_state.ss_orders:
            oid = str(order.get('orderId') or order.get('orderKey') or '')
            if oid in st.session_state.loaded_ss:
                continue
            try:
                oc = st.container(border=True)
            except TypeError:
                oc = st.container()
            with oc:
                oc_cols = st.columns([4, 1, 1])
                with oc_cols[0]:
                    n_items = len(order.get('items') or [])
                    st.write(f"**{order.get('orderNumber', '(no number)')}** — "
                             f"{ss_order_channel(order, st.session_state.ss_store_map)}")
                    _shared = _addr_counts.get(_addr_key(order), 0) > 1
                    st.caption(
                        f"{(order.get('shipTo') or {}).get('name', '')} • "
                        f"{_fmt_ss_date(order.get('orderDate'))} • "
                        f"{order.get('orderStatus', '')} • {n_items} item(s)"
                        + (" • 🔗 shares address" if _shared else ""))
                with oc_cols[1]:
                    if st.button("Load", key=f"ss_load_{oid}"):
                        st.session_state.invoices.append(
                            build_shipstation_order_invoice(order, ('shipstation', oid),
                                                            st.session_state.ss_store_map))
                        st.session_state.loaded_ss.add(oid)
                        st.rerun()
                with oc_cols[2]:
                    if oid not in st.session_state.ss_labels:
                        _sel = st.checkbox("Batch", key=f"sel_{oid}",
                                           help="Include in batch label printing")
                        if _sel:
                            st.session_state.ss_selected.add(oid)
                        else:
                            st.session_state.ss_selected.discard(oid)

                # --- Shipping label (billable: buys postage, marks shipped,
                #     notifies the marketplace) ---
                with st.expander("🏷️ Shipping label"):
                    _lbl = st.session_state.ss_labels.get(oid)
                    if _lbl:
                        st.success(f"✅ Label created — tracking {_lbl['tracking']}")
                        st.download_button(
                            "📥 Download label PDF",
                            _lbl['pdf'],
                            file_name=f"label-{order.get('orderNumber', oid)}.pdf",
                            mime="application/pdf",
                            key=f"lbl_dl_{oid}")
                    else:
                        if not st.session_state.ss_carriers:
                            try:
                                st.session_state.ss_carriers = ss_list_carriers(ss_key, ss_secret)
                            except Exception as e:
                                st.error(f"Could not load carriers: {e}")
                        _carriers = {c.get('name', c.get('code')): c.get('code')
                                     for c in st.session_state.ss_carriers}
                        if _carriers:
                            lc1, lc2 = st.columns(2)
                            with lc1:
                                _cname = st.selectbox("Carrier", list(_carriers),
                                                      key=f"lbl_car_{oid}")
                                _ccode = _carriers[_cname]
                            try:
                                _svc = st.session_state.ss_opts_cache.get(_ccode + '_svc') or \
                                    ss_list_services(ss_key, ss_secret, _ccode)
                                st.session_state.ss_opts_cache[_ccode + '_svc'] = _svc
                                _pkg = st.session_state.ss_opts_cache.get(_ccode + '_pkg') or \
                                    ss_list_packages(ss_key, ss_secret, _ccode)
                                st.session_state.ss_opts_cache[_ccode + '_pkg'] = _pkg
                            except Exception as e:
                                st.error(f"Carrier options failed: {e}")
                                _svc, _pkg = [], []
                            _svc_map = {s.get('name', s.get('code')): s.get('code') for s in _svc}
                            _pkg_map = {p.get('name', p.get('code')): p.get('code') for p in _pkg}
                            with lc2:
                                _sname = st.selectbox("Service", list(_svc_map) or ['(none)'],
                                                      key=f"lbl_svc_{oid}")
                                _pname = st.selectbox("Package", list(_pkg_map) or ['(none)'],
                                                      key=f"lbl_pkg_{oid}")
                            _w = (order.get('weight') or {}).get('value') or 16
                            lw1, lw2 = st.columns(2)
                            with lw1:
                                _wt = st.number_input("Weight (oz)", min_value=0.0,
                                                      value=float(_w), key=f"lbl_w_{oid}")
                            with lw2:
                                _test = st.checkbox("Test label (no postage)",
                                                    value=False, key=f"lbl_test_{oid}",
                                                    help="USPS/Stamps.com supports free test labels")
                            if st.button(f"Create label for {order.get('orderNumber', oid)}",
                                         type="primary", key=f"lbl_go_{oid}"):
                                if not _svc_map or not _pkg_map:
                                    st.error("Pick a carrier that has services/packages available.")
                                else:
                                    with st.spinner("Purchasing label..."):
                                        try:
                                            resp = ss_create_label(
                                                ss_key, ss_secret, oid,
                                                carrier_code=_ccode,
                                                service_code=_svc_map[_sname],
                                                package_code=_pkg_map[_pname],
                                                weight_oz=_wt, test=_test)
                                            st.session_state.ss_labels[oid] = {
                                                'tracking': resp.get('trackingNumber', ''),
                                                'pdf': label_pdf_bytes(resp)}
                                            st.rerun()
                                        except Exception as e:
                                            st.error(f"Label failed: {label_error_message(e)}")
                                            st.markdown(
                                                f"[🔧 Fix order in ShipStation]({order_web_url(oid)}) "
                                                f"— search order **{order.get('orderNumber', oid)}** "
                                                "and correct the ship-to address")

        # --- Batch label printing ---
        _sel_orders = [
            o for o in st.session_state.ss_orders
            if str(o.get('orderId') or o.get('orderKey') or '') in st.session_state.ss_selected
            and str(o.get('orderId') or o.get('orderKey') or '') not in st.session_state.ss_labels]
        if st.session_state.ss_selected or st.session_state.ss_batch_pdf:
            st.divider()
            st.subheader("🏷️ Batch Labels")
            for kind, line in st.session_state.ss_batch_results:
                st.write(line)
                if kind == 'fail':
                    st.markdown(
                        f"&nbsp;&nbsp;&nbsp;&nbsp;[🔧 Fix in ShipStation]({order_web_url('')}) "
                        "— correct the ship-to address, then retry")
            if st.session_state.ss_batch_pdf:
                st.download_button(
                    "📥 Download batch labels PDF",
                    st.session_state.ss_batch_pdf,
                    file_name="batch-labels.pdf",
                    mime="application/pdf",
                    type="primary")
                if st.button("Clear batch"):
                    st.session_state.ss_batch_pdf = None
                    st.session_state.ss_batch_results = []
                    st.session_state.ss_selected = set()
                    st.rerun()
            elif not _sel_orders:
                st.info("Selected orders already have labels.")
            else:
                st.caption(f"{len(_sel_orders)} order(s) selected — one carrier/service/package "
                           "applies to all; each order uses its own recorded weight.")
                if not st.session_state.ss_carriers:
                    try:
                        st.session_state.ss_carriers = ss_list_carriers(ss_key, ss_secret)
                    except Exception as e:
                        st.error(f"Could not load carriers: {e}")
                _carriers = {c.get('name', c.get('code')): c.get('code')
                             for c in st.session_state.ss_carriers}
                if _carriers:
                    bc1, bc2, bc3 = st.columns(3)
                    with bc1:
                        _bcname = st.selectbox("Carrier", list(_carriers), key='batch_carrier')
                    _bccode = _carriers[_bcname]
                    try:
                        _bsvc = st.session_state.ss_opts_cache.get(_bccode + '_svc') or \
                            ss_list_services(ss_key, ss_secret, _bccode)
                        st.session_state.ss_opts_cache[_bccode + '_svc'] = _bsvc
                        _bpkg = st.session_state.ss_opts_cache.get(_bccode + '_pkg') or \
                            ss_list_packages(ss_key, ss_secret, _bccode)
                        st.session_state.ss_opts_cache[_bccode + '_pkg'] = _bpkg
                    except Exception as e:
                        st.error(f"Carrier options failed: {e}")
                        _bsvc, _bpkg = [], []
                    _bsvc_map = {s.get('name', s.get('code')): s.get('code') for s in _bsvc}
                    _bpkg_map = {p.get('name', p.get('code')): p.get('code') for p in _bpkg}
                    with bc2:
                        _bsname = st.selectbox("Service", list(_bsvc_map) or ['(none)'],
                                               key='batch_svc')
                    with bc3:
                        _bpname = st.selectbox("Package", list(_bpkg_map) or ['(none)'],
                                               key='batch_pkg')
                    bw1, bw2 = st.columns(2)
                    with bw1:
                        _bwt = st.number_input("Default weight (oz) for orders without one",
                                               min_value=0.0, value=16.0, key='batch_w')
                    with bw2:
                        _btest = st.checkbox("Test labels (no postage)", value=False,
                                             key='batch_test')
                    _bcombine = st.checkbox(
                        "Combine same-address orders into single labels",
                        value=True, key='batch_combine',
                        help="Orders going to the same ship-to get one label; "
                             "siblings are marked shipped with the same tracking")
                    _n_labels = (len({_addr_key(o) or ('__solo__' + str(
                                     o.get('orderId') or o.get('orderKey') or ''))
                                     for o in _sel_orders})
                                 if _bcombine else len(_sel_orders))
                    if st.button(
                            f"Create {_n_labels} label(s) for {len(_sel_orders)} order(s)",
                            type="primary", key='batch_go'):
                        if not _bsvc_map or not _bpkg_map:
                            st.error("Pick a carrier that has services/packages available.")
                        else:
                            results, pdfs = [], []
                            if _bcombine:
                                groups = {}
                                for o in _sel_orders:
                                    _ak = _addr_key(o) or ('__solo__' + str(
                                        o.get('orderId') or o.get('orderKey') or ''))
                                    groups.setdefault(_ak, []).append(o)
                                work_groups = list(groups.values())
                            else:
                                work_groups = [[o] for o in _sel_orders]
                            total_units = sum(len(g) for g in work_groups)
                            done = 0
                            prog = st.progress(0, text="Creating labels...")
                            for grp in work_groups:
                                primary = grp[0]
                                oid2 = str(primary.get('orderId') or primary.get('orderKey') or '')
                                nums = ', '.join(str(x.get('orderNumber')) for x in grp)
                                wt = sum(float((x.get('weight') or {}).get('value') or 0)
                                         for x in grp) or _bwt
                                try:
                                    resp = ss_create_label(
                                        ss_key, ss_secret, oid2,
                                        carrier_code=_bccode,
                                        service_code=_bsvc_map[_bsname],
                                        package_code=_bpkg_map[_bpname],
                                        weight_oz=wt, test=_btest)
                                    track = resp.get('trackingNumber', '')
                                    pdf = label_pdf_bytes(resp)
                                    if pdf[:4] == b'%PDF':
                                        pdfs.append(pdf)
                                    st.session_state.ss_labels[oid2] = {
                                        'tracking': track, 'pdf': pdf}
                                    for sib in grp[1:]:
                                        sib_id = str(sib.get('orderId') or sib.get('orderKey') or '')
                                        try:
                                            ss_mark_as_shipped(
                                                ss_key, ss_secret, sib_id,
                                                _bccode, track,
                                                notify_marketplace=True)
                                            st.session_state.ss_labels[sib_id] = {
                                                'tracking': track, 'pdf': pdf}
                                        except Exception as e2:
                                            results.append(('fail',
                                                f"⚠️ {sib.get('orderNumber')}: label created but "
                                                f"not marked shipped - {label_error_message(e2)}"))
                                    tag = f" (combined {len(grp)} orders)" if len(grp) > 1 else ""
                                    results.append(('ok', f"✅ {nums}{tag}: {track}"))
                                except Exception as e:
                                    results.append(('fail', f"❌ {nums}: {label_error_message(e)}"))
                                done += len(grp)
                                prog.progress(done / total_units)
                            prog.empty()
                            if pdfs:
                                st.session_state.ss_batch_pdf = _merge_pdfs(pdfs)
                            else:
                                for kind, line in results:
                                    st.write(line)
                            st.session_state.ss_batch_results = results
                            st.rerun()

        _ss_pick = [(inv['name'], inv['df']) for inv in st.session_state.invoices
                    if inv['df'] is not None and not inv['df'].empty and (
                        inv['name'].startswith('ShipStation')
                        or ('Comments' in inv['df'].columns
                            and inv['df']['Comments'].astype(str).str.contains(
                                'shipstation', case=False).any()))]
        if _ss_pick:
            st.download_button(
                "🖨 Download pick list PDF (ShipStation orders)",
                data=build_pick_list_pdf(_ss_pick),
                file_name="pick-list.pdf",
                mime="application/pdf",
                help="Printable pick list of the ShipStation orders in the queue")

    st.divider()
    st.subheader("Upload Order File(s)")
    uploaded_files = st.file_uploader(
        "Choose PDF order(s) or ShipStation CSV export(s)",
        type=['pdf', 'csv'],
        accept_multiple_files=True,
        key=f"pdf_uploader_{st.session_state.upload_version}",
        help="Upload PDF order documents or ShipStation CSV exports - each becomes a card below, all export to one CSV"
    )

    if uploaded_files:
        existing = {inv['key'] for inv in st.session_state.invoices}
        for uf in uploaded_files:
            file_key = ('upload', uf.name, uf.size)
            if file_key not in existing:
                raw = bytes(uf.getbuffer())
                if uf.name.lower().endswith('.csv'):
                    inv = build_csv_invoice(raw, uf.name, file_key)
                else:
                    inv = build_invoice(raw, uf.name, file_key)
                st.session_state.invoices.append(inv)
                existing.add(file_key)

    # --- Invoice queue: one card per order, PDF left / data right ---
    if st.session_state.invoices:
        st.divider()
        st.subheader(f"Orders in Queue ({len(st.session_state.invoices)})")

        # Sage customer list for the per-card selector
        customers_df = load_customers()
        cust_numbers = [n for n in customers_df['Customer Number'] if str(n).strip()]
        cust_names = dict(zip(customers_df['Customer Number'], customers_df['Customer Name']))

        for idx, inv in enumerate(st.session_state.invoices):
            wkey = str(inv['key'])
            resolve_vendor(inv)
            try:
                card = st.container(border=True)
            except TypeError:
                card = st.container()
            with card:
                title_cols = st.columns([6, 1])
                with title_cols[0]:
                    cust_label = cust_names.get(inv.get('customer', ''), '')
                    st.subheader(f"📄 {inv['name']}"
                                 + (f"  ·  👤 {inv['customer']} {cust_label}" if inv.get('customer') else ""))
                    if inv.get('email'):
                        st.caption(f"✉️ Email: \"{inv['email']['subject']}\" — "
                                   f"{inv['email']['sender']} • {inv['email']['date']}")
                with title_cols[1]:
                    if st.button("🗑 Remove", key=f"remove_{wkey}"):
                        st.session_state.invoices.pop(idx)
                        st.session_state.upload_version += 1
                        st.rerun()

                col_pdf, col_data = st.columns([1, 1])

                with col_pdf:
                    if inv.get('raw_csv_df') is not None:
                        # Source CSV shown in a scrollable table for verification
                        try:
                            src_container = st.container(height=600)
                        except TypeError:
                            src_container = st.container()
                        with src_container:
                            st.caption("Source CSV data")
                            st.dataframe(inv['raw_csv_df'], use_container_width=True)
                    elif inv['pdf_images']:
                        # Scrollable container - PDF scrolls independently
                        try:
                            pdf_container = st.container(height=600)
                        except TypeError:
                            pdf_container = st.container()
                        with pdf_container:
                            for page_num, img in enumerate(inv['pdf_images']):
                                try:
                                    st.image(img, width='stretch',
                                             caption=f"Page {page_num + 1} of {len(inv['pdf_images'])}")
                                except TypeError:
                                    st.image(img, use_container_width=True,
                                             caption=f"Page {page_num + 1} of {len(inv['pdf_images'])}")
                    elif inv['pdf_bytes']:
                        try:
                            st.pdf(inv['pdf_bytes'], height=600)
                        except TypeError:
                            st.pdf(inv['pdf_bytes'])
                        except Exception:
                            st.download_button(
                                label="📥 Download PDF to view",
                                data=inv['pdf_bytes'],
                                file_name=inv['name'],
                                mime="application/pdf",
                                key=f"dl_{wkey}"
                            )
                    if inv.get('extracted_text'):
                        # Expand by default when there's no document preview to show
                        no_preview = inv['pdf_bytes'] is None and inv.get('raw_csv_df') is None
                        with st.expander("📧 Email / Raw Text", expanded=no_preview):
                            st.text_area(
                                "Raw Extracted Text",
                                inv['extracted_text'],
                                height=250,
                                label_visibility="collapsed",
                                key=f"raw_{wkey}"
                            )

                with col_data:
                    # Sage customer selector - applies the customer number to the
                    # Vendor column of every row in this order
                    cust_opts = [''] + cust_numbers
                    sel_idx = cust_opts.index(inv['customer']) if inv.get('customer', '') in cust_opts else 0
                    sel_cust = st.selectbox(
                        "Sage Customer #",
                        options=cust_opts,
                        index=sel_idx,
                        format_func=lambda n: f"{n} — {cust_names.get(n, '')}" if n else "Select customer...",
                        key=f"custsel_{wkey}"
                    )
                    if sel_cust and sel_cust != inv.get('customer', ''):
                        inv['customer'] = sel_cust
                        if 'Vendor' in inv['df'].columns:
                            inv['df']['Vendor'] = sel_cust
                        st.rerun()

                    edited_df = st.data_editor(
                        inv['df'],
                        num_rows="dynamic",
                        use_container_width=True,
                        key=f"editor_{wkey}_{inv.get('editor_version', 0)}"
                    )
                    # Keep Total in sync when the user edits Quantity/Price -
                    # but only on rows that changed, so the OCR mismatch check
                    # still sees the document's original totals on first render.
                    _pc = ('Unit Price' if 'Unit Price' in edited_df.columns
                           else ('Price' if 'Price' in edited_df.columns else None))
                    if _pc and 'Quantity' in edited_df.columns and 'Total' in edited_df.columns:
                        _q = pd.to_numeric(edited_df['Quantity'], errors='coerce').fillna(0)
                        _p = pd.to_numeric(edited_df[_pc], errors='coerce').fillna(0)
                        _prev = inv['df']
                        if (_prev is not None and len(_prev) == len(edited_df)
                                and 'Quantity' in _prev.columns and _pc in _prev.columns):
                            _qo = pd.to_numeric(_prev['Quantity'], errors='coerce').fillna(0)
                            _po = pd.to_numeric(_prev[_pc], errors='coerce').fillna(0)
                            _chg = (_q != _qo) | (_p != _po)
                            edited_df.loc[_chg, 'Total'] = (_q * _p)[_chg]
                        else:
                            edited_df['Total'] = _q * _p
                    inv['df'] = edited_df
                    # Re-resolve AFTER the editor hands data back - its cached
                    # widget state can still carry the stale vendor string.
                    resolve_vendor(inv)

                    qc1, qc2, qc3 = st.columns(3)
                    with qc1:
                        if st.button("📋 Copy Header to All Rows", key=f"copyhdr_{wkey}"):
                            if not edited_df.empty:
                                header_cols = ['Purchase Order Number', 'Date of Order', 'Ship By Date', 'Ship To', 'Invoice Address', 'Vendor', 'Shipping Instructions', 'Comments']
                                for col in header_cols:
                                    if col in edited_df.columns:
                                        first_value = edited_df[col].dropna().iloc[0] if not edited_df[col].dropna().empty else ""
                                        if first_value:
                                            edited_df[col] = first_value
                                inv['df'] = edited_df
                                st.rerun()
                    with qc2:
                        if st.button("➕ Add Row", key=f"addrow_{wkey}"):
                            if not edited_df.empty:
                                new_row = {col: "" for col in edited_df.columns}
                                header_cols = ['Purchase Order Number', 'Date of Order', 'Ship By Date', 'Ship To', 'Invoice Address', 'Vendor', 'Shipping Instructions', 'Comments']
                                for col in header_cols:
                                    if col in edited_df.columns:
                                        first_value = edited_df[col].dropna().iloc[0] if not edited_df[col].dropna().empty else ""
                                        if first_value:
                                            new_row[col] = first_value
                                inv['df'] = pd.concat([edited_df, pd.DataFrame([new_row])], ignore_index=True)
                                st.rerun()
                    with qc3:
                        st.download_button(
                            "📥 This order → SO.CSV",
                            data=build_sage_sales_orders(edited_df).to_csv(index=False)
                                 if not edited_df.empty else "",
                            file_name=f"SO_{inv['name'].rsplit('.', 1)[0]}.csv",
                            mime="text/csv",
                            key=f"ordercsv_{wkey}",
                            disabled=edited_df.empty,
                        )

                # Order confirmation - only for orders loaded from the inbox
                if inv.get('email'):
                    if inv.get('confirmed'):
                        st.caption("✅ Order confirmation email sent")
                    else:
                        if st.button("✉️ Send Order Confirmation", key=f"confirm_{wkey}"):
                            smtp_host = os.getenv('EMAIL_SMTP_HOST', 'smtp.mail.yahoo.com')
                            smtp_port = os.getenv('EMAIL_SMTP_PORT', '465')
                            to_addr = extract_email_address(inv['email']['sender'])
                            if not email_user or not email_pass:
                                st.warning("Email credentials not configured.")
                            elif not to_addr:
                                st.error(f"Could not determine sender address from '{inv['email']['sender']}'")
                            else:
                                po = ''
                                if inv['df'] is not None and 'Purchase Order Number' in inv['df'].columns and not inv['df'].empty:
                                    pos = inv['df']['Purchase Order Number'].dropna()
                                    po = str(pos.iloc[0]) if not pos.empty else ''
                                n = len(inv['df']) if inv['df'] is not None else 0
                                total_str = ''
                                if inv['df'] is not None and 'Total' in inv['df'].columns:
                                    s = pd.to_numeric(inv['df']['Total'], errors='coerce').fillna(0).sum()
                                    total_str = f"${s:,.2f}"
                                body = (
                                    "Hello,\n\n"
                                    "Your purchase order has been received and entered into our system.\n\n"
                                    f"PO Number: {po}\n"
                                    f"Line Items: {n}\n"
                                    f"Order Total: {total_str}\n\n"
                                    "If anything looks incorrect, please reply to this email.\n\n"
                                    "Thank you,\nRatech Manufacturing\n"
                                    f"{email_user}"
                                )
                                with st.spinner("Sending confirmation..."):
                                    try:
                                        send_email(smtp_host, smtp_port, email_user, email_pass,
                                                   to_addr, f"RE: {inv['email']['subject']}", body)
                                        inv['confirmed'] = True
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Failed to send confirmation: {e}")
            st.markdown("")

        # --- Combined validation & export ---
        # Last-chance vendor resolution so the export always carries Sage
        # customer numbers even if a card's table state is stale.
        for inv in st.session_state.invoices:
            resolve_vendor(inv)
        all_dfs = [inv['df'] for inv in st.session_state.invoices
                   if inv['df'] is not None and not inv['df'].empty]
        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True)

            # Numeric coercion + fill missing totals
            for col in ['Quantity', 'Unit Price', 'Price', 'Total']:
                if col in combined.columns:
                    combined[col] = pd.to_numeric(combined[col], errors='coerce').fillna(0)
            price_col = 'Unit Price' if 'Unit Price' in combined.columns else ('Price' if 'Price' in combined.columns else None)
            if price_col and 'Quantity' in combined.columns and 'Total' in combined.columns:
                mask = (combined['Total'].isna()) | (combined['Total'] == 0)
                combined.loc[mask, 'Total'] = combined.loc[mask, 'Quantity'] * combined.loc[mask, price_col]

            # Calculation error check across all invoices
            errors = []
            if price_col and 'Quantity' in combined.columns and 'Total' in combined.columns:
                for ridx, row in combined.iterrows():
                    try:
                        qty = float(row['Quantity']) if pd.notna(row['Quantity']) else 0
                        price = float(row[price_col]) if pd.notna(row[price_col]) else 0
                        total = float(row['Total']) if pd.notna(row['Total']) else 0
                        calculated_total = qty * price
                        if abs(calculated_total - total) > 0.01:
                            errors.append({
                                'Row': ridx,
                                'Item': row.get('Description', 'Unknown'),
                                'Quantity': qty,
                                'Price': price,
                                'Total Listed': total,
                                'Total Calculated': calculated_total,
                                'Difference': calculated_total - total
                            })
                    except (ValueError, TypeError):
                        continue

            try:
                export_card = st.container(border=True)
            except TypeError:
                export_card = st.container()
            with export_card:
                if errors:
                    st.error(f"⚠️ Found {len(errors)} calculation error(s):")
                    st.dataframe(pd.DataFrame(errors), use_container_width=True)
                    if st.button("🧮 Auto-Calculate All Totals", type="primary"):
                        for inv in st.session_state.invoices:
                            df2 = inv['df']
                            if df2 is not None and not df2.empty and price_col and 'Quantity' in df2.columns:
                                df2['Quantity'] = pd.to_numeric(df2['Quantity'], errors='coerce').fillna(0)
                                df2[price_col] = pd.to_numeric(df2[price_col], errors='coerce').fillna(0)
                                df2['Total'] = df2['Quantity'] * df2[price_col]
                                inv['df'] = df2
                        st.rerun()
                else:
                    st.success(f"✅ All calculations correct across {len(all_dfs)} order(s), {len(combined)} line item(s)")

                st.subheader("Export Combined CSV")
                sage_df = build_sage_sales_orders(combined)
                n_tx = int((sage_df['SO/Proposal Distribution'] == 1).sum())
                st.caption(f"{len(sage_df)} line item(s) across {n_tx} Sage sales order(s) "
                           "- Sage 50 Sales Order Journal (SO.CSV) layout. "
                           "ShipStation orders merge into one sales order under customer 100B.")
                if not os.getenv('SAGE_AR_ACCOUNT') or not os.getenv('SAGE_GL_ACCOUNT'):
                    st.caption("Using built-in defaults: AR account 12000, G/L account 30500 "
                               "- override in Settings if needed.")
                with st.expander("Preview SO.CSV data (Sage format)", expanded=True):
                    st.dataframe(sage_df, use_container_width=True)
                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    st.download_button(
                        label="📥 Download SO.CSV for Sage 50",
                        data=sage_df.to_csv(index=False),
                        file_name="SO.CSV",
                        mime="text/csv",
                        type="primary",
                        use_container_width=True
                    )
                with ec2:
                    if sage_queue_configured():
                        if st.button("🚀 Send to Sage", use_container_width=True,
                                     help="Queue these orders for the desktop "
                                          "bridge to post into Sage 50"):
                            bad = set()
                            for _col in ('Date of Order', 'Ship By Date'):
                                if _col in combined.columns:
                                    for _v in combined[_col]:
                                        _s = str(_v).strip()
                                        if not _s or _s.lower() in ('nan', 'none', 'nat'):
                                            continue
                                        try:
                                            pd.to_datetime(_s)
                                        except Exception:
                                            bad.add(f"{_col} '{_s}'")
                            if bad:
                                st.error("Invalid date(s) — fix before sending: "
                                         + "; ".join(sorted(bad)[:6]))
                            else:
                                try:
                                    sent, skipped = sage_queue_push(sage_df)
                                    msg = f"Queued {len(sent)} order(s) for Sage."
                                    if skipped:
                                        msg += (f" {len(skipped)} already in the queue "
                                                "(unchanged orders are never re-posted).")
                                    packed_new, _ = create_open_orders(sage_df)
                                    if packed_new:
                                        msg += f" {len(packed_new)} released to packing."
                                    st.success(msg)
                                except Exception as e:
                                    st.error(f"Queue push failed: {e}")
                    else:
                        st.caption("Supabase not configured - see sage_bridge/README.md")
                with ec3:
                    if st.button("🧹 Clear All Orders", use_container_width=True):
                        st.session_state.invoices = []
                        st.session_state.upload_version += 1
                        st.rerun()

                if sage_queue_configured():
                    with st.expander("🖨️ Print order sheets"):
                        st.caption("One printable page per sales order — "
                                   "click the Print button inside the sheet.")
                        components.html(_orders_print_html(sage_df),
                                        height=520, scrolling=True)

                if sage_queue_configured():
                    with st.expander("📤 Sage queue status"):
                        try:
                            rows = sage_queue_status()
                            if rows:
                                qdf = pd.DataFrame([{
                                    'Status': r['status'],
                                    'Sage SO #': r.get('sage_order_no') or '',
                                    'Customer PO': (r.get('payload') or {}).get('customer_po', ''),
                                    'Error': (r.get('error') or '')[:120],
                                    'Queued': str(r.get('created_at', ''))[:19],
                                } for r in rows])
                                st.dataframe(qdf, use_container_width=True,
                                             hide_index=True)
                            else:
                                st.caption("Queue is empty.")
                        except Exception as e:
                            st.error(f"Could not load queue: {e}")

elif page == "Create Invoices":
    st.header("📦 Create Invoices")
    st.caption("Packing list: enter the quantity actually packaged on each "
               "line, then mark the order. Short orders are pushed back to "
               "Sage automatically.")

    if not sage_queue_configured():
        st.warning("Supabase not configured - see sage_bridge/README.md")
    else:
        show_done = st.checkbox("Show completed orders", value=False)
        try:
            open_orders = list_open_orders(include_done=show_done)
        except Exception as e:
            open_orders = []
            st.error(f"Could not load open orders: {e}")
        if not open_orders:
            st.info("No open sales orders.")
        badge = {'open': '🟡', 'short': '🟠', 'complete': '✅'}
        for o in open_orders:
            so_no = o.get('sage_order_no') or '—'
            title = (f"{badge.get(o['status'], '⚪')} SO {so_no} · "
                     f"{o.get('customer_id', '')} · {o.get('customer_po', '')}")
            if o.get('packed_by'):
                title += f" · packed by {o['packed_by']}"
            with st.expander(title, expanded=(o['status'] != 'complete')):
                raw_lines = o.get('lines') or []
                for l in raw_lines:
                    if l.get('stock_warning'):
                        st.warning(f"⚠️ {l.get('item_id')}: {l['stock_warning']}")
                        for c in (l.get('stock_components') or []):
                            ac1, ac2, ac3 = st.columns([4, 2, 2])
                            ac1.markdown(f"**{c['item']}** — need {c['need']:g}, have {c['have']:g}")
                            adj_q = ac2.number_input(
                                "receive qty", min_value=0.0, step=1.0,
                                value=max(0.0, float(c['need']) - float(c['have'])),
                                key=f"stadj_{o['order_key']}_{l['item_id']}_{c['item']}",
                                label_visibility="collapsed")
                            if ac3.button("Adjust", key=f"stbtn_{o['order_key']}_{l['item_id']}_{c['item']}"):
                                if adj_q > 0:
                                    try:
                                        queue_inventory_adjustment(
                                            str(c['item']), float(adj_q),
                                            f"stock for {o.get('customer_po') or o['order_key']}")
                                        note_line_adjustment(o['order_key'], str(l['item_id']),
                                                             str(c['item']), float(adj_q))
                                        st.success(f"Queued +{adj_q:g} for {c['item']} — "
                                                   "re-save packed qty after it imports.")
                                    except Exception as e:
                                        st.error(f"Could not queue adjustment: {e}")
                        if l.get('adjust_note'):
                            st.caption(f"✓ {l['adjust_note']} — warning clears when the build imports")
                df = pd.DataFrame([{
                    'Item': l.get('item_id'),
                    'Description': ('⚙ ' if l.get('assembly') else '')
                                   + str(l.get('description') or ''),
                    'Required': l.get('qty_required'),
                    'Packed': l.get('qty_packed'),
                } for l in raw_lines])
                ed = st.data_editor(
                    df, use_container_width=True,
                    key=f"open_{o['order_key']}",
                    disabled=(o['status'] == 'complete'))
                req = pd.to_numeric(ed['Required'], errors='coerce').fillna(0)
                packed = pd.to_numeric(ed['Packed'], errors='coerce').fillna(0)
                any_short = bool((packed < req).any())
                packed_by = ((st.session_state.user or {}).get('name', '')
                             if st.session_state.get('user') else '')
                bc1, bc2 = st.columns([4, 1])
                with bc1:
                    mark = st.button(
                        "⚠️ Mark Short — update Sage" if any_short else "✅ Mark Complete",
                        key=f"done_{o['order_key']}", type='primary',
                        disabled=(o['status'] == 'complete'),
                        use_container_width=True)
                with bc2:
                    if st.button("🗑", key=f"del_{o['order_key']}",
                                 help="Remove from packing list (also deletes "
                                      "the Sage sales order)",
                                 use_container_width=True):
                        try:
                            if o.get('sage_order_no'):
                                queue_order_void(o['order_key'],
                                                 o['sage_order_no'])
                            delete_open_order(o['order_key'])
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not delete: {e}")
                if mark:
                    try:
                        new_lines = []
                        builds = []
                        for i, (_, r) in enumerate(ed.iterrows()):
                            prev = raw_lines[i] if i < len(raw_lines) else {}
                            desc = re.sub(r'^⚙ ', '', str(r['Description']))
                            packed_qty = (float(r['Packed'])
                                          if pd.notna(r['Packed']) else 0)
                            prev_built = float(prev.get('built_qty') or 0)
                            new_lines.append({
                                'item_id': r['Item'],
                                'description': desc,
                                'qty_required': r['Required'],
                                'qty_packed': (r['Packed']
                                               if pd.notna(r['Packed']) else None),
                                'assembly': bool(prev.get('assembly')),
                                'built_qty': (packed_qty
                                              if prev.get('assembly')
                                              else prev.get('built_qty')),
                            })
                            if (prev.get('assembly')
                                    and packed_qty > prev_built):
                                builds.append({
                                    'item_id': r['Item'],
                                    'qty': packed_qty - prev_built,
                                    'description': desc})
                        update_packed(o['order_key'], new_lines,
                                      'short' if any_short else 'complete',
                                      packed_by=packed_by)
                        if builds:
                            queue_assembly_build(o['order_key'], builds)
                        if any_short and o.get('sage_order_no'):
                            queue_qty_update(o['order_key'], o['sage_order_no'],
                                             new_lines)
                            st.warning("Marked short — quantity update queued for Sage.")
                        elif any_short:
                            st.warning("Marked short — Sage SO number not known yet; "
                                       "the office will need to enter this manually.")
                        else:
                            if o.get('sage_order_no'):
                                try:
                                    queue_invoice(o['order_key'],
                                                  o['sage_order_no'], new_lines)
                                    st.info("Invoice queued for Sage.")
                                except Exception as e:
                                    st.warning(f"Could not queue invoice: {e}")
                            st.success("Marked complete.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not save: {e}")

elif page == "Inventory":
    st.header("🧮 Inventory")
    st.markdown("Queue a quantity adjustment (**+/−**) — the Sage bridge "
                "writes it to the Inventory Adjustments Journal and imports it.")
    with st.form("inv_adj_form", clear_on_submit=True):
        adj_item = st.text_input("Item ID")
        adj_qty = st.number_input("Quantity change (+/-)", step=1.0, format="%g")
        adj_reason = st.text_input("Reason", placeholder="e.g. physical count correction")
        if st.form_submit_button("Queue adjustment", type="primary"):
            if not adj_item.strip() or not adj_qty:
                st.error("Item ID and a nonzero quantity are required.")
            elif not sage_queue_configured():
                st.error("Supabase is not configured.")
            else:
                try:
                    queue_inventory_adjustment(
                        adj_item.strip(), float(adj_qty),
                        adj_reason.strip() or "manual adjustment")
                    st.success("Adjustment queued — the bridge will write and "
                               "import it on its next sweep.")
                except Exception as e:
                    st.error(f"Could not queue adjustment: {e}")
    if sage_queue_configured():
        try:
            adj_jobs = [r for r in sage_queue_status(50)
                        if r.get('order_key', '').startswith('adj-')][:10]
            if adj_jobs:
                st.subheader("Recent adjustments")
                st.dataframe(pd.DataFrame([{
                    'Queued': r.get('created_at', '')[:16].replace('T', ' '),
                    'Status': r.get('status'),
                    'Result': r.get('error') or r.get('sage_order_no') or '',
                } for r in adj_jobs]), use_container_width=True,
                hide_index=True)
        except Exception:
            pass

elif page == "Customers":
    st.header("👥 Customers")
    st.markdown("""
    Map customers to their **Sage 50 customer numbers** — *your* number for them,
    which is what the exported sales order needs. This is written into the
    `Vendor` column of the exported CSV.

    - **Customer Number** — YOUR Sage 50 customer ID for them (exported)
    - **Customer Name** — display name
    - **Aliases** — semicolon-separated keywords used to auto-match orders
      (e.g. `summitracing` matches any document containing that text; `amazon`/`ebay`
      match ShipStation order channels)
    - **Their Code For Us** — reference only: the number/code THEY use for Ratech
      on their documents (e.g. Ford's `DBYBA`). Not exported.
    """)

    if 'cust_override' not in st.session_state:
        st.session_state.cust_override = None
    if 'cust_editor_ver' not in st.session_state:
        st.session_state.cust_editor_ver = 0

    with st.expander("📥 Bulk load (upload a file or paste CSV text)"):
        up_file = st.file_uploader("Upload a customer CSV", type=['csv'],
                                   key="cust_csv_upload")
        pasted = st.text_area("...or paste CSV text", height=120,
                              placeholder="Customer Number,Customer Name,Aliases,Their Code For Us\n...")
        mode = st.radio("Load mode", ["Replace table", "Append to table"],
                        horizontal=True, key="cust_load_mode")
        if st.button("Load Customers"):
            try:
                text = (bytes(up_file.getbuffer()).decode('utf-8-sig', errors='replace')
                        if up_file is not None else pasted)
                new_df = pd.read_csv(StringIO(text), dtype=str).fillna('')
                # Map common header variants onto our columns
                ren = {}
                for c in new_df.columns:
                    cl = c.strip().lower()
                    if cl in ('customer number', 'customer id', 'customer no',
                              'customer #', 'cust id', 'cust number', 'id'):
                        ren[c] = 'Customer Number'
                    elif cl in ('customer name', 'name', 'customer'):
                        ren[c] = 'Customer Name'
                    elif cl in ('aliases', 'alias', 'keywords'):
                        ren[c] = 'Aliases'
                    elif cl in ('their code for us', 'their code', 'customer code',
                                'our account', 'account number', 'account #'):
                        ren[c] = 'Their Code For Us'
                new_df = new_df.rename(columns=ren)
                if 'Customer Number' not in new_df.columns and len(new_df.columns):
                    fallback = {new_df.columns[0]: 'Customer Number'}
                    if len(new_df.columns) > 1:
                        fallback[new_df.columns[1]] = 'Customer Name'
                    new_df = new_df.rename(columns=fallback)
                for c in CUSTOMER_COLS:
                    if c not in new_df.columns:
                        new_df[c] = ''
                new_df = new_df[CUSTOMER_COLS]
                if mode == "Append to table":
                    base = (st.session_state.cust_override
                            if st.session_state.cust_override is not None
                            else load_customers())
                    new_df = pd.concat([base, new_df], ignore_index=True)
                st.session_state.cust_override = new_df
                st.session_state.cust_editor_ver += 1
                st.rerun()
            except Exception as e:
                st.error(f"Could not parse CSV: {e}")

    cust_df = (st.session_state.cust_override
               if st.session_state.cust_override is not None
               else load_customers())
    edited_cust = st.data_editor(
        cust_df,
        num_rows="dynamic",
        use_container_width=True,
        key=f"customers_editor_{st.session_state.cust_editor_ver}"
    )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save Customers", type="primary"):
            try:
                edited_cust.to_csv(CUSTOMERS_CSV, index=False)
                st.session_state.cust_override = edited_cust
                st.success("✅ Customers saved")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save: {e}")
    with c2:
        st.download_button(
            label="Download customers.csv",
            data=edited_cust.to_csv(index=False),
            file_name="customers.csv",
            mime="text/csv"
        )

    st.caption("Customer data lives in the `CUSTOMERS_CSV` secret (not committed to the "
               "repo). After editing, copy the block below into Streamlit Cloud "
               "**Secrets** to make it permanent.")
    with st.expander("Copy into Streamlit secrets"):
        st.code('CUSTOMERS_CSV = """\n' + edited_cust.to_csv(index=False) + '"""',
                language="toml")

elif page == "Inventory Management":
    st.header("📋 Inventory Management")
    st.markdown("Photograph a packing slip, verify expected vs received counts, and export the inventory update.")

    if 'receipt' not in st.session_state:
        st.session_state.receipt = None
    if 'receipt_id' not in st.session_state:
        st.session_state.receipt_id = None

    st.subheader("📷 Capture Packing Slip")
    src = st.file_uploader("Take or upload a photo of the packing slip",
                           type=['png', 'jpg', 'jpeg', 'webp'],
                           key='packing_uploader',
                           help="On mobile, tap Browse and choose 'Take Photo' to use the camera directly")

    if src is not None:
        img_bytes = bytes(src.getbuffer())
        rid = hashlib.sha256(img_bytes).hexdigest()
        if st.session_state.receipt_id != rid:
            google_credentials = os.getenv('GOOGLE_CREDENTIALS')
            if not google_credentials:
                st.warning("Google Cloud credentials not configured - needed to read photos. Add them in Settings.")
            else:
                with st.spinner("Reading packing slip..."):
                    try:
                        processor = VisionProcessor(credentials_json=google_credentials)
                        text = processor.extract_text_from_bytes(img_bytes, file_type='image')
                        parsed = parse_packing_slip_text(text, gemini_api_key=os.getenv('GEMINI_API_KEY'))

                        cols = ['PO Number', 'Vendor', 'Date', 'Inventory Item', 'Description',
                                'Expected Qty', 'Received Qty', 'Notes']
                        items = parsed.get('items') or []
                        df = pd.DataFrame(items)
                        df = df.rename(columns={'Expected Quantity': 'Expected Qty',
                                                'Received Quantity': 'Received Qty'})
                        for c in cols:
                            if c not in df.columns:
                                df[c] = ''
                        df = df[cols]
                        # Apply slip-level header fields to all rows
                        for c, v in (('PO Number', parsed.get('po_number', '')),
                                     ('Vendor', parsed.get('vendor', '')),
                                     ('Date', parsed.get('date', ''))):
                            if v and (df[c].astype(str).str.strip() == '').all():
                                df[c] = v
                        st.session_state.receipt = {'image': img_bytes, 'text': text, 'df': df}
                        st.session_state.receipt_id = rid
                        st.success(f"✅ Read {len(df)} line item(s) from the packing slip")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not read packing slip: {e}")

    rec = st.session_state.receipt
    if rec:
        st.divider()
        col_img, col_tbl = st.columns([1, 1])

        with col_img:
            st.caption("Packing slip photo")
            try:
                img_container = st.container(height=600)
            except TypeError:
                img_container = st.container()
            with img_container:
                try:
                    st.image(rec['image'], width='stretch')
                except TypeError:
                    st.image(rec['image'], use_container_width=True)
            if rec.get('text'):
                with st.expander("Raw Extracted Text"):
                    st.text_area("Raw Extracted Text", rec['text'], height=250,
                                 label_visibility="collapsed", key="receipt_raw")

        with col_tbl:
            edited = st.data_editor(
                rec['df'],
                num_rows="dynamic",
                use_container_width=True,
                key="receipt_editor"
            )
            rec['df'] = edited

            if not edited.empty:
                work = edited.copy()
                work['Expected Qty'] = pd.to_numeric(work['Expected Qty'], errors='coerce')
                work['Received Qty'] = pd.to_numeric(work['Received Qty'], errors='coerce')
                work['Variance'] = work['Received Qty'] - work['Expected Qty']

                pending = work[work['Received Qty'].isna()]
                disc = work[work['Received Qty'].notna() & (work['Variance'] != 0)]

                if len(pending):
                    st.info(f"⏳ {len(pending)} line(s) awaiting received count")
                if not disc.empty:
                    st.error(f"⚠️ {len(disc)} discrepancy line(s):")
                    st.dataframe(
                        disc[['Inventory Item', 'Description', 'Expected Qty', 'Received Qty', 'Variance']],
                        use_container_width=True
                    )

                    # Draft discrepancy email - requires explicit approval to send
                    st.subheader("✉️ Draft Discrepancy Email")
                    disc_to = st.text_input(
                        "Send to",
                        value=os.getenv('DISCREPANCY_EMAIL_TO', ''),
                        key="disc_to"
                    )
                    hdr = work.iloc[0]
                    lines_txt = "\n".join(
                        f"- {r['Inventory Item']} {r['Description']}: expected "
                        f"{r['Expected Qty']}, received {r['Received Qty']} "
                        f"(variance {r['Variance']:+g})"
                        for _, r in disc.iterrows()
                    )
                    default_body = (
                        "A packing slip was received with quantity discrepancies.\n\n"
                        f"PO Number: {hdr.get('PO Number', '')}\n"
                        f"Vendor: {hdr.get('Vendor', '')}\n"
                        f"Date: {hdr.get('Date', '')}\n\n"
                        "Discrepancies:\n"
                        f"{lines_txt}\n\n"
                        "Please advise on how to proceed.\n\n"
                        "Ratech Manufacturing"
                    )
                    body = st.text_area("Email body", value=default_body, height=230,
                                        key="disc_email_body")
                    if st.button("Approve & Send Discrepancy Email", type="primary",
                                 key="send_disc"):
                        smtp_host = os.getenv('EMAIL_SMTP_HOST', 'smtp.mail.yahoo.com')
                        smtp_port = os.getenv('EMAIL_SMTP_PORT', '465')
                        email_user = os.getenv('EMAIL_USER')
                        email_pass = os.getenv('EMAIL_PASSWORD')
                        if not email_user or not email_pass:
                            st.warning("Email credentials not configured.")
                        elif not disc_to.strip():
                            st.warning("Enter a recipient address.")
                        else:
                            with st.spinner("Sending discrepancy email..."):
                                try:
                                    send_email(smtp_host, smtp_port, email_user, email_pass,
                                               disc_to.strip(),
                                               f"Inventory discrepancy - PO {hdr.get('PO Number', '')}",
                                               body)
                                    st.success(f"✅ Discrepancy email sent to {disc_to.strip()}")
                                except Exception as e:
                                    st.error(f"Failed to send: {e}")
                elif not len(pending):
                    st.success("✅ All counts match expected quantities")

            st.divider()
            ex1, ex2 = st.columns(2)
            with ex1:
                out = work if not edited.empty else edited
                st.download_button(
                    label="📥 Download Inventory CSV",
                    data=out.to_csv(index=False),
                    file_name="inventory_update.csv",
                    mime="text/csv",
                    type="primary",
                    use_container_width=True
                )
            with ex2:
                if st.button("🧹 Clear Receipt", use_container_width=True):
                    st.session_state.receipt = None
                    st.session_state.receipt_id = None
                    st.rerun()

elif page == "Settings":
    st.header("⚙️ Settings")
    st.caption("⚠️ Saving here writes to the app's temporary `.env` — values are "
               "**lost on reboot/redeploy**. For anything permanent, add it to "
               "**Streamlit Cloud → Secrets** instead. Each section shows where its "
               "credentials are actually coming from.")

    with st.expander("🔍 Secrets diagnostic"):
        try:
            _keys = list(st.secrets.keys())
            st.write(f"st.secrets has {len(_keys)} top-level key(s): `{_keys}`")
        except Exception as e:
            st.error(f"st.secrets failed to load: {type(e).__name__}: {e}")
            st.caption("A TOML parse error in Secrets makes ALL secrets "
                       "invisible to the app - check for unclosed quotes or "
                       "a stray section header like [secrets]")

    st.subheader("Google Cloud Vision API")
    
    # Show current status
    current_creds = os.getenv('GOOGLE_CREDENTIALS')
    cred_status_caption('GOOGLE_CREDENTIALS', 'Google Cloud credentials')
    
    google_credentials = st.text_area(
        "Google Cloud Credentials JSON",
        value=current_creds if current_creds else "",
        height=200,
        help="Paste your Google Cloud service account JSON credentials here"
    )
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("Save Credentials"):
            if google_credentials:
                # Validate JSON format before saving
                try:
                    import json
                    json.loads(google_credentials.strip())
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON format: {e}")
                    st.info("Please ensure you pasted the entire JSON file contents without extra characters.")
                else:
                    # Update .env file
                    env_file = ".env"
                    if os.path.exists(env_file):
                        # Read existing env
                        with open(env_file, 'r') as f:
                            lines = f.readlines()
                        
                        # Update or add GOOGLE_CREDENTIALS
                        updated = False
                        new_lines = []
                        for line in lines:
                            if line.startswith('GOOGLE_CREDENTIALS='):
                                new_lines.append(f'GOOGLE_CREDENTIALS={google_credentials}\n')
                                updated = True
                            else:
                                new_lines.append(line)
                        
                        if not updated:
                            new_lines.append(f'\nGOOGLE_CREDENTIALS={google_credentials}\n')
                        
                        with open(env_file, 'w') as f:
                            f.writelines(new_lines)
                    else:
                        # Create new .env file
                        with open(env_file, 'w') as f:
                            f.write(f'GOOGLE_CREDENTIALS={google_credentials}\n')
                    
                    # Reload environment
                    load_dotenv(override=True)
                    st.success("Credentials saved to .env file")
                    st.rerun()
            else:
                st.warning("Please enter credentials")
    
    with col2:
        if st.button("Clear Credentials"):
            if os.path.exists(".env"):
                with open(".env", 'r') as f:
                    lines = f.readlines()
                
                new_lines = [line for line in lines if not line.startswith('GOOGLE_CREDENTIALS=')]
                
                with open(".env", 'w') as f:
                    f.writelines(new_lines)
                
                load_dotenv(override=True)
                st.success("Credentials cleared")
                st.rerun()
    
    st.divider()
    
    st.subheader("Gemini AI (Optional)")
    st.info("Add a Gemini API key to enable AI-powered parsing for better accuracy with complex formats.")
    
    # Show current status
    current_gemini = os.getenv('GEMINI_API_KEY')
    cred_status_caption('GEMINI_API_KEY', 'Gemini API key')
    
    gemini_key = st.text_input(
        "Gemini API Key",
        value=current_gemini if current_gemini else "",
        type="password",
        help="Enter your Gemini API key from Google AI Studio"
    )
    
    if st.button("Save Gemini Key"):
        if gemini_key:
            # Update .env file
            env_file = ".env"
            if os.path.exists(env_file):
                # Read existing env
                with open(env_file, 'r') as f:
                    lines = f.readlines()
                
                # Update or add GEMINI_API_KEY
                updated = False
                new_lines = []
                for line in lines:
                    if line.startswith('GEMINI_API_KEY='):
                        new_lines.append(f'GEMINI_API_KEY={gemini_key}\n')
                        updated = True
                    else:
                        new_lines.append(line)
                
                if not updated:
                    new_lines.append(f'\nGEMINI_API_KEY={gemini_key}\n')
                
                with open(env_file, 'w') as f:
                    f.writelines(new_lines)
            else:
                # Create new .env file
                with open(env_file, 'w') as f:
                    f.write(f'GEMINI_API_KEY={gemini_key}\n')
            
            # Reload environment
            load_dotenv(override=True)
            st.success("Gemini API key saved")
            st.rerun()
        else:
            st.warning("Please enter a Gemini API key")
    
    st.divider()
    
    st.subheader("Sage 50 Defaults")
    st.caption("Required by the Sales Order Journal (SO.CSV) import template. "
               "Get these from your Sage chart of accounts and sales tax codes.")

    cur_ar = os.getenv('SAGE_AR_ACCOUNT', '12000')
    cur_gl = os.getenv('SAGE_GL_ACCOUNT', '30500')
    cur_tax = os.getenv('SAGE_TAX_ID', '')
    cur_ttype = os.getenv('SAGE_TAX_TYPE', '1')
    cur_terms = os.getenv('SAGE_TERMS', 'Net Due')
    if cur_ar and cur_gl:
        st.success("✅ Sage defaults configured")
    else:
        st.warning("⚠️ Sage defaults incomplete - the SO.CSV import needs all three")

    sage_ar = st.text_input("Accounts Receivable Account", value=cur_ar,
                            help="Your A/R G/L account number (your export showed 12000)")
    sage_gl = st.text_input("Sales G/L Account", value=cur_gl,
                            help="Income account for line items (your export showed 30500)")
    sage_tax = st.text_input("Sales Tax ID", value=cur_tax,
                             help="Sales tax code from Sage - your export left this blank")
    sage_ttype = st.text_input("Line Tax Type", value=cur_ttype,
                               help="0-26; your export used 1 and 2 per line")
    sage_terms = st.text_input("Displayed Terms", value=cur_terms,
                               help="Your export showed 'Net Due'")
    sage_mkt = st.text_input("Marketplace Customer", value=os.getenv('SAGE_MARKETPLACE_CUSTOMER', '100B'),
                             help="Sage customer ID for all ShipStation/marketplace orders")
    if st.button("Save Sage Defaults"):
        save_env_vars({
            'SAGE_AR_ACCOUNT': sage_ar,
            'SAGE_GL_ACCOUNT': sage_gl,
            'SAGE_TAX_ID': sage_tax,
            'SAGE_TAX_TYPE': sage_ttype or '1',
            'SAGE_TERMS': sage_terms or 'Net Due',
            'SAGE_MARKETPLACE_CUSTOMER': sage_mkt or '100B',
        })
        st.success("Sage defaults saved")
        st.rerun()
    
    st.divider()
    
    st.subheader("Email Inbox (IMAP)")
    st.info("Reading is non-destructive: the app only marks an email read after it has been "
            "loaded into the queue or explicitly ignored. Nothing is moved or deleted. "
            "For Yahoo, use an **app password** (Yahoo Account Security → Generate app password) — "
            "your regular password will be rejected.")

    cur_host = os.getenv('EMAIL_HOST', 'imap.mail.yahoo.com')
    cur_port = os.getenv('EMAIL_PORT', '993')
    cur_user = os.getenv('EMAIL_USER', '')
    cur_pass = os.getenv('EMAIL_PASSWORD', '')

    cred_status_caption('EMAIL_USER', 'Email user')
    cred_status_caption('EMAIL_PASSWORD', 'Email app password')
    if cur_user:
        st.caption(f"Configured for {cur_user}")

    email_host = st.text_input("IMAP Host", value=cur_host)
    email_port = st.text_input("IMAP Port", value=cur_port)
    email_user = st.text_input("Email Address", value=cur_user)
    email_pass = st.text_input("Email App Password", value=cur_pass, type='password',
                               help="Yahoo: generate at Account Security → Generate app password")

    if st.button("Save Email Settings"):
        save_env_vars({
            'EMAIL_HOST': email_host,
            'EMAIL_PORT': email_port,
            'EMAIL_USER': email_user,
            'EMAIL_PASSWORD': email_pass,
        })
        st.success("Email settings saved")
        st.rerun()

    st.divider()

    st.subheader("ShipStation API")
    st.info("Generate an **API key + secret** in ShipStation: "
            "Settings → Account → API Settings. This is separate from your login "
            "email/password, and requires a Standard plan or higher.")

    cur_ss_key = os.getenv('SHIPSTATION_API_KEY', '')
    cur_ss_secret = os.getenv('SHIPSTATION_API_SECRET', '')
    cred_status_caption('SHIPSTATION_API_KEY', 'ShipStation API key')
    cred_status_caption('SHIPSTATION_API_SECRET', 'ShipStation API secret')

    ss_key_in = st.text_input("ShipStation API Key", value=cur_ss_key)
    ss_secret_in = st.text_input("ShipStation API Secret", value=cur_ss_secret,
                                 type='password')
    if st.button("Save ShipStation Keys"):
        if ss_key_in and ss_secret_in:
            save_env_vars({
                'SHIPSTATION_API_KEY': ss_key_in,
                'SHIPSTATION_API_SECRET': ss_secret_in,
            })
            st.success("ShipStation keys saved")
            st.rerun()
        else:
            st.warning("Enter both the API key and secret")

st.sidebar.markdown("---")
st.sidebar.markdown("### Quick Links")
st.sidebar.markdown("[Google Cloud Console](https://console.cloud.google.com)")
st.sidebar.markdown("[Streamlit Docs](https://docs.streamlit.io)")
