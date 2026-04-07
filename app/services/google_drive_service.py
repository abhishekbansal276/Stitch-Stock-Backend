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

    def generate_barcode(self, code_id: str, label: str) -> str:
        """Generates a high-fidelity Code 128 Barcode and archives it in Google Drive."""
        # 0. IDEMPOTENCY GUARD: Skip if already generated in this process
        if code_id in self._cache:
            print(f"♻️ [BARCODE_GEN] Returning cached link for {code_id}")
            return self._cache[code_id]

        print(f"📁 [BARCODE_GEN] Request for {code_id} ({label})")
        # traceback.print_stack(limit=5) # Enable if needed for deep trace
 
        try:
            # 1. CREATE BARCODE (Code 128)
            # Standard options for high contrast and readability
            BARCODE_CLASS = barcode.get_barcode_class('code128')
            writer = ImageWriter()
            
            # Create barcode object
            code_obj = BARCODE_CLASS(code_id, writer=writer)
            
            # 2. SAVE TO BUFFER WITH OPTIONS
            buffer = io.BytesIO()
            # We add a bit of padding and ensure the white background is clean
            options = {
                "module_height": 15.0,
                "module_width": 0.3,
                "quiet_zone": 6.5,
                "font_size": 10,
                "text_distance": 5.0,
                "write_text": True  # Human readable text at bottom
            }
            code_obj.write(buffer, options=options)
            img_data = buffer.getvalue()

            # 3. CONVERT TO BASE64
            base64_data = base64.b64encode(img_data).decode('utf-8')
            filename = f"BARCODE_{code_id}_{label[:15]}.png"

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

    def generate_qr_code(self, code_id: str, label: str) -> str:
        """DEPRECATED: Use generate_barcode instead."""
        return self.generate_barcode(code_id, label)

drive_service = GoogleDriveService()
