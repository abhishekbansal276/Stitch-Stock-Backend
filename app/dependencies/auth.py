from fastapi import Header, HTTPException, Depends
from firebase_admin import auth
from app.services.firebase import db


def get_current_user(authorization: str = Header(...)):
    """
    Verify the Firebase ID token from the Authorization header
    and fetch the user's role/profile from Firestore.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = authorization.split("Bearer ", 1)[1]

    try:
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token.get("uid")
        email = decoded_token.get("email")

        if not email:
            raise HTTPException(status_code=401, detail="Token missing email claim")

        # Fetch user profile from Firestore
        user_doc = db.collection("users").document(email).get()
        if not user_doc.exists:
            raise HTTPException(
                status_code=403,
                detail="User profile not found in Firestore. Contact admin.",
            )

        user_data = user_doc.to_dict()
        user_data["uid"] = uid
        user_data["email"] = email  # Ensure email is always present
        
        if not user_data.get("is_active", True):
            raise HTTPException(
                status_code=403,
                detail="Your account has been deactivated. Contact admin.",
            )
            
        return user_data

    except HTTPException:
        raise
    except auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase ID token")
    except auth.ExpiredIdTokenError:
        raise HTTPException(status_code=401, detail="Firebase ID token has expired")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Authentication error: {str(e)}")


def require_admin(user: dict = Depends(get_current_user)):
    """Dependency that ensures the authenticated user has admin role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user
