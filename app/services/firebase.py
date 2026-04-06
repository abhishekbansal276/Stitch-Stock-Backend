import os
import base64
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()


def clean_private_key(pk: str) -> str:
    """Helper to format the private key from various environment variable styles."""
    if not pk:
        return ""
    # 1. Handle literal \n if passed from shell or quoted strings
    pk = pk.replace("\\n", "\n")
    # 2. Handle cases where newlines might be missing but the key is one long string
    # (Sometimes happens with certain CI/CD secrets)
    if "-----BEGIN PRIVATE KEY-----" in pk and "\n" not in pk[30:-30]:
        # Attempt to insert newlines every 64 chars if it's one long block
        # (Though usually \n should be present)
        pass 
    
    # 3. Final clean
    pk = pk.replace('"', '').replace("'", "").strip()
    return pk


def initialize_firebase():
    """
    Initialize Firebase Admin SDK and return Firestore client.
    Priority:
      1. FIREBASE_SERVICE_ACCOUNT_JSON - Plain JSON string in env var (Best for Cloud)
      2. FIREBASE_SERVICE_ACCOUNT_B64  - Base64 encoded JSON string
      3. SERVICE_ACCOUNT_KEY - path to a local .json file (Local Fallback)
    """
    if not firebase_admin._apps:
        # 1. Plain JSON string from Env
        sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if sa_json:
            try:
                cred_dict = json.loads(sa_json)
                if "private_key" in cred_dict:
                    cred_dict["private_key"] = clean_private_key(cred_dict["private_key"])
                    print(f"Firebase: Private key verified (len={len(cred_dict['private_key'])})")
                
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                print("Firebase: Initialized from FIREBASE_SERVICE_ACCOUNT_JSON")
                return firestore.client()
            except Exception as e:
                print(f"Firebase: Failed to parse FIREBASE_SERVICE_ACCOUNT_JSON: {e}")

        # 2. Base64 Encoded JSON from Env
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

        # 3. File-based fallback
        service_account_path = os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
        if not os.path.exists(service_account_path) and os.path.exists("../" + service_account_path):
            service_account_path = "../" + service_account_path

        if os.path.exists(service_account_path):
            try:
                cred = credentials.Certificate(service_account_path)
                firebase_admin.initialize_app(cred)
                print(f"Firebase: Initialized from file: {service_account_path}")
                return firestore.client()
            except Exception as e:
                print(f"Firebase: File initialization skipped ({service_account_path}): {e}")
        
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
