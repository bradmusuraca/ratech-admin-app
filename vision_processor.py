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
    
    # Simple pattern matching for common fields
    full_text = ' '.join(lines)
    
    # Look for PO number
    po_match = re.search(r'(?:PO|P\.O\.|PURCHASE ORDER)[\s#:\-]*([A-Z0-9\-]+)', full_text, re.IGNORECASE)
    if po_match:
        order_info['Purchase Order Number'] = po_match.group(1)
    
    # Look for dates (first date is usually order date)
    dates = re.findall(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', full_text)
    if dates:
        order_info['Date of Order'] = dates[0]
        if len(dates) > 1:
            order_info['Ship By Date'] = dates[1]
    
    # Look for vendor name (usually first line with company name)
    vendor_match = re.search(r'^([A-Z][a-zA-Z\s&]+(?:Parts|Company|Corp|Inc|LLC)?)', full_text, re.MULTILINE)
    if vendor_match:
        order_info['Vendor'] = vendor_match.group(1).strip()
    
    # Look for addresses (lines with street addresses)
    address_lines = []
    for line in lines:
        if re.search(r'\d+\s+[A-Z][a-z]+\s+(?:St|Ave|Dr|Rd|Ln|Blvd)', line, re.IGNORECASE):
            address_lines.append(line.strip())
    
    if len(address_lines) >= 2:
        order_info['Ship To'] = address_lines[0]
        order_info['Invoice Address'] = address_lines[1]
    elif address_lines:
        order_info['Ship To'] = address_lines[0]
        order_info['Invoice Address'] = address_lines[0]
    
    # Look for shipping method
    shipping_keywords = ['FEDEX', 'UPS', 'USPS', 'FREIGHT', 'TRUCK', 'DELIVERY']
    for line in lines:
        if any(keyword in line.upper() for keyword in shipping_keywords):
            order_info['Shipping Instructions'] = line.strip()
            break
    
    # Look for comments (text after "Notes:", "Comments:", etc.)
    comment_match = re.search(r'(?:Notes|Comments|Special Instructions)[\s:]*(.+?)(?:\n|$)', full_text, re.IGNORECASE | re.DOTALL)
    if comment_match:
        order_info['Comments'] = comment_match.group(1).strip()
    
    # Try to find items - look for patterns like "Item: 5 x $20.00" or "Widget A - $10.00"
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Look for lines with prices
        if '$' in line or 'USD' in line or re.search(r'\d+\.\d{2}', line):
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
                    
                    # Try to extract code (alphanumeric before description)
                    code_match = re.search(r'([A-Z0-9\-]+)\s+', item_name)
                    code = code_match.group(1) if code_match else ""
                    
                    if item_name:
                        item = {
                            'Code': code,
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
