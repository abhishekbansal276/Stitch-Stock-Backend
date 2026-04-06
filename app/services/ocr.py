import os
import json
import re
from typing import Dict, List

# ── FIXED: Migrated from deprecated google.generativeai → google.genai ────────
from google import genai
from google.genai import types
# ──────────────────────────────────────────────────────────────────────────────


class OCRService:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
            # ELITE FIX: Always use the latest high-performance model. 
            self.model_name = self._pick_model()
            print(f"OCRService: Initialized with model '{self.model_name}'")
        else:
            print("WARNING: GEMINI_API_KEY not found. OCRService running in MOCK mode.")
            self.client = None

    def _pick_model(self) -> str:
        preferred = [
            "gemini-2.0-flash",
            "gemini-2.0-flash-exp",
            "gemini-1.5-flash-latest",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]
        try:
            # The new SDK list() returns model objects with name like 'models/gemini-2.0-flash'
            available_models = self.client.models.list()
            available_names = {m.name.replace("models/", "") for m in available_models}
            
            for p in preferred:
                if p in available_names:
                    return p
        except Exception as e:
            print(f"WARNING: Could not list models: {e}. Defaulting to gemini-2.0-flash")
        return "gemini-2.0-flash"

    # ─────────────────────────────────────────────────────────────────────────

    def extract_from_file(self, content: bytes, filename: str,
                          existing_headers: List[str] = None) -> Dict:
        if not self.client:
            raise Exception("Gemini API key not found. Extraction is disabled.")

        ext = filename.rsplit(".", 1)[-1].lower()
        mime_map = {"pdf": "application/pdf", "png": "image/png",
                    "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/jpeg")
        print(f"OCRService: Processing '{filename}' (Mime: {mime_type}, Size: {len(content)} bytes)")

        try:
            print(f"OCRService: Requesting Gemini extraction [Model: {self.model_name}]...")
            import time
            start_time = time.time()
            
            response = self.client.models.generate_content(
                model=self.model_name,
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

            print(f"OCRService: Gemini API responded in {time.time() - start_time:.2f}s")
            raw = response.text.strip()
            # Strip markdown fences if the model ignores response_mime_type
            raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"```$", "", raw).strip()
            # Grab outermost JSON object
            s, e = raw.find("{"), raw.rfind("}")
            if s != -1 and e != -1:
                raw = raw[s : e + 1]

            print("OCRService: Parsing raw JSON output...")
            data = json.loads(raw)
            data["items"] = self._consolidate(data.get("items", []))
            print(f"OCRService: Extraction complete. Found {len(data['items'])} line items.")
            return data

        except Exception as e:
            print(f"Extraction Error: {e}")
            raise Exception(f"Failed to extract info: {str(e)}")

    # ── CONSOLIDATION ─────────────────────────────────────────────────────────
    # Merge rows that share the same Product Code (e.g. multi-batch invoices)

    def _consolidate(self, items: List[Dict]) -> List[Dict]:
        merged: Dict[str, Dict] = {}
        for item in items:
            key = str(item.get("Product Code") or "UNKNOWN").strip()
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
                base["Taxes (IGST/CGST/SGST)"] = self._sum_str(
                    base.get("Taxes (IGST/CGST/SGST)"), item.get("Taxes (IGST/CGST/SGST)"))
                # Keep the more specific unit
                if not base.get("Unit") or base.get("Unit") == "PCS":
                    base["Unit"] = item.get("Unit") or base.get("Unit")
                # Accumulate batch numbers
                if item.get("Batch Number") and item["Batch Number"] not in str(base.get("Batch Number", "")):
                    base["Batch Number"] = f"{base.get('Batch Number', '')}, {item['Batch Number']}".strip(", ")
            else:
                merged[key] = dict(item)
        return list(merged.values())

    # ── HELPERS ───────────────────────────────────────────────────────────────

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

    # ── MOCK ──────────────────────────────────────────────────────────────────



