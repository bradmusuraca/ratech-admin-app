import os
import json
import html
from typing import List, Dict, Optional
from google.cloud import vision
from PIL import Image
import io


class VisionProcessor:
    """Handles Google Cloud Vision API operations for text extraction"""
    
    def __init__(self, credentials_json: Optional[str] = None):
        """
        Initialize the Vision Processor
        
        Args:
            credentials_json: Optional JSON string of Google Cloud credentials.
                            If not provided, will use GOOGLE_APPLICATION_CREDENTIALS env var.
        """
        self.client = None
        
        if credentials_json:
            # Parse and use credentials from string
            try:
                # Clean up the credentials string - remove extra whitespace
                credentials_json = credentials_json.strip()
                credentials_dict = json.loads(credentials_json)
                from google.oauth2 import service_account
                credentials = service_account.Credentials.from_service_account_info(credentials_dict)
                
                # Debug: Show which project is being used
                print(f"DEBUG: Using credentials for project: {credentials_dict.get('project_id')}")
                print(f"DEBUG: Service account: {credentials_dict.get('client_email')}")
                
                self.client = vision.ImageAnnotatorClient(credentials=credentials)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON format in credentials: {e}")
                raise ValueError(
                    f"The Google Cloud credentials are not valid JSON. "
                    f"Error: {e}. Please ensure you pasted the entire JSON file contents."
                )
            except Exception as e:
                print(f"Error loading credentials from string: {e}")
                raise
        else:
            # Use default credentials from environment
            try:
                self.client = vision.ImageAnnotatorClient()
            except Exception as e:
                print(f"Error initializing Vision client: {e}")
                raise
    
    def extract_text_from_image(self, image_path: str) -> str:
        """
        Extract text from an image file using Vision API
        
        Args:
            image_path: Path to the image file
            
        Returns:
            Extracted text as a string
        """
        try:
            with io.open(image_path, 'rb') as image_file:
                content = image_file.read()
            
            image = vision.Image(content=content)
            response = self.client.document_text_detection(image=image)
            
            if response.error.message:
                raise Exception(f"Vision API Error: {response.error.message}")
            
            return response.full_text_annotation.text
            
        except Exception as e:
            print(f"Error extracting text from image: {e}")
            raise
    
    def extract_text_from_pdf(self, pdf_path: str) -> str:
        """
        Extract text from a PDF file
        First tries to extract text directly, falls back to OCR if needed
        
        Args:
            pdf_path: Path to the PDF file
            
        Returns:
            Extracted text as a string
        """
        try:
            from pypdf import PdfReader
            
            # Try direct text extraction first
            reader = PdfReader(pdf_path)
            text = ""
            
            for page in reader.pages:
                text += page.extract_text() + "\n"
            
            # If text extraction yielded very little, might need OCR
            if len(text.strip()) < 50:
                print("Direct PDF text extraction yielded minimal text, attempting OCR...")
                text = self._ocr_pdf(pdf_path)
            
            return text
            
        except ImportError:
            print("pypdf not installed, using OCR only")
            return self._ocr_pdf(pdf_path)
        except Exception as e:
            print(f"Error in direct PDF extraction: {e}, falling back to OCR")
            return self._ocr_pdf(pdf_path)
    
    def _ocr_pdf(self, pdf_path: str) -> str:
        """
        OCR a PDF by converting pages to images using PyMuPDF
        
        Args:
            pdf_path: Path to the PDF file
            
        Returns:
            Extracted text as a string
        """
        try:
            import fitz  # PyMuPDF
            
            # Open the PDF
            pdf_document = fitz.open(pdf_path)
            
            full_text = ""
            for page_num in range(len(pdf_document)):
                # Get the page
                page = pdf_document[page_num]
                
                # Convert to image
                pix = page.get_pixmap()
                img_bytes = pix.tobytes("png")
                
                # Use Vision API on the image
                vision_image = vision.Image(content=img_bytes)
                response = self.client.document_text_detection(image=vision_image)
                
                if response.error.message:
                    print(f"Error on page {page_num+1}: {response.error.message}")
                    continue
                
                full_text += response.full_text_annotation.text + "\n"
            
            pdf_document.close()
            return full_text
            
        except ImportError:
            print("PyMuPDF (fitz) not available. For scanned PDFs, please convert to images first.")
            raise NotImplementedError(
                "OCR for scanned PDFs requires PyMuPDF. "
                "Please convert your PDF to images or use a PDF with embedded text."
            )
        except Exception as e:
            print(f"Error in OCR PDF: {e}")
            raise
        except Exception as e:
            print(f"Error OCRing PDF: {e}")
            raise
    
    def extract_text_from_bytes(self, file_bytes: bytes, file_type: str = 'pdf') -> str:
        """
        Extract text from file bytes (useful for Streamlit uploads)
        
        Args:
            file_bytes: File content as bytes
            file_type: Type of file ('pdf' or 'image')
            
        Returns:
            Extracted text as a string
        """
        try:
            if file_type == 'image':
                image = vision.Image(content=file_bytes)
                response = self.client.document_text_detection(image=image)
                
                if response.error.message:
                    raise Exception(f"Vision API Error: {response.error.message}")
                
                return response.full_text_annotation.text
            
            elif file_type == 'pdf':
                # Save bytes to temp file and process
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                    tmp.write(file_bytes)
                    tmp_path = tmp.name
                
                try:
                    text = self.extract_text_from_pdf(tmp_path)
                    return text
                finally:
                    os.unlink(tmp_path)
            
            else:
                raise ValueError(f"Unsupported file type: {file_type}")
                
        except Exception as e:
            print(f"Error extracting text from bytes: {e}")
            raise


