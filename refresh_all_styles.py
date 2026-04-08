import sys
import os
from dotenv import load_dotenv

# Add the current directory to path so we can import app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))

from app.services.sheets import SheetsService

def refresh_all():
    print("🚀 Starting Global Style Refresh...")
    load_dotenv()
    
    sheets = SheetsService()
    target_sheets = ["Stock Register", "Stock Movements", "Stock Summary"]
    
    for title in target_sheets:
        print(f"🔄 Refreshing styles for '{title}'...")
        try:
            sheets.refresh_styles(title)
            print(f"✅ Success: '{title}' is now pure.")
        except Exception as e:
            print(f"❌ Failed: '{title}' — {e}")
            
    print("\n✨ All styles have been synchronized to the new Three-Tier system!")

if __name__ == "__main__":
    refresh_all()
