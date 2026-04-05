import os
import time
from datetime import datetime, timedelta
import uuid
import base64
import json
import re
from typing import List, Dict
from google.oauth2 import service_account
from googleapiclient.discovery import build
from app.services.firebase import db

class SheetsService:
    # ELITE SCHEMA DEFINITION (Product Info FIRST)
    BASE_SCHEMA = [
        "Product Name", "Product Code", "Quantity Received", "Unit", "Barcode Link",
        "Supplier Name", "Date", "Invoice Number", "Supplier GST", "Batch Number",
        "Number of Bags", "Rate per Unit", "Total Amount", "Transport / Freight",
        "Taxes (IGST/CGST/SGST)", "Final Amount", "Vehicle Number", "Transporter Name",
        "Remarks", "Barcode ID", "Created At", "Created By", "Updated At", "Updated By"
    ]
    MOVEMENTS_SCHEMA = [
        "Timestamp", "Product Name", "Type", "Quantity", "Warehouse", "Location", "User",
        "Movement ID", "Barcode ID", "Transaction ID", "Position ID", "Warehouse ID"
    ]
    SUMMARY_SCHEMA = [
        "Product Name", "Product Code", "Current Balance", "Unit", "Total Received", "Total Dispatched", "Last Updated"
    ]

    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.scopes = ['https://www.googleapis.com/auth/spreadsheets']
        self.service = self._initialize_service()
        self.header_map = {name: i for i, name in enumerate(self.BASE_SCHEMA)}
        self._cache_expiry = 0
        self._cached_header_map = {}

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
        """Initializes all sheets for the Elite Stock Infrastructure with O(1) result caching."""
        if not self.service: return self.header_map
        
        if self._cached_header_map and time.time() < self._cache_expiry:
            return self._cached_header_map

        try:
            # OPTIMIZATION: Fetch ALL metadata in ONE call to reduce 502/Latency risks
            metadata = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
            # Map of sheet_title -> sheet_id
            existing_sheets = {s['properties']['title']: s['properties']['sheetId'] for s in metadata.get('sheets', [])}

            # Sequentially ensure all 3 core sheets exist
            self._ensure_sheet_v2('Stock Register', self.BASE_SCHEMA, existing_sheets)
            self._ensure_sheet_v2('Stock Movements', self.MOVEMENTS_SCHEMA, existing_sheets)
            self._ensure_sheet_v2('Stock Summary', self.SUMMARY_SCHEMA, existing_sheets)

            self._cached_header_map = {name: i for i, name in enumerate(self.BASE_SCHEMA)}
            self._cache_expiry = time.time() + 300 
            return self._cached_header_map
        except Exception as e:
            print(f"Global Header Init Error: {e}")
            return self.header_map

    def get_current_headers(self) -> List[str]:
        return self.BASE_SCHEMA

    def _ensure_sheet_v2(self, title: str, schema: List[str], existing_sheets: Dict):
        """Standardized Detection/Creation using pre-fetched metadata."""
        try:
            sheet_id = existing_sheets.get(title)
            
            if sheet_id is None:
                # 1. Create New Sheet
                body = {'requests': [{'addSheet': {'properties': {'title': title}}}]}
                res = self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute()
                sheet_id = res['replies'][0]['addSheet']['properties']['sheetId']
                
                # 2. Add Headers
                self._write_headers(title, schema)
                # 3. Add Elite Visuals
                self._apply_elite_styles_safe(sheet_id, title)
                if title == 'Stock Summary':
                    self._add_dashboard_charts_safe(sheet_id)
            else:
                # Sheet exists - just check if headers are missing
                result = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=f'{title}!1:1'
                ).execute()
                if not result.get('values'):
                    self._write_headers(title, schema)
                    self._apply_elite_styles_safe(sheet_id, title)
        except Exception as e:
            print(f"Error ensuring sheet {title}: {e}")

    def _write_headers(self, title: str, schema: List[str]):
        try:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id, range=f'{title}!1:1',
                valueInputOption='RAW', body={'values': [schema]}
            ).execute()
        except: pass

    def _apply_elite_styles_safe(self, sheet_id: int, title: str):
        """Applies Indigo Header Styling and Zebra Striping without crashing core sync."""
        try:
            requests = [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 63/255, "green": 81/255, "blue": 181/255}, 
                                "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True},
                                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                    }
                },
                {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2 if title != 'Stock Movements' else 1}}, "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1}],
                            "booleanRule": {
                                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=ISEVEN(ROW())"}]},
                                "format": {"backgroundColor": {"red": 0.96, "green": 0.97, "blue": 1.0}}
                            }
                        }, "index": 0
                    }
                }
            ]
            
            if title == 'Stock Summary':
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 3}],
                            "booleanRule": {
                                "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "10"}]},
                                "format": {"backgroundColor": {"red": 1.0, "green": 0.9, "blue": 0.9}, "textFormat": {"foregroundColor": {"red": 0.8, "green": 0.0, "blue": 0.0}, "bold": True}}
                            }
                        }, "index": 0
                    }
                })

            self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={'requests': requests}).execute()
        except Exception as e:
            print(f"Styling Error (Ignored): {e}")

    def _add_dashboard_charts_safe(self, sheet_id: int):
        try:
            requests = [{
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": "Inventory Levels",
                            "basicChart": {
                                "chartType": "BAR",
                                "legendPosition": "BOTTOM_LEGEND",
                                "domains": [{"domain": {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": 1}]}}}],
                                "series": [{"series": {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 2, "endColumnIndex": 3}]}}, "targetAxis": "BOTTOM_AXIS"}]
                            }
                        },
                        "position": {"newSheet": False, "overlayPosition": {"anchorCell": {"sheetId": sheet_id, "rowIndex": 2, "columnIndex": 8}}}
                    }
                }
            }]
            self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={'requests': requests}).execute()
        except Exception as e:
            print(f"Chart Creation Error (Ignored): {e}")

    def save_stock_batch(self, header: Dict, items: List[Dict], item_ids: List[str], user_email: str):
        if not self.service: return
        self.header_map = self._get_or_create_headers()
        now = time.strftime('%Y-%m-%d %H:%M:%S')

        for i, item in enumerate(items):
            item_id = item_ids[i]
            row_data = [""] * len(self.BASE_SCHEMA)
            
            # ELITE COLUMN ORDERING
            name = item.get('Product Name') or header.get('Product Name') or "Unknown Item"
            code = item.get('Product Code') or header.get('Product Code') or item_id[:8]
            qty_val = self._parse_numeric(item.get('Quantity Received') or header.get('Quantity Received') or 0)
            unit = item.get('Unit') or header.get('Unit') or "PCS"
            
            row_data[0] = name
            row_data[1] = code
            row_data[2] = str(qty_val)
            row_data[3] = unit
            row_data[4] = "" # Link added after upload
            
            # Metadata at the end
            row_data[19] = item_id 
            row_data[20] = now
            row_data[21] = user_email
            row_data[22] = now
            row_data[23] = user_email

            # Fill mid columns
            for key in self.BASE_SCHEMA[5:19]:
                idx = self.header_map.get(key)
                if idx is not None:
                    row_data[idx] = header.get(key) or item.get(key) or ""
            
            # ATOMIC REGISTER SYNC
            existing_row_idx = self._find_row_by_col(19, item_id) # Unique Check
            if existing_row_idx != -1:
                 self._merge_into_row_elite(existing_row_idx, item, user_email)
            else:
                 # Check by code to avoid duplicates in first columns
                 existing_by_code = self._find_row_by_col(1, code)
                 if existing_by_code != -1:
                      self._merge_into_row_elite(existing_by_code, item, user_email)
                 else:
                      self._append_row('Stock Register', row_data)
            
            # MOVEMENT & SUMMARY SYNC
            trans_id = header.get('Invoice Number', f"TRANS-{str(uuid.uuid4())[:4].upper()}")
            distributions = item.get('distributions', [])
            
            if not distributions:
                self.add_movement(item_id, trans_id, 'IN', qty_val, user_email, item_name=name)
                self._update_summary(code, name, qty_val, unit, 'IN')
            else:
                for dist in distributions:
                    d_qty = self._parse_numeric(dist.get('qty', 0))
                    d_loc = dist.get('location', 'Main Floor')
                    self.add_movement(item_id, trans_id, 'IN', d_qty, user_email, location=d_loc, item_name=name)
                    self._update_summary(code, name, d_qty, unit, 'IN')

    def add_movement(self, barcode_id: str, trans_id: str, move_type: str, qty: float, user_email: str, warehouse: str = "Main Warehouse", location: str = "Full Receive", dist_id: str = "N/A", warehouse_id: str = "N/A", item_name: str = "Audit Item"):
        if not self.service: return
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        
        movement_row = [
            now, item_name, move_type, qty, warehouse, location, user_email,
            f"MOV-{str(uuid.uuid4())[:6].upper()}", barcode_id, trans_id, dist_id, warehouse_id
        ]
        self._append_row('Stock Movements', movement_row)

    def _update_summary(self, code: str, name: str, qty: float, unit: str, move_type: str):
        if not self.service: return
        try:
            # 1. Product Discovery
            res = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Summary!B:B').execute()
            codes = [row[0] for row in res.get('values', [])] if res.get('values') else []
            
            row_idx = -1
            for i, c in enumerate(codes):
                if str(c).strip() == str(code).strip():
                    row_idx = i + 1
                    break
            
            if row_idx == -1:
                # Fresh Item Entry
                received = qty if move_type == 'IN' else 0
                dispatched = qty if move_type == 'OUT' else 0
                balance = received - dispatched
                new_row = [name, code, balance, unit, received, dispatched, time.strftime('%Y-%m-%d %H:%M:%S')]
                self._append_row('Stock Summary', new_row)
            else:
                # Running Balance Update
                range_name = f'Stock Summary!C{row_idx}:F{row_idx}'
                current = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=range_name).execute().get('values', [[0, 0, 0, 0]])[0]
                
                balance = self._parse_numeric(current[0])
                received = self._parse_numeric(current[2])
                dispatched = self._parse_numeric(current[3])
                
                if move_type == 'IN': 
                    received += qty
                    balance += qty
                else: 
                    dispatched += qty
                    balance -= qty
                
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range=range_name,
                    valueInputOption='USER_ENTERED', body={'values': [[balance, unit, received, dispatched]]}
                ).execute()
        except Exception as e:
            print(f"Summary Update Error: {e}")

    def _merge_into_row_elite(self, row_idx: int, new_item: Dict, user_email: str):
        try:
            col_letter = "C" # Column C is Quantity Received
            result = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}{row_idx}').execute()
            old_qty = self._parse_numeric(result.get('values', [[0]])[0][0])
            new_qty = self._parse_numeric(new_item.get('Quantity Received', '0'))
            
            final_qty = old_qty + new_qty
            now = time.strftime('%Y-%m-%d %H:%M:%S')

            self.service.spreadsheets().values().update(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}{row_idx}', valueInputOption='USER_ENTERED', body={'values': [[final_qty]]}).execute()
            self.service.spreadsheets().values().update(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!W{row_idx}:X{row_idx}', valueInputOption='USER_ENTERED', body={'values': [[now, user_email]]}).execute()
        except: pass

    def update_barcode_link(self, barcode_id: str, link: str):
        if not self.service: return
        try:
            row_idx = self._find_row_by_col(19, barcode_id) # Column T
            if row_idx != -1:
                self.service.spreadsheets().values().update(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!E{row_idx}', valueInputOption='USER_ENTERED', body={'values': [[link]]}).execute()
        except: pass
            
    def update_stock_quantity(self, barcode_id: str, new_qty: float):
        if not self.service: return
        try:
            row_idx = self._find_row_by_col(19, barcode_id)
            if row_idx != -1:
                self.service.spreadsheets().values().update(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!C{row_idx}', valueInputOption='USER_ENTERED', body={'values': [[new_qty]]}).execute()
        except: pass

    def get_summary_stats(self, period: str = "all") -> Dict:
        if not self.service: return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}
        try:
            res = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Summary!C:F').execute()
            rows = res.get('values', [])[1:]
            
            t_in, t_out, t_bal, low_count = 0.0, 0.0, 0.0, 0
            for row in rows:
                if len(row) >= 4:
                    bal, tin, tout = self._parse_numeric(row[0]), self._parse_numeric(row[2]), self._parse_numeric(row[3])
                    t_in += tin; t_out += tout; t_bal += bal
                    if bal < 10: low_count += 1
            return {"total_in": t_in, "total_out": t_out, "available_balance": t_bal, "low_stock_count": low_count}
        except: return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}

    def get_stock_item(self, item_id: str) -> Dict:
        if not self.service: return {}
        try:
            result = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Register!A:T').execute().get('values', [])
            for row in result:
                if row and len(row) >= 20 and str(row[19]) == str(item_id):
                    return {"stock_item_id": row[19], "item_name": row[0], "quantity_remaining": self._parse_numeric(row[2]), "unit": row[3], "supplier_name": row[5], "product_code": row[1]}
        except: pass
        return {}

    def _find_row_by_col(self, col_idx: int, value: str) -> int:
        if not self.service: return -1
        col_letter = chr(65 + col_idx)
        try:
            result = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}:{col_letter}').execute()
            values = result.get('values', [])
            for i, row in enumerate(values):
                if row and str(row[0]).strip() == str(value).strip():
                    return i + 1
            return -1
        except: return -1

    def get_summary_stats(self, period: str = "all") -> Dict:
        if not self.service: return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}
        try:
            res = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Summary!C:F').execute()
            rows = res.get('values', [])[1:]
            
            t_in, t_out, t_bal, low_count = 0.0, 0.0, 0.0, 0
            for row in rows:
                if len(row) >= 4:
                    bal, tin, tout = self._parse_numeric(row[0]), self._parse_numeric(row[2]), self._parse_numeric(row[3])
                    t_in += tin; t_out += tout; t_bal += bal
                    if bal < 10: low_count += 1
            return {"total_in": t_in, "total_out": t_out, "available_balance": t_bal, "low_stock_count": low_count}
        except: return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}

    def get_graph_data(self) -> Dict:
        if not self.service: return {"movement": [], "zones": []}
        try:
            move_res = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Movements!A:D').execute()
            move_rows = move_res.get('values', [])[1:]
            
            days = [(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
            movement_data = {d: {"in": 0.0, "out": 0.0} for d in days}
            
            for row in move_rows:
                if len(row) >= 4:
                    ts, m_type, m_qty = row[0], row[2], abs(self._parse_numeric(row[3]))
                    d_key = ts.split(' ')[0]
                    if d_key in movement_data:
                        if m_type == 'IN': movement_data[d_key]["in"] += m_qty
                        elif m_type == 'OUT': movement_data[d_key]["out"] += m_qty
            
            from app.services.inventory import inventory_service
            return {"movement": [{"date": d, "in": v["in"], "out": v["out"]} for d, v in movement_data.items()], "zones": inventory_service.get_all_zones()}
        except: return {"movement": [], "zones": []}

    def _append_row(self, sheet_name: str, row_data: List):
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id, range=f'{sheet_name}!A:A',
                valueInputOption='USER_ENTERED', body={'values': [row_data]}
            ).execute()
        except: pass

    def _parse_numeric(self, val: any) -> float:
        if isinstance(val, (int, float)): return float(val)
        try:
            matches = re.findall(r"[-+]?\d*\.\d+|\d+", str(val))
            return float(matches[0]) if matches else 0.0
        except: return 0.0

sheets_service = SheetsService()
