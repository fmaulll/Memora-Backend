import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from app.schemas.subscription import Entitlement


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=8)

    @field_validator("password")
    @classmethod
    def password_length(cls, value):
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 UTF-8 bytes")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: EmailStr
    created_at: datetime
    is_anonymous: bool
    app_account_token: uuid.UUID
    free_ai_deck_available: bool
    entitlement: Entitlement


class UserUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None


class TokenResponse(BaseModel):
    user: UserResponse
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class AnonymousUserRequest(BaseModel):
    name: str


class AuthResponse(BaseModel):
    user: UserResponse
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshTokenRequest(BaseModel):
    refresh_token: str