def parse_order_text(extracted_text: str, gemini_api_key: Optional[str] = None) -> List[Dict]:
    """
    Parse extracted text into structured order data
    Uses Gemini AI for parsing, falls back to simple regex if unavailable
    
    Args:
        extracted_text: Raw text extracted from PDF
        gemini_api_key: Optional Gemini API key for LLM-based parsing
        
    Returns:
        List of dictionaries representing order items
    """
    import re
    items = []
    # Normalize HTML entities that can leak through email body conversion
    extracted_text = html.unescape(extracted_text)
    lines = extracted_text.split('\n')
    full_text = ' '.join(lines)

    # Extract order header information
    order_info = {
        'Purchase Order Number': '',
        'Date of Order': '',
        'Ship By Date': '',
        'Ship To': '',
        'Invoice Address': '',
        'Vendor': '',
        'Shipping Instructions': '',
        'Comments': ''
    }

    # Helper function to safely convert to float
    def safe_float(value, default=0.0):
        try:
            if isinstance(value, str):
                # Remove non-numeric characters except . and -
                cleaned = re.sub(r'[^\d.\-]', '', value)
                return float(cleaned) if cleaned else default
            return float(value)
        except (ValueError, TypeError):
            return default

    # Helper function to safely convert to int
    def safe_int(value, default=0):
        try:
            if isinstance(value, str):
                # Remove non-numeric characters except . and -
                cleaned = re.sub(r'[^\d]', '', value)
                return int(float(cleaned)) if cleaned else default
            return int(float(value))
        except (ValueError, TypeError):
            return default

    # Check if it's S&S Truck Parts format
    is_ss_truck = 'S&S Truck Parts' in full_text or 'sandstruck.com' in full_text or 'SSPO' in full_text

    # Check if it's Summit Racing format
    is_summit = 'Summit Racing' in full_text or 'summitracing.com' in full_text.lower() or 'SUMMIT RACING' in full_text

    # Check if it's Drive Train Specialists format
    is_dts = 'DRIVE TRAIN SPECIALISTS' in full_text.upper()

    # Check if it's Ford Motor Company format ("Purchase Notification")
    is_ford = ('ford motor company' in full_text.lower()
               or 'purchase notification' in full_text.lower())

    # Check if it's a simple emailed order: "CUSTOMER ID: 208" header +
    # lines like "26) 9132" (qty) part
    is_simple_email = (bool(re.search(r'CUSTOMER\s*ID', extracted_text, re.IGNORECASE))
                       and bool(re.search(r'^\s*\d+\s*\)\s*[A-Za-z0-9]', extracted_text, re.MULTILINE)))

    def summit_dates(text):
        """Summit's 'Order Details' block lists labels then values, and
        text extraction can interleave them two ways:
            Order Date:\\n10/6/2026          Order Date:\\nArrive On/Before:
            Arrive On/Before:\\n10/22/2026   10/6/2026\\n10/22/2026
        In both, the first date in the block is the order date and the
        second is the arrive-on/before (ship-by) date."""
        m = re.search(r'Order Details(.*?)Shipping Information', text, re.DOTALL)
        if not m:
            return (None, None)
        ds = re.findall(r'\d{1,2}/\d{1,2}/\d{2,4}', m.group(1))
        return (ds[0] if ds else None, ds[1] if len(ds) > 1 else None)

    # Simple emailed orders (e.g. "26) 9132") - trivially parseable,
    # so skip Gemini entirely and parse directly
    if is_simple_email:
        cust_match = re.search(r'CUSTOMER\s*ID[:\s]*([A-Za-z0-9\-]+)', extracted_text, re.IGNORECASE)
        if cust_match:
            order_info['Vendor'] = cust_match.group(1)
        name_match = re.search(r'ORDER\s+FOR\s+(.+?)\s+CUSTOMER\s*ID', extracted_text, re.IGNORECASE)
        if name_match:
            order_info['Ship To'] = ' '.join(name_match.group(1).split())
        order_info['Comments'] = 'Emailed order (plain text)'
        for m in re.finditer(r'^\s*(\d+)\s*\)\s*([A-Za-z0-9][A-Za-z0-9\-_.]*)',
                             extracted_text, re.MULTILINE):
            item = {
                'Inventory Item': m.group(2),
                'Description': '',
                'Quantity': int(m.group(1)),
                'Unit Price': '',
                'Total': ''
            }
            item.update(order_info)
            items.append(item)
        return items

    known_format = is_summit or is_dts or is_ss_truck or is_ford

    # Known layouts always use the hand-tuned deterministic parsers below -
    # a model will happily copy the wrong column (e.g. DTS's "CR 13988"
    # internal number instead of "13988") and pass every sanity check.
    # Gemini is primary only for unknown formats, and the last resort for
    # known formats whose regex finds nothing.
    if gemini_api_key and not known_format:
        print("DEBUG: Using Gemini API for parsing")
        gemini_items = parse_order_with_gemini(extracted_text, gemini_api_key)
        if gemini_items:
            print(f"DEBUG: Gemini returned {len(gemini_items)} items")
            return gemini_items
        print("DEBUG: Gemini returned no items, falling back to regex")

    # Fallback to regex-based parsing
    print("DEBUG: Using regex fallback")
    
    if is_summit:
        # Summit Racing format parsing
        # Look for Order Number
        po_match = re.search(r'Order Number\s*(\d+)', extracted_text)
        if po_match:
            order_info['Purchase Order Number'] = po_match.group(1)
        
        # Order Details block: first date = order date, second = ship-by.
        # (Text extraction stacks labels and values in either order.)
        od, sb = summit_dates(extracted_text)
        if od:
            order_info['Date of Order'] = od
        if sb:
            order_info['Ship By Date'] = sb
        # Label-anchored fallbacks if the Order Details block wasn't found
        if not order_info['Date of Order']:
            date_match = re.search(r'Order Date\D*?(\d{1,2}/\d{1,2}/\d{2,4})', extracted_text)
            if date_match:
                order_info['Date of Order'] = date_match.group(1)
        if not order_info['Ship By Date']:
            delivery_match = re.search(r'Arrive\s*On[/\s]*Before\D*?(\d{1,2}/\d{1,2}/\d{2,4})',
                                       extracted_text, re.IGNORECASE)
            if delivery_match:
                order_info['Ship By Date'] = delivery_match.group(1)
        
        # Shipping info comes in two layouts: labels with inline values
        # (PDF), or all four labels stacked then the value blocks in
        # order From / Bill To / Ship To / Purchase Order To (HTML email
        # flattened to text), each block ending with PH:/FX: lines.
        stacked = re.search(
            r'From:\s*\n\s*Bill To:\s*\n\s*Ship To:\s*\n\s*Purchase Order To:',
            extracted_text)
        if stacked:
            tail = extracted_text[stacked.end():]
            segs = re.split(r'(?im)^\s*(?:PH|FX)\s*:[^\n]*\n?', tail)
            blocks = [s.strip() for s in segs if s.strip()]
            if len(blocks) >= 2:
                order_info['Invoice Address'] = blocks[1]
            if len(blocks) >= 3:
                ship_to = blocks[2]
                if 'SUMMIT' not in ship_to.upper():
                    ship_to = 'SUMMIT RACING\n' + ship_to
                order_info['Ship To'] = ship_to
            if len(blocks) >= 4:
                vm = re.match(r'\((.*?)\)\s*([^\n]+)', blocks[3])
                order_info['Vendor'] = (f"{vm.group(1)} {vm.group(2).strip()}"
                                        if vm else blocks[3].split('\n')[0].strip())
        else:
            # Look for Ship To address
            ship_to_match = re.search(r'Ship To:\s*\n\s*(.*?)\s*\n\s*(?:PH|FX)\s*:', extracted_text, re.DOTALL)
            if ship_to_match:
                ship_to = ship_to_match.group(1).strip()
                # Drop any FX:/PH: lines that were captured, and avoid
                # duplicating the SUMMIT RACING header line
                ship_to = '\n'.join(l for l in ship_to.split('\n')
                                    if not re.match(r'\s*(?:PH|FX)\s*[:]', l))
                if 'SUMMIT' not in ship_to.upper():
                    ship_to = 'SUMMIT RACING\n' + ship_to
                order_info['Ship To'] = ship_to

            # Look for Bill To address
            bill_to_match = re.search(r'Bill To:\s*\n\s*(.*?)\s*\n\s*PH:', extracted_text, re.DOTALL)
            if bill_to_match:
                order_info['Invoice Address'] = bill_to_match.group(1).strip()

            # Look for Vendor (Purchase Order To)
            vendor_match = re.search(r'Purchase Order To:\s*\n\s*\((.*?)\)\s*([^\n]+)', extracted_text)
            if vendor_match:
                order_info['Vendor'] = f"{vendor_match.group(1)} {vendor_match.group(2).strip()}"
        
        # Look for comments
        comments_match = re.search(r'Comments\s*\n\s*(.*?)\s*\n\s*Order Items', extracted_text, re.DOTALL)
        if comments_match:
            order_info['Comments'] = comments_match.group(1).strip()
        
        # Find items - Summit Racing email table spans multiple lines per row.
        # Layout: "0001" / "RAT-xxxx" / qty / description... / $cost / 0% / $ext
        # Some descriptions are split: first line appears BEFORE the item number
        # and the last line AFTER the extended price.
        # Drop blank lines: the HTML-table email layout flattens with empty
        # lines between every field, which would break the anchor detection.
        tbl_lines = [l.strip() for l in extracted_text.split('\n') if l.strip()]

        # Start scanning after the "Ext Price" column header
        start_idx = 0
        for idx, l in enumerate(tbl_lines):
            if 'Ext Price' in l:
                start_idx = idx + 1

        def is_junk_line(l):
            low = l.lower()
            return (not l
                    or 'http' in low
                    or 'outlook' in low
                    or 'inbox' in low
                    or 'this email was sent' in low
                    or 'copyright' in low
                    or bool(re.match(r'^\d{1,2}/\d{1,2}/\d{2,4},', l)))

        def stray_lines(a, b):
            zone = []
            for l in tbl_lines[a:b]:
                if (is_junk_line(l) or '$' in l or re.match(r'^\d+%$', l)
                        or re.match(r'^\d{4}$', l) or re.match(r'^\d+$', l)
                        or re.match(r'^RAT-', l)):
                    continue
                zone.append(l)
            return zone

        # Locate item anchors: "0001" / "RAT-xxxx" / qty on consecutive lines
        anchors = [i for i in range(start_idx, len(tbl_lines) - 2)
                   if re.match(r'^\d{4}$', tbl_lines[i])
                   and re.match(r'^RAT-[A-Za-z0-9]+', tbl_lines[i + 1])
                   and re.match(r'^\d+$', tbl_lines[i + 2])]

        money_re = re.compile(r'\$([\d,]+\.?\d*)')
        blocks = []
        for i in anchors:
            part_num = re.match(r'^(RAT-[A-Za-z0-9]+)', tbl_lines[i + 1]).group(1)
            qty = safe_int(tbl_lines[i + 2])
            middle = []
            unit_price = 0.0
            ext_price = 0.0
            ext_idx = None
            j = i + 3
            while j < len(tbl_lines):
                m = money_re.search(tbl_lines[j])
                if m:
                    unit_price = safe_float(m.group(1).replace(',', ''))
                    break
                if re.match(r'^\d{4}$', tbl_lines[j]):
                    break
                if not is_junk_line(tbl_lines[j]):
                    middle.append(tbl_lines[j])
                j += 1
            for k in range(j + 1, min(j + 4, len(tbl_lines))):
                m = money_re.search(tbl_lines[k])
                if m:
                    ext_price = safe_float(m.group(1).replace(',', ''))
                    ext_idx = k
                    break
            blocks.append({'anchor': i, 'part': part_num, 'qty': qty,
                           'middle': middle, 'unit_price': unit_price,
                           'ext_price': ext_price,
                           'ext_idx': ext_idx if ext_idx is not None else j})

        # Distribute stray description lines: when an item has no middle
        # description, its first part sits before the marker and its last
        # line sits right after the extended price
        prev_end = start_idx
        for n, blk in enumerate(blocks):
            zone = stray_lines(prev_end, blk['anchor'])
            if n > 0 and not blocks[n - 1]['middle'] and zone:
                blocks[n - 1]['suffix'] = [zone[0]]
                zone = zone[1:]
            blk['prefix'] = zone
            prev_end = blk['ext_idx'] + 1
        if blocks:
            last_zone = stray_lines(blocks[-1]['ext_idx'] + 1, len(tbl_lines))
            blocks[-1]['suffix'] = ([last_zone[0]]
                                    if not blocks[-1]['middle'] and last_zone
                                    else [])

        for blk in blocks:
            desc = ' '.join(blk.get('prefix', []) + blk['middle'] + blk.get('suffix', []))
            desc = html.unescape(html.unescape(desc)).strip()
            item = {
                'Inventory Item': re.sub(r'^RAT-', '', blk['part']),
                'Description': desc,
                'Quantity': blk['qty'],
                'Unit Price': blk['unit_price'],
                'Total': blk['ext_price']
            }
            item.update(order_info)
            items.append(item)
    
    elif is_dts:
        # Drive Train Specialists format
        # Number : 87882 Date: 10/07/2026
        po_match = re.search(r'Number\s*:\s*(\d+)', extracted_text)
        if po_match:
            order_info['Purchase Order Number'] = po_match.group(1)

        date_match = re.search(r'Number\s*:\s*\d+\s*Date:\s*(\d{1,2}/\d{1,2}/\d{4})', extracted_text)
        if date_match:
            order_info['Date of Order'] = date_match.group(1)

        # Need By date is the 2nd |-separated field on the "PO #:" line
        need_match = re.search(r'PO #:\s*\d+\s*\|\s*(\d{1,2}/\d{1,2}/\d{4})', extracted_text)
        if need_match:
            order_info['Ship By Date'] = need_match.group(1)

        # Vendor SO # column holds DTS-internal codes like 'STOCK', never a
        # usable customer number - leave Vendor blank so resolve_vendor()
        # fills it from the customer list via the 'DRIVE TRAIN SPECIALISTS'
        # name in the header.

        # Shipping instruction is the last |-separated field on the PO# line
        ship_match = re.search(r'PO #:.*\|.*\|.*\|.*\|\s*([^|_\n]+)', extracted_text)
        if ship_match:
            order_info['Shipping Instructions'] = ship_match.group(1).strip()

        # Ship To / Bill To = Drive Train Specialists' own address
        ship_parts = []
        name_match = re.search(r'(DRIVE TRAIN SPECIALISTS[^\n]*)', extracted_text)
        street_match = re.search(r'(\d{4,6}\s+Groesbeck[^\n]*)', extracted_text)
        csz_match = re.search(r'([A-Za-z]+,\s*MI\s*\d{5})', extracted_text)
        if name_match:
            ship_parts.append(name_match.group(1).strip())
        if street_match:
            ship_parts.append(street_match.group(1).strip())
        if csz_match:
            ship_parts.append(re.sub(r'\s+', ' ', csz_match.group(1)).strip())
        if ship_parts:
            order_info['Ship To'] = '\n'.join(ship_parts)
            order_info['Invoice Address'] = order_info['Ship To']

        comments = []
        terms_match = re.search(r'(Net\s+\d+\s+Days)', extracted_text)
        if terms_match:
            comments.append('Terms: ' + terms_match.group(1))
        tax_match = re.search(r'\b(\d{2}-\d{6,})\b', extracted_text)
        if tax_match:
            comments.append('Tax Exempt: ' + tax_match.group(1))
        req_match = re.search(r'Requested By\s*:\s*(\w+)', extracted_text)
        if req_match:
            comments.append('Requested By: ' + req_match.group(1))
        if comments:
            order_info['Comments'] = ' | '.join(comments)

        # Items: " 75 EACH AXLE SIDE SEAL  $3.00  $225.00" on one line,
        # then optional description continuation lines and a part number
        # line like "CR  13988" until the "____" section separator.
        dts_lines = extracted_text.split('\n')
        item_row_re = re.compile(r'^\s*([\d,]*\.?\d+)\s+[A-Z]{2,5}\s+(.*\S)\s+\$([\d,]+\.?\d*)\s+\$([\d,]+\.?\d*)\s*$')
        part_re = re.compile(r'^\s*([A-Z]{1,5})\s+([0-9][0-9A-Za-z\-]*)\s*$')
        raw_items = []
        in_items = False
        cur = None
        for l in dts_lines:
            if not in_items:
                if re.search(r'Quantity\s+UM\s+Description', l):
                    in_items = True
                continue
            if re.match(r'^\s*_{5,}', l) or re.match(r'^\s*Terms\b', l):
                break
            m = item_row_re.match(l)
            if m:
                cur = {
                    'qty': safe_float(m.group(1)),
                    'desc': [html.unescape(html.unescape(m.group(2))).strip()],
                    'price': safe_float(m.group(3)),
                    'total': safe_float(m.group(4)),
                    'part': ''
                }
                raw_items.append(cur)
                continue
            pm = part_re.match(l)
            if cur is not None and pm:
                # 'CR 13988' is DTS's internal number: the leading letters are
                # their mfr prefix, our part number is the numeric tail.
                cur['part'] = pm.group(2)
            elif cur is not None and l.strip():
                cur['desc'].append(html.unescape(html.unescape(l.strip())))

        for r in raw_items:
            qty = r['qty']
            item = {
                'Inventory Item': r['part'],
                'Description': ' '.join(r['desc']).strip(),
                'Quantity': int(qty) if qty == int(qty) else qty,
                'Unit Price': r['price'],
                'Total': r['total']
            }
            item.update(order_info)
            items.append(item)

    elif is_ford:
        # Ford Motor Company "Purchase Notification" format
        # PO number: "No - 837150-1"
        po_match = re.search(r'\bNo\s*-\s*([\d]+-[\d]+|\d+)\b', extracted_text)
        if po_match:
            order_info['Purchase Order Number'] = po_match.group(1)

        # "Date of order:\n9/10/2026"
        date_match = re.search(r'Date of order:\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})', extracted_text)
        if date_match:
            order_info['Date of Order'] = date_match.group(1)

        # "Ship No Later Than\n11/09/2026"
        ship_date_match = re.search(r'Ship No Later Than\s*\n?\s*(\d{1,2}/\d{1,2}/\d{4})', extracted_text)
        if ship_date_match:
            order_info['Ship By Date'] = ship_date_match.group(1)

        # Ship To block (includes location code like J7XPB)
        ship_to_match = re.search(r'\*SHIP TO:\s*\n(.*?)(?:Phone:|E-mail:|FAILURE)', extracted_text, re.DOTALL)
        if ship_to_match:
            order_info['Ship To'] = ship_to_match.group(1).strip()

        # Invoice address block
        inv_match = re.search(r'\*INVOICE:\s*\n(.*?)(?:Submit Invoices|Payment terms)', extracted_text, re.DOTALL)
        if inv_match:
            order_info['Invoice Address'] = inv_match.group(1).strip()

        # Supplier's Code = Ford's code for Ratech
        sup_match = re.search(r"Supplier's Code:\s*(\S+)", extracted_text)
        if sup_match:
            order_info['Vendor'] = sup_match.group(1)

        # Shipping point (e.g. FCA)
        sp_match = re.search(r'Shipping point:\s*\n?\s*(\S+)', extracted_text)
        if sp_match:
            order_info['Shipping Instructions'] = sp_match.group(1)

        comments = []
        pt_match = re.search(r'Payment terms:\s*\n?\s*([^\n]+)', extracted_text)
        if pt_match:
            comments.append('Payment Terms: ' + pt_match.group(1).strip())
        buyer_match = re.search(r'([A-Za-z]+\s+[A-Za-z]+),?\s*buyer', extracted_text, re.IGNORECASE)
        if buyer_match:
            comments.append('Buyer: ' + buyer_match.group(1))
        if comments:
            order_info['Comments'] = ' | '.join(comments)

        # Items: "873 M-1225-B 8.8" AXLE BEARING AND SEAL KIT (PAIR) 1 12.5500 USD 10,956.15 USD"
        # units | code | description | units/buy pack | unit price USD | total USD
        ford_item_re = re.compile(
            r'^\s*(\d+)\s+(\S+)\s+(.+?)\s+(\d+)\s+([\d.]+)\s*USD\s+([\d,]+\.\d+)\s*USD\s*$'
        )
        for line in lines:
            m = ford_item_re.match(line.strip())
            if m:
                qty = safe_int(m.group(1))
                desc = html.unescape(html.unescape(m.group(3))).strip()
                item = {
                    'Inventory Item': m.group(2),
                    'Description': desc,
                    'Quantity': qty,
                    'Unit Price': safe_float(m.group(5)),
                    'Total': safe_float(m.group(6))
                }
                item.update(order_info)
                items.append(item)

    elif is_ss_truck:
        # S&S Truck Parts specific parsing
        # Look for PO number - appears after "** PURCHASE ORDER **"
        po_match = re.search(r'\*\*\s*PURCHASE ORDER\s*\*\*\s*([P|p]?\d+)', extracted_text)
        if po_match:
            order_info['Purchase Order Number'] = po_match.group(1)
        
        # Look for date and vendor - date appears before 6-digit vendor number
        # Pattern: "10/06/26\n101050" 
        date_vendor_match = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})\s*\n\s*(\d{6})', extracted_text)
        if date_vendor_match:
            order_info['Date of Order'] = date_vendor_match.group(1)
            order_info['Vendor'] = date_vendor_match.group(2)
        
        # Look for DELIVER ON date
        delivery_match = re.search(r'DELIVER ON\s*(\d{1,2}/\d{1,2}/\d{2,4})', extracted_text)
        if delivery_match:
            order_info['Ship By Date'] = delivery_match.group(1)
        
        # Look for S&S Truck Parts address - appears twice in the text
        # The pattern is: S&S Truck Parts\n9401 Georgia Street Suite 1\nCrown Point IN 46307\nU.S.A.
        ss_addresses = re.findall(r'S&S Truck Parts\s*\n\s*(9401.*?U\.S\.A\.)', extracted_text, re.DOTALL)
        if ss_addresses:
            # Use the first occurrence
            ss_address = 'S&S Truck Parts\n' + ss_addresses[0].strip()
            order_info['Ship To'] = ss_address
            order_info['Invoice Address'] = ss_address
        
        # Look for shipping method - find DAYTON FREIGHT or similar
        ship_match = re.search(r'\b(DAYTON FREIGHT|FEDEX|UPS|UPS GROUND|FREIGHT COLLECT)\b', extracted_text)
        if ship_match:
            order_info['Shipping Instructions'] = ship_match.group(1)
        
        # Extract comments from specific sections
        comments = []
        
        # Look for freight terms
        freight_match = re.search(r'\b(Collect|Prepaid|FOB Factory)\b', extracted_text)
        if freight_match:
            comments.append(f"Freight: {freight_match.group(1)}")
        
        # Look for payment terms
        terms_match = re.search(r'NET \d+ DAYS [A-Z\s]+', extracted_text)
        if terms_match:
            comments.append(terms_match.group(0).strip())
        
        # Look for shipping notes
        notes_match = re.search(r'Seller shall acknowledge order.*?billed back to shipper\.', extracted_text, re.DOTALL)
        if notes_match:
            notes_text = re.sub(r'\s+', ' ', notes_match.group(0)).strip()
            comments.append(notes_text)
        
        if comments:
            order_info['Comments'] = ' | '.join(comments)
        
        # Find items - look for ITEM- pattern
        for idx, line in enumerate(lines):
            line = line.strip()
            if 'ITEM-' in line:
                # Extract CAT NBR (this is the inventory item code)
                # Look for CAT NBR in subsequent lines
                code = ""
                for i in range(idx + 1, min(idx + 10, len(lines))):
                    next_line = lines[i].strip()
                    cat_match = re.search(r'CAT NBR\s*-\s*(\d+)', next_line)
                    if cat_match:
                        code = cat_match.group(1)
                        break
                
                # Fallback to ITEM- number if CAT NBR not found
                if not code:
                    item_match = re.search(r'ITEM-\s*(\S+)', line)
                    code = item_match.group(1) if item_match else ""
                
                if code:
                    # Look for quantity and price in subsequent lines
                    for i in range(idx + 1, min(idx + 10, len(lines))):
                        next_line = lines[i].strip()
                        
                        # Look for DELIVER ON line with quantity and price
                        if 'DELIVER ON' in next_line:
                            # Extract quantity (e.g., "1500.000 EA")
                            qty_match = re.search(r'(\d+\.\d+)\s+EA', next_line)
                            quantity = safe_float(qty_match.group(1)) if qty_match else 0
                            quantity = int(quantity) if quantity else 0
                            
                            # Extract unit price (e.g., ".7300")
                            price_match = re.search(r'(\d+\.\d+)\s+EA\s+(\.\d+|\d+\.\d+)', next_line)
                            if price_match:
                                price_str = price_match.group(2)
                                unit_price = safe_float('0' + price_str) if price_str.startswith('.') else safe_float(price_str)
                            else:
                                unit_price = 0.0
                            
                            # Extract description from previous lines
                            desc_lines = []
                            for j in range(idx + 1, i):
                                prev_line = lines[j].strip()
                                if prev_line and 'CAT NBR' not in prev_line and 'PKG CODE' not in prev_line:
                                    desc_lines.append(prev_line)
                            
                            description = ' '.join(desc_lines)
                            # Clean up description
                            description = re.sub(r'^\d+\s+ITEM-\S+\s*', '', description)
                            description = re.sub(r'WMS UPC#:\d+\s*', '', description)
                            description = re.sub(r'CAT NBR - \d+\s*', '', description)
                            description = re.sub(r'PKG CODE:.*?APPROVED\s*', '', description)
                            description = re.sub(r'\s+', ' ', description).strip()
                            
                            # Get total from next line
                            total = 0.0
                            if i + 1 < len(lines):
                                total_line = lines[i + 1].strip()
                                total_match = re.search(r'(\d+\.\d+)\s*$', total_line)
                                if total_match:
                                    total = safe_float(total_match.group(1))
                                else:
                                    total = unit_price * quantity
                            
                            item = {
                                'Inventory Item': str(code),
                                'Description': str(description),
                                'Quantity': int(quantity),
                                'Unit Price': float(unit_price),
                                'Total': float(total)
                            }
                            item.update(order_info)
                            items.append(item)
                            break
                    break
    
    else:
        # Generic parsing for unknown formats - conservative: only create a
        # row when the line has BOTH a quantity token and a price, and the
        # leading text looks like a description rather than a label
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Skip obvious non-item lines
            low = line.lower()
            if any(k in low for k in ('total', 'subtotal', 'tax', 'balance',
                                      'amount due', 'payment', 'phone', 'fax')):
                continue

            if re.search(r'\d+\.\d{2}', line):
                try:
                    price_match = re.search(r'(\d+\.\d{2})', line)
                    if not price_match:
                        continue
                    price = safe_float(price_match.group(1))

                    # Require a quantity token like "5 x", "5 EA", "Qty 5"
                    qty_match = re.search(r'(?:qty\s*:?\s*|\b)(\d+)\s*(?:x|ea|pcs|units|each)\b', line, re.IGNORECASE)
                    if not qty_match:
                        continue
                    quantity = safe_int(qty_match.group(1))
                    if quantity <= 0:
                        continue

                    # Extract item name (everything before the first price)
                    item_name = re.sub(r'\d+\.\d{2}.*', '', line).strip()
                    item_name = re.sub(r'^\d+\s*(?:x|ea|pcs|units|each)\b[:\-]?', '', item_name, flags=re.IGNORECASE).strip(' :-')

                    if item_name and len(item_name) > 2:
                        item = {
                            'Inventory Item': '',
                            'Description': item_name,
                            'Quantity': quantity,
                            'Unit Price': price,
                            'Total': price * quantity
                        }
                        item.update(order_info)
                        items.append(item)
                except (ValueError, IndexError):
                    continue

    # Known formats get a Gemini last resort when the deterministic parser
    # found nothing (layout drift beats missing the order entirely).
    if not items and gemini_api_key and known_format:
        print("DEBUG: regex found no items, trying Gemini as last resort")
        gemini_items = parse_order_with_gemini(extracted_text, gemini_api_key)
        if gemini_items:
            return gemini_items

    return items


