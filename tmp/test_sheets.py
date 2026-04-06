
import os
import sys

# Mocking the environment
os.environ["GOOGLE_SHEETS_ID"] = "mock_id"

# Add the project root to sys.path
sys.path.append(r"d:\Stitch Stock\backend")

from app.services.sheets import sheets_service

def test():
    print(f"BASE_SCHEMA length: {len(sheets_service.BASE_SCHEMA)}")
    print(f"Header Map: { {n: i for i, n in enumerate(sheets_service.BASE_SCHEMA)} }")
    
    # Test _get_col_letter
    test_indices = [0, 1, 2, 25, 26, 27]
    for idx in test_indices:
        print(f"Index {idx} -> {sheets_service._get_col_letter(idx)}")

    # Verify key column positions
    h = {n: i for i, n in enumerate(sheets_service.BASE_SCHEMA)}
    print(f"Product Name: {h['Product Name']}")
    print(f"Barcode ID: {h['Barcode ID']}")

if __name__ == "__main__":
    test()
