import streamlit as st
import os
import re
from dotenv import load_dotenv
import pandas as pd
from io import StringIO
from vision_processor import VisionProcessor, parse_order_text
from email_inbox import list_unseen_orders, fetch_pdf_from_message

load_dotenv()

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
for _k, _v in (('invoices', []), ('inbox_items', []), ('loaded_uids', set()), ('upload_version', 0)):
    if _k not in st.session_state:
        st.session_state[_k] = _v


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
    """Load the customer-name -> Sage customer number mapping table."""
    if os.path.exists(CUSTOMERS_CSV):
        try:
            df = pd.read_csv(CUSTOMERS_CSV, dtype=str).fillna('')
            for c in CUSTOMER_COLS:
                if c not in df.columns:
                    df[c] = ''
            return df[CUSTOMER_COLS]
        except Exception:
            pass
    return pd.DataFrame(columns=CUSTOMER_COLS)


def guess_customer(text):
    """Return the Sage customer number whose alias appears in the given
    text (extracted document text, filename, channel name, etc.)."""
    tl = (text or '').lower()
    for _, row in load_customers().iterrows():
        for alias in str(row.get('Aliases', '')).split(';'):
            alias = alias.strip().lower()
            if alias and alias in tl:
                return str(row.get('Customer Number', '')).strip()
    return ''


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


def build_invoice(pdf_bytes, pdf_name, doc_key):
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
    gc = guess_customer(f"{pdf_name}\n{extracted_text}")
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
            'Inventory Item': str(r.get('Item SKU', '')).strip(),
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

# Sidebar navigation
st.sidebar.markdown("""
<div style="padding: 0.2rem 0.2rem 0.4rem;">
    <div style="font-size:1.5rem; font-weight:700; color:#2460A7;">📦 Ratech</div>
    <div style="font-size:0.8rem; color:#8a93a0;">Order Management</div>
</div>
""", unsafe_allow_html=True)
page = st.sidebar.radio("Select Page", ["Order Processing", "Customers", "Inventory Management", "Settings"])

st.sidebar.divider()
st.sidebar.caption("Connections")
for _label, _ok in [
    ("Vision OCR", os.getenv('GOOGLE_CREDENTIALS')),
    ("Gemini parser", os.getenv('GEMINI_API_KEY')),
    ("Email inbox", os.getenv('EMAIL_USER') and os.getenv('EMAIL_PASSWORD')),
]:
    st.sidebar.markdown(f"{'🟢' if _ok else '⚪'} {_label}")
