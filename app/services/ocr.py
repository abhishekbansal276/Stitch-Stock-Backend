import os
import json
import re
import time
import logging
import base64
import io
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from groq import Groq
from PIL import Image
# from app.services.firebase import db # USER DIRECTIVE: Stopped using Firebase for OCR cache

# ── OPTIMIZATION CONFIGURATION ──────────────────────────────────────────────
MAX_IMAGE_DIMENSION = 1200
JPEG_QUALITY = 80

# ── LOGGING CONFIGURATION ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OCRService")

# ── DATA MODELS (STRUCTURED OUTPUT) ──────────────────────────────────────────

class TaxItem(BaseModel):
    label: str = Field(description="Tax type, e.g. CGST, SGST, IGST")
    amount: float = Field(description="Tax amount")

class InvoiceHeader(BaseModel):
    date: str = Field(alias="Date", description="Invoice date in YYYY-MM-DD format")
    invoice_number: str = Field(alias="Invoice Number", description="Invoice/Bill/Challan number")
    supplier_name: str = Field(alias="Supplier Name", description="Name of the supplier/vendor")
    supplier_gst: str = Field(alias="Supplier GST", description="GSTIN of the supplier")
    vehicle_number: Optional[str] = Field(alias="Vehicle Number", description="Vehicle registration number if available")
    transporter_name: Optional[str] = Field(alias="Transporter Name", description="Name of the transport company if available")
    taxable_amount: float = Field(alias="Taxable Amount", description="Total taxable value before taxes")
    taxes: List[TaxItem] = Field(alias="Taxes", description="List of individual tax components")
    transport_freight: float = Field(alias="Transport / Freight", description="Total freight/shipping charges")
    grand_total: float = Field(alias="Grand Total", description="Final invoice total including all taxes and charges")

class InvoiceItem(BaseModel):
    product_code: str = Field(alias="Product Code", description="HSN, SKU, or Model number")
    product_name: str = Field(alias="Product Name", description="Full description of the item")
    batch_number: Optional[str] = Field(alias="Batch Number", description="Batch or lot number if available")
    quantity_received: float = Field(alias="Quantity Received", description="Total quantity being received")
    unit: str = Field(alias="Unit", description="Unit of measure (e.g. PCS, MT, KGS)")
    number_of_bags: int = Field(alias="Number of Bags", description="Count of bags or packages")
    rate_per_unit: float = Field(alias="Rate per Unit", description="Price per single unit")
    total_amount: float = Field(alias="Total Amount", description="Line item total (Qty * Rate)")

class InvoiceExtraction(BaseModel):
    header: InvoiceHeader
    items: List[InvoiceItem]

# ──────────────────────────────────────────────────────────────────────────────

