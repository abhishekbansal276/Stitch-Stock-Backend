from pydantic import BaseModel
from typing import List, Dict, Optional

class StockItemDistribution(BaseModel):
    location: str
    qty: float

class StockItem(BaseModel):
    product_code: str
    product_name: str
    quantity_received: float
    unit: str
    distributions: List[StockItemDistribution]

class StockRequest(BaseModel):
    header: Dict
    items: List[Dict]

class StockRemovalRequest(BaseModel):
    quantity: float
    location: str
    usage: str
    remarks: Optional[str] = ""
    transaction_id: Optional[str] = None

class StockTransferRequest(BaseModel):
    barcode_id: str
    from_location: str # loc_id
    from_location_name: str
    to_location: str # loc_id
    to_location_name: str
    quantity: float
    bags: float = 0 # Explicit bags for N/A items
    from_warehouse_name: str = "N/A"
    to_warehouse_name: str = "N/A"
    reason: Optional[str] = "Spatial Relocation"
