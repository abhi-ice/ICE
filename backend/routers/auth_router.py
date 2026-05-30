"""Auth router: register (creates org/tenant), login, me, team invites,
admin password reset."""
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field
from database import get_db
from models import (
    UserRegister, UserLogin, User, Tenant, TokenResponse,
    InviteCreate, InviteAccept, StrictModel,
    gen_id, utc_now,
)
from auth import hash_password, verify_password, create_token, require_tenant
from audit import write_audit

router = APIRouter(prefix="/auth", tags=["auth"])
team_router = APIRouter(prefix="/team", tags=["team"])


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower().strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:48] or "org"


async def _unique_slug(db, base: str) -> str:
    slug = base
    i = 2
    while await db.tenants.find_one({"slug": slug}, {"_id": 0, "id": 1}):
        slug = f"{base}-{i}"
        i += 1
        if i > 50:
            slug = f"{base}-{secrets.token_hex(3)}"
            break
    return slug


@router.post("/register", response_model=TokenResponse)
async def register(payload: UserRegister):
    """Public signup. Creates a fresh tenant + makes the registrant its admin.

    For team-member onboarding into an existing tenant use POST /auth/accept-invite.
    """
    db = get_db()
    if await db.users.find_one({"email": payload.email.lower()}, {"_id": 0, "id": 1}):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    base_slug = _slugify(payload.organization_name)
    slug = await _unique_slug(db, base_slug)

    tenant = {
        "id": gen_id(),
        "legal_name": payload.organization_name,
        "display_name": payload.organization_name,
        "slug": slug,
        "type": "general_contractor",
        "status": "active",
        "cmmc_level": "none",
        "work_item_label": "Field Issue",
        "approval_rules": {},
        "created_at": utc_now().isoformat(),
    }
    await db.tenants.insert_one(tenant.copy())

    user_id = gen_id()
    user_doc = {
        "id": user_id,
        "email": payload.email.lower(),
        "full_name": payload.full_name,
        "tenant_id": tenant["id"],
        "role": "admin",
        "password_hash": hash_password(payload.password),
        "created_at": utc_now().isoformat(),
    }
    await db.users.insert_one(user_doc.copy())
    await db.tenant_users.insert_one({
        "id": gen_id(),
        "user_id": user_id,
        "tenant_id": tenant["id"],
        "role": "admin",
        "status": "active",
        "created_at": utc_now().isoformat(),
    })

    # Best-effort: seed CSI taxonomy + cost rates for this new tenant so AI agents have rates.
    try:
        from seed_aux import seed_taxonomy_and_rates
        await seed_taxonomy_and_rates(tenant["id"])
    except Exception:
        pass

    user_view = {k: v for k, v in user_doc.items() if k != "password_hash"}
    token = create_token(user_id, tenant["id"], payload.email.lower())
    return TokenResponse(access_token=token, user=User(**user_view), tenant=Tenant(**tenant))


@router.post("/login", response_model=TokenResponse)
async def login(payload: UserLogin):
    db = get_db()
    user = await db.users.find_one({"email": payload.email.lower()}, {"_id": 0})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    if not tenant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tenant not found")
    user_view = {k: v for k, v in user.items() if k != "password_hash"}
    token = create_token(user["id"], tenant["id"], user["email"])
    return TokenResponse(access_token=token, user=User(**user_view), tenant=Tenant(**tenant))


@router.get("/me", response_model=User)
async def me(ctx: dict = Depends(require_tenant)):
    return User(**ctx["user"])


@router.get("/tenants", response_model=list[Tenant])
async def my_tenants(ctx: dict = Depends(require_tenant)):
    db = get_db()
    memberships = await db.tenant_users.find({"user_id": ctx["user"]["id"], "status": "active"}, {"_id": 0}).to_list(20)
    tenant_ids = [m["tenant_id"] for m in memberships]
    if ctx["user"].get("tenant_id") and ctx["user"]["tenant_id"] not in tenant_ids:
        tenant_ids.append(ctx["user"]["tenant_id"])
    tenants = await db.tenants.find({"id": {"$in": tenant_ids}}, {"_id": 0}).to_list(50)
    return [Tenant(**t) for t in tenants]