class OCRService:
    def __init__(self):
        # 1. Gemini Configuration
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        if self.gemini_key:
            # --- SECURITY-SAFE LOGGING FOR KEY VERIFICATION ---
            key_id = f"{self.gemini_key[:4]}...{self.gemini_key[-4:]}" if len(self.gemini_key) > 8 else "***"
            logger.info(f"OCRService: Initializing Gemini with key: {key_id}")
            
            self.gemini_client = genai.Client(api_key=self.gemini_key)
            self.preferred_gemini = [
                "gemini-2.5-flash",
                "gemini-3.1-flash-live",
            ]
            self.gemini_model = self._pick_gemini_model()
            logger.info(f"OCRService: Gemini initialized with model: {self.gemini_model}")
            print(f"✅ GEMINI ACTIVE: Using Model '{self.gemini_model}'")
        else:
            logger.warning("OCRService: GEMINI_API_KEY not found. Gemini features disabled.")
            self.gemini_client = None
            self.gemini_model = None

        # 2. Groq Configuration (High-performance Vision Fallback)
        self.groq_key = os.getenv("GROQ_API_KEY")
        if self.groq_key:
            try:
                self.groq_client = Groq(api_key=self.groq_key)
                self.preferred_groq = [
                    "llama-3.2-11b-vision-preview",
                    "llama-3.2-90b-vision-preview",
                    "meta-llama/llama-4-scout-17b-16e-instruct",
                ]
                self.groq_model = self._pick_groq_model()
                logger.info(f"OCRService: Groq initialized with model: {self.groq_model}")
            except Exception as e:
                logger.error(f"OCRService: Failed to initialize Groq client: {e}")
                self.groq_client = None
        else:
            logger.info("OCRService: GROQ_API_KEY not found. Groq fallback disabled.")
            self.groq_client = None
            self.groq_model = None

    def _pick_gemini_model(self) -> str:
        try:
            available_models = self.gemini_client.models.list()
            available_names = {m.name.replace("models/", "") for m in available_models}
            for p in self.preferred_gemini:
                if p in available_names:
                    return p
        except Exception as e:
            logger.warning(f"OCRService: Could not list Gemini models: {e}. Defaulting to gemini-2.0-flash")
        return self.preferred_gemini[0]

    def _pick_groq_model(self) -> str:
        try:
            available_models = self.groq_client.models.list()
            available_ids = {m.id for m in available_models.data}
            for p in self.preferred_groq:
                if p in available_ids:
                    return p
        except Exception as e:
            logger.warning(f"OCRService: Could not list Groq models: {e}. Defaulting to llama-3.2-11b-vision-preview")
        return self.preferred_groq[0]

    # ─────────────────────────────────────────────────────────────────────────

    def extract_from_file(self, content: bytes, filename: str,
                          existing_headers: List[str] = None) -> Dict:
        """
        Dual-Vision extraction pipeline:
        1. Gemini Vision (Stage 1)
        2. Groq Vision (Stage 2 - Direct image fallback if Gemini exceeds quota)
        """
        ext = filename.rsplit(".", 1)[-1].lower()
        # USER DIRECTIVE: Remove PDF support. Only allow images.
        mime_map = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp"
        }
        
        if ext not in mime_map:
            logger.error(f"OCRService: Rejected unsupported file type '{ext}'")
            raise Exception(f"Unsupported file type '{ext}'. Only images (PNG, JPG, WEBP) are supported.")
            
        mime_type = mime_map[ext]
        logger.info(f"OCRService: Processing '{filename}' ({mime_type})")

        # --- LOCAL CACHING LAYER (3-HOUR SLIDING EXPIRY) ---
        file_hash = hashlib.md5(content).hexdigest()
        cache_dir = os.path.join(os.getcwd(), ".ocr_cache")
        cache_path = os.path.join(cache_dir, f"{file_hash}.json")
        
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir, exist_ok=True)

        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                
                expires_at = cache_data.get("expires_at")
                # Check if still valid
                if expires_at and datetime.fromisoformat(expires_at) > datetime.now():
                    logger.info(f"🚀 [LOCAL CACHE HIT] Reusing data for {filename} (Hash: {file_hash})")
                    print(f"✨ LOCAL CACHE HIT: Resetting 3h timeline for {file_hash}")
                    
                    # RESET CACHE TIMELINE (Next 3 hours)
                    new_expiry = (datetime.now() + timedelta(hours=3)).isoformat()
                    cache_data["expires_at"] = new_expiry
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache_data, f)
                    
                    return cache_data["data"]
                else:
                    logger.info(f"🗑️ [CACHE EXPIRED] Deleting stale cache for {filename}")
                    os.remove(cache_path)
        except Exception as cache_err:
            logger.warning(f"OCRService: Local cache check failed: {cache_err}")

        # --- OPTIMIZATION STEP ---
        try:
            content, mime_type = self._optimize_image(content, mime_type)
        except Exception as e:
            logger.warning(f"OCRService: Image optimization failed, proceeding with original: {e}")

        # --- STAGE 1: GEMINI VISION ---
        if self.gemini_client:
            try:
                res = self._run_gemini_vision(content, mime_type)
                final_res = self._post_process_results(res)
                # Store in cache before returning
                self._store_in_cache(file_hash, final_res)
                return final_res
            except Exception as e:
                logger.warning(f"OCRService: Stage 1 (Gemini Vision) failed: {e}")
                if self.groq_client:
                    logger.info("OCRService: 🔄 Falling back to Stage 2 (Groq Vision)...")
                else:
                    raise Exception(f"Extraction failed: Gemini exhausted and no Groq fallback configured. Error: {e}")

        # --- STAGE 2: GROQ VISION FALLBACK ---
        if self.groq_client:
            try:
                res = self._run_groq_vision(content, mime_type)
                final_res = self._post_process_results(res)
                # Store in cache before returning
                self._store_in_cache(file_hash, final_res)
                return final_res
            except Exception as e:
                logger.error(f"OCRService: Stage 2 (Groq Vision) failed: {e}")
                # If we get a "model_decommissioned" or 400 error, try rotating and retrying once
                if "model_decommissioned" in str(e).lower() or "400" in str(e):
                    logger.warning("OCRService: Groq model decommissioned or invalid. Attempting rotation...")
                    self._rotate_groq_model()
                    res = self._run_groq_vision(content, mime_type)
                    final_res = self._post_process_results(res)
                    self._store_in_cache(file_hash, final_res)
                    return final_res
                raise Exception(f"Total extraction failure across all vision providers: {e}")

        raise Exception("OCRService: Extraction failed. No working Vision clients available.")

    def _store_in_cache(self, file_hash: str, data: Dict):
        """Helper to persist results locally with a 3-hour TTL."""
        try:
            cache_dir = os.path.join(os.getcwd(), ".ocr_cache")
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir, exist_ok=True)
                
            cache_path = os.path.join(cache_dir, f"{file_hash}.json")
            expiry = (datetime.now() + timedelta(hours=3)).isoformat()
            
            payload = {
                "hash": file_hash,
                "data": data,
                "created_at": datetime.now().isoformat(),
                "expires_at": expiry
            }
            
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                
            logger.info(f"💾 [LOCAL CACHE STORE] Saved extraction for key {file_hash}")
        except Exception as e:
            logger.error(f"OCRService: Failed to store local cache: {e}")

    # ── INDIVIDUAL STAGE RUNNERS ──────────────────────────────────────────────

    def _run_gemini_vision(self, content: bytes, mime_type: str) -> Dict:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"OCRService: Requesting Gemini Vision [{self.gemini_model}] (Attempt {attempt+1})")
                print(f"🤖 GEMINI REQUEST: Model={self.gemini_model}, Attempt={attempt+1}")
                response = self.gemini_client.models.generate_content(
                    model=self.gemini_model,
                    contents=[
                        types.Part.from_bytes(data=content, mime_type=mime_type),
                        types.Part.from_text(text=EXTRACTION_PROMPT),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=InvoiceExtraction,
                        temperature=0,
                        max_output_tokens=8192,
                    ),
                )
                
                # Check for parsed response directly (Elite SDK feature)
                if hasattr(response, "parsed") and response.parsed:
                    # Convert Pydantic model to dict
                    return response.parsed.model_dump(by_alias=True)
                
                return self._clean_and_parse(response.text.strip())
            except Exception as e:
                # Fast Fallback: If it's a 404 or first 429, don't wait too long if we have alternatives
                err_str = str(e).lower()
                is_quota = "429" in err_str or "resource_exhausted" in err_str
                is_not_found = "404" in err_str
                
                if is_not_found:
                    logger.warning(f"OCRService: Gemini model {self.gemini_model} not found (404). Rotating...")
                    self._rotate_gemini_model()
                    if attempt < max_retries - 1: continue
                    raise e

                wait_time = self._handle_quota_error(e, attempt, max_retries, provider="Gemini")
                if wait_time:
                    # If it's the first attempt and we have Groq, maybe just fail-over instead of waiting 16s?
                    # For now, we'll keep the wait but make it shorter for Gemini rotation
                    time.sleep(wait_time)
                    continue
                raise e

    def _run_groq_vision(self, content: bytes, mime_type: str) -> Dict:
        """Sends the image directly to Groq's Vision model via Base64 encoding."""
        max_retries = 1
        for attempt in range(max_retries):
            try:
                logger.info(f"OCRService: Requesting Groq Vision [{self.groq_model}]")
                base64_image = base64.b64encode(content).decode('utf-8')
                
                chat_completion = self.groq_client.chat.completions.create(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": EXTRACTION_PROMPT},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{mime_type};base64,{base64_image}"
                                    }
                                }
                            ]
                        }
                    ],
                    model=self.groq_model,
                    temperature=0,
                    response_format={"type": "json_object"}
                )
                raw = chat_completion.choices[0].message.content
                return self._clean_and_parse(raw)
            except Exception as e:
                wait_time = self._handle_quota_error(e, attempt, max_retries, provider="Groq")
                if wait_time:
                    time.sleep(wait_time)
                    continue
                raise e

    def _handle_quota_error(self, e: Exception, attempt: int, max_retries: int, provider: str) -> Optional[int]:
        err_str = str(e).upper()
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            # Rotate immediately on 429 to try a different model in the next attempt
            if provider == "Gemini":
                self._rotate_gemini_model()
            elif provider == "Groq":
                self._rotate_groq_model()

            if attempt < max_retries - 1:
                # Fast Fallback: Use a shorter wait (2s) for internal provider rotation
                wait = 2 
                logger.warning(f"OCRService: ⚠️ {provider} Quota Exhausted. Rotating and retrying in {wait}s...")
                return wait
            else:
                logger.error(f"OCRService: 🛑 Quota limit reached for {provider}.")
        return None

    def _rotate_gemini_model(self):
        """Switches current Gemini model to the next one in preferred list."""
        try:
            curr_idx = self.preferred_gemini.index(self.gemini_model)
            next_idx = (curr_idx + 1) % len(self.preferred_gemini)
            self.gemini_model = self.preferred_gemini[next_idx]
            logger.info(f"OCRService: 🔄 Rotating Gemini model to: {self.gemini_model}")
        except:
            pass

    def _rotate_groq_model(self):
        """Switches current Groq model to the next one in preferred list."""
        try:
            curr_idx = self.preferred_groq.index(self.groq_model)
            next_idx = (curr_idx + 1) % len(self.preferred_groq)
            self.groq_model = self.preferred_groq[next_idx]
            logger.info(f"OCRService: 🔄 Rotating Groq model to: {self.groq_model}")
        except:
            pass

    def _post_process_results(self, data: Dict) -> Dict:
        """Centralized cleanup, consolidation, and normalization for all providers."""
        if not data: return {}
        
        # 1. Ensure keys exist
        if "items" not in data: data["items"] = []
        if "header" not in data: data["header"] = {}
        
        # 2. Consolidate items with same Product Code
        data["items"] = self._consolidate(data["items"])
        
        # 3. Unit Normalization (Elite Correction)
        for item in data["items"]:
            # Pick 'Unit' or 'UO' or 'UOM'
            unit = str(item.get("Unit") or item.get("UO") or item.get("UOM") or "").upper().strip()
            
            if any(x in unit for x in ["METRIC TON", "MTN", "M/T", "TONNE", "TONS", "METRIC T", " MT", "MT "]) or unit == "MT" or unit == "TO":
                item["Unit"] = "MT"
            elif any(x in unit for x in ["PIECE", "PCS", "PIECES", "NOS", "NUMBER", " NO"]):
                item["Unit"] = "PCS"
            elif any(x in unit for x in ["KILOGRAM", "KG.", "KGS", "K.G"]) or unit == "KG":
                item["Unit"] = "KG"
            elif unit:
                item["Unit"] = unit # Keep original if not matched but exists
            else:
                item["Unit"] = "PCS" # Default fallback
                
        logger.info(f"OCRService: Post-processed results. {len(data['items'])} items consolidated.")
        return data

    # ── CLEAN & PARSE ─────────────────────────────────────────────────────────

    def _clean_and_parse(self, raw: str) -> Dict:
        try:
            # 1. Basic Cleaning (Markdown fences and whitespace)
            raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"```$", "", raw).strip()
            s, e = raw.find("{"), raw.rfind("}")
            if s != -1 and e != -1:
                raw = raw[s : e + 1]

            # 2. Robust Cleaning (Hallucinated scratchpads/calculations)
            # e.g., "amount": 100, "- calculation" -> "amount": 100
            raw = re.sub(r'(\d+\.?\d*)\s*,\s*["\']- [^"\'\}]+["\']', r'\1', raw)
            
            # 3. Trailing comma cleanup
            raw = re.sub(r',\s*\}', '}', raw)
            raw = re.sub(r',\s*\]', ']', raw)
            
            data = json.loads(raw)
            return data
        except Exception as e:
            logger.error(f"OCRService: JSON Parsing Error: {e}\nRaw output: {raw[:200]}...")
            raise Exception(f"Failed to parse AI output into JSON: {str(e)}")

    def _consolidate(self, items: List[Dict]) -> List[Dict]:
        merged: Dict[str, Dict] = {}
        for item in items:
            p_code = str(item.get("Product Code") or "").strip().upper()
            p_name = str(item.get("Product Name") or "").strip().upper()
            p_batch = str(item.get("Batch Number") or "").strip().upper()
            
            # Key priority: (Product Code + Batch) if valid, else Product Name
            code_part = p_code if (p_code and p_code not in ["...", "NONE", "UNKNOWN"]) else p_name
            if not code_part: code_part = "UNKNOWN"
            
            # Composite key ensures different batches stay separate
            key = f"{code_part}|{p_batch}"

            if key in merged:
                base = merged[key]
                base["Quantity Received"] = self._sum_str(
                    base.get("Quantity Received"), item.get("Quantity Received"))
                base["Number of Bags"] = int(float(self._sum_str(
                    base.get("Number of Bags", 0), item.get("Number of Bags", 0))))
                base["Total Amount"] = (
                    self._to_float(base.get("Total Amount"))
                    + self._to_float(item.get("Total Amount")))
                
                # --- Elite Batch Merger ---
                b1 = str(base.get("Batch Number", "")).strip()
                b2 = str(item.get("Batch Number", "")).strip()
                
                if b2 and b2 not in b1:
                    if b1 and b1 not in ["...", "NONE", "UNKNOWN"]:
                        base["Batch Number"] = f"{b1}, {b2}"
                    else:
                        base["Batch Number"] = b2
            else:
                merged[key] = dict(item)
        return list(merged.values())

    def _merge_taxes(self, taxes1: List[Dict], taxes2: List[Dict]) -> List[Dict]:
        """Sums amounts for taxes with the same label."""
        tax_map: Dict[str, float] = {}
        for t in (taxes1 or []) + (taxes2 or []):
            if isinstance(t, dict):
                label = str(t.get("label", "Tax")).strip().upper()
                amt = self._to_float(t.get("amount", 0))
                tax_map[label] = tax_map.get(label, 0.0) + amt
        
        return [{"label": label, "amount": round(val, 2)} for label, val in tax_map.items()]

    def _to_float(self, val) -> float:
        if isinstance(val, (int, float)):
            return float(val)
        try:
            # Handle cases with commas like "1,234.56"
            clean_val = str(val).replace(",", "")
            nums = re.findall(r"[-+]?\d*\.?\d+", clean_val)
            return float(nums[0]) if nums else 0.0
        except Exception:
            return 0.0

    def _sum_str(self, a, b) -> str:
        return str(self._to_float(a) + self._to_float(b))

    def _optimize_image(self, content: bytes, mime_type: str) -> (bytes, str):
        """Resizes and compresses image to reduce payload size and costs."""
        img = Image.open(io.BytesIO(content))
        orig_size = len(content)
        
        # 1. Resize if too large
        w, h = img.size
        if max(w, h) > MAX_IMAGE_DIMENSION:
            scale = MAX_IMAGE_DIMENSION / max(w, h)
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            logger.info(f"OCRService: Resized image from {w}x{h} to {new_size[0]}x{new_size[1]}")

        # 2. Compress as JPEG
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        optimized_content = buffer.getvalue()
        
        new_size = len(optimized_content)
        reduction = (1 - new_size / orig_size) * 100
        logger.info(f"OCRService: Optimized image {orig_size/1024:.1f}KB -> {new_size/1024:.1f}KB ({reduction:.1f}% reduction)")
        
        return optimized_content, "image/jpeg"

