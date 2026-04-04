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
    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.scopes = ['https://www.googleapis.com/auth/spreadsheets']
        self.service = self._initialize_service()
        self.header_map = None # { "Header Name": ColumnIndex }

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

    def _get_or_create_headers(self, sample_header: Dict, sample_items: List[Dict]) -> Dict:
        """
        Reads existing headers or creates them based on the first bill analysis.
        Locks the schema once created.
        """
        if not self.service: return {}
        
        try:
            # 1. Fetch Row 1
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Register!1:1'
            ).execute()
            values = result.get('values', [])
            
            if values and len(values[0]) > 0:
                # Headers exist, build map
                return {name: i for i, name in enumerate(values[0])}
            
            # 2. If Row 1 is empty, define new Professional Headers
            # We start with fixed anchor columns A-C
            new_headers = ["Barcode ID", "Entry Timestamp", "Actor Email"]
            
            # Add dynamic header fields from AI
            for key in sample_header.keys():
                if key not in new_headers:
                    new_headers.append(key)
            
            # Add dynamic item fields (collapsed into the same row for 1:N items mapping)
            # Standard item keys to ensure we can scan them
            standard_item_keys = ["Product Code", "Item Name", "Quantity Total", "Quantity Remaining", "Unit", "Rate", "Amount"]
            for key in standard_item_keys:
                if key not in new_headers:
                    new_headers.append(key)
            
            # Add any other fields discovered in the first item
            if sample_items:
                for key in sample_items[0].keys():
                    if key not in new_headers:
                        new_headers.append(key)

            # 3. Write Headers to Sheet
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range='Stock Register!1:1',
                valueInputOption='RAW',
                body={'values': [new_headers]}
            ).execute()
            
            # 4. Apply Elite Styling
            self._apply_professional_styles(len(new_headers))
            
            return {name: i for i, name in enumerate(new_headers)}

        except Exception as e:
            print(f"Schema Initialization Error: {e}")
            return {}

    def _apply_professional_styles(self, column_count: int):
        """Applies Indigo Headers and Zebra Striping to the Sheet."""
        try:
            # Get sheet ID
            sheet_metadata = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
            sheet_id = 0 # Assuming 'Stock Register' is the first sheet
            for s in sheet_metadata.get('sheets', []):
                if s['properties']['title'] == 'Stock Register':
                    sheet_id = s['properties']['sheetId']
                    break

            requests = [
                # 1. Format Header (Row 1)
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 129/255, "green": 140/255, "blue": 248/255}, # Neon Indigo #818CF8
                                "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True, "fontSize": 10},
                                "horizontalAlignment": "CENTER"
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
                    }
                },
                # 2. Freeze Row 1
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                        "fields": "gridProperties.frozenRowCount"
                    }
                },
                # 3. Zebra Striping (Alternating colors)
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
        except Exception as e:
            print(f"Styling Error: {e}")

    def save_stock(self, header: Dict, items: List[Dict], user_email: str) -> List[str]:
        if not self.service:
            return [f"MOCK-{uuid.uuid4().hex[:6]}" for _ in items]

        # Sync schema and get mapping
        self.header_map = self._get_or_create_headers(header, items)
        if not self.header_map:
            raise Exception("Failed to initialize Sheet Schema.")

        new_ids = []
        now = time.strftime('%Y-%m-%d %H:%M:%S')

        for item in items:
            # Anchor field for barcode / ID
            item_id = f"STK-{int(time.time())}-{uuid.uuid4().hex[:4].upper()}"
            
            # Map data to dynamic columns
            row_data = [""] * (max(self.header_map.values()) + 1)
            
            # Fill Fixed Pillars
            row_data[self.header_map.get("Barcode ID", 0)] = item_id
            row_data[self.header_map.get("Entry Timestamp", 1)] = now
            row_data[self.header_map.get("Actor Email", 2)] = user_email
            
            # Fill Dynamic Header Fields
            for k, v in header.items():
                if k in self.header_map:
                    row_data[self.header_map[k]] = v
            
            # Fill Item Fields
            for k, v in item.items():
                # Remap common keys to professional names if needed
                prof_key = k
                if k == 'quantity_remaining': prof_key = 'Quantity Remaining'
                if k == 'quantity_total': prof_key = 'Quantity Total'
                
                if prof_key in self.header_map:
                    row_data[self.header_map[prof_key]] = v
                elif k in self.header_map:
                    row_data[self.header_map[k]] = v

            # Check for existing product (Simple check on 'Product Code' column if it exists)
            p_code_idx = self.header_map.get("Product Code", -1)
            p_code = item.get("Product Code", item.get("product_code", "N/A"))
            
            existing_row_idx = -1
            if p_code_idx != -1:
                existing_row_idx = self._find_row_by_col(p_code_idx, p_code)

            if existing_row_idx != -1:
                # Merge logic
                item_id = self._merge_into_row_dynamic(existing_row_idx, item, user_email)
            else:
                # Append new row
                self._append_row('Stock Register', row_data)
            
            # Record Movement
            trans_id = header.get('Bill Number', header.get('document_no', 'TRANS-NEW'))
            self.add_movement(item_id, trans_id, 'IN', float(item.get('Quantity Total', item.get('quantity_total', 0))), user_email)
            new_ids.append(item_id)

        return new_ids

    def _find_row_by_col(self, col_idx: int, value: str) -> int:
        col_letter = chr(65 + col_idx) if col_idx < 26 else "A" # Simplified for MVP
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

    def _merge_into_row_dynamic(self, row_idx: int, new_item: Dict, user_email: str) -> str:
        """Dynamically merges quantities in an existing row."""
        try:
            # Need indices for Quantity Total (H->8) and Remaining (I->9)
            # In dynamic schema these might move, but for standard we stick to names
            tot_idx = self.header_map.get("Quantity Total", 7)
            rem_idx = self.header_map.get("Quantity Remaining", 8)
            
            # Get current values
            curr_vals = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!A{row_idx}:Z{row_idx}'
            ).execute().get('values', [[]])[0]
            
            item_id = curr_vals[self.header_map["Barcode ID"]]
            new_total = float(curr_vals[tot_idx]) + float(new_item.get('Quantity Total', 0))
            new_rem = float(curr_vals[rem_idx]) + float(new_item.get('Quantity Total', 0))
            
            # Update
            ranges = [
                f'Stock Register!{chr(65+tot_idx)}{row_idx}',
                f'Stock Register!{chr(65+rem_idx)}{row_idx}'
            ]
            for i, r in enumerate(ranges):
                val = new_total if i == 0 else new_rem
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range=r,
                    valueInputOption='USER_ENTERED', body={'values': [[val]]}
                ).execute()
            return item_id
        except: return "ERROR"

    def _append_row(self, sheet_name: str, row_data: List):
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id, range=f'{sheet_name}!A:A',
                valueInputOption='USER_ENTERED', body={'values': [row_data]}
            ).execute()
        except Exception as e:
            print(f"Sheets Append Error: {e}")

    def add_movement(self, stock_item_id: str, trans_id: str, type: str, qty: float, user_email: str):
        now_ts = int(time.time())
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        if self.service:
            row = [str(uuid.uuid4())[:8].upper(), stock_item_id, trans_id, type, qty, user_email, now_str]
            self._append_row('Stock Movements', row)
        try:
            db.collection('activity_logs').add({
                'stock_item_id': stock_item_id, 'transaction_id': trans_id,
                'movement_type': type, 'quantity_changed': qty,
                'actor_email': user_email, 'created_at': now_ts, 'item_name': 'Stock Update'
            })
        except: pass

    def get_stock_item(self, item_id: str) -> Dict:
        """Fetch stock item from the dynamically mapped register (Anchor Barcode ID in Col A)."""
        if not self.service: return {}
        try:
            # 1. Fetch entire row
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Register!A:Z'
            ).execute().get('values', [])
            
            if not result: return {}
            
            # 2. Setup mapping if not cached
            if not self.header_map:
                self.header_map = {name: i for i, name in enumerate(result[0])}
            
            # 3. Search for item_id in Barcode ID column
            barcode_idx = self.header_map.get("Barcode ID", 0)
            for row in result:
                if row and row[barcode_idx] == item_id:
                    return {
                        "stock_item_id": row[self.header_map.get("Barcode ID", 0)],
                        "item_name": row[self.header_map.get("Item Name", 6)],
                        "quantity_remaining": float(row[self.header_map.get("Quantity Remaining", 8)]),
                        "unit": row[self.header_map.get("Unit", 9)],
                        "supplier_name": row[self.header_map.get("Supplier Name", 2)],
                        "product_code": row[self.header_map.get("Product Code", 5)],
                        "transaction_id": row[self.header_map.get("Bill Number", 3)]
                    }
        except Exception as e:
            print(f"Fetch Error: {e}")
        return {}

    def get_current_headers(self) -> List[str]:
        """Fetches the first row of 'Stock Register' to get existing column names."""
        if not self.service: return []
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Register!1:1'
            ).execute()
            values = result.get('values', [])
            return values[0] if values else []
        except: return []

sheets_service = SheetsService()
