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
        # Flat list of unique locations for fast Firestore indexing/querying
        location_list = list(set([d.get('location', 'Main Floor') for d in distributions if float(d.get('qty', 0)) > 0]))
        
        doc_ref = self.collection.document(barcode_id)
        
        doc_ref.set({
            'barcode_id': barcode_id,
            'product_name': product_name,
            'product_code': product_code,
            'unit': unit,
            'total_qty': total_qty,
            'distributions': distributions,
            'locations': location_list, # Elite Indexing
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
        # Update locations index
        new_locations = list(set([d.get('location') for d in new_distributions if float(d.get('qty', 0)) > 0]))
        
        transaction.update(doc_ref, {
            'distributions': new_distributions,
            'total_qty': new_total,
            'locations': new_locations,
            'updated_at': int(time.time())
        })
        return new_total

    def remove_stock_spatial(self, barcode_id: str, location_name: str, qty: float):
        """Wrapper to perform a safe atomic deduction."""
        doc_ref = self.collection.document(barcode_id)
        transaction = db.transaction()
        return self.deduct_from_location(transaction, doc_ref, location_name, qty)

    # --- Warehouse Explorer & Transfer Methods ---

    def get_all_zones(self) -> List[Dict]:
        """Aggregate unique locations across all stock positions."""
        # Note: In a massive warehouse, we might want a separate 'zones' collection.
        # For this scale, we aggregate from the flattened 'locations' index.
        docs = self.collection.stream()
        zones = {}
        for doc in docs:
            data = doc.to_dict()
            for loc in data.get('locations', []):
                if loc not in zones:
                    zones[loc] = {"name": loc, "items_count": 0}
                zones[loc]["items_count"] += 1
        return list(zones.values())

    def get_items_in_zone(self, zone_name: str) -> List[Dict]:
        """Fetch all items residing in a specific physical coordinate."""
        docs = self.collection.where('locations', 'array_contains', zone_name).stream()
        results = []
        for doc in docs:
            data = doc.to_dict()
            # Filter the distribution list to only show the target zone's quantity
            matching_dist = next((d for d in data.get('distributions', []) if d['location'] == zone_name), None)
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
    def execute_transfer(self, transaction, doc_ref, from_loc: str, to_loc: str, qty: float):
        """Atomic inter-zone transfer within a stock document."""
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise Exception("Stock not found.")
        
        data = snapshot.to_dict()
        distributions = data.get('distributions', [])
        
        # 1. Deduct from Source
        source_found = False
        dest_found = False
        new_distributions = []
        
        for dist in distributions:
            if dist['location'] == from_loc:
                curr_qty = float(dist.get('qty', 0))
                if curr_qty < qty:
                    raise Exception(f"Insufficient stock in {from_loc}.")
                dist['qty'] = curr_qty - qty
                source_found = True
            
            if dist['location'] == to_loc:
                dist['qty'] = float(dist.get('qty', 0)) + qty
                dest_found = True
            
            new_distributions.append(dist)

        if not source_found:
            raise Exception(f"Source location {from_loc} not found.")

        # 2. Handle New Destination Zone
        if not dest_found:
            new_distributions.append({'location': to_loc, 'qty': qty})

        # 3. Finalize and Update Indices
        # Remove empty distributions to keep it clean
        cleaned_distributions = [d for d in new_distributions if float(d.get('qty', 0)) > 0]
        new_locations = list(set([d.get('location') for d in cleaned_distributions]))

        transaction.update(doc_ref, {
            'distributions': cleaned_distributions,
            'locations': new_locations,
            'updated_at': int(time.time())
        })
        return True

    def transfer_stock(self, barcode_id: str, from_loc: str, to_loc: str, qty: float):
        """Wrapper to trigger atomic spatial transfer."""
        doc_ref = self.collection.document(barcode_id)
        transaction = db.transaction()
        return self.execute_transfer(transaction, doc_ref, from_loc, to_loc, qty)

inventory_service = InventoryService()
