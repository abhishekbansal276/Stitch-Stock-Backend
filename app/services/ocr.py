import os
import json
import re
import time
import logging
from typing import Dict, List, Optional
from google import genai
from google.genai import types
from groq import Groq
from app.services.local_ocr import local_ocr_service

# ── LOGGING CONFIGURATION ───────────────────────────────────────────────────
# Ensuring we have visible logs for quota issues
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OCRService")

# ──────────────────────────────────────────────────────────────────────────────

class OCRService:
    def __init__(self):
        # 1. Gemini Configuration
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        if self.gemini_key:
            self.gemini_client = genai.Client(api_key=self.gemini_key)
            self.preferred_gemini = [
                "gemini-2.0-flash",
                "gemini-1.5-flash",
                "gemini-1.5-pro",
            ]
            self.gemini_model = self._pick_gemini_model()
            logger.info(f"OCRService: Gemini initialized with model: {self.gemini_model}")
        else:
            logger.warning("OCRService: GEMINI_API_KEY not found. Gemini features disabled.")
            self.gemini_client = None
            self.gemini_model = None

        # 2. Groq Configuration (High-performance Text LLM Fallback)
        self.groq_key = os.getenv("GROQ_API_KEY")
        if self.groq_key:
            try:
                self.groq_client = Groq(api_key=self.groq_key)
                self.groq_model = "llama-3.3-70b-versatile"
                logger.info(f"OCRService: Groq initialized with model: {self.groq_model}")
            except Exception as e:
                logger.error(f"OCRService: Failed to initialize Groq client: {e}")
                self.groq_client = None
        else:
            logger.info("OCRService: GROQ_API_KEY not found. Groq fallback disabled.")
            self.groq_client = None

    def _pick_gemini_model(self) -> str:
        try:
            available_models = self.gemini_client.models.list()
            available_names = {m.name.replace("models/", "") for m in available_models}
            for p in self.preferred_gemini:
                if p in available_names:
                    return p
        except Exception as e:
            logger.warning(f"OCRService: Could not list Gemini models: {e}. Defaulting to gemini-2.0-flash")
        return "gemini-2.0-flash"

    # ─────────────────────────────────────────────────────────────────────────

    def extract_from_file(self, content: bytes, filename: str,
                          existing_headers: List[str] = None) -> Dict:
        """
        Main extraction pipeline with multi-stage fallback:
        1. Gemini Vision (Directly from file)
        2. Local OCR + Gemini Text (If Vision fails/quota hit)
        3. Local OCR + Groq Text (If Gemini Text fails/quota hit)
        """
        ext = filename.rsplit(".", 1)[-1].lower()
        mime_map = {"pdf": "application/pdf", "png": "image/png",
                    "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/jpeg")
        
        logger.info(f"OCRService: Processing '{filename}' ({mime_type})")

        # --- STAGE 1: GEMINI VISION ---
        if self.gemini_client:
            try:
                return self._run_gemini_vision(content, mime_type)
            except Exception as e:
                logger.warning(f"OCRService: Stage 1 (Vision) failed: {e}. Falling back to Stage 2...")
        
        # --- STAGE 2: LOCAL OCR + GEMINI/GROQ TEXT ---
        logger.info("OCRService: 🛠️ STAGE 2: Running Local OCR (Tesseract)...")
        extracted_text = local_ocr_service.extract_text(content)
        
        if not extracted_text:
            raise Exception("Total extraction failure: Vision stage failed and Local OCR returned no text.")

        # Try Gemini Text first
        if self.gemini_client:
            try:
                return self._run_gemini_text(extracted_text)
            except Exception as e:
                logger.warning(f"OCRService: Stage 2 (Gemini Text) failed: {e}")
                if self.groq_client:
                    logger.info("OCRService: 🔄 Falling back to Groq for text structuring...")
                else:
                    logger.error("OCRService: Gemini exhausted and no Groq fallback configured.")
                    raise Exception(f"Extraction failed: Gemini exhausted and no Groq fallback configured. Error: {e}")

        # Final Fallback to Groq Text
        if self.groq_client:
            try:
                return self._run_groq_text(extracted_text)
            except Exception as e:
                logger.error(f"OCRService: Stage 3 (Groq) failed: {e}")
                raise Exception(f"Total extraction failure across all providers: {e}")

        raise Exception("OCRService: Extraction failed. No working AI clients available.")

    # ── INDIVIDUAL STAGE RUNNERS ──────────────────────────────────────────────

    def _run_gemini_vision(self, content: bytes, mime_type: str) -> Dict:
        max_retries = 2
        for attempt in range(max_retries):
            try:
                logger.info(f"OCRService: Requesting Gemini Vision [{self.gemini_model}] (Attempt {attempt+1})")
                response = self.gemini_client.models.generate_content(
                    model=self.gemini_model,
                    contents=[
                        types.Part.from_bytes(data=content, mime_type=mime_type),
                        types.Part.from_text(text=EXTRACTION_PROMPT),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0,
                        max_output_tokens=4096,
                    ),
                )
                return self._clean_and_parse(response.text.strip())
            except Exception as e:
                wait_time = self._handle_quota_error(e, attempt, max_retries)
                if wait_time:
                    time.sleep(wait_time)
                    continue
                raise e

    def _run_gemini_text(self, text: str) -> Dict:
        max_retries = 1
        prompt = f"Convert the following OCR text into structured JSON:\n\n{text}\n\nREMAINDER: {EXTRACTION_PROMPT}"
        for attempt in range(max_retries):
            try:
                logger.info(f"OCRService: Requesting Gemini Text [{self.gemini_model}]")
                response = self.gemini_client.models.generate_content(
                    model=self.gemini_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                return self._clean_and_parse(response.text.strip())
            except Exception as e:
                wait_time = self._handle_quota_error(e, attempt, max_retries)
                if wait_time:
                    time.sleep(wait_time)
                    continue
                raise e

    def _run_groq_text(self, text: str) -> Dict:
        logger.info(f"OCRService: Requesting Groq Structuring [{self.groq_model}]")
        prompt = f"Convert the following OCR text into structured JSON as per requirements:\n\n{text}\n\nINSTRUCTION: {EXTRACTION_PROMPT}"
        try:
            chat_completion = self.groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "You are an AI that converts OCR text to exact JSON. Return ONLY the JSON object."},
                    {"role": "user", "content": prompt}
                ],
                model=self.groq_model,
                temperature=0,
                response_format={"type": "json_object"}
            )
            raw = chat_completion.choices[0].message.content
            return self._clean_and_parse(raw)
        except Exception as e:
            logger.error(f"OCRService: Groq API Error: {e}")
            raise e

    def _handle_quota_error(self, e: Exception, attempt: int, max_retries: int) -> Optional[int]:
        err_str = str(e).upper()
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            if attempt < max_retries - 1:
                # Standard Gemini wait time for 429
                wait = 16 
                logger.warning(f"OCRService: ⚠️ Quota Exhausted. Waiting {wait}s before retry...")
                return wait
            else:
                logger.error(f"OCRService: 🛑 Quota limit reached for {self.gemini_model}.")
                # Model Rotation: Try to switch model for future calls
                self._rotate_gemini_model()
        return None

    def _rotate_gemini_model(self):
        """Switches current model to the next on one in preferred list."""
        try:
            curr_idx = self.preferred_gemini.index(self.gemini_model)
            next_idx = (curr_idx + 1) % len(self.preferred_gemini)
            self.gemini_model = self.preferred_gemini[next_idx]
            logger.info(f"OCRService: 🔄 Rotating Gemini model to: {self.gemini_model}")
        except:
            pass

    # ── CLEAN & PARSE ─────────────────────────────────────────────────────────

    def _clean_and_parse(self, raw: str) -> Dict:
        try:
            raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"```$", "", raw).strip()
            s, e = raw.find("{"), raw.rfind("}")
            if s != -1 and e != -1:
                raw = raw[s : e + 1]

            data = json.loads(raw)
            if "items" not in data:
                data["items"] = []
            data["items"] = self._consolidate(data.get("items", []))
            logger.info(f"OCRService: Extraction complete. Found {len(data['items'])} line items.")
            return data
        except Exception as e:
            logger.error(f"OCRService: JSON Parsing Error: {e}\nRaw output: {raw[:200]}...")
            raise Exception(f"Failed to parse AI output into JSON: {str(e)}")

    def _consolidate(self, items: List[Dict]) -> List[Dict]:
        merged: Dict[str, Dict] = {}
        for item in items:
            key = str(item.get("Product Code") or item.get("Product Name") or "UNKNOWN").strip()
            if key in merged:
                base = merged[key]
                base["Quantity Received"] = self._sum_str(
                    base.get("Quantity Received"), item.get("Quantity Received"))
                base["Number of Bags"] = self._sum_str(
                    base.get("Number of Bags"), item.get("Number of Bags"))
                base["Total Amount"] = (
                    self._to_float(base.get("Total Amount"))
                    + self._to_float(item.get("Total Amount")))
                base["Final Amount"] = (
                    self._to_float(base.get("Final Amount"))
                    + self._to_float(item.get("Final Amount")))
                if not base.get("Unit") or base.get("Unit") == "PCS":
                    base["Unit"] = item.get("Unit") or base.get("Unit")
                if item.get("Batch Number") and item["Batch Number"] not in str(base.get("Batch Number", "")):
                    base["Batch Number"] = f"{base.get('Batch Number', '')}, {item['Batch Number']}".strip(", ")
            else:
                merged[key] = dict(item)
        return list(merged.values())

    def _to_float(self, val) -> float:
        if isinstance(val, (int, float)):
            return float(val)
        try:
            nums = re.findall(r"[-+]?\d*\.?\d+", str(val))
            return float(nums[0]) if nums else 0.0
        except Exception:
            return 0.0

    def _sum_str(self, a, b) -> str:
        return str(self._to_float(a) + self._to_float(b))

# ── EXTRACTION PROMPT ─────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """
You are an Inventory Auditor AI. Extract invoice data into JSON format. Match these fields:
header: {Date, Invoice Number, Supplier Name, Supplier GST, Vehicle Number, Transporter Name, Transport / Freight}
items: [{Product Code, Product Name, Model Name, Batch Number, Quantity Received, Unit, Number of Bags, Rate per Unit, Total Amount, Taxes (IGST/CGST/SGST), Final Amount, Remarks}]

Rules:
1. Return ONLY JSON.
2. If fields missing, use null.
3. Quantities as numbers/strings.
4. Normalize dates to YYYY-MM-DD.
5. All amounts numeric.
"""

ocr_service = OCRService()
