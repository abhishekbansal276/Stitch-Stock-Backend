import time
from typing import List, Dict
from app.services.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter

class LocationService:
    def __init__(self):
        self.collection = db.collection('master_locations')

    def get_all_locations(self) -> List[Dict]:
        """Fetch all predefined warehouse masters with nested zones."""
        print("LocationService: Fetching all warehouse locations...")
        try:
            docs = self.collection.order_by('name').stream()
            locations = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                if 'zones' not in data:
                    data['zones'] = ["Main Floor"]
                locations.append(data)
            
            return locations
        except Exception as e:
            print(f"LocationService ERROR: {e}")
            raise e

    def create_location(self, name: str, zones: List[str] = None) -> str:
        """Add a new warehouse master with optional initial zones."""
        if zones is None:
            zones = ["Main Floor"]

        # Check for duplication (by name)
        existing = self.collection.where(filter=FieldFilter('name', '==', name)).limit(1).get()
        if existing:
            return existing[0].id
            
        doc_ref = self.collection.add({
            'name': name,
            'zones': zones,
            'created_at': int(time.time()),
            'status': 'active'
        })
        return doc_ref[1].id

    def add_zone_to_warehouse(self, warehouse_id: str, zone_name: str):
        """Append a specific zone to a warehouse's collection."""
        from google.cloud import firestore
        self.collection.document(warehouse_id).update({
            'zones': firestore.ArrayUnion([zone_name])
        })

    def rename_zone(self, warehouse_id: str, old_name: str, new_name: str):
        """Atomically renames a zone within a warehouse."""
        from google.cloud import firestore
        doc_ref = self.collection.document(warehouse_id)
        # Update both simultaneously: Remove old, Add new
        doc_ref.update({
            'zones': firestore.ArrayRemove([old_name])
        })
        doc_ref.update({
            'zones': firestore.ArrayUnion([new_name])
        })

    def get_location_by_id(self, loc_id: str) -> Dict:
        """Fetch details for a specific warehouse by ID."""
        doc = self.collection.document(loc_id).get()
        if doc.exists:
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return {}

location_service = LocationService()