def parse_order_with_gemini(text: str, api_key: str) -> List[Dict]:
    """
    Use Google Gemini to parse order text into structured data
    
    Args:
        text: Raw extracted text from the document
        api_key: Gemini API key
        
    Returns:
        List of dictionaries containing order items
    """
    try:
        import google.generativeai as genai
        
        # Check if API key is provided
        if not api_key:
            print("ERROR: No Gemini API key provided")
            return []
        
        print(f"DEBUG: Using Gemini API key: {api_key[:10]}...")
        
        # Configure Gemini
        genai.configure(api_key=api_key)
        
        # Create prompt for structured extraction
        prompt = f"""Extract order information from this text and return it as JSON. The text may be from an email printout, so ignore email headers and formatting.

Required fields for each item:
- Purchase Order Number (from the order, not the email)
- Date of Order (format: MM/DD/YY or MM/DD/YYYY)
- Ship By Date (format: MM/DD/YY or MM/DD/YYYY)
- Ship To (full address)
- Invoice Address (full address)
- Vendor (name or code)
- Shipping Instructions
- Comments
- Inventory Item (part number or code - remove any "RAT -" or "RAT-" prefix if present)
- Description (item description)
- Quantity (numeric)
- Unit Price (numeric)
- Total (numeric)

Important:
- If the text contains email headers (From:, To:, Subject:, Date:), ignore them
- If an item code starts with "RAT -" or "RAT-", remove that prefix and keep only the actual part number
- Return ONLY valid JSON, no explanations
- Extract ALL items from the order

Return ONLY valid JSON in this exact format:
{{
  "items": [
    {{
      "Purchase Order Number": "...",
      "Date of Order": "...",
      "Ship By Date": "...",
      "Ship To": "...",
      "Invoice Address": "...",
      "Vendor": "...",
      "Shipping Instructions": "...",
      "Comments": "...",
      "Inventory Item": "...",
      "Description": "...",
      "Quantity": 0,
      "Unit Price": 0.0,
      "Total": 0.0
    }}
  ]
}}

Text to parse:
{text}

JSON Output:"""

        # Generate response - try current models first, fall back if retired
        print("DEBUG: Calling Gemini API...")
        response = None
        last_err = None
        for model_name in ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-1.5-flash']:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                print(f"DEBUG: Success with model {model_name}")
                break
            except Exception as model_err:
                print(f"DEBUG: Model {model_name} failed: {model_err}")
                last_err = model_err
        if response is None:
            raise last_err or Exception("All Gemini models failed")
        response_text = response.text.strip()
        
        print(f"DEBUG: Gemini response: {response_text[:200]}...")
        
        # Try to extract JSON from response
        # Sometimes Gemini adds markdown code blocks
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()
        
        # Parse JSON
        import json
        result = json.loads(response_text)
        
        # Return items array
        if 'items' in result and isinstance(result['items'], list):
            items = result['items']
            print(f"DEBUG: Gemini returned {len(items)} items")
            # Clean up any "RAT -" prefixes that Gemini might have missed
            for item in items:
                if 'Inventory Item' in item and isinstance(item['Inventory Item'], str):
                    # Remove "RAT -" or "RAT-" prefix
                    item['Inventory Item'] = re.sub(r'^RAT\s*-\s*', '', item['Inventory Item'])
            return items
        else:
            # If items not in expected format, return single item
            print("DEBUG: Gemini response not in expected format")
            return [result] if isinstance(result, dict) else []
            
    except Exception as e:
        print(f"Gemini parsing error: {e}")
        import traceback
        traceback.print_exc()
        return []


