import time
from typing import List, Dict
from app.services.firebase import db

class LocationService:
    def __init__(self):
        self.collection = db.collection('all_locations')

    def get_all_locations(self) -> List[Dict]:
        """Fetch all predefined warehouse zones."""
        docs = self.collection.order_by('warehouse').order_by('name').stream()
        locations = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            locations.append(data)
        
        # Seed default locations if none exist
        if not locations:
            defaults = [
                {"warehouse": "Warehouse 1", "zone": "Main Floor"},
                {"warehouse": "Warehouse 1", "zone": "Shed 1"},
                {"warehouse": "Warehouse 2", "zone": "Shed 2"},
                {"warehouse": "Warehouse 2", "zone": "Row A"},
            ]
            for loc in defaults:
                self.create_location(loc['zone'], loc['warehouse'])
            return self.get_all_locations()
            
        return locations

    def create_location(self, name: str, warehouse: str = "Main Warehouse") -> str:
        """Add a new physical zone to the master list."""
        # Check for duplication (Combined check)
        existing = self.collection.where('name', '==', name).where('warehouse', '==', warehouse).limit(1).get()
        if existing:
            return existing[0].id
            
        doc_ref = self.collection.add({
            'name': name,
            'warehouse': warehouse,
            'created_at': int(time.time()),
            'status': 'active'
        })
        return doc_ref[1].id

    def get_location_by_id(self, loc_id: str) -> Dict:
        """Fetch details for a specific zone by ID."""
        doc = self.collection.document(loc_id).get()
        if doc.exists:
            return doc.to_dict()
        return {}

location_service = LocationService()
