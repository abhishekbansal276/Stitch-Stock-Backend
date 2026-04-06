import os
import json
import re
import time
import logging
import base64
from typing import Dict, List, Optional
from google import genai
from google.genai import types
from groq import Groq

# ── LOGGING CONFIGURATION ───────────────────────────────────────────────────
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
            ]
            self.gemini_model = self._pick_gemini_model()
            logger.info(f"OCRService: Gemini initialized with model: {self.gemini_model}")
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
                    "meta-llama/llama-4-scout-17b-16e-instruct", # Latest 2026 Vision
                    "llama-3.2-11b-vision-instant",
                    "llama-3.2-90b-vision-preview",
                ]
                self.groq_model = self._pick_groq_model()
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

    def _pick_groq_model(self) -> str:
        try:
            available_models = self.groq_client.models.list()
            available_ids = {m.id for m in available_models}
            for p in self.preferred_groq:
                if p in available_ids:
                    return p
        except Exception as e:
            logger.warning(f"OCRService: Could not list Groq models: {e}. Defaulting to meta-llama/llama-4-scout-17b-16e-instruct")
        return "meta-llama/llama-4-scout-17b-16e-instruct"

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

        # --- STAGE 1: GEMINI VISION ---
        if self.gemini_client:
            try:
                return self._run_gemini_vision(content, mime_type)
            except Exception as e:
                logger.warning(f"OCRService: Stage 1 (Gemini Vision) failed: {e}")
                if self.groq_client:
                    logger.info("OCRService: 🔄 Falling back to Stage 2 (Groq Vision)...")
                else:
                    raise Exception(f"Extraction failed: Gemini exhausted and no Groq fallback configured. Error: {e}")

        # --- STAGE 2: GROQ VISION FALLBACK ---
        if self.groq_client:
            try:
                return self._run_groq_vision(content, mime_type)
            except Exception as e:
                logger.error(f"OCRService: Stage 2 (Groq Vision) failed: {e}")
                # If we get a "model_decommissioned" or 400 error, try rotating and retrying once
                if "model_decommissioned" in str(e).lower() or "400" in str(e):
                    logger.warning("OCRService: Groq model decommissioned or invalid. Attempting rotation...")
                    self._rotate_groq_model()
                    return self._run_groq_vision(content, mime_type)
                raise Exception(f"Total extraction failure across all vision providers: {e}")

        raise Exception("OCRService: Extraction failed. No working Vision clients available.")

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
                wait_time = self._handle_quota_error(e, attempt, max_retries, provider="Gemini")
                if wait_time:
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
            if attempt < max_retries - 1:
                # Standard wait time for quota resets
                wait = 16 
                logger.warning(f"OCRService: ⚠️ {provider} Quota Exhausted. Waiting {wait}s before retry...")
                return wait
            else:
                logger.error(f"OCRService: 🛑 Quota limit reached for {provider}.")
                if provider == "Gemini":
                    self._rotate_gemini_model()
                elif provider == "Groq":
                    self._rotate_groq_model()
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
            
            # --- ELITE FIX: Calculate Final Amount Locally ---
            for item in data["items"]:
                total_amt = self._to_float(item.get("Total Amount", 0))
                taxes = item.get("Taxes", [])
                if not isinstance(taxes, list):
                    taxes = []
                
                tax_sum = sum(self._to_float(t.get("amount", 0)) for t in taxes if isinstance(t, dict))
                item["Final Amount"] = round(total_amt + tax_sum, 2)
                item["Taxes"] = taxes # Ensure it stays a list
            
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
                
                # --- NEW: Merge Tax Lists ---
                base["Taxes"] = self._merge_taxes(base.get("Taxes", []), item.get("Taxes", []))

                if not base.get("Unit") or base.get("Unit") == "PCS":
                    base["Unit"] = item.get("Unit") or base.get("Unit")
                if item.get("Batch Number") and item["Batch Number"] not in str(base.get("Batch Number", "")):
                    base["Batch Number"] = f"{base.get('Batch Number', '')}, {item['Batch Number']}".strip(", ")
            else:
                merged[key] = dict(item)
        return list(merged.values())

    def _merge_taxes(self, taxes1: List[Dict], taxes2: List[Dict]) -> List[Dict]:
        """Sums amounts for taxes with the same label."""
        tax_map: Dict[str, float] = {}
        for t in taxes1 + taxes2:
            if isinstance(t, dict):
                label = str(t.get("label", "Tax")).strip().upper()
                amt = self._to_float(t.get("amount", 0))
                tax_map[label] = tax_map.get(label, 0.0) + amt
        
        return [{"label": label, "amount": round(val, 2)} for label, val in tax_map.items()]

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
You are an elite Inventory Auditor AI. Your job is to extract data with 100% accuracy 
from Indian supplier invoices and delivery challans into the JSON format below.

