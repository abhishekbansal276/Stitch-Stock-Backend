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


# ── DESIGN TOKENS ─────────────────────────────────────────────────────────────
# A refined industrial palette: deep navy header, teal accents, warm amber alerts
PALETTE = {
    # Header backgrounds per sheet
    "register_header":  {"red": 0.082, "green": 0.192, "blue": 0.349},   # #152F59 deep navy
    "movements_header": {"red": 0.067, "green": 0.290, "blue": 0.271},   # #114A45 deep teal
    "summary_header":   {"red": 0.220, "green": 0.110, "blue": 0.380},   # #381C61 deep violet

    # Section sub-header tints (used for column-group rows)
    "register_subhdr": {"red": 0.180, "green": 0.380, "blue": 0.620},    # #2E619E slate blue
    "movements_subhdr":{"red": 0.110, "green": 0.490, "blue": 0.450},    # #1C7D73 teal
    "summary_subhdr":  {"red": 0.380, "green": 0.200, "blue": 0.600},    # #613399 violet

    # Alternating row fills
    "stripe_a":  {"red": 0.973, "green": 0.976, "blue": 0.988},          # #F8F9FC near-white
    "stripe_b":  {"red": 0.929, "green": 0.945, "blue": 0.973},          # #EDF1F8 light blue

    # Accent fills
    "low_stock_bg":  {"red": 1.000, "green": 0.922, "blue": 0.918},      # warm red tint
    "low_stock_fg":  {"red": 0.714, "green": 0.102, "blue": 0.102},      # #B61A1A dark red
    "in_move_bg":    {"red": 0.898, "green": 0.973, "blue": 0.933},      # green tint
    "in_move_fg":    {"red": 0.063, "green": 0.431, "blue": 0.239},      # dark green
    "out_move_bg":   {"red": 1.000, "green": 0.945, "blue": 0.882},      # amber tint
    "out_move_fg":   {"red": 0.580, "green": 0.310, "blue": 0.000},      # dark amber

    "white": {"red": 1.0, "green": 1.0, "blue": 1.0},
}


def _rgb(key: str) -> dict:
    return PALETTE[key]


