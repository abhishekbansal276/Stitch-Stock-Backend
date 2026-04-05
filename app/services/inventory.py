import time
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
        distributions: [{'loc_id': 'LOC-1', 'loc_name': 'Shed A', 'qty': 10}, ...]
        """
        total_qty = sum(float(d.get('qty', 0)) for d in distributions if d.get('qty'))
        # Flat list of location IDs for fast Firestore indexing/querying
        location_ids = list(set([d.get('loc_id') for d in distributions if float(d.get('qty', 0)) > 0]))
        
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
            'location_ids': location_ids,
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
            if dist['loc_id'] == loc_id:
                curr_qty = float(dist.get('qty', 0))
                if curr_qty < qty:
                    raise Exception(f"Insufficient stock in {dist['loc_name']}. Available: {curr_qty}")
                dist['qty'] = curr_qty - qty
                found = True
            new_distributions.append(dist)
            
        if not found:
            raise Exception(f"Location ID {loc_id} not found for this item.")
            
        new_total = data.get('total_qty', 0) - qty
        # Update locations index
        new_location_ids = list(set([d.get('loc_id') for d in new_distributions if float(d.get('qty', 0)) > 0]))
        
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
        """Aggregate unique locations across all stock positions."""
        docs = self.collection.stream()
        zones = {}
        for doc in docs:
            data = doc.to_dict()
            for dist in data.get('distributions', []):
                loc_id = dist.get('loc_id')
                loc_name = dist.get('loc_name', 'Unknown')
                if loc_id and loc_id not in zones:
                    zones[loc_id] = {"id": loc_id, "name": loc_name, "items_count": 0}
                if loc_id:
                    zones[loc_id]["items_count"] += 1
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
                'distributions': data.get('distributions', [])
            })
        return items

    def set_min_stock_level(self, barcode_id: str, min_level: float):
        """Admin override for threshold alerts."""
        self.collection.document(barcode_id).update({
            'min_stock_level': min_level,
            'updated_at': int(time.time())
        })

    def get_items_in_zone(self, loc_id: str) -> List[Dict]:
        """Fetch all items residing in a specific physical ID."""
        docs = self.collection.where('location_ids', 'array_contains', loc_id).stream()
        results = []
        for doc in docs:
            data = doc.to_dict()
            matching_dist = next((d for d in data.get('distributions', []) if d['loc_id'] == loc_id), None)
            if matching_dist:
                results.append({
                    "barcode_id": data['barcode_id'],
                    "product_name": data['product_name'],
                    "product_code": data['product_code'],
                    "unit": data['unit'],
                    "zone_qty": matching_dist['qty'],
                    "total_qty": data['total_qty']
                })
        return results

    @firestore.transactional
    def execute_transfer(self, transaction, doc_ref, from_loc_id: str, to_loc_id: str, to_loc_name: str, qty: float):
        """Atomic inter-zone transfer within a stock document."""
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise Exception("Stock not found.")
        
        data = snapshot.to_dict()
        distributions = data.get('distributions', [])
        
        source_found = False
        dest_found = False
        new_distributions = []
        
        for dist in distributions:
            if dist['loc_id'] == from_loc_id:
                curr_qty = float(dist.get('qty', 0))
                if curr_qty < qty:
                    raise Exception(f"Insufficient stock in source.")
                dist['qty'] = curr_qty - qty
                source_found = True
            
            if dist['loc_id'] == to_loc_id:
                dist['qty'] = float(dist.get('qty', 0)) + qty
                dest_found = True
            
            new_distributions.append(dist)

        if not source_found:
            raise Exception(f"Source location ID {from_loc_id} not found.")

        if not dest_found:
            new_distributions.append({'loc_id': to_loc_id, 'loc_name': to_loc_name, 'qty': qty})

        cleaned_distributions = [d for d in new_distributions if float(d.get('qty', 0)) > 0]
        new_location_ids = list(set([d.get('loc_id') for d in cleaned_distributions]))

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
