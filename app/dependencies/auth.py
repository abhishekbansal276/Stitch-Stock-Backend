from fastapi import Header, HTTPException, Depends
from firebase_admin import auth
import time
from app.services.firebase import db


def get_current_user(
    authorization: str = Header(...),
    x_user_role: str = Header(None, alias="X-User-Role")
):
    """
    Verify the Firebase ID token and trust the X-User-Role header 
    (provided by the client after their initial login).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = authorization.split("Bearer ", 1)[1]

    try:
        print(f"Auth: Verifying token for user...")
        v_start = time.time()
        decoded_token = auth.verify_id_token(token)
        print(f"Auth: Token verified in {time.time() - v_start:.4f}s")
        
        uid = decoded_token.get("uid")
        email = decoded_token.get("email")

        if not email:
            print("Auth Error: Token missing email claim.")
            raise HTTPException(status_code=401, detail="Token missing email claim")

        # CLIENT-SIDE ROLE TRUST (Performance Opt)
        if not x_user_role:
            print(f"Auth Error: Missing X-User-Role header for {email}.")
            raise HTTPException(
                status_code=401,
                detail="Authentication Error: Missing user role. Please relogin."
            )

        # Simplified user data without Firestore fetch
        user_data = {
            "uid": uid,
            "email": email,
            "role": x_user_role,
            "is_active": True,
            "full_name": email.split('@')[0].capitalize() # Fallback name
        }
        
        return user_data

    except HTTPException:
        raise
    except auth.InvalidIdTokenError:
        print("Auth Error: Invalid or expired Firebase ID token.")
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase ID token")
    except auth.ExpiredIdTokenError:
        print("Auth Error: Firebase ID token has expired.")
        raise HTTPException(status_code=401, detail="Firebase ID token has expired")
    except Exception as e:
        import traceback
        error_type = type(e).__name__
        error_details = str(e)
        print(f"!!! AUTH ERROR [{error_type}]: {error_details}")
        traceback.print_exc()
        
        # Friendly error message for specific common issues
        if "Invalid JWT Signature" in error_details:
            msg = "Authentication Error: The server credentials (JWT) are invalid. Please check the FIREBASE_SERVICE_ACCOUNT_JSON."
        elif "Timeout" in error_details:
            msg = "Authentication Error: Connection to Firebase timed out. Please try again."
        else:
            msg = f"Authentication error: {error_details}"
            
        raise HTTPException(status_code=500, detail=msg)


def require_admin(user: dict = Depends(get_current_user)):
    """Dependency that ensures the authenticated user has admin role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user