class SheetsService:
    # ── SCHEMA DEFINITIONS ────────────────────────────────────────────────────
    BASE_SCHEMA = [
        "Product Name", "Product Code", "Model Name", "Quantity Received", "Unit", "Barcode Link",
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

    # Column widths (pixels) — tuned per sheet for readability
    REGISTER_COL_WIDTHS = {
        0: 220,   # Product Name
        1: 130,   # Product Code
        2: 150,   # Model Name
        3: 130,   # Quantity Received
        4: 80,    # Unit
        5: 180,   # Barcode Link
        6: 160,   # Supplier Name
        7: 110,   # Date
        8: 150,   # Invoice Number
        9: 140,   # Supplier GST
        10: 130,  # Batch Number
        11: 110,  # Number of Bags
        12: 130,  # Rate per Unit
        13: 140,  # Total Amount
        14: 160,  # Transport / Freight
        15: 190,  # Taxes
        16: 140,  # Final Amount
        17: 140,  # Vehicle Number
        18: 160,  # Transporter Name
        19: 200,  # Remarks
        20: 170,  # Barcode ID
        21: 160,  # Created At
        22: 170,  # Created By
        23: 160,  # Updated At
        24: 170,  # Updated By
    }
    MOVEMENTS_COL_WIDTHS = {
        0: 160, 1: 200, 2: 90, 3: 100, 4: 160, 5: 160,
        6: 170, 7: 140, 8: 170, 9: 150, 10: 130, 11: 140,
    }
    SUMMARY_COL_WIDTHS = {
        0: 220, 1: 130, 2: 140, 3: 80, 4: 140, 5: 150, 6: 160,
    }

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.service = self._initialize_service()
        self.header_map = {name: i for i, name in enumerate(self.BASE_SCHEMA)}
        self._cache_expiry = 0
        self._cached_header_map: Dict = {}

    # ── SERVICE INIT ──────────────────────────────────────────────────────────

    def _initialize_service(self):
        info = get_service_account_info()
        if info:
            try:
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=self.SCOPES)
                print("SheetsService: Initialized with service account info")
                return build("sheets", "v4", credentials=creds)
            except Exception as e:
                print(f"SheetsService: Failed to build from info — {e}")

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
        if not self.service:
            return self.header_map
        if self._cached_header_map and time.time() < self._cache_expiry:
            return self._cached_header_map
        try:
            spreadsheet = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id).execute()
            sheets = spreadsheet.get("sheets", [])
            existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheets}

            self._ensure_sheet("Stock Register",  self.BASE_SCHEMA,      existing)
            self._ensure_sheet("Stock Movements", self.MOVEMENTS_SCHEMA, existing)
            self._ensure_sheet("Stock Summary",   self.SUMMARY_SCHEMA,   existing)

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
                self._apply_full_styles(sheet_id, title, schema)
                if title == "Stock Summary":
                    self._add_chart(sheet_id)
            else:
                result = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=f"{title}!1:1"
                ).execute()
                current_headers = result.get("values", [[]])[0]
                if not current_headers:
                    self._write_headers(title, schema)
                    self._apply_full_styles(sheet_id, title, schema)
                else:
                    # Check for missing columns
                    missing = [c for c in schema if c not in current_headers]
                    if missing:
                        print(f"SheetsService: Adding missing columns {missing} to '{title}'")
                        self._write_headers(title, schema)
                        self._apply_full_styles(sheet_id, title, schema)
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

    # ── FULL STYLE ENGINE ─────────────────────────────────────────────────────

    def _apply_full_styles(self, sheet_id: int, title: str, schema: List[str]):
        """
        Applies the complete design system to a sheet:
          1. Header row — deep colour, bold white, tall row, centred
          2. Column widths — tuned per sheet
          3. Data area — clean font, subtle alternating stripes
          4. Frozen pane — row 1 + first 2 cols (or 1 for Movements)
          5. Borders — thin inner, medium outer
          6. Conditional formats — low stock, IN/OUT type badges
          7. Tab colour — sheet-specific accent
        """
        requests = []

        # ── Resolve sheet-specific design tokens ──────────────────────────────
        if title == "Stock Register":
            hdr_color  = _rgb("register_header")
            tab_color  = {"red": 0.180, "green": 0.380, "blue": 0.620}
            col_widths = self.REGISTER_COL_WIDTHS
            freeze_cols = 2
        elif title == "Stock Movements":
            hdr_color  = _rgb("movements_header")
            tab_color  = {"red": 0.067, "green": 0.490, "blue": 0.440}
            col_widths = self.MOVEMENTS_COL_WIDTHS
            freeze_cols = 1
        else:  # Stock Summary
            hdr_color  = _rgb("summary_header")
            tab_color  = {"red": 0.380, "green": 0.200, "blue": 0.600}
            col_widths = self.SUMMARY_COL_WIDTHS
            freeze_cols = 2

        num_cols = len(schema)

        # ── 1. TAB COLOUR ─────────────────────────────────────────────────────
        requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "tabColor": tab_color,
                    "gridProperties": {
                        "frozenRowCount": 1,
                        "frozenColumnCount": freeze_cols,
                    },
                },
                "fields": "tabColor,gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        })

        # ── 2. HEADER ROW STYLE ───────────────────────────────────────────────
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": hdr_color,
                        "textFormat": {
                            "foregroundColor": _rgb("white"),
                            "bold": True,
                            "fontSize": 10,
                            "fontFamily": "Google Sans",
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                        "padding": {"top": 6, "bottom": 6, "left": 8, "right": 8},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,"
                          "verticalAlignment,wrapStrategy,padding)",
            }
        })

        # ── 3. HEADER ROW HEIGHT (taller for visibility) ──────────────────────
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 38},
                "fields": "pixelSize",
            }
        })

        # ── 4. DATA ROWS — font & base formatting ─────────────────────────────
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "fontSize": 9,
                            "fontFamily": "Google Sans",
                        },
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                        "padding": {"top": 4, "bottom": 4, "left": 8, "right": 8},
                    }
                },
                "fields": "userEnteredFormat(textFormat,verticalAlignment,wrapStrategy,padding)",
            }
        })

        # ── 5. DATA ROW HEIGHT ────────────────────────────────────────────────
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 1000},
                "properties": {"pixelSize": 26},
                "fields": "pixelSize",
            }
        })

        # ── 6. COLUMN WIDTHS ──────────────────────────────────────────────────
        for col_idx, px in col_widths.items():
            if col_idx < num_cols:
                requests.append({
                    "updateDimensionProperties": {
                        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                  "startIndex": col_idx, "endIndex": col_idx + 1},
                        "properties": {"pixelSize": px},
                        "fields": "pixelSize",
                    }
                })

        # ── 7. ALTERNATING STRIPE (even rows) ────────────────────────────────
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 1}],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": "=ISEVEN(ROW())"}],
                        },
                        "format": {"backgroundColor": _rgb("stripe_b")},
                    },
                },
                "index": 0,
            }
        })

        # ── 8. OUTER BORDER (header row) ─────────────────────────────────────
        solid_medium = {
            "style": "SOLID_MEDIUM",
            "color": {"red": 0.6, "green": 0.6, "blue": 0.6},
        }
        solid_thin = {
            "style": "SOLID",
            "color": {"red": 0.82, "green": 0.84, "blue": 0.88},
        }
        requests.append({
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "top":    solid_medium,
                "bottom": solid_medium,
                "left":   solid_medium,
                "right":  solid_medium,
                "innerVertical": {
                    "style": "SOLID",
                    "color": {"red": 0.3, "green": 0.5, "blue": 0.75},
                },
            }
        })

        # Inner vertical borders for data rows
        requests.append({
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "innerVertical": solid_thin,
                "innerHorizontal": {
                    "style": "SOLID",
                    "color": {"red": 0.90, "green": 0.91, "blue": 0.94},
                },
            }
        })

        # ── 9. SHEET-SPECIFIC CONDITIONAL FORMATS ─────────────────────────────

        if title == "Stock Summary":
            # Low stock: Current Balance (col C, index 2) < 10
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 0, "endColumnIndex": num_cols,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": "=$C2<10"}],
                            },
                            "format": {
                                "backgroundColor": _rgb("low_stock_bg"),
                                "textFormat": {
                                    "foregroundColor": _rgb("low_stock_fg"),
                                    "bold": True,
                                },
                            },
                        },
                    },
                    "index": 0,
                }
            })
            # Healthy stock: Current Balance ≥ 50 — subtle green highlight on balance col only
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 2, "endColumnIndex": 3,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "NUMBER_GREATER_THAN_EQ",
                                "values": [{"userEnteredValue": "50"}],
                            },
                            "format": {
                                "backgroundColor": _rgb("in_move_bg"),
                                "textFormat": {
                                    "foregroundColor": _rgb("in_move_fg"),
                                    "bold": True,
                                },
                            },
                        },
                    },
                    "index": 1,
                }
            })

        elif title == "Stock Movements":
            # IN movement rows — soft green
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 0, "endColumnIndex": num_cols,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": '=$C2="IN"'}],
                            },
                            "format": {
                                "backgroundColor": _rgb("in_move_bg"),
                            },
                        },
                    },
                    "index": 0,
                }
            })
            # OUT movement rows — soft amber
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 0, "endColumnIndex": num_cols,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": '=$C2="OUT"'}],
                            },
                            "format": {
                                "backgroundColor": _rgb("out_move_bg"),
                            },
                        },
                    },
                    "index": 1,
                }
            })
            # Type cell itself — bold coloured text
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 2, "endColumnIndex": 3,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "IN"}],
                            },
                            "format": {
                                "textFormat": {
                                    "foregroundColor": _rgb("in_move_fg"),
                                    "bold": True,
                                },
                            },
                        },
                    },
                    "index": 2,
                }
            })
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 2, "endColumnIndex": 3,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "OUT"}],
                            },
                            "format": {
                                "textFormat": {
                                    "foregroundColor": _rgb("out_move_fg"),
                                    "bold": True,
                                },
                            },
                        },
                    },
                    "index": 3,
                }
            })

        elif title == "Stock Register":
            # Highlight rows with no barcode ID (empty col T / index 19)
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": 0, "endColumnIndex": num_cols,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": '=$T2=""'}],
                            },
                            "format": {
                                "backgroundColor": _rgb("out_move_bg"),
                            },
                        },
                    },
                    "index": 0,
                }
            })

        # ── 10. FIRST COLUMN — bold text for product/item names ───────────────
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "bold": True,
                            "fontSize": 9,
                            "fontFamily": "Google Sans",
                        },
                    }
                },
                "fields": "userEnteredFormat.textFormat",
            }
        })

        # ── 11. NUMERIC COLUMNS — right-aligned ───────────────────────────────
        numeric_cols = {
            "Stock Register":  [2, 10, 11, 12, 13, 14, 15],
            "Stock Movements": [3],
            "Stock Summary":   [2, 4, 5],
        }.get(title, [])

        for col in numeric_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": col, "endColumnIndex": col + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "horizontalAlignment": "RIGHT",
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": "#,##0.##",
                                },
                            }
                        },
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                })

        # ── 12. DATE COLUMNS — consistent formatting ──────────────────────────
        date_cols = {
            "Stock Register":  [6, 20, 22],
            "Stock Movements": [0],
            "Stock Summary":   [6],
        }.get(title, [])

        for col in date_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": col, "endColumnIndex": col + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "horizontalAlignment": "CENTER",
                                "numberFormat": {
                                    "type": "DATE_TIME",
                                    "pattern": "dd MMM yyyy HH:mm",
                                },
                            }
                        },
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                })

        # ── EXECUTE ALL REQUESTS ──────────────────────────────────────────────
        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests},
            ).execute()
            print(f"Styles applied: '{title}'")
        except Exception as e:
            print(f"Styling error for '{title}' (ignored): {e}")

    # Keep the old name as a shim so nothing else breaks
    def _apply_styles(self, sheet_id: int, title: str):
        schema = {
            "Stock Register":  self.BASE_SCHEMA,
            "Stock Movements": self.MOVEMENTS_SCHEMA,
            "Stock Summary":   self.SUMMARY_SCHEMA,
        }.get(title, self.BASE_SCHEMA)
        self._apply_full_styles(sheet_id, title, schema)

    # ── CHART ─────────────────────────────────────────────────────────────────

    def _add_chart(self, sheet_id: int):
        """
        Adds a polished dual-column chart on Stock Summary:
          - Column A (Product Name) as domain
          - Column C (Current Balance) as series
          - Column E (Total Received) as series
          - Column F (Total Dispatched) as series
        Positioned to the right of the data area.
        """
        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{
                    "addChart": {
                        "chart": {
                            "spec": {
                                "title": "📦 Inventory Levels Overview",
                                "titleTextFormat": {
                                    "bold": True,
                                    "fontSize": 13,
                                    "fontFamily": "Google Sans",
                                    "foregroundColor": {"red": 0.22, "green": 0.11, "blue": 0.38},
                                },
                                "subtitle": "Current Balance · Received · Dispatched",
                                "subtitleTextFormat": {
                                    "fontSize": 9,
                                    "italic": True,
                                    "foregroundColor": {"red": 0.45, "green": 0.45, "blue": 0.55},
                                },
                                "basicChart": {
                                    "chartType": "COLUMN",
                                    "legendPosition": "BOTTOM_LEGEND",
                                    "headerCount": 1,
                                    "axis": [
                                        {
                                            "position": "BOTTOM_AXIS",
                                            "title": "Product",
                                            "titleTextPosition": {"horizontalAlignment": "CENTER"},
                                        },
                                        {
                                            "position": "LEFT_AXIS",
                                            "title": "Units",
                                            "titleTextPosition": {"horizontalAlignment": "CENTER"},
                                        },
                                    ],
                                    "domains": [{"domain": {"sourceRange": {"sources": [{
                                        "sheetId": sheet_id,
                                        "startRowIndex": 0, "endRowIndex": 500,
                                        "startColumnIndex": 0, "endColumnIndex": 1,  # Product Name
                                    }]}}}],
                                    "series": [
                                        {
                                            "series": {"sourceRange": {"sources": [{
                                                "sheetId": sheet_id,
                                                "startRowIndex": 0, "endRowIndex": 500,
                                                "startColumnIndex": 2, "endColumnIndex": 3,  # Current Balance
                                            }]}},
                                            "targetAxis": "LEFT_AXIS",
                                            "color": {"red": 0.22, "green": 0.59, "blue": 0.44},  # green
                                        },
                                        {
                                            "series": {"sourceRange": {"sources": [{
                                                "sheetId": sheet_id,
                                                "startRowIndex": 0, "endRowIndex": 500,
                                                "startColumnIndex": 4, "endColumnIndex": 5,  # Total Received
                                            }]}},
                                            "targetAxis": "LEFT_AXIS",
                                            "color": {"red": 0.18, "green": 0.38, "blue": 0.72},  # blue
                                        },
                                        {
                                            "series": {"sourceRange": {"sources": [{
                                                "sheetId": sheet_id,
                                                "startRowIndex": 0, "endRowIndex": 500,
                                                "startColumnIndex": 5, "endColumnIndex": 6,  # Total Dispatched
                                            }]}},
                                            "targetAxis": "LEFT_AXIS",
                                            "color": {"red": 0.85, "green": 0.40, "blue": 0.10},  # amber
                                        },
                                    ],
                                    "threeDimensional": False,
                                },
                                "backgroundColor": {"red": 0.98, "green": 0.98, "blue": 1.0},
                                "fontName": "Google Sans",
                            },
                            "position": {
                                "overlayPosition": {
                                    "anchorCell": {
                                        "sheetId": sheet_id,
                                        "rowIndex": 1,
                                        "columnIndex": 8,
                                    },
                                    "widthPixels": 650,
                                    "heightPixels": 380,
                                }
                            },
                        }
                    }
                }]},
            ).execute()
            print("Chart added to Stock Summary")
        except Exception as e:
            print(f"Chart creation error (ignored): {e}")

    # ── STOCK WRITE ───────────────────────────────────────────────────────────

    def save_stock_batch(self, header: Dict, items: List[Dict],
                         item_ids: List[str], user_email: str):
        """
        Ingest a batch of items with extreme efficiency:
        1. Single batchGet for Register & Summary lookups.
        2. In-memory state tracking for updates/appends.
        3. Single batchUpdate for all row writes across all sheets.
        """
        if not self.service:
            return
        
        self.header_map = self._get_or_create_headers()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        
        # ── 1. BULK LOOKUP ────────────────────────────────────────────────────
        try:
            # Fetch lookups for Register (Barcode IDs) and Summary (Product Codes)
            # Use dynamic range for Register
            reg_max_col = self._get_col_letter(len(self.BASE_SCHEMA) - 1)
            lookup_ranges = [f"Stock Register!A:{reg_max_col}", "Stock Summary!A:G"]
            batch_res = self.service.spreadsheets().values().batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=lookup_ranges
            ).execute().get("valueRanges", [])
            
            reg_rows = batch_res[0].get("values", []) if len(batch_res) > 0 else []
            sum_rows = batch_res[1].get("values", []) if len(batch_res) > 1 else []
            
            # Map column names for fast access
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 19)
            code_idx = h.get("Product Code", 1)
            qty_idx  = h.get("Quantity Received", 2)
            
            # Index for fast search
            existing_reg_barcode = {str(r[b_id_idx]).strip(): i+1 for i, r in enumerate(reg_rows) if len(r) > b_id_idx}
            existing_reg_code    = {str(r[code_idx]).strip(): i+1 for i, r in enumerate(reg_rows) if len(r) > code_idx}
            summary_idx          = {str(r[1]).strip(): i+1 for i, r in enumerate(sum_rows) if len(r) > 1}
            
            # Cache summary data in memory for accumulation
            summary_data_map = {}
            for i, r in enumerate(sum_rows):
                if len(r) >= 6:
                    summary_data_map[str(r[1]).strip()] = {
                        "row": i + 1,
                        "name": r[0],
                        "balance": self._to_float(r[2]),
                        "unit": r[3],
                        "received": self._to_float(r[4]),
                        "dispatched": self._to_float(r[5])
                    }
        except Exception as e:
            print(f"Sheets Bulk Lookup Error: {e}")
            existing_reg_barcode = {}; existing_reg_code = {}; summary_idx = {}; summary_data_map = {}

        updates_batch = []  # List of {range, values} for batchUpdate
        movements_append = []
        summary_appends = []
        register_appends = []

        # ── 2. PROCESS ITEMS ──────────────────────────────────────────────────
        processed_summary_codes = set() # Track codes we've updated in this batch

        for i, item in enumerate(items):
            item_id = item_ids[i]
            name = item.get("Product Name") or header.get("Product Name") or "Unknown Item"
            code = str(item.get("Product Code") or header.get("Product Code") or item_id[:8]).strip()
            qty  = self._to_float(item.get("Quantity Received") or header.get("Quantity Received") or 0)
            unit = item.get("Unit") or header.get("Unit") or "PCS"

            # ── A. Register Upsert ──
            row_idx = existing_reg_barcode.get(str(item_id).strip()) or existing_reg_code.get(code)
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            
            if row_idx:
                # Prepare Update for Existing Row (Quantity only)
                old_reg_qty = 0.0
                qty_idx = h.get("Quantity Received", 2)
                if row_idx <= len(reg_rows):
                    old_reg_qty = self._to_float(reg_rows[row_idx-1][qty_idx]) if len(reg_rows[row_idx-1]) > qty_idx else 0.0
                
                updates_batch.append({
                    "range": f"Stock Register!{self._get_col_letter(qty_idx)}{row_idx}",
                    "values": [[old_reg_qty + qty]]
                })
                # Update "Updated At" and "Updated By"
                upd_at_idx = h.get("Updated At", 22)
                upd_by_idx = h.get("Updated By", 23)
                col_range = f"{self._get_col_letter(upd_at_idx)}{row_idx}:{self._get_col_letter(upd_by_idx)}{row_idx}"
                updates_batch.append({
                    "range": f"Stock Register!{col_range}",
                    "values": [[now, user_email]]
                })
            else:
                # Prepare Append for New Row
                row_data = [""] * len(self.BASE_SCHEMA)
                # Primary fields
                row_data[h["Product Name"]] = name
                row_data[h["Product Code"]] = code
                row_data[h["Model Name"]]   = item.get("Model Name") or item.get("mname") or header.get("Model Name") or ""
                row_data[h["Quantity Received"]] = str(qty)
                row_data[h["Unit"]] = unit
                
                # Metadata
                row_data[h["Barcode ID"]] = item_id
                row_data[h["Created At"]] = now
                row_data[h["Created By"]] = user_email
                row_data[h["Updated At"]] = now
                row_data[h["Updated By"]] = user_email
                
                # Dynamic mapping for everything else
                for col_name in self.BASE_SCHEMA:
                    if not row_data[h[col_name]]: # Only if not already set
                        row_data[h[col_name]] = item.get(col_name) or header.get(col_name) or ""
                
                register_appends.append(row_data)

            # ── B. Movement Entry ──
            trans_id = header.get("Invoice Number") or f"TRANS-{str(uuid.uuid4())[:4].upper()}"
            distributions = item.get("distributions", [])
            
            if not distributions:
                movements_append.append([now, name, "IN", qty, "Main Warehouse", "Full Receive", user_email, f"MOV-{uuid.uuid4().hex[:6].upper()}", item_id, trans_id, "N/A", "N/A"])
            else:
                for dist in distributions:
                    d_qty = self._to_float(dist.get("qty", 0))
                    d_loc = dist.get("location", "Main Floor")
                    movements_append.append([now, name, "IN", d_qty, "Main Warehouse", d_loc, user_email, f"MOV-{uuid.uuid4().hex[:6].upper()}", item_id, trans_id, "N/A", "N/A"])

            # ── C. Summary Update ──
            s_entry = summary_data_map.get(code)
            if s_entry:
                s_entry["balance"] += qty
                s_entry["received"] += qty
                updates_batch.append({
                    "range": f"Stock Summary!C{s_entry['row']}:G{s_entry['row']}",
                    "values": [[s_entry["balance"], s_entry["unit"], s_entry["received"], s_entry["dispatched"], now]]
                })
            else:
                summary_appends.append([name, code, qty, unit, qty, 0, now])
                # Add to map so if same code appears twice in batch, we update it rather than append again
                summary_data_map[code] = {"row": len(sum_rows) + len(summary_appends), "name": name, "balance": qty, "unit": unit, "received": qty, "dispatched": 0}

        # ── 3. EXECUTE SHIPMENT ───────────────────────────────────────────────
        try:
            # First, standard value updates (Upserts)
            if updates_batch:
                self.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": updates_batch}
                ).execute()

            # Second, Appends (Done via batchUpdate with AppendCells to be fast if many, or just append)
            for title, data in [("Stock Register", register_appends), ("Stock Movements", movements_append), ("Stock Summary", summary_appends)]:
                if data:
                    self.service.spreadsheets().values().append(
                        spreadsheetId=self.spreadsheet_id,
                        range=f"{title}!A:A",
                        valueInputOption="USER_ENTERED",
                        body={"values": data}
                    ).execute()
                    
            print(f"🚀 BATCH SYNC COMPLETE: {len(items)} items processed.")
        except Exception as e:
            print(f"Sheets Execution Error: {e}")
            traceback.print_exc()

    def sync_batch_to_ledger(self, items: List[Dict], user_email: str) -> bool:
        try:
            item_ids = [item.get("id") or item.get("Barcode ID") for item in items]
            self.save_stock_batch({}, items, item_ids, user_email)
            return True
        except Exception as e:
            print(f"Sheets Sync Error: {e}")
            return False

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
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range="Stock Movements!A:A",
                valueInputOption="USER_ENTERED",
                body={"values": [row]}
            ).execute()
        except Exception as e:
            print(f"Add movement error: {e}")

    # ── READ HELPERS ──────────────────────────────────────────────────────────

    def update_barcode_link(self, barcode_id: str, link: str):
        if not self.service:
            return
        try:
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 19)
            link_idx = h.get("Barcode Link", 4)
            row_idx = self._find_row_by_col(b_id_idx, barcode_id)
            if row_idx != -1:
                col_let = self._get_col_letter(link_idx)
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Stock Register!{col_let}{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[link]]},
                ).execute()
        except Exception as e:
            print(f"Update barcode link error: {e}")
            print(f"Update barcode link error: {e}")

    def update_stock_quantity(self, barcode_id: str, new_qty: float):
        if not self.service:
            return
        try:
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 19)
            qty_idx  = h.get("Quantity Received", 2)
            row_idx = self._find_row_by_col(b_id_idx, barcode_id)
            if row_idx != -1:
                col_let = self._get_col_letter(qty_idx)
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Stock Register!{col_let}{row_idx}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[new_qty]]},
                ).execute()
        except Exception as e:
            print(f"Update stock quantity error: {e}")

    def get_stock_item(self, item_id: str) -> Dict:
        if not self.service:
            return {}
        try:
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 19)
            max_col  = self._get_col_letter(len(self.BASE_SCHEMA) - 1)
            
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"Stock Register!A:{max_col}"
            ).execute().get("values", [])
            for row in result:
                if row and len(row) > b_id_idx and str(row[b_id_idx]) == str(item_id):
                    return {
                        "stock_item_id": row[b_id_idx],
                        "item_name": row[h["Product Name"]],
                        "quantity_remaining": self._to_float(row[h["Quantity Received"]]),
                        "unit": row[h["Unit"]],
                        "supplier_name": row[h["Supplier Name"]],
                        "product_code": row[h["Product Code"]],
                        "model_name": row[h["Model Name"]] if len(row) > h["Model Name"] else ""
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
        col_letter = self._get_col_letter(col_idx)
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


    def refresh_styles(self, title: str):
        """Public method to re-apply UI styling to a specific sheet."""
        if not self.service: return
        try:
            spreadsheet = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
            sheets = {s["properties"]["title"]: s["properties"]["sheetId"] for s in spreadsheet.get("sheets", [])}
            sid = sheets.get(title)
            if sid is not None:
                schema = {
                    "Stock Register":  self.BASE_SCHEMA,
                    "Stock Movements": self.MOVEMENTS_SCHEMA,
                    "Stock Summary":   self.SUMMARY_SCHEMA
                }.get(title, self.BASE_SCHEMA)
                self._apply_full_styles(sid, title, schema)
                print(f"🎨 UI Refresh: Styles applied to '{title}'")
            else:
                print(f"⚠️ Refresh failed: Sheet '{title}' not found.")
        except Exception as e:
            print(f"Refresh error: {e}")

    def _get_col_letter(self, idx: int) -> str:
        """Convert 0-based column index to Excel-style letter (A, B, C...)."""
        result = ""
        while idx >= 0:
            result = chr(65 + (idx % 26)) + result
            idx = (idx // 26) - 1
        return result


sheets_service = SheetsService()
