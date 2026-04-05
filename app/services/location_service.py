import time
from typing import List, Dict
from app.services.firebase import db

class LocationService:
    def __init__(self):
        self.collection = db.collection('master_locations')

    def get_all_locations(self) -> List[Dict]:
        """Fetch all predefined warehouse masters with nested zones."""
        docs = self.collection.order_by('name').stream()
        locations = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            if 'zones' not in data:
                data['zones'] = ["Main Floor"]
            locations.append(data)
        
        # Seed default warehouses if none exist
        if not locations:
            defaults = [
                {"name": "Main Warehouse", "zones": ["Main Floor", "Shed 1", "Shed 2"]},
                {"name": "North Facility", "zones": ["Rack A", "Rack B", "Office"]},
                {"name": "South Warehouse", "zones": ["Cold Storage", "Loading Dock"]},
            ]
            for d in defaults:
                self.create_location(d['name'], d['zones'])
            return self.get_all_locations()
            
        return locations

    def create_location(self, name: str, zones: List[str] = None) -> str:
        """Add a new warehouse master with optional initial zones."""
        if zones is None:
            zones = ["Main Floor"]

        # Check for duplication (by name)
        existing = self.collection.where('name', '==', name).limit(1).get()
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

    def get_location_by_id(self, loc_id: str) -> Dict:
        """Fetch details for a specific warehouse by ID."""
        doc = self.collection.document(loc_id).get()
        if doc.exists:
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return {}

location_service = LocationService()
