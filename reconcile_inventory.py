import os
import time
from typing import Dict, List
from dotenv import load_dotenv

# Set up environment
os.environ["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__))
os.environ["SERVICE_ACCOUNT_FILE"] = "serviceAccountKey.json"
load_dotenv()

from app.services.firebase import db
from app.services.sheets import sheets_service
from app.utils import normalize_id

def reconcile():
    print("🚀 Starting Inventory Reconciliation (Authoritative from Sheets)...")
    
    # Authoritative ID from known configuration
    SHEET_ID = os.getenv("GOOGLE_SHEETS_ID") or "1I4HadNgye0WU_Zirn6TG2rCGLlzTuLuDaeMMPiwUjzQ"
    print(f"📊 Using Spreadsheet ID: {SHEET_ID}")
    
    collection = db.collection('inventory_positions')
    
    # 1. Fetch Authoritative Data from Stock Summary Sheet
    print("📡 Fetching data from 'Stock Summary' sheet...")
    try:
        # Get data from Stock Summary!A:G (includes Code and Name)
        result = sheets_service.service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Stock Summary!A:G"
        ).execute()
        rows = result.get('values', [])
        
        if len(rows) < 3:
            print("⚠️ No data found in Stock Summary (skipping sheet sync).")
            return

        # Skip header rows (assuming row 3 is first data row)
        data_rows = rows[2:]
        print(f"📊 Found {len(data_rows)} products in sheet.")

        batch = db.batch()
        count = 0

        for row in data_rows:
            if len(row) < 4: continue
            
            p_name = row[0]
            p_code = row[1]
            balance = row[2]
            bags = row[3]
            unit = row[4] if len(row) > 4 else "UKN"
            
            if not p_code: continue
            
            clean_code = normalize_id(p_code)
            doc_id = f"PROD-{clean_code}"
            
            # Map values
            try:
                # Clean strings like "311.325 MT" or "12MT" or "1,200"
                def clean_float(val):
                    if not val: return 0.0
                    # Remove units and commas
                    cleaned = "".join(c for i, c in enumerate(str(val)) if c.isdigit() or c == '.')
                    return float(cleaned) if cleaned else 0.0

                balance_val = clean_float(balance)
                bags_val = clean_float(bags)
            except Exception as pe:
                print(f"⚠️ Error parsing values for {p_code}: {pe}")
                continue

            doc_ref = collection.document(doc_id)
            doc_snap = doc_ref.get()
            
            # Update core totals to match Sheets
            doc_data = {
                'total_qty': balance_val,
                'number_of_bags': bags_val,
                'product_name': p_name,
                'product_code': p_code,
                'unit': unit,
                'updated_at': int(time.time()),
                'updated_by': 'Admin Reconciliation (Authoritative)',
                'is_merged': True
            }

            if doc_snap.exists:
                batch.update(doc_ref, doc_data)
            else:
                doc_data.update({
                    'doc_id': doc_id,
                    'barcode_ids': [],
                    'distributions': [],
                    'location_ids': [],
                    'sync_status': 'success'
                })
                batch.set(doc_ref, doc_data)

            count += 1
            if count % 20 == 0:
                batch.commit()
                batch = db.batch()
                print(f"✅ Reconciled {count} products...")

        batch.commit()
        print(f"🏁 Reconciliation Complete. Fixed {count} products (Synced from Sheets Truth).")

    except Exception as e:
        print(f"❌ Error during reconciliation: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    reconcile()
