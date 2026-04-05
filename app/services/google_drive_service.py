import os
import io
import base64
import json
import barcode
from barcode.writer import ImageWriter
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

class GoogleDriveService:
    def __init__(self):
        self.scopes = ['https://www.googleapis.com/auth/drive.file']
        self.folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.service = self._initialize_service()

    def _initialize_service(self):
        b64_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if b64_key:
            try:
                decoded_key = base64.b64decode(b64_key).decode('utf-8')
                creds = service_account.Credentials.from_service_account_info(
                    json.loads(decoded_key), scopes=self.scopes
                )
                return build('drive', 'v3', credentials=creds)
            except: pass

        service_account_file = os.getenv("SERVICE_ACCOUNT_FILE", "serviceAccountKey.json")
        if os.path.exists(service_account_file):
            creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=self.scopes)
            return build('drive', 'v3', credentials=creds)
        return None

    def generate_and_upload_barcode(self, barcode_id: str, product_name: str) -> str:
        """Generates a barcode image and uploads it to Google Drive."""
        if not self.service or not self.folder_id:
            print("WARNING: Drive service not configured. Skipping barcode upload.")
            return ""

        try:
            # 1. Generate Barcode Image (Code128) in memory
            CODE128 = barcode.get_barcode_class('code128')
            qr = CODE128(barcode_id, writer=ImageWriter())
            
            buffer = io.BytesIO()
            qr.write(buffer)
            buffer.seek(0)

            # 2. Upload to Google Drive
            file_metadata = {
                'name': f"{barcode_id}_{product_name[:15]}.png",
                'parents': [self.folder_id]
            }
            media = MediaIoBaseUpload(buffer, mimetype='image/png', resumable=True)
            
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()

            # 3. Set Permissions (Make it viewable by anyone with the link)
            try:
                self.service.permissions().create(
                    fileId=file.get('id'),
                    body={'role': 'reader', 'type': 'anyone'}
                ).execute()
            except: pass

            return file.get('webViewLink', '')
        except Exception as e:
            print(f"Drive Upload Error: {e}")
            return ""

drive_service = GoogleDriveService()
