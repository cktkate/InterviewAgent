"""Password hashing and minimal HS256 JWT helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings

PBKDF2_ITERATIONS = 600_000
_bearer = HTTPBearer(auto_error=False)


class TokenError(ValueError):
    pass


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(digest, _b64decode(expected))
    except (TypeError, ValueError):
        return False


def create_access_token(user_id: str, username: str) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "username": username,
        "iat": now,
        "exp": now + settings.jwt_expire_minutes * 60,
    }
    header_part = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    payload_part = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(
        settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()
    return f"{header_part}.{payload_part}.{_b64encode(signature)}"


def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        header_part, payload_part, signature_part = token.split(".")
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        expected = hmac.new(
            settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64decode(signature_part)):
            raise TokenError("invalid signature")
        header = json.loads(_b64decode(header_part))
        payload = json.loads(_b64decode(payload_part))
        if header.get("alg") != "HS256":
            raise TokenError("invalid algorithm")
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise TokenError("token expired")
        if not payload.get("sub"):
            raise TokenError("missing subject")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, TokenError):
            raise
        raise TokenError("invalid token") from exc


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        return decode_access_token(credentials.credentials)
    except TokenError:
        raise _unauthorized()


def get_current_user_id(current_user: Dict[str, Any] = Depends(get_current_user)) -> str:
    return str(current_user["sub"])


def assert_session_owner(state: Dict[str, Any], user_id: str) -> None:
    if state.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You do not own this interview session")
