import os
import time
import uuid
import base64
import json
from typing import List, Dict
from google.oauth2 import service_account
from googleapiclient.discovery import build

class SheetsService:
    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.scopes = ['https://www.googleapis.com/auth/spreadsheets']
        self.service = self._initialize_service()

    def _initialize_service(self):
        # 1. Try Base64 string from environment (for Render)
        b64_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if b64_key:
            try:
                decoded_key = base64.b64decode(b64_key).decode('utf-8')
                cred_dict = json.loads(decoded_key)
                creds = service_account.Credentials.from_service_account_info(cred_dict, scopes=self.scopes)
                return build('sheets', 'v4', credentials=creds)
            except Exception as e:
                print(f"Error loading Base64 Sheets key: {e}")

        # 2. Try physical file (for Local Development)
        service_account_file = os.getenv("SERVICE_ACCOUNT_FILE", "serviceAccountKey.json")
        if os.path.exists(service_account_file):
            creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=self.scopes)
            return build('sheets', 'v4', credentials=creds)
        
        print("WARNING: SheetsService running in MOCK mode.")
        return None

    def save_stock(self, header: Dict, items: List[Dict], user_email: str) -> List[str]:
        if not self.service:
            return [f"MOCK-{uuid.uuid4().hex[:6]}" for _ in items]

        new_ids = []
        now = time.strftime('%Y-%m-%d %H:%M:%S')

        for item in items:
            product_code = item.get('product_code', 'N/A')
            
            # 1. Check for existing product (Merging Logic)
            existing_row_index = self._find_product_row(product_code)
            
            if existing_row_index != -1:
                # Update existing row (Merge quantity)
                item_id = self._merge_into_row(existing_row_index, item, user_email)
            else:
                # Create NEW row
                item_id = f"STK-{int(time.time())}-{uuid.uuid4().hex[:4].upper()}"
                row_data = [
                    item_id, now, header.get('supplier_name'), header.get('document_no'),
                    header.get('document_date'), product_code, item.get('item_name'),
                    item.get('quantity_total'), item.get('quantity_total'), # quantity_remaining same as total initially
                    item.get('unit'), 'ACTIVE', user_email
                ]
                self._append_row('Stock Register', row_data)
            
            # 2. Record Movement (IN)
            trans_id = header.get('document_no', 'TRANS-NEW')
            self.add_movement(item_id, trans_id, 'IN', float(item.get('quantity_total', 0)), user_email)
            new_ids.append(item_id)

        return new_ids

    def _find_product_row(self, product_code: str) -> int:
        """Helper to find the row number (1-indexed) of a product in 'Stock Register'."""
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Register!F:F' # Column F is product_code
            ).execute()
            values = result.get('values', [])
            for i, row in enumerate(values):
                if row and row[0] == product_code:
                    return i + 1
            return -1
        except: return -1

    def _merge_into_row(self, row_idx: int, item: Dict, user_email: str) -> str:
        """Updates quantity_total and quantity_remaining in existing row."""
        try:
            # Get current values to add to them
            curr_vals = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!A{row_idx}:L{row_idx}'
            ).execute().get('values', [[]])[0]
            
            item_id = curr_vals[0]
            new_total = float(curr_vals[7]) + float(item['quantity_total'])
            new_rem = float(curr_vals[8]) + float(item['quantity_total'])
            
            # Update columns H and I (Quantity Total and Remaining)
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f'Stock Register!H{row_idx}:I{row_idx}',
                valueInputOption='USER_ENTERED',
                body={'values': [[new_total, new_rem]]}
            ).execute()
            return item_id
        except: return "ERROR"

    def _append_row(self, sheet_name: str, row_data: List):
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f'{sheet_name}!A:A',
                valueInputOption='RAW',
                body={'values': [row_data]}
            ).execute()
        except Exception as e:
            print(f"Sheets Append Error: {e}")

    def add_movement(self, stock_item_id: str, trans_id: str, type: str, qty: float, user_email: str):
        if not self.service: return
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        row = [str(uuid.uuid4())[:8].upper(), stock_item_id, trans_id, type, qty, user_email, now]
        self._append_row('Stock Movements', row)

    def get_stock_item(self, item_id: str) -> Dict:
        """Fetch a specific item from the register (for QR scanning)."""
        if not self.service: return {}
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Register!A:L'
            ).execute().get('values', [])
            for row in result:
                if row and row[0] == item_id:
                    return {
                        "stock_item_id": row[0], "item_name": row[6],
                        "quantity_remaining": float(row[8]), "unit": row[9],
                        "supplier_name": row[2], "product_code": row[5], "transaction_id": row[3]
                    }
        except: pass
        return {}

sheets_service = SheetsService()
