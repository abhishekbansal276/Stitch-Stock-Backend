import hashlib
import re

def generate_12_digit_hash(seed: str) -> str:
    """
    Generates a deterministic 12-digit numeric string from a given seed string.
    Uses SHA-256 for collision resistance and modulo 10^12 for exact numbering.
    """
    if not seed:
        return "000000000000"
        
    # Standardize seed: uppercase and alphanumeric only
    clean_seed = re.sub(r'[^A-Z0-9]', '', str(seed).upper())
    
    # Generate SHA-256 hash
    hash_object = hashlib.sha256(clean_seed.encode())
    # Convert hex to integer
    numeric_hash = int(hash_object.hexdigest(), 16)
    
    # Get 12 digits (modulo 10^12) and zero-pad
    return str(numeric_hash % 10**12).zfill(12)