# ── EXTRACTION PROMPT ─────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """
Extract invoice data from the image into the specified JSON format.
Ensure 100% accuracy for financial totals and product details.
Consolidate identical product codes by summing quantities and joining batches.

COMMAND — Return the data in the following standardized JSON format:
{
  "header": {
    "Date": "YYYY-MM-DD",
    "Invoice Number": "...",
    "Supplier Name": "...",
    "Supplier GST": "...",
    "Vehicle Number": "...",
    "Transporter Name": "...",
    "Taxable Amount": 0.0,
    "Taxes": [
      { "label": "CGST", "amount": 0.0 },
      { "label": "SGST", "amount": 0.0 },
      { "label": "IGST", "amount": 0.0 }
    ],
    "Transport / Freight": 0.0,
    "Grand Total": 0.0
  },
  "items": [
    {
      "Product Code": "...",
      "Product Name": "...",
      "Batch Number": "...",
      "Quantity Received": 0.0,
      "Unit": "...",
      "Number of Bags": 0,
      "Rate per Unit": 0.0,
      "Total Amount": 0.0
    }
  ]
}

═══════════════════════════════════════════════════════════════════════
FIELD MAPPING — Accept ANY of these aliases (case-insensitive, fuzzy-match)
═══════════════════════════════════════════════════════════════════════

