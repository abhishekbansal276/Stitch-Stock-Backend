import time
import os
import uuid
import traceback
import asyncio
from datetime import datetime
from typing import List, Dict, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from app.routers import users
from app.services.firebase import initialize_firebase, db
from app.services.ocr import ocr_service
from app.services.sheets import sheets_service
from app.services.inventory import inventory_service
from app.services.location_service import location_service
from app.services.activity_service import activity_service
from app.services.google_drive_service import drive_service
from app.dependencies.auth import get_current_user
from app.models.stock import StockTransferRequest

# Load environment variables for local development
load_dotenv()

app = FastAPI(title="Stitch Stock Management System")

# Enable CORS for Flutter web and general cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── SYNC REAPER (Data Integrity) ───────────────────────────────────────────

async def _automated_sync_reaper():
    """Background loop to identify and retry failed/pending syncs."""
    await asyncio.sleep(20) # Give the server time to fully boot
    print("🔄 SYNC REAPER: Starting periodic integrity check...")
    
    while True:
        try:
            # Query Firestore for docs needing sync
            # Collection: 'inventory_positions'
            docs = db.collection("inventory_positions")\
                     .where("sync_status", "in", ["pending", "error"])\
                     .limit(100).get()
            
            if docs:
                print(f"📊 SYNC REAPER: Found {len(docs)} items requiring sync. Processing...")
                for doc in docs:
                    data = doc.to_dict()
                    doc_id = doc.id
                    # Re-trigger ingestion for this specific record
                    await _process_single_sync(doc_id, data)
            
        except Exception as e:
            print(f"❌ SYNC REAPER ERROR: {e}")
            
        # Run every 5 minutes
        await asyncio.sleep(300)

async def _process_single_sync(doc_id: str, data: Dict):
    """Internal helper to sync a single Firestore doc to Drive and Sheets."""
    try:
        user_email = data.get("created_by", "system@reaper.auto")
        label = f"{data.get('product_name', 'Item')} - {data.get('warehouse', 'WH')}"
        
        # 1. ARCHIVE BARCODE (to Drive)
        link = data.get("barcode_link")
        if not link:
            link = drive_service.generate_barcode(doc_id, label)
            if link:
                db.collection("inventory_positions").document(doc_id).update({
                    "barcode_link": link
                })

        # 2. SYNC TO SHEETS
        # Prepare item for SheetsService
        sheets_item = {**data, "id": doc_id, "barcode_link": link}
        success = sheets_service.sync_batch_to_ledger([sheets_item], user_email)
        
        # 3. UPDATE STATUS
        if success:
            db.collection("inventory_positions").document(doc_id).update({
                "sync_status": "synced",
                "synced_at": datetime.now().isoformat(),
                "sync_error": None
            })
            print(f"✅ SYNC REAPER: Successfully synced {doc_id}")
        else:
            print(f"⚠️ SYNC REAPER: Failed to sync {doc_id} to Sheets.")
            
    except Exception as e:
        print(f"❌ SYNC REAPER: Crash processing {doc_id}: {e}")

@app.on_event("startup")
async def startup_event():
    """Kick off background workers on server start."""
    asyncio.create_task(_automated_sync_reaper())
    print("🚀 BACKEND: Automated Sync Reaper is active.")

# Root/Health check
@app.get("/")
def read_root():
    return {"message": "Stitch Stock Management System API is live"}

# Include routers - Users router handles user management and /me
app.include_router(users.router)

