import time
import uuid
import re
from typing import List, Dict
from app.services.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud import firestore
from app.services.email_service import email_service
from app.utils import generate_12_digit_hash, normalize_id, safe_float, safe_int

class InventoryService:
    def __init__(self):
        self.collection = db.collection('inventory_positions')
        self.index_collection = db.collection('position_cross_index')

    def upsert_position(self, barcode_id: str, product_name: str, product_code: str, 
                       unit: str, distributions: List[Dict], 
                       supplier_name: str = None, batch_number: str = None,
                       storage_type: str = "UNIT", number_of_bags: int = 0,
                       user_name: str = "System", is_merged: bool = False):
        """
        UPGRADED: One Doc per Product/Batch.
        Instead of using barcode_id as DocID, we query for existing doc first.
        """
        # ── 1. NORMALIZE METADATA ──
        batch_number = str(batch_number or 'NB').strip().upper() if batch_number else 'NB'
        if batch_number == "": batch_number = 'NB'
        
        # ── 2. DETERMINISTIC IDENTITY (Safety against Duplicates) ──
        clean_code = normalize_id(product_code)
        if is_merged:
            target_doc_id = f"PROD-{clean_code}"
        else:
            clean_batch = normalize_id(batch_number)
            target_doc_id = f"BATCH-{clean_code}-{clean_batch}"
            
        doc_ref = self.collection.document(target_doc_id)
        doc_snap = doc_ref.get()
        
        existing_data = {}
        legacy_doc_ids = []
        
        # ── 3. LEGACY HUNT: Find and Merge auto-generated 'Ghost' documents ──
        try:
            # Targeted Search: 
            # - If summary (PROD-), we can pull broadly to aggregate.
            # - If batch-specific, ONLY merge if it's an exact batch match.
            query = self.collection.where(filter=FieldFilter('product_code', '==', product_code))
            
            if not is_merged:
                # IMPORTANT: Only pull documents that share the same batch to avoid contamination.
                query = query.where(filter=FieldFilter('batch_number', '==', batch_number))
                
            legacy_docs = list(query.stream())
            
            for ld in legacy_docs:
                l_data = ld.to_dict()
                
                # S-Tier Protection: NEVER merge a summary (PROD-) into a specific batch document.
                if not is_merged and l_data.get('is_merged') == True:
                    continue

                if ld.id == target_doc_id:
                    existing_data = l_data
                else:
                    # Found a legacy or matching batch document! Merge its guts.
                    print(f"🕵️ LEGACY HUNT: Found related document {ld.id} for {product_code} (Batch: {batch_number}). Merging...")
                    legacy_doc_ids.append(ld.id)
                    
                    # Merge distributions from ghost to master
                    ghost_dists = l_data.get('distributions', [])
                    master_dists = existing_data.get('distributions', [])
                    
                    # Shallow merge for speed, cleanup happens below
                    existing_data['distributions'] = master_dists + ghost_dists
                    
                    # Merge barcode_ids
                    ghost_bids = l_data.get('barcode_ids', [])
                    master_bids = existing_data.get('barcode_ids', [])
                    existing_data['barcode_ids'] = list(set(master_bids + ghost_bids))

            if not existing_data and doc_snap.exists:
                existing_data = doc_snap.to_dict()

            if doc_snap.exists:
                print(f"📦 UPSERT: Consolidating into master document {target_doc_id}")
            else:
                print(f"🆕 UPSERT: Initializing master document {target_doc_id}")
        except Exception as e:
            print(f"⚠️ Legacy Hunt Error: {e}")
            if doc_snap.exists: existing_data = doc_snap.to_dict()

        # ── 2. PREPARE & MERGE DISTRIBUTIONS ──
        current_dists = existing_data.get('distributions', [])
        
        # MERGE LOGIC: Combine incoming distributions with existing ones
        merged_dists = current_dists.copy()
        for new_d in distributions:
            # Ensure incoming dist has an ID and a local batch reference
            batch_ref = new_d.get('batch_number') or batch_number or 'NB'
            if not new_d.get('dist_id') or new_d.get('dist_id') == 'AUTO':
                # Deterministic seed based on product, location and batch
                seed = f"{clean_code}-{new_d.get('warehouse')}-{new_d.get('location')}-{batch_ref}"
                new_d['dist_id'] = generate_12_digit_hash(seed)
            
            # If the incoming distribution doesn't have a batch, use the parent one
            if not new_d.get('batch_number') or str(new_d['batch_number']).strip() == "":
                new_d['batch_number'] = batch_number

            found_idx = -1
            for idx, old_d in enumerate(merged_dists):
                if old_d.get('warehouse') == new_d.get('warehouse') and \
                   old_d.get('location') == new_d.get('location') and \
                   old_d.get('batch_number') == new_d.get('batch_number'): # MATCH PER BATCH POSITION
                    found_idx = idx
                    break
            
            if found_idx >= 0:
                merged_dists[found_idx]['qty'] = safe_float(merged_dists[found_idx].get('qty', 0)) + safe_float(new_d.get('qty', 0))
                if 'bags' in new_d:
                    old_bags = merged_dists[found_idx].get('bags', 0)
                    new_bags = new_d.get('bags', 0)
                    merged_dists[found_idx]['bags'] = safe_int(old_bags) + safe_int(new_bags)
            else:
                merged_dists.append(new_d)

        # ── 3. FINAL AUDIT & CONSOLIDATION: Merge distribution duplicates ──
        consolidated = {}
        for d in merged_dists:
            # Identity key: Warehouse + Location + Batch
            key = f"{normalize_id(d.get('warehouse', 'WH'))}-{normalize_id(d.get('location', 'LOC'))}-{normalize_id(d.get('batch_number', 'NB'))}"
            
            if key not in consolidated:
                consolidated[key] = d
            else:
                consolidated[key]['qty'] = safe_float(consolidated[key].get('qty', 0)) + safe_float(d.get('qty', 0))
                consolidated[key]['bags'] = safe_int(consolidated[key].get('bags', 0)) + safe_int(d.get('bags', 0))
        
        merged_dists = list(consolidated.values())

        # Final audit for missing batch numbers
        for d in merged_dists:
            if not d.get('batch_number') or str(d['batch_number']).strip() == "":
                d['batch_number'] = batch_number or 'NB'

        # ── 3. FINALIZE DATA ──
        total_qty = sum(safe_float(d.get('qty', 0)) for d in merged_dists)
        # Bag count is now derived from the consolidated distributions for truth
        total_bags = sum(safe_int(d.get('bags', 0)) for d in merged_dists)
        
        # Track all barcode IDs associated with this document
        barcode_ids = existing_data.get('barcode_ids', [])
        if barcode_id not in barcode_ids:
            barcode_ids.append(barcode_id)

        # Search index calculation (Human-Readable only)
        search_locations = []
        for d in merged_dists:
            if safe_float(d.get('qty', 0)) > 0:
                wh = d.get('warehouse')
                zn = d.get('location')
                if wh: search_locations.append(wh)
                if zn: search_locations.append(zn)
                if wh and zn: search_locations.append(f"{wh} - {zn}")

        doc_data = {
            'doc_id': doc_ref.id, 
            'barcode_id': barcode_id, # Latest barcode as primary ref
            'barcode_ids': barcode_ids,
            'product_name': product_name,
            'product_code': product_code,
            'unit': unit,
            'total_qty': total_qty,
            'distributions': merged_dists,
            'location_ids': list(set([l for l in search_locations if l])),
            'min_stock_level': existing_data.get('min_stock_level', 0),
            'supplier_name': supplier_name or existing_data.get('supplier_name') or 'N/A',
            'batch_number': 'AGGREGATED' if is_merged else (batch_number or existing_data.get('batch_number')),
            'storage_type': storage_type,
            'number_of_bags': total_bags,
            'created_by': existing_data.get('created_by', user_name),
            'updated_by': user_name,
            'updated_at': int(time.time()),
            'is_merged': is_merged,
            'sync_status': 'pending', # 🆕 Track for Sheets sync
            'last_sync_error': None
        }
        
        # If existing doc has a barcode link, keep it (unless we want to overwrite with newest)
        if existing_data.get('barcode_link'):
            doc_data['barcode_link'] = existing_data['barcode_link']

        doc_ref.set(doc_data)
        
        # ── 4. SEARCH & DESTROY: Cleanup legacy docs ──
        if legacy_doc_ids:
            print(f"🧹 CLEANUP: Deleting {len(legacy_doc_ids)} legacy documents...")
            for old_id in legacy_doc_ids:
                try:
                    self.collection.document(old_id).delete()
                    print(f"🗑️ Deleted legacy doc: {old_id}")
                except Exception as e:
                    print(f"⚠️ Error deleting legacy doc {old_id}: {e}")
        
        # ── CROSS-INDEX UPDATE ──
        self._update_cross_index(doc_ref.id, merged_dists)
        return doc_ref.id

    def mark_as_synced(self, doc_id: str):
        """Mark a document as successfully synced to Google Sheets."""
        self.collection.document(doc_id).update({
            'sync_status': 'success',
            'last_synced_at': int(time.time()),
            'last_sync_error': None
        })

    def mark_as_sync_error(self, doc_id: str, error: str):
        """Mark a document as failed to sync with a specific error."""
        self.collection.document(doc_id).update({
            'sync_status': 'error',
            'last_sync_error': str(error),
            'last_sync_attempt': int(time.time())
        })

    def queue_pending_sync(self, sync_type: str, payload: Dict):
        """Adds a task to the persistent retry queue."""
        db.collection('pending_syncs').add({
            'type': sync_type,
            'payload': payload,
            'sync_status': 'pending',
            'retry_count': 0,
            'created_at': int(time.time()),
            'last_error': None
        })

    def get_pending_syncs(self, limit: int = 50) -> List[Dict]:
        """Find any documents that have not been successfully synced to Google Sheets."""
        query = self.collection.where(filter=FieldFilter('sync_status', '!=', 'success')).limit(limit)
        return [doc.to_dict() for doc in query.stream()]

    def get_existing_barcode(self, doc_id: str) -> str:
        """Returns existing barcode link from doc_id."""
        doc = self.collection.document(doc_id).get()
        if doc.exists:
            return doc.to_dict().get("barcode_link", "")
        return ""

    def get_existing_qr(self, doc_id: str, dist_id: str) -> str:
        """Returns existing QR link for a specific distribution."""
        doc = self.collection.document(doc_id).get()
        if doc.exists:
            for dist in doc.to_dict().get("distributions", []):
                if dist.get("dist_id") == dist_id:
                    return dist.get("qr_link", "")
        return ""

    def get_position(self, doc_id: str) -> Dict:
        """Fetches the current spatial map from doc_id."""
        doc = self.collection.document(doc_id).get()
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
        Robust search for a stock item by its specific shelf/zone position ID,
        main Barcode ID, or Product Code.
        Uses a four-tier discovery system.
        """
        if not dist_id: return {}
        
        # Normalize input for comparison across all tiers
        clean_id = normalize_id(dist_id)

        # ── TIER 1: CROSS-INDEX LOOKUP (Location Discovery) ──
        # We try both raw and clean IDs for maximum compatibility
        for lookup_id in [dist_id, clean_id]:
            idx_doc = self.index_collection.document(lookup_id).get()
            if idx_doc.exists:
                map_data = idx_doc.to_dict()
                doc_id = map_data.get('barcode_id')
                if doc_id:
                    doc = self.collection.document(doc_id).get()
                    if doc.exists:
                        data = doc.to_dict()
                        target_dist = next((d for d in data.get('distributions', []) if d.get('dist_id') == lookup_id), None)
                        return {"item": data, "target_distribution": target_dist}

        # ── TIER 2: MAIN BARCODE ARRAY SEARCH ──
        # Search the 'barcode_ids' array for the scanned ID
        for query_id in [dist_id, clean_id]:
            query = self.collection.where(filter=FieldFilter('barcode_ids', 'array_contains', query_id)).limit(1).get()
            if query:
                data = query[0].to_dict()
                target_dist = data.get('distributions', [None])[0]
                return {"item": data, "target_distribution": target_dist}

        # ── TIER 3: LEGACY DIRECT LOOKUP ──
        for direct_id in [dist_id, clean_id]:
            doc = self.collection.document(direct_id).get()
            if doc.exists:
                data = doc.to_dict()
                return {"item": data, "target_distribution": data.get('distributions', [None])[0]}
            
        # ── TIER 4: PRODUCT CODE LOOKUP (Batch-First Priority) ──
        # Prioritize discrete batches (is_merged=False) over global summaries
        for q_id in [dist_id, clean_id]:
            # 1. Try to find a real discrete batch first
            batch_query = self.collection.where(filter=FieldFilter('product_code', '==', q_id))\
                                         .where(filter=FieldFilter('is_merged', '==', False))\
                                         .limit(1).get()
            if batch_query:
                data = batch_query[0].to_dict()
                target_dist = data.get('distributions', [None])[0]
                return {"item": data, "target_distribution": target_dist}

            # 2. Fallback to any matching doc (including PROD- summaries)
            summary_query = self.collection.where(filter=FieldFilter('product_code', '==', q_id))\
                                           .limit(1).get()
            if summary_query:
                data = summary_query[0].to_dict()
                target_dist = data.get('distributions', [None])[0]
                return {"item": data, "target_distribution": target_dist}
            
        print(f"⚠️ DISPATCH FAILED: Position {dist_id} (Clean: {clean_id}) not found in any index.")
        return {}

    @staticmethod
    @firestore.transactional
    def check_and_alert_only(transaction, doc_ref):
        """Checks if an item is currently in low-stock and triggers alert if first time."""
        print(f"🔍 [ALERT_CHECK] Checking stock levels for {doc_ref.id}")
        """Checks if an item is currently in low-stock and triggers alert if first time."""
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists: return
        
        data = snapshot.to_dict()
        new_total = float(data.get('total_qty', 0))
        
        # 1. Get Product Custom Level
        min_level = float(data.get('min_stock_level') or 0)
        
        # 2. Get Global Default Fallback
        if min_level <= 0:
            try:
                g_doc = db.collection('alert_config').document('settings').get()
                if g_doc.exists:
                    min_level = float(g_doc.to_dict().get('default_min_stock', 0))
            except:
                min_level = 0
                
        # For 'check only' mode, we might want a 'last_alert_qty' or similar 
        # to avoid double emails if the sync API is called multiple times.
        if min_level > 0 and new_total <= min_level:
            # We check a 'last_notified_at' or just look at the timestamp of the last movement
            # In 'Sync' mode, we usually assume the frontend already did the movement.
            # We'll rely on the email service to avoid spam if possible or just log it.
            email_service.send_low_stock_alert(
                data['product_name'], 
                data['product_code'], 
                new_total, 
                min_level, 
                data['unit']
            )

    @staticmethod
    @firestore.transactional
    def deduct_from_location(transaction, doc_ref, loc_id: str, qty: float, bags_removed: float = 0):
        """Atomic deduction from a specific shelf/zone ID."""
        print(f"📉 [DEDUCTION] Attempting to remove {qty} from location {loc_id} of item {doc_ref.id}")
        """Atomic deduction from a specific shelf/zone ID."""
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise Exception("Stock position not found in Firestore")
        
        data = snapshot.to_dict()
        distributions = data.get('distributions', [])
        found = False
        
        new_distributions = []
        for dist in distributions:
            if dist.get('dist_id') == loc_id:
                curr_qty = float(dist.get('qty', 0))
                curr_bags = float(dist.get('bags', 0))
                
                if curr_qty < qty - 0.001:
                    qty = curr_qty
                
                dist['qty'] = float(curr_qty - qty)
                dist['bags'] = float(max(0, curr_bags - bags_removed))
                found = True
            new_distributions.append(dist)
            
        if not found:
            raise Exception(f"Position ID {loc_id} not found for this item.")
            
        old_total = float(data.get('total_qty', 0))
        old_bags = float(data.get('number_of_bags', 0))
        new_total = old_total - qty
        new_bags = max(0, old_bags - bags_removed)
        
        # Update searchable locations index
        search_locations = []
        for d in new_distributions:
            if float(d.get('qty', 0)) > 0:
                search_locations.append(d.get('warehouse'))
                search_locations.append(d.get('dist_id'))
                search_locations.append(f"{d.get('warehouse')} - {d.get('location')}")
        
        search_locations = []
        for d in distributions:
            wh = d.get('warehouse')
            zn = d.get('location')
            if wh: search_locations.append(wh)
            if zn: search_locations.append(zn)
            if wh and zn: search_locations.append(f"{wh} - {zn}")
            
        new_location_ids = list(set([l for l in search_locations if l]))
        
        # ── Check for Low Stock Alert ──
        min_level = float(data.get('min_stock_level') or 0)
        if min_level <= 0:
            try:
                g_doc = db.collection('alert_config').document('settings').get()
                if g_doc.exists:
                    min_level = float(g_doc.to_dict().get('default_min_stock', 0))
            except:
                min_level = 0
                
        if min_level > 0 and new_total <= min_level and old_total > min_level:
            email_service.send_low_stock_alert(
                data['product_name'], 
                data['product_code'], 
                new_total, 
                min_level, 
                data['unit']
            )

        transaction.update(doc_ref, {
            'distributions': new_distributions,
            'total_qty': float(max(0, new_total)),
            'number_of_bags': float(new_bags),
            'location_ids': new_location_ids,
            'updated_at': int(time.time())
        })
        return new_total

    def remove_stock_spatial(self, doc_id: str, loc_id: str, qty: float, user: dict = None, bags_removed: float = 0, skip_deduction: bool = False):
        """Wrapper to perform a safe atomic deduction or just an audit/alert check."""
        doc_ref = self.collection.document(doc_id)
        
        if skip_deduction:
            print(f"📖 [AUDIT_MODE] Stock {doc_id} removal (Audit Check Only)")
            transaction = db.transaction()
            self.check_and_alert_only(transaction, doc_ref)
            data = doc_ref.get().to_dict()
            new_total = data.get('total_qty', 0)
        else:
            print(f"⚡ [DEDUCTION_MODE] Stock {doc_id} removal from location {loc_id}")
            transaction = db.transaction()
            # We must pass the transaction object to the internal logic
            new_total = self.deduct_from_location(transaction, doc_ref, loc_id, qty, bags_removed=bags_removed)
        
        # ── RELIABLE HYBRID SYNC ──
        try:
            from app.services.sheets import sheets_service
            # Fetch doc again to get warehouse/location for movement record
            data = doc_ref.get().to_dict()
            dist = next((d for d in data.get('distributions', []) if d.get('dist_id') == loc_id), {})
            user_display = user.get('full_name', user['email']) if user else "System"
            
            # Prepare payload for potential retry
            sync_payload = {
                "barcode_id": data.get('barcode_id', doc_id),
                "product_code": data.get('product_code', ''),
                "batch_number": data.get('batch_number', ''),
                "qty": qty,
                "bags_removed": bags_removed,
                "warehouse": dist.get('warehouse', 'Main Floor'),
                "location": dist.get('location', 'General'),
                "user_display": user_display,
                "dist_id": loc_id,
                "warehouse_id": dist.get('warehouse_id', 'default')
            }

            try:
                sheets_service.record_dispatch(**sync_payload)
            except ValueError as ve:
                # Logic Error (e.g. Product NOT found in Sheets) - RE-RAISE for immediate user feedback
                print(f"🛑 [SYNC_LOGIC_ERROR] Permanent failure: {ve}")
                raise ve
            except Exception as te:
                # Technical Error (e.g. Network Timeout) - QUEUE for Background Reaper
                print(f"📡 [SYNC_TECH_ERROR] Backgrounding deduction due to network: {te}")
                self.queue_pending_sync("DISPATCH", sync_payload)
                # We return the new total but the API should ideally return a 202
                
        except ValueError:
            raise # Pass-through logic errors
        except Exception as e:
            print(f"⚠️ Unexpected Sync Handler Error: {e}")
            
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

    def set_min_stock_level(self, doc_id: str, min_level: float):
        """Admin override for threshold alerts."""
        self.collection.document(doc_id).update({
            'min_stock_level': min_level,
            'updated_at': int(time.time())
        })

    def update_barcode_link(self, doc_id: str, link: str):
        """Updates the stored link to the cloud-archived barcode image."""
        self.collection.document(doc_id).update({
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
        
        # 1. Calculate proportional bags to move if source has bag metadata
        source_dist = next((d for d in distributions if d.get('dist_id') == from_dist_id), None)
        bags_to_move = 0
        if source_dist:
            s_qty = float(source_dist.get('qty', 0))
            s_bags = float(source_dist.get('bags', 0))
            if s_qty > 0 and s_bags > 0:
                # Estimate bags based on proportion of qty being moved
                bags_to_move = int(round((qty / s_qty) * s_bags))
                # Safety: can't move more bags than exist
                bags_to_move = min(bags_to_move, int(s_bags))
        
        # 2. Execute movement within distributions
        for dist in distributions:
            if dist.get('dist_id') == from_dist_id:
                curr_qty = float(dist.get('qty', 0))
                curr_bags = float(dist.get('bags', 0))
                if curr_qty < qty - 0.001:
                    raise Exception(f"Insufficient stock in source position (Has {curr_qty}, Needs {qty}).")
                
                dist['qty'] = curr_qty - qty
                dist['bags'] = max(0, int(curr_bags) - bags_to_move)
                source_found = True
            
            # Check if destination exists (Warehouse matches AND Location matches)
            if dist.get('warehouse') == to_warehouse and dist.get('location') == to_location:
                dist['qty'] = float(dist.get('qty', 0)) + qty
                dist['bags'] = int(dist.get('bags', 0)) + bags_to_move
                dest_found = True
            
            new_distributions.append(dist)

        if not source_found:
            raise Exception(f"Source Position ID {from_dist_id} not found.")

        if not dest_found:
            # Create new distribution ID using standard 12-digit hash for consistency
            clean_code = normalize_id(data.get('product_code', 'UNKNOWN'))
            batch_ref = source_dist.get('batch_number', 'NB') if source_dist else 'NB'
            seed = f"{clean_code}-{to_warehouse}-{to_location}-{batch_ref}"
            new_dist_id = generate_12_digit_hash(seed)
            new_distributions.append({
                'warehouse': to_warehouse, 
                'location': to_location, 
                'qty': qty, 
                'bags': bags_to_move,
                'dist_id': new_dist_id,
                'batch_number': batch_ref
            })

        cleaned_distributions = [d for d in new_distributions if float(d.get('qty', 0)) > 0.001]
        
        # 3. Recalculate location search index (Single Clean Pass)
        search_locations = []
        for d in cleaned_distributions:
            wh = d.get('warehouse')
            zn = d.get('location')
            di = d.get('dist_id')
            if wh: search_locations.append(wh)
            if zn: search_locations.append(zn)
            if di: search_locations.append(di)
            if wh and zn: search_locations.append(f"{wh} - {zn}")
            
        new_location_ids = list(set([l for l in search_locations if l]))

        transaction.update(doc_ref, {
            'distributions': cleaned_distributions,
            'location_ids': new_location_ids,
            'updated_at': int(time.time())
        })
        # Update index for new positions
        self._update_cross_index(data.get('barcode_id'), cleaned_distributions)
        return True

    def transfer_stock(self, doc_id: str, from_loc_id: str, to_loc_id: str, to_loc_name: str, qty: float):
        doc_ref = self.collection.document(doc_id)
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