▸ Date: Date, Dated, Bill Date, Date of Issue, Doc Date, Invoice Date, Voucher Date,
  Entry Date, Transaction Date, Challan Date, GRN Date, PO Date, Delivery Date,
  Receipt Date, Posting Date, Value Date, Tax Invoice Date, Dt, Dte, Date of Supply,
  Date of Delivery, Dispatch Date, Shipment Date, Order Date, Booking Date,
  Created Date, Due Date, Expiry Date, Valid Till, Date of Prep, Date of Preparation,
  Date of Removal, Prep Date, Issue Date, Invoice Dt, Bill Dt, Challan Dt,
  Document Date, DATE OF ISSUE, Date & Time (extract date part only),
  Inv. Date, Inv Date, Tax Inv. Date, Sale Date, Purchase Date, Supply Date,
  Gate Entry Date, Inward Date, Outward Date, Material Date, Consignment Date,
  Loading Date, Unloading Date, Weighment Date, Billing Date, Generation Date...

▸ Invoice Number: Invoice No, Bill No, Bill Number, Serial No, Serial Number, Ref No,
  Challan No, D.O. No, DO No, Tax Invoice No, Invoice #, Bill #, Voucher No,
  Voucher Number, Document No, Doc No, Doc Number, GRN No, GRN Number, PO No,
  PO Number, Order No, Order Number, Delivery Note No, DN No, LR No, LR Number,
  Docket No, AWB No, E-Way Bill No, E-Way No, E-WayBill No, EWB No, EWB Number,
  Receipt No, Memo No, Credit Note No, Debit Note No, Note No, Consignment No,
  Parcel No, Slip No, Challan Number, Ref Number, Reference No, Reference Number,
  Transaction No, Transaction ID, Indent No, Gate Entry No, Inward No,
  Material Receipt No, MRN No, SRN No, SERIAL NUMBER, UPG No, Sr. No, SR NO,
  SR. NO., Supply Invoice No, Tax Inv No, Tax Inv. No., IRN (only if no invoice found),
  ORDER No, D.O. No & Date (number only), L.R. No & Date (number only),
  REF1, REF 1, Ref. No, Inv No, Inv. No., Inv #, Supply No, SB No, BE No,
  Customs No, Port Code, Job No, Work Order No, Contract No, Agreement No,
  Consignment Note No, CN No, RR No, Railway Receipt No, PWB No, Airway Bill No,
  Booking No, Lot No (invoice level), Batch Invoice No, Run No (invoice level),
  Proforma Invoice No, PI No, Advance Invoice No, Tax Credit Note No,
  Debit Memo No, Credit Memo No, Supplementary Invoice No, Revised Invoice No...

