import os
import base64
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()


def initialize_firebase():
    """Initialize Firebase Admin SDK and return Firestore client."""
    if not firebase_admin._apps:
        # 1. Try Base64 string from environment (for Cloud/Railway)
        b64_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if b64_key:
            try:
                # Clean the Base64 string (remove whitespace/newlines from copy-paste)
                b64_key = "".join(b64_key.split())
                
                # Fix padding if necessary
                padding = len(b64_key) % 4
                if padding > 0:
                    b64_key += "=" * (4 - padding)
                    
                decoded_key = base64.b64decode(b64_key).decode("utf-8")
                cred_dict = json.loads(decoded_key)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                print("Firebase initialized successfully from Base64 env var.")
                return firestore.client()
            except Exception as e:
                print(f"Error loading Base64 Firebase key: {e}")

        # 2. Try physical file (Fallback or Local Dev)
        service_account_path = os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
        if os.path.exists(service_account_path):
            try:
                cred = credentials.Certificate(service_account_path)
                firebase_admin.initialize_app(cred)
                print(f"Firebase initialized from file: {service_account_path}")
                return firestore.client()
            except Exception as e:
                print(f"Error initializing from file: {e}")

        # 3. Fatal error if no credentials work
        error_msg = (
            "FIREBASE AUTH FAILED: No valid credentials found! "
            "Please check 'FIREBASE_SERVICE_ACCOUNT_B64' in your cloud dashboard "
            "or ensure 'serviceAccountKey.json' exists in the backend directory."
        )
        print(f"FATAL ERROR: {error_msg}")
        raise ValueError(error_msg)

    return firestore.client()


db = initialize_firebase()
