"""JWT auth — Bearer-only.

Order of resolution per request:
  1. Authorization: Bearer <JWT> → decode HS256

Google OAuth + cookie session_token purge (2026-05-11): the prior dual-auth
path (cookie OR JWT) was removed per federal-contractor data-confidentiality
policy. No third-party auth providers. The `user_sessions` collection is
dropped by migration 016. Only the Bearer-JWT path remains.

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
import secrets
import bcrypt
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Header, status
from starlette.requests import HTTPConnection
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from database import get_db
from models import gen_id

log = logging.getLogger("auth")


def _resolve_jwt_secret(raw: Optional[str]) -> str:
    """Day-1 hardening (2026-10-25, audit findings 6/18/48).

    The old code fell back to the well-known string "dev-secret" when
    JWT_SECRET was missing, so anyone could sign a login token for any
    user. Now:
      * missing / empty / "dev-secret" -> CRITICAL log + a random
        per-process secret. Forged tokens become impossible; the cost is
        that everyone is signed out whenever the server restarts (and a
        multi-worker deployment would not share the secret), so the
        operator MUST set a real JWT_SECRET.
      * the old committed placeholder (contains "change-in-prod") -> keep
        using it so tonight's production logins keep working, but log
        CRITICAL telling the operator to rotate it.
    """
    value = raw or ""
    if not value.strip() or value.strip() == "dev-secret":
        log.critical(
            "JWT_SECRET is missing or set to the known default. Using a random "
            "secret for this process only: everyone will be signed out on every "
            "restart. Set JWT_SECRET to a long random value in the deployment "
            "environment (python -c \"import secrets;print(secrets.token_urlsafe(64))\")."
        )
        return secrets.token_urlsafe(64)
    if "change-in-prod" in value:
        log.critical(
            "JWT_SECRET is still the placeholder value that was committed to git. "
            "Anyone who has seen the repository can forge login tokens. Rotate it "
            "now to a long random value in the deployment environment."
        )
    return value


JWT_SECRET = _resolve_jwt_secret(os.environ.get("JWT_SECRET"))
JWT_ALG = os.environ.get("JWT_ALG", "HS256")
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))
BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))

bearer = HTTPBearer(auto_error=False)

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


def token_version_of(user: Optional[dict]) -> int:
    """The user's current sign-in version. Missing or unreadable = 0, so
    every user created before this field existed starts at 0."""
    try:
        return int((user or {}).get("token_version") or 0)
    except (TypeError, ValueError):
        return 0


def create_token(user_id: str, tenant_id: str, email: str, token_version: int = 0) -> str:
    """Sign a login token. `tv` carries the user's token_version at sign-in;
    a password reset / account close / member removal bumps the user's
    version, which retires every token signed before it."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id, "tenant_id": tenant_id, "tid": tenant_id, "email": email,
        "tv": int(token_version or 0),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRE_HOURS)).timestamp()),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def create_access_token(user: dict) -> str:
    """Compatibility shim used by voice-PIN flow + tests."""
    return create_token(user["id"], user["tenant_id"], user.get("email", ""),
                        token_version_of(user))


# ------------------------------------------------------------------
# Timing-safe login for unknown emails (2026-11 security round).
# When the email is not on file we still run one bcrypt check against a
# fixed throwaway hash, so "no such account" takes as long as "wrong
# password" and the response time cannot be used to discover accounts.
# ------------------------------------------------------------------
_DUMMY_HASH: Optional[str] = None


def dummy_password_check(plain: str) -> None:
    global _DUMMY_HASH
    try:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
        bcrypt.checkpw((plain or "").encode("utf-8"), _DUMMY_HASH.encode("utf-8"))
    except Exception:
        pass