▸ Supplier Name: Supplier, Sold By, Seller, From, Consignor, Company, Vendor,
  Vendor Name, Party Name, Party, Manufacturer, Distributor, Dealer, Trader,
  Firm Name, Business Name, Billed By, Dispatched By, Shipped By, Forwarded By,
  Agent, Broker, Mill Name, Factory Name, Source, Principal, Exporter, Importer,
  Proprietor, Organization, Entity Name, Supplier / Vendor, Name of Supplier,
  Name of Seller, Creditor, Remitter, Issuer, Consignor Name, Seller Name,
  Service Provider, Biller, Invoice Party, Billing Party, Originator,
  Maker, Producer, Grower, Packer, Brand Owner, Label Holder, Licensor,
  **CRITICAL**: Always extract the SUPPLIER / INVOICE ISSUER, never the buyer/consignee...

▸ Supplier GST: GST No, GSTIN, GST Number, Tax ID, TIN No, TIN, VAT No, VAT Number,
  Service Tax No, CIN No, PAN No, PAN Number, GSTIN of Supplier, Seller GSTIN,
  Vendor GSTIN, Party GSTIN, Tax Registration No, Tax Reg No, GST Reg No,
  GST Registration Number, CST No, LST No, Excise No, IEC Code, FSSAI No,
  Import Export Code, Udyam No, MSME No, GST NO. (under SUPPLIER column),
  **FORMAT**: Must be 15-character alphanumeric (e.g. 09AAACG1209J3ZS).
  Extract SUPPLIER's GSTIN only. Reject buyer/consignee GSTIN...

▸ Vehicle Number: Vehicle No, Veh. No, Truck No, RC No, Registration Number, Reg No,
  Transport No, Lorry No, Lorry Number, Truck Number, Tempo No, Vehicle Registration,
  Vehicle Reg No, Tractor No, Tanker No, Container No, Fleet No, Conveyance No,
  Carrier No, Car No, Auto No, Van No, LCV No, HCV No, Transport Vehicle No,
  VEHICLE REGN. NO., Veh Reg No, Vehicle Regd No, Vehicle Registration No, Vehicle No.,
  Regn. No., Reg. No., RC Number, GJ/MH/UP/HP/DL/RJ/MP/KA/TN/AP/TS prefix plates,
  Trailer No, Semi-Trailer No, Bulker No, Tipper No, Container No, Flat Bed No,
  Wagon No, Rail No (for rail transport), Ship No, Vessel No (for sea transport)...

▸ Transporter Name: Transporter, Transporter Name, Transport Company, Carrier Name,
  Logistics Company, Shipping Company, Freight Company, Courier, Forwarding Agent,
  Transport Agency, Transporter / Carrier, Carried By, Dispatched Via, Shipped Via,
  Transport Party, Lorry Operator, Fleet Owner, GTA Name, Goods Transport Agency,
  TRANSPORTER NAME, Transport Name, Logistics Partner, LR Issued By,
  Courier Company, Freight Forwarder, C&F Agent, Clearing Agent, Shipping Agent,
  Freight Broker, Cargo Agent, Hauler, Road Carrier, Rail Carrier, Air Carrier,
  Sea Carrier, NVOCC, MTO, Multimodal Operator, Express Company, Last Mile Partner...

▸ Product Code: Product Code, Item Code, Part No, SKU, Article No, Model No, ID,
  Part Number, Item No, Item Number, Material Code, Material No, Cat No,
  Catalogue No, Catalogue Number, Stock Code, Stock No, Reference Code, Ref Code,
  Product ID, Item ID, BOM Code, Component Code, HSN, HSN Code, HSN/SAC, SAC Code,
  UPC Code, EAN Code, ASIN, Internal Code, System Code, Drawing No,
  Specification No, Grade Code, Variant Code, PRODUCT CODE (column header),
  Item Ref, P. Code, Prod Code, Code, Item#, Product#, Material Number,
  Stock Keeping Unit, Commodity Code, Tariff Code, HS Code, CAS No (chemicals),
  UN No (hazardous), IMDS No, OEM Code, Vendor Code, Buyer Code, Alt Code,
  Substitute Code, Legacy Code, Old Code, New Code, Cross Ref Code,
  Color Code, Size Code, Grade, Quality Code, Batch Code (if used as product ID)...
  **CRITICAL**: If an internal "Product Code", "SKU", or "Item ID" is present, use it as 
  the primary "Product Code". Use HSN/SAC only as a fallback if no other code exists.

▸ Product Name: Description, Description of Goods, Product Name, Material,
  Item Description, Item Name, Product Description, Goods Description, Particulars,
  Commodity, Article, Material Description, Name of Product, Name of Goods,
  Name of Item, Material Name, Commodity Name, Subject, Nature of Goods,
  Details, Specification, Product Details, Item Particulars, Goods, Stock Item,
  Service Description, Nature of Supply, Category, Product Title, Brand Name,
  Short Description, Full Description, DESCRIPTION OF GOODS, Product, Item,
  Good, Supply Description, Goods/Services, Product & Grade, Grade,
  Material Grade, Product Grade, Prod. Name, Item Desc, Article Name, Goods Name,
  Chemical Name, Trade Name, Generic Name, Common Name, Technical Name,
  IUPAC Name, Composition, Formulation, Mixture, Blend, Alloy, Grade/Spec,
  Size/Spec, Dimension, Type, Variety, Make, Model, Configuration, Pack Size...

▸ Batch Number: Batch No, Batch Number, Lot No, Lot Number, Batch/Lot No,
  Manufacturing Lot, Mfg Batch, Production Batch, Batch ID, Lot ID, Serial Batch,
  BATCH NO. (column header), Batch, Lot, Production No, Run No, Cast No,
  Heat No, Melt No, Charge No, Coil No, Roll No, Bundle No, Drum No, Can No,
  Pack Lot, Manufacture Batch, Quality Lot, MFG Lot, Production Lot,
  Manufacturing Batch No, Mfg. Batch No, Process Batch, Campaign No,
  Expiry Batch, EXP Batch, Best Before Batch, BBD Batch, COA Batch,
  Test Batch, QC Batch, Release Batch, Approved Batch, Quarantine Batch...

