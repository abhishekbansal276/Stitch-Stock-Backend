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

    def extract_from_file(self, content: bytes, filename: str, existing_headers: List[str] = None) -> Dict:
        if not self.model:
            return self._mock_extract(filename)

        # ELITE SCHEMA: User's strictly requested 17 fields
        elite_fields = [
            "Date", "Invoice Number", "Supplier Name", "Supplier GST", 
            "Product Code", "Product Name", "Batch Number", "Quantity Received", 
            "Unit", "Rate per Unit", "Total Amount", "Transport / Freight", 
            "Taxes (IGST/CGST/SGST)", "Final Amount", "Vehicle Number", 
            "Transporter Name", "Remarks"
        ]

        # Build schema context
        schema_context = f"\nPREFERRED DATABASE COLUMNS: {', '.join(elite_fields)}"
        if existing_headers:
            schema_context += f"\nEXISTING SHEET COLUMNS: {', '.join(existing_headers)}\nPLEASE MAP YOUR FINDINGS TO THESE EXACT NAMES."

        # Create the multimodal prompt for Elite Standardized Extraction
        prompt = f"""
        Act as an Elite Inventory Digitizer. Analyze this document (Invoice, Bill, or Gate Pass) 
        and extract EVERY piece of information into a strictly valid JSON format.
        {schema_context}
        
        RULES:
        1. Prioritize these keys: {', '.join(elite_fields)}.
        2. SMART MAPPING: If a bill says "Freight" or "Extra Charges", map it to "Transport / Freight". 
        3. SMART MAPPING: If a bill says "GST Amount", map it to "Taxes (IGST/CGST/SGST)".
        4. "Batch Number" is critical. Look for Lot No, Batch, or Date-based codes.
        5. Divide the data into "header" (unique fields like Supplier, Invoice, Vehicle) and "items" (list of products).
        6. Return ONLY the JSON object. No markdown.
        
        JSON STRUCTURE:
        {{
          "header": {{ "Key Name": "Value", ... }},
          "items": [ {{ "Product Name": "...", "Quantity Received": 0.0, "Batch Number": "...", ... }}, ... ]
        }}
        """

        try:
            ext = filename.split('.')[-1].lower()
            mime_type = "image/jpeg"
            if ext == 'png': mime_type = "image/png"
            elif ext == 'pdf': mime_type = "application/pdf"

            response = self.model.generate_content([
                prompt,
                {"mime_type": mime_type, "data": content}
            ])
            
            text = response.text.strip()
            if text.startswith("```json"): text = text[7:-3]
            elif text.startswith("```"): text = text[3:-3]
            
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
                'Supplier Name': 'Standard Mock Corp', 
                'Invoice Number': 'INV-MOCK-99', 
                'Date': '2024-04-04', 
                'Vehicle Number': 'MH-01-AB-1234'
            },
            'items': [
                {
                    'Product Name': 'Premium Thread Spool', 
                    'Product Code': 'TH-BLUE-01', 
                    'Batch Number': 'BATCH-2024-A',
                    'Quantity Received': 500.0, 
                    'Unit': 'Spools', 
                    'Rate per Unit': 12.0, 
                    'Total Amount': 6000.0
                }
            ]
        }

ocr_service = OCRService()
