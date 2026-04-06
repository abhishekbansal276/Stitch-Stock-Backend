import os
import time
import json
import re
import uuid
import traceback
import sys
from datetime import datetime, timedelta
from typing import List, Dict

from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud.firestore_v1.base_query import FieldFilter
from google.auth import exceptions as auth_exceptions
from app.services.firebase import db, clean_private_key, BASE_DIR, get_service_account_info


class SheetsService:
    # ── SCHEMA DEFINITIONS ────────────────────────────────────────────────────
    BASE_SCHEMA = [
        "Product Name", "Product Code", "Quantity Received", "Unit", "Barcode Link",
        "Supplier Name", "Date", "Invoice Number", "Supplier GST", "Batch Number",
        "Number of Bags", "Rate per Unit", "Total Amount", "Transport / Freight",
        "Taxes (IGST/CGST/SGST)", "Final Amount", "Vehicle Number", "Transporter Name",
        "Remarks", "Barcode ID", "Created At", "Created By", "Updated At", "Updated By",
    ]
    MOVEMENTS_SCHEMA = [
        "Timestamp", "Product Name", "Type", "Quantity", "Warehouse", "Location", "User",
        "Movement ID", "Barcode ID", "Transaction ID", "Position ID", "Warehouse ID",
    ]
    SUMMARY_SCHEMA = [
        "Product Name", "Product Code", "Current Balance", "Unit",
        "Total Received", "Total Dispatched", "Last Updated",
    ]

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.service = self._initialize_service()
        self.header_map = {name: i for i, name in enumerate(self.BASE_SCHEMA)}
        self._cache_expiry = 0
        self._cached_header_map: Dict = {}

    # ── SERVICE INIT ──────────────────────────────────────────────────────────

    def _initialize_service(self):
        """Build the Sheets API client with robust credential triaging."""
        info = get_service_account_info()
        if info:
            try:
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=self.SCOPES)
                print("SheetsService: Initialized with service account info")
                return build("sheets", "v4", credentials=creds)
            except Exception as e:
                print(f"SheetsService: Failed to build from info — {e}")

        # FINAL FALLBACK (e.g. for CI/CD or Cloud Run)
        try:
            from google import auth
            creds, _ = auth.default(scopes=self.SCOPES)
            print("SheetsService: Initialized with default Application Credentials")
            return build("sheets", "v4", credentials=creds)
        except Exception as e:
            print(f"SheetsService: Could not initialize (no creds) — {e}")
            return None

    # ── SHEET BOOTSTRAP ───────────────────────────────────────────────────────

    def _get_or_create_headers(self) -> Dict:
        """Ensure all three sheets exist with correct headers. Result is cached 5 min."""
        if not self.service:
            return self.header_map

        if self._cached_header_map and time.time() < self._cache_expiry:
            return self._cached_header_map

        try:
            metadata = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id).execute()

            self._cached_header_map = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            self._cache_expiry = time.time() + 300
            return self._cached_header_map
        except Exception as e:
            print(f"CRITICAL: Header init error: {e}")
            traceback.print_exc()
            return self.header_map

    def _ensure_sheet(self, title: str, schema: List[str], existing: Dict):
        try:
            sheet_id = existing.get(title)
            if sheet_id is None:
                res = self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
                ).execute()
                sheet_id = res["replies"][0]["addSheet"]["properties"]["sheetId"]
                self._write_headers(title, schema)
                self._apply_styles(sheet_id, title)
                if title == "Stock Summary":
                    self._add_chart(sheet_id)
            else:
                result = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=f"{title}!1:1"
                ).execute()
                if not result.get("values"):
                    self._write_headers(title, schema)
                    self._apply_styles(sheet_id, title)
        except Exception as e:
            print(f"Error ensuring sheet '{title}': {e}")

    def _write_headers(self, title: str, schema: List[str]):
        try:
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{title}!1:1",
                valueInputOption="RAW",
                body={"values": [schema]},
            ).execute()
        except Exception as e:
            print(f"Write headers error for '{title}': {e}")

    def _apply_styles(self, sheet_id: int, title: str):
        try:
            requests = [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 63/255, "green": 81/255, "blue": 181/255},
                                "textFormat": {
                                    "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                                    "bold": True,
                                },
                                "horizontalAlignment": "CENTER",
                                "verticalAlignment": "MIDDLE",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {
                                "frozenRowCount": 1,
                                "frozenColumnCount": 2 if title != "Stock Movements" else 1,
                            },
                        },
                        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1}],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [{"userEnteredValue": "=ISEVEN(ROW())"}],
                                },
                                "format": {"backgroundColor": {"red": 0.96, "green": 0.97, "blue": 1.0}},
                            },
                        },
                        "index": 0,
                    }
                },
            ]
            if title == "Stock Summary":
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1,
                                        "startColumnIndex": 2, "endColumnIndex": 3}],
                            "booleanRule": {
                                "condition": {"type": "NUMBER_LESS",
                                              "values": [{"userEnteredValue": "10"}]},
                                "format": {
                                    "backgroundColor": {"red": 1.0, "green": 0.9, "blue": 0.9},
                                    "textFormat": {
                                        "foregroundColor": {"red": 0.8, "green": 0.0, "blue": 0.0},
                                        "bold": True,
                                    },
                                },
                            },
                        },
                        "index": 0,
                    }
                })
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            ).execute()
        except Exception as e:
            print(f"Styling error (ignored): {e}")

    def _add_chart(self, sheet_id: int):
        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{
                    "addChart": {
                        "chart": {
                            "spec": {
                                "title": "Inventory Levels",
                                "basicChart": {
                                    "chartType": "BAR",
                                    "legendPosition": "BOTTOM_LEGEND",
                                    "domains": [{"domain": {"sourceRange": {"sources": [
                                        {"sheetId": sheet_id, "startRowIndex": 0,
                                         "startColumnIndex": 0, "endColumnIndex": 1}
                                    ]}}}],
                                    "series": [{"series": {"sourceRange": {"sources": [
                                        {"sheetId": sheet_id, "startRowIndex": 0,
                                         "startColumnIndex": 2, "endColumnIndex": 3}
                                    ]}}, "targetAxis": "BOTTOM_AXIS"}],
                                },
                            },
                            "position": {
                                "overlayPosition": {
                                    "anchorCell": {"sheetId": sheet_id, "rowIndex": 2, "columnIndex": 8}
                                }
                            },
                        }
                    }
                }]},
            ).execute()
        except Exception as e:
            print(f"Chart creation error (ignored): {e}")

    # ── STOCK WRITE ───────────────────────────────────────────────────────────

    def save_stock_batch(self, header: Dict, items: List[Dict],
                         item_ids: List[str], user_email: str):
        if not self.service:
            return
        self.header_map = self._get_or_create_headers()
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        # ── OPTIMIZATION: Fetch existing data once for lookups ──
        # Fetch barcode IDs (col 20 / index 19) and Product Codes (col 2 / index 1)
        try:
            lookup_res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Register!A:T"
            ).execute().get("values", [])
            existing_barcode_ids = {str(row[19]).strip(): i+1 for i, row in enumerate(lookup_res) if len(row) > 19}
            existing_product_codes = {str(row[1]).strip(): i+1 for i, row in enumerate(lookup_res) if len(row) > 1}
        except Exception as e:
            print(f"Sheets Lookup Error: {e}")
            existing_barcode_ids = {}
            existing_product_codes = {}

        for i, item in enumerate(items):
            item_id = item_ids[i]
            name   = item.get("Product Name") or header.get("Product Name") or "Unknown Item"
            code   = item.get("Product Code") or header.get("Product Code") or item_id[:8]
            qty    = self._to_float(item.get("Quantity Received") or header.get("Quantity Received") or 0)
            unit   = item.get("Unit") or header.get("Unit") or "PCS"

            row_data = [""] * len(self.BASE_SCHEMA)
            row_data[0]  = name
            row_data[1]  = code
            row_data[2]  = str(qty)
            row_data[3]  = unit
            row_data[4]  = ""          # Barcode link filled after upload
            row_data[19] = item_id
            row_data[20] = now
            row_data[21] = user_email
            row_data[22] = now
            row_data[23] = user_email

            for col_name in self.BASE_SCHEMA[5:19]:
                idx = self.header_map.get(col_name)
                if idx is not None:
                    row_data[idx] = item.get(col_name) or header.get(col_name) or ""

            # Upsert logic using in-memory lookup
            existing_idx = existing_barcode_ids.get(str(item_id).strip())
            if not existing_idx:
                existing_idx = existing_product_codes.get(str(code).strip())

            if existing_idx:
                self._merge_row(existing_idx, item, user_email)
            else:
                self._append_row("Stock Register", row_data)

            # Movement + Summary
            trans_id = header.get("Invoice Number") or f"TRANS-{str(uuid.uuid4())[:4].upper()}"
            distributions = item.get("distributions", [])
            if not distributions:
                self.add_movement(item_id, trans_id, "IN", qty, user_email, item_name=name)
                self._update_summary(code, name, qty, unit, "IN")
            else:
                for dist in distributions:
                    d_qty = self._to_float(dist.get("qty", 0))
                    d_loc = dist.get("location", "Main Floor")
                    self.add_movement(item_id, trans_id, "IN", d_qty, user_email,
                                      location=d_loc, item_name=name)
                    self._update_summary(code, name, d_qty, unit, "IN")

    def add_movement(self, barcode_id: str, trans_id: str, move_type: str,
                     qty: float, user_email: str, warehouse: str = "Main Warehouse",
                     location: str = "Full Receive", dist_id: str = "N/A",
                     warehouse_id: str = "N/A", item_name: str = "Audit Item"):
        if not self.service:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        row = [
            now, item_name, move_type, qty, warehouse, location, user_email,
            f"MOV-{str(uuid.uuid4())[:6].upper()}", barcode_id,
            trans_id, dist_id, warehouse_id,
        ]
        self._append_row("Stock Movements", row)

    def _update_summary(self, code: str, name: str, qty: float, unit: str, move_type: str):
        if not self.service:
            return
        try:
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Summary!B:B"
            ).execute()
            codes = [r[0] for r in res.get("values", []) if r]

            row_idx = next((i + 1 for i, c in enumerate(codes)
                            if str(c).strip() == str(code).strip()), -1)

            if row_idx == -1:
                received   = qty if move_type == "IN" else 0
                dispatched = qty if move_type == "OUT" else 0
                balance    = received - dispatched
                self._append_row("Stock Summary",
                                 [name, code, balance, unit, received, dispatched,
                                  time.strftime("%Y-%m-%d %H:%M:%S")])
            else:
                rng = f"Stock Summary!C{row_idx}:F{row_idx}"
                current = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=rng
                ).execute().get("values", [[0, 0, 0, 0]])[0]

                balance    = self._to_float(current[0])
                received   = self._to_float(current[2])
                dispatched = self._to_float(current[3])

                if move_type == "IN":
                    received += qty
                    balance  += qty
                else:
                    dispatched += qty
                    balance    -= qty

                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id, range=rng,
                    valueInputOption="USER_ENTERED",
                    body={"values": [[balance, unit, received, dispatched]]},
                ).execute()
        except Exception as e:
            print(f"Summary update error: {e}")

    def _merge_row(self, row_idx: int, new_item: Dict, user_email: str):
        """Add new_item quantity to existing row and update timestamps."""
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"Stock Register!C{row_idx}",
            ).execute()
            old_qty = self._to_float(result.get("values", [[0]])[0][0])
            new_qty = self._to_float(new_item.get("Quantity Received", 0))
            now = time.strftime("%Y-%m-%d %H:%M:%S")

            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"Stock Register!C{row_idx}",
                valueInputOption="USER_ENTERED",
                body={"values": [[old_qty + new_qty]]},
            ).execute()
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"Stock Register!W{row_idx}:X{row_idx}",
                valueInputOption="USER_ENTERED",
                body={"values": [[now, user_email]]},
            ).execute()
        except Exception as e:
            print(f"Merge row error: {e}")

    # ── READ HELPERS ──────────────────────────────────────────────────────────

    def update_barcode_link(self, barcode_id: str, link: str):
        if not self.service:
            return
        try:
            row_idx = self._find_row_by_col(19, barcode_id)
            if row_idx != -1:
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Stock Register!E{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[link]]},
                ).execute()
        except Exception as e:
            print(f"Update barcode link error: {e}")

    def update_stock_quantity(self, barcode_id: str, new_qty: float):
        if not self.service:
            return
        try:
            row_idx = self._find_row_by_col(19, barcode_id)
            if row_idx != -1:
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Stock Register!C{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[new_qty]]},
                ).execute()
        except Exception as e:
            print(f"Update stock quantity error: {e}")

    def get_stock_item(self, item_id: str) -> Dict:
        if not self.service:
            return {}
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Register!A:T"
            ).execute().get("values", [])
            for row in result:
                if row and len(row) >= 20 and str(row[19]) == str(item_id):
                    return {
                        "stock_item_id": row[19],
                        "item_name": row[0],
                        "quantity_remaining": self._to_float(row[2]),
                        "unit": row[3],
                        "supplier_name": row[5],
                        "product_code": row[1],
                    }
        except Exception as e:
            print(f"Get stock item error: {e}")
        return {}

    def get_summary_stats(self, period: str = "all") -> Dict:
        if not self.service:
            return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}
        try:
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Summary!C:F"
            ).execute()
            rows = res.get("values", [])[1:]
            t_in = t_out = t_bal = low = 0.0
            for row in rows:
                if len(row) >= 4:
                    bal  = self._to_float(row[0])
                    tin  = self._to_float(row[2])
                    tout = self._to_float(row[3])
                    t_in += tin; t_out += tout; t_bal += bal
                    if bal < 10:
                        low += 1
            return {"total_in": t_in, "total_out": t_out,
                    "available_balance": t_bal, "low_stock_count": int(low)}
        except Exception as e:
            print(f"Get summary stats error: {e}")
            return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0}

    def get_graph_data(self) -> Dict:
        if not self.service:
            return {"movement": [], "zones": []}
        try:
            move_res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Movements!A:D"
            ).execute()
            move_rows = move_res.get("values", [])[1:]

            days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(6, -1, -1)]
            movement_data = {d: {"in": 0.0, "out": 0.0} for d in days}

            for row in move_rows:
                if len(row) >= 4:
                    d_key  = str(row[0]).split(" ")[0]
                    m_type = row[2]
                    m_qty  = abs(self._to_float(row[3]))
                    if d_key in movement_data:
                        if m_type == "IN":
                            movement_data[d_key]["in"] += m_qty
                        elif m_type == "OUT":
                            movement_data[d_key]["out"] += m_qty

            from app.services.inventory import inventory_service
            return {
                "movement": [{"date": d, "in": v["in"], "out": v["out"]}
                             for d, v in movement_data.items()],
                "zones": inventory_service.get_all_zones(),
            }
        except Exception as e:
            print(f"Get graph data error: {e}")
            return {"movement": [], "zones": []}

    def get_current_headers(self) -> List[str]:
        return self.BASE_SCHEMA

    # ── LOW-LEVEL HELPERS ─────────────────────────────────────────────────────

    def _find_row_by_col(self, col_idx: int, value: str) -> int:
        if not self.service:
            return -1
        col_letter = chr(65 + col_idx)
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"Stock Register!{col_letter}:{col_letter}",
            ).execute()
            for i, row in enumerate(result.get("values", [])):
                if row and str(row[0]).strip() == str(value).strip():
                    return i + 1
        except Exception as e:
            print(f"Find row error: {e}")
        return -1

    def _append_row(self, sheet_name: str, row_data: List):
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet_name}!A:A",
                valueInputOption="USER_ENTERED",
                body={"values": [row_data]},
            ).execute()
        except Exception as e:
            print(f"Append row error for '{sheet_name}': {e}")

    def _to_float(self, val) -> float:
        if isinstance(val, (int, float)):
            return float(val)
        try:
            nums = re.findall(r"[-+]?\d*\.?\d+", str(val))
            return float(nums[0]) if nums else 0.0
        except Exception:
            return 0.0


sheets_service = SheetsService()
