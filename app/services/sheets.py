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
        self._sheet_ids_cache = {}

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
        """Initializes all sheets for the Elite Stock Infrastructure with $O(1)$ result caching."""
        if not self.service: return {}
        
        if self._cached_header_map and time.time() < self._cache_expiry:
            return self._cached_header_map

        try:
            self._ensure_sheet('Stock Register', self.BASE_SCHEMA)
            self._ensure_sheet('Stock Movements', self.MOVEMENTS_SCHEMA)
            self._ensure_sheet('Stock Summary', self.SUMMARY_SCHEMA)

            self._cached_header_map = {name: i for i, name in enumerate(self.BASE_SCHEMA)}
            self._cache_expiry = time.time() + 300 
            return self._cached_header_map
        except Exception as e:
            print(f"Header Init Error: {e}")
            return self.header_map

    def get_current_headers(self) -> List[str]:
        return self.BASE_SCHEMA

    def _ensure_sheet(self, title: str, schema: List[str]):
        try:
            metadata = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
            sheets = {s['properties']['title']: s['properties']['sheetId'] for s in metadata.get('sheets', [])}
            
            if title not in sheets:
                body = {'requests': [{'addSheet': {'properties': {'title': title}}}]}
                res = self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute()
                sheet_id = res['replies'][0]['addSheet']['properties']['sheetId']
                self._write_headers(title, schema)
                self._apply_elite_styles(sheet_id, len(schema), title)
                if title == 'Stock Summary':
                    self._add_dashboard_charts(sheet_id)
            else:
                result = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=f'{title}!1:1'
                ).execute()
                if not result.get('values'):
                    self._write_headers(title, schema)
                    self._apply_elite_styles(sheets[title], len(schema), title)
        except Exception as e:
            print(f"Error ensuring sheet {title}: {e}")

    def _write_headers(self, title: str, schema: List[str]):
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id, range=f'{title}!1:1',
            valueInputOption='RAW', body={'values': [schema]}
        ).execute()

    def _apply_elite_styles(self, sheet_id: int, column_count: int, title: str):
        try:
            requests = [
                # 1. Header Styling
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 63/255, "green": 81/255, "blue": 181/255}, # Deep indigo
                                "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True, "fontSize": 11},
                                "horizontalAlignment": "CENTER",
                                "verticalAlignment": "MIDDLE"
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                    }
                },
                # 2. Frozen Rows & Columns
                {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2 if title != 'Stock Movements' else 1}}, "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
                # 3. Zebra Striping (Conditional Formatting)
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1}],
                            "booleanRule": {
                                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=ISEVEN(ROW())"}]},
                                "format": {"backgroundColor": {"red": 0.96, "green": 0.97, "blue": 1.0}}
                            }
                        },
                        "index": 0
                    }
                }
            ]
            
            # Low Stock Alert for Summary Sheet
            if title == 'Stock Summary':
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 3}],
                            "booleanRule": {
                                "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "10"}]},
                                "format": {"backgroundColor": {"red": 1.0, "green": 0.9, "blue": 0.9}, "textFormat": {"foregroundColor": {"red": 0.8, "green": 0.0, "blue": 0.0}, "bold": True}}
                            }
                        },
                        "index": 0
                    }
                })

            self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={'requests': requests}).execute()
        except: pass

    def _add_dashboard_charts(self, sheet_id: int):
        """Injects Bar and Pie charts into the Stock Summary sheet for visual analytics."""
        try:
            requests = [
                {
                    "addChart": {
                        "chart": {
                            "spec": {
                                "title": "Current Inventory Levels",
                                "basicChart": {
                                    "chartType": "BAR",
                                    "legendPosition": "BOTTOM_LEGEND",
                                    "axis": [{"position": "BOTTOM_AXIS", "title": "Quantity"}, {"position": "LEFT_AXIS", "title": "Products"}],
                                    "domains": [{"domain": {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": 1}]}}}],
                                    "series": [{"series": {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 2, "endColumnIndex": 3}]}}, "targetAxis": "BOTTOM_AXIS"}]
                                }
                            },
                            "position": {"newSheet": False, "overlayPosition": {"anchorCell": {"sheetId": sheet_id, "rowIndex": 2, "columnIndex": 8}, "offsetXPixels": 0, "offsetYPixels": 0}}
                        }
                    }
                }
            ]
            self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body={'requests': requests}).execute()
        except Exception as e:
            print(f"Chart Creation Error: {e}")

    def save_stock_batch(self, header: Dict, items: List[Dict], item_ids: List[str], user_email: str):
        if not self.service: return
        self.header_map = self._get_or_create_headers()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        all_rows = []

        for i, item in enumerate(items):
            item_id = item_ids[i]
            row_data = [""] * len(self.BASE_SCHEMA)
            
            row_data[0] = item.get('Product Name') or header.get('Product Name') or "Unknown Item"
            row_data[1] = item.get('Product Code') or header.get('Product Code') or item_id[:8]
            row_data[2] = item.get('Quantity Received') or header.get('Quantity Received') or "0"
            row_data[3] = item.get('Unit') or header.get('Unit') or "PCS"
            row_data[4] = "" 
            row_data[19] = item_id # Barcode ID
            row_data[20] = now
            row_data[21] = user_email
            row_data[22] = now
            row_data[23] = user_email
            
            for key in self.BASE_SCHEMA[5:19]:
                idx = self.header_map.get(key)
                if idx is not None:
                    row_data[idx] = header.get(key) or item.get(key) or ""
            
            all_rows.append(row_data)
            p_code = row_data[1]
            existing_row_idx = self._find_row_by_col(1, p_code) # Search Column B

            if existing_row_idx != -1:
                self._merge_into_row_elite(existing_row_idx, item, user_email)
            else:
                self._append_row('Stock Register', row_data)
            
            trans_id = header.get('Invoice Number', 'TRANS-NEW')
            raw_qty = self._parse_numeric(row_data[2])
            
            distributions = item.get('distributions', [])
            if not distributions:
                # Add default movement if no spatial distribution provided
                self.add_movement(item_id, trans_id, 'IN', raw_qty, user_email)
                self._update_summary(p_code, row_data[0], raw_qty, row_data[3], 'IN')
            else:
                for dist in distributions:
                    wh = dist.get('warehouse', 'Main Warehouse')
                    loc = dist.get('location', 'Full Receive')
                    l_qty = self._parse_numeric(dist.get('qty', 0))
                    self.add_movement(item_id, trans_id, 'IN', l_qty, user_email, warehouse=wh, location=loc)
                    self._update_summary(p_code, row_data[0], l_qty, row_data[3], 'IN')

    def add_movement(self, barcode_id: str, trans_id: str, type: str, qty: float, user_email: str, warehouse: str = "Main Warehouse", location: str = "Full Receive", dist_id: str = "N/A", warehouse_id: str = "N/A"):
        if not self.service: return
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        item_name = "Audit Item"
        try:
             res = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Register!A:B').execute()
             rows = res.get('values', [])
             for r in rows:
                 if len(r) > 1 and r[1] == barcode_id:
                     item_name = r[0]
                     break
        except: pass

        movement_row = [
            now, item_name, type, qty, warehouse, location, user_email,
            f"MOV-{str(uuid.uuid4())[:6].upper()}", barcode_id, trans_id, dist_id, warehouse_id
        ]
        self._append_row('Stock Movements', movement_row)

    def _update_summary(self, code: str, name: str, qty: float, unit: str, move_type: str):
        if not self.service: return
        try:
            res = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Summary!B:B').execute()
            codes = [row[0] for row in res.get('values', [])] if res.get('values') else []
            
            row_idx = -1
            for i, c in enumerate(codes):
                if c == code:
                    row_idx = i + 1
                    break
            
            if row_idx == -1:
                received = qty if move_type == 'IN' else 0
                dispatched = qty if move_type == 'OUT' else 0
                balance = received - dispatched
                new_row = [name, code, balance, unit, received, dispatched, time.strftime('%Y-%m-%d %H:%M:%S')]
                self._append_row('Stock Summary', new_row)
            else:
                range_name = f'Stock Summary!C{row_idx}:F{row_idx}'
                current = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=range_name).execute().get('values', [[0, 0, 0, 0]])[0]
                
                balance = float(current[0])
                received = float(current[2])
                dispatched = float(current[3])
                
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

    def _merge_into_row_elite(self, row_idx: int, new_item: Dict, user_email: str) -> str:
        try:
            col_letter = "C" # Quantity Received
            result = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}{row_idx}').execute()
            old_qty = self._parse_numeric(result.get('values', [[0]])[0][0])
            new_qty = self._parse_numeric(new_item.get('Quantity Received', '0'))
            
            final_qty = old_qty + new_qty
            now = time.strftime('%Y-%m-%d %H:%M:%S')

            self.service.spreadsheets().values().update(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!{col_letter}{row_idx}', valueInputOption='USER_ENTERED', body={'values': [[final_qty]]}).execute()
            self.service.spreadsheets().values().update(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!W{row_idx}:X{row_idx}', valueInputOption='USER_ENTERED', body={'values': [[now, user_email]]}).execute()
            
            res_id = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=f'Stock Register!T{row_idx}').execute().get('values', [['UNKNOWN']])[0][0]
            return res_id
        except: return "ERROR-MERGE"

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
            
            total_in = 0.0
            total_out = 0.0
            balance = 0.0
            low_count = 0
            
            for row in rows:
                if len(row) >= 4:
                    bal = self._parse_numeric(row[0])
                    tin = self._parse_numeric(row[2])
                    tout = self._parse_numeric(row[3])
                    total_in += tin
                    total_out += tout
                    balance += bal
                    if bal < 10: low_count += 1

            return {"total_in": total_in, "total_out": total_out, "available_balance": balance, "low_stock_count": low_count}
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

    def get_stock_item(self, item_id: str) -> Dict:
        if not self.service: return {}
        try:
            result = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range='Stock Register!A:T').execute().get('values', [])
            for row in result:
                if row and len(row) >= 20 and row[19] == item_id:
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

    def _parse_numeric(self, val: any) -> float:
        if isinstance(val, (int, float)): return float(val)
        try:
            matches = re.findall(r"[-+]?\d*\.\d+|\d+", str(val))
            return float(matches[0]) if matches else 0.0
        except: return 0.0

sheets_service = SheetsService()