▸ Quantity Received: Quantity, Qty, Qty Received, Received Qty, Nos, Pcs, Pieces,
  Count, Number, No. of Units, Units Received, Total Qty, Dispatched Qty,
  Shipped Qty, Delivered Qty, Accepted Qty, Inspected Qty, Actual Qty, GRN Qty,
  Inward Qty, Received Quantity, Net Qty, Gross Qty, Billed Qty, Ordered Qty,
  Supply Qty, Qty Supplied, Qty Accepted, Qty Delivered, Volume, Amount of Goods,
  QUANTITY (column), Qty (MT), Qty (KG), Qty (PCS), Net Weight, Net Wt,
  Net Wt., NW, Gross Weight (use net if both), GW, Gross Wt, Total Weight,
  Wt., Weight, Measure, Measurement, Extent, Magnitude, Size, Count Received,
  **CRITICAL**: Extract as plain number only. "7.675 TO" → 7.675...

▸ Unit (UOM) — WEIGHT:
  MT, Metric Tonne, Metric Ton, M.T., M/T, MTS, TO, Ton, Tonne, Tonnes, Tons,
  T (when weight context), Long Ton, Short Ton, LT, ST,
  KG, Kilogram, Kilograms, Kgs, Kg., K.G., KGS,
  G, GM, Gram, Grams, Grm, Gm., GMS,
  MG, Milligram, Milligrams, MGS,
  LB, Lbs, Pound, Pounds, Lb.,
  OZ, Ounce, Ounces,
  QUINTAL, QTL, Qtl., Q, Quintal, Quintals, QNT, QNTL,
  → NORMALIZE: TO/Ton/Tonne/Tonnes/M.T. → MT | Kgs/KGS → KG | Qnl/Qtl → QTL

▸ Unit (UOM) — VOLUME:
  LTR, L, Liter, Litre, Litres, Liters, Ltr., Lt,
  ML, Milliliter, Millilitre, Milliliters, Millilitres, Ml,
  KL, Kiloliter, Kilolitre, KL., KLT,
  CBM, M3, Cubic Meter, Cubic Metre, CUM, CU.M,
  CFT, CuFt, Cubic Feet, Cubic Foot, CU.FT,
  GAL, Gallon, Gallons, US Gal, Imp Gal,
  BBL, Barrel, Barrels, BRL,
  → NORMALIZE: Ltr/Lt/Litre → LTR | Ml → ML | KL/KLT → KL

▸ Unit (UOM) — LENGTH / AREA:
  MTR, M, Meter, Metre, Meters, Metres, Mtr., Mt (length context),
  CM, Centimeter, Centimetre, CMS,
  MM, Millimeter, Millimetre, MMS,
  FT, Feet, Foot, Ft.,
  INCH, In, Inches, IN,
  YD, Yard, Yards,
  RMT, RM, Running Meter, Running Metre, R/MTR, RNG MTR,
  SQM, M2, Square Meter, Square Metre, Sq.M, SQ.MTR,
  SQFT, Sq.Ft, Square Feet, Square Foot, SQ.FT,
  SQYD, Sq.Yd, Square Yard,
  → NORMALIZE: Mtr/M/Metre → MTR | Sq.M/SQM → SQM | Sq.Ft → SQFT

▸ Unit (UOM) — PIECES / COUNT:
  PCS, Piece, Pieces, PC, Pcs.,
  NOS, No., Nos., Numbers, Number,
  EA, Each, Each.,
  UNIT, Units, U,
  SET, Sets, ST,
  PAIR, Pairs, PR,
  DOZEN, DZ, Doz, Dozen, Dozens,
  GROSS, GR (count context), Gross,
  → NORMALIZE: Nos/Number/No. → PCS | Each/EA → PCS | Unit/U → PCS

▸ Unit (UOM) — PACKAGING:
  BAG, Bags, BG,
  BUNDLE, Bundles, BDL, Bndl,
  BOX, Boxes, BX,
  CTN, Carton, Cartons, Ctn.,
  ROLL, Rolls, RL, Rll,
  DRUM, Drums, DR,
  CAN, Cans, CN,
  TIN, Tins, TN,
  POUCH, Pouches, PCH,
  PACKET, Packets, PKT, Pkt.,
  SACK, Sacks, SK,
  CASE, Cases, CS,
  PALLET, Pallets, PLT,
  BALE, Bales, BL,
  CRATE, Crates, CR,
  COIL, Coils, CL,
  SLAB, Slabs, SB (packaging context),
  → NORMALIZE: Bndl → BUNDLE | Ctn/CTN → CTN | Pkt → PACKET

▸ Unit (UOM) — INDUSTRIAL / CONSTRUCTION:
  BAR, Bars, BR,
  ROD, Rods, RD,
  PIPE, Pipes, PP,
  SHEET, Sheets, SHT,
  PLATE, Plates, PLT (plate context),
  SLAB, Slabs,
  COIL, Coils,
  LENGTH, Lengths, LNG,
  SECTION, Sections, SEC,
  BEAM, Beams,
  ANGLE, Angles,
  CHANNEL, Channels,
  FLANGE, Flanges,
  FITTING, Fittings,
  JOINT, Joints,
  VALVE, Valves,
  PUMP, Pumps,
  MOTOR, Motors,
  → Keep as-is unless a standard abbreviation exists

▸ Taxable Amount: Total Taxable Value, Sub-Total, Net Weight Total Value,
  Total before Tax, Pre-Tax Total, Basic Total, Assessed Value Total,
  Amount Before Tax, TAXABLE AMOUNT, Taxable Value, Total Taxable Amount,
  Total Basic Amount, Gross Taxable, Subtotal, SUB TOTAL, Total (Excl. Tax),
  Total (Excl. GST), Total Ex-Tax, Total Pre-Tax, Net Amount (before GST),
  Basic Amount Total, Chargeable Amount, Value Before Tax, Taxable Base,
  Taxable Supply Value, Assessable Value, Pre-GST Total, Tax Base,
  Total Assessable Value, Aggregate Value, Basic Value, Supply Value...