# ============================================================
# TEAM INVITES — admin invites teammates into the SAME tenant
# ============================================================
@router.post("/accept-invite", response_model=TokenResponse)
async def accept_invite(payload: InviteAccept):
    """Public route — code + new password produces an account joined to the
    tenant that issued the code. Code is single-use; expires after 14 days."""
    db = get_db()
    invite = await db.team_invites.find_one({"code": payload.code, "status": "pending"}, {"_id": 0})
    if not invite:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found or already used")
    if invite["email"].lower() != payload.email.lower():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Email does not match the invite")
    expires = invite.get("expires_at")
    if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
        await db.team_invites.update_one({"id": invite["id"]}, {"$set": {"status": "expired"}})
        raise HTTPException(status.HTTP_410_GONE, "Invite expired")
    if await db.users.find_one({"email": payload.email.lower()}, {"_id": 0, "id": 1}):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered — sign in instead")

    tenant = await db.tenants.find_one({"id": invite["tenant_id"]}, {"_id": 0})
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant for this invite no longer exists")

    user_id = gen_id()
    user_doc = {
        "id": user_id,
        "email": payload.email.lower(),
        "full_name": payload.full_name,
        "tenant_id": tenant["id"],
        "role": invite.get("role", "pm"),
        "password_hash": hash_password(payload.password),
        "created_at": utc_now().isoformat(),
    }
    await db.users.insert_one(user_doc.copy())
    await db.tenant_users.insert_one({
        "id": gen_id(),
        "user_id": user_id,
        "tenant_id": tenant["id"],
        "role": invite.get("role", "pm"),
        "status": "active",
        "created_at": utc_now().isoformat(),
    })
    await db.team_invites.update_one({"id": invite["id"]}, {"$set": {
        "status": "accepted",
        "accepted_at": utc_now().isoformat(),
        "accepted_user_id": user_id,
    }})

    user_view = {k: v for k, v in user_doc.items() if k != "password_hash"}
    token = create_token(user_id, tenant["id"], payload.email.lower())
    return TokenResponse(access_token=token, user=User(**user_view), tenant=Tenant(**tenant))


