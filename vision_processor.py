import os
import json
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
        OCR a PDF by converting pages to images first
        Note: This requires pdf2image which may not be available on all platforms
        
        Args:
            pdf_path: Path to the PDF file
            
        Returns:
            Extracted text as a string
        """
        try:
            from pdf2image import convert_from_path
            
            # Convert PDF to images
            images = convert_from_path(pdf_path)
            
            full_text = ""
            for i, image in enumerate(images):
                # Convert PIL image to bytes
                img_byte_arr = io.BytesIO()
                image.save(img_byte_arr, format='PNG')
                img_byte_arr = img_byte_arr.getvalue()
                
                # Use Vision API
                vision_image = vision.Image(content=img_byte_arr)
                response = self.client.document_text_detection(image=vision_image)
                
                if response.error.message:
                    print(f"Error on page {i+1}: {response.error.message}")
                    continue
                
                full_text += response.full_text_annotation.text + "\n"
            
            return full_text
            
        except ImportError:
            print("pdf2image not available. For scanned PDFs, please convert to images first.")
            raise NotImplementedError(
                "OCR for scanned PDFs requires pdf2image. "
                "Please convert your PDF to images or use a PDF with embedded text."
            )
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


def parse_order_text(extracted_text: str) -> List[Dict]:
    """
    Parse extracted text into structured order data
    Supports multiple order formats (Ford, S&S Truck Parts, etc.)
    
    Args:
        extracted_text: Raw text extracted from PDF
        
    Returns:
        List of dictionaries representing order items
    """
    import re
    
    items = []
    lines = extracted_text.split('\n')
    
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
    
    # Detect format type
    full_text = ' '.join(lines)
    
    # Check if it's Ford format
    is_ford = 'Ford Motor Company' in full_text or 'FORD RACING' in full_text
    # Check if it's S&S Truck Parts format
    is_ss_truck = 'S&S Truck Parts' in full_text or 'sandstruck.com' in full_text
    
    if is_ford:
        # Ford format parsing
        po_match = re.search(r'No\s*-\s*(\d+(?:-\d+)?)', full_text)
        if po_match:
            order_info['Purchase Order Number'] = po_match.group(1)
        
        date_match = re.search(r'Date\s+of\s+order:\s*(\d{1,2}/\d{1,2}/\d{4})', full_text)
        if date_match:
            order_info['Date of Order'] = date_match.group(1)
        
        ship_match = re.search(r'Ship\s+No\s+Later\s+Than\s*(\d{1,2}/\d{1,2}/\d{4})', full_text)
        if ship_match:
            order_info['Ship By Date'] = ship_match.group(1)
        
        for i, line in enumerate(lines):
            if 'SHIP TO:' in line:
                ship_to_lines = []
                for j in range(i + 1, min(i + 5, len(lines))):
                    if lines[j].strip() and not any(x in lines[j] for x in ['INVOICE', 'Payment', 'Phone', 'E-mail', 'FAILURE']):
                        ship_to_lines.append(lines[j].strip())
                    elif 'Phone:' in lines[j] or 'E-mail:' in lines[j] or 'FAILURE' in lines[j]:
                        break
                order_info['Ship To'] = ' '.join(ship_to_lines)
                break
        
        for i, line in enumerate(lines):
            if 'INVOICE:' in line:
                invoice_lines = []
                for j in range(i + 1, min(i + 5, len(lines))):
                    if lines[j].strip() and not any(x in lines[j] for x in ['Submit', 'Email', 'Payment']):
                        invoice_lines.append(lines[j].strip())
                    elif 'Submit' in lines[j] or 'Email' in lines[j]:
                        break
                order_info['Invoice Address'] = ' '.join(invoice_lines)
                break
        
        # Extract freight/shipping instructions
        freight_idx = -1
        for i, line in enumerate(lines):
            if 'Freight Routing Instructions:' in line:
                freight_idx = i
                break
        
        if freight_idx > 0:
            freight_lines = []
            for j in range(freight_idx + 1, min(freight_idx + 20, len(lines))):
                if lines[j].strip() and not any(x in lines[j] for x in ['Notes:', 'For additional', 'Ford\'s Global']):
                    freight_lines.append(lines[j].strip())
                elif 'Notes:' in lines[j] or 'For additional' in lines[j]:
                    break
            order_info['Shipping Instructions'] = ' '.join(freight_lines)
    
    elif is_ss_truck:
        # S&S Truck Parts format parsing
        # The headers are at the top, data appears later in the document
        
        # Extract PO number - look for "P059502" pattern after "** PURCHASE ORDER **"
        po_match = re.search(r'\*\*\s*PURCHASE ORDER\s*\*\*\s*([P|p]?\d+)', full_text)
        if po_match:
            order_info['Purchase Order Number'] = po_match.group(1)
        
        # Extract PO date and vendor - look for "10/06/26\n101050" pattern
        # The date appears before the 6-digit vendor number
        date_vendor_match = re.search(r'(\d{2}/\d{2}/\d{2})\s*\n\s*(\d{6})', full_text)
        if date_vendor_match:
            order_info['Date of Order'] = date_vendor_match.group(1)
            order_info['Vendor'] = date_vendor_match.group(2)
        
        # Extract delivery date from "DELIVER ON" line
        delivery_match = re.search(r'DELIVER ON\s*(\d{1,2}/\d{1,2}/\d{2,4})', full_text)
        if delivery_match:
            order_info['Ship By Date'] = delivery_match.group(1)
        
        # Extract Ship To and Bill To - both are S&S Truck Parts
        # The format is: S&S Truck Parts\n9401 Georgia Street Suite 1\nCrown Point IN 46307\nU.S.A.
        # Find all occurrences of S&S Truck Parts followed by address
        ss_matches = re.findall(r'S&S Truck Parts\s*\n\s*(9401.*?U\.S\.A\.)', full_text, re.DOTALL)
        if ss_matches:
            # Use the first match (they're the same address)
            ss_address_text = ss_matches[0].strip()
            ss_full_address = 'S&S Truck Parts\n' + ss_address_text
            order_info['Ship To'] = ss_full_address
            order_info['Invoice Address'] = ss_full_address
        
        # Extract Shipping Instructions - look for freight carrier
        ship_via_match = re.search(r'(DAYTON FREIGHT|FEDEX|UPS|UPS GROUND|FREIGHT COLLECT)', full_text)
        if ship_via_match:
            order_info['Shipping Instructions'] = ship_via_match.group(1).strip()
        
        # Extract Comments - get freight terms and payment terms
        comments = []
        
        # Look for freight terms (Collect, Prepaid, FOB Factory, etc.)
        freight_match = re.search(r'\b(Collect|Prepaid|FOB Factory|Freight Collect)\b', full_text)
        if freight_match:
            comments.append(f"Freight: {freight_match.group(1)}")
        
        # Look for NET payment terms
        terms_match = re.search(r'NET \d+ DAYS [A-Z\s]+', full_text)
        if terms_match:
            comments.append(terms_match.group(0).strip())
        
        # Add the shipping notes from the vendor copy section
        notes_match = re.search(r'Seller shall acknowledge order.*?billed back to shipper\.', full_text, re.DOTALL)
        if notes_match:
            notes_text = re.sub(r'\s+', ' ', notes_match.group(0)).strip()
            comments.append(notes_text)
        
        if comments:
            order_info['Comments'] = ' | '.join(comments)
    
    # Try to detect order format for items
    if is_ford:
        # Ford order format pattern:
        # "873 M-1225-B 8.8" AXLE BEARING AND SEAL KIT (PAIR) 1 12.5500 USD 10,956.15 USD"
        # Pattern: quantity code description unit_pack unit_price total_price
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Look for lines with USD prices (indicates order item line)
            if 'USD' in line and re.search(r'\d+\.\d+\s+USD', line):
                try:
                    # Extract quantity (first number)
                    quantity_match = re.search(r'^(\d+)\s+', line)
                    if not quantity_match:
                        continue
                    quantity = int(quantity_match.group(1))
                    
                    # Extract code (alphanumeric with dashes, after quantity)
                    code_match = re.search(r'^\d+\s+([A-Z0-9-]+)\s+', line)
                    code = code_match.group(1) if code_match else ""
                    
                    # Extract unit price (first USD price)
                    unit_price_match = re.search(r'(\d+[\.,]\d+)\s+USD', line)
                    if not unit_price_match:
                        continue
                    unit_price = float(unit_price_match.group(1).replace(',', ''))
                    
                    # Extract total price (second USD price) - handle commas properly
                    # Find all USD prices in the line
                    total_price_matches = re.findall(r'([\d,]+\.?\d*)\s+USD', line)
                    if len(total_price_matches) >= 2:
                        # Use the last match as total price (typically the larger value)
                        total_price_str = total_price_matches[-1].replace(',', '')
                        total_price = float(total_price_str)
                    else:
                        total_price = unit_price * quantity
                    
                    # Extract description (between code and unit price)
                    description_parts = line.split(code)
                    if len(description_parts) > 1:
                        description_part = description_parts[1]
                        # Remove unit pack and price info
                        desc_match = re.match(r'\s+(.+?)\s+\d+\s+\d+[\.,]\d+\s+USD', description_part)
                        if desc_match:
                            description = desc_match.group(1).strip()
                        else:
                            # Fallback: take everything before the price
                            description = re.sub(r'\s+\d+\s+\d+[\.,]\d+\s+USD.*', '', description_part).strip()
                    else:
                        description = line
                    
                    item = {
                        'Code': code,
                        'Item': description,
                        'Quantity': quantity,
                        'Unit Price': unit_price,
                        'Total': total_price
                    }
                    
                    # Add order info to each item
                    item.update(order_info)
                    
                    items.append(item)
                    
                except (ValueError, IndexError, AttributeError) as e:
                    # If Ford parsing fails, try generic parsing
                    continue
    
    elif is_ss_truck:
        # S&S Truck Parts format - handle multi-line items
        # Format example:
        # "    1 ITEM- 3507678         WMS UPC#:698231109694
        #    CAT NBR - 3103
        #   CRUSH SLEEVE C9.25"
        #   PKG CODE: PP014-BULK PACKAGING APPROVED
        #                       DELIVER ON 10/22/26    1500.000 EA            .7300  
        #       1095.00"
        
        # Find the first line that starts an item (number followed by ITEM-)
        for i in range(len(lines)):
            line = lines[i].strip()
            
            # Check if this line starts an item (number followed by ITEM-)
            if re.search(r'^\s*\d+\s+ITEM-', line):
                try:
                    # Extract item number
                    item_match = re.search(r'ITEM-\s*(\S+)', line)
                    code = item_match.group(1) if item_match else ""
                    
                    # Collect description lines and find price info
                    description_lines = []
                    unit_price = 0
                    total = 0
                    quantity = 0
                    j = i + 1
                    
                    # Look ahead for description and price lines
                    while j < len(lines) and j < i + 8:
                        current_line = lines[j].strip()
                        
                        # Check if this line has DELIVER ON with prices
                        if 'DELIVER ON' in current_line:
                            # Extract quantity from DELIVER ON line (e.g., "1500.000 EA")
                            qty_match = re.search(r'(\d+\.\d+)\s+EA', current_line)
                            if qty_match:
                                quantity = int(float(qty_match.group(1)))
                            
                            # Extract unit price - look for pattern like ".7300" or "0.7300"
                            price_match = re.search(r'(\d+\.\d+)\s+EA\s+(\.\d+|\d+\.\d+)', current_line)
                            if price_match:
                                unit_price_str = price_match.group(2)
                                # Handle leading decimal point (e.g., ".7300" -> 0.73)
                                if unit_price_str.startswith('.'):
                                    unit_price = float('0' + unit_price_str)
                                else:
                                    unit_price = float(unit_price_str)
                            
                            # Get total from next line
                            if j + 1 < len(lines):
                                total_line = lines[j + 1].strip()
                                total_match = re.search(r'(\d+\.\d+)\s*$', total_line)
                                if total_match:
                                    total = float(total_match.group(1))
                                else:
                                    total = unit_price * quantity
                            
                            # Build description
                            description = ' '.join(description_lines)
                            # Clean up description
                            description = re.sub(r'^\s*\d+\s+ITEM-\S+\s*', '', description)
                            description = re.sub(r'WMS UPC#:\d+\s*', '', description)
                            description = re.sub(r'CAT NBR - \d+\s*', '', description)
                            description = re.sub(r'PKG CODE:.*?APPROVED\s*', '', description)
                            description = re.sub(r'\s+', ' ', description).strip()
                            
                            item = {
                                'Code': code,
                                'Item': description,
                                'Quantity': quantity,
                                'Unit Price': unit_price,
                                'Total': total
                            }
                            
                            # Add order info to each item
                            item.update(order_info)
                            
                            items.append(item)
                            break
                        
                        # Collect description lines (skip metadata)
                        if current_line and not re.search(r'CAT NBR|PKG CODE', current_line):
                            description_lines.append(current_line)
                        
                        j += 1
                    
                    # Only process the first item we find, then exit
                    break
                    
                except (ValueError, IndexError, AttributeError) as e:
                    break
    
    # If Ford parsing found items, return them
    if items:
        return items
    
    # Fallback: generic parsing for other formats
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Look for patterns like: "Item Name - $10.00" or "Item: 5 x $20.00"
        if '$' in line or 'USD' in line:
            try:
                # Try to extract price
                price_match = re.search(r'[\$]?(\d+[\.,]\d+)\s*(USD)?', line)
                if price_match:
                    price = float(price_match.group(1).replace(',', ''))
                    
                    # Try to extract quantity
                    qty_match = re.search(r'(\d+)\s*(x|ea|pcs|units)?', line)
                    quantity = int(qty_match.group(1)) if qty_match else 1
                    
                    # Extract item name (everything before price)
                    item_name = re.sub(r'[\$]?\d+[\.,]\d+\s*(USD)?.*', '', line).strip()
                    
                    if item_name:
                        item = {
                            'Code': '',
                            'Item': item_name,
                            'Quantity': quantity,
                            'Unit Price': price,
                            'Total': price * quantity
                        }
                        item.update(order_info)
                        items.append(item)
            except (ValueError, IndexError):
                continue
    
    return items