▸ Transport / Freight: Freight, Freight Amount, Freight Charges, Transport Charges,
  Cartage, Delivery Charges, Carriage, Carriage Inward, Carriage Outward,
  Forwarding Charges, Handling Charges, Loading Charges, Unloading Charges,
  Logistics Charges, Shipping Charges, Courier Charges, Packing & Forwarding,
  P&F Charges, Octroi, Entry Tax, Toll Charges, Transit Charges, Conveyance Charges,
  Drayage, Porterage, Haulage, Godown Charges, Demurrage, Transportation Cost,
  Freight & Cartage, LR Charges, Dispatch Charges, FREIGHT (row label), Freight Total,
  Freight Value, Transport Total, Logistics Total, Door Delivery, Home Delivery,
  Last Mile Charges, First Mile Charges, Cross Docking, Transhipment Charges,
  Container Freight, Port Handling, CFS Charges, Inland Haulage, ICD Charges,
  **CRITICAL**: Extract TOTAL FREIGHT RUPEE AMOUNT only, never the per-unit rate...

▸ Grand Total: Grand Total, Invoice Total, Total Payable, Net Amount, Bill Value,
  Net Total, Gross Total, Final Total, Total Amount Payable, GRAND TOTAL,
  INVOICE VALUE, Invoice Grand Total, Total Invoice Value, Total Bill Amount,
  Amount Payable, Net Payable, Balance Due, ROUND OFF TOTAL, Rounded Total,
  Total (Incl. Tax), Total (Incl. GST), Total with GST, Total Including All Taxes,
  Rs. Total, INR Total, Net Due, Closing Balance, Total Charges, Final Amount,
  Bill Total, Payable Amount, QUALITY CHECK INVOICE VALUE, Amount in Words (as backup),
  Total Amount Due, Invoice Amount, Final Invoice Value, Net Invoice Amount,
  Total Sum, Total Consideration, Contract Value, PO Value, Supply Value...

▸ Taxes — INDIAN GST COMPONENTS:
  IGST, Integrated GST, Integrated Tax, IGST Amount, IGST @%, IGST @ %,
  CGST, Central GST, Central Tax, CGST Amount, CGST @%, CGST @ %,
  SGST, State GST, State Tax, SGST Amount, SGST @%, SGST @ %,
  UTGST, Union Territory GST, UT Tax, UTGST Amount,
  GST Cess, GST Compensation Cess, Cess, Cess Amount,
  Additional Cess, Higher Cess, Clean Energy Cess,
  Tobacco Cess, Pan Masala Cess, Luxury Cess,

▸ Taxes — CUSTOMS & IMPORT DUTIES:
  BCD, Basic Customs Duty, Basic Duty, Import Duty,
  CVD, Countervailing Duty,
  SAD, Special Additional Duty, Additional Customs Duty,
  AIDC, Agriculture Infrastructure Development Cess,
  SWS, Social Welfare Surcharge, Surcharge on BCD,
  Anti-Dumping Duty, ADD, Safeguard Duty, SGD,
  Countervailing Duty on Subsidy, CVD Subsidy,
  Protective Duty, Transitional Product Specific Safeguard Duty,

▸ Taxes — LEGACY / PRE-GST (older invoices):
  VAT, Value Added Tax, VAT Amount, VAT @%,
  CST, Central Sales Tax, CST Amount,
  Excise Duty, CENVAT, Central Excise, BED, Basic Excise Duty,
  AED, Additional Excise Duty, SED, Special Excise Duty,
  Service Tax, ST Amount, Education Cess, EC, SHE Cess, SHEC,
  Swachh Bharat Cess, SBC, Krishi Kalyan Cess, KKC,
  Entry Tax, Octroi, LBT, Local Body Tax,
  Purchase Tax, Turnover Tax, TOT,

▸ Taxes — OTHER DEDUCTIONS / CHARGES (as negative tax or separate):
  TDS, Tax Deducted at Source, TDS Amount, TDS @%,
  TCS, Tax Collected at Source, TCS Amount, TCS @%,
  WHT, Withholding Tax,
  Reverse Charge, RCM Amount,
  **FORMAT ALL TAXES AS**:
  [{"label": "IGST", "amount": 271242.22}, {"label": "CGST", "amount": 0.0}, ...]
  Extract EACH component ONCE. Never sum CGST+SGST into IGST or vice versa...

═══════════════════════════════════════════════════════════════════
DIGIT ACCURACY RULES — OCR Error Prevention (READ CAREFULLY)
═══════════════════════════════════════════════════════════════════

COMMON OCR CONFUSIONS — CHECK EVERY NUMBER:

  ① 0 vs O/o    → Zero looks like letter O in poor scans. 
                   If in a numeric field and surrounded by digits, it is 0.
                   e.g. "1O06" → likely "1006" | "O.5" → likely "0.5"

  ② 1 vs l/I/i  → Digit 1, lowercase L, uppercase I, lowercase i all look alike.
                   In numeric fields: always treat as digit 1.
                   e.g. "l25" → "125" | "1OI" in number → "101"

  ③ 2 vs Z      → "2" and "Z" look similar in handwriting/bad print.
                   e.g. "Z5.00" in amount field → "25.00"

  ④ 3 vs 8      → Top half of 8 can look like 3 in poor scans.
                   VERIFY: Qty × Rate must ≈ Total Amount.
                   e.g. if Qty=7.675, Rate=125120 → Total ≈ 960,297 → 
                   if extracted total shows "360296" → likely "960296"

  ⑤ 5 vs 6      → Curved top of 6 can look like 5 in low-res scans.
                   e.g. "5000" vs "6000" — cross-check with totals.

  ⑥ 6 vs 0      → "6" and "0" can swap in bold/compressed fonts.
                   e.g. "16,00,000" vs "10,00,000" — verify with grand total.

  ⑦ 7 vs 1      → Diagonal stroke of 7 may look like 1 in some fonts.
                   e.g. "7.675" vs "1.675" — check if qty makes sense with bags.

  ⑧ 8 vs 3      → Already noted above. Especially dangerous in:
                   GST amounts, rate per unit, total amounts.
                   ALWAYS cross-verify: SubTotal + Taxes ≈ Grand Total.

  ⑨ 9 vs 4/7    → "9" can resemble "4" in certain typefaces.
                   e.g. "₹9000" vs "₹4000" — verify against line totals.

  ⑩ Comma vs Period (Decimal):
                   Indian invoices use "1,00,000.00" (comma=thousands, period=decimal).
                   European invoices may use "1.00.000,00" (reversed).
                   Strip ALL commas from Indian invoices before parsing numbers.
                   e.g. "1,50,000.50" → 150000.50

  ⑪ ₹ / Rs / Rs. prefix:
                   Always strip before parsing. "₹1,50,000.50" → 150000.50

  ⑫ Lakh / Crore words:
                   "1.5 Lakh" → 150000 | "1 Crore" → 10000000
                   "17,78,144" → 1778144 (Indian lakh format, read as: 17 lakh 78 thousand 144)

  ⑬ Trailing/Leading spaces in numbers:
                   " 125120.000 " → 125120.0 (strip whitespace)

  ⑭ Hyphen vs Negative:
                   "–1000.00" or "(1000.00)" both mean negative (discounts).
                   Discounts reduce taxable amount; do not extract as separate field.

  ⑮ Superscript / Subscript confusion:
                   "125¹²⁰" is NOT 12,500,000. It is likely "125120" with poor print.
                   Read the full numeric string in context.

