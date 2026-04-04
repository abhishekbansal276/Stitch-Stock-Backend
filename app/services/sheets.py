import os
import time
import uuid
import base64
import json
from typing import List, Dict
from google.oauth2 import service_account
from googleapiclient.discovery import build
from app.services.firebase import db

class SheetsService:
    # ELITE SCHEMA DEFINITION (22 Columns)
    BASE_SCHEMA = [
        "Barcode ID", "Created At", "Created By", "Updated At", "Updated By",
        "Date", "Invoice Number", "Supplier Name", "Supplier GST", 
        "Product Code", "Product Name", "Batch Number", "Quantity Received", 
        "Unit", "Rate per Unit", "Total Amount", "Transport / Freight", 
        "Taxes (IGST/CGST/SGST)", "Final Amount", "Vehicle Number", 
        "Transporter Name", "Remarks"
    ]

    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.scopes = ['https://www.googleapis.com/auth/spreadsheets']
        self.service = self._initialize_service()
        self.header_map = {name: i for i, name in enumerate(self.BASE_SCHEMA)}

    def _initialize_service(self):
        b64_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if b64_key:
            try:
                decoded_key = base64.b64decode(b64_key).decode('utf-8')
                cred_dict = json.loads(decoded_key)
                creds = service_account.Credentials.from_service_account_info(cred_dict, scopes=self.scopes)
                return build('sheets', 'v4', credentials=creds)
            except Exception as e:
                print(f"Error loading Base64 Sheets key: {e}")

        service_account_file = os.getenv("SERVICE_ACCOUNT_FILE", "serviceAccountKey.json")
        if os.path.exists(service_account_file):
            creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=self.scopes)
            return build('sheets', 'v4', credentials=creds)
        
        print("WARNING: SheetsService running in MOCK mode.")
        return None

    def _get_or_create_headers(self) -> Dict:
        """Initializes the Sheet with the Elite Professional Schema (22 Columns)."""
        if not self.service: return {}
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Register!1:1'
            ).execute()
            values = result.get('values', [])
            
            if not values or len(values[0]) == 0:
                # Write the 22 Elite Headers
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range='Stock Register!1:1',
                    valueInputOption='RAW', body={'values': [self.BASE_SCHEMA]}
                ).execute()
                self._apply_professional_styles(len(self.BASE_SCHEMA))
            
            return {name: i for i, name in enumerate(self.BASE_SCHEMA)}
        except Exception as e:
            print(f"Header Init Error: {e}")
            return self.header_map

    def _apply_professional_styles(self, column_count: int):
        try:
            sheet_metadata = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
            sheet_id = 0
            for s in sheet_metadata.get('sheets', []):
                if s['properties']['title'] == 'Stock Register':
                    sheet_id = s['properties']['sheetId']
                    break

            requests = [
                # 1. Indigo Header
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 129/255, "green": 140/255, "blue": 248/255},
                                "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True},
                                "horizontalAlignment": "CENTER"
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
                    }
                },
                {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
                # 2. Zebra Striping
                {
                    "addConditionalFormatRule": {
                      "rule": {
                        "ranges": [{"sheetId": sheet_id, "startRowIndex": 1}],
                        "booleanRule": {
                          "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=ISEVEN(ROW())"}]},
                          "format": {"backgroundColor": {"red": 0.95, "green": 0.96, "blue": 1.0}}
                        }
                      },
                      "index": 0
                    }
                }
            ]
            self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={'requests': requests}).execute()
        except: pass

    def save_stock(self, header: Dict, items: List[Dict], user_email: str) -> List[str]:
        if not self.service:
            return [f"MOCK-{uuid.uuid4().hex[:6]}" for _ in items]

        self.header_map = self._get_or_create_headers()
        new_ids = []
        now = time.strftime('%Y-%m-%d %H:%M:%S')

        for item in items:
            item_id = f"STK-{int(time.time())}-{uuid.uuid4().hex[:4].upper()}"
            row_data = [""] * len(self.BASE_SCHEMA)
            
            # Fill Fixed / Audit Columns
            row_data[self.header_map["Barcode ID"]] = item_id
            row_data[self.header_map["Created At"]] = now
            row_data[self.header_map["Created By"]] = user_email
            
            # Fill Elite 17 Columns
            # We check both the 'header' data and the 'item' data for these fields
            # Since some like 'Batch Number' are item-level, others like 'Supplier' are header-level.
            for key in self.BASE_SCHEMA[5:]: # Skip audit columns
                val = header.get(key) or item.get(key)
                if val:
                    idx = self.header_map[key]
                    row_data[idx] = val

            # MERGE LOGIC (Anchor: Product Code)
            p_code_idx = self.header_map["Product Code"]
            p_code = item.get("Product Code", "N/A")
            
            existing_row_idx = self._find_row_by_col(p_code_idx, p_code)

            if existing_row_idx != -1:
                item_id = self._merge_into_row_elite(existing_row_idx, item, user_email)
            else:
                self._append_row('Stock Register', row_data)
            
            # Record Movement (Include Batch Number for legacy traceability)
            batch = item.get('Batch Number', 'N/A')
            trans_id = header.get('Invoice Number', 'TRANS-NEW')
            self.add_movement(item_id, f"{trans_id} (Batch: {batch})", 'IN', float(item.get('Quantity Received', 0)), user_email)
            new_ids.append(item_id)

        return new_ids

    def _merge_into_row_elite(self, row_idx: int, new_item: Dict, user_email: str) -> str:
        """Standardized Elite Merge: Sum Quantity + Update Audit Fields"""
        try:
            qty_idx = self.header_map["Quantity Received"]
            upd_at_idx = self.header_map["Updated At"]
            upd_by_idx = self.header_map["Updated By"]
            
            # 1. Fetch current quantity
            col_letter = chr(65 + qty_idx)
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}{row_idx}'
            ).execute()
            old_qty = float(result.get('values', [[0]])[0][0])
            
            new_qty = old_qty + float(new_item.get('Quantity Received', 0))
            now = time.strftime('%Y-%m-%d %H:%M:%S')

            # 2. Update Quantity + Updated At + Updated By
            # Note: We use individual updates to be safer with column mapping
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{chr(65+qty_idx)}{row_idx}',
                valueInputOption='USER_ENTERED', body={'values': [[new_qty]]}
            ).execute()
            
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{chr(65+upd_at_idx)}{row_idx}:{chr(65+upd_by_idx)}{row_idx}',
                valueInputOption='USER_ENTERED', body={'values': [[now, user_email]]}
            ).execute()
            
            # 3. Get item_id from the first column
            res_id = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!A{row_idx}'
            ).execute().get('values', [['UNKNOWN']])[0][0]
            
            return res_id
        except: return "ERROR-MERGE"

    def _find_row_by_col(self, col_idx: int, value: str) -> int:
        if not self.service: return -1
        col_letter = chr(65 + col_idx) if col_idx < 26 else "Z"
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}:{col_letter}'
            ).execute()
            values = result.get('values', [])
            for i, row in enumerate(values):
                if row and str(row[0]) == str(value):
                    return i + 1
            return -1
        except: return -1

    def _append_row(self, sheet_name: str, row_data: List):
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id, range=f'{sheet_name}!A:A',
                valueInputOption='USER_ENTERED', body={'values': [row_data]}
            ).execute()
        except: pass

    def add_movement(self, stock_item_id: str, trans_id: str, type: str, qty: float, user_email: str):
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        if self.service:
            row = [str(uuid.uuid4())[:8].upper(), stock_item_id, trans_id, type, qty, user_email, now_str]
            self._append_row('Stock Movements', row)
        try:
            db.collection('activity_logs').add({
                'stock_item_id': stock_item_id, 'transaction_id': trans_id,
                'movement_type': type, 'quantity_changed': qty,
                'actor_email': user_email, 'created_at': int(time.time()), 'item_name': 'Audit Move'
            })
        except: pass

    def get_stock_item(self, item_id: str) -> Dict:
        if not self.service: return {}
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Register!A:V'
            ).execute().get('values', [])
            for row in result:
                if row and row[0] == item_id:
                    return {
                        "stock_item_id": row[0], "item_name": row[10], # Product Name
                        "quantity_remaining": float(row[12]), # Quantity Received
                        "unit": row[13], "supplier_name": row[7], "product_code": row[9]
                    }
        except: pass
        return {}

    def get_current_headers(self) -> List[str]:
        return self.BASE_SCHEMA

sheets_service = SheetsService()
