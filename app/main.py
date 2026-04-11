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
from google.cloud.firestore_v1.base_query import FieldFilter
from app.routers import users
from app.services.firebase import initialize_firebase, db
from app.services.ocr import ocr_service
from app.services.sheets import sheets_service
from app.services.inventory import inventory_service
from app.services.location_service import location_service
from app.services.activity_service import activity_service
from app.services.google_drive_service import drive_service
from app.utils import generate_12_digit_hash, normalize_id, safe_float, safe_int
from app.dependencies.auth import get_current_user, require_admin, require_staff
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
    await asyncio.sleep(60) # Wait 1 minute after boot for stability
    
    while True:
        try:
            # 1. POSITIONS SYNC (Legacy / Intake)
            docs = db.collection("inventory_positions")\
                     .where(filter=FieldFilter("sync_status", "in", ["pending", "error"]))\
                     .limit(20).get()
            
            if docs:
                print(f"📊 SYNC REAPER [POSITIONS]: Processing {len(docs)} items...")
                for doc in docs:
                    await _process_single_sync(doc.id, doc.to_dict())
            
            # 2. PENDING TASKS SYNC (Deductions / Transfers)
            tasks = db.collection("pending_syncs")\
                      .where(filter=FieldFilter("sync_status", "==", "pending"))\
                      .limit(20).get()
            
            if tasks:
                print(f"📊 SYNC REAPER [TASKS]: Processing {len(tasks)} operations...")
                for task in tasks:
                    await _process_pending_sync_task(task.id, task.to_dict())
            
        except Exception as e:
            print(f"❌ SYNC REAPER ERROR: {e}")
            
        # Run every 15 minutes (900 seconds)
        await asyncio.sleep(900)

async def _process_single_sync(doc_id: str, data: Dict):
    """Internal helper to sync a single Firestore doc to Drive and Sheets."""
    try:
        user_display = data.get("created_by", "system@reaper.auto")
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
        success = sheets_service.sync_batch_to_ledger([sheets_item], user_display)
        
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

