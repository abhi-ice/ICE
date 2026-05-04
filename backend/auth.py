"""JWT auth + tenant context.

SUBSTITUTION: master prompt §5.3 specified Clerk middleware. Emergent runtime
doesn't ship Clerk; we use HS256 JWT with bcrypt password hashing. Tenant
context is read from the JWT `tenant_id` claim and (optionally) overridden
by an `X-Tenant-ID` header for users that belong to multiple tenants.
"""
import os
import bcrypt
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Header, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from database import get_db, tenant_filter
from models import User, Tenant, gen_id, utc_now

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
JWT_ALG = os.environ.get("JWT_ALG", "HS256")
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))

bearer = HTTPBearer(auto_error=False)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(user_id: str, tenant_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "email": email,
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


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-ID"),
) -> dict:
    """Returns dict with keys: user, tenant_id, request_id."""
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Authorization header")
    payload = decode_token(creds.credentials)
    db = get_db()
    user_doc = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user_doc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")

    # tenant override via header — but only if user is member of that tenant
    active_tenant_id = x_tenant_id or payload.get("tenant_id") or user_doc.get("tenant_id")
    membership = await db.tenant_users.find_one(
        {"user_id": user_doc["id"], "tenant_id": active_tenant_id, "status": "active"},
        {"_id": 0},
    )
    if not membership:
        # fall back to user's primary tenant_id
        active_tenant_id = user_doc.get("tenant_id")

    return {
        "user": user_doc,
        "tenant_id": active_tenant_id,
        "request_id": gen_id(),
    }


async def require_tenant(ctx: dict = Depends(get_current_user)) -> dict:
    if not ctx.get("tenant_id"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No active tenant")
    return ctx
