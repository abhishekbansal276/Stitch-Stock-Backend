import os
import io
import base64
import json
import barcode
import requests
from barcode.writer import ImageWriter

class GoogleDriveService:
    def __init__(self):
        self.script_url = os.getenv("GOOGLE_SCRIPT_URL")
        self.folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    def generate_and_upload_barcode(self, barcode_id: str, product_name: str) -> str:
        """Generates a barcode image and uploads it to Google Drive via Apps Script Proxy."""
        if not self.script_url or not self.folder_id:
            print("WARNING: Apps Script URL or Folder ID not configured. Skipping barcode upload.")
            return ""

        try:
            # 1. Generate Barcode Image (Code128) in memory
            CODE128 = barcode.get_barcode_class('code128')
            qr = CODE128(barcode_id, writer=ImageWriter())
            
            buffer = io.BytesIO()
            qr.write(buffer)
            img_data = buffer.getvalue()
            
            # 2. Convert to Base64 for Apps Script payload
            base64_data = base64.b64encode(img_data).decode('utf-8')
            filename = f"{barcode_id}_{product_name[:20]}.png"

            # 3. POST to Google Apps Script Web App
            payload = {
                "folder_id": self.folder_id,
                "filename": filename,
                "base64_data": base64_data
            }

            response = requests.post(
                self.script_url,
                data=json.dumps(payload),
                headers={'Content-Type': 'application/json'},
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "success":
                    return result.get("webViewLink", "")
                else:
                    print(f"Apps Script Error: {result.get('message')}")
            else:
                print(f"Apps Script Connection Failed: {response.status_code}")

            return ""
        except Exception as e:
            print(f"Elite Drive Proxy Error: {e}")
            return ""

drive_service = GoogleDriveService()
