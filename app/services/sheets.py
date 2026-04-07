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
    "out_move_bg":   {"red": 1.000, "green": 0.922, "blue": 0.918},      # red tint (matches low stock)
    "out_move_fg":   {"red": 0.714, "green": 0.102, "blue": 0.102},      # dark red

    "white": {"red": 1.0, "green": 1.0, "blue": 1.0},
}


def _rgb(key: str) -> dict:
    return PALETTE[key]


class SheetsService:
    # ── SCHEMA DEFINITIONS ────────────────────────────────────────────────────
    BASE_SCHEMA = [
        # 1. Bill Info
        "Date", "Supplier Name", "Invoice Number", "Supplier GST",
        # 2. Product Details
        "Product Name", "Product Code", "Batch Number",
        # 3. Quantity / Packaging
        "Quantity Received", "Unit", "Number of Bags", "Storage Type",
        # 4. Item Financials
        "Rate per Unit", "Item Amount",
        # 5. Bill Totals
        "Taxable Amount", "Taxes (IGST/CGST/SGST)", "Transport / Freight", "Grand Total",
        # 6. Transport
        "Vehicle Number", "Transporter Name",
        # 7. System Meta
        "Barcode Link", "Barcode ID", "Remarks", "Created At", "Created By", "Updated At", "Updated By",
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
        0: 110,   # Date
        1: 160,   # Supplier Name
        2: 150,   # Invoice Number
        3: 140,   # Supplier GST
        4: 220,   # Product Name
        5: 130,   # Product Code
        6: 130,   # Batch Number
        7: 130,   # Quantity Received
        8: 80,    # Unit
        9: 110,   # Number of Bags
        10: 120,  # Storage Type
        11: 130,  # Rate per Unit
        12: 140,  # Item Amount
        13: 140,  # Taxable Amount
        14: 190,  # Taxes
        15: 160,  # Transport / Freight
        16: 150,  # Grand Total
        17: 140,  # Vehicle Number
        18: 160,  # Transporter Name
        19: 180,  # Barcode Link
        20: 170,  # Barcode ID
        21: 200,  # Remarks
        22: 150,  # Created At
        23: 130,  # Created By
        24: 150,  # Updated At
        25: 130,  # Updated By
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
                check_range = f"{title}!2:2" if title == "Stock Register" else f"{title}!1:1"
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
                        self._apply_full_styles(sheet_id, title, schema)
        except Exception as e:
            print(f"Error ensuring sheet '{title}': {e}")

    def _write_headers(self, title: str, schema: List[str]):
        try:
            if title == "Stock Register":
                super_row = [
                    "BILL INFO (A)", "", "BILL INFO (B)", "",
                    "PRODUCT DETAILS", "", "",
                    "QUANTITY / PACKAGING", "", "", "",
                    "ITEM FINANCIALS", "",
                    "BILL TOTALS", "", "", "",
                    "TRANSPORT", "",
                    "SYSTEM META", "", "", "", "", "", ""
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
                    "PRODUCT INFO", "", "LIVE INVENTORY", "", "VOLUME ACTIVITY", "", "SYSTEM INFO"
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
                # Adjusted for new Storage Type column at index 10
                super_spans = [(0, 2), (2, 4), (4, 7), (7, 11), (11, 13), (13, 17), (17, 19), (19, 26)]
            elif title == "Stock Movements":
                super_spans = [(0, 2), (2, 7), (7, 12)]
            elif title == "Stock Summary":
                super_spans = [(0, 2), (2, 4), (4, 6), (6, 7)]
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

        # ── 11. NUMERIC COLUMNS — right-aligned ───────────────────────────────
        numeric_cols = {
            "Stock Register":  [7, 9, 10, 11, 12, 14, 15],
            "Stock Movements": [3],
            "Stock Summary":   [2, 4, 5],
        }.get(title, [])

        for col in numeric_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": frozen_rows,
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
            "Stock Register":  [0, 21, 23],
            "Stock Movements": [0],
            "Stock Summary":   [6],
        }.get(title, [])

        for col in date_cols:
            if col < num_cols:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": frozen_rows,
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
                         item_ids: List[str], user_display: str):
        """
        Ingest a batch of items with extreme efficiency:
        1. Single batchGet for Register & Summary lookups.
        2. In-memory state tracking for updates/appends.
        3. Single batchUpdate for all row writes across all sheets.
        """
        if not self.service:
            return
        
        self.header_map = self._get_or_create_headers()
        now = self._get_now_ist()
        
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
            
            # Index for fast search - ONLY barcode matches are updates to existing ledger rows
            existing_reg_barcode = {str(r[b_id_idx]).strip(): i+1 for i, r in enumerate(reg_rows) if len(r) > b_id_idx}
            summary_idx          = {str(r[1]).strip().upper(): i+1 for i, r in enumerate(sum_rows) if len(r) > 1}
            
            # Cache summary data in memory for accumulation
            summary_data_map = {}
            for i, r in enumerate(sum_rows):
                if i == 0: continue # SKIP HEADER
                if len(r) >= 2:
                    code_key = str(r[1]).strip().upper()
                    if code_key and code_key != "PRODUCT CODE" and code_key not in summary_data_map:
                        summary_data_map[code_key] = {
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
        batch_new_codes = {}    # code -> idx in register_appends
        batch_new_barcodes = {} # barcode -> idx in register_appends

        for i, item in enumerate(items):
            item_id = str(item_ids[i]).strip()
            name = str(item.get("Product Name") or header.get("Product Name") or "Unknown Item").strip()
            code = str(item.get("Product Code") or header.get("Product Code") or item_id[:8]).strip()
            qty  = self._to_float(item.get("Quantity Received") or header.get("Quantity Received") or 0)
            unit = str(item.get("Unit") or header.get("Unit") or "PCS").strip()

            # Safety: Skip 'Ghost' entries
            if qty < 0.001 and name == "Unknown Item":
                continue

            # ── A. Register Upsert ──
            # CRITICAL: We only update if the Barcode ID matches exactly. 
            # If Product Code matches but Barcode ID is new, we Append (New Batch).
            row_idx = existing_reg_barcode.get(item_id)
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            
            if row_idx:
                # Update Existing Row in Spreadsheet
                qty_idx = h.get("Quantity Received", 2)
                old_reg_qty = 0.0
                if row_idx <= len(reg_rows):
                    old_reg_qty = self._to_float(reg_rows[row_idx-1][qty_idx]) if len(reg_rows[row_idx-1]) > qty_idx else 0.0
                
                updates_batch.append({
                    "range": f"Stock Register!{self._get_col_letter(qty_idx)}{row_idx}",
                    "values": [[old_reg_qty + qty]]
                })
                updates_at_idx = h.get("Updated At", 23)
                updates_by_idx = h.get("Updated By", 24)
                col_range = f"{self._get_col_letter(updates_at_idx)}{row_idx}:{self._get_col_letter(updates_by_idx)}{row_idx}"
                updates_batch.append({
                    "range": f"Stock Register!{col_range}",
                    "values": [[now, user_display]]
                })
            elif item_id in batch_new_barcodes:
                # Update within CURRENT batch (Barcode match)
                idx = batch_new_barcodes[item_id]
                curr_q = self._to_float(register_appends[idx][h["Quantity Received"]])
                register_appends[idx][h["Quantity Received"]] = str(curr_q + qty)
            elif code in batch_new_codes:
                # Update within CURRENT batch (Code match)
                idx = batch_new_codes[code]
                curr_q = self._to_float(register_appends[idx][h["Quantity Received"]])
                register_appends[idx][h["Quantity Received"]] = str(curr_q + qty)
            else:
                # Append NEW row
                row_data = [""] * len(self.BASE_SCHEMA)
                h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
                
                # Manual assignments for speed/certainty
                row_data[h["Product Name"]] = name
                row_data[h["Product Code"]] = code
                row_data[h["Quantity Received"]] = str(qty)
                row_data[h["Unit"]] = unit
                row_data[h["Batch Number"]] = str(item.get("Batch Number", "")).strip()
                row_data[h["Number of Bags"]] = str(item.get("Number of Bags") or 0)
                row_data[h["Storage Type"]] = item.get("storage_type") or "UNIT"
                row_data[h["Barcode ID"]] = item_id
                row_data[h["Created At"]] = now
                row_data[h["Created By"]] = user_display
                row_data[h["Updated At"]] = now
                row_data[h["Updated By"]] = user_display
                
                # Map remaining fields from item/header
                for col_name in self.BASE_SCHEMA:
                    idx = h[col_name]
                    if not row_data[idx]:
                        val = item.get(col_name) or header.get(col_name)
                        if val is None and col_name == "Taxes (IGST/CGST/SGST)":
                            val = header.get("Taxes") or item.get("Taxes")
                        
                        if isinstance(val, list):
                            val = ", ".join([f"{str(t.get('label'))}: {t.get('amount')}" for t in val if isinstance(t, dict)])
                        row_data[idx] = str(val) if val is not None else ""
                
                batch_new_barcodes[item_id] = len(register_appends)
                register_appends.append(row_data)

            # ── B. Movement Entry ──
            trans_id = header.get("Invoice Number") or f"TRANS-{str(uuid.uuid4())[:4].upper()}"
            distributions = item.get("distributions", [])
            if not distributions:
                movements_append.append([now, name, "IN", qty, "Main Warehouse", "Full Receive", user_display, f"MOV-{uuid.uuid4().hex[:6].upper()}", item_id, trans_id, item_id, "default"])
            else:
                for dist in distributions:
                    d_qty = self._to_float(dist.get("qty") or dist.get("quantity") or 0)
                    d_loc = dist.get("location") or dist.get("loc_name") or "Main Floor"
                    d_wh  = dist.get("warehouse") or dist.get("wh_name") or "Main Warehouse"
                    d_id  = dist.get("dist_id") or item_id
                    wh_id = dist.get("warehouse_id") or "default"
                    movements_append.append([now, name, "IN", d_qty, d_wh, d_loc, user_display, f"MOV-{uuid.uuid4().hex[:6].upper()}", item_id, trans_id, d_id, wh_id])

        # ── 3. CONSOLIDATE SUMMARY UPDATES ────────────────────────────────────
        # To avoid duplicate rows and incorrect appends, we aggregate all 
        # changes for this session by Product Code first.
        session_summary_map = {} # code_key: {name, code, qty, unit, row_idx}
        for item in items:
            code = str(item.get("Product Code") or "").strip().upper()
            if not code: continue
            
            qty = self._to_float(item.get("Quantity Received") or 0)
            name = item.get("Product Name") or "Item"
            unit = item.get("Unit") or "PCS"
            
            if code not in session_summary_map:
                session_summary_map[code] = {"name": name, "code": code, "qty": 0.0, "unit": unit}
            session_summary_map[code]["qty"] += qty

        for code_key, session_data in session_summary_map.items():
            qty = session_data["qty"]
            name = session_data["name"]
            unit = session_data["unit"]
            code = session_data["code"] # Preservation of case if needed, but we use upper for map

            s_entry = summary_data_map.get(code_key)
            if s_entry:
                # Update existing row
                s_entry["balance"] += qty
                s_entry["received"] += qty
                updates_batch.append({
                    "range": f"Stock Summary!A{s_entry['row']}:G{s_entry['row']}",
                    "values": [[s_entry["name"], code, s_entry["balance"], s_entry["unit"], s_entry["received"], s_entry["dispatched"], now]]
                })
            else:
                # Append new row
                summary_appends.append([name, code, qty, unit, qty, 0, now])
                # Update internal map to prevent double-appending if same code used later (though already merged above)
                summary_data_map[code_key] = {"row": len(sum_rows) + len(summary_appends), "name": name, "balance": qty, "unit": unit, "received": qty, "dispatched": 0}

        # ── 4. EXECUTE SHIPMENT ───────────────────────────────────────────────
        try:
            # First, standard value updates (Upserts)
            if updates_batch:
                self.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": updates_batch}
                ).execute()

            # Second, Appends (Done via batchUpdate with AppendCells or just append)
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

    def sync_batch_to_ledger(self, items: List[Dict], user_display: str) -> bool:
        try:
            item_ids = [item.get("id") or item.get("Barcode ID") for item in items]
            self.save_stock_batch({}, items, item_ids, user_display)
            return True
        except Exception as e:
            print(f"Sheets Sync Error: {e}")
            return False

    def add_movement(self, barcode_id: str, trans_id: str, move_type: str,
                     qty: float, user_display: str, warehouse: str = "Main Warehouse",
                     location: str = "Full Receive", dist_id: str = "default",
                     warehouse_id: str = "default", item_name: str = "Audit Item"):
        if not self.service:
            return
        now = self._get_now_ist()
        row = [
            now, item_name, move_type, qty, warehouse, location, user_display,
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
            b_id_idx = h.get("Barcode ID", 20)
            link_idx = h.get("Barcode Link", 19)
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
            b_id_idx = h.get("Barcode ID", 20)
            qty_idx  = h.get("Quantity Received", 7)
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
                        "product_code": row[h["Product Code"]]
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

    def record_dispatch(self, barcode_id: str, qty: float, bags_removed: float = 0, warehouse: str = "", location: str = "", user_display: str = "System", dist_id: str = "default", warehouse_id: str = "default"):
        """
        Deducts stock from the spreadsheet ledger and records the movement.
        """
        if not self.service: return
        try:
            h = {n: i for i, n in enumerate(self.BASE_SCHEMA)}
            b_id_idx = h.get("Barcode ID", 20)
            row_idx = self._find_row_by_col(b_id_idx, barcode_id)
            
            if row_idx == -1:
                print(f"⚠️ Sheets Deduction: Barcode ID {barcode_id} not found in Register.")
                return

            # 1. Update Stock Register Row
            qty_col = self._get_col_letter(h.get("Quantity Received", 7))
            bags_col = self._get_col_letter(h.get("Number of Bags", 9))
            updated_at_col = self._get_col_letter(h.get("Updated At", 24))
            updated_by_col = self._get_col_letter(h.get("Updated By", 25))

            # Fetch current qty and bags
            range_to_fetch = f"Stock Register!{qty_col}{row_idx}:{bags_col}{row_idx}"
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=range_to_fetch
            ).execute()
            
            row_vals = res.get("values", [[0, 0, 0]])[0]
            curr_qty = self._to_float(row_vals[0])
            # Index 2 in fetched row_vals corresponds to Bags column if range is Qty(7) to Bags(9)
            curr_bags = self._to_float(row_vals[2]) if len(row_vals) > 2 else 0
            
            new_qty = max(0.0, curr_qty - qty)
            new_bags = max(0.0, curr_bags - bags_removed)

            # Batch update the row
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": [
                    {"range": f"Stock Register!{qty_col}{row_idx}", "values": [[new_qty]]},
                    {"range": f"Stock Register!{bags_col}{row_idx}", "values": [[new_bags]]},
                    {"range": f"Stock Register!{updated_at_col}{row_idx}:{updated_by_col}{row_idx}", "values": [[self._get_now_ist(), user_display]]}
                ]}
            ).execute()

            # 2. Record Movement
            # We need Product Name for movement record
            name_res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"Stock Register!{self._get_col_letter(h.get('Product Name', 4))}{row_idx}"
            ).execute()
            p_name = name_res.get("values", [["Unknown"]])[0][0]
            p_code = "" # Optional
            
            movement_row = [
                self._get_now_ist(), p_name, "OUT", qty, warehouse, location, user_display,
                f"MOV-{int(time.time())}", barcode_id, f"DISP-{uuid.uuid4().hex[:4].upper()}", dist_id, warehouse_id
            ]
            self._append_row("Stock Movements", movement_row)

            # 3. Update Summary
            code_res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"Stock Register!{self._get_col_letter(h.get('Product Code', 5))}{row_idx}"
            ).execute()
            p_code = code_res.get("values", [[""]])[0][0]
            
            if p_code:
                self._update_summary_row(p_name, p_code, -qty, "OUT")

            print(f"✅ Sheets Sync: Deducted {qty} of {p_name} ({barcode_id})")

        except Exception as e:
            print(f"Sheets Record Dispatch Error: {e}")
            traceback.print_exc()

    def _update_summary_row(self, name: str, code: str, qty_delta: float, m_type: str):
        """Helper to update the aggregate balance and totals in Stock Summary."""
        if not self.service: return
        try:
            res = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range="Stock Summary!A:G"
            ).execute()
            rows = res.get("values", [])
            header = rows[0] if rows else self.SUMMARY_SCHEMA
            data = rows[1:]
            
            found_idx = -1
            code_to_find = self._normalize_code(code)
            
            for i, row in enumerate(data):
                if len(row) >= 2:
                    current_code = self._normalize_code(row[1])
                    if current_code == code_to_find:
                        found_idx = i + 2 # +2 because 1-based and skip header
                        break
            
            if found_idx == -1:
                if m_type == "OUT":
                    print(f"⚠️ [SUMMARY_WARN] Could not find code {code} for deduction. Skipping summary update to avoid duplication.")
                    return

                # Add new summary row (Only for IN)
                new_row = [name, code, qty_delta, "PCS", 
                           qty_delta if m_type == "IN" else 0.0,
                           abs(qty_delta) if m_type == "OUT" else 0.0,
                           self._get_now_ist()]
                self._append_row("Stock Summary", new_row)
            else:
                curr_row = data[found_idx - 2]
                curr_bal = self._to_float(curr_row[2]) if len(curr_row) > 2 else 0.0
                curr_in = self._to_float(curr_row[4]) if len(curr_row) > 4 else 0.0
                curr_out = self._to_float(curr_row[5]) if len(curr_row) > 5 else 0.0
                
                new_bal = curr_bal + qty_delta
                new_in = curr_in + (qty_delta if m_type == "IN" else 0.0)
                new_out = curr_out + (abs(qty_delta) if m_type == "OUT" else 0.0)
                
                update_range = f"Stock Summary!C{found_idx}:G{found_idx}"
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=update_range,
                    valueInputOption="USER_ENTERED",
                    body={"values": [[new_bal, curr_row[3] if len(curr_row) > 3 else "PCS", new_in, new_out, self._get_now_ist()]]}
                ).execute()
        except Exception as e:
            print(f"Update summary row error: {e}")

    def _get_now_ist(self) -> str:
        """Returns current time in Indian Standard Time (UTC+5:30)."""
        # Manual offset for IST (5 hours 30 mins = 19800 seconds)
        ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%Y-%m-%d %H:%M:%S")

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

    def _normalize_code(self, code) -> str:
        """Standardizes a product code for reliable matching."""
        if not code: return ""
        return re.sub(r'[^A-Z0-9]', '', str(code).upper())


sheets_service = SheetsService()
