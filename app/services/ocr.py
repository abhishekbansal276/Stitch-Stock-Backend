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

        # Create the multimodal prompt for Dynamic Extraction
        prompt = """
        Act as an Elite Inventory Digitizer. Analyze this document (Invoice, Bill, or Gate Pass) 
        and extract EVERY piece of information into a strictly valid JSON format.
        
        RULES:
        1. Use "Professional Title Case" for all keys (e.g., "Supplier Name", "Bill Number", "Date").
        2. Divide the data into "header" (unique fields) and "items" (list of table rows).
        3. Extract fields like: Supplier, Invoice No, Vehicle No, Date, Total Amount, etc.
        4. For items, extract: Item Name, Product Code (SKU), Quantity, Unit, Rate, Amount.
        5. If a field name is not standard, use the most professional equivalent.
        6. Return ONLY the JSON object. No markdown, no preambles.
        
        JSON STRUCTURE:
        {
          "header": { "Key Name": "Value", ... },
          "items": [ { "Item Name": "...", "Quantity": 0.0, ... }, ... ]
        }
        
        CRITICAL: Be thorough. Don't skip any fields visible on the page.
        """

        try:
            # Prepare image for Gemini
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
            
            # Clean and sanitize response text
            text = response.text.strip()
            
            # Remove potential markdown wraps
            if text.startswith("```json"): text = text[7:-3]
            elif text.startswith("```"): text = text[3:-3]
            
            # Find JSON bounds
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end != -1:
                text = text[start:end+1]

            return json.loads(text.strip())

        except Exception as e:
            print(f"Gemini Extraction Error: {str(e)}")
            return self._mock_extract(filename)

    def _mock_extract(self, filename: str):
        return {
            'header': {
                'Supplier Name': 'Mock Fabric Supplier', 
                'Bill Number': 'MOCK-101', 
                'Date': '2024-04-04', 
                'Vehicle ID': 'N/A'
            },
            'items': [
                {
                    'Item Name': 'Elite Raw Fabric', 
                    'Product Code': 'FAB-MOCK', 
                    'Quantity': 150.0, 
                    'Unit': 'Meters', 
                    'Rate': 85.0, 
                    'Amount': 12750.0
                }
            ]
        }

ocr_service = OCRService()
