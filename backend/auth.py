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


# Bucket H Phase 3 (2026-05-08) -- per-project RBAC helper.
#
# Returns the list of project ids the current user can see, or None
# if they have all-access (admin). Returns [] if the user is scoped
# but has no project memberships -- endpoints translate to either
# an empty response or a 404 depending on context.
#
# Role rules per the Phase 3 spec:
#   admin    -> None (all projects in tenant)
#   pm       -> projects where role_on_project = pm
#   super    -> projects where role_on_project in {pm, super, foreman}
#   foreman  -> any membership (own crew / own project)
#   viewer   -> any membership (read-only)
#   other    -> [] (no access; restrictive default)

# Map user.role -> set of project_members.role_on_project values
# they're permitted to see. None means all-access.
_PROJECT_ACCESS_ROLES: dict[str, Optional[set[str]]] = {
    "admin":   None,
    "pm":      {"pm"},
    "super":   {"pm", "super", "foreman"},
    "foreman": {"pm", "super", "foreman", "viewer"},
    "viewer":  {"pm", "super", "foreman", "viewer"},
    # legacy tenant-user roles (px, pe) map per migration 009:
    "px":      {"pm"},                                # treated like pm
    "pe":      {"pm", "super", "foreman"},            # treated like super
    # Bucket J Phase 2 (2026-05-11) -- specialty roles. Both see
    # every project they have membership on (any role_on_project
    # tier counts), so cross-project specialists can navigate
    # their workload without admin per-project chasing.
    "submittal_tech":      {"pm", "super", "foreman", "viewer",
                              "submittal_tech"},
    "scheduling_engineer": {"pm", "super", "foreman", "viewer",
                              "scheduling_engineer"},
}


# Bucket J Phase 2 -- role -> write-capability flags. Each helper
# returns True iff the role is allowed to write to that surface.
# The tenant-wide admin role bypasses all of these.
def role_can_write_submittals(user_role: str) -> bool:
    """submittal_tech + pm + admin can write submittals; everyone
    else is read-only on the surface."""
    return (user_role or "").lower() in {
        "admin", "pm", "submittal_tech",
    }


def role_can_write_schedule(user_role: str) -> bool:
    """scheduling_engineer + pm + admin + pe (alias) can write
    tasks / schedule proposals."""
    return (user_role or "").lower() in {
        "admin", "pm", "pe", "scheduling_engineer",
    }


def role_can_write_field_events(user_role: str) -> bool:
    """Anything in the daily-ops surface (delays, RFIs, QC,
    safety, deliveries, coord, photos, daily_reports).
    submittal_tech + scheduling_engineer are READ-ONLY here per
    spec."""
    return (user_role or "").lower() in {
        "admin", "pm", "super", "foreman",
    }


def role_can_commit_reallocation(user_role: str) -> bool:
    """Resource leveling reallocation commit. submittal_tech +
    scheduling_engineer can VIEW but cannot commit; only admin +
    pm + super can write."""
    return (user_role or "").lower() in {
        "admin", "pm", "super",
    }


def role_can_recompute_resource_leveling(user_role: str) -> bool:
    """Read + recompute access. scheduling_engineer can recompute
    (their core workflow) but not commit; submittal_tech is
    read-only."""
    return (user_role or "").lower() in {
        "admin", "pm", "super", "scheduling_engineer", "pe",
    }


async def get_accessible_project_ids(
    *, tenant_id: str, user_id: str, user_role: str,
) -> Optional[list[str]]:
    """Returns list of project ids the user can see, or None on
    admin all-access. Empty list = scoped but no memberships."""
    role = (user_role or "").lower()
    if role == "admin":
        return None
    allowed = _PROJECT_ACCESS_ROLES.get(role)
    if allowed is None and role != "admin":
        # Unknown role -> deny.
        return []
    db = get_db()
    rows = await db.project_members.find(
        {"tenant_id": tenant_id, "user_id": user_id,
         "role_on_project": {"$in": list(allowed)}},
        {"_id": 0, "project_id": 1},
    ).to_list(length=None)
    return [r["project_id"] for r in rows]


async def has_project_access(
    *, tenant_id: str, user_id: str, user_role: str, project_id: str,
) -> bool:
    """Quick per-project check. Admin always True."""
    accessible = await get_accessible_project_ids(
        tenant_id=tenant_id, user_id=user_id, user_role=user_role,
    )
    if accessible is None:
        return True
    return project_id in accessible


def can_manage_project_members(user_role: str) -> bool:
    """Only admins + PMs can add/remove/change project members.
    Note: PM scope is still tenant-wide for THIS check -- they can
    manage members on projects where they themselves have role_on_
    project=pm. The router cross-checks before mutating."""
    return (user_role or "").lower() in ("admin", "pm")


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