def parse_packing_slip_with_gemini(text: str, api_key: str) -> Dict:
    """Use Gemini to extract structured data from a packing slip / receiving document."""
    import google.generativeai as genai
    import json

    genai.configure(api_key=api_key)

    prompt = f"""You are a data extraction assistant. Extract structured data from this PACKING SLIP / receiving document text.

The document lists items shipped. A worker may have hand-written the quantity actually received next to each line item.

Extract:
- po_number: the purchase order / order number
- vendor: the vendor/supplier name
- date: the document or ship date
- items: array of line items with:
  - "Inventory Item": part/item number or code (strip prefixes like RAT-)
  - "Description": item description
  - "Expected Quantity": printed quantity shipped/ordered (numeric)
  - "Received Quantity": handwritten received count if visible next to the line (numeric, or null if none)

Return ONLY valid JSON:
{{
  "po_number": "...",
  "vendor": "...",
  "date": "...",
  "items": [
    {{"Inventory Item": "...", "Description": "...", "Expected Quantity": 0, "Received Quantity": null}}
  ]
}}

Text to parse:
{text}

JSON Output:"""

    response = None
    last_err = None
    for model_name in ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-1.5-flash']:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            break
        except Exception as model_err:
            print(f"DEBUG: Model {model_name} failed: {model_err}")
            last_err = model_err
    if response is None:
        raise last_err or Exception("All Gemini models failed")

    response_text = response.text.strip()
    if '```json' in response_text:
        response_text = response_text.split('```json')[1].split('```')[0].strip()
    elif '```' in response_text:
        response_text = response_text.split('```')[1].split('```')[0].strip()

    result = json.loads(response_text)
    if not isinstance(result, dict):
        return {}
    return result