CROSS-VALIDATION CHECKS (perform before output):
  ✓ CHECK 1 — Line level:   Qty × Rate ≈ Line Total Amount (tolerance ±1%)
  ✓ CHECK 2 — Item total:   Sum of all Line Totals ≈ Sub Total
  ✓ CHECK 3 — Tax check:    CGST ≈ SGST (for intra-state; if not equal, re-examine)
  ✓ CHECK 4 — Grand total:  Taxable Amount + Sum(All Taxes) + Freight ≈ Grand Total
  ✓ CHECK 5 — Words check:  Grand Total in words (if present) must match numeric total
                             "Seventeen Lakh Seventy Eight Thousand One Hundred Forty Four"
                             → 1,778,144 → must match extracted Grand Total
  ✓ CHECK 6 — Bag sense:    Number of Bags should be a whole integer, never a decimal.
                             307 bags ✓ | 30.7 bags ✗ → re-examine

═══════════════════════════════════════════════════════════════════
EXTRACTION RULES
═══════════════════════════════════════════════════════════════════

1.  STRICT JSON OUTPUT
    Return ONLY valid JSON. No scratchpad, no explanation, no markdown fences,
    no comments inside JSON. All numbers must be plain floats or integers.

2.  SUPPLIER vs BUYER DISAMBIGUATION
    Always extract SUPPLIER (invoice issuer), never buyer/consignee/sold-to party.
    On GAIL-style invoices with a SUPPLIER column and a CONSIGNEE column —
    SUPPLIER side = Supplier Name + Supplier GST.

3.  INVOICE NUMBER PRIORITY
    Serial Number > Tax Invoice No > Invoice No > DO No > LR No > Order No > IRN.
    Extract the primary number only. If "& Date" follows, extract number only.

4.  TAX EXTRACTION — ZERO DOUBLE COUNTING
    Extract each tax line ONCE using the bill-level summary table.
    IGST only → CGST=0, SGST=0. CGST+SGST only → IGST=0.
    Never sum or split components. Never merge CGST+SGST into one field.
    Include ALL tax types found: Cess, TCS, UTGST, legacy taxes etc.

5.  FREIGHT — TOTAL AMOUNT NOT RATE
    Extract the TOTAL freight rupee amount (e.g. 23461.20).
    Rs/MT or Rs/KG is the RATE — ignore it for this field.
    Freight total is found in the summary/totals table, not the line-item table.

6.  UNIT NORMALIZATION
    TO/Ton/Tonne/Tonnes/M.T./MTS → MT
    Nos/Number/No./EA/Each/Unit → PCS
    Kgs/KGS → KG | Ltr/Lt/Litre → LTR | Mtr/M/Metre → MTR
    Qnl/Qtl/Quintal → QTL | Sq.M/SQM/M2 → SQM | Sq.Ft/SQFT → SQFT

7.  QUANTITY — NUMBERS ONLY
    Strip unit text from quantity cell. "7.675 TO" → 7.675.
    Never include unit abbreviation inside Quantity Received.

8.  DATE NORMALIZATION
    All dates → YYYY-MM-DD.
    "March 08, 2026" → "2026-03-08"
    "08/03/26" → "2026-03-08"
    "08-Mar-26" → "2026-03-08"

9.  DISCOUNTS
    Cash Discount, MIS Discount, Trade Discount reduce taxable base.
    Reflect the POST-DISCOUNT taxable amount. Do not extract discount rows.

10. MULTI-LINE ITEMS
    Same product on multiple rows (different batch/partial qty) → extract as
    SEPARATE items in the array. Never merge or sum line items.

11. MISSING FIELDS
    Genuinely absent → "" for strings, 0.0 for numbers. Never hallucinate.

12. HANDWRITTEN / STAMPED VALUES
    Gate entry numbers, bag counts, or handwritten annotations are valid.
    Extract if they map to a defined field (e.g. bag count stamped at bottom).

13. OBSCURED TEXT
    Extract what is legible. Do not fabricate obscured portions.

14. NUMBER OF BAGS — INTEGER ONLY
    Physical package count. Always a whole number. Never a decimal.
    "307 Bags" → 307. If unclear, extract 0.

15. AMOUNT IN WORDS — USE AS BACKUP VALIDATOR
    If Grand Total in words is present, parse it and confirm it matches
    the numeric Grand Total. If mismatch, trust the words over a possibly
    misread numeric (OCR often corrupts numbers, rarely corrupts words).

16. GSTIN FORMAT VALIDATION
    Valid GSTIN = 15 chars: 2-digit state code + 10-char PAN + 1 entity + 1 Z + 1 check.
    e.g. 09AAACG1209J3ZS. If extracted value doesn't match this pattern, re-examine.

17. VEHICLE NUMBER FORMAT
    Indian vehicle numbers follow: [State Code][District][Series][Number]
    e.g. GJ05CW8825 | MH12AB1234 | UP78CD9012
    Extract full alphanumeric. Do not split at spaces.

18. RATE × QTY = TOTAL VALIDATION
    For every line item, verify: Rate per Unit × Quantity Received ≈ Total Amount.
    If mismatch > 2%, re-examine all three values for OCR digit errors.
    Use the cross-validation digit confusion table (Section above) to correct.
"""

ocr_service = OCRService()
