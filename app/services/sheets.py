import os
import time
import uuid
import base64
import json
import re
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
        "Unit", "Number of Bags", "Rate per Unit", "Total Amount", "Transport / Freight", 
        "Taxes (IGST/CGST/SGST)", "Final Amount", "Vehicle Number", 
        "Transporter Name", "Remarks"
    ]
    MOVEMENTS_SCHEMA = [
        "Movement ID", "Barcode ID", "Transaction ID", "Type", "Quantity", "Location", "User", "Timestamp"
    ]
    SUMMARY_SCHEMA = [
        "Product Code", "Product Name", "Total Received", "Total Dispatched", "Current Balance", "Unit"
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
        """Initializes all sheets for the Elite Stock Infrastructure."""
        if not self.service: return {}
        try:
            # 1. Main Stock Register
            self._ensure_sheet('Stock Register', self.BASE_SCHEMA)
            # 2. Stock Movements
            self._ensure_sheet('Stock Movements', self.MOVEMENTS_SCHEMA)
            # 3. Stock Summary
            self._ensure_sheet('Stock Summary', self.SUMMARY_SCHEMA)

            return {name: i for i, name in enumerate(self.BASE_SCHEMA)}
        except Exception as e:
            print(f"Header Init Error: {e}")
            return self.header_map

    def _ensure_sheet(self, title: str, schema: List[str]):
        """Detects if a sheet exists, if not creates it with standard styling."""
        try:
            # Check if sheet exists
            metadata = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
            sheets = {s['properties']['title']: s['properties']['sheetId'] for s in metadata.get('sheets', [])}
            
            if title not in sheets:
                body = {'requests': [{'addSheet': {'properties': {'title': title}}}]}
                res = self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute()
                sheet_id = res['replies'][0]['addSheet']['properties']['sheetId']
                
                # Write Headers
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range=f'{title}!1:1',
                    valueInputOption='RAW', body={'values': [schema]}
                ).execute()
                
                self._apply_elite_styles(sheet_id, len(schema), title)
            else:
                # Check for headers
                result = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=f'{title}!1:1'
                ).execute()
                if not result.get('values'):
                    self.service.spreadsheets().values().update(
                        spreadsheetId=self.spreadsheet_id, range=f'{title}!1:1',
                        valueInputOption='RAW', body={'values': [schema]}
                    ).execute()
        except Exception as e:
            print(f"Error ensuring sheet {title}: {e}")

    def _apply_elite_styles(self, sheet_id: int, column_count: int, title: str):
        """Applies Indigo Header Styling and Zebra Striping."""
        try:
            requests = [
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
            ]
            self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={'requests': requests}).execute()
        except: pass

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
            
            # Record Movement
            batch = item.get('Batch Number', 'N/A')
            trans_id = header.get('Invoice Number', 'TRANS-NEW')
            raw_qty = item.get('Quantity Received', '0')
            numeric_qty = self._parse_numeric(raw_qty)
            
            # Distribution Logic (Default to 'Main Warehouse' if not provided)
            # This will be used for both Sheets and Firestore
            distributions = item.get('distributions', [{'location': 'Main Warehouse', 'qty': numeric_qty}])
            
            for dist in distributions:
                loc_name = dist.get('location', 'Main Warehouse')
                l_qty = dist.get('qty', 0)
                self.add_movement(item_id, f"{trans_id} (Batch: {batch})", 'IN', l_qty, user_email, location=loc_name)
                self._update_summary(p_code, item.get('Product Name', 'N/A'), l_qty, item.get('Unit', 'PCS'), 'IN')
            
            new_ids.append(item_id)

        return new_ids

    def _update_summary(self, code: str, name: str, qty: float, unit: str, move_type: str):
        """Calculates real-time running balances in the Stock Summary sheet."""
        if not self.service: return
        try:
            # 1. Find product in Summary
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Summary!A:A'
            ).execute()
            codes = [row[0] for row in res.get('values', [])] if res.get('values') else []
            
            row_idx = -1
            for i, c in enumerate(codes):
                if c == code:
                    row_idx = i + 1
                    break
            
            if row_idx == -1:
                # Create New Summary Row
                received = qty if move_type == 'IN' else 0
                dispatched = qty if move_type == 'OUT' else 0
                balance = received - dispatched
                new_row = [code, name, received, dispatched, balance, unit]
                self._append_row('Stock Summary', new_row)
            else:
                # Update Existing Summary Row
                # Columns: A:Code, B:Name, C:Received, D:Dispatched, E:Balance, F:Unit
                range_name = f'Stock Summary!C{row_idx}:E{row_idx}'
                current = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=range_name
                ).execute().get('values', [[0, 0, 0]])[0]
                
                received = float(current[0])
                dispatched = float(current[1])
                
                if move_type == 'IN': received += qty
                else: dispatched += qty
                
                balance = received - dispatched
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range=range_name,
                    valueInputOption='USER_ENTERED', body={'values': [[received, dispatched, balance]]}
                ).execute()
        except Exception as e:
            print(f"Summary Update Error: {e}")

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
            old_qty_str = result.get('values', [[0]])[0][0]
            old_qty = self._parse_numeric(old_qty_str)
            
            raw_new_qty = new_item.get('Quantity Received', '0')
            new_qty_parsed = self._parse_numeric(raw_new_qty)
            
            # For the summary, we store the new calculated text if it was a hybrid, 
            # but for simplicity we'll just sum the numeric parts for now 
            # and append the new raw text for audit.
            final_qty_val = old_qty + new_qty_parsed
            now = time.strftime('%Y-%m-%d %H:%M:%S')

            # 2. Update Quantity + Updated At + Updated By
            # Note: We use individual updates to be safer with column mapping
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{chr(65+qty_idx)}{row_idx}',
                valueInputOption='USER_ENTERED', body={'values': [[final_qty_val]]}
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

    def add_movement(self, stock_item_id: str, trans_id: str, type: str, qty: float, user_email: str, location: str = "Main Warehouse"):
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        if self.service:
            # Row mapping for Location-Aware Movements
            row = [str(uuid.uuid4())[:8].upper(), stock_item_id, trans_id, type, qty, location, user_email, now_str]
            self._append_row('Stock Movements', row)
            
            # Sync to Summary
            item = self.get_stock_item(stock_item_id)
            if item:
                self._update_summary(item['product_code'], item['item_name'], abs(qty), item['unit'], type)
        try:
            db.collection('activity_logs').add({
                'stock_item_id': stock_item_id, 'transaction_id': trans_id,
                'movement_type': type, 'quantity_changed': qty, 'location': location,
                'actor_email': user_email, 'created_at': int(time.time()), 'item_name': 'Audit Move'
            })
        except: pass

    def update_stock_quantity(self, barcode_id: str, new_qty: float):
        """Standardized Deduct: Finds row by ID and updates the Quantity column."""
        if not self.service: return
        try:
            row_idx = self._find_row_by_col(0, barcode_id) # Column A is Barcode ID
            if row_idx != -1:
                qty_idx = self.header_map["Quantity Received"]
                col_letter = chr(65 + qty_idx)
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}{row_idx}',
                    valueInputOption='USER_ENTERED', body={'values': [[new_qty]]}
                ).execute()
        except Exception as e:
            print(f"Update Qty Error: {e}")

    def get_summary_stats(self) -> Dict:
        """Aggregates totals from the Stock Summary sheet for the Dashboard."""
        if not self.service: return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}
        try:
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range='Stock Summary!C:E'
            ).execute()
            rows = res.get('values', [])[1:] # Skip headers
            
            total_in = 0.0
            total_out = 0.0
            low_stock = 0
            
            for row in rows:
                if len(row) >= 3:
                    tin = float(row[0] or 0)
                    tout = float(row[1] or 0)
                    bal = float(row[2] or 0)
                    total_in += tin
                    total_out += tout
                    if bal < 10: low_stock += 1
            
            return {
                "total_in": total_in,
                "total_out": total_out,
                "available_balance": total_in - total_out,
                "low_stock_count": low_stock
            }
        except:
            return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}

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

    def _parse_numeric(self, val: any) -> float:
        """Extracts the first floating point number from a string (e.g. '7.675 MT' -> 7.675)"""
        if isinstance(val, (int, float)): return float(val)
        try:
            matches = re.findall(r"[-+]?\d*\.\d+|\d+", str(val))
            return float(matches[0]) if matches else 0.0
        except: return 0.0

    def get_current_headers(self) -> List[str]:
        """Returns the base schema for AI audit context."""
        return self.BASE_SCHEMA

sheets_service = SheetsService()
