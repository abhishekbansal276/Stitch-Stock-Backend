from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth, firestore
from app.services.firebase import db
from app.models.user import UserCreate, UserResponse
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/users", tags=["users"])

@router.post("/create", response_model=UserResponse)
def create_new_user(user_in: UserCreate, admin: dict = Depends(require_admin)):
    try:
        # 1. Create user in Firebase Authentication
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
        raise HTTPException(status_code=400, detail="User already exists")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create user: {str(e)}")

@router.get("/me", response_model=UserResponse)
def get_me(user: dict = Depends(get_current_user)):
    # Simply return the already fetched user data from the middleware
    return UserResponse(**user)
