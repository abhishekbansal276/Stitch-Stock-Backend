import os
import easyocr
import numpy as np
from PIL import Image
import io

class LocalOCRService:
    def __init__(self):
        self.reader = None
        self._initialize_on_demand()

    def _initialize_on_demand(self):
        """Lazy load the reader to save memory during startup."""
        if self.reader is None:
            try:
                print("LocalOCRService: Initializing EasyOCR Reader (English)...")
                # downloads models on first use (~500MB)
                self.reader = easyocr.Reader(['en'], gpu=False) 
            except Exception as e:
                print(f"LocalOCRService: Initialization failed: {e}")

    def extract_text(self, image_bytes: bytes) -> str:
        """Processes an image locally and returns all detected text as a single string."""
        self._initialize_on_demand()
        if not self.reader:
            return ""

        try:
            # EasyOCR expects an image file path, a PIL image, or a numpy array
            image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
            # Convert PIL to numpy array
            image_np = np.array(image)
            
            # detail=0 returns only the text list
            results = self.reader.readtext(image_np, detail=0)
            
            # Join with spacing to help LLM understand layout
            return "\n".join(results)
        except Exception as e:
            print(f"LocalOCRService: Extraction error: {e}")
            return ""

local_ocr_service = LocalOCRService()
