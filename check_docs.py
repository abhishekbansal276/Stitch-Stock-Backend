import os
from dotenv import load_dotenv

# Set up environment
os.environ["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__))
os.environ["SERVICE_ACCOUNT_FILE"] = "serviceAccountKey.json"
load_dotenv()

from app.services.firebase import db

def check_prod():
    collection = db.collection('inventory_positions')
    # Find the document with total_qty around 311.325 (or check everything)
    docs = collection.stream()
    for doc in docs:
        data = doc.to_dict()
        qty = data.get('total_qty', 0)
        if qty > 0:
            print(f"ID: {doc.id} | Name: {data.get('product_name')} | Qty: {qty} | Bags: {data.get('number_of_bags')}")

if __name__ == "__main__":
    check_prod()
