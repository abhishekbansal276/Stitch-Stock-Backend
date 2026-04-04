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
        schema_context = f"\nREQUIRED COLUMNS: {', '.join(elite_fields)}"

        # MECHANICAL PRECISION PROMPT (V10)
        prompt = f"""
        Act as a Professional Inventory Auditor. Analyze the provided image (Invoice/Bill) with 100% mechanical precision.
        
        {schema_context}
        
        STRICT EXTRACTION RULES:
        1. TABLE EXTRACTION: Trace every row in the product table. Do NOT skip any rows.
        2. QUANTITY (TO, bags): This is critical. If 'Bags' and 'Metric Tons (MT/TO)' are both present, format 'Quantity Received' as: '[Tons Value] MT ([Bags Value] Bags)'.
        3. BATCH NUMBER: Look specifically for the 'BATCH NO.' column in the product table. Capture it per row.
        4. VEHICLE/TRANSPORTER: Look for 'Vehicle Regn. No.' and 'Transporter Name' in the transport/mode of transport section.
        5. TAXES/FREIGHT: Look at the footer summary. Map 'FREIGHT' to 'Transport / Freight'. Map 'IGST/CGST/SGST' to 'Taxes (IGST/CGST/SGST)'.
        6. SMART MAPPING: 
           - 'Serial Number' or 'UPG...' -> 'Invoice Number'
           - 'Description of Goods' -> 'Product Name'
           - 'Price Rs./UOM' -> 'Rate per Unit'
        7. OUTPUT: Strictly valid JSON. Header fields for unique data, Items list for product rows.
        
        JSON STRUCTURE:
        {{
          "header": {{ "Date": "...", "Invoice Number": "...", "Supplier Name": "...", "Vehicle Number": "...", ... }},
          "items": [ 
            {{ 
              "Product Code": "...", "Product Name": "...", "Batch Number": "...", 
              "Quantity Received": "...", "Unit": "MT", "Rate per Unit": 0.0, 
              "Taxes (IGST/CGST/SGST)": "...", "Final Amount": 0.0 
            }}, ... 
          ]
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
            print(f"Extraction Error: {str(e)}")
            return self._mock_extract(filename)

    def _mock_extract(self, filename: str):
        return {
            'header': {
                'Supplier Name': 'GAIL (India) Limited Mock', 
                'Invoice Number': 'UPG3A25212061228', 
                'Date': '2026-03-08', 
                'Vehicle Number': 'GJ05CW8825'
            },
            'items': [
                {
                    'Product Name': 'G-LEX HDPE-1', 
                    'Product Code': 'B63A003A', 
                    'Batch Number': '26021097',
                    'Quantity Received': '7.675 MT (307 Bags)', 
                    'Unit': 'MT', 
                    'Rate per Unit': 125120.0, 
                    'Total Amount': 960296.0
                }
            ]
        }

ocr_service = OCRService()
