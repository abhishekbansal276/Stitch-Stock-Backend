import os
import sys
from dotenv import load_dotenv

# Ensure we are in the backend directory OR can find the app package
backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.append(backend_dir)

from app.services.sheets import SheetsService

def migrate():
    load_dotenv()
    print("\n" + "="*50)
    print("STARTING SHEETS SCHEMA MIGRATION")
    print("="*50)
    
    sheets_service = SheetsService()
    if not sheets_service.service:
        print("CRITICAL: Failed to initialize Sheets Service. Check credentials.")
        return

    spreadsheet_id = sheets_service.spreadsheet_id
    if not spreadsheet_id:
        print("CRITICAL: GOOGLE_SHEETS_ID not found in environment.")
        return

    print(f"Spreadsheet ID: {spreadsheet_id}")

    try:
        # 1. Locate 'Stock Register' sheet ID
        print("Scanning sheet metadata...")
        sheet_metadata = sheets_service.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id).execute()
        
        stock_reg_id = None
        for sheet in sheet_metadata['sheets']:
            if sheet['properties']['title'] == "Stock Register":
                stock_reg_id = sheet['properties']['sheetId']
                break
        
        if stock_reg_id is None:
            print("ERROR: 'Stock Register' sheet not found in this spreadsheet.")
            return

        # 2. Check headers
        print("Checking current headers...")
        res = sheets_service.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range="'Stock Register'!1:1"
        ).execute()
        
        headers = res.get('values', [[]])[0]
        
        # Check if 'Supplier Name' is already there (case-insensitive)
        has_supplier = any("SUPPLIER" in str(h).upper() for h in headers)
        
        if has_supplier:
            print("DONE: 'Supplier Name' already exists in headers.")
        else:
            print("MISSING: 'Supplier Name' NOT FOUND. Inserting column at index 2 (Column C)...")
            
            # Logic: We want [Date, Invoice, Supplier, Transporter, Remarks]
            # Index 2 is Column C.
            
            requests = [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": stock_reg_id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": 3
                        },
                        "inheritFromBefore": True
                    }
                },
                {
                    "updateCells": {
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": {"stringValue": "Supplier Name"}}
                                ]
                            }
                        ],
                        "fields": "userEnteredValue",
                        "range": {
                            "sheetId": stock_reg_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3
                        }
                    }
                }
            ]
            
            sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()
            print("Column physical insertion complete.")

        # 3. Refresh Styles
        print("Refreshing styles...")
        sheets_service.beautify_all()
        print("MIGRATION SUCCESSFUL: Sheet is now schema-aligned and beautified.")
        print("="*50 + "\n")
        
    except Exception as e:
        print(f"❌ MIGRATION FAILED: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    migrate()
