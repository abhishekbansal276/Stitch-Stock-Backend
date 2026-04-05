from pydantic import BaseModel, EmailStr
from typing import Optional


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: str = "operator"
    is_active: bool = True


class UserResponse(BaseModel):
    email: str
    full_name: str
    role: str
    is_active: bool
    uid: Optional[str] = None