st.sidebar.caption(f"🗂 {len(st.session_state.invoices)} order(s) in queue")

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

    # --- Inbox orders (read-only IMAP - never marks mail as read) ---
    st.subheader("📥 Inbox Orders")
    email_host = os.getenv('EMAIL_HOST', 'imap.mail.yahoo.com')
    email_port = os.getenv('EMAIL_PORT', '993')
    email_user = os.getenv('EMAIL_USER')
    email_pass = os.getenv('EMAIL_PASSWORD')

    if st.button("🔍 Check Inbox for New Orders"):
        if not email_user or not email_pass:
            st.warning("Email credentials not configured. Add them in the Settings page.")
        else:
            with st.spinner("Checking inbox for unread emails..."):
                try:
                    items = list_unseen_orders(email_host, email_port, email_user, email_pass)
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
            item_cols = st.columns([5, 1])
            with item_cols[0]:
                st.write(f"**{item['subject']}**")
                st.caption(f"{item['sender']} • {item['date']}")
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
                            st.warning("No PDF attachment found in this email.")
                        else:
                            existing = {inv['key'] for inv in st.session_state.invoices}
                            loaded = 0
                            for pdf in pdfs:
                                inv_key = ('inbox', item['uid'], pdf['filename'])
                                if inv_key not in existing:
                                    st.session_state.invoices.append(
                                        build_invoice(pdf['bytes'], pdf['filename'], inv_key))
                                    existing.add(inv_key)
                                    loaded += 1
                            st.session_state.loaded_uids.add(item['uid'])
                            st.session_state.inbox_items = [i for i in st.session_state.inbox_items if i['uid'] != item['uid']]
                            if loaded:
                                st.rerun()

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
                        with st.expander("Raw Extracted Text"):
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
                        key=f"editor_{wkey}"
                    )
                    inv['df'] = edited_df

                    qc1, qc2 = st.columns(2)
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
            st.markdown("")

        # --- Combined validation & export ---
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
                st.caption(f"{len(combined)} line item(s) from {len(all_dfs)} order(s) will be exported to one file")
                with st.expander("Preview combined CSV data", expanded=True):
                    st.dataframe(combined, use_container_width=True)
                ec1, ec2 = st.columns(2)
                with ec1:
                    st.download_button(
                        label="📥 Download CSV for Sage 50",
                        data=combined.to_csv(index=False),
                        file_name="sage50_sales_orders.csv",
                        mime="text/csv",
                        type="primary",
                        use_container_width=True
                    )
                with ec2:
                    if st.button("🧹 Clear All Orders", use_container_width=True):
                        st.session_state.invoices = []
                        st.session_state.upload_version += 1
                        st.rerun()

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

    cust_df = load_customers()
    edited_cust = st.data_editor(
        cust_df,
        num_rows="dynamic",
        use_container_width=True,
        key="customers_editor"
    )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save Customers", type="primary"):
            try:
                edited_cust.to_csv(CUSTOMERS_CSV, index=False)
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

    st.caption("Saved to `customers.csv` in the app folder. On Streamlit Cloud this file is "
               "restored from the repository on each redeploy — to make changes permanent, "
               "commit the updated `customers.csv` to GitHub (or download it here and commit it).")

elif page == "Inventory Management":
    st.header("📋 Inventory Management")
    st.info("This feature will be implemented in Phase 2. It will allow workers to:")
    st.markdown("""
    - Take photos of packing slips
    - Read inventory counts from images
    - Compare received vs expected quantities
    - Generate alerts for discrepancies
    - Create approval emails for exceptions
    """)

elif page == "Settings":
    st.header("⚙️ Settings")
    
    st.subheader("Google Cloud Vision API")
    
    # Show current status
    current_creds = os.getenv('GOOGLE_CREDENTIALS')
    if current_creds:
        st.success("✅ Google Cloud credentials are configured")
    else:
        st.warning("⚠️ Google Cloud credentials not configured")
    
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
    if current_gemini:
        st.success("✅ Gemini API key is configured")
    else:
        st.info("ℹ️ Gemini API key not configured (optional - will use regex parsing)")
    
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
    
    st.subheader("Sage 50 CSV Format")
    st.info("Please provide a sample Sage 50 2019 CSV import format. This will be used to structure the export.")
    st.markdown("""
    **Expected CSV structure (placeholder):**
    - Item Name
    - Quantity
    - Price
    - Total
    - Date
    - Order Number
    
    Once you provide the actual format, the export logic will be updated accordingly.
    """)
    
    st.divider()
    
    st.subheader("Email Inbox (IMAP)")
    st.info("Read-only inbox check: the app never marks emails as read, moves, or deletes anything. "
            "For Yahoo, use an **app password** (Yahoo Account Security → Generate app password) — "
            "your regular password will be rejected.")

    cur_host = os.getenv('EMAIL_HOST', 'imap.mail.yahoo.com')
    cur_port = os.getenv('EMAIL_PORT', '993')
    cur_user = os.getenv('EMAIL_USER', '')
    cur_pass = os.getenv('EMAIL_PASSWORD', '')

    if cur_user and cur_pass:
        st.success(f"✅ Email configured for {cur_user}")
    else:
        st.warning("⚠️ Email credentials not configured")

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

st.sidebar.markdown("---")
st.sidebar.markdown("### Quick Links")
st.sidebar.markdown("[Google Cloud Console](https://console.cloud.google.com)")
st.sidebar.markdown("[Streamlit Docs](https://docs.streamlit.io)")
