import os
import json
import re
import base64
from typing import Dict, List
import google.generativeai as genai

class OCRService:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        if self.api_key:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel('gemini-1.5-flash')
        else:
            print("WARNING: GEMINI_API_KEY not found. OCRService running in MOCK mode.")
            self.model = None

    def extract_from_file(self, content: bytes, filename: str) -> Dict:
        if not self.model:
            return self._mock_extract(filename)

        # Create the multimodal prompt
        prompt = """
        Analyze this invoice/bill image and extract the following fields into a PRECISE JSON format.
        
        {
          "header": {
            "supplier_name": "String",
            "document_no": "String",
            "document_date": "String (YYYY-MM-DD)",
            "vehicle_no": "String (if present, else empty)"
          },
          "items": [
            {
              "item_name": "String (Description)",
              "product_code": "String (Extract Code/SKU if present, else create a short slug from name)",
              "quantity_total": "Number",
              "unit": "String (Kg/Meter/Pc)",
              "rate": "Number",
              "amount": "Number"
            }
          ]
        }
        
        Return ONLY the JSON object. No markdown, no triple backticks, no explanations.
        Handle multiple items if present. If values are missing, use empty strings or 0.
        CRITICAL: Search thoroughly for all line items in the invoice. If no items are found, MUST create one item with empty fields.
        """

        try:
            # Prepare image for Gemini
            # Detecting mime type based on extension (simple check)
            ext = filename.split('.')[-1].lower()
            mime_type = "image/jpeg"
            if ext == 'png': mime_type = "image/png"
            elif ext == 'pdf': mime_type = "application/pdf"

            response = self.model.generate_content([
                prompt,
                {
                    "mime_type": mime_type,
                    "data": content
                }
            ])
            
            # Clean response text
            text = response.text.strip()
            # Remove markdown backticks if Gemini added them despite my prompt lol
            if text.startswith("```json"):
                text = text[7:-3]
            elif text.startswith("```"):
                text = text[3:-3]
            
            # Find the first { and last } to be safe
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end != -1:
                text = text[start:end+1]

            return json.loads(text.strip())

        except Exception as e:
            print(f"Gemini Extraction Error: {str(e)}")
            # Fallback to mock if it fails during deployment testing
            return self._mock_extract(filename)

    def _mock_extract(self, filename: str):
        return {
            'header': {
                'supplier_name': 'Sample Supplier Ltd', 
                'document_no': 'INV-999', 
                'document_date': '2024-04-04', 
                'vehicle_no': 'N/A'
            },
            'items': [
                {
                    'item_name': 'Digitally Scanned Fabric', 
                    'product_code': 'FAB-001', 
                    'quantity_total': 100.0, 
                    'unit': 'Meters', 
                    'rate': 55.0, 
                    'amount': 5500.0
                }
            ]
        }

ocr_service = OCRService()
