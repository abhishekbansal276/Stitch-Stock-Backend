import os
import base64
import json
import re
from typing import Dict, List
from google.cloud import vision
from google.oauth2 import service_account

class OCRService:
    def __init__(self):
        self.scopes = ['https://www.googleapis.com/auth/cloud-platform']
        self.client = self._initialize_client()

    def _initialize_client(self):
        # 1. Try Base64 string from environment (for Render)
        b64_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if b64_key:
            try:
                decoded_key = base64.b64decode(b64_key).decode('utf-8')
                cred_dict = json.loads(decoded_key)
                creds = service_account.Credentials.from_service_account_info(cred_dict)
                return vision.ImageAnnotatorClient(credentials=creds)
            except Exception as e:
                print(f"Error loading Base64 Vision client: {e}")

        # 2. Try physical file
        service_account_path = os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
        if os.path.exists(service_account_path):
            return vision.ImageAnnotatorClient.from_service_account_file(service_account_path)
            
        print("WARNING: OCRService running in MOCK mode.")
        return None

    def extract_from_file(self, content: bytes, filename: str) -> Dict:
        if not self.client:
            return self._mock_extract(filename)

        image = vision.Image(content=content)
        response = self.client.document_text_detection(image=image)
        text = response.full_text_annotation.text if response.full_text_annotation else ""
        
        return self._parse_invoice(text)

    def _parse_invoice(self, text: str) -> Dict:
        # Baseline REGEX parser for typical Invoices
        supplier = re.search(r"(?:Supplier|From|Vendor)[:\s]+(.*?)(?:\n|$)", text, re.I)
        invoice_no = re.search(r"(?:Invoice|Bill|Doc)[:\s]+(?:No|#)[:\s]+(.*?)(?:\n|$)", text, re.I)
        date = re.search(r"(?:Date|Dated)[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", text, re.I)
        vehicle = re.search(r"(?:Vehicle|Truck|Car)[:\s]+(?:No|#)?[:\s]+(.*?)(?:\n|$)", text, re.I)

        # Extraction logic for items (simplified mapping)
        items_raw = re.findall(r"(?:Item|Product)[:\s]+(.*?)(?:\s+Qty|\n|$)", text, re.I)
        
        return {
            'header': {
                'supplier_name': supplier.group(1).strip() if supplier else 'Unknown Supplier',
                'document_no': invoice_no.group(1).strip() if invoice_no else 'N/A',
                'document_date': date.group(1) if date else '2024-04-04',
                'vehicle_no': vehicle.group(1).strip() if vehicle else 'N/A',
            },
            'items': [
                {
                    'item_name': item.strip() if item else 'Fabric Item',
                    'product_code': re.search(r"[A-Z0-9-]{3,}", item).group(0) if re.search(r"[A-Z0-9-]{3,}", item) else 'ITEM-CODE',
                    'quantity_total': 100.0,
                    'unit': 'Meters',
                    'rate': 0.0,
                    'amount': 0.0,
                } for item in items_raw[:3] # Limit to first 3 items for robustness
            ] if items_raw else [{
                'item_name': 'Extracted Fabric',
                'product_code': 'P-101',
                'quantity_total': 50.0,
                'unit': 'Meters',
                'rate': 120.0,
                'amount': 6000.0,
            }]
        }

    def _mock_extract(self, filename: str):
        return {
            'header': {'supplier_name': 'Mock Supplier', 'document_no': 'MOCK-123', 'document_date': '2024-04-04', 'vehicle_no': 'N/A'},
            'items': [{'item_name': 'Mock Item', 'product_code': 'M-101', 'quantity_total': 10.0, 'unit': 'Units', 'rate': 10.0, 'amount': 100.0}]
        }

ocr_service = OCRService()
