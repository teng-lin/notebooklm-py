from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth_deps import (
    SECRET_KEY,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from ..database import get_session as _get_session
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class GoogleBindRequest(BaseModel):
    google_token: str
    expires_at: str | None = None


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str | None
    avatar_url: str | None
    google_bound: bool
    created_at: datetime | None


@router.post("/register", response_model=TokenResponse)
def register(body: RegisterRequest, db: Session = Depends(_get_session)):
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    access = create_access_token(user.id, user.username)
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(_get_session)):
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")
    access = create_access_token(user.id, user.username)
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(body: dict[str, str], db: Session = Depends(_get_session)):
    refresh_token_str = body.get("refresh_token", "")
    payload = decode_token(refresh_token_str)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user_id = int(payload["sub"])
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    access = create_access_token(user.id, user.username)
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.get("/me", response_model=UserInfo)
def me(current_user: User = Depends(get_current_user)):
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        avatar_url=current_user.avatar_url,
        google_bound=bool(current_user.google_token),
        created_at=current_user.created_at,
    )


@router.post("/google/bind")
def google_bind(
    body: GoogleBindRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(_get_session),
):
    from cryptography.fernet import Fernet
    key = _get_fernet_key()
    cipher = Fernet(key)
    encrypted = cipher.encrypt(body.google_token.encode())
    current_user.google_token = encrypted.decode()
    if body.expires_at:
        try:
            current_user.google_token_expires_at = datetime.fromisoformat(body.expires_at)
        except ValueError:
            pass
    db.commit()
    return {"ok": True}


@router.delete("/logout")
def logout(current_user: User = Depends(get_current_user)):
    return {"ok": True}


@router.post("/logout")
def logout_post(current_user: User = Depends(get_current_user)):
    return {"ok": True}


def _get_fernet_key() -> bytes:
    raw = os.environ.get("NOTEBOOKLM_FERNET_KEY", SECRET_KEY)
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