async def _process_pending_sync_task(task_id: str, data: Dict):
    """Processes a polymorphic sync task (DISPATCH, etc.) from pending_syncs."""
    try:
        t_type = data.get("type")
        payload = data.get("payload", {})
        retry_count = data.get("retry_count", 0)

        success = False
        if t_type == "DISPATCH":
            success = sheets_service.record_dispatch(**payload)
        elif t_type == "INGESTION_BATCH":
            # For batches, we use the original logic but wrapped for task processor
            try:
                # payload = {header, items, item_ids, user_display}
                await asyncio.to_thread(
                    sheets_service.save_stock_batch, 
                    payload['header'], 
                    payload['items'], 
                    payload['item_ids'], 
                    payload['user_display']
                )
                success = True
            except Exception as e:
                print(f"⚠️ Reaper Batch Retry Failed: {e}")
                success = False
        elif t_type == "TRANSFER":
            try:
                sheets_service.add_movement(**payload)
                success = True
            except Exception as e:
                print(f"⚠️ Reaper Transfer Retry Failed: {e}")
                success = False
        
        if success:
            db.collection("pending_syncs").document(task_id).delete()
            print(f"✅ SYNC REAPER: Task {task_id} [{t_type}] completed and archived.")
        else:
            new_retry = retry_count + 1
            update_data = {
                "retry_count": new_retry,
                "last_sync_attempt": int(time.time())
            }
            if new_retry >= 5:
                # Permanent failure alert
                update_data["sync_status"] = "failed"
                from app.services.fcm_service import fcm_service
                fcm_service.send_multicast_to_admins(
                    title="🚨 SYNC FAILURE",
                    body=f"A background {t_type} operation failed after 5 retries. Manual intervention required.",
                    data={"task_id": task_id, "type": t_type}
                )
            
            db.collection("pending_syncs").document(task_id).update(update_data)
            print(f"⚠️ SYNC REAPER: Task {task_id} failed attempt {new_retry}.")
            
    except Exception as e:
        print(f"❌ SYNC REAPER [TASK_ERROR] {task_id}: {e}")

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
    user_display = user.get('email', 'Unknown')
    print(f"\n--- EXTRACTION START [User: {user_display}] ---")
    
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
        
        # 0. PRE-SYNC VALIDATION: Ensure bill hasn't been entered
        invoice_num = header.get('Invoice Number')
        transporter = header.get('Transporter Name')
        
        if invoice_num:
            print(f"🕵️ Syncing Bill #{invoice_num}... Checking for duplicates...")
            if sheets_service.check_invoice_duplicate(invoice_num):
                err_msg = f"Invoice/Bill #{invoice_num} already exists in the Stock Register."
                print(f"🛑 [SYNC-ABORTED] {err_msg}")
                raise HTTPException(status_code=400, detail=err_msg)
        
        # 1. BATCH MERGE: Decide identity based on 'merge_mode'
        merge_on = payload.get('merge_mode', False)
        final_merged = {}
        for item in items:
            p_code_raw = str(item.get('Product Code') or 'UKN').upper()
            batch_raw  = str(item.get('Batch Number') or 'NB').upper()
            
            # Use identical normalization as Sheets/Inventory services
            p_code = normalize_id(p_code_raw)
            batch  = normalize_id(batch_raw)
            
            # IDENTITY DEFINITION: Merged means 1 Per Product. Non-merged means 1 Per Batch.
            group_key = p_code if merge_on else f"{p_code}-{batch}"
            item_id = generate_12_digit_hash(group_key)
            
            # BATCH SYNC: Ensure distributions carry the parent batch number
            item_dists = item.get('distributions', [])
            for d in item_dists:
                if not d.get('batch_number'):
                    d['batch_number'] = batch
            
            if item_id not in final_merged:
                item['id'] = item_id
                item['barcode_id'] = item_id
                item['is_merged'] = merge_on
                item['distributions'] = item_dists
                final_merged[item_id] = item
            else:
                base = final_merged[item_id]
                base['distributions'] = list(base.get('distributions', [])) + list(item_dists)
                # Aggregate counts
                try:
                    # -- COMPATIBILITY LAYER --
                    q1 = float(base.get('Quantity Received (In Unit)') or base.get('Quantity Received', 0))
                    q2 = float(item.get('Quantity Received (In Unit)') or item.get('Quantity Received', 0))
                    base['Quantity Received (In Unit)'] = q1 + q2
                    
                    b1 = float(base.get('Number of Bags', 0))
                    b2 = float(item.get('Number of Bags', 0))
                    base['Number of Bags'] = b1 + b2
                except: pass

        items = list(final_merged.values())
        item_ids = list(final_merged.keys())
        
        print(f"🚀 INGESTION START: Processing {len(items)} unique identities. IDs: {item_ids}")

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
        
        # 1. PARALLEL BARCODE GENERATION 🚀
        # We generate links FIRST so they are available for the first Sheets write
        barcode_tasks = []
        for i, item_id in enumerate(item_ids):
            item_data = items[i]
            p_code = normalize_id(item_data.get('Product Code', 'UKN'))
            batch_label = "M" if item_data.get('is_merged') else normalize_id(item_data.get('Batch Number', 'NB'))
            barcode_tasks.append(_process_barcode_archiving_async(item_id, p_code, batch_label))
        
        print(f"📡 Generating {len(barcode_tasks)} barcodes in parallel...")
        barcode_links = await asyncio.gather(*barcode_tasks)
        
        # Inject links back into items for Sheets/Firestore
        for i, link in enumerate(barcode_links):
            if link:
                items[i]['Barcode Link'] = link

        # 2. STEP 1: Update Firestore (Source of Truth - Marked as PENDING)
        doc_ids_to_sync = []
        total_qty_combined = 0.0
        log_details = []
        
        for i, item_id in enumerate(item_ids):
            try:
                item_data = items[i]
                distributions = item_data.get('distributions', [
                    {'loc_id': 'default', 'loc_name': 'Main Floor', 'qty': item_data.get('Quantity Received (In Unit)') or item_data.get('Quantity Received', 0)}
                ])
                
                # Pass 'N/A' strings directly, otherwise convert to safe numeric
                raw_bags_val = item_data.get('Number of Bags') or item_data.get('number_of_bags', 0)
                final_bags_p = "N/A" if str(raw_bags_val).upper() == "N/A" else safe_int(raw_bags_val)
                
                raw_qty_val = item_data.get('Quantity Received (In Unit)') or item_data.get('Quantity Received', 0)
                final_qty_p = "N/A" if str(raw_qty_val).upper() == "N/A" else safe_float(raw_qty_val)

                d_id = inventory_service.upsert_position(
                    barcode_id=item_id, 
                    product_name=item_data.get('Product Name'), 
                    product_code=item_data.get('Product Code'), 
                    unit=item_data.get('Unit', 'PCS'),
                    distributions=distributions, 
                    supplier_name=header.get('Supplier Name'),
                    batch_number=item_data.get('batch_number') or item_data.get('Batch Number'),
                    storage_type=item_data.get('storage_type', 'UNIT'),
                    number_of_bags=final_bags_p,
                    total_qty=final_qty_p,
                    user_name=user_display,
                    is_merged=item_data.get('is_merged', False)
                )
                doc_ids_to_sync.append(d_id)
                
                # Accumulate for Log
                # -- COMPATIBILITY LAYER --
                item_qty = safe_float(item_data.get('Quantity Received (In Unit)') or item_data.get('Quantity Received', 0))
                total_qty_combined += item_qty
                log_details.append({
                    'product_name': item_data.get('Product Name'),
                    'product_code': item_data.get('Product Code'),
                    'qty': item_qty,
                    'unit': item_data.get('Unit', 'MT')
                })
                
                # Update link if generated
                if item_data.get('Barcode Link'):
                    inventory_service.update_barcode_link(d_id, item_data['Barcode Link'])
                    
                # ── SYNC LOCK: Mark as INGESTING so Reaper ignores it ──
                db.collection("inventory_positions").document(d_id).update({
                    "sync_status": "ingesting",
                    "updated_at": int(time.time())
                })
                    
            except Exception as e:
                print(f"⚠️ Item Pre-save Error: {e}")

        # 3. STEP 2: Sync to Google Sheets (Batched)
        try:
            print(f"📊 SYNCING TO SHEETS: Batch size {len(doc_ids_to_sync)}")
            await asyncio.to_thread(sheets_service.save_stock_batch, header, items, item_ids, user_display)
            
            # STEP 3: Mark as SUCCESS in Firestore
            for d_id in doc_ids_to_sync:
                try: inventory_service.mark_as_synced(d_id)
                except: pass
            print(f"✅ BATCH SYNC SUCCESSFUL: {len(doc_ids_to_sync)} docs finalized.")
            
        except Exception as sheet_err:
            print(f"❌ SHEETS SYNC FAILED: {sheet_err}")
            # Mark as ERROR for Janitor retry
            for d_id in doc_ids_to_sync:
                try: inventory_service.mark_as_sync_error(d_id, str(sheet_err))
                except: pass
            
            # Queue as a global task for extra persistence
            inventory_service.queue_pending_sync("INGESTION_BATCH", {
                "header": header,
                "items": items,
                "item_ids": item_ids,
                "user_display": user_display
            })

        # 4. Final Logging and Notification
        invoice_num = header.get('Invoice Number', 'INV-N/A')
        supplier = header.get('Supplier Name', 'N/A')
        total_items_in_batch = len(item_ids)

        activity_service.log_and_notify(
            user=user,
            action_type="IN",
            item_name=f"Invoice #{invoice_num}",
            product_code=f"{total_items_in_batch} Identities",
            qty_change=total_qty_combined,
            location=supplier,
            description=f"Batch Ingestion of {total_items_in_batch} stock items (Sync Status checked).",
            details=log_details
        )

    except Exception as e:
        print(f"🛑 CRITICAL ASYNC INGESTION FAILURE: {e}")
        traceback.print_exc()