def parse_packing_slip_text(extracted_text: str, gemini_api_key: Optional[str] = None) -> Dict:
    """Parse packing slip text into receiving data.

    Returns {'po_number': str, 'vendor': str, 'date': str,
             'items': [{Inventory Item, Description, Expected Quantity, Received Quantity}]}
    """
    import re
    result = {'po_number': '', 'vendor': '', 'date': '', 'items': []}

    if gemini_api_key:
        try:
            gem = parse_packing_slip_with_gemini(extracted_text, gemini_api_key)
            if gem.get('items'):
                result['po_number'] = str(gem.get('po_number') or '')
                result['vendor'] = str(gem.get('vendor') or '')
                result['date'] = str(gem.get('date') or '')
                result['items'] = gem['items']
        except Exception as e:
            print(f"Gemini packing slip parse failed: {e}")

    if not result['items']:
        # Heuristic fallback: lines like "PARTNUM  description  qty"
        po = re.search(r'(?:P\.?\s*O\.?|Purchase Order|Order)\s*#?\s*:?\s*([A-Za-z0-9\-]{3,})',
                       extracted_text)
        if po:
            result['po_number'] = po.group(1)
        d = re.search(r'\d{1,2}/\d{1,2}/\d{2,4}', extracted_text)
        if d:
            result['date'] = d.group(0)
        for line in extracted_text.split('\n'):
            line = line.strip()
            m = re.match(r'^([A-Za-z0-9][A-Za-z0-9\-_./]{2,})\s+(.+?)\s+(\d{1,5})\s*$', line)
            if m:
                desc = m.group(2)
                if any(k in desc.lower() for k in ('total', 'page', 'phone', 'fax', 'order')):
                    continue
                result['items'].append({
                    'Inventory Item': m.group(1),
                    'Description': desc,
                    'Expected Quantity': int(m.group(3)),
                    'Received Quantity': None,
                })
    return result