# ----- Authenticated team management -----
@team_router.post("/invites")
async def create_invite(payload: InviteCreate, request: Request, ctx: dict = Depends(require_tenant)):
    if (ctx["user"].get("role") or "").lower() != "admin":
        raise HTTPException(403, "Only tenant admins can invite teammates")
    db = get_db()
    if await db.users.find_one({"email": payload.email.lower(), "tenant_id": ctx["tenant_id"]}, {"_id": 0, "id": 1}):
        raise HTTPException(409, "User with this email is already a member")
    pending = await db.team_invites.find_one({
        "email": payload.email.lower(), "tenant_id": ctx["tenant_id"], "status": "pending",
    }, {"_id": 0})
    if pending:
        return {"invite": pending, "already_existed": True}

    code = secrets.token_urlsafe(9)[:12]
    expires_at = (utc_now() + timedelta(days=14)).isoformat()
    invite = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "email": payload.email.lower(),
        "role": payload.role,
        "code": code,
        "status": "pending",
        "invited_by": ctx["user"]["id"],
        "invited_at": utc_now().isoformat(),
        "expires_at": expires_at,
        "accepted_at": None,
        "accepted_user_id": None,
    }
    await db.team_invites.insert_one(invite.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="team_invites", op="INSERT",
        record_id=invite["id"], before=None, after=invite,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    # Bug #47 (2026-05-14) -- send the invite email via SendGrid (or
    # whatever EMAIL_PROVIDER selects). Sequence: invite row -> audit
    # op -> email send -> response. Email failure NEVER blocks the
    # response -- the invite_code is already persisted and the admin
    # can fall back to copy-paste the invite link from the UI.
    from services.invite_email import send_invite_email
    tenant_row = await db.tenants.find_one(
        {"id": ctx["tenant_id"]}, {"_id": 0, "name": 1},
    )
    tenant_name = (tenant_row or {}).get("name") or "your team"
    # 2026-05-18 -- derive the invite link host from the INCOMING REQUEST
    # so an invite sent from production lands on production, and an
    # invite sent from preview lands on preview. Prevents the deployed
    # app from sending preview-URL links (and vice versa) without
    # juggling per-environment env vars.
    #
    # Precedence:
    #   1. Origin header (set by the browser; trustworthy for CORS-clean
    #      first-party calls; carries scheme + host + port)
    #   2. Referer header host (fallback if Origin missing on
    #      same-origin POSTs that some browsers omit Origin for)
    #   3. INVITE_BASE_URL env var (explicit override, e.g. when admins
    #      run a back-fill script that has no request context)
    #   4. REACT_APP_BACKEND_URL env var (legacy fallback)
    base_url = ""
    origin = request.headers.get("origin") or ""
    if origin:
        base_url = origin.rstrip("/")
    else:
        referer = request.headers.get("referer") or ""
        if referer:
            try:
                from urllib.parse import urlparse
                u = urlparse(referer)
                if u.scheme and u.netloc:
                    base_url = f"{u.scheme}://{u.netloc}"
            except Exception:
                base_url = ""
    if not base_url:
        base_url = (
            os.environ.get("INVITE_BASE_URL")
            or os.environ.get("REACT_APP_BACKEND_URL")
            or ""
        ).rstrip("/")
    # Strip a trailing /api so the base for the user-facing accept-
    # invite page is the frontend origin. If the resolved URL points
    # at the frontend directly (no /api), this is a no-op.
    if base_url.endswith("/api"):
        base_url = base_url[:-4]
    invite_link = f"{base_url}/accept-invite?code={code}" if base_url else f"/accept-invite?code={code}"

    email_result = send_invite_email(
        tenant_name=tenant_name,
        invitee_email=payload.email,
        role=payload.role,
        invite_code=code,
        invite_link=invite_link,
    )

    # Audit either INVITE_EMAIL_SENT or INVITE_EMAIL_FAILED so legal
    # can trace delivery attempts alongside the invite INSERT op.
    # Reasons for both ops to exist (vs one with a status field):
    # the audit log dropdown filters by op string, so distinct ops
    # let counsel grep "INVITE_EMAIL_FAILED" to find delivery issues
    # without scanning all invite ops.
    try:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="team_invites",
            op=("INVITE_EMAIL_SENT" if email_result.get("sent")
                else "INVITE_EMAIL_FAILED"),
            record_id=invite["id"], before=None,
            after={
                "email": payload.email,
                "role": payload.role,
                "provider_message_id": email_result.get("provider_message_id"),
                "error": email_result.get("error"),
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    except Exception:
        # Audit failure is non-fatal here. The INSERT op already
        # captured the invite creation; this second op is supplementary
        # delivery-tracking metadata.
        pass

    return {
        "invite": invite,
        "already_existed": False,
        # Bug #47: surface email-send status to the frontend so the
        # Team page can toast "Invite sent" vs "Invite created -- copy
        # the link manually". The invite link itself remains visible
        # in the Invites list regardless.
        "email_sent": bool(email_result.get("sent")),
        "email_error": email_result.get("error"),
    }


@team_router.get("/invites")
async def list_invites(ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.team_invites.find(
        {"tenant_id": ctx["tenant_id"]}, {"_id": 0},
    ).sort("invited_at", -1).to_list(200)
    return docs


@team_router.delete("/invites/{invite_id}")
async def revoke_invite(invite_id: str, ctx: dict = Depends(require_tenant)):
    if (ctx["user"].get("role") or "").lower() != "admin":
        raise HTTPException(403, "Only tenant admins can revoke invites")
    db = get_db()
    res = await db.team_invites.update_one(
        {"id": invite_id, "tenant_id": ctx["tenant_id"], "status": "pending"},
        {"$set": {"status": "revoked", "revoked_at": utc_now().isoformat(), "revoked_by": ctx["user"]["id"]}},
    )
    if not res.matched_count:
        raise HTTPException(404, "Pending invite not found")
    return {"ok": True}


@team_router.get("/members")
async def list_members(ctx: dict = Depends(require_tenant)):
    db = get_db()
    members = await db.users.find(
        {"tenant_id": ctx["tenant_id"]}, {"_id": 0, "password_hash": 0},
    ).sort("created_at", 1).to_list(500)
    return members


@team_router.delete("/members/{user_id}")
async def remove_member(user_id: str, ctx: dict = Depends(require_tenant)):
    if (ctx["user"].get("role") or "").lower() != "admin":
        raise HTTPException(403, "Only tenant admins can remove members")
    if user_id == ctx["user"]["id"]:
        raise HTTPException(400, "Cannot remove yourself; ask another admin")
    db = get_db()
    user = await db.users.find_one({"id": user_id, "tenant_id": ctx["tenant_id"]}, {"_id": 0, "id": 1})
    if not user:
        raise HTTPException(404, "Member not found")
    await db.tenant_users.update_many({"user_id": user_id, "tenant_id": ctx["tenant_id"]},
                                      {"$set": {"status": "revoked"}})
    await db.users.delete_one({"id": user_id, "tenant_id": ctx["tenant_id"]})
    return {"ok": True}


# ============================================================
# Admin password reset (followup-fix 2026-05-08)
# ============================================================
# When a user gets locked out (e.g. password hash drift), the previous
# code had no recovery path -- accept-invite refuses if the email is
# already registered, and login refuses on hash mismatch. The only fix
# was direct DB intervention. Adding a tenant-admin endpoint so the
# operator can recover any teammate (including themselves, via another
# admin) without DB access. CLI fallback (scripts/reset_password.py)
# exists for the case where every admin is locked out.

class PasswordResetPayload(StrictModel):
    new_password: str = Field(..., min_length=8, max_length=128)


@team_router.post("/members/{user_id}/reset-password")
async def admin_reset_password(
    user_id: str, payload: PasswordResetPayload,
    ctx: dict = Depends(require_tenant),
):
    """Tenant-admin only. Sets a user's password_hash to a freshly-
    bcrypt'd version of the provided plaintext. Audit-logged."""
    if (ctx["user"].get("role") or "").lower() != "admin":
        raise HTTPException(403, "Only tenant admins can reset member passwords")
    db = get_db()
    user = await db.users.find_one(
        {"id": user_id, "tenant_id": ctx["tenant_id"]},
        {"_id": 0, "id": 1, "email": 1, "tenant_id": 1, "password_hash": 1},
    )
    if not user:
        raise HTTPException(404, "Member not found in this tenant")

    new_hash = hash_password(payload.new_password)
    # Capture the BEFORE-prefix so the audit log shows the old hash got
    # rotated without storing the plaintext or the full hash.
    before_prefix = (user.get("password_hash") or "")[:7] if user.get("password_hash") else None
    await db.users.update_one(
        {"id": user_id, "tenant_id": ctx["tenant_id"]},
        {"$set": {"password_hash": new_hash,
                   "password_reset_at": utc_now().isoformat(),
                   "password_reset_by": ctx["user"]["id"]}},
    )
    # Bearer JWTs are stateless and will expire naturally; document that
    # in the response so the operator knows tokens issued before the
    # reset remain valid until expiry. (The Google-OAuth cookie session
    # path was removed in the 2026-05-11 purge, so there's no longer
    # anything else to invalidate here.)
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="users",
        op="ADMIN_PASSWORD_RESET",
        record_id=user_id,
        before={"hash_prefix": before_prefix},
        after={"hash_prefix": new_hash[:7],
                "reset_by": ctx["user"]["id"],
                "reset_user_email": user.get("email")},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {
        "ok": True,
        "user_id": user_id,
        "email": user.get("email"),
        "note": "User must re-login with the new password. Active JWTs "
                "remain valid until expiry.",
    }



# ============================================================
# Close-tenant endpoint (2026-02-XX).
# Wipes every collection scoped to the tenant_id, including the
# tenant record + users + memberships + invites. Email addresses
# of the users in this tenant become eligible to sign up a fresh
# new organization immediately after. Admin only.
#
# Irreversible. Returns deletion counts per collection.
# ============================================================
TENANT_SCOPED_COLLECTIONS = [
    "projects", "tasks", "floors", "locations", "resources",
    "daily_reports", "daily_report_activities", "daily_presence",
    "delays", "rfis", "safety_events", "qc_issues", "deliveries",
    "coordination_events", "photos", "specs", "submittals", "drawings",
    "schedule_imports", "schedule_revisions", "recovery_reports",
    "marketplace_listings", "notifications", "approvals", "audit_log",
    "team_invites", "users", "memberships", "tia_generations",
    "edit_requests", "photo_tray", "subcontractors", "suppliers",
]


@router.post("/close-account")
async def close_account(ctx: dict = Depends(require_tenant)):
    """Permanently close (delete) the current tenant and every
    collection record scoped to it. Admin only. The user's email +
    password are removed alongside, so they can sign up again with
    the same email to start a brand-new organization.
    """
    role = (ctx["user"].get("role") or "").lower()
    if role != "admin":
        raise HTTPException(403, "Only the tenant admin can close the account")
    db = get_db()
    tenant_id = ctx["tenant_id"]
    tenant = await db.tenants.find_one({"id": tenant_id}, {"_id": 0})
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    # Audit row first so the close intent survives the cascade. We
    # write it INTO the same audit_log collection that's about to be
    # wiped — that's fine; the act of closing implies all history goes
    # too, but the response below captures the deleted_counts for the
    # admin's records.
    await write_audit(
        tenant_id=tenant_id, table_name="tenants", op="CLOSE_TENANT",
        record_id=tenant_id, before=tenant, after={},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    deleted_counts: dict[str, int] = {}
    for coll in TENANT_SCOPED_COLLECTIONS:
        try:
            res = await db[coll].delete_many({"tenant_id": tenant_id})
            deleted_counts[coll] = res.deleted_count
        except Exception:
            deleted_counts[coll] = -1
    await db.tenants.delete_one({"id": tenant_id})
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "cascaded": deleted_counts,
        "note": "Account closed. Your email is now free to sign up a new organization.",
    }



# ============================================================
# Self-service password reset (2026-02-XX, Abhi).
#
# Public flow, two endpoints:
#   POST /api/auth/forgot-password  { email }
#     -- Always returns 200. Generates a 32-byte URL-safe token; stores
#        a SHA-256 HASH of it on the user doc with a 30-min expiry;
#        emails the plain token to the recipient inside the reset URL.
#        Account enumeration is mitigated by ALWAYS responding 200
#        regardless of whether the email matches a user.
#
#   POST /api/auth/reset-password   { token, new_password }
#     -- Verifies the token's hash matches a non-expired entry; sets
#        a fresh bcrypt password_hash; invalidates the reset token
#        (one-time use) and stamps password_reset_at/by="self".
#
# Why hash the token at rest:
#   A DB dump must not let an attacker forge resets. The plain token
#   only ever exists in transit (email body) and ephemerally in the
#   user's URL bar after they click. The server only stores SHA-256.
# ============================================================
import hashlib  # noqa: E402
from services.password_reset_email import send_password_reset_email  # noqa: E402


_RESET_TOKEN_TTL_MINUTES = 30


class ForgotPasswordRequest(StrictModel):
    email: str = Field(..., min_length=3, max_length=320)


class ResetPasswordRequest(StrictModel):
    token: str = Field(..., min_length=20, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=200)


def _hash_reset_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _frontend_base_url(request: Request) -> str:
    """Mirror the invite-flow resolution so reset links land on the
    same origin the user is already using."""
    base_url = ""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if origin:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            if parsed.scheme and parsed.netloc:
                base_url = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            base_url = ""
    if not base_url:
        base_url = (
            os.environ.get("INVITE_BASE_URL")
            or os.environ.get("REACT_APP_BACKEND_URL")
            or ""
        ).rstrip("/")
    if base_url.endswith("/api"):
        base_url = base_url[:-4]
    return base_url


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, request: Request):
    """Always 200 -- account enumeration is prevented by returning the
    same shape whether or not the email matches a user. Email delivery
    is best-effort; failure is audited but never returned to the caller.
    """
    db = get_db()
    email_norm = (payload.email or "").strip().lower()
    user = await db.users.find_one({"email": email_norm}, {"_id": 0})

    plain_token = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(plain_token)
    expires_at = utc_now() + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES)

    if user:
        await db.users.update_one(
            {"id": user["id"]},
            {"$set": {
                "password_reset_token_hash": token_hash,
                "password_reset_expires_at": expires_at.isoformat(),
                "password_reset_requested_at": utc_now().isoformat(),
            }},
        )
        base_url = _frontend_base_url(request)
        reset_link = (
            f"{base_url}/reset-password?token={plain_token}" if base_url
            else f"/reset-password?token={plain_token}"
        )
        email_result = send_password_reset_email(
            recipient_email=user["email"],
            recipient_name=user.get("full_name"),
            reset_token=plain_token,
            reset_link=reset_link,
            expires_minutes=_RESET_TOKEN_TTL_MINUTES,
        )
        op = "PASSWORD_RESET_EMAIL_SENT" if email_result.get("sent") else "PASSWORD_RESET_EMAIL_FAILED"
        try:
            await write_audit(
                tenant_id=user.get("tenant_id"),
                table_name="users", op=op, record_id=user["id"],
                before={}, after={
                    "email": user["email"],
                    "provider_message_id": email_result.get("provider_message_id"),
                    "error": email_result.get("error"),
                },
                actor_id=user["id"],
                request_id=str(request.headers.get("x-request-id") or ""),
            )
        except Exception:
            pass

    return {
        "ok": True,
        "message": (
            "If an account exists for that email, a password-reset link "
            "has been sent. Check your inbox (and spam folder)."
        ),
    }


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest, request: Request):
    """Consume a reset token + set a new password. One-time use; the
    token is invalidated on success even if the user immediately
    retries. Returns 400 on bad/expired/used token without revealing
    which of those it was.
    """
    db = get_db()
    token_hash = _hash_reset_token(payload.token)
    user = await db.users.find_one(
        {"password_reset_token_hash": token_hash},
        {"_id": 0},
    )
    bad_token_msg = "Reset link is invalid or has expired. Request a new one."
    if not user:
        raise HTTPException(400, bad_token_msg)
    expires_at_raw = user.get("password_reset_expires_at")
    if not expires_at_raw:
        raise HTTPException(400, bad_token_msg)
    try:
        if expires_at_raw.endswith("Z"):
            expires_at_raw = expires_at_raw[:-1] + "+00:00"
        expires_at = datetime.fromisoformat(expires_at_raw)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except Exception:
        raise HTTPException(400, bad_token_msg)
    if expires_at < utc_now():
        await db.users.update_one(
            {"id": user["id"]},
            {"$unset": {
                "password_reset_token_hash": "",
                "password_reset_expires_at": "",
            }},
        )
        raise HTTPException(400, bad_token_msg)

    new_hash = hash_password(payload.new_password)
    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "password_hash": new_hash,
                "password_reset_at": utc_now().isoformat(),
                "password_reset_by": "self",
            },
            "$unset": {
                "password_reset_token_hash": "",
                "password_reset_expires_at": "",
                "password_reset_requested_at": "",
            },
        },
    )
    try:
        await write_audit(
            tenant_id=user.get("tenant_id"),
            table_name="users", op="PASSWORD_RESET_SELF",
            record_id=user["id"], before={"email": user["email"]},
            after={"reset_via": "forgot_password_email"},
            actor_id=user["id"],
            request_id=str(request.headers.get("x-request-id") or ""),
        )
    except Exception:
        pass

    return {"ok": True, "email": user["email"]}
