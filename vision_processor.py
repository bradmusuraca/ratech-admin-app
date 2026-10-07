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
    Custom parser for Ford Motor Company purchase orders
    
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
        'Invoice Address': ''
    }
    
    # Extract header info - look for patterns across the entire text
    full_text = ' '.join(lines)
    
    # Purchase Order number
    po_match = re.search(r'No\s*-\s*(\d+(?:-\d+)?)', full_text)
    if po_match:
        order_info['Purchase Order Number'] = po_match.group(1)
    
    # Date of order - handle various spacing
    date_match = re.search(r'Date\s+of\s+order:\s*(\d{1,2}/\d{1,2}/\d{4})', full_text)
    if date_match:
        order_info['Date of Order'] = date_match.group(1)
    
    # Ship by date - handle various spacing
    ship_match = re.search(r'Ship\s+No\s+Later\s+Than\s*(\d{1,2}/\d{1,2}/\d{4})', full_text)
    if ship_match:
        order_info['Ship By Date'] = ship_match.group(1)
    
    # Ship to address (capture multiple lines)
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
    
    # Invoice address
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
    
    # Try to detect Ford order format for items
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Ford order format pattern:
        # "873 M-1225-B 8.8" AXLE BEARING AND SEAL KIT (PAIR) 1 12.5500 USD 10,956.15 USD"
        # Pattern: quantity code description unit_pack unit_price total_price
        
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
