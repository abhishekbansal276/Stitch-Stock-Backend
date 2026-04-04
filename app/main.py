from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from app.routers import users
from app.services.firebase import initialize_firebase
from app.services.ocr import ocr_service
from app.services.sheets import sheets_service
from app.dependencies.auth import get_current_user

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

# Include routers
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
        extracted_data = ocr_service.extract_from_file(content, file.filename)
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
    If product_code exists, update/merge. Otherwise, create new row.
    """
    try:
        header = payload.get('header', {})
        items = payload.get('items', [])
        
        # Save to (Mock) Google Sheets
        item_ids = sheets_service.save_stock(header, items, user['email'])
        
        return {"message": "Stock created successfully", "stock_item_ids": item_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Creation failed: {str(e)}")

@app.get("/stock/{stock_item_id}")
async def get_stock_item(
    stock_item_id: str,
    user: dict = Depends(get_current_user)
):
    """
    Fetch details for a specific stock item (called after scanning QR).
    """
    item = sheets_service.get_stock_item(stock_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Stock item not found")
    return item

@app.post("/stock/{stock_item_id}/remove")
async def remove_stock(
    stock_item_id: str,
    payload: dict,
    user: dict = Depends(get_current_user)
):
    """
    Deduct quantity from a stock item and record the movement.
    """
    try:
        qty_to_remove = float(payload.get('quantity', 0))
        usage = payload.get('usage', 'General')
        remarks = payload.get('remarks', '')
        
        item = sheets_service.get_stock_item(stock_item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Stock item not found")
            
        if qty_to_remove <= 0 or qty_to_remove > item['quantity_remaining']:
            raise HTTPException(status_code=400, detail="Invalid quantity")
        
        # Update (Mock) Sheets
        item['quantity_remaining'] -= qty_to_remove
        if item['quantity_remaining'] == 0:
            item['status'] = 'CONSUMED'
        elif item['quantity_remaining'] < item['quantity_total']:
            item['status'] = 'PARTIAL'
            
        # Record Movement
        sheets_service.add_movement(stock_item_id, item['transaction_id'], 'OUT', -qty_to_remove, user['email'])
        
        return {"message": "Stock removed successfully", "remaining": item['quantity_remaining']}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Removal failed: {str(e)}")

@app.get("/reports/summary")
async def get_summary(user: dict = Depends(get_current_user)):
    """
    Get summary data for the dashboard (Total In, Out, Balance).
    In 'Real Integration', this fetches from the 'stock_summary' sheet.
    """
    # MOCK: In production, query Google Sheets summary sheet
    return {
        "total_in": 1250,
        "total_out": 450,
        "available_balance": 800,
        "low_stock_count": 3
    }

@app.get("/logs")
async def get_logs(user: dict = Depends(get_current_user)):
    """
    Get audit logs from Firestore.
    """
    try:
        # Fetching from Firestore collection
        logs_ref = db.collection('activity_logs').order_by('created_at', direction='descending').limit(10)
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
