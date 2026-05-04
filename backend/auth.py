"""JWT auth + Google session_token (cookie) — dual auth.

Order of resolution per request:
  1. session_token cookie  → look up in `user_sessions`
  2. Authorization: Bearer <JWT> → decode HS256
"""
import os
import bcrypt
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Header, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from database import get_db
from models import gen_id

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
JWT_ALG = os.environ.get("JWT_ALG", "HS256")
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))

bearer = HTTPBearer(auto_error=False)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(user_id: str, tenant_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id, "tenant_id": tenant_id, "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRE_HOURS)).timestamp()),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except pyjwt.InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")


async def _resolve_via_cookie(token: str) -> dict | None:
    db = get_db()
    sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not sess:
        return None
    expires_at = sess.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            expires_at = None
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None
    user = await db.users.find_one({"id": sess["user_id"]}, {"_id": 0, "password_hash": 0})
    return user


async def _resolve_via_jwt(token: str) -> dict | None:
    db = get_db()
    claims = decode_token(token)
    user = await db.users.find_one({"id": claims["sub"]}, {"_id": 0, "password_hash": 0})
    return user


async def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-ID"),
) -> dict:
    """Dual-auth: cookie OR Bearer JWT."""
    user = None
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        user = await _resolve_via_cookie(cookie_token)
    if not user and creds:
        user = await _resolve_via_jwt(creds.credentials)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    db = get_db()
    active_tenant_id = x_tenant_id or user.get("tenant_id")
    membership = await db.tenant_users.find_one(
        {"user_id": user["id"], "tenant_id": active_tenant_id, "status": "active"},
        {"_id": 0},
    )
    if not membership:
        active_tenant_id = user.get("tenant_id")

    return {"user": user, "tenant_id": active_tenant_id, "request_id": gen_id()}


async def require_tenant(ctx: dict = Depends(get_current_user)) -> dict:
    if not ctx.get("tenant_id"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No active tenant")
    return ctx
