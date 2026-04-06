from firebase_admin import messaging
from app.services.firebase import db
from google.cloud.firestore_v1.base_query import FieldFilter
from typing import List

class FcmService:
    def send_multicast_to_admins(self, title: str, body: str, data: dict = None):
        """Sends a push notification to all users with role 'admin'."""
        try:
            # 1. Fetch all admin FCM tokens
            admins_ref = db.collection('users').where(filter=FieldFilter('role', '==', 'admin')).stream()
            tokens = []
            for admin in admins_ref:
                admin_data = admin.to_dict()
                token = admin_data.get('fcm_token')
                if token:
                    tokens.append(token)

            if not tokens:
                print("No admin tokens found for notification.")
                return

            # 2. Construct the message
            message = messaging.MulticastMessage(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=data or {},
                tokens=tokens,
            )

            # 3. Send via Firebase Admin SDK
            response = messaging.send_multicast(message)
            print(f"Successfully sent {response.success_count} notifications to admins.")
            
            # 4. Optional: Log partial failures
            if response.failure_count > 0:
                responses = response.responses
                failed_tokens = []
                for idx, resp in enumerate(responses):
                    if not resp.success:
                        failed_tokens.append(tokens[idx])
                print(f"Failed tokens: {failed_tokens}")

        except Exception as e:
            print(f"FCM Multicast Error: {e}")

fcm_service = FcmService()
