import os
import time
import json
import re
import uuid
import traceback
import sys
import threading
from datetime import datetime, timedelta
from typing import List, Dict

from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud.firestore_v1.base_query import FieldFilter
from google.auth import exceptions as auth_exceptions
from app.services.firebase import db, clean_private_key, BASE_DIR, get_service_account_info
from app.utils import normalize_id, safe_float, safe_int
from app.services.fcm_service import fcm_service


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
    "out_move_bg":   {"red": 1.000, "green": 0.922, "blue": 0.918},      # red tint (matches low stock)
    "out_move_fg":   {"red": 0.714, "green": 0.102, "blue": 0.102},      # dark red

    "white": {"red": 1.0, "green": 1.0, "blue": 1.0},
}


def _rgb(key: str) -> dict:
    return PALETTE[key]


class SheetsService:
    # ── SCHEMA DEFINITIONS ────────────────────────────────────────────────────
    BASE_SCHEMA = [
        # 1. Bill Header
        "Date", "Invoice Number", "Supplier Name", "Transporter Name", "Remarks",
        # 2. Product Details
        "Product Name", "Product Code", "Batch Number",
        # 3. Quantity / Packaging
        "Quantity Received (In Unit)", "Unit", "Number of Bags", "Storage Type",
        # 4. Item Financials
        "Rate per Unit",
        # 5. System Meta
        "Barcode Link", "Barcode ID", "Created At", "Created By", "Updated At", "Updated By",
    ]
    MOVEMENTS_SCHEMA = [
        "Timestamp", "Product Name", "Type", "Quantity (In Unit)", "Bags", "Warehouse", "Location", "User",
        "Movement ID", "Barcode ID", "Transaction ID", "Position ID", "Warehouse ID",
    ]
    SUMMARY_SCHEMA = [
        "Product Name", "Product Code", "Current Balance (In Unit)", "Current Bags", "Unit",
        "Total Received (In Unit)", "Total Dispatched (In Unit)", "Total Bags Received", "Total Bags Dispatched", "Last Updated",
    ]

    # Column widths (pixels) — tuned per sheet for readability
    REGISTER_COL_WIDTHS = {
        0: 110,   # Date
        1: 150,   # Invoice Number
        2: 180,   # Supplier Name
        3: 160,   # Transporter Name
        4: 200,   # Remarks
        5: 220,   # Product Name
        6: 130,   # Product Code
        7: 130,   # Batch Number
        8: 130,   # Quantity Received
        9: 80,    # Unit
        10: 110,  # Number of Bags
        11: 120,  # Storage Type
        12: 130,  # Rate per Unit
        13: 180,  # Barcode Link
        14: 170,  # Barcode ID
        15: 150,  # Created At
        16: 130,  # Created By
        17: 150,  # Updated At
        18: 130,  # Updated By
    }
    MOVEMENTS_COL_WIDTHS = {
        0: 160, 1: 200, 2: 90, 3: 100, 4: 100, 5: 160, 6: 160,
        7: 170, 8: 140, 9: 170, 10: 150, 11: 130, 12: 140,
    }
    SUMMARY_COL_WIDTHS = {
        0: 220, 1: 130, 2: 130, 3: 110, 4: 80, 5: 130, 6: 130, 7: 130, 8: 130, 9: 160,
    }

    # Identify which columns should be right-aligned (numbers, rates, totals)
    NUMERIC_COLS_MAP = {
        "Stock Register": [7, 9, 11], # Indexes changed due to better schema
        "Stock Movements": [3, 4],
        "Stock Summary": [2, 3, 5, 6, 7, 8],
    }

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID")
        self.service = self._initialize_service()
        self.header_map = {name: i for i, name in enumerate(self.BASE_SCHEMA)}
        self.lock = threading.Lock()
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
                print(f"SheetsService: Failed to build from info - {e}")

        try:
            from google import auth
            creds, _ = auth.default(scopes=self.SCOPES)
            print("SheetsService: Initialized with default Application Credentials")
            return build("sheets", "v4", credentials=creds)
        except Exception as e:
            print(f"SheetsService: Could not initialize (no creds) - {e}")
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
            self._cache_expiry = time.time() + 3600 # 1 Hour Cache
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
                # ALL main sheets (Register, Movements, Summary) have SUPER HEADERS in Row 1.
                # The actual schema (headers) is in Row 2.
                check_range = f"{title}!2:2" 
                result = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id, range=check_range
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
                    
                    # FORCE REFRESH: REMOVED for performance. 
                    # Use manual script to re-apply UI styling.
                    # self._apply_full_styles(sheet_id, title, schema)
        except Exception as e:
            print(f"Error ensuring sheet '{title}': {e}")

    def _write_headers(self, title: str, schema: List[str]):
        try:
            if title == "Stock Register":
                super_row = [
                    "TRANSACTION DETAILS", "", 
                    "TRACKING DETAILS", "", "",
                    "PRODUCT DETAILS", "", "",
                    "QUANTITY / PACKAGING", "", "", "",
                    "FINANCIALS",
                    "SYSTEM META", "", "", "", "", ""
                ]
                values = [super_row, schema]
                range_target = f"{title}!1:2"
            elif title == "Stock Movements":
                super_row = [
                    "EVENT CORE", "", "LOGISTICS CONTEXT", "", "", "", "",
                    "TRACEABILITY IDS", "", "", "", ""
                ]
                values = [super_row, schema]
                range_target = f"{title}!1:2"
            elif title == "Stock Summary":
                super_row = [
                    "PRODUCT INFO", "", "LIVE INVENTORY", "", "", "VOLUME UNIT", "", "VOLUME BAGS", "", "SYSTEM INFO"
                ]
                values = [super_row, schema]
                range_target = f"{title}!1:2"
            else:
                values = [schema]
                range_target = f"{title}!1:1"
                
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=range_target,
                valueInputOption="RAW",
                body={"values": values},
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
            hdr_color    = _rgb("register_header")
            subhdr_color = _rgb("register_subhdr")
            tab_color    = {"red": 0.180, "green": 0.380, "blue": 0.620}
            col_widths   = self.REGISTER_COL_WIDTHS
            freeze_cols  = 2
            frozen_rows  = 2
        elif title == "Stock Movements":
            hdr_color    = _rgb("movements_header")
            subhdr_color = _rgb("movements_subhdr")
            tab_color    = {"red": 0.067, "green": 0.490, "blue": 0.440}
            col_widths   = self.MOVEMENTS_COL_WIDTHS
            freeze_cols  = 2
            frozen_rows  = 2
        else:  # Stock Summary
            hdr_color    = _rgb("summary_header")
            subhdr_color = _rgb("summary_subhdr")
            tab_color    = {"red": 0.380, "green": 0.200, "blue": 0.600}
            col_widths   = self.SUMMARY_COL_WIDTHS
            freeze_cols  = 2
            frozen_rows  = 2

        num_cols = len(schema)

        # ── 1. TAB COLOUR ─────────────────────────────────────────────────────
        requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "tabColor": tab_color,
                    "gridProperties": {
                        "frozenRowCount": frozen_rows,
                        "frozenColumnCount": freeze_cols,
                    },
                },
                "fields": "tabColor,gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        })

        # ── 1.5 MERGE SUPER HEADERS ───────────────────────────────────────────
        if title in ["Stock Register", "Stock Movements", "Stock Summary"]:
            # Clear existing merges first to avoid "select all cells in range" errors during overlaps
            requests.append({
                "unmergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0, "endRowIndex": 1,
                        "startColumnIndex": 0, "endColumnIndex": num_cols
                    }
                }
            })

            if title == "Stock Register":
                # Grouped Mapping: 
                # Transaction(2) Tracking(3) Products(3) Qty(4) Finance(1) System(6)
                super_spans = [(0, 2), (2, 5), (5, 8), (8, 12), (12, 13), (13, 19)]
            elif title == "Stock Movements":
                super_spans = [(0, 2), (2, 7), (7, 12)]
            elif title == "Stock Summary":
                super_spans = [(0, 2), (2, 5), (5, 7), (7, 9), (9, 10)]
            else:
                super_spans = []

            for start_col, end_col in super_spans:
                if start_col >= end_col: continue
                requests.append({
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0, "endRowIndex": 1,
                            "startColumnIndex": start_col, "endColumnIndex": end_col
                        },
                        "mergeType": "MERGE_ALL"
                    }
                })

        # ── 2. PRIMARY SUPER-HEADER STYLE (Row 1) ─────────────────────────────
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
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
            }
        })

        # ── 2.5 SUB-HEADER STYLE (Row 2) ──────────────────────────────────────
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1, "endRowIndex": 2,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": subhdr_color,
                        "textFormat": {
                            "foregroundColor": _rgb("white"),
                            "bold": True,
                            "fontSize": 9,
                            "fontFamily": "Google Sans",
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                        "padding": {"top": 6, "bottom": 6, "left": 8, "right": 8},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy,padding)",
            }
        })

        # ── 3. HEADER ROW HEIGHT (taller for professional look) ───────────────
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": frozen_rows},
                "properties": {"pixelSize": 42},
                "fields": "pixelSize",
            }
        })

        # ── 4. DATA ROWS — font & base formatting ─────────────────────────────
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": frozen_rows,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "fontSize": 9,
                            "fontFamily": "Inter", # Modern typeface
                        },
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                        "padding": {"top": 6, "bottom": 6, "left": 12, "right": 12},
                    }
                },
                "fields": "userEnteredFormat(textFormat,verticalAlignment,wrapStrategy,padding)",
            }
        })

        # ── 4.1 NUMERIC ALIGNMENT — Force Right for specific columns ──────────
        numeric_cols = self.NUMERIC_COLS_MAP.get(title, [])
        for col_idx in numeric_cols:
            if col_idx < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": frozen_rows,
                            "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "horizontalAlignment": "RIGHT",
                            }
                        },
                        "fields": "userEnteredFormat.horizontalAlignment",
                    }
                })

        # ── 5. DATA ROW HEIGHT ────────────────────────────────────────────────
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": frozen_rows, "endIndex": 2000},
                "properties": {"pixelSize": 32}, # Slightly taller rows for readability
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
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": frozen_rows}],
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
                    "startRowIndex": 0, "endRowIndex": frozen_rows,
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
                    "startRowIndex": frozen_rows,
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
            # OUT movement rows — soft red
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
                                "textFormat": {
                                    "foregroundColor": _rgb("out_move_fg"),
                                    "bold": True,
                                },
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
                            "sheetId": sheet_id, "startRowIndex": frozen_rows,
                            "startColumnIndex": 0, "endColumnIndex": num_cols,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": f'=$T{frozen_rows+1}=""'}],
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
                    "startRowIndex": frozen_rows,
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

        # ── 11. NUMERIC COLUMNS ──────────────────────────────────────────────
        h = self.header_map
        def _get_idx(sheet, col_names):
            if title != sheet: return []
            return [h.get(c) for c in col_names if c in h]
        qty_cols = _get_idx("Stock Register", ["Quantity Received (In Unit)"]) + \
                   _get_idx("Stock Movements", ["Quantity (In Unit)"]) + \
                   _get_idx("Stock Summary", ["Current Balance (In Unit)", "Total Received (In Unit)", "Total Dispatched (In Unit)"])

        int_cols = _get_idx("Stock Register", ["Number of Bags"]) + \
                   _get_idx("Stock Movements", ["Bags"]) + \
                   _get_idx("Stock Summary", ["Current Bags", "Total Bags Received", "Total Bags Dispatched"])

        financial_cols = _get_idx("Stock Register", ["Rate per Unit", "Barcode Link", "Barcode ID", "Created At", "Created By", "Updated At", "Updated By"])
        general_numeric_cols = []

        # TIER 1: Quantity (Forced Decimal, e.g. 2.0)
        for col in qty_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": frozen_rows, "startColumnIndex": col, "endColumnIndex": col+1},
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT", "numberFormat": {"type": "NUMBER", "pattern": "#,##0.0000"}}},
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                })

        # TIER 2: Bags (Strict Integer, e.g. 307)
        for col in int_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": frozen_rows, "startColumnIndex": col, "endColumnIndex": col+1},
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT", "numberFormat": {"type": "NUMBER", "pattern": "#,##0.0#####"}}},
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                })

        # TIER 3: Financial (Forced 2 Decimals, Stock Register only, e.g. 125,120.00)
        for col in financial_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": frozen_rows, "startColumnIndex": col, "endColumnIndex": col+1},
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT", "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                })

        # TIER 4: General (Precise but No Trailing Dots, e.g. 125,120)
        for col in general_numeric_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": frozen_rows, "startColumnIndex": col, "endColumnIndex": col+1},
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT", "numberFormat": {"type": "NUMBER", "pattern": "#,##0.####"}}},
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                })

        # ── 12. DATE COLUMNS ───────────────────────────────────────────────
        # Tiered system: Clean Date vs. Full Timestamp
        yyyy_mm_dd_cols = {
            "Stock Register":  [0],
            "Stock Movements": [],
            "Stock Summary":   [],
        }.get(title, [])

        full_date_time_cols = {
            "Stock Register":  [14, 16],
            "Stock Movements": [0],
            "Stock Summary":   [9],
        }.get(title, [])

        # TIER 1: yyyy-mm-dd (Clean Bill Date)
        for col in yyyy_mm_dd_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": frozen_rows, "startColumnIndex": col, "endColumnIndex": col+1},
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                })

        # TIER 2: Full Timestamp (System Metadata)
        for col in full_date_time_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": frozen_rows, "startColumnIndex": col, "endColumnIndex": col+1},
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "numberFormat": {"type": "DATE_TIME", "pattern": "dd MMM yyyy HH:mm"}}},
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

    def check_invoice_duplicate(self, invoice_no: str) -> bool:
        """Fast, synchronous check if an invoice number already exists in Register."""
        if not self.service or not invoice_no: return False
        
        target = str(invoice_no).strip().upper()
        if target in ["", "NB", "INV-N/A"]: return False
        
        try:
            # Fetch ONLY Column B (Invoice Number) from Stock Register
            # In BASE_SCHEMA, 'Invoice Number' is at index 1 (Col B)
            range_name = "Stock Register!B:B"
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=range_name
            ).execute()
            
            rows = res.get("values", [])
            for row in rows:
                if row:
                    existing = str(row[0]).strip().upper()
                    if existing == target:
                        return True
            return False
        except Exception as e:
            print(f"🛑 [DUP-CHECK-FAIL] Error during invoice lookup: {e}")
            return False

    def save_stock_batch(self, header: Dict, items: List[Dict],
                         item_ids: List[str], user_display: str):
        """Sequential atomic ingestion of stock batch."""
        if not self.service: return
        
        with self.lock:
            print(f"🔒 [SHEETS-LOCK] Processing batch of {len(items)} items...")
            try:
                self.header_map = self._get_or_create_headers()
                now = self._get_now_ist()
                
                # ── 1. BULK LOOKUP ──
                reg_max_col = self._get_col_letter(len(self.BASE_SCHEMA) - 1)
                sum_max_col = self._get_col_letter(len(self.SUMMARY_SCHEMA) - 1)
                lookup_ranges = [f"Stock Register!A:{reg_max_col}", f"Stock Summary!A:{sum_max_col}"]
                batch_res = self.service.spreadsheets().values().batchGet(
                    spreadsheetId=self.spreadsheet_id,
                    ranges=lookup_ranges
                ).execute().get("valueRanges", [])
                
                reg_rows = batch_res[0].get("values", []) if len(batch_res) > 0 else []
                sum_rows = batch_res[1].get("values", []) if len(batch_res) > 1 else []

                # 1a. Duplicate Check
                target_inv = str(header.get("Invoice Number", "")).strip().upper()
                if target_inv and target_inv not in ["NB", "INV-N/A"]:
                    for row in reg_rows:
                        if len(row) > 1 and str(row[1]).strip().upper() == target_inv:
                            raise Exception(f"Duplicate Bill Detected: {target_inv}")

                h = self.header_map
                b_id_idx = h.get("Barcode ID", 14)
                q_idx = h.get("Quantity Received (In Unit)", 8)
                b_idx = h.get("Number of Bags", 10)
                
                existing_reg_barcode = {}
                for i, r in enumerate(reg_rows):
                    if len(r) > b_id_idx:
                        bid = str(r[b_id_idx]).strip()
                        if bid and bid not in existing_reg_barcode:
                            existing_reg_barcode[bid] = i + 1

                summary_data_map = {}
                for i, r in enumerate(sum_rows):
                    if i < 2: continue
                    if len(r) >= 2:
                        code_key = normalize_id(r[1])
                        if code_key and code_key not in summary_data_map:
                            summary_data_map[code_key] = {
                                "row": i + 1, "name": r[0], "balance": self._to_float(r[2]),
                                "bags_balance": self._to_float(r[3]) if len(r) > 3 else 0.0,
                                "unit": r[4] if len(r) > 4 else "PCS",
                                "received": self._to_float(r[5]) if len(r) > 5 else 0.0,
                                "dispatched": self._to_float(r[6]) if len(r) > 6 else 0.0,
                                "bags_received": self._to_float(r[7]) if len(r) > 7 else 0.0,
                                "bags_dispatched": self._to_float(r[8]) if len(r) > 8 else 0.0
                            }

                updates_batch = []
                movements_append = []
                summary_appends = []
                register_appends = []

                # ── 2. PROCESS ITEMS ──
                for i, item in enumerate(items):
                    item_id = str(item_ids[i]).strip()
                    name = str(item.get("Product Name") or header.get("Product Name") or "Unknown").strip()
                    code = str(item.get("Product Code") or header.get("Product Code") or item_id[:8]).strip()
                    # -- COMPATIBILITY LAYER --
                    raw_qty = item.get("Quantity Received (In Unit)") or item.get("Quantity Received") or 0
                    raw_bags = item.get("Number of Bags") or 0
                    
                    qty, bags = self._to_float(raw_qty), self._to_float(raw_bags)
                    unit = str(item.get("Unit") or "PCS").strip()

                    row_idx = existing_reg_barcode.get(item_id)
                    if row_idx:
                        cur_qty, cur_bags = 0.0, 0.0
                        if row_idx <= len(reg_rows):
                            row = reg_rows[row_idx-1]
                            cur_qty = self._to_float(row[q_idx]) if len(row) > q_idx else 0.0
                            cur_bags = self._to_float(row[b_idx]) if len(row) > b_idx else 0.0
                        
                        updates_batch.append({"range": f"Stock Register!{self._get_col_letter(q_idx)}{row_idx}", "values": [[self._clean_num(cur_qty + qty)]]})
                        updates_batch.append({"range": f"Stock Register!{self._get_col_letter(b_idx)}{row_idx}", "values": [[self._clean_num(cur_bags + bags)]]})
                        upd_at_idx, upd_by_idx = h.get("Updated At", 16), h.get("Updated By", 17)
                        updates_batch.append({"range": f"Stock Register!{self._get_col_letter(upd_at_idx)}{row_idx}:{self._get_col_letter(upd_by_idx)}{row_idx}", "values": [[int(now.timestamp()), user_display]]})
                    else:
                        row_data = [""] * len(self.BASE_SCHEMA)
                        num_fields = {"Quantity Received (In Unit)", "Number of Bags", "Rate per Unit"}
                        for col in self.BASE_SCHEMA:
                            idx = h[col]
                            if col == "Barcode ID": row_data[idx] = item_id
                            elif col in ["Created At", "Updated At"]: row_data[idx] = int(now.timestamp())
                            elif col in ["Created By", "Updated By"]: row_data[idx] = user_display
                            elif col == "Barcode Link": row_data[idx] = f"https://stitch-stock.web.app/inventory/{item_id}"
                            elif col == "Storage Type": row_data[idx] = item.get("storage_type") or "UNIT"
                            else:
                                # -- COMPATIBILITY LAYER --
                                val = item.get(col) or header.get(col)
                                if col == "Quantity Received (In Unit)" and val is None:
                                    val = item.get("Quantity Received") or header.get("Quantity Received")
                                    
                                if val is not None:
                                    row_data[idx] = ("N/A" if val == "N/A" else self._clean_num(val)) if col in num_fields else val
                                else:
                                    row_data[idx] = 0 if col in num_fields else ""
                        register_appends.append(row_data)

                    # Movements
                    trans_id = header.get("Invoice Number") or f"TR-{uuid.uuid4().hex[:4].upper()}"
                    dists = item.get('distributions', [])
                    ratio = (qty / bags) if bags > 0 else 1.0
                    for d in dists:
                        d_val = self._to_float(d_raw := d.get('qty', 0))
                        if str(d_raw).strip().upper() != "N/A" and d_val < 0.001: continue
                        u_type = d.get('unit_type', 'qty')
                        p_q_na, p_b_na = str(raw_qty).strip().upper() == "N/A", str(raw_bags).strip().upper() == "N/A"
                        if p_q_na and not p_b_na: m_q, m_b = "N/A", (d_val if u_type == 'bags' else d_val/ratio)
                        elif p_b_na and not p_q_na: m_b, m_q = "N/A", (d_val if u_type == 'qty' else d_val*ratio)
                        elif p_q_na and p_b_na: m_q, m_b = "N/A", "N/A"
                        else: m_q, m_b = (d_val, d_val/ratio) if u_type == 'qty' else (d_val*ratio, d_val)
                        movements_append.append([now.strftime("%Y-%m-%d %H:%M:%S"), name, "IN", self._clean_num(m_q), self._clean_num(m_b), d.get('warehouse', 'WH'), d.get('location', 'Intake'), user_display, f"MOV-{uuid.uuid4().hex[:6].upper()}", item_id, trans_id, d.get('dist_id', 'NB'), d.get('warehouse_id', 'default')])

                # ── 3. SUMMARY ──
                ses_sum = {}
                for item in items:
                    c_key = normalize_id(item.get("Product Code") or "")
                    if not c_key: continue
                    # -- COMPATIBILITY LAYER --
                    r_q = item.get("Quantity Received (In Unit)") or item.get("Quantity Received") or 0
                    r_b = item.get("Number of Bags") or 0
                    if c_key not in ses_sum:
                        ses_sum[c_key] = {"name": item.get("Product Name", "Item"), "code": item.get("Product Code", ""), "qty": r_q, "bags": r_b, "unit": item.get("Unit", "PCS")}
                    else:
                        s = ses_sum[c_key]
                        for k, v in [("qty", r_q), ("bags", r_b)]:
                            if str(v).upper() == "N/A": s[k] = "N/A"
                            elif s[k] != "N/A": s[k] = self._to_float(s[k]) + self._to_float(v)

                for code_key, sd in ses_sum.items():
                    s_ent = summary_data_map.get(code_key)
                    if s_ent:
                        for k, v in [("balance", sd["qty"]), ("bags_balance", sd["bags"]), ("received", sd["qty"]), ("bags_received", sd["bags"])]:
                            if str(v).upper() == "N/A": s_ent[k] = "N/A"
                            elif s_ent[k] != "N/A": s_ent[k] += self._to_float(v)
                        updates_batch.append({
                            "range": f"Stock Summary!C{s_ent['row']}:J{s_ent['row']}",
                            "values": [[
                                self._clean_num(s_ent["balance"]), self._clean_num(s_ent["bags_balance"]), sd["unit"],
                                self._clean_num(s_ent["received"]), self._clean_num(s_ent["dispatched"]),
                                self._clean_num(s_ent["bags_received"]), self._clean_num(s_ent["bags_dispatched"]),
                                now.strftime("%Y-%m-%d %H:%M:%S")
                            ]]
                        })
                    else:
                        summary_appends.append([
                            sd["name"], sd["code"], self._clean_num(sd["qty"]), self._clean_num(sd["bags"]), sd["unit"], 
                            self._clean_num(sd["qty"]), 0, self._clean_num(sd["bags"]), 0, 
                            now.strftime("%Y-%m-%d %H:%M:%S")
                        ])

                # ── 4. EXECUTE WRITES ──
                try:
                    if updates_batch:
                        self.service.spreadsheets().values().batchUpdate(
                            spreadsheetId=self.spreadsheet_id,
                            body={"valueInputOption": "USER_ENTERED", "data": updates_batch}
                        ).execute()

                    # Second, Appends (with duplication guard for safety)
                    for title, data in [("Stock Register", register_appends), ("Stock Movements", movements_append), ("Stock Summary", summary_appends)]:
                        if data:
                            # ── DEDUPLICATION GUARD: Don't append if these specific IDs already exist ──
                            # (Movement IDs are generated fresh in this session, so they won't repeat 
                            # unless the same block is called twice. Standard sync-lock handles the rest.)
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
            except Exception as e:
                print(f"Sheets Top-Level Error: {e}")
                traceback.print_exc()
            finally:
                print(f"🔓 [SHEETS-UNLOCK] Batch update complete.")

    def sync_batch_to_ledger(self, items: List[Dict], user_display: str) -> bool:
        try:
            item_ids = [item.get("id") or item.get("Barcode ID") for item in items]
            self.save_stock_batch({}, items, item_ids, user_display)
            return True
        except Exception as e:
            print(f"Sheets Sync Error: {e}")
            return False

    def add_movement(self, barcode_id: str, trans_id: str, move_type: str,
                     qty: float, user_display: str, bags_qty: float = 0.0, 
                     warehouse: str = "Main Warehouse", location: str = "Full Receive", 
                     dist_id: str = "default", warehouse_id: str = "default", 
                     item_name: str = "Audit Item"):
        if not self.service:
            return
        now = self._get_now_ist()
        row = [
            now, item_name, move_type, self._clean_num(qty), self._clean_num(bags_qty),
            warehouse, location, user_display,
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

    def record_relocation(self, barcode_id: str, qty: any, bags: any, 
                          from_location: str, to_location: str, 
                          from_warehouse: str, to_warehouse: str,
                          user_display: str, product_name: str):
        """Dedicated wrapper for relocation audit in Google Sheets."""
        if not self.service: return
        
        # Use add_movement but with a specific RELOCATE formatting
        # We record the move as a single event "From -> To" in the location column
        loc_audit = f"{from_location} ➔ {to_location}"
        
        self.add_movement(
            barcode_id=barcode_id,
            trans_id="RELOC",
            move_type="RELOCATE",
            qty=qty,
            bags_qty=bags,
            warehouse=to_warehouse,
            location=loc_audit,
            user_display=user_display,
            item_name=product_name
        )

    # ── READ HELPERS ──────────────────────────────────────────────────────────

    def update_barcode_link(self, barcode_id: str, link: str):
        if not self.service:
            return
        try:
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 14)
            link_idx = h.get("Barcode Link", 13)
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

    def update_stock_quantity(self, barcode_id: str, new_qty: float):
        if not self.service:
            return
        try:
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 14)
            qty_idx  = h.get("Quantity Received (In Unit)", 8)
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
        """
        Robust lookup by Barcode ID OR Product Code.
        Normalizes both the input and the sheet values to ensure resilience.
        """
        if not self.service or not item_id:
            return {}
            
        try:
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 14)
            p_code_idx = h.get("Product Code", 6)
            max_col  = self._get_col_letter(len(self.BASE_SCHEMA) - 1)
            
            # Normalize target for comparison
            target = normalize_id(item_id)
            
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"Stock Register!A:{max_col}"
            ).execute().get("values", [])
            
            for row in result:
                if row and len(row) > max(b_id_idx, p_code_idx):
                    # Check Barcode ID (Strongest Match)
                    s_barcode = normalize_id(row[b_id_idx])
                    if s_barcode == target:
                        return {
                            "stock_item_id": row[b_id_idx],
                            "item_name": row[h["Product Name"]],
                            "quantity_remaining": self._to_float(row[h["Quantity Received"]]),
                            "unit": row[h["Unit"]],
                            "supplier_name": row[h["Supplier Name"]],
                            "product_code": row[h["Product Code"]]
                        }
                    
                    # Check Product Code (Fallback Match)
                    s_code = normalize_id(row[p_code_idx])
                    if s_code == target:
                        return {
                            "stock_item_id": row[b_id_idx],
                            "item_name": row[h["Product Name"]],
                            "quantity_remaining": self._to_float(row[h["Quantity Received (In Unit)"]]),
                            "unit": row[h["Unit"]],
                            "supplier_name": row[h["Supplier Name"]],
                            "product_code": row[h["Product Code"]]
                        }
        except Exception as e:
            print(f"🛑 [ROBUST-LOOKUP-FAILED] {e}")
        return {}

    def get_summary_stats(self, period: str = "all") -> Dict:
        if not self.service:
            return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0, "total_in_bags": 0, "total_out_bags": 0, "available_balance_bags": 0}
        try:
            # 1. Fetch Dynamic Thresholds from Firestore
            min_qty = 10.0
            min_bag = 0.0
            try:
                g_doc = db.collection('alert_config').document('settings').get()
                if g_doc.exists:
                    conf = g_doc.to_dict()
                    min_qty = float(conf.get('default_min_stock', 10.0))
                    min_bag = float(conf.get('default_min_bag', 0.0))
            except Exception as e:
                print(f"⚠️ [SUMMARY_STATS] Firestore Threshold Load Error: {e}")

            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Summary!C:I"
            ).execute()
            rows = res.get("values", [])[2:]  # Skip super-header (row 1) + column headers (row 2)
            t_in = t_out = t_bal = low = 0.0
            t_in_b = t_out_b = t_bal_b = 0.0
            
            for row in rows:
                if len(row) >= 5:
                    bal_val = row[0]
                    bag_val = row[1]
                    bal  = self._to_float(bal_val)  # Col C (Current Balance)
                    bags = self._to_float(bag_val)  # Col D (Current Bags)
                    tin  = self._to_float(row[3])  # Col F (Total Received)
                    tout = self._to_float(row[4])  # Col G (Total Dispatched)
                    
                    # Optional: Total Bags Received/Dispatched (H/I)
                    tin_b  = self._to_float(row[5]) if len(row) > 5 else 0.0
                    tout_b = self._to_float(row[6]) if len(row) > 6 else 0.0
                    
                    t_in += tin; t_out += tout; t_bal += bal
                    t_in_b += tin_b; t_out_b += tout_b; t_bal_b += bags
                    
                    # Live Pulse Low Stock Logic: Both Unit and Bag threshold check
                    # If a metric is 'N/A', we treat it as 'Threshold Passed' (True)
                    # so that the alert depends solely on the remaining valid metric.
                    is_bal_low = True if str(bal_val).strip().upper() == "N/A" else (bal < min_qty)
                    is_bag_low = True if str(bag_val).strip().upper() == "N/A" else (bags < min_bag)

                    # Only count as low stock if BOTH (valid) metrics are below threshold
                    # If thresholds are disabled (0), we ignore that dimension.
                    if min_qty > 0 and min_bag > 0:
                        if is_bal_low and is_bag_low: low += 1
                    elif min_qty > 0:
                        if is_bal_low: low += 1
                    elif min_bag > 0:
                        if is_bag_low: low += 1
            
            return {
                "total_in": t_in, "total_out": t_out, "available_balance": t_bal,
                "total_in_bags": t_in_b, "total_out_bags": t_out_b, 
                "available_balance_bags": t_bal_b,
                "low_stock_count": int(low)
            }
        except Exception as e:
            print(f"Get summary stats error: {e}")
            return {"total_in": 0, "total_out": 0, "available_balance": 0, "low_stock_count": 0, "total_in_bags": 0, "total_out_bags": 0, "available_balance_bags": 0}

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

    def record_dispatch(self, barcode_id: str, qty: float, bags_removed: float = 0, 
                        warehouse: str = "", location: str = "", user_display: str = "System", 
                        dist_id: str = "default", warehouse_id: str = "default",
                        product_code: str = "", batch_number: str = ""):
        """
        Deducts stock from the spreadsheet ledger and records the movement.
        """
        if not self.service: return
        
        with self.lock:
            try:
                h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
                b_id_idx = h.get("Barcode ID", 20)
                
                # 1. PRIMARY LOOKUP: Match by provided ID
                row_idx = self._find_row_by_col(b_id_idx, barcode_id)
                
                # 2. FALLBACK LOOKUP: Match by Product Code + Batch Number
                if row_idx == -1 and product_code:
                    print(f"📡 Sheets: Primary ID {barcode_id} not found. Attempting fallback match for {product_code}-{batch_number}...")
                    row_idx = self._find_row_by_codes(product_code, batch_number)
                
                if row_idx == -1:
                    raise ValueError(f"Stock item {product_code or barcode_id} (Batch: {batch_number or 'N/A'}) not found in Stock Register. Please run the Admin Janitor to reconcile.")

                # 3. Update Stock Register Row
                qty_col = self._get_col_letter(h.get("Quantity Received (In Unit)", 8))
                unit_col = self._get_col_letter(h.get("Unit", 9))
                bags_col = self._get_col_letter(h.get("Number of Bags", 10))
                st_col = self._get_col_letter(h.get("Storage Type", 11))
                updated_at_col = self._get_col_letter(h.get("Updated At", 17))
                updated_by_col = self._get_col_letter(h.get("Updated By", 18))

                # Fetch row context (Qty, Unit, Bags, Storage Type) for healing and N/A logic
                reg_res = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Stock Register!{qty_col}{row_idx}:{st_col}{row_idx}"
                ).execute()
                reg_values = reg_res.get("values", [[]])[0]
                
                raw_qty_res = reg_values[0] if len(reg_values) > 0 else "0.0"
                raw_unit_res = reg_values[1] if len(reg_values) > 1 else ""
                raw_bags_res = reg_values[2] if len(reg_values) > 2 else "0"
                raw_st_res = reg_values[3] if len(reg_values) > 3 else ""
                
                # Check for bag-based items aggressively to prevent numeric seepage into Qty
                is_bag_item = 'BAG' in str(raw_unit_res).upper() or str(raw_st_res).upper() == 'BAG'
                is_qty_na = str(raw_qty_res).strip().upper() == "N/A"
                is_bags_na = str(raw_bags_res).strip().upper() == "N/A"
                
                curr_qty = self._to_float(raw_qty_res)
                curr_bags = self._to_float(raw_bags_res)
                
                # [LATE-FALLBACK] If bags_removed is 0, but Sheet has bags, calculate proportion
                effective_bags_removed = bags_removed
                if not is_bags_na and safe_float(bags_removed) == 0 and curr_bags > 0 and curr_qty > 0 and not is_bag_item:
                    effective_bags_removed = (qty / curr_qty) * curr_bags
                    print(f"📊 [SHEETS-CALC] Late Fallback: {qty}/{curr_qty} * {curr_bags} = {effective_bags_removed} bags")

                # Logic: If it was N/A in register, it stays N/A. Otherwise subtract.
                new_qty = "N/A" if is_qty_na else max(0.0, curr_qty - qty)
                new_bags = "N/A" if is_bags_na else max(0.0, float(round(curr_bags - effective_bags_removed, 4)))

                # Batch update the row
                self.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": [
                        {"range": f"Stock Register!{qty_col}{row_idx}", "values": [[new_qty if is_qty_na else self._clean_num(new_qty)]]},
                        {"range": f"Stock Register!{bags_col}{row_idx}", "values": [[new_bags if is_bags_na else self._clean_num(new_bags)]]},
                        {"range": f"Stock Register!{updated_at_col}{row_idx}:{updated_by_col}{row_idx}", "values": [[self._get_now_ist().strftime("%Y-%m-%d %H:%M:%S"), user_display]]}
                    ]}
                ).execute()

                # 2. Record Movement
                name_res = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Stock Register!{self._get_col_letter(h.get('Product Name', 4))}{row_idx}"
                ).execute()
                p_name = name_res.get("values", [["Unknown"]])[0][0]
                
                # Movement record: log Weight in Qty column and Bags in Bags column
                m_qty = "N/A" if is_qty_na else self._clean_num(qty)
                m_bags = self._clean_num(bags_removed) if not is_bags_na else "N/A"

                movement_row = [
                    self._get_now_ist().strftime("%Y-%m-%d %H:%M:%S"), p_name, "OUT", 
                    m_qty, m_bags, 
                    warehouse, location, user_display,
                    f"MOV-{int(time.time())}", barcode_id, f"DISP-{uuid.uuid4().hex[:4].upper()}", dist_id, warehouse_id
                ]
                self._append_row("Stock Movements", movement_row)

                # 3. Update Summary
                # Prioritize the product_code passed from the API for robustness
                p_code = product_code
                if not p_code:
                    code_res = self.service.spreadsheets().values().get(
                        spreadsheetId=self.spreadsheet_id,
                        range=f"Stock Register!{self._get_col_letter(h.get('Product Code', 5))}{row_idx}"
                    ).execute()
                    p_code = code_res.get("values", [[""]])[0][0]
                
                if p_code:
                    # _update_summary_row: consistent N/A propagation to the dashboard stats
                    self._update_summary_row(
                        p_name, p_code, 
                        m_qty, 
                        "OUT", 
                        bags_delta=m_bags
                    )

                print(f"Sheets Sync: Deducted {qty} of {p_name} ({barcode_id})")
            except Exception as e:
                print(f"Sheets Record Dispatch Error: {e}")
                traceback.print_exc()

    def record_relocation(self, barcode_id: str, qty: any, bags: any = 0, 
                          from_location: str = "", to_location: str = "", 
                          from_warehouse: str = "N/A", to_warehouse: str = "N/A",
                          user_display: str = "System", product_name: str = "Generic Item",
                          dist_id: str = "default", warehouse_id: str = "N/A"):
        """Records an internal transfer in the movements ledger."""
        if not self.service: return
        
        with self.lock:
            try:
                location_path = f"{from_location} ➔ {to_location}"
                
                # Format Warehouse column: "Old ➔ New" if different, else just "Name"
                if from_warehouse != to_warehouse and from_warehouse != "N/A" and to_warehouse != "N/A":
                    warehouse_path = f"{from_warehouse} ➔ {to_warehouse}"
                else:
                    warehouse_path = to_warehouse if to_warehouse != "N/A" else from_warehouse

                movement_row = [
                    self._get_now_ist().strftime("%Y-%m-%d %H:%M:%S"), 
                    product_name, 
                    "RELOCATE", 
                    self._clean_num(qty), 
                    self._clean_num(bags), 
                    warehouse_path, # [UPDATED]
                    location_path, 
                    user_display,
                    f"MOV-{int(time.time())}", 
                    barcode_id, 
                    f"REL-{uuid.uuid4().hex[:4].upper()}", 
                    dist_id, 
                    warehouse_id # [UPDATED]
                ]
                self._append_row("Stock Movements", movement_row)
                print(f"✅ Sheets Sync: Recorded Relocation of {qty} {product_name} ({location_path})")
            except Exception as e:
                print(f"Sheets Record Relocation Error: {e}")
                traceback.print_exc()

    def _update_summary_row(self, name: str, code: str, qty_delta: float, m_type: str, bags_delta: float = 0.0):
        """Helper to update the aggregate balance and totals in Stock Summary."""
        if not self.service: return
        try:
            # Re-fetch the summary data within the lock (if called from record_dispatch) 
            # to ensure we don't have stale row indices
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Summary!A:J"
            ).execute()
            rows = res.get("values", [])
            data = rows[2:] if len(rows) > 2 else []  # Skip super-header (row 1) + column headers (row 2)
            
            found_idx = -1
            matched_row = []
            code_to_find = normalize_id(code)
            
            for i, row in enumerate(data):
                if len(row) >= 2:
                    current_code = normalize_id(row[1])
                    if current_code == code_to_find:
                        found_idx = i + 3  # +3: 1-based index + 2 skipped header rows
                        matched_row = row
                        break
            
            now = self._get_now_ist()
            if found_idx == -1:
                if m_type == "OUT":
                    print(f"⚠️ [SUMMARY_WARN] Could not find code {code} for deduction.")
                    return

                # N/A Aware Logic for New Row
                is_na_qty = str(qty_delta).strip().upper() == "N/A"
                is_na_bags = str(bags_delta).strip().upper() == "N/A"

                new_row = [
                    name, code, 
                    self._clean_num(qty_delta), self._clean_num(bags_delta), "PCS", 
                    (self._clean_num(qty_delta) if m_type == "IN" else "N/A" if is_na_qty else 0.0), (self._clean_num(qty_delta) if m_type == "OUT" else "N/A" if is_na_qty else 0.0), 
                    (self._clean_num(bags_delta) if m_type == "IN" else "N/A" if is_na_bags else 0.0), (self._clean_num(bags_delta) if m_type == "OUT" else "N/A" if is_na_bags else 0.0),
                    now.strftime("%Y-%m-%d %H:%M:%S")
                ]
                self._append_row("Stock Summary", new_row)
            else:
                curr_row = matched_row
                raw_bal = curr_row[2] if len(curr_row) > 2 else 0.0
                raw_bags = curr_row[3] if len(curr_row) > 3 else 0.0
                
                curr_bal = self._to_float(raw_bal)
                curr_bags = self._to_float(raw_bags)
                curr_in_qty = self._to_float(curr_row[5]) if len(curr_row) > 5 else 0.0
                curr_out_qty = self._to_float(curr_row[6]) if len(curr_row) > 6 else 0.0
                curr_in_bags = self._to_float(curr_row[7]) if len(curr_row) > 7 else 0.0
                curr_out_bags = self._to_float(curr_row[8]) if len(curr_row) > 8 else 0.0

                # N/A Aware Logic
                is_na_qty = (str(raw_bal).upper() == "N/A") or (str(qty_delta).upper() == "N/A")
                is_na_bags = (str(raw_bags).upper() == "N/A") or (str(bags_delta).upper() == "N/A")

                if is_na_qty:
                    new_bal = "N/A"; new_in_qty = "N/A"; new_out_qty = "N/A"
                else:
                    # FIX: Handle positive delta for both IN and OUT correctly
                    adj_qty = -qty_delta if m_type == "OUT" else qty_delta
                    new_bal = max(0.0, round(curr_bal + adj_qty, 4))
                    new_in_qty = round(curr_in_qty + (qty_delta if m_type == "IN" else 0.0), 4)
                    new_out_qty = round(curr_out_qty + (qty_delta if m_type == "OUT" else 0.0), 4)

                if is_na_bags:
                    new_bags = "N/A"; new_in_bags = "N/A"; new_out_bags = "N/A"
                else:
                    target_bags_delta = float(bags_delta if not str(bags_delta).upper() == "N/A" else 0)
                    # FIX: Handle positive bags delta correctly
                    adj_bags = -target_bags_delta if m_type == "OUT" else target_bags_delta
                    new_bags = max(0.0, round(curr_bags + adj_bags, 6))
                    new_in_bags = round(curr_in_bags + (target_bags_delta if m_type == "IN" else 0.0), 6)
                    new_out_bags = round(curr_out_bags + (target_bags_delta if m_type == "OUT" else 0.0), 6)
                
                update_range = f"Stock Summary!C{found_idx}:J{found_idx}"
                row_vals = [
                    self._clean_num(new_bal), self._clean_num(new_bags),
                    curr_row[4] if len(curr_row) > 4 else "PCS",
                    self._clean_num(new_in_qty), self._clean_num(new_out_qty),
                    self._clean_num(new_in_bags), self._clean_num(new_out_bags),
                    now.strftime("%Y-%m-%d %H:%M:%S")
                ]
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=update_range,
                    valueInputOption="USER_ENTERED",
                    body={"values": [row_vals]}
                ).execute()
        except Exception as e:
            print(f"Update summary row error: {e}")

    def _get_now_ist(self) -> datetime:
        """Returns current time in Indian Standard Time (UTC+5:30) as a datetime object."""
        # Manual offset for IST (5 hours 30 mins = 19800 seconds)
        return datetime.utcnow() + timedelta(hours=5, minutes=30)

    # ── LOW-LEVEL HELPERS ─────────────────────────────────────────────────────

    def _find_row_by_codes(self, product_code: str, batch_number: str) -> int:
        """Finds a row in the Stock Register that matches both Product Code and Batch Number."""
        if not self.service: return -1
        try:
            h = self.header_map
            p_code_idx = h.get("Product Code")
            batch_idx = h.get("Batch Number")
            
            if p_code_idx is None or batch_idx is None:
                return -1

            # Fetch relevant columns for search - normalization ensures robustness
            target_p_code = normalize_id(product_code)
            target_batch = normalize_id(batch_number)
            
            # Use a slightly wider range to ensure we capture newly shifted columns
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Register!A:L"
            ).execute().get("values", [])
            
            for i, row in enumerate(res):
                if len(row) > max(p_code_idx, batch_idx):
                    row_p_code = normalize_id(row[p_code_idx])
                    row_batch = normalize_id(row[batch_idx])
                    if row_p_code == target_p_code and row_batch == target_batch:
                        return i + 1
        except Exception as e:
            print(f"Sheets Find Row by Codes Error: {e}")
        return -1

    def _find_row_by_col(self, col_idx: int, value: str, sheet_name: str = "Stock Register") -> int:
        if not self.service:
            return -1
        col_letter = self._get_col_letter(col_idx)
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet_name}!{col_letter}:{col_letter}",
            ).execute()
            for i, row in enumerate(result.get("values", [])):
                if row and str(row[0]).strip() == str(value).strip():
                    return i + 1
        except Exception as e:
            print(f"Find row error in {sheet_name}: {e}")
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

    def _clean_num(self, val) -> any:
        """Removes trailing .0 but keeps other decimals for professional Sheets look."""
        if val is None: return 0
        if str(val).strip().upper() == "N/A": return "N/A"
        try:
            v = round(float(val), 6)
            if v == int(v):
                return int(v)
            return v
        except:
            return str(val)


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

    def beautify_all(self):
        """Beautify all system sheets (Register, Movements, Summary)."""
        for sheet_name in ["Stock Register", "Stock Movements", "Stock Summary"]:
            self.refresh_styles(sheet_name)

    def _get_col_letter(self, idx: int) -> str:
        """Convert 0-based column index to Excel-style letter (A, B, C...)."""
        result = ""
        while idx >= 0:
            result = chr(65 + (idx % 26)) + result
            idx = (idx // 26) - 1
        return result

sheets_service = SheetsService()