async def _process_barcode_archiving_async(code_id: str, product_code: str, batch_number: str) -> str:
    """Internal helper to generate/upload QR using a thread and return the link."""
    try:
        # 1. DEDUPLICATION CHECK
        existing_link = await asyncio.to_thread(inventory_service.get_existing_barcode, code_id)

        if existing_link:
            print(f"♻️ [REUSE] Barcode link exists for {code_id}.")
            return existing_link

        # 2. GENERATE NEW
        print(f"🆕 Generating barcode for {code_id}...")
        link = await asyncio.to_thread(drive_service.generate_barcode, code_id, product_code, batch_number)
        return link or ""
    except Exception as e:
        print(f"Background QR Error [{code_id}]: {e}")
        return ""

@app.get("/stock/position/{dist_id}")
async def get_stock_by_position(
    dist_id: str,
    user: dict = Depends(get_current_user)
):
    """Fetch main item + target distribution for a shelf-specific QR code."""
    # Normalize ID for robust cross-index/legacy discovery
    clean_dist_id = normalize_id(dist_id)
    result = inventory_service.find_by_dist_id(clean_dist_id)
    if not result:
        # Fallback to raw ID for absolute compatibility if normalization was too aggressive
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
    # 0. Normalize Input
    clean_id = normalize_id(stock_item_id)

    # 1. Use robust discovery to find Position in Firestore (resolves barcode -> BATCH doc)
    # Search with clean ID first, fallback to raw
    result = inventory_service.find_by_dist_id(clean_id)
    if not result:
        result = inventory_service.find_by_dist_id(stock_item_id)
        
    pos = result.get('item')
    search_id = pos.get('barcode_id', clean_id) if pos else clean_id

    # 2. Get master details from Sheets
    item = sheets_service.get_stock_item(search_id)
    if not item:
        # Final fallback check with raw ID if sheets lookup failed with clean ID
        item = sheets_service.get_stock_item(stock_item_id)
        
    if not item:
        raise HTTPException(status_code=404, detail="Stock item not found")
        
    if pos:
        item['distributions'] = pos.get('distributions', [])
        item['doc_id'] = pos.get('doc_id')
        item['barcode_ids'] = pos.get('barcode_ids', [])
        
        # Merge missing metadata from Firestore Source of Truth
        if not item.get('supplier_name'):
            item['supplier_name'] = pos.get('supplier_name')
            
        # 🚨 DEEP SEARCH: If still N/A and it's a merged doc, look for ANY batch with this supplier
        if (not item.get('supplier_name') or item.get('supplier_name') == 'N/A') and pos.get('is_merged'):
            try:
                p_code = pos.get('product_code')
                if p_code:
                    batches = inventory_service.collection.where(filter=FieldFilter('product_code', '==', p_code))\
                                                           .where(filter=FieldFilter('is_merged', '==', False))\
                                                           .limit(1).get()
                    if batches:
                        item['supplier_name'] = batches[0].to_dict().get('supplier_name')
            except: pass

        if not item.get('batch_number'):
            item['batch_number'] = pos.get('batch_number')
        
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
        bags_removed = float(payload.get('bags_removed', 0))
        loc_id = payload.get('loc_id', 'default')
        loc_name = payload.get('location', 'Main Floor')
        usage = payload.get('usage', 'General')
        remarks = payload.get('remarks', '')
        
        print(f"📉 [REMOVE_START] ID: {stock_item_id} | Qty: {qty_to_remove} | Loc: {loc_name}")

        # 0. ROBUST LOOKUP: Prioritize Firestore Source of Truth for logging
        item_details = {}
        res = inventory_service.find_by_dist_id(stock_item_id)
        if res and res.get('item'):
            f_item = res['item']
            item_details = {
                'item_name': f_item.get('product_name', 'Stock Item'),
                'product_code': f_item.get('product_code', stock_item_id)
            }
        else:
            # Fallback to Sheets only if not in Firestore
            sheets_item = sheets_service.get_stock_item(stock_item_id)
            if sheets_item:
                item_details = {
                    'item_name': sheets_item.get('item_name'),
                    'product_code': sheets_item.get('product_code')
                }

        if not item_details:
            print(f"⚠️ [REMOVE_WARN] Item {stock_item_id} not found in any source.")
            raise HTTPException(status_code=404, detail="Stock item not found")

        # Update 'item' variable name to 'item_details' in follow-up code
        item = item_details
            
        # 1. Authoritative Deduction (Backend updates Firestore & Sheet)
        new_remaining = inventory_service.remove_stock_spatial(
            stock_item_id, loc_id, qty_to_remove, 
            user=user, bags_removed=bags_removed, skip_deduction=False
        )
        
        # 2. Log Activity & Notify Admins
        activity_service.log_and_notify(
            user=user,
            action_type="OUT",
            item_name=item.get('item_name', 'Stock Item'),
            product_code=item.get('product_code', stock_item_id),
            qty_change=-qty_to_remove,
            location=loc_name,
            description=remarks
        )
        
        print(f"✅ [REMOVE_SUCCESS] ID: {stock_item_id} | New Total: {new_remaining}")
        return {"message": "Stock removed successfully", "remaining": new_remaining}
    except ValueError as ve:
        # LOGIC ERROR: Immediate feedback (e.g. "Product not found in Sheets")
        print(f"🛑 [REMOVE_LOGIC_ERROR] {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        import traceback
        print(f"🛑 [REMOVE_ERROR] Fatal error during deduction for {stock_item_id}:")
        print(f"Payload: {payload}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Deduction system failure: {str(e)}")

@app.get("/reports/summary")
async def get_summary(period: str = "all", user: dict = Depends(require_staff)):
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
async def get_graph_data(user: dict = Depends(require_staff)):
    """
    Get 7-day time-series and zone distribution for dashboard charts.
    """
    try:
        return sheets_service.get_graph_data()
    except Exception as e:
        print(f"Graph Data Error: {e}")
        return {"movement": [], "zones": []}

@app.get("/logs")
async def get_logs(limit: int = 20, last_ts: int = None, search: str = None, user: dict = Depends(require_staff)):
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
        user_display = user.get('full_name', user['email'])
        
        # 0. Fetch Item details for rich logging and proportional calculation
        res = inventory_service.find_by_dist_id(req.barcode_id)
        if not res or not res.get('item'):
            raise HTTPException(status_code=404, detail="Stock position not found")
            
        f_item = res['item']
        storage_type = f_item.get('storage_type', 'UNIT')
        
        # Find the source distribution to calculate bags proportionally (if not provided explicitly)
        from_dist = next((d for d in f_item.get('distributions', []) if d.get('dist_id') == req.from_location), None)
        
        # Prioritize manual bag count if provided (>0)
        u_type = str(f_item.get('unit', '')).lower()
        st_type = str(f_item.get('storage_type', '')).upper()
        is_bag_item = 'bag' in u_type or st_type == 'BAG'

        if is_bag_item:
            # For bag items, bags always equals qty
            bags_to_move = req.quantity
        else:
            bags_to_move = float(req.bags) if req.bags > 0 else 0
            if bags_to_move <= 0 and from_dist and from_dist.get('qty', 0) > 0:
                current_qty = float(from_dist.get('qty', 0))
                current_bags = float(from_dist.get('number_of_bags') or from_dist.get('bags') or 0)
                ratio = req.quantity / current_qty
                bags_to_move = int(round(current_bags * ratio))

        # Determine if we should record "N/A" for metrics in sheets log
        final_qty_log = req.quantity
        final_bags_log = bags_to_move
        
        print(f"🔄 [RELOC-CALC] From: {req.from_location} | Qty: {req.quantity} | Bags: {req.bags}")
        print(f"⚖️ [RELOC-CALC] Computed: bags_to_move={bags_to_move} | is_bag_item={is_bag_item}")

        if is_bag_item:
            # Sheets Strategy: Keep Qty as N/A in the Movements ledger for bag items 
            final_qty_log = "N/A"
        elif st_type == 'UNIT' and bags_to_move <= 0:
            # Only use N/A if it's a UNIT item AND no bags were calculated/provided
            final_bags_log = "N/A"

        # 1. Update Firestore Atomic Map (Internal distributions)
        inventory_service.transfer_stock(
            doc_id=req.barcode_id, 
            from_loc_id=req.from_location, 
            to_loc_id=req.to_location, # ID for cross-indexing
            to_warehouse=req.to_warehouse_name,
            to_location=req.to_location_name,
            to_warehouse_id=req.to_warehouse_id, # [SCHEMA-ALIGNED]
            from_warehouse=req.from_warehouse_name,
            from_location=req.from_location_name,
            qty=req.quantity, 
            bags=bags_to_move
        )
        
        # 2. RELIABLE HYBRID SYNC: Google Sheets Movement
        loc_audit = f"{req.from_location_name} ➔ {req.to_location_name}"
        
        try:
            # Record in Sheets Movements ledger as RELOCATE
            sheets_service.record_relocation(
                barcode_id=f_item.get('barcode_id', req.barcode_id),
                qty=final_qty_log,
                bags=final_bags_log,
                from_location=req.from_location_name,
                to_location=req.to_location_name,
                from_warehouse=req.from_warehouse_name,
                to_warehouse=req.to_warehouse_name,
                user_display=user_display,
                product_name=f_item.get('product_name', 'Stock Item'),
                dist_id=req.from_location,
                warehouse_id=req.to_warehouse_id
            )
        except Exception as e:
            print(f"📡 [TRANSFER_SHEETS_ERROR] Failed to sync relocation to Sheets: {e}")
            # Resilience fallback: queue sync task (optional, here we rely on the manual janitor)
        
        # 3. Log Activity & Notify Admins (Global Audit Trail)
        activity_service.log_and_notify(
            user=user,
            action_type="TRANSFER",
            item_name=f_item.get('product_name', 'Stock Item'),
            product_code=f_item.get('product_code', req.barcode_id),
            qty_change=req.quantity,
            location=loc_audit,
            description=f"Relocated {req.quantity} ({bags_to_move} bags) from {req.from_location_name} to {req.to_location_name}."
        )

        # 4. Store in Dedicated Relocation Collection
        try:
            relocation_data = {
                'product_name': f_item.get('product_name', 'Stock Item'),
                'product_code': f_item.get('product_code', req.barcode_id),
                'barcode_id': f_item.get('barcode_id', req.barcode_id),
                'from_location': req.from_location_name,
                'to_location': req.to_location_name,
                'qty': float(req.quantity),
                'unit_type': from_dist.get('unit_type') if from_dist else f_item.get('unit', 'QTY'),
                'actor_name': user_display,
                'created_at': int(time.time()),
                'type': 'RELOCATE'
            }
            db.collection('relocation_logs').add(relocation_data)
        except Exception as e:
            print(f"📡 [RELOCATION_LOG_ERROR] Failed to save dedicated log: {e}")
        
        return {"status": "success", "message": f"Stock moved to {req.to_location_name} successfully."}
    except Exception as e:
        import traceback
        traceback.print_exc()
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

@app.post("/admin/beautify-sheets")
async def beautify_sheets(user: dict = Depends(get_current_user)):
    """Admin only: Trigger global UI/formatting refresh for all Google Sheets."""
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        from app.services.sheets import sheets_service
        sheets_service.beautify_all()
        return {"status": "success", "message": "All sheets have been beautified with Elite design tokens."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Beautification failed: {str(e)}")

# ── 🤖 ADMIN: SYNC JANITOR ──────────────────────────────────────────────────

@app.post("/admin/sync-janitor")
async def run_sync_janitor(background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    """Finds all failed or pending syncs and re-tries them."""
    if user.get('role') != 'admin':
        # Safety: allow in dev if needed, but enforce for prod
        raise HTTPException(status_code=403, detail="Admin access required")
    
    background_tasks.add_task(_janitor_routine)
    return {"message": "Sync Janitor worker started in background."}

async def _janitor_routine():
    """Logic to reconcile pending syncs."""
    print("🤖 JANITOR: Searching for pending syncs...")
    try:
        pendings = inventory_service.get_pending_syncs(limit=20)
        
        if not pendings:
            print("🤖 JANITOR: Database is healthy. No pending syncs.")
            return

        for item in pendings:
            try:
                print(f"🤖 JANITOR: Retrying sync for {item.get('doc_id')}")
                reconstructed_header = {
                    'Invoice Number': 'RECOVERY-SYNC',
                    'Transporter Name': item.get('transporter_name', 'RECOVERED')
                }
                # Sync to Sheets
                sheets_service.save_stock_batch(
                    header=reconstructed_header,
                    items=[item],
                    item_ids=[item.get('barcode_id', 'RECOVERED')],
                    user_display="Sync Janitor"
                )
                inventory_service.mark_as_synced(item.get('doc_id'))
            except Exception as e:
                print(f"🤖 JANITOR ERROR: Failed to recover {item.get('doc_id')}: {e}")
    except Exception as e:
        print(f"🤖 JANITOR SYSTEM ERROR: {e}")

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
