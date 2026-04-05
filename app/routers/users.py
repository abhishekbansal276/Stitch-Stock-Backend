from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth, firestore
from app.services.firebase import db
from app.models.user import UserCreate, UserResponse
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/users", tags=["users"])

@router.post("/create", response_model=UserResponse)
def create_new_user(user_in: UserCreate, admin: dict = Depends(require_admin)):
    """
    Admin-only endpoint to create a new user in both Firebase Auth and Firestore.
    """
    try:
        # 1. Create user in Firebase Authentication
        # Note: In a production app, you might want to send a password reset email
        new_auth_user = auth.create_user(
            email=user_in.email,
            password=user_in.password,
            display_name=user_in.full_name
        )
        
        # 2. Store user metadata in Firestore
        user_data = {
            "email": user_in.email,
            "full_name": user_in.full_name,
            "role": user_in.role,
            "is_active": user_in.is_active,
            "created_at": firestore.SERVER_TIMESTAMP,
            "uid": new_auth_user.uid
        }
        
        db.collection('users').document(user_in.email).set(user_data)
        
        return UserResponse(
            email=user_in.email,
            full_name=user_in.full_name,
            role=user_in.role,
            is_active=user_in.is_active,
            uid=new_auth_user.uid
        )
        
    except auth.EmailAlreadyExistsError:
        raise HTTPException(status_code=400, detail="User with this email already exists")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create user: {str(e)}")

@router.get("/", response_model=list[UserResponse])
def list_users(admin: dict = Depends(require_admin)):
    """Admin-only: List all users from Firestore."""
    try:
        users_ref = db.collection('users').stream()
        return [UserResponse(**u.to_dict()) for u in users_ref]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/{email}/status")
def update_user_status(email: str, status: bool, admin: dict = Depends(require_admin)):
    """Admin-only: Deactivate or Activate a user."""
    try:
        user_ref = db.collection('users').document(email)
        if not user_ref.get().exists:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_ref.update({"is_active": status})
        return {"message": f"User status updated to {'active' if status else 'inactive'}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/me", response_model=UserResponse)
def get_me(user: dict = Depends(get_current_user)):
    """
    Returns the currently authenticated user's profile.
    """
    return UserResponse(
        email=user.get('email'),
        full_name=user.get('full_name'),
        role=user.get('role'),
        is_active=user.get('is_active', True),
        uid=user.get('uid')
    )