# ------------------------------------------------------------------
# Hiding secrets in audit rows (2026-11 security round).
# Audit rows must never carry an invite code, a reset token, a PIN or a
# password hash. Used when writing new rows AND when reading old ones
# back out (older production rows still hold invite codes; we mask them
# on the way out instead of rewriting live records).
# ------------------------------------------------------------------
_SECRET_KEYS = {
    "invite_code", "token", "access_token", "refresh_token",
    "pin", "secret", "api_key",
}
# "code" is an ordinary field in many tables (cost codes, room codes,
# predecessor codes), so it is only hidden where it is the invite code:
# callers pass extra_keys=INVITE_SECRET_KEYS for team_invites rows.
INVITE_SECRET_KEYS = frozenset({"code"})
REDACTED = "[hidden]"


def _is_secret_key(key, extra_keys=()) -> bool:
    k = str(key or "").strip().lower()
    if k in _SECRET_KEYS or k in extra_keys:
        return True
    if k.endswith("_hash") or k.endswith("_token") or k.endswith("_secret"):
        return True
    # password, password_hash, password_reset_token_hash ... but keep the
    # harmless "when / by whom" stamps (password_reset_at, _by).
    if k.startswith("password") and not k.endswith(("_at", "_by")):
        return True
    return False


def redact_secrets(value, extra_keys=()):
    """Return a copy of `value` with secret-looking fields masked, at any
    depth. Non-dict / non-list values are returned unchanged.
    `extra_keys` adds field names to hide for this call (for example
    INVITE_SECRET_KEYS on invite rows)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _is_secret_key(k, extra_keys) and v not in (None, ""):
                out[k] = REDACTED
            else:
                out[k] = redact_secrets(v, extra_keys)
        return out
    if isinstance(value, list):
        return [redact_secrets(v, extra_keys) for v in value]
    return value


# ------------------------------------------------------------------
# Sign-in throttling (2026-11 security round).
#
# Failed attempts are stored in the `auth_attempts` collection (one small
# row per failure, removed automatically by a TTL index after an hour),
# so the limits survive a restart and hold across several servers.
# Each check names one or more buckets with their own limit, e.g. the
# account (10 per 15 minutes) and the client address (50 per 15 minutes,
# higher so a site office sharing one internet connection is not shut
# out by one person's typos). If the database itself has a problem we let
# the attempt through: a throttling hiccup must never lock people out.
# ------------------------------------------------------------------
THROTTLE_WINDOW = timedelta(minutes=15)
# Per (account, client address): 10 wrong tries in the window. A stranger
# guessing from their own address can no longer lock the real person out,
# because the real person signs in from a different address.
ACCOUNT_FAILURE_LIMIT = 10
# Per account across ALL addresses: a much higher ceiling (100) that still
# stops a guessing attack spread over many addresses.
ACCOUNT_TOTAL_FAILURE_LIMIT = 100
IP_FAILURE_LIMIT = 50
_ATTEMPTS_TTL_SECONDS = 3600
_attempt_indexes_ready = False


async def ensure_auth_attempt_indexes(db=None) -> None:
    """Create the TTL + lookup indexes for auth_attempts. Safe to call
    many times (index creation is idempotent); runs once per process."""
    global _attempt_indexes_ready
    if _attempt_indexes_ready:
        return
    db = db or get_db()
    try:
        await db.auth_attempts.create_index("at", expireAfterSeconds=_ATTEMPTS_TTL_SECONDS)
        await db.auth_attempts.create_index([("key", 1), ("at", 1)])
        _attempt_indexes_ready = True
    except Exception as e:  # never block sign-in on an index problem
        log.warning("auth_attempts index setup failed: %s", e)


def client_ip(request) -> str:
    """The caller's address. The server runs uvicorn with proxy_headers on
    (serve.py), so request.client.host is already the real client address
    taken from X-Forwarded-For. Falls back to that header, then "unknown"."""
    host = ""
    try:
        if request is not None and request.client:
            host = request.client.host or ""
    except Exception:
        host = ""
    if not host and request is not None:
        try:
            host = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        except Exception:
            host = ""
    return host or "unknown"


async def check_throttle(buckets: list[tuple[str, int]], *, includes_current: bool = False) -> None:
    """Raise 429 (with Retry-After) when any bucket already holds `limit`
    failures inside the window. `buckets` is a list of (key, limit).
    `includes_current` means this attempt's own row is already counted
    (see reserve_attempt), so one more row is allowed."""
    db = get_db()
    await ensure_auth_attempt_indexes(db)
    now = datetime.now(timezone.utc)
    since = now - THROTTLE_WINDOW
    allowed_extra = 1 if includes_current else 0
    for key, limit in buckets:
        try:
            n = await db.auth_attempts.count_documents({"key": key, "at": {"$gte": since}})
            if n < limit + allowed_extra:
                continue
            oldest = await db.auth_attempts.find_one(
                {"key": key, "at": {"$gte": since}}, {"_id": 0, "at": 1},
                sort=[("at", 1)],
            )
        except Exception as e:
            log.warning("sign-in throttle check failed (letting attempt through): %s", e)
            continue
        retry = int(THROTTLE_WINDOW.total_seconds())
        at = (oldest or {}).get("at")
        if isinstance(at, datetime):
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            retry = int((at + THROTTLE_WINDOW - now).total_seconds()) + 1
        retry = max(1, retry)
        minutes = max(1, (retry + 59) // 60)
        plural = "s" if minutes != 1 else ""
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many attempts. Try again in {minutes} minute{plural}.",
            headers={"Retry-After": str(retry)},
        )


async def reserve_attempt(buckets: list[tuple[str, int]]) -> Optional[str]:
    """Count this attempt as a failure FIRST, then check the limit.

    Checking first and recording afterwards let many requests sent at the
    same moment all pass the check before any of them was recorded, so an
    attacker could make far more than 10 guesses on one account. Recording
    first closes that gap. The caller removes the row again with
    release_attempt() when the attempt turns out not to be a wrong guess
    (a good sign-in clears the account's rows anyway). A refused attempt
    removes its own row, so retrying while locked does not extend the lock.
    Used for the per-account buckets only; per-address buckets keep the
    check-then-record order so a busy site office is never refused for
    good sign-ins that happen at the same time.
    Returns an id for release_attempt(), or None if nothing was recorded
    (database problem: the attempt is let through, as in check_throttle)."""
    db = get_db()
    await ensure_auth_attempt_indexes(db)
    rid = secrets.token_hex(12)
    now = datetime.now(timezone.utc)
    try:
        for key, _limit in buckets:
            await db.auth_attempts.insert_one({"key": key, "at": now, "rid": rid})
    except Exception as e:
        log.warning("could not record sign-in attempt (letting it through): %s", e)
        await release_attempt(rid)
        return None
    try:
        await check_throttle(buckets, includes_current=True)
    except HTTPException:
        await release_attempt(rid)
        raise
    return rid


async def release_attempt(rid: Optional[str]) -> None:
    """Remove the rows reserve_attempt() added for one attempt. Best effort."""
    if not rid:
        return
    db = get_db()
    try:
        await db.auth_attempts.delete_many({"rid": rid})
    except Exception as e:
        log.warning("could not release sign-in attempt: %s", e)


async def record_failure(keys: list[str]) -> None:
    """Count one failed attempt against each key. Best effort."""
    db = get_db()
    now = datetime.now(timezone.utc)
    for key in keys:
        try:
            await db.auth_attempts.insert_one({"key": key, "at": now})
        except Exception as e:
            log.warning("could not record sign-in failure: %s", e)


async def clear_failures(keys: list[str]) -> None:
    """Forget the failures for these keys (after a successful sign-in)."""
    db = get_db()
    try:
        await db.auth_attempts.delete_many({"key": {"$in": list(keys)}})
    except Exception as e:
        log.warning("could not clear sign-in failures: %s", e)


def role_required(ctx: dict, roles: list[str]) -> None:
    """Refuse (403) unless the caller is an admin or their role is in
    `roles`. Day-1 fix (2026-10-25, audit findings 1/14): the old check
    `user_role not in roles and "admin" not in roles` was always False
    for every caller (they all pass "admin"), so it never refused anyone."""
    user_role = ((ctx.get("user") or {}).get("role") or "").strip().lower()
    allowed = {(r or "").strip().lower() for r in (roles or [])}
    if user_role == "admin" or (user_role and user_role in allowed):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role can't do this.")


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


def default_access_scope(role: Optional[str]) -> str:
    """Per-project access (2026-11-16). Admins see every project unless they
    were limited to specific projects; everyone else sees only the projects
    they are assigned to. Only an admin can ever have "all"."""
    return "all" if (role or "").strip().lower() == "admin" else "assigned"


def effective_access_scope(role: Optional[str], stored: Optional[str]) -> str:
    """The scope that applies: the stored value when it is valid for the
    role, otherwise the default. A non-admin is always "assigned", whatever
    is stored, so a stale "all" left on a demoted admin's row grants nothing."""
    if (role or "").strip().lower() != "admin":
        return "assigned"
    val = (stored or "").strip().lower()
    return val if val in ("all", "assigned") else "all"


async def stored_access_scope(*, tenant_id: str, user_id: str, db=None) -> Optional[str]:
    """Read the saved scope for (company, user): the membership row first,
    then the user record when that company is the user's home company.
    Returns None when nothing is saved (callers then use the default)."""
    db = db if db is not None else get_db()
    try:
        tu = await db.tenant_users.find_one(
            {"tenant_id": tenant_id, "user_id": user_id},
            {"_id": 0, "access_scope": 1},
        )
        if tu and tu.get("access_scope"):
            return tu.get("access_scope")
        u = await db.users.find_one(
            {"id": user_id, "tenant_id": tenant_id}, {"_id": 0, "access_scope": 1},
        )
        return (u or {}).get("access_scope")
    except Exception:
        return None


async def get_accessible_project_ids(
    *, tenant_id: str, user_id: str, user_role: str,
) -> Optional[list[str]]:
    """Returns list of project ids the user can see, or None on
    admin all-access. Empty list = scoped but no memberships.

    2026-11-16: an admin limited to specific projects ("assigned" scope) is
    no longer all-access here; any membership row counts for them."""
    role = (user_role or "").lower()
    db = get_db()
    if role == "admin":
        scope = effective_access_scope(
            role, await stored_access_scope(tenant_id=tenant_id, user_id=user_id, db=db))
        if scope == "all":
            return None
        rows = await db.project_members.find(
            {"tenant_id": tenant_id, "user_id": user_id},
            {"_id": 0, "project_id": 1},
        ).to_list(length=None)
        return [r["project_id"] for r in rows]
    allowed = _PROJECT_ACCESS_ROLES.get(role)
    if allowed is None:
        # Unknown role -> deny.
        return []
    rows = await db.project_members.find(
        {"tenant_id": tenant_id, "user_id": user_id,
         "role_on_project": {"$in": list(allowed)}},
        {"_id": 0, "project_id": 1},
    ).to_list(length=None)
    return [r["project_id"] for r in rows]


async def has_project_access(
    *, tenant_id: str, user_id: str, user_role: str, project_id: str,
) -> bool:
    """Quick per-project check. An all-projects admin is always True."""
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
        # Keep the library's reason in the server log only; the caller just
        # needs to know the token was rejected.
        log.info("Rejected token: %s", e)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


async def _resolve_via_jwt(token: str) -> dict | None:
    db = get_db()
    claims = decode_token(token)
    user = await db.users.find_one({"id": claims["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        return None
    # Token retired by a password reset / account close / member removal.
    # Tokens issued before this check existed carry no "tv" claim and
    # count as 0, the same as a user with no token_version yet, so they
    # keep working until one of those events happens.
    try:
        claim_tv = int(claims.get("tv") or 0)
    except (TypeError, ValueError):
        claim_tv = 0
    if claim_tv < token_version_of(user):
        return None
    return user


# The request context is built once per request and kept on the request
# itself, so the route-level project check (services/project_access) and the
# endpoint's own require_tenant share it without a second database trip.
_CTX_SCOPE_KEY = "ice.auth_ctx"


async def build_request_context(token: Optional[str], x_tenant_id: Optional[str]) -> dict:
    """Resolve a bearer token into the request context.

    2026-11-16 (per-project access):
      * ctx["role"] and ctx["user"]["role"] are the role in the ACTIVE company.
        For the user's home company that is users.role (unchanged); for
        another company picked with X-Tenant-ID it is that company's
        tenant_users.role. Before this, an admin of company A was treated as
        an admin in company B, whatever role B gave them.
      * ctx["access_scope"] is "all" or "assigned" (see
        effective_access_scope). Saved on tenant_users.access_scope (home
        company: also users.access_scope); nothing saved = the default, so
        existing admins stay all-projects without rewriting any record.
    """
    user = None
    if token:
        user = await _resolve_via_jwt(token)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    db = get_db()
    home_tenant_id = user.get("tenant_id")
    active_tenant_id = x_tenant_id or home_tenant_id
    membership = await db.tenant_users.find_one(
        {"user_id": user["id"], "tenant_id": active_tenant_id, "status": "active"},
        {"_id": 0},
    )
    if not membership:
        active_tenant_id = home_tenant_id
        membership = None

    if active_tenant_id == home_tenant_id:
        role = (user.get("role") or "").strip().lower()
        stored_scope = ((membership or {}).get("access_scope")
                        or user.get("access_scope"))
    else:
        # Another company: its own membership row decides. A row with no
        # role gets the least-privileged one.
        role = ((membership or {}).get("role") or "viewer").strip().lower()
        stored_scope = (membership or {}).get("access_scope")
    scope = effective_access_scope(role, stored_scope)
    if role:
        user["role"] = role
    user["access_scope"] = scope

    return {"user": user, "tenant_id": active_tenant_id, "request_id": gen_id(),
            "role": role, "access_scope": scope}


def _bearer_token_from(conn: Optional[HTTPConnection]) -> Optional[str]:
    if conn is None:
        return None
    raw = conn.headers.get("authorization") or ""
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _note_route(ctx: dict, conn: Optional[HTTPConnection]) -> None:
    """Remember the route and method on the context, for the project access
    watch log (services/project_access)."""
    if conn is None:
        return
    try:
        route = conn.scope.get("route")
        ctx.setdefault("_pa_route", getattr(route, "path", None) or conn.url.path)
        ctx.setdefault("_pa_method", (conn.scope.get("method") or "GET").upper())
        # The phone's background offline warm-up marks its reads, so the
        # watch log does not count them as someone opening the project.
        ctx.setdefault("_pa_prefetch", conn.headers.get("x-ice-prefetch") == "1")
    except Exception:
        pass


async def context_for_connection(conn: HTTPConnection) -> Optional[dict]:
    """The request context for this request, built at most once. Returns
    None (never raises) when the request carries no usable sign-in; the
    endpoint's own require_tenant then gives the usual 401."""
    cached = conn.scope.get(_CTX_SCOPE_KEY)
    if cached is not None:
        return cached
    token = _bearer_token_from(conn)
    if not token:
        return None
    try:
        ctx = await build_request_context(token, conn.headers.get("x-tenant-id"))
    except HTTPException:
        return None
    _note_route(ctx, conn)
    conn.scope[_CTX_SCOPE_KEY] = ctx
    return ctx


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-ID"),
    request: HTTPConnection = None,
) -> dict:
    """Bearer-JWT auth. The Google-OAuth cookie path was removed in the
    2026-05-11 Google-Auth purge (federal-contractor data-confidentiality
    policy). Reuses the context already built for this request (see
    context_for_connection) when there is one."""
    if request is not None:
        cached = request.scope.get(_CTX_SCOPE_KEY)
        if cached is not None:
            return cached
    ctx = await build_request_context(creds.credentials if creds else None, x_tenant_id)
    if request is not None:
        _note_route(ctx, request)
        request.scope[_CTX_SCOPE_KEY] = ctx
    return ctx


async def require_tenant(ctx: dict = Depends(get_current_user)) -> dict:
    if not ctx.get("tenant_id"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No active tenant")
    return ctx
