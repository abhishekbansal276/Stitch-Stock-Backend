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
        # 1. Try Base64 string from environment (for Render/Cloud)
        b64_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
        if b64_key:
            try:
                decoded_key = base64.b64decode(b64_key).decode("utf-8")
                cred_dict = json.loads(decoded_key)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                print("Firebase initialized from Base64 env var.")
                return firestore.client()
            except Exception as e:
                print(f"Error loading Base64 Firebase key: {e}")

        # 2. Try physical file (for Local Development)
        service_account_path = os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
        if os.path.exists(service_account_path):
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            print(f"Firebase initialized from file: {service_account_path}")
        else:
            # 3. Fallback to default (GCP environment)
            firebase_admin.initialize_app()
            print("Firebase initialized with default credentials.")

    return firestore.client()


db = initialize_firebase()
