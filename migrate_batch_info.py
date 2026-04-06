import time
from app.services.firebase import db

def migrate_stock_metadata():
    """
    Migrates existing inventory documents to move supplier and batch info 
    from the nested 'meta' object to the top level for better visibility.
    """
    print("🚀 Starting Migration: Batch and Supplier Information...")
    collection = db.collection('inventory_positions')
    docs = collection.stream()
    
    count = 0
    for doc in docs:
        data = doc.to_dict()
        meta = data.get('meta', {})
        updates = {}
        
        # Pull from meta if top-level is missing
        if 'supplier_name' not in data and 'supplier' in meta:
            updates['supplier_name'] = meta['supplier']
            
        if 'batch_number' not in data and 'batch' in meta:
            updates['batch_number'] = meta['batch']
            
        if updates:
            doc.reference.update(updates)
            print(f"✅ Updated {doc.id}: {updates}")
            count += 1
            
    print(f"🎉 Migration Complete. {count} documents updated.")

if __name__ == "__main__":
    migrate_stock_metadata()
