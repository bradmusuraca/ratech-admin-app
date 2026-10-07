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
                credentials_dict = json.loads(credentials_json)
                from google.oauth2 import service_account
                credentials = service_account.Credentials.from_service_account_info(credentials_dict)
                self.client = vision.ImageAnnotatorClient(credentials=credentials)
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
    This is a placeholder - actual parsing logic will depend on your order format
    
    Args:
        extracted_text: Raw text extracted from PDF
        
    Returns:
        List of dictionaries representing order items
    """
    # This is a basic placeholder parser
    # You'll need to customize this based on your actual order format
    
    items = []
    lines = extracted_text.split('\n')
    
    # Example parsing logic - customize based on your order format
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Look for patterns like: "Item Name - $10.00" or "Item: 5 x $20.00"
        # This is just an example - adjust regex patterns to match your format
        if '$' in line:
            try:
                parts = line.split('$')
                if len(parts) >= 2:
                    item_name = parts[0].strip()
                    price = float(parts[1].strip())
                    
                    items.append({
                        'Item': item_name,
                        'Quantity': 1,  # Default to 1 if not specified
                        'Price': price,
                        'Total': price
                    })
            except (ValueError, IndexError):
                continue
    
    return items
