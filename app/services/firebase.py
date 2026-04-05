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
        # File-based initialization (source of truth for this deployment)
        service_account_path = os.getenv("SERVICE_ACCOUNT_KEY", "serviceAccountKey.json")
        
        # In Docker/Railway, the file is usually in /app/serviceAccountKey.json
        # If running from app/, check one level up
        if not os.path.exists(service_account_path) and os.path.exists("../" + service_account_path):
            service_account_path = "../" + service_account_path

        if os.path.exists(service_account_path):
            try:
                cred = credentials.Certificate(service_account_path)
                firebase_admin.initialize_app(cred)
                print(f"Firebase initialized successfully from file: {service_account_path}")
                return firestore.client()
            except Exception as e:
                print(f"Error initializing Firebase from file: {e}")
                raise e
        
        # Fallback error if file is missing
        error_msg = (
            f"FIREBASE AUTH FAILED: '{service_account_path}' not found! "
            "Please ensure the file is committed to GitHub and present in the app root."
        )
        print(f"FATAL ERROR: {error_msg}")
        raise ValueError(error_msg)

    return firestore.client()


db = initialize_firebase()
