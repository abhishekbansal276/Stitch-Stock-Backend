import time
import os
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from app.routers import users
from app.services.firebase import initialize_firebase, db
from app.services.ocr import ocr_service
from app.services.sheets import sheets_service
from app.services.inventory import inventory_service
from app.dependencies.auth import get_current_user

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

# Root/Health check
@app.get("/")
def read_root():
    return {"message": "Stitch Stock Management System API is live"}

# Include routers - Users router handles user management and /me
app.include_router(users.router)

@app.post("/stock/extract")
async def extract_stock(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    """
    Upload a PDF or Image and get extracted JSON data.
    """
    try:
        content = await file.read()
        
        # ELITE SYNC: Get existing headers to help Gemini map them correctly
        existing_headers = sheets_service.get_current_headers()
        
        extracted_data = ocr_service.extract_from_file(content, file.filename, existing_headers)
        return extracted_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

@app.post("/stock/create")
async def create_stock(
    payload: dict,
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
        item_ids = sheets_service.save_stock(header, items, user_display)
        
        # 2. Persist Spatial Positions to Firestore
        for i, item_id in enumerate(item_ids):
            item_data = items[i]
            distributions = item_data.get('distributions', [
                {'location': 'Main Floor', 'qty': item_data.get('Quantity Received', 0)}
            ])
            inventory_service.save_position(
                item_id, item_data.get('Product Name'), 
                item_data.get('Product Code'), 
                item_data.get('Unit', 'PCS'),
                distributions
            )
        
        return {"message": "Stock created successfully", "stock_item_ids": item_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Creation failed: {str(e)}")

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
        location_name = payload.get('location', 'Main Floor')
        usage = payload.get('usage', 'General')
        remarks = payload.get('remarks', '')
        
        item = sheets_service.get_stock_item(stock_item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Stock item not found")
            
        # 1. Atomic Firestore Deduction (Spatial Map)
        inventory_service.remove_stock_spatial(stock_item_id, location_name, qty_to_remove)
        
        # 2. Update Sheets Ledger
        new_remaining = item['quantity_remaining'] - qty_to_remove
        sheets_service.update_stock_quantity(stock_item_id, new_remaining)
            
        # 3. Record Movement (OUT) with Location Tag
        trans_id = payload.get('transaction_id', f"OUT-{int(time.time())}")
        sheets_service.add_movement(stock_item_id, trans_id, 'OUT', -qty_to_remove, user['email'], location=location_name)
        
        return {"message": "Stock removed successfully", "remaining": new_remaining}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Removal failed: {str(e)}")

@app.get("/reports/summary")
async def get_summary(user: dict = Depends(get_current_user)):
    """
    Get live summary data from the 'Stock Summary' sheet.
    """
    try:
        return sheets_service.get_summary_stats()
    except Exception as e:
        print(f"Summary Fetch Error: {e}")
        return {
            "total_in": 0,
            "total_out": 0,
            "available_balance": 0,
            "low_stock_count": 0
        }

@app.get("/logs")
async def get_logs(user: dict = Depends(get_current_user)):
    """
    Get audit logs from Firestore.
    """
    try:
        # Fetching from Firestore collection
        logs_ref = db.collection('activity_logs').order_by('created_at', direction='descending').limit(15)
        logs = [doc.to_dict() for doc in logs_ref.stream()]
        
        # Fallback for empty collections
        if not logs:
            return [
                {
                    "item_name": "Welcome to Stitch!",
                    "movement_type": "INFO",
                    "quantity_changed": 0,
                    "actor_email": "System",
                    "created_at": int(time.time()),
                    "stock_item_id": "STK-000"
                }
            ]
        return logs
    except Exception as e:
        print(f"Log fetch failed: {e}")
        return []

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
