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
    
    # Try Gemini parsing first if API key is provided
    if gemini_api_key:
        gemini_items = parse_order_with_gemini(extracted_text, gemini_api_key)
        if gemini_items:
            return gemini_items
    
    # Fallback to simple regex-based parsing
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
    
    # Check if it's S&S Truck Parts format (has specific patterns)
    is_ss_truck = 'S&S Truck Parts' in full_text or 'sandstruck.com' in full_text or 'SSPO' in full_text
    
    if is_ss_truck:
        # S&S Truck Parts specific parsing
        # Look for PO number in format like "P059502"
        po_match = re.search(r'\b(P\d{5,})\b', full_text)
        if po_match:
            order_info['Purchase Order Number'] = po_match.group(1)
        
        # Look for dates - specifically the date before a 6-digit vendor number
        # Pattern: date\nvendor_number (like 10/06/26\n101050)
        date_vendor_match = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})\s*\n\s*(\d{6})', full_text)
        if date_vendor_match:
            order_info['Date of Order'] = date_vendor_match.group(1)
            order_info['Vendor'] = date_vendor_match.group(2)
        
        # Look for DELIVER ON date
        delivery_match = re.search(r'DELIVER ON\s*(\d{1,2}/\d{1,2}/\d{2,4})', full_text)
        if delivery_match:
            order_info['Ship By Date'] = delivery_match.group(1)
        
        # Look for S&S Truck Parts address (appears twice - use first occurrence)
        ss_address_match = re.search(r'S&S Truck Parts\s*\n\s*(9401.*?U\.S\.A\.)', full_text, re.DOTALL)
        if ss_address_match:
            ss_address = 'S&S Truck Parts\n' + ss_address_match.group(1).strip()
            order_info['Ship To'] = ss_address
            order_info['Invoice Address'] = ss_address
        
        # Look for shipping method
        ship_match = re.search(r'\b(DAYTON FREIGHT|FEDEX|UPS|UPS GROUND|FREIGHT)\b', full_text)
        if ship_match:
            order_info['Shipping Instructions'] = ship_match.group(1)
        
        # Extract comments from specific sections
        comments = []
        
        # Look for freight terms
        freight_match = re.search(r'\b(Collect|Prepaid|FOB Factory)\b', full_text)
        if freight_match:
            comments.append(f"Freight: {freight_match.group(1)}")
        
        # Look for payment terms
        terms_match = re.search(r'NET \d+ DAYS [A-Z\s]+', full_text)
        if terms_match:
            comments.append(terms_match.group(0).strip())
        
        # Look for shipping notes
        notes_match = re.search(r'Seller shall acknowledge order.*?billed back to shipper\.', full_text, re.DOTALL)
        if notes_match:
            notes_text = re.sub(r'\s+', ' ', notes_match.group(0)).strip()
            comments.append(notes_text)
        
        if comments:
            order_info['Comments'] = ' | '.join(comments)
        
        # Find items - look for ITEM- pattern
        for line in lines:
            line = line.strip()
            if 'ITEM-' in line:
                # Extract item code
                item_match = re.search(r'ITEM-\s*(\S+)', line)
                if item_match:
                    code = item_match.group(1)
                    
                    # Look for quantity and price in subsequent lines
                    idx = lines.index(line)
                    for i in range(idx + 1, min(idx + 10, len(lines))):
                        next_line = lines[i].strip()
                        
                        # Look for DELIVER ON line with quantity and price
                        if 'DELIVER ON' in next_line:
                            # Extract quantity (e.g., "1500.000 EA")
                            qty_match = re.search(r'(\d+\.\d+)\s+EA', next_line)
                            quantity = int(float(qty_match.group(1))) if qty_match else 0
                            
                            # Extract unit price (e.g., ".7300")
                            price_match = re.search(r'(\d+\.\d+)\s+EA\s+(\.\d+|\d+\.\d+)', next_line)
                            if price_match:
                                price_str = price_match.group(2)
                                unit_price = float('0' + price_str) if price_str.startswith('.') else float(price_str)
                            else:
                                unit_price = 0
                            
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
                            total = 0
                            if i + 1 < len(lines):
                                total_line = lines[i + 1].strip()
                                total_match = re.search(r'(\d+\.\d+)\s*$', total_line)
                                if total_match:
                                    total = float(total_match.group(1))
                                else:
                                    total = unit_price * quantity
                            
                            item = {
                                'Code': code,
                                'Item': description,
                                'Quantity': quantity,
                                'Unit Price': unit_price,
                                'Total': total
                            }
                            item.update(order_info)
                            items.append(item)
                            break
                    break
    
    else:
        # Generic parsing for other formats
        # Look for lines with prices and try to extract items
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Look for lines that might contain items (have prices)
            if re.search(r'\d+\.\d{2}', line):
                try:
                    # Try to extract price
                    price_match = re.search(r'(\d+\.\d{2})', line)
                    if price_match:
                        price = float(price_match.group(1))
                        
                        # Try to extract quantity (look for pattern like "5 x" or "5 EA")
                        qty_match = re.search(r'(\d+)\s*(?:x|ea|pcs|units)', line, re.IGNORECASE)
                        quantity = int(qty_match.group(1)) if qty_match else 1
                        
                        # Extract item name (everything before price)
                        item_name = re.sub(r'\d+\.\d{2}.*', '', line).strip()
                        
                        if item_name and len(item_name) > 2:
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
        
        # Configure Gemini
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Create prompt for structured extraction
        prompt = f"""Extract order information from this purchase order text and return it as JSON.

Required fields:
- Purchase Order Number
- Date of Order (format: MM/DD/YY or MM/DD/YYYY)
- Ship By Date (format: MM/DD/YY or MM/DD/YYYY)
- Ship To (full address)
- Invoice Address (full address)
- Vendor (name or code)
- Shipping Instructions
- Comments
- Items (array of objects with: Code, Item, Quantity, Unit Price, Total)

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
      "Code": "...",
      "Item": "...",
      "Quantity": 0,
      "Unit Price": 0.0,
      "Total": 0.0
    }}
  ]
}}

Purchase Order Text:
{text}

JSON Output:"""

        # Generate response
        response = model.generate_content(prompt)
        response_text = response.text.strip()
        
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
            return result['items']
        else:
            # If items not in expected format, return single item
            return [result] if isinstance(result, dict) else []
            
    except Exception as e:
        print(f"Gemini parsing error: {e}")
        return []
