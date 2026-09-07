"""Registration, login, and current-user endpoints."""
from __future__ import annotations

import re
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.security import create_access_token, get_current_user, hash_password, verify_password
from memory.auth_repository import auth_repository

router = APIRouter(prefix="/auth", tags=["Authentication"])


class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str


def _normalize_username(username: str) -> str:
    value = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", value):
        raise HTTPException(
            status_code=422,
            detail="Username must be 3-32 letters, numbers, or underscores",
        )
    return value


def _token_response(user: Dict[str, Any]) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user["user_id"], user["username"]),
        user_id=user["user_id"],
        username=user["username"],
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(req: AuthRequest):
    username = _normalize_username(req.username)
    try:
        user = auth_repository.create_user(username, hash_password(req.password))
    except ValueError:
        raise HTTPException(status_code=409, detail="Username already exists")
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(req: AuthRequest):
    username = _normalize_username(req.username)
    account = auth_repository.get_by_username(username)
    if account is None or not account.is_active or not verify_password(
        req.password, account.password_hash
    ):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    auth_repository.mark_login(account.user_id)
    return _token_response({"user_id": account.user_id, "username": account.username})


@router.get("/me")
def me(current_user: Dict[str, Any] = Depends(get_current_user)):
    return {
        "user_id": current_user["sub"],
        "username": current_user.get("username", ""),
    }
