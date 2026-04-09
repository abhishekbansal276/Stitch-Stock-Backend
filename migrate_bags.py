import sys
import os
import time

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from dotenv import load_dotenv

# Path to the .env file in the backend directory
env_path = os.path.join(os.getcwd(), 'backend', '.env')
load_dotenv(dotenv_path=env_path)

from app.services.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter

def migrate():
    print("🚀 Starting Bag Metric Consolidation Migration...")
    
    collection = db.collection('inventory_positions')
    docs = list(collection.stream())
    
    updated_count = 0
    total_docs = len(docs)
    
    for i, doc in enumerate(docs):
        data = doc.to_dict()
        doc_id = doc.id
        
        unit = str(data.get('unit', '')).lower()
        storage_type = str(data.get('storage_type', '')).lower()
        
        is_bag_item = 'bag' in unit or 'bag' in storage_type
        
        if is_bag_item:
            print(f"[{i+1}/{total_docs}] Processing Bag-Item: {doc_id} ('{data.get('product_name')}')")
            
            distributions = data.get('distributions', [])
            modified = False
            
            # 1. Clean up distributions
            for dist in distributions:
                if 'bags' in dist:
                    del dist['bags']
                    modified = True
            
            # 2. Sync top-level totals
            total_qty = data.get('total_qty', 0)
            if data.get('number_of_bags') != total_qty:
                data['number_of_bags'] = total_qty
                modified = True
                
            if modified:
                collection.document(doc_id).update({
                    'distributions': distributions,
                    'number_of_bags': data['number_of_bags'],
                    'updated_at': int(time.time()),
                    'updated_by': 'Bag Migration'
                })
                updated_count += 1
                print(f"   ✅ Cleaned up distributions and synced bag-total.")
        else:
            # For non-bag items, ensure they HAVE a bag field if missing (optional but good for consistency)
            pass

    print(f"\n✨ Migration Complete!")
    print(f"📦 Total Docs Scanned: {total_docs}")
    print(f"🛠️ Documents Updated: {updated_count}")

if __name__ == "__main__":
    migrate()