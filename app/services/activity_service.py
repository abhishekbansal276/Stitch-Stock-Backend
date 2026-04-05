import time
from typing import Dict, Any, Optional
from app.services.firebase import db
from app.services.fcm_service import fcm_service

class ActivityService:
    def log_and_notify(self, user: Dict[str, Any], action_type: str, item_name: str, product_code: str, qty_change: float, location: str, description: str = ""):
        """Standardized Log & Alert Engine."""
        try:
            # 1. Create the Audit Log Entry
            user_name = user.get('full_name', user['email'])
            user_role = user.get('role', 'stock person')
            
            log_entry = {
                'item_name': item_name,
                'description': description,
                'product_code': product_code,
                'movement_type': action_type, # IN, OUT, TRANSFER
                'quantity_changed': qty_change,
                'location': location or "General Stock",
                'actor_name': user_name,
                'actor_email': user['email'],
                'actor_role': user_role,
                'created_at': int(time.time())
            }
            
            # 2. Persist to Firestore
            db.collection('activity_logs').add(log_entry)
            
            # 3. Trigger Notification (Only if 'stock person' performed the action)
            if user_role == 'stock person':
                title = f"📦 {action_type}: {item_name}"
                body = f"{user_name} moved {qty_change} of {product_code} in {location or 'Warehouse'}."
                if action_type == 'IN':
                    body = f"{user_name} added {qty_change} of {product_code} to {location or 'Warehouse'}."
                elif action_type == 'OUT':
                    body = f"{user_name} removed {qty_change} of {product_code} from {location or 'Warehouse'}."
                
                fcm_service.send_multicast_to_admins(
                    title=title,
                    body=body,
                    data={
                        "product_code": product_code,
                        "action": action_type,
                        "click_action": "FLUTTER_NOTIFICATION_CLICK"
                    }
                )
        except Exception as e:
            print(f"Activity Logging Error: {e}")

    def get_logs_paged(self, limit: int = 20, last_ts: Optional[int] = None, search: Optional[str] = None):
        """Fetches logs with lazy-loading and search support."""
        try:
            query = db.collection('activity_logs').order_by('created_at', direction='DESCENDING')
            
            if search:
                # Firestore limited search: start at prefix
                # We also pull all and search in Python if firestore lacks complex indexes
                # For better optimization at scale, we use 'product_code' field filter
                query = query.where('product_code', '>=', search).where('product_code', '<=', search + '\uf8ff')

            if last_ts:
                query = query.start_after({'created_at': last_ts})

            query = query.limit(limit)
            
            docs = query.stream()
            results = [doc.to_dict() for doc in docs]
            return results
        except Exception as e:
            print(f"Log Retrieval Error: {e}")
            return []

    def log_user_management_action(self, admin_email: str, action: str, target_email: str, details: str = ""):
        """Administrative Log for User Management."""
        try:
            log_entry = {
                'admin_email': admin_email,
                'action_type': action,
                'target_user': target_email,
                'details': details,
                'created_at': int(time.time())
            }
            db.collection('user_management_logs').add(log_entry)
        except Exception as e:
            print(f"User Audit Logging Error: {e}")

activity_service = ActivityService()
