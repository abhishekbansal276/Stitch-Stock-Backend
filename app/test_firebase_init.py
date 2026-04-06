import os
import json
import base64
from dotenv import load_dotenv

load_dotenv()

def clean_private_key(pk: str) -> str:
    """Helper to format the private key to the exact PKCS#8 standard (64 chars per line)."""
    if not pk:
        return ""
    
    # 1. Normalize all forms of escaping
    pk = pk.replace("\\n", "\n").replace('"', "").replace("'", "").strip()
    
    # 2. Extract the core key body
    if "-----BEGIN PRIVATE KEY-----" in pk and "-----END PRIVATE KEY-----" in pk:
        body = pk.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "").strip()
        # Clean out ALL existing whitespace/newlines to handle mangled formats
        clean_body = "".join(body.split())
        
        # 3. Reshape into 64-character lines (PKCS#8 standard)
        lines = [clean_body[i:i+64] for i in range(0, len(clean_body), 64)]
        
        reconstructed = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"
        return reconstructed
        
    return pk.strip()

def test_credentials():
    print("--- Firebase Diagnostic Report ---")
    
    # 1. Test JSON Env
    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        print("\n[CHECK] Found FIREBASE_SERVICE_ACCOUNT_JSON")
        try:
            cred_dict = json.loads(sa_json)
            pk = cred_dict.get("private_key", "")
            cleaned_pk = clean_private_key(pk)
            
            print(f"  - Parsed JSON: Success")
            print(f"  - Original PK Length: {len(pk)}")
            print(f"  - Cleaned PK Length: {len(cleaned_pk)}")
            print(f"  - PK starts with: {cleaned_pk[:30]}...")
            print(f"  - PK ends with: ...{cleaned_pk[-30:]}")
            
            if "-----BEGIN PRIVATE KEY-----" not in cleaned_pk:
                print("  - ERROR: Missing BEGIN PRIVATE KEY header!")
            if "-----END PRIVATE KEY-----" not in cleaned_pk:
                print("  - ERROR: Missing END PRIVATE KEY footer!")
        except Exception as e:
            print(f"  - ERROR: Failed to parse JSON: {e}")
    else:
        print("\n[SKIP] FIREBASE_SERVICE_ACCOUNT_JSON not set.")

    # 2. Test B64 Env
    sa_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
    if sa_b64:
        print("\n[CHECK] Found FIREBASE_SERVICE_ACCOUNT_B64")
        try:
            decoded = base64.b64decode(sa_b64).decode("utf-8")
            cred_dict = json.loads(decoded)
            pk = cred_dict.get("private_key", "")
            cleaned_pk = clean_private_key(pk)
            
            print(f"  - Decoded Base64: Success")
            print(f"  - PK Length: {len(cleaned_pk)}")
            print(f"  - PK starts with: {cleaned_pk[:30]}...")
            
            if "-----BEGIN PRIVATE KEY-----" not in cleaned_pk:
                print("  - ERROR: Missing header in B64 decoded content!")
        except Exception as e:
            print(f"  - ERROR: Failed to decode/parse B64: {e}")
    else:
        print("\n[SKIP] FIREBASE_SERVICE_ACCOUNT_B64 not set.")

if __name__ == "__main__":
    test_credentials()
