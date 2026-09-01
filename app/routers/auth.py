from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.schemas.auth import UserUpdate
from app.core.auth import get_current_user
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.db.database import get_db
from app.models.user import User
from app.schemas.auth import (
    AnonymousUserRequest,
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
import uuid


router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
)

@router.post(
    "/anonymous",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_anonymous_user(
    data: AnonymousUserRequest,
    db: Session = Depends(get_db),
):
    user_id = uuid.uuid4()

    anonymous_email = (
        f"anonymous-{user_id}@memoraapp.com"
    )

    user = User(
        id=user_id,
        name=data.name,
        email=anonymous_email,
        password_hash=hash_password(
            str(uuid.uuid4())
        ),
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(
        str(user.id)
    )

    return AuthResponse(
        user=user,
        access_token=token,
    )


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    data: RegisterRequest,
    db: Session = Depends(get_db),
):
    existing_user = db.scalar(
        select(User).where(User.email == data.email)
    )

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered",
        )

    user = User(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user


@router.post(
    "/login",
    response_model=TokenResponse,
)
def login(
    data: LoginRequest,
    db: Session = Depends(get_db),
):
    user = db.scalar(
        select(User).where(User.email == data.email)
    )

    if user is None or not verify_password(
        data.password,
        user.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    token = create_access_token(
        str(user.id)
    )

    return TokenResponse(
        access_token=token,
    )


@router.get(
    "/me",
    response_model=UserResponse,
)
def get_me(
    current_user: User = Depends(get_current_user),
):
    return current_user

@router.put(
    "/me",
    response_model=UserResponse,
)
def update_me(
    data: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.email is not None and data.email != current_user.email:
        existing_user = db.scalar(
            select(User).where(
                User.email == data.email,
                User.id != current_user.id,
            )
        )

        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email is already registered",
            )

        current_user.email = data.email

    if data.name is not None:
        current_user.name = data.name

    db.commit()
    db.refresh(current_user)

    return current_user