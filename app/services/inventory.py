import time
import uuid
from typing import List, Dict
from app.services.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud import firestore
from app.services.email_service import email_service

class InventoryService:
    def __init__(self):
        self.collection = db.collection('inventory_positions')
        self.index_collection = db.collection('position_cross_index')

    def save_position(self, barcode_id: str, product_name: str, product_code: str, 
                      unit: str, distributions: List[Dict], 
                      supplier_name: str = None, batch_number: str = None,
                      storage_type: str = "UNIT", number_of_bags: int = 0,
                      user_name: str = "System"):
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
                search_locations.append(d.get('dist_id'))
                search_locations.append(f"{d.get('warehouse')} - {d.get('location')}")
        
        doc_ref = self.collection.document(barcode_id)
        existing = doc_ref.get()
        min_stock = 0
        if existing.exists:
            min_stock = existing.to_dict().get('min_stock_level', 0)

        doc_data = {
            'barcode_id': barcode_id,
            'product_name': product_name,
            'product_code': product_code,
            'unit': unit,
            'total_qty': total_qty,
            'distributions': distributions,
            'location_ids': list(set([l for l in search_locations if l])),
            'min_stock_level': min_stock,
            'supplier_name': supplier_name,
            'batch_number': batch_number,
            'storage_type': storage_type,
            'number_of_bags': number_of_bags,
            'created_by': user_name,
            'updated_by': user_name,
            'updated_at': int(time.time())
        }
        doc_ref.set(doc_data)
        
        # ── CROSS-INDEX UPDATE ──
        self._update_cross_index(barcode_id, distributions)

    def get_position(self, barcode_id: str) -> Dict:
        """Fetches the current spatial map for a barcode."""
        doc = self.collection.document(barcode_id).get()
        if doc.exists:
            return doc.to_dict()
        return {}

    def update_distribution_qr(self, barcode_id: str, dist_id: str, qr_link: str):
        """Atomically updates a specific distribution's QR link in Firestore."""
        doc_ref = self.collection.document(barcode_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            distributions = data.get('distributions', [])
            updated = False
            for d in distributions:
                if d.get('dist_id') == dist_id:
                    d['qr_link'] = qr_link
                    updated = True
                    break
            if updated:
                doc_ref.update({'distributions': distributions})
                self._update_cross_index(barcode_id, distributions)

    def find_by_dist_id(self, dist_id: str) -> Dict:
        """
        Robust search for a stock item by its specific shelf/zone position ID.
        Uses a two-tier O(1) direct lookup system for 100% reliability.
        """
        # ── TIER 1: CROSS-INDEX LOOKUP (Absolute Reliability) ──
        idx_doc = self.index_collection.document(dist_id).get()
        if idx_doc.exists:
            map_data = idx_doc.to_dict()
            parent_id = map_data.get('barcode_id')
            if parent_id:
                parent_doc = self.collection.document(parent_id).get()
                if parent_doc.exists:
                    data = parent_doc.to_dict()
                    target_dist = next((d for d in data.get('distributions', []) if d.get('dist_id') == dist_id), None)
                    return {"item": data, "target_distribution": target_dist}

        # ── TIER 2: DIRECT BARCODE LOOKUP (Fallback for main labels) ──
        doc = self.collection.document(dist_id).get()
        if doc.exists:
            data = doc.to_dict()
            return {"item": data, "target_distribution": data.get('distributions', [None])[0]}

        # ── TIER 3: ARRAY INDEX SEARCH (Safety Fallback) ──
        query = self.collection.where(filter=FieldFilter('location_ids', 'array_contains', dist_id)).limit(1).get()
        if query:
            doc = query[0]
            data = doc.to_dict()
            target_dist = next((d for d in data.get('distributions', []) if d.get('dist_id') == dist_id), None)
            return {"item": data, "target_distribution": target_dist}
            
        print(f"⚠️ DISPATCH FAILED: Position {dist_id} not found in any index.")
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
                search_locations.append(d.get('dist_id'))
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
        # Note: We don't remove from cross-index here to avoid race conditions; 
        # lookups will fail gracefully if distributions change.
        return new_total

    def remove_stock_spatial(self, barcode_id: str, loc_id: str, qty: float, user: dict = None, bags_removed: float = 0):
        """Wrapper to perform a safe atomic deduction."""
        doc_ref = self.collection.document(barcode_id)
        transaction = db.transaction()
        new_total = self.deduct_from_location(transaction, doc_ref, loc_id, qty)
        
        # ── SYNC TO SHEETS ──
        try:
            from app.services.sheets import sheets_service
            # Fetch doc again to get warehouse/location for movement record
            data = doc_ref.get().to_dict()
            dist = next((d for d in data.get('distributions', []) if d.get('dist_id') == loc_id), {})
            user_display = user.get('full_name', user['email']) if user else "System"
            
            sheets_service.record_dispatch(
                barcode_id=barcode_id,
                qty=qty,
                bags_removed=bags_removed,
                warehouse=dist.get('warehouse', 'Main Floor'),
                location=dist.get('location', 'General'),
                user_display=user_display,
                dist_id=loc_id,
                warehouse_id=dist.get('warehouse_id', 'default')
            )
        except Exception as e:
            print(f"⚠️ Sheets Sync Failed (Dispatch): {e}")
            
        return new_total

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
        docs = self.collection.where(filter=FieldFilter('location_ids', 'array_contains', zone_key)).stream()
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
            search_locations.append(d.get('dist_id'))
            search_locations.append(f"{d.get('warehouse')} - {d.get('location')}")
        
        new_location_ids = list(set([l for l in search_locations if l]))

        transaction.update(doc_ref, {
            'distributions': cleaned_distributions,
            'location_ids': new_location_ids,
            'updated_at': int(time.time())
        })
        # Update index for new positions
        self._update_cross_index(data.get('barcode_id'), cleaned_distributions)
        return True

    def transfer_stock(self, barcode_id: str, from_loc_id: str, to_loc_id: str, to_loc_name: str, qty: float):
        doc_ref = self.collection.document(barcode_id)
        transaction = db.transaction()
        return self.execute_transfer(transaction, doc_ref, from_loc_id, to_loc_id, to_loc_name, qty)

    def _update_cross_index(self, barcode_id: str, distributions: List[Dict]):
        """Internal worker to map position IDs back to parent items for O(1) discovery."""
        if not barcode_id: return
        batch = db.batch()
        for d in distributions:
            dist_id = d.get('dist_id')
            if dist_id and dist_id != 'AUTO':
                idx_ref = self.index_collection.document(dist_id)
                batch.set(idx_ref, {
                    'barcode_id': barcode_id,
                    'warehouse': d.get('warehouse'),
                    'location': d.get('location'),
                    'updated_at': int(time.time())
                })
        batch.commit()

inventory_service = InventoryService()