═══════════════════════════════════════════════════════════════════
OUTPUT — Return ONLY this JSON. No explanation. No markdown fences.
═══════════════════════════════════════════════════════════════════
{
  "header": {
    "Date": "YYYY-MM-DD",
    "Invoice Number": "...",
    "Supplier Name": "...",
    "Supplier GST": "...",
    "Vehicle Number": "...",
    "Transporter Name": "...",
    "Transport / Freight": 0.0
  },
  "items": [
    {
      "Product Code": "...",
      "Product Name": "...",
      "Model Name": "...",
      "Batch Number": "...",
      "Quantity Received": "...",
      "Unit": "...",
      "Number of Bags": "...",
      "Rate per Unit": 0.0,
      "Total Amount": 0.0,
      "Taxes": [
        { "label": "CGST", "amount": 0.0 },
        { "label": "SGST", "amount": 0.0 }
      ],
      "Remarks": "..."
    }
  ]
}

═══════════════════════════════════════════════
FIELD MAPPING — accept ANY of these label aliases
═══════════════════════════════════════════════

▸ Date: Date, Dated, Bill Date, Date of Issue, Doc Date, Invoice Date, Voucher Date, Entry Date, Transaction Date, Challan Date, GRN Date, PO Date, Delivery Date, Receipt Date, Posting Date, Value Date, Tax Invoice Date, Dt, Dte, Date of Supply, Date of Delivery, Dispatch Date, Shipment Date, Order Date, Booking Date, Created Date, Due Date, Expiry Date, Valid Till...

▸ Invoice Number: Invoice No, Bill No, Bill Number, Serial No, Ref No, Challan No, D.O. No, Tax Invoice No, Invoice #, Bill #, Voucher No, Voucher Number, Document No, Doc No, Doc Number, GRN No, GRN Number, PO No, PO Number, Order No, Order Number, Delivery Note No, DN No, LR No, Docket No, AWB No, E-Way Bill No, E-Way No, Receipt No, Memo No, Credit Note No, Debit Note No, Note No, Consignment No, Parcel No, Slip No, Challan Number, Ref Number, Reference No, Reference Number, Transaction No, Transaction ID, Indent No, Gate Entry No, Inward No, Material Receipt No, MRN No, SRN No...

▸ Supplier Name: Supplier, Sold By, Seller, From, Consignor, Company, Vendor, Vendor Name, Party Name, Party, Manufacturer, Distributor, Dealer, Trader, Firm Name, Business Name, Billed By, Dispatched By, Shipped By, Forwarded By, Agent, Broker, Mill Name, Factory Name, Source, Principal, Exporter, Importer, Proprietor, Organization, Entity Name, Supplier / Vendor, Name of Supplier, Name of Seller, Creditor, Remitter, Issuer...

▸ Supplier GST: GST No, GSTIN, GST Number, Tax ID, TIN No, TIN, VAT No, VAT Number, Service Tax No, CIN No, PAN No, PAN Number, GSTIN of Supplier, Seller GSTIN, Vendor GSTIN, Party GSTIN, Tax Registration No, Tax Reg No, GST Reg No, GST Registration Number, CST No, LST No, Excise No, IEC Code, FSSAI No, Import Export Code, Udyam No, MSME No...

▸ Vehicle Number: Vehicle No, Veh. No, Truck No, RC No, Registration Number, Reg No, Transport No, Lorry No, Lorry Number, Truck Number, Tempo No, Vehicle Registration, Vehicle Reg No, Tractor No, Tanker No, Container No, Fleet No, Conveyance No, Carrier No, Car No, Auto No, Van No, LCV No, HCV No, Transport Vehicle No...

▸ Transport / Freight: Freight, Transport Charges, Cartage, Delivery Charges, Freight Charges, Carriage, Carriage Inward, Carriage Outward, Forwarding Charges, Handling Charges, Loading Charges, Unloading Charges, Logistics Charges, Shipping Charges, Courier Charges, Packing & Forwarding, P&F Charges, Octroi, Entry Tax, Toll Charges, Transit Charges, Conveyance Charges, Drayage, Porterage, Haulage, Godown Charges, Demurrage, Transportation Cost, Freight & Cartage, LR Charges, Dispatch Charges...

▸ Product Code: Product Code, Item Code, Part No, SKU, HSN, Article No, Model No, Part Number, Item No, Item Number, Material Code, Material No, Cat No, Catalogue No, Catalogue Number, Stock Code, Stock No, Reference Code, Ref Code, Product ID, Item ID, BOM Code, Component Code, HSN Code, SAC Code, HSN/SAC, UPC Code, EAN Code, ASIN, Internal Code, System Code, Drawing No, Specification No, Grade Code, Variant Code...

