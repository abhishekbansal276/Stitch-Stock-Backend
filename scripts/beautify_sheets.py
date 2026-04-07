import sys
import os
# Add the current directory to sys.path to allow importing 'app'
sys.path.append(os.getcwd())

# Ensure environment is loaded (handled by how we run it)
from app.services.sheets import sheets_service

if __name__ == "__main__":
    print("🚀 Starting Global Beautification of all Inventory Sheets...")
    try:
        sheets_service.beautify_all()
        print("\n✨ SUCCESS: All sheets (Register, Movements, Summary) have been colorized and formatted with the Elite design system.")
    except Exception as e:
        print(f"\n❌ ERROR during beautification: {e}")
        import traceback
        traceback.print_exc()
