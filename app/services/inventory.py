import time
from typing import List, Dict
from app.services.firebase import db
from google.cloud import firestore

class InventoryService:
    def __init__(self):
        self.collection = db.collection('inventory_positions')

    def save_position(self, barcode_id: str, product_name: str, product_code: str, unit: str, distributions: List[Dict]):
        """
        Stores the spatial distribution of a stock item in Firestore.
        distributions: [{'location': 'Shed A', 'qty': 10, 'bags': 4}, ...]
        """
        total_qty = sum(float(d.get('qty', 0)) for d in distributions)
        doc_ref = self.collection.document(barcode_id)
        
        doc_ref.set({
            'barcode_id': barcode_id,
            'product_name': product_name,
            'product_code': product_code,
            'unit': unit,
            'total_qty': total_qty,
            'distributions': distributions,
            'updated_at': int(time.time())
        })

    def get_position(self, barcode_id: str) -> Dict:
        """Fetches the current spatial map for a barcode."""
        doc = self.collection.document(barcode_id).get()
        if doc.exists:
            return doc.to_dict()
        return {}

    @firestore.transactional
    def deduct_from_location(self, transaction, doc_ref, location_name: str, qty: float):
        """Atomic deduction from a specific shelf/zone."""
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise Exception("Stock position not found in Firestore")
        
        data = snapshot.to_dict()
        distributions = data.get('distributions', [])
        found = False
        
        new_distributions = []
        for dist in distributions:
            if dist['location'] == location_name:
                curr_qty = float(dist.get('qty', 0))
                if curr_qty < qty:
                    raise Exception(f"Insufficient stock in {location_name}. Available: {curr_qty}")
                dist['qty'] = curr_qty - qty
                found = True
            new_distributions.append(dist)
            
        if not found:
            raise Exception(f"Location {location_name} not found for this item.")
            
        new_total = data.get('total_qty', 0) - qty
        
        transaction.update(doc_ref, {
            'distributions': new_distributions,
            'total_qty': new_total,
            'updated_at': int(time.time())
        })
        return new_total

    def remove_stock_spatial(self, barcode_id: str, location_name: str, qty: float):
        """Wrapper to perform a safe atomic deduction."""
        doc_ref = self.collection.document(barcode_id)
        transaction = db.transaction()
        return self.deduct_from_location(transaction, doc_ref, location_name, qty)

inventory_service = InventoryService()
