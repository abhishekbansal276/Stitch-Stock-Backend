import time
import uuid
from typing import List, Dict
from app.services.firebase import db
from google.cloud import firestore
from app.services.email_service import email_service

class InventoryService:
    def __init__(self):
        self.collection = db.collection('inventory_positions')

    def save_position(self, barcode_id: str, product_name: str, product_code: str, unit: str, distributions: List[Dict]):
        """
        Stores the spatial distribution of a stock item in Firestore.
        distributions: [{'warehouse': 'DC1', 'location': 'Row 7', 'qty': 10, 'dist_id': 'UID'}, ...]
        """
        # Ensure every distribution split has a unique traceable ID
        for d in distributions:
            if not d.get('dist_id') or d.get('dist_id') == 'AUTO':
                d['dist_id'] = f"POS-{str(uuid.uuid4())[:8].upper()}"
        
        total_qty = sum(float(d.get('qty', 0)) for d in distributions if d.get('qty'))
        # Store flat list of warehouses and specific locations for search
        search_locations = []
        for d in distributions:
            if float(d.get('qty', 0)) > 0:
                search_locations.append(d.get('warehouse'))
                search_locations.append(d.get('warehouse_id'))
                search_locations.append(f"{d.get('warehouse')} - {d.get('location')}")
        
        doc_ref = self.collection.document(barcode_id)
        existing = doc_ref.get()
        min_stock = 0
        if existing.exists:
            min_stock = existing.to_dict().get('min_stock_level', 0)

        doc_ref.set({
            'barcode_id': barcode_id,
            'product_name': product_name,
            'product_code': product_code,
            'unit': unit,
            'total_qty': total_qty,
            'distributions': distributions,
            'location_ids': list(set([l for l in search_locations if l])),
            'min_stock_level': min_stock,
            'updated_at': int(time.time())
        })

    def get_position(self, barcode_id: str) -> Dict:
        """Fetches the current spatial map for a barcode."""
        doc = self.collection.document(barcode_id).get()
        if doc.exists:
            return doc.to_dict()
        return {}

    @firestore.transactional
    def deduct_from_location(self, transaction, doc_ref, loc_id: str, qty: float):
        """Atomic deduction from a specific shelf/zone ID."""
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise Exception("Stock position not found in Firestore")
        
        data = snapshot.to_dict()
        distributions = data.get('distributions', [])
        found = False
        
        new_distributions = []
        for dist in distributions:
            if dist.get('dist_id') == loc_id: # loc_id here refers to the specific Position ID (dist_id)
                curr_qty = float(dist.get('qty', 0))
                if curr_qty < qty:
                    raise Exception(f"Insufficient stock in this position. Available: {curr_qty}")
                dist['qty'] = curr_qty - qty
                found = True
            new_distributions.append(dist)
            
        if not found:
            raise Exception(f"Position ID {loc_id} not found for this item.")
            
        new_total = data.get('total_qty', 0) - qty
        # Update searchable locations index
        search_locations = []
        for d in new_distributions:
            if float(d.get('qty', 0)) > 0:
                search_locations.append(d.get('warehouse'))
                search_locations.append(f"{d.get('warehouse')} - {d.get('location')}")
        
        new_location_ids = list(set([l for l in search_locations if l]))
        
        # Check for Low Stock Alert
        min_level = data.get('min_stock_level', 0)
        if new_total <= min_level and min_level > 0:
            # We only alert if it wasn't already below (to avoid repeat emails)
            # Or if it's the first time dipping
            old_total = data.get('total_qty', 0)
            if old_total > min_level:
                email_service.send_low_stock_alert(
                    data['product_name'], 
                    data['product_code'], 
                    new_total, 
                    min_level, 
                    data['unit']
                )

        transaction.update(doc_ref, {
            'distributions': new_distributions,
            'total_qty': new_total,
            'location_ids': new_location_ids,
            'updated_at': int(time.time())
        })
        return new_total

    def remove_stock_spatial(self, barcode_id: str, loc_id: str, qty: float):
        """Wrapper to perform a safe atomic deduction."""
        doc_ref = self.collection.document(barcode_id)
        transaction = db.transaction()
        return self.deduct_from_location(transaction, doc_ref, loc_id, qty)

    # --- Warehouse Explorer & Transfer Methods ---

    def get_all_zones(self) -> List[Dict]:
        """Aggregate unique locations across all stock positions with total quantities."""
        docs = self.collection.stream()
        zones = {}
        for doc in docs:
            data = doc.to_dict()
            for dist in data.get('distributions', []):
                # We use a combined key for aggregation in the explorer
                warehouse = dist.get('warehouse', 'Main Warehouse')
                location = dist.get('location', 'Full Receive')
                key = f"{warehouse} - {location}"
                qty = float(dist.get('qty', 0))
                
                if key not in zones:
                    zones[key] = {"id": key, "name": location, "warehouse": warehouse, "items_count": 0, "total_stock": 0.0}
                
                zones[key]["items_count"] += 1
                zones[key]["total_stock"] += qty
        return list(zones.values())

    def get_inventory_summary(self) -> List[Dict]:
        """God View: Returns ALL products and where they are located."""
        docs = self.collection.stream()
        items = []
        for doc in docs:
            data = doc.to_dict()
            items.append({
                'barcode_id': data['barcode_id'],
                'product_name': data['product_name'],
                'product_code': data['product_code'],
                'total_qty': data['total_qty'],
                'unit': data['unit'],
                'updated_at': data.get('updated_at'),
                'min_stock_level': data.get('min_stock_level', 0),
                'barcode_link': data.get('barcode_link'),
                'distributions': data.get('distributions', [])
            })
        return items

    def set_min_stock_level(self, barcode_id: str, min_level: float):
        """Admin override for threshold alerts."""
        self.collection.document(barcode_id).update({
            'min_stock_level': min_level,
            'updated_at': int(time.time())
        })

    def update_barcode_link(self, barcode_id: str, link: str):
        """Updates the stored link to the cloud-archived barcode image."""
        self.collection.document(barcode_id).update({
            'barcode_link': link,
            'updated_at': int(time.time())
        })

    def get_items_in_zone(self, zone_key: str) -> List[Dict]:
        """Fetch all items residing in a specific physical ID or hierarchical key."""
        # zone_key is "Warehouse - Location"
        docs = self.collection.where('location_ids', 'array_contains', zone_key).stream()
        results = []
        for doc in docs:
            data = doc.to_dict()
            # Match specific distribution entry
            distributions = data.get('distributions', [])
            for dist in distributions:
                key = f"{dist.get('warehouse')} - {dist.get('location')}"
                if key == zone_key:
                    results.append({
                        "barcode_id": data['barcode_id'],
                        "product_name": data['product_name'],
                        "product_code": data['product_code'],
                        "unit": data['unit'],
                        "zone_qty": dist['qty'],
                        "total_qty": data['total_qty'],
                        "dist_id": dist.get('dist_id')
                    })
        return results

    @firestore.transactional
    def execute_transfer(self, transaction, doc_ref, from_dist_id: str, to_warehouse: str, to_location: str, qty: float):
        """Atomic inter-zone transfer within a stock document."""
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise Exception("Stock not found.")
        
        data = snapshot.to_dict()
        distributions = data.get('distributions', [])
        
        source_found = False
        dest_found = False
        new_distributions = []
        
        # 1. Deduct from source
        for dist in distributions:
            if dist.get('dist_id') == from_dist_id:
                curr_qty = float(dist.get('qty', 0))
                if curr_qty < qty:
                    raise Exception(f"Insufficient stock in source position.")
                dist['qty'] = curr_qty - qty
                source_found = True
            
            # Check if destination exists (Warehouse matches AND Location matches)
            if dist.get('warehouse') == to_warehouse and dist.get('location') == to_location:
                dist['qty'] = float(dist.get('qty', 0)) + qty
                dest_found = True
            
            new_distributions.append(dist)

        if not source_found:
            raise Exception(f"Source Position ID {from_dist_id} not found.")

        if not dest_found:
            # Create new distribution ID for the new location
            new_dist_id = f"POS-{str(uuid.uuid4())[:8].upper()}"
            new_distributions.append({
                'warehouse': to_warehouse, 
                'location': to_location, 
                'qty': qty, 
                'dist_id': new_dist_id
            })

        cleaned_distributions = [d for d in new_distributions if float(d.get('qty', 0)) > 0]
        
        # Recalculate location search index
        search_locations = []
        for d in cleaned_distributions:
            search_locations.append(d.get('warehouse'))
            search_locations.append(f"{d.get('warehouse')} - {d.get('location')}")
        
        new_location_ids = list(set([l for l in search_locations if l]))

        transaction.update(doc_ref, {
            'distributions': cleaned_distributions,
            'location_ids': new_location_ids,
            'updated_at': int(time.time())
        })
        return True

    def transfer_stock(self, barcode_id: str, from_loc_id: str, to_loc_id: str, to_loc_name: str, qty: float):
        doc_ref = self.collection.document(barcode_id)
        transaction = db.transaction()
        return self.execute_transfer(transaction, doc_ref, from_loc_id, to_loc_id, to_loc_name, qty)

inventory_service = InventoryService()
