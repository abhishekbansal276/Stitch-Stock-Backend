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
    if not firebase_admin._apps:
        # 1. Plain JSON string from Env
        sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if sa_json:
            try:
                cred_dict = json.loads(sa_json)
                # ELITE FIX: Handle literal \n in private_key if passed from env var
                if "private_key" in cred_dict:
                    cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
                
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
