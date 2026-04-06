import os
import base64
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# Base directory for the project (up to 'backend/' folder)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

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


def initialize_firebase():
    """Build the Firestore client using private key credentials."""
    if not firebase_admin._apps:
        # 1. File-based priority (Repository uploaded)
        sa_file = os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
        abs_sa_path = BASE_DIR / sa_file
        if abs_sa_path.exists():
            try:
                cred = credentials.Certificate(str(abs_sa_path))
                firebase_admin.initialize_app(cred)
                print(f"Firebase: Initialized from absolute repository file: {abs_sa_path}")
                return firestore.client()
            except Exception as e:
                print(f"Firebase: Repository file check failed ({abs_sa_path}): {e}")

        # 2. Plain JSON env var
        sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if sa_json:
            try:
                cred_dict = json.loads(sa_json)
                if "private_key" in cred_dict:
                    cred_dict["private_key"] = clean_private_key(cred_dict["private_key"])
                
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                print("Firebase: Initialized from FIREBASE_SERVICE_ACCOUNT_JSON")
                return firestore.client()
            except Exception as e:
                print(f"Firebase: Failed to parse FIREBASE_SERVICE_ACCOUNT_JSON: {e}")

        # 3. Base64 fallback (Legacy)
        sa_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if sa_b64:
            try:
                decoded = base64.b64decode(sa_b64).decode("utf-8")
                cred_dict = json.loads(decoded)
                if "private_key" in cred_dict:
                    cred_dict["private_key"] = clean_private_key(cred_dict["private_key"])
                    print(f"Firebase: B64 Private key verified (len={len(cred_dict['private_key'])})")

                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                print("Firebase: Initialized from FIREBASE_SERVICE_ACCOUNT_B64")
                return firestore.client()
            except Exception as e:
                print(f"Firebase: Failed to parse FIREBASE_SERVICE_ACCOUNT_B64: {e}")

        # 3. File fallback (Absolute Resolve)
        sa_file = os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
        abs_sa_path = BASE_DIR / sa_file
        if abs_sa_path.exists():
            try:
                cred = credentials.Certificate(str(abs_sa_path))
                firebase_admin.initialize_app(cred)
                print(f"Firebase: Initialized from absolute file: {abs_sa_path}")
                return firestore.client()
            except Exception as e:
                print(f"Firebase: Failed to read from file {abs_sa_path}: {e}")
        
        # FINAL FALLBACK (e.g. for CI/CD or local without key)
        try:
            firebase_admin.initialize_app()
            print("Firebase: Initialized with default Application Credentials")
            return firestore.client()
        except Exception as e:
            print(f"CRITICAL ERROR: Firebase could not be initialized: {e}")
            raise e

    return firestore.client()


db = initialize_firebase()
