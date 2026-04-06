import os
import pytesseract
from PIL import Image
import io
import shutil

class LocalOCRService:
    def __init__(self):
        self.is_ready = False
        self._initialize_on_demand()

    def _initialize_on_demand(self):
        """Check if Tesseract is installed and configured."""
        if not self.is_ready:
            # 1. Check if Tesseract is in PATH (Universal - standard for Linux/Docker)
            if shutil.which("tesseract"):
                self.is_ready = True
                print("LocalOCRService: Tesseract detected in PATH.")
                return

            # 2. Check common Windows path (Local dev only)
            windows_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
            if os.path.exists(windows_path):
                pytesseract.pytesseract.tesseract_cmd = windows_path
                self.is_ready = True
                print("LocalOCRService: Tesseract detected at Windows path.")
                return

            print("WARNING: LocalOCRService: Tesseract binary not found. Local OCR fallback is disabled.")

    def extract_text(self, image_bytes: bytes) -> str:
        """Processes an image locally using Tesseract and returns all detected text."""
        self._initialize_on_demand()
        if not self.is_ready:
            return ""

        try:
            image = Image.open(io.BytesIO(image_bytes))
            # Tesseract image_to_string handles the OCR
            text = pytesseract.image_to_string(image)
            return text.strip()
        except Exception as e:
            print(f"LocalOCRService: Tesseract Extraction error: {e}")
            return ""

local_ocr_service = LocalOCRService()
