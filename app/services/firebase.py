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


def get_service_account_info():
    """
    Triage and return service account info as a dict from environment variables or file.
    Priority: JSON Env Var > Base64 Env Var > Explicit File Path Env Var.
    """
    # 1. Plain JSON string (Primary)
    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        try:
            info = json.loads(sa_json)
            if "private_key" in info:
                info["private_key"] = clean_private_key(info["private_key"])
            print("CredentialProvider: Using FIREBASE_SERVICE_ACCOUNT_JSON")
            return info
        except Exception as e:
            print(f"CredentialProvider: Error parsing FIREBASE_SERVICE_ACCOUNT_JSON: {e}")

    # 2. Base64 encoded string
    sa_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
    if sa_b64:
        try:
            decoded = base64.b64decode(sa_b64).decode("utf-8")
            info = json.loads(decoded)
            if "private_key" in info:
                info["private_key"] = clean_private_key(info["private_key"])
            print("CredentialProvider: Using FIREBASE_SERVICE_ACCOUNT_B64")
            return info
        except Exception as e:
            print(f"CredentialProvider: Error parsing FIREBASE_SERVICE_ACCOUNT_B64: {e}")

    # 3. Explicit File Path (Optional, for local dev only)
    sa_file = os.getenv("SERVICE_ACCOUNT_FILE")
    if sa_file:
        path = BASE_DIR / sa_file if not Path(sa_file).is_absolute() else Path(sa_file)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                    if "private_key" in info:
                        info["private_key"] = clean_private_key(info["private_key"])
                    print(f"CredentialProvider: Using explicit file {path}")
                    return info
            except Exception as e:
                print(f"CredentialProvider: Error reading {path}: {e}")
    
    return None


def initialize_firebase():
    """Build the Firestore client with modern credential triaging."""
    if not firebase_admin._apps:
        info = get_service_account_info()
        
        if info:
            try:
                cred = credentials.Certificate(info)
                firebase_admin.initialize_app(cred)
                return firestore.client()
            except Exception as e:
                print(f"Firebase: Failed to initialize with provided info: {e}")

        # FINAL FALLBACK (e.g. for CI/CD or Cloud Run Service Accounts)
        try:
            firebase_admin.initialize_app()
            print("Firebase: Initialized with default Application Credentials")
            return firestore.client()
        except Exception as e:
            print(f"CRITICAL ERROR: Firebase could not be initialized: {e}")
            raise e

    return firestore.client()


db = initialize_firebase()