@app.post("/users/fcm-token")
async def register_fcm_token(payload: dict, user: dict = Depends(get_current_user)):
    """Registers a mobile device FCM token for push notifications."""
    try:
        token = payload.get('fcm_token')
        if not token:
            raise HTTPException(status_code=400, detail="Token required.")
        
        db.collection('users').document(user['email']).update({
            'fcm_token': token,
            'updated_at': int(time.time())
        })
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stock/extract")
async def extract_stock(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    """
    Upload a PDF or Image and get extracted JSON data.
    """
    start_time = time.time()
    user_email = user.get('email', 'Unknown')
    print(f"\n--- EXTRACTION START [User: {user_email}] ---")
    
    try:
        content = await file.read()
        print(f"File Received: {file.filename} ({len(content)} bytes)")
        
        # ELITE SYNC: Get existing headers to help Gemini map them correctly
        h_start = time.time()
        existing_headers = sheets_service.get_current_headers()
        print(f"Sheets headers fetched in {time.time() - h_start:.4f}s")
        
        extracted_data = ocr_service.extract_from_file(content, file.filename, existing_headers)
        
        total_msg = f"--- EXTRACTION SUCCESS in {time.time() - start_time:.2f}s ---"
        print(total_msg + "\n")
        return extracted_data
    except Exception as e:
        print(f"!!! EXTRACTION FAILED: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

@app.post("/stock/create")
async def create_stock(
    payload: dict,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user)
):
    """
    Save reviewed stock data into the register.
    Supports Multi-Location Distributions.
    """
    try:
        header = payload.get('header', {})
        items = payload.get('items', [])
        user_display = user.get('full_name', user['email'])
        
        # 1. GENERATE IDs UPFRONT - Use provided IDs if available from frontend
        item_ids = []
        for item in items:
            item_id = item.get('id') or f"STK-{int(time.time())}-{uuid.uuid4().hex[:4].upper()}"
            item_ids.append(item_id)
            item['id'] = item_id # Ensure ID is present for background tasks
        
        print(f"🚀 INGESTION START: Received {len(items)} items. IDs: {item_ids}")

        # 3. BACKGROUND TASKS (Heavy / Slow Operations)
        background_tasks.add_task(_process_async_ingestion, header, items, item_ids, user)
        
        return {
            "message": "Stock registration initiated. Tracking IDs generated.", 
            "status": "success",
            "stock_item_ids": item_ids,
            "items": items 
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"Ingestion setup failed: {str(e)}")

async def _process_async_ingestion(header: dict, items: list, item_ids: list, user: dict):
    """Heavy lifted background task for Firestore sync, Ledger sync and audit logging."""
    try:
        user_display = user.get('full_name', user['email'])
        
        # 1. Update Firestore & Google Sheets
        # Sheets Save (Batch logic)
        try:
            print(f"📊 SYNCING TO SHEETS: Batch size {len(item_ids)}")
            sheets_service.save_stock_batch(header, items, item_ids, user_display)
        except Exception as sheet_err:
            print(f"❌ SHEETS CRITICAL ERROR: {sheet_err}")
            traceback.print_exc()
            # If sheets fail, we still try to update Firestore status to 'error'
            for item_id in item_ids:
                try: inventory_service.db.collection('inventory_positions').document(item_id).update({'sync_status': 'error', 'sync_error': str(sheet_err)})
                except: pass
            return

        # 2. Process Individual Items
        for i, item_id in enumerate(item_ids):
            try:
                item_data = items[i]
                
                # Firestore Save (Idempotent check)
                distributions = item_data.get('distributions', [
                    {'loc_id': 'default', 'loc_name': 'Main Floor', 'qty': item_data.get('Quantity Received', 0)}
                ])
                
                # Only save to Firestore if it doesn't already exist (Avoid overwriting app-side write)
                doc_ref = inventory_service.collection.document(item_id)
                if not doc_ref.get().exists:
                    inventory_service.save_position(
                        item_id, item_data.get('Product Name'), 
                        item_data.get('Product Code'), 
                        item_data.get('Unit', 'PCS'),
                        distributions
                    )
                
                # MARK AS SYNCED ✅
                doc_ref.update({'sync_status': 'synced', 'sync_at': int(time.time())})

                # LOG & NOTIFY
                qty_val = 0.0
                try: qty_val = float(item_data.get('Quantity Received', 0))
                except: pass
                
                activity_service.log_and_notify(
                    user=user,
                    action_type="IN",
                    item_name=item_data.get('Product Name', 'New Stock'),
                    product_code=item_data.get('Product Code', 'N/A'),
                    qty_change=qty_val,
                    location="System Ingestion",
                    description=item_data.get('Description', '')
                )
                
                # Barcode Archiving
                _process_barcode_archiving(item_id, item_data.get('Product Name', 'Stock Item'))
                
                for dist in item_data.get('distributions', []):
                    dist_id = dist.get('dist_id')
                    dist_label = f"{item_data.get('Product Name')} @ {dist.get('warehouse')}"
                    if dist_id:
                        _process_barcode_archiving(dist_id, dist_label, is_position=True, parent_id=item_id)
                        
            except Exception as item_err:
                print(f"⚠️ ITEM SYNC FAILURE [{item_id}]: {item_err}")
                traceback.print_exc()
                continue

        print(f"✅ BATCH SYNC COMPLETED: {item_ids}")

    except Exception as e:
        print(f"🛑 CRITICAL ASYNC INGESTION FAILURE: {e}")
        traceback.print_exc()

def _process_barcode_archiving(code_id: str, label: str, is_position: bool = False, parent_id: str = None):
    """Internal helper to generate/upload QR and update storage in background."""
    try:
        # Switch to the new Elite QR Generator
        # Archive as standard 1D Barcode (Code 128)
        link = drive_service.generate_barcode(code_id, label)
        if link:
            if not is_position:
                # Main Item ID
                sheets_service.update_barcode_link(code_id, link)
                inventory_service.update_barcode_link(code_id, link)
            else:
                # Individual Distribution ID
                # We store the link directly in the position record for instant fetch
                inventory_service.update_distribution_qr(parent_id, code_id, link)
    except Exception as e:
        print(f"Background QR Error [{code_id}]: {e}")

@app.get("/stock/position/{dist_id}")
async def get_stock_by_position(
    dist_id: str,
    user: dict = Depends(get_current_user)
):
    """Fetch main item + target distribution for a shelf-specific QR code."""
    result = inventory_service.find_by_dist_id(dist_id)
    if not result:
        raise HTTPException(status_code=404, detail="Position not found")
    return result

@app.get("/stock/{stock_item_id}")
async def get_stock_item(
    stock_item_id: str,
    user: dict = Depends(get_current_user)
):
    """
    Fetch details + Spatial Positions (from Firestore).
    """
    item = sheets_service.get_stock_item(stock_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Stock item not found")
        
    pos = inventory_service.get_position(stock_item_id)
    if pos:
        item['distributions'] = pos.get('distributions', [])
        
    return item

@app.post("/stock/{stock_item_id}/remove")
async def remove_stock(
    stock_item_id: str,
    payload: dict,
    user: dict = Depends(get_current_user)
):
    """
    Deduct quantity from a stock item and record the spatial movement.
    """
    try:
        qty_to_remove = float(payload.get('quantity', 0))
        loc_id = payload.get('loc_id', 'default')
        loc_name = payload.get('location', 'Main Floor')
        usage = payload.get('usage', 'General')
        remarks = payload.get('remarks', '')
        
        item = sheets_service.get_stock_item(stock_item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Stock item not found")
            
        # 1. Atomic Firestore Deduction (Spatial Map)
        inventory_service.remove_stock_spatial(stock_item_id, loc_id, qty_to_remove)
        
        # 2. Update Sheets Ledger
        new_remaining = item['quantity_remaining'] - qty_to_remove
        sheets_service.update_stock_quantity(stock_item_id, new_remaining)
            
        # 3. Record Movement (OUT) with Location Tag
        trans_id = payload.get('transaction_id', f"OUT-{int(time.time())}")
        sheets_service.add_movement(stock_item_id, trans_id, 'OUT', -qty_to_remove, user['email'], location=loc_name)
        
        # 4. Log Activity & Notify Admins
        activity_service.log_and_notify(
            user=user,
            action_type="OUT",
            item_name=item.get('item_name', 'Stock Item'),
            product_code=item.get('product_code', stock_item_id),
            qty_change=-qty_to_remove,
            location=loc_name,
            description=item.get('description', '')
        )
        
        return {"message": "Stock removed successfully", "remaining": new_remaining}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Removal failed: {str(e)}")

@app.get("/reports/summary")
async def get_summary(period: str = "all", user: dict = Depends(get_current_user)):
    """
    Get live summary data from the 'Stock Summary' sheet.
    Supports 'period=today' for daily stats.
    """
    try:
        return sheets_service.get_summary_stats(period=period)
    except Exception as e:
        print(f"Summary Fetch Error: {e}")
        return {
            "total_in": 0,
            "total_out": 0,
            "available_balance": 0,
            "low_stock_count": 0
        }

@app.get("/reports/graphs")
async def get_graph_data(user: dict = Depends(get_current_user)):
    """
    Get 7-day time-series and zone distribution for dashboard charts.
    """
    try:
        return sheets_service.get_graph_data()
    except Exception as e:
        print(f"Graph Data Error: {e}")
        return {"movement": [], "zones": []}

@app.get("/logs")
async def get_logs(limit: int = 20, last_ts: int = None, search: str = None, user: dict = Depends(get_current_user)):
    """Advanced Audit Timeline with Pagination and Multi-Field Search."""
    try:
        return activity_service.get_logs_paged(limit=limit, last_ts=last_ts, search=search)
    except Exception as e:
        print(f"Log fetch failed: {e}")
        return []

# --- WAREHOUSE EXPLORER & SPATIAL TRANSFER ---

@app.get("/warehouse/locations")
async def get_all_locations(user: dict = Depends(get_current_user)):
    """Fetch all defined warehouse masters with nested zones."""
    try:
        return location_service.get_all_locations()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/warehouse/locations")
async def create_location(payload: dict, user: dict = Depends(get_current_user)):
    """Add a new warehouse master or add a zone to an existing one."""
    try:
        wh_id = payload.get('warehouse_id')
        zone = payload.get('name')
        
        if wh_id and zone:
            # Add Zone to existing Warehouse
            location_service.add_zone_to_warehouse(wh_id, zone)
            return {"status": "success", "message": "Zone added"}
        
        # Create new Warehouse
        wh_name = payload.get('warehouse_name')
        if not wh_name:
            raise HTTPException(status_code=400, detail="Warehouse name required")
        
        loc_id = location_service.create_location(wh_name)
        return {"id": loc_id, "name": wh_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/warehouse/locations/{wh_id}/zones/{old_name}")
async def rename_zone_endpoint(wh_id: str, old_name: str, payload: dict, user: dict = Depends(get_current_user)):
    """Rename a specific zone inside a warehouse master."""
    try:
        new_name = payload.get('new_name')
        if not new_name:
            raise HTTPException(status_code=400, detail="New name required")
        
        location_service.rename_zone(wh_id, old_name, new_name)
        return {"status": "success", "message": "Zone renamed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/warehouse/zones")
async def get_all_zones(user: dict = Depends(get_current_user)):
    """Aggregate unique hierarchical locations across all stock positions."""
    try:
        return inventory_service.get_all_zones()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/warehouse/inventory-report")
async def get_inventory_report(user: dict = Depends(get_current_user)):
    """Full Visibility: All products and their locations."""
    try:
        return inventory_service.get_inventory_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/warehouse/items/{loc_id}")
async def get_items_by_zone(loc_id: str, user: dict = Depends(get_current_user)):
    """Drill-down: Returns all products present in a specific zone."""
    try:
        return inventory_service.get_items_in_zone(loc_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stock/transfer")
async def transfer_stock_position(req: StockTransferRequest, user: dict = Depends(get_current_user)):
    """Operation: Atomically moves stock between zones with audit trail."""
    try:
        user_email = user['email']
        
        # 0. Fetch Item details for rich logging
        item = sheets_service.get_stock_item(req.barcode_id)
        item_name = item.get('item_name', 'Transfer Item') if item else "Move Operation"
        item_desc = item.get('description', '') if item else ""

        # 1. Update Firestore Atomic Map
        inventory_service.transfer_stock(
            req.barcode_id, req.from_location, req.to_location, req.to_location_name, req.quantity
        )
        
        # 2. Log to Google Sheets Movements (Audit)
        # Format: "Zone A -> Zone B" for spatial traceability
        loc_audit = f"{req.from_location_name} ➔ {req.to_location_name}"
        sheets_service.add_movement(
            req.barcode_id, 
            req.reason or "WMS-TRANSFER", 
            "TRANSFER", 
            req.quantity, 
            user_email,
            location=loc_audit
        )
        
        # 3. Log Activity & Notify Admins
        activity_service.log_and_notify(
            user=user,
            action_type="TRANSFER",
            item_name=item_name,
            product_code=req.barcode_id,
            qty_change=req.quantity,
            location=loc_audit,
            description=item_desc
        )
        
        return {"status": "success", "message": f"Stock moved to {req.to_location} successfully."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/stock/{stock_item_id}/min-stock")
async def update_min_stock(
    stock_item_id: str,
    payload: dict,
    user: dict = Depends(get_current_user)
):
    """Admin only: Set the minimum stock threshold for alerts."""
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        min_level = float(payload.get('min_stock', 0))
        inventory_service.set_min_stock_level(stock_item_id, min_level)
        return {"status": "success", "min_stock_level": min_level}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Alert Recipient Management (Dynamic) ---

@app.get("/admin/alert-emails")
async def get_alert_emails(user: dict = Depends(get_current_user)):
    """Fetch the global list of email recipients for low-stock alerts."""
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        doc = db.collection('senttoemail').document('recipients').get()
        if not doc.exists:
            # Initialize if not present
            db.collection('senttoemail').document('recipients').set({'emails': []})
            return []
        return doc.to_dict().get('emails', [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/alert-emails/add")
async def add_alert_email(payload: dict, user: dict = Depends(get_current_user)):
    """Add a new email to the alert recipient list."""
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        email = payload.get('email')
        if not email:
            raise HTTPException(status_code=400, detail="Email required")
            
        from google.cloud import firestore
        db.collection('senttoemail').document('recipients').update({
            'emails': firestore.ArrayUnion([email])
        })
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/alert-emails/remove")
async def remove_alert_email(payload: dict, user: dict = Depends(get_current_user)):
    """Remove an email from the alert recipient list."""
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        email = payload.get('email')
        if not email:
            raise HTTPException(status_code=400, detail="Email required")
            
        from google.cloud import firestore
        db.collection('senttoemail').document('recipients').update({
            'emails': firestore.ArrayRemove([email])
        })
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
