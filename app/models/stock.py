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
    from_location: str
    to_location: str
    quantity: float
    reason: Optional[str] = "Spatial Relocation"
