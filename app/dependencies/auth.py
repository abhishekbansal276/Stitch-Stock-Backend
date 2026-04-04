from fastapi import Header, HTTPException, Depends
from firebase_admin import auth
from app.services.firebase import db

# Role lookup from Firestore
def get_current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    token = authorization.split("Bearer ")[1]
    
    try:
        # Verify the ID token from Flutter
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token['uid']
        email = decoded_token['email']
        
        # Get user details from Firestore
        user_doc = db.collection('users').document(email).get()
        if not user_doc.exists:
            # Maybe the user was just created in auth but not in Firestore yet
            # Or the user was deleted from Firestore but not Auth
            raise HTTPException(status_code=403, detail="User details not found in Firestore")
        
        user_data = user_doc.to_dict()
        user_data['uid'] = uid
        return user_data
        
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Firebase ID Token")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Authentication error: {str(e)}")

def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user
