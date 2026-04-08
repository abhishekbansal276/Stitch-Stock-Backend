import hashlib
import re

def normalize_id(val) -> str:
    """
    Cleans an ID (product code, batch, etc.) to a standardized alphanumeric format.
    Specifically handles Google Sheets' tendency to add .0 to numeric strings.
    """
    if val is None:
        return ""
    
    # 1. Standardize string type and case
    s = str(val).strip().upper()
    
    # 2. Handle trailing .0 (common spreadsheet artifact)
    if s.endswith('.0'):
        s = s[:-2]
    
    # 3. Final alphanumeric pass
    return re.sub(r'[^A-Z0-9]', '', s)

def generate_12_digit_hash(seed: str) -> str:
    """
    Generates a deterministic 12-digit numeric string from a given seed string.
    Uses SHA-256 for collision resistance and modulo 10^12 for exact numbering.
    """
    if not seed:
        return "000000000000"
        
    # Standardize seed using the same logic as the rest of the system
    clean_seed = normalize_id(seed)
    
    # Generate SHA-256 hash
    hash_object = hashlib.sha256(clean_seed.encode())
    # Convert hex to integer
    numeric_hash = int(hash_object.hexdigest(), 16)
    
    # Get 12 digits (modulo 10^12) and zero-pad
    return str(numeric_hash % 10**12).zfill(12)