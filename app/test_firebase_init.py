import os
import json
import base64
from dotenv import load_dotenv

load_dotenv()

def clean_private_key(pk: str) -> str:
    """Helper to properly formatting the private key from env vars."""
    if not pk:
        return ""
    # Handle literal \n if passed from shell
    pk = pk.replace("\\n", "\n")
    # Remove any extra quotes or whitespace
    pk = pk.strip("'").strip('"').strip()
    return pk

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
