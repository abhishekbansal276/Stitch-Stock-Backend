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

    def generate_qr_code(self, code_id: str, label: str) -> str:
        """Generates a high-fidelity QR code and archives it in Google Drive."""
        if not self.script_url or not self.folder_id:
            return ""

        try:
            import qrcode
            from PIL import Image, ImageDraw, ImageFont

            # 1. CREATE ELITE QR (Error Correction 'H' for warehouse durability)
            qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
            qr.add_data(code_id)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert('RGB')

            # 2. Add Label (Product Name) for human-readability on shelf
            # Note: We keep it simple to ensure it remains valid in memory
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            img_data = buffer.getvalue()

            # 3. CONVERT TO BASE64
            base64_data = base64.b64encode(img_data).decode('utf-8')
            filename = f"QR_{code_id}_{label[:15]}.png"

            # 4. DISPATCH TO CLOUD ARCHIVE
            payload = {"folder_id": self.folder_id, "filename": filename, "base64_data": base64_data}
            response = requests.post(self.script_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "success":
                    return result.get("webViewLink", "")
            return ""
        except Exception as e:
            print(f"QR Generation Error: {e}")
            return ""

drive_service = GoogleDriveService()
