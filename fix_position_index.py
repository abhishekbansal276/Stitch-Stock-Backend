import os
import sys
from dotenv import load_dotenv

# Add the current directory to sys.path so we can import 'app'
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.firebase import db

def migrate_position_indices():
    print("🔍 Starting Firestore Position Index Migration...")
    collection_ref = db.collection('inventory_positions')
    docs = collection_ref.stream()
    
    updated_count = 0
    skipped_count = 0
    
    for doc in docs:
        data = doc.to_dict()
        doc_id = doc.id
        distributions = data.get('distributions', [])
        current_location_ids = set(data.get('location_ids', []))
        
        # Collect all dist_ids from distributions
        dist_ids = [d.get('dist_id') for d in distributions if d.get('dist_id')]
        
        # Check if we need to update
        needs_update = False
        new_location_ids = current_location_ids.copy()
        
        for dist_id in dist_ids:
            if dist_id not in current_location_ids:
                new_location_ids.add(dist_id)
                needs_update = True
        
        if needs_update:
            print(f"✅ Updating {doc_id}: Adding {len(new_location_ids) - len(current_location_ids)} new IDs to index.")
            collection_ref.document(doc_id).update({
                'location_ids': list(new_location_ids)
            })
            updated_count += 1
        else:
            skipped_count += 1
            
    print(f"\n✨ Migration Complete!")
    print(f"📊 Updated: {updated_count}")
    print(f"📊 Skipped: {skipped_count}")

if __name__ == "__main__":
    load_dotenv()
    migrate_position_indices()
