import os
import io
import base64
import json
import barcode
import requests
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

class GoogleDriveService:
    def __init__(self):
        self.script_url = os.getenv("GOOGLE_SCRIPT_URL")
        self.folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self._cache = {} # code_id -> webViewLink

    def generate_barcode(self, code_id: str, product_code: str, batch_number: str) -> str:
        """Generates a high-fidelity Code 128 Barcode and archives it in Google Drive."""
        # 0. IDEMPOTENCY GUARD
        if code_id in self._cache:
            return self._cache[code_id]

        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d_%H%M")
        # Standardized Filename: ProductCode_BatchNumber_DateTime.png
        filename = f"{product_code}_{batch_number}_{now_str}.png"

        try:
            # 1. CREATE BARCODE (Code 128)
            BARCODE_CLASS = barcode.get_barcode_class('code128')
            writer = ImageWriter()
            code_obj = BARCODE_CLASS(code_id, writer=writer)
            
            # 2. SAVE TO BUFFER
            buffer = io.BytesIO()
            options = {
                "module_height": 15.0,
                "module_width": 0.3,
                "quiet_zone": 6.5,
                "font_size": 10,
                "text_distance": 5.0,
                "write_text": True
            }
            code_obj.write(buffer, options=options)
            img_data = buffer.getvalue()

            # 3. CONVERT TO BASE64
            base64_data = base64.b64encode(img_data).decode('utf-8')

            # 4. DISPATCH TO CLOUD ARCHIVE
            payload = {"folder_id": self.folder_id, "filename": filename, "base64_data": base64_data}
            response = requests.post(
                self.script_url, 
                data=json.dumps(payload), 
                headers={'Content-Type': 'application/json'}, 
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "success":
                    link = result.get("webViewLink", "")
                    if link:
                        self._cache[code_id] = link
                    return link
            
            print(f"Barcode Cloud Error: {response.text}")
            return ""
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Barcode Generation Error: {e}")
            return ""

    def generate_qr_code(self, code_id: str, product_code: str, batch_number: str) -> str:
        """DEPRECATED: Use generate_barcode instead."""
        return self.generate_barcode(code_id, product_code, batch_number)

drive_service = GoogleDriveService()