# ── EXTRACTION PROMPT ─────────────────────────────────────────────────────────
# Designed to handle maximum variability across Indian supplier invoices:
# GAIL, Reliance, HPCL, BPCL, traders, transporters, hand-stamped bills, etc.

EXTRACTION_PROMPT = """
You are an elite Inventory Auditor AI trained on thousands of Indian supplier invoices,
delivery challans, tax invoices, and purchase orders. Your job is to extract data with
100% accuracy regardless of how the bill is formatted, labelled, or printed.

═══════════════════════════════════════════════════════════════════
OUTPUT — Return ONLY this JSON. No explanation. No markdown fences.
═══════════════════════════════════════════════════════════════════
{
  "header": {
    "Date": "YYYY-MM-DD or original format if unparseable",
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
      "Taxes (IGST/CGST/SGST)": "...",
      "Final Amount": 0.0,
      "Remarks": "..."
    }
  ]
}

Use null for any field that is genuinely absent. Numbers must be numeric (not strings).

═══════════════════════════════════════════════
FIELD MAPPING — accept ANY of these label aliases
═══════════════════════════════════════════════

▸ Date
  → Date, Dated, Invoice Date, Bill Date, Date of Issue, Doc Date,
    Dispatch Date, Delivery Date, Challan Date, Tax Invoice Date,
    Date of Supply, Transaction Date, Order Date, YYYYMMDD stamps

▸ Invoice Number
  → Invoice No., Invoice No, Invoice #, Bill No., Bill Number,
    Serial No., Serial Number, Ref No., Ref. No., Reference Number,
    Challan No., Delivery Order No., D.O. No., DO No, PO No.,
    Document No., Doc No., Tax Invoice No., Voucher No.,
    Consignment No., LR No., GR No., Waybill No.,
    any alphanumeric string in header that looks like an ID (e.g. UPG3A…)

▸ Supplier Name
  → Supplier, Supplier Name, Sold By, Seller, From, Consignor,
    Manufacturer, Company, Party Name, Vendor Name, Shipper,
    the largest bold company name printed at the top of the document

▸ Supplier GST
  → GST No., GSTIN, GST Number, GST Reg. No., Tax ID,
    TIN No., VAT No., CIN, PAN (if GST absent),
    look inside the Supplier / Seller address block

▸ Vehicle Number
  → Vehicle No., Veh. No., Vehicle Reg. No., Vehicle Registration,
    Motor Vehicle No., Truck No., Transport Vehicle, LR Vehicle,
    Lorry No., Registration Number, RC No.

▸ Transporter Name
  → Transporter, Transporter Name, Carried by, Transport Co.,
    Logistics, Carrier, Shipping Agent, Courier, Transport Party,
    look in Mode of Transport / Transport Details section

▸ Transport / Freight
  → Freight, Freight Charges, Transport Charges, Cartage,
    Delivery Charges, Handling Charges, Logistics Charges,
    look in the invoice footer / charges summary table

▸ Product Code
  → Product Code, Item Code, Material Code, Part No., Part Number,
    SKU, HSN Code, SAC Code, UPC, Catalogue No., Article No.,
    Item No., Stock Code, Model No., Product ID

▸ Product Name
  → Description, Description of Goods, Product Name, Item Name,
    Item Description, Material Description, Particulars,
    Product, Commodity, Goods Description, Article, Item,
    Name of Goods, Product Details

▸ Model Name
  → Model Name, Model, Model No., Model Number, mname, 
    Type, Variant, Version, Specification

▸ Batch Number
  → Batch No., Batch Number, Lot No., Lot Number, Serial Batch,
    Batch/Lot, Batch ID, Manufacturing Batch, Production Batch,
    Mfg. Batch, Batch Code

▸ Quantity Received
  → Quantity, Qty, Qty Received, Received Qty, Quantity Received,
    No. of Units, Nos., Pieces, Count,
    PREFERENCE ORDER: use Metric Tons (MT/TO/T) > Kgs > Bags > Pcs
    If multiple quantity columns exist, pick the primary dispatch quantity.

▸ Unit
  → UOM, UOM*, Unit, Units, Measure, Measurement Unit,
    common values: MT, TO, T, KG, KGS, PCS, NOS, BAG, BAGS, LTR, L, M, SQM
    Normalise: "Metric Tons" → MT, "Kilograms" → KG, "Pieces" → PCS

▸ Number of Bags
  → No. of Bags, Bags, No. Bags, Bag Count, Packing Qty,
    Bags/Packets, Packs, Cartons, Bundles, Cases

▸ Rate per Unit
  → Rate, Rate/Unit, Price, Unit Price, Price/UOM, Rate per MT,
    Basic Rate, Basic Price, MRP, Per Unit Cost, Rate Rs/UOM

▸ Total Amount
  → Amount, Total, Taxable Amount, Taxable Value, Basic Amount,
    Line Total, Gross Amount, Sub Total, Value, Assessable Value,
    Amount before Tax, Net Amount (before tax)

▸ Taxes (IGST/CGST/SGST)
  → IGST, CGST, SGST, UTGST, GST, Tax, VAT, Cess, Surcharge,
    Tax Amount, GST Amount, Total Tax
  → FORMAT: sum all tax components into one number, then note type.
    Example: "271242.22 (IGST)" or "12000 (CGST 6% + SGST 6%)"

▸ Final Amount
  → Final Amount, Grand Total, Total Amount, Invoice Value,
    Invoice Total, Net Payable, Amount Payable, Net Amount,
    Total (Rounded), Round Off Total, Total Invoice Value,
    Total Value, Balance Due

▸ Remarks
  → Remarks, Notes, Comments, Special Instructions, Narration,
    any handwritten or stamped annotation not captured elsewhere

═══════════════════════════════════════
EXTRACTION RULES — follow in strict order
═══════════════════════════════════════

1. SCAN THE ENTIRE DOCUMENT before starting extraction.
   Do not stop at the first table — many invoices have continuation pages.

2. HEADER SECTION: Usually top 20% of document.
   Look for the supplier box, buyer box, invoice metadata box.
   GST numbers follow pattern: 2 digits + 5 letters + 4 digits + 1 letter + 1 digit + Z + 1 char.

3. LINE ITEMS TABLE: Extract EVERY row. Do not skip any.
   - Rows may span across pages — treat them as one continuous table.
   - Ignore pure sub-total or tax rows (capture their values in header/taxes fields instead).
   - If a row has no Product Code, use a generated key like "ITEM-1", "ITEM-2".

4. FOOTER SECTION: Bottom 15% — find Grand Total, Freight, Tax summary here.
   These override any mid-table values.

5. MULTI-BATCH INVOICES: If the same product appears on multiple rows with
   different batch numbers, keep them as separate items (consolidation happens externally).

6. AMBIGUOUS QUANTITIES: If both "Bags" and "MT" columns exist, put MT in
   Quantity Received and Bags count in Number of Bags.

7. HANDWRITTEN STAMPS: Treat stamped text as valid data if it represents
   structured fields (e.g. "Vehicle: MH12AB1234" stamped in red).
   Ignore decorative stamps (company seals, paid/received stamps).

8. AMOUNT EXTRACTION: Strip currency symbols (₹, Rs., INR, $).
   Remove commas from numbers. Convert lakh/crore notation if present.
   Example: "12,34,567.89" → 1234567.89

9. DATE NORMALISATION: Convert to YYYY-MM-DD if possible.
   "08/03/2026" → "2026-03-08", "8-Mar-26" → "2026-03-08"
   If format is truly ambiguous, return the original string.

10. CONFIDENCE: If a field value is partially legible or uncertain,
    extract your best reading and append " [?]" to that value only.
    Do not omit uncertain fields — a best-effort value is more useful than null.
"""

ocr_service = OCRService()