▸ Product Name: Description, Description of Goods, Product Name, Material, Item Description, Item Name, Product Description, Goods Description, Particulars, Commodity, Article, Material Description, Name of Product, Name of Goods, Name of Item, Material Name, Commodity Name, Subject, Nature of Goods, Details, Specification, Product Details, Item Particulars, Goods, Stock Item, Service Description, Nature of Supply, Category, Product Title, Brand Name, Short Description, Full Description...

▸ Quantity Received: Quantity, Qty, Qty Received, Received Qty, Nos, Pcs, Pieces, Count, Number, No. of Units, Units Received, Total Qty, Dispatched Qty, Shipped Qty, Delivered Qty, Accepted Qty, Inspected Qty, Actual Qty, GRN Qty, Inward Qty, Received Quantity, Net Qty, Gross Qty, Billed Qty, Ordered Qty, Supply Qty, Qty Supplied, Qty Accepted, Qty Delivered, Volume, Amount of Goods...

▸ Unit: UOM, Unit, Measure, MT, TO, T, KG, PCS, NOS, BAG, G, GM, LTR, L, ML, CFT, CBM, SQM, SQFT, RMT, RM, SET, PAIR, BOX, CTN, ROLL, DRUM, CAN, BUNDLE, SHEET, PLATE, MTR, FT, INCH, MM, CM, TON, QUINTAL, QTL, PACKET, PKT, POUCH, UNIT, NUMBER, GROSS, DOZEN, DZ, CASE, PALLET, SLAB, COIL, BAR, ROD, PIPE, LENGTH, EACH, EA, PC...

▸ Number of Bags: No. of Bags, Bags, Bag Count, Packs, Cartons, No. of Packs, Pack Count, No. of Cartons, Carton Count, No. of Bundles, Bundles, No. of Boxes, Boxes, No. of Drums, Drums, No. of Packets, Packets, No. of Rolls, Rolls, No. of Cases, Cases, No. of Pallets, Pallets, No. of Pieces, Bundle Count, Box Count, Sacks, No. of Sacks, Pouches, No. of Pouches, Lots, No. of Lots, Containers, Crates, No. of Crates, Bales, No. of Bales, Cans, Tins, Nos of Pkg, Pkg Count, Total Packages, Total Packs...

▸ Rate per Unit: Rate, Price, Unit Price, Price/UOM, Basic Rate, Rate per Kg, Rate per MT, Rate per Bag, Rate per Piece, Rate per Unit, Rate per Ltr, Unit Rate, Base Price, Basic Price, List Price, MRP, Selling Price, Purchase Price, Cost Price, Per Unit Cost, Price per Unit, Price per Piece, Price per Kg, Ex-Factory Rate, Ex-Works Rate, Landing Rate, Net Rate, Agreed Rate, Contract Rate, Standard Rate, Market Rate, Quote Rate, Quoted Price, Offer Rate, Rate (Excl. Tax), Rate (Incl. Tax)...

▸ Total Amount: Amount, Taxable Amount, Taxable Value, Sub Total, Value, Amount before Tax, Basic Amount, Gross Amount, Pre-Tax Amount, Assessable Value, Taxable Base, Total Value, Chargeable Amount, Billed Amount, Invoice Amount, Merchandise Value, Commodity Value, Goods Value, Product Value, Line Total, Line Amount, Net Value, Subtotal, Total Before Tax, Total Before GST, Amount (Excl. GST), Ex-Tax Amount, Before Tax Total, Tax Base Amount, Material Value, Supply Value...

▸ Taxes: List all individual tax components (IGST, CGST, SGST, Cess) separately. 
  Example: [{"label": "CGST", "amount": 12.50}, {"label": "SGST", "amount": 12.50}]

▸ Remarks: Remarks, Notes, Handwritten or stamped annotations, Comments, Observations, Narration, Description, Additional Notes, Special Instructions, Note, Memo, Annotation, Instructions, Terms, Conditions, Terms & Conditions, Delivery Terms, Payment Terms, Special Notes, Additional Comments, Internal Notes, Buyer Notes, Seller Notes, Dispatch Remarks, Quality Remarks, Inspection Notes, QC Remarks, Return Remarks, Rejection Remarks, Shortage Remarks, Damage Remarks, Excess Remarks, GRN Remarks, PO Remarks, Adjustment Notes, Disclaimer, Declaration, Clarification, Free Text, Other Details, Extra Info, Addendum...

═══════════════════════════════════════
EXTRACTION RULES
═══════════════════════════════════════
1. NO FINAL AMOUNT EXTRACTION: Do not return a Final Amount field in the JSON. Extract Total Amount and individual Taxes only.
2. SCAN THE ENTIRE DOCUMENT: Taxes are often listed at the line item level OR in the summary section.
3. CLEAN NUMBERS: Strip currency symbols (₹, Rs) and remove commas. (e.g. "1,234.00" -> 1234.00).
4. DATE NORMALISATION: Convert to YYYY-MM-DD.
"""

ocr_service = OCRService()
