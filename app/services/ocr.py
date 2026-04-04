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

        # ELITE SCHEMA: User's strictly requested fields
        elite_fields = [
            "Date", "Invoice Number", "Supplier Name", "Supplier GST", 
            "Product Code", "Product Name", "Batch Number", "Quantity Received", 
            "Unit", "Number of Bags", "Rate per Unit", "Total Amount", "Transport / Freight", 
            "Taxes (IGST/CGST/SGST)", "Final Amount", "Vehicle Number", 
            "Transporter Name", "Remarks"
        ]

        # Build schema context
        schema_context = f"\nREQUIRED COLUMNS: {', '.join(elite_fields)}"

        # MECHANICAL PRECISION PROMPT (V11)
        prompt = f"""
        Act as a Professional Inventory Auditor. Analyze the provided image (Invoice/Bill) with 100% mechanical precision.
        {schema_context}
        
        STRICT EXTRACTION RULES:
        1. LANDMARK (SUPPLIER GST): Look for the 'SUPPLIER' box at the top. Inside that box, find the row labeled 'GST NO.' and extract that value as 'Supplier GST'.
        2. LANDMARK (TRANSPORT): Look for the 'MODE OF TRANSPORT' section. Extract 'TRANSPORTER NAME' and 'VEHICLE REGN. NO.'.
        3. LANDMARK (FREIGHT): Look at the table footer summary. Map the 'FREIGHT' amount to 'Transport / Freight'.
        4. TABLE EXTRACTION: Trace every row in the product table. Do NOT skip any rows.
        5. BAGS: Find the 'NO. OF BAGS' column. Extract it into 'Number of Bags' for each item.
        6. QUANTITY: For 'Quantity Received', use the Metric Tons (TO/MT) value. 
        7. SMART MAPPING: 
           - 'UOM' or 'UOM*' -> 'Unit'
           - 'Serial Number' or 'UPG...' -> 'Invoice Number'
           - 'Description' -> 'Product Name'
           - 'Price/UOM', 'Rate' -> 'Rate per Unit'
           - 'Invoice Value', 'Grand Total', 'Total (Rounded)' -> 'Final Amount'
        8. TAXES (EXHAUSTIVE): Search for ANY tax row (IGST, CGST, SGST, UTGST, Cess). Sum them into 'Taxes (IGST/CGST/SGST)'.
        9. OUTPUT: Strictly valid JSON. Header for unique fields, Items for product list.
        
        JSON STRUCTURE:
        {{
          "header": {{ "Date": "...", "Invoice Number": "...", "Supplier Name": "...", "Supplier GST": "...", "Vehicle Number": "...", "Transporter Name": "...", "Transport / Freight": 0.0 }},
          "items": [ 
            {{ 
              "Product Code": "...", "Product Name": "...", "Batch Number": "...", 
              "Quantity Received": "...", "Unit": "MT", "Number of Bags": "...", "Rate per Unit": 0.0, 
              "Total Amount": 0.0, "Taxes (IGST/CGST/SGST)": "...", "Final Amount": 0.0 
            }} 
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

            data = json.loads(text.strip())
            
            # CONSOLIDATION (v11): Sum items with same Product Code
            items = data.get('items', [])
            consolidated = {}
            for item in items:
                p_code = str(item.get('Product Code', 'UNKNOWN')).strip()
                if p_code in consolidated:
                    base = consolidated[p_code]
                    base['Quantity Received'] = self._sum_strings(base.get('Quantity Received'), item.get('Quantity Received'))
                    base['Number of Bags'] = self._sum_strings(base.get('Number of Bags'), item.get('Number of Bags'))
                    base['Total Amount'] = self._safe_float(base.get('Total Amount')) + self._safe_float(item.get('Total Amount'))
                    base['Final Amount'] = self._safe_float(base.get('Final Amount')) + self._safe_float(item.get('Final Amount'))
                    base['Taxes (IGST/CGST/SGST)'] = self._sum_strings(base.get('Taxes (IGST/CGST/SGST)'), item.get('Taxes (IGST/CGST/SGST)'))
                    
                    # Unit Persistence: Take the strongest non-empty unit
                    if not base.get('Unit') or base.get('Unit') == 'PCS':
                        base['Unit'] = item.get('Unit')
                    
                    if item.get('Batch Number') and item.get('Batch Number') not in base['Batch Number']:
                        base['Batch Number'] = f"{base['Batch Number']}, {item.get('Batch Number')}"
                else:
                    consolidated[p_code] = item
            
            data['items'] = list(consolidated.values())
            return data

        except Exception as e:
            print(f"Extraction Error: {str(e)}")
            return self._mock_extract(filename)

    def _safe_float(self, val) -> float:
        if not val: return 0.0
        try:
            matches = re.findall(r"[-+]?\d*\.\d+|\d+", str(val))
            return float(matches[0]) if matches else 0.0
        except: return 0.0

    def _sum_strings(self, val1, val2) -> str:
        f1 = self._safe_float(val1)
        f2 = self._safe_float(val2)
        unit = ""
        if isinstance(val1, str):
            unit_match = re.search(r'[a-zA-Z]+', val1)
            if unit_match: unit = f" {unit_match.group()}"
        return f"{f1 + f2}{unit}".strip()

    def _mock_extract(self, filename: str):
        return {
            'header': {
                'Supplier Name': 'GAIL MOCK', 
                'Invoice Number': 'UPG3A...', 
                'Supplier GST': '09AAACG1209J3ZS',
                'Date': '2026-03-08', 
                'Vehicle Number': 'GJ05CW8825',
                'Transporter Name': 'RITCO LOGISTICS'
            },
            'items': [
                {
                    'Product Name': 'G-LEX HDPE-1', 
                    'Product Code': 'B63A003A', 
                    'Batch Number': '26021097, 26031098',
                    'Quantity Received': '12.0 MT', 
                    'Number of Bags': '480',
                    'Unit': 'MT', 
                    'Rate per Unit': 125120.0, 
                    'Total Amount': 1501440.0
                }
            ]
        }

ocr_service = OCRService()
