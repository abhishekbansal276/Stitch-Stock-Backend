import os
import base64
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()


def initialize_firebase():
    """
    Initialize Firebase Admin SDK and return Firestore client.
    Priority:
      1. FIREBASE_SERVICE_ACCOUNT_JSON - Plain JSON string in env var (Best for Cloud)
      2. FIREBASE_SERVICE_ACCOUNT_B64  - Base64 encoded JSON string
      3. SERVICE_ACCOUNT_KEY - path to a local .json file (Local Fallback)
    """
    """Build the Firestore client with JSON-First priority."""
    if not firebase_admin._apps:
        # 1. Plain JSON env var (PRIMARY as requested by user)
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

        # 2. File-based fallback (Repository uploaded)
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

        # 3. Base64 fallback (Legacy)
        sa_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if sa_b64:
            try:
                decoded = base64.b64decode(sa_b64).decode("utf-8")
                cred_dict = json.loads(decoded)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                print("Firebase: Initialized from FIREBASE_SERVICE_ACCOUNT_B64")
                return firestore.client()
            except Exception as e:
                print(f"Firebase: Failed to parse FIREBASE_SERVICE_ACCOUNT_B64: {e}")
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
