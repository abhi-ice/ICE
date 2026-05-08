"""JWT auth + Google session_token (cookie) — dual auth.

Order of resolution per request:
  1. session_token cookie  → look up in `user_sessions`
  2. Authorization: Bearer <JWT> → decode HS256

Followup-fix (2026-05-08): verify_password used to swallow every
exception as `return False`, so a malformed hash, encoding bug, or
bcrypt-library breakage all surfaced to the user as the same generic
"Invalid email or password" — and nothing was logged. That made the
"my password drifted after a restart" incident impossible to root-
cause from logs. The new verify_password logs the actual failure
reason at WARN level (with a redacted hash prefix), and validate_
bcrypt_runtime() runs a self-test the lifespan hook calls at server
startup so any runtime-level bcrypt break fails loud instead of
silently locking everyone out.
"""
import logging
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
BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))

bearer = HTTPBearer(auto_error=False)

log = logging.getLogger("auth")

# bcrypt 4.x dropped __about__; tolerate either layout.
try:
    _BCRYPT_VERSION = bcrypt.__version__  # type: ignore[attr-defined]
except AttributeError:
    try:
        _BCRYPT_VERSION = bcrypt.__about__.__version__  # type: ignore[attr-defined]
    except AttributeError:
        _BCRYPT_VERSION = "unknown"
log.info("bcrypt runtime: version=%s rounds=%d", _BCRYPT_VERSION, BCRYPT_ROUNDS)

# Bcrypt prefixes we recognize. Anything else is treated as malformed.
_VALID_HASH_PREFIXES = ("$2a$", "$2b$", "$2x$", "$2y$")


def _looks_like_bcrypt_hash(s: str) -> bool:
    return isinstance(s, str) and len(s) >= 60 and s.startswith(_VALID_HASH_PREFIXES)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    """Returns True iff `plain` matches the stored hash. Logs the actual
    reason on every failure path so a future "I can't log in" incident
    is debuggable from server logs without DB access."""
    if not hashed:
        log.warning("verify_password: stored hash is empty/None -- caller cannot authenticate")
        return False
    if not _looks_like_bcrypt_hash(hashed):
        # Truncated, plaintext, base64'd by mistake, etc. Log the
        # prefix (first 4 chars) only; never the full hash.
        log.warning(
            "verify_password: stored hash format unrecognized (prefix=%r, len=%d). "
            "Treating as auth failure.",
            (hashed[:4] if isinstance(hashed, str) else type(hashed).__name__),
            len(hashed) if isinstance(hashed, str) else -1,
        )
        return False
    try:
        ok = bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
        if not ok:
            # Genuine wrong-password case; INFO not WARN to avoid log spam
            # from normal failed-login attempts.
            log.info("verify_password: bcrypt mismatch (hash_prefix=%r)", hashed[:7])
        return ok
    except Exception as e:
        # Library breakage, encoding bug, hash truncation that snuck past
        # the format check. Log enough context to diagnose without
        # leaking the hash itself.
        log.warning(
            "verify_password: bcrypt.checkpw raised %s: %s (hash_prefix=%r, len=%d, plain_len=%d)",
            type(e).__name__, str(e)[:200], hashed[:7], len(hashed), len(plain),
        )
        return False


def validate_bcrypt_runtime() -> dict:
    """Self-test: hash a known string, verify it round-trips. Run from
    the lifespan startup hook so any bcrypt-runtime breakage fails the
    deploy loudly instead of locking users out at first login.

    Returns a dict {ok, version, error?}. Caller decides whether to
    raise -- typically the lifespan hook logs WARN and proceeds (so the
    server still boots and the operator can patch via the CLI reset
    script), but the WARN will appear in Railway's log stream.
    """
    sample = "smoketest-Ice-bcrypt-runtime"
    try:
        h = hash_password(sample)
        if not _looks_like_bcrypt_hash(h):
            return {"ok": False, "version": _BCRYPT_VERSION,
                    "error": f"hash_password produced non-bcrypt output (prefix={h[:4]!r})"}
        ok = bcrypt.checkpw(sample.encode("utf-8"), h.encode("utf-8"))
        if not ok:
            return {"ok": False, "version": _BCRYPT_VERSION,
                    "error": "bcrypt.checkpw returned False on a freshly-generated hash"}
        # Round-trip verify with verify_password too so the format-check
        # path is exercised.
        if not verify_password(sample, h):
            return {"ok": False, "version": _BCRYPT_VERSION,
                    "error": "verify_password rejected a freshly-generated hash"}
        return {"ok": True, "version": _BCRYPT_VERSION}
    except Exception as e:
        return {"ok": False, "version": _BCRYPT_VERSION,
                "error": f"{type(e).__name__}: {e}"}


def create_token(user_id: str, tenant_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id, "tenant_id": tenant_id, "tid": tenant_id, "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRE_HOURS)).timestamp()),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def create_access_token(user: dict) -> str:
    """Compatibility shim used by voice-PIN flow + tests."""
    return create_token(user["id"], user["tenant_id"], user.get("email", ""))


def role_required(ctx: dict, roles: list[str]) -> None:
    user_role = (ctx.get("user") or {}).get("role")
    if user_role not in roles and "admin" not in roles:
        # admin always has access
        if user_role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role {user_role} not in {roles}")


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
