"""Auth router: register (creates org/tenant), login, me, team invites,
admin password reset."""
import asyncio
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field
from database import get_db
from models import (
    UserRegister, UserLogin, User, Tenant, TokenResponse,
    InviteCreate, InviteAccept, MemberAccessUpdate, StrictModel,
    gen_id, utc_now,
)
from auth import (
    hash_password, verify_password, create_token, require_tenant,
    token_version_of, dummy_password_check, redact_secrets, INVITE_SECRET_KEYS,
    check_throttle, record_failure, clear_failures, client_ip,
    reserve_attempt, release_attempt,
    ACCOUNT_FAILURE_LIMIT, ACCOUNT_TOTAL_FAILURE_LIMIT, IP_FAILURE_LIMIT,
    effective_access_scope,
)
from audit import write_audit
from services.project_access import (
    is_full_admin, require_full_admin, ensure_keeps_full_admin,
    allowed_project_ids, role_on_project_for, has_full_admin, LAST_FULL_ADMIN,
)

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


def _public_signup_allowed() -> bool:
    return (os.environ.get("ALLOW_PUBLIC_SIGNUP") or "").strip().lower() in {"1", "true", "yes"}


@router.post("/register", response_model=TokenResponse)
async def register(payload: UserRegister):
    """Public signup. Creates a fresh tenant + makes the registrant its admin.

    For team-member onboarding into an existing tenant use POST /auth/accept-invite.

    Day-1 fix (2026-10-25, audit finding 2): public sign-up is OFF unless
    ALLOW_PUBLIC_SIGNUP is 1/true/yes. Anyone who signs up becomes admin of
    a new company on this shared server, and a new super who taps
    "Create account" by mistake ends up in an empty company.
    """
    if not _public_signup_allowed():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Sign-up is by invitation only. Ask your company admin to invite you.",
        )
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
async def login(payload: UserLogin, request: Request):
    """2026-11 security round:
      * throttled -- 10 failed tries per account from one client address,
        100 per account across all addresses, and 50 per client address in
        15 minutes, then 429 with Retry-After; a good sign-in clears the
        account's count. (2026-11-16: the account limit used to be 10 from
        anywhere, so a stranger could lock a supervisor out of their own
        account by typing wrong passwords from another address.)
      * an unknown email still runs one bcrypt check so the reply takes
        the same time as a wrong password (no account discovery by timing).
      * the token carries the user's token_version ("tv")."""
    db = get_db()
    email_norm = payload.email.lower()
    ip = client_ip(request)
    account_key = f"login:acct:{email_norm}"
    account_ip_key = f"login:acctip:{email_norm}|{ip}"
    ip_key = f"login:ip:{ip}"
    await check_throttle([(ip_key, IP_FAILURE_LIMIT)])
    # The account's rows are written before the password check, so a burst
    # of simultaneous guesses cannot all slip past the limits; a good
    # sign-in clears them below.
    await reserve_attempt([(account_ip_key, ACCOUNT_FAILURE_LIMIT),
                           (account_key, ACCOUNT_TOTAL_FAILURE_LIMIT)])
    user = await db.users.find_one({"email": email_norm}, {"_id": 0})
    if not user:
        dummy_password_check(payload.password)
        ok = False
    else:
        ok = verify_password(payload.password, user.get("password_hash", ""))
    if not ok:
        await record_failure([ip_key])
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    if not tenant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tenant not found")
    await clear_failures([account_key, account_ip_key])
    user_view = {k: v for k, v in user.items() if k != "password_hash"}
    token = create_token(user["id"], tenant["id"], user["email"], token_version_of(user))
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
async def accept_invite(payload: InviteAccept, request: Request):
    """Public route — code + new password produces an account joined to the
    tenant that issued the code. Code is single-use; expires after 14 days.

    2026-11 security round: wrong codes / wrong emails are throttled like
    sign-in (10 per email from one address, 100 per email from anywhere,
    50 per client address, per 15 minutes).

    2026-11-16: the invite's project access is applied -- access_scope on
    the membership (and the user record), and one project_members row per
    project chosen on the invite that still exists. Invites made before
    this carry neither field: admins get "all", everyone else no projects
    (the same as before)."""
    db = get_db()
    ip = client_ip(request)
    email_key = f"invite:acct:{payload.email.lower()}"
    email_ip_key = f"invite:acctip:{payload.email.lower()}|{ip}"
    ip_key = f"invite:ip:{ip}"
    await check_throttle([(ip_key, IP_FAILURE_LIMIT)])
    rid = await reserve_attempt([(email_ip_key, ACCOUNT_FAILURE_LIMIT),
                                 (email_key, ACCOUNT_TOTAL_FAILURE_LIMIT)])
    invite = await db.team_invites.find_one({"code": payload.code, "status": "pending"}, {"_id": 0})
    if not invite:
        await record_failure([ip_key])
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found or already used")
    if invite["email"].lower() != payload.email.lower():
        await record_failure([ip_key])
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Email does not match the invite")
    # Right code for the right email: not a guess, so it does not count.
    await release_attempt(rid)
    expires = invite.get("expires_at")
    if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
        await db.team_invites.update_one({"id": invite["id"]}, {"$set": {"status": "expired"}})
        raise HTTPException(status.HTTP_410_GONE, "Invite expired")
    if await db.users.find_one({"email": payload.email.lower()}, {"_id": 0, "id": 1}):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered — sign in instead")

    tenant = await db.tenants.find_one({"id": invite["tenant_id"]}, {"_id": 0})
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant for this invite no longer exists")

    role = invite.get("role") or "super"
    scope = effective_access_scope(role, invite.get("access_scope"))
    user_id = gen_id()
    user_doc = {
        "id": user_id,
        "email": payload.email.lower(),
        "full_name": payload.full_name,
        "tenant_id": tenant["id"],
        "role": role,
        "access_scope": scope,
        "password_hash": hash_password(payload.password),
        "created_at": utc_now().isoformat(),
    }
    await db.users.insert_one(user_doc.copy())
    await db.tenant_users.insert_one({
        "id": gen_id(),
        "user_id": user_id,
        "tenant_id": tenant["id"],
        "role": role,
        "access_scope": scope,
        "status": "active",
        "created_at": utc_now().isoformat(),
    })
    await db.team_invites.update_one({"id": invite["id"]}, {"$set": {
        "status": "accepted",
        "accepted_at": utc_now().isoformat(),
        "accepted_user_id": user_id,
    }})
    if scope != "all" and invite.get("project_ids"):
        await _assign_projects(db, tenant["id"], user_id, role,
                               list(invite.get("project_ids") or []),
                               added_by=invite.get("invited_by"))

    user_view = {k: v for k, v in user_doc.items() if k != "password_hash"}
    token = create_token(user_id, tenant["id"], payload.email.lower())
    return TokenResponse(access_token=token, user=User(**user_view), tenant=Tenant(**tenant))


# ----- Authenticated team management -----
def _invite_is_expired(invite: dict) -> bool:
    expires = invite.get("expires_at")
    if not expires:
        return False
    try:
        exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp < datetime.now(timezone.utc)
    except Exception:
        return False


def _invite_base_url(request: Request) -> str:
    """Base URL for invite links. Day-1 fix (audit findings 31/54/68): use
    the same allowlisted resolver as password reset (_frontend_base_url:
    the request Origin only if it is in INVITE_BASE_URL / ALLOWED_FRONTEND_
    HOSTS / REACT_APP_BACKEND_URL / PRODUCTION_URL, else INVITE_BASE_URL)
    instead of trusting the raw Origin header.

    Safety net: when NONE of those env vars is set, the resolver returns ""
    and the emailed link would be a dead relative path. Only in that case do
    we fall back to the request's own Origin/Referer (the old behavior), and
    log a warning telling the operator to set INVITE_BASE_URL."""
    base_url = _frontend_base_url(request)
    if base_url:
        return base_url
    # Review fix: _frontend_base_url only falls back to INVITE_BASE_URL /
    # REACT_APP_BACKEND_URL. If the operator set only PRODUCTION_URL or
    # ALLOWED_FRONTEND_HOSTS, use the first of those (operator-configured,
    # so safe) rather than emailing a dead relative link.
    for raw in [os.environ.get("PRODUCTION_URL") or ""] + (
        os.environ.get("ALLOWED_FRONTEND_HOSTS") or ""
    ).split(","):
        v = (raw or "").strip().rstrip("/")
        if v.endswith("/api"):
            v = v[:-4]
        if v.startswith(("https://", "http://")):
            return v
    configured = any(
        (os.environ.get(k) or "").strip()
        for k in ("INVITE_BASE_URL", "REACT_APP_BACKEND_URL",
                  "PRODUCTION_URL", "ALLOWED_FRONTEND_HOSTS")
    )
    if configured:
        return ""
    from urllib.parse import urlparse
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    try:
        u = urlparse(origin)
        if u.scheme and u.netloc:
            import logging
            logging.getLogger("auth").warning(
                "Invite link host taken from the browser address (%s://%s) because "
                "INVITE_BASE_URL is not set. Set INVITE_BASE_URL to the production "
                "address so invite links never point at a preview copy.",
                u.scheme, u.netloc,
            )
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return ""


async def _send_invite_email_and_audit(db, ctx: dict, request: Request, invite: dict):
    """Build the invite link, send the invite email and write the
    INVITE_EMAIL_SENT / INVITE_EMAIL_FAILED audit row. Shared by a new
    invite and a re-send of a pending one. Never raises on email failure.
    Returns (email_result, invite_link)."""
    from services.invite_email import send_invite_email
    tenant_row = await db.tenants.find_one(
        {"id": ctx["tenant_id"]},
        {"_id": 0, "display_name": 1, "legal_name": 1, "name": 1},
    )
    # Tenants store display_name / legal_name (see register); "name" is
    # only a fallback for any older row that used it.
    tenant_row = tenant_row or {}
    tenant_name = (tenant_row.get("display_name") or tenant_row.get("legal_name")
                   or tenant_row.get("name") or "your team")
    base_url = _invite_base_url(request)
    code = invite["code"]
    invite_link = f"{base_url}/accept-invite?code={code}" if base_url else f"/accept-invite?code={code}"

    email_result = send_invite_email(
        tenant_name=tenant_name,
        invitee_email=invite["email"],
        role=invite.get("role"),
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
                "email": invite["email"],
                "role": invite.get("role"),
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
    return email_result, invite_link


# ------------------------------------------------------------
# Per-project access helpers (2026-11-16)
# ------------------------------------------------------------
async def _validated_project_ids(db, ctx: dict, project_ids) -> list[str]:
    """Clean a requested project list: no blanks or repeats, every id must
    be a project of the caller's company (422 naming the unknown ones), and a
    caller limited to specific projects may only hand out their own."""
    ids: list[str] = []
    for pid in project_ids or []:
        if isinstance(pid, str) and pid.strip() and pid.strip() not in ids:
            ids.append(pid.strip())
    if not ids:
        return []
    rows = await db.projects.find(
        {"tenant_id": ctx["tenant_id"], "id": {"$in": ids}}, {"_id": 0, "id": 1},
    ).to_list(length=None)
    found = {r.get("id") for r in rows}
    missing = [pid for pid in ids if pid not in found]
    if missing:
        raise HTTPException(422, f"These projects are not in your company: {', '.join(missing)}")
    mine = await allowed_project_ids(ctx)
    if mine is not None:
        outside = [pid for pid in ids if pid not in mine]
        if outside:
            raise HTTPException(403, "You can only give access to projects you are assigned to.")
    return ids


async def _assign_projects(db, tenant_id: str, user_id: str, role: str,
                           project_ids: list[str], *, added_by: str | None) -> list[str]:
    """Add a project_members row for each project the user is not on yet.
    Only projects that exist in the company are used. Returns the ids added."""
    if not project_ids:
        return []
    rows = await db.projects.find(
        {"tenant_id": tenant_id, "id": {"$in": list(project_ids)}}, {"_id": 0, "id": 1},
    ).to_list(length=None)
    real = {r.get("id") for r in rows}
    existing = await db.project_members.find(
        {"tenant_id": tenant_id, "user_id": user_id}, {"_id": 0, "project_id": 1},
    ).to_list(length=None)
    have = {r.get("project_id") for r in existing}
    added = []
    for pid in project_ids:
        if pid not in real or pid in have or pid in added:
            continue
        doc = {
            "id": gen_id(), "tenant_id": tenant_id, "project_id": pid,
            "user_id": user_id, "role_on_project": role_on_project_for(role),
            "added_at": utc_now().isoformat(), "added_by": added_by,
        }
        try:
            await db.project_members.insert_one(doc)
            added.append(pid)
        except Exception:
            # Unique index race: someone added the same row a moment ago.
            pass
    return added


async def _is_company_member(db, tenant_id: str, user_id: str) -> dict | None:
    """The user record when the user belongs to this company (home company,
    or an active membership); otherwise None."""
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not user:
        return None
    if user.get("tenant_id") == tenant_id:
        return user
    tu = await db.tenant_users.find_one(
        {"tenant_id": tenant_id, "user_id": user_id, "status": "active"}, {"_id": 0, "id": 1},
    )
    return user if tu else None


@team_router.post("/invites")
async def create_invite(payload: InviteCreate, request: Request, ctx: dict = Depends(require_tenant)):
    # 2026-11-16: inviting is a company-wide control -- admins limited to
    # specific projects cannot invite.
    require_full_admin(ctx, "Only an admin with access to all projects can invite teammates")
    db = get_db()
    invite_scope = effective_access_scope(payload.role, payload.access_scope)
    invite_projects = await _validated_project_ids(db, ctx, payload.project_ids)
    if invite_scope == "all":
        invite_projects = []
    elif (payload.role or "").strip().lower() == "admin" and not invite_projects:
        # A limited admin is held to their projects from day one, so with
        # none they would see nothing.
        raise HTTPException(422, "An admin limited to specific projects needs at least one project. Tick a project, or choose All projects.")
    if await db.users.find_one({"email": payload.email.lower(), "tenant_id": ctx["tenant_id"]}, {"_id": 0, "id": 1}):
        raise HTTPException(409, "User with this email is already a member")
    pending = await db.team_invites.find_one({
        "email": payload.email.lower(), "tenant_id": ctx["tenant_id"], "status": "pending",
    }, {"_id": 0})
    if pending and _invite_is_expired(pending):
        # Day-1 fix (audit finding 26): an expired code used to be handed
        # back as-is, so the super hit "Invite expired" on the jobsite.
        # Mark it expired and fall through to issue a fresh code + email.
        await db.team_invites.update_one(
            {"id": pending["id"], "tenant_id": ctx["tenant_id"], "status": "pending"},
            {"$set": {"status": "expired"}},
        )
        pending = None
    if pending:
        # Day-1 fix (audit finding 26): "Send invite" again for the same
        # address now actually re-sends the email. The stored role is kept
        # (to change it, revoke and invite again) -- the role is returned so
        # the page can show it.
        email_result, invite_link = await _send_invite_email_and_audit(db, ctx, request, pending)
        return {
            "invite": pending,
            "already_existed": True,
            "invite_link": invite_link,
            "email_sent": bool(email_result.get("sent")),
            "email_error": email_result.get("error"),
        }

    code = secrets.token_urlsafe(9)[:12]
    expires_at = (utc_now() + timedelta(days=14)).isoformat()
    invite = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "email": payload.email.lower(),
        "role": payload.role,
        "access_scope": invite_scope,
        "project_ids": invite_projects,
        "code": code,
        "status": "pending",
        "invited_by": ctx["user"]["id"],
        "invited_at": utc_now().isoformat(),
        "expires_at": expires_at,
        "accepted_at": None,
        "accepted_user_id": None,
    }
    await db.team_invites.insert_one(invite.copy())
    # 2026-11 security round: the audit row must never hold the invite
    # code (anyone who could read the log could claim the invite).
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="team_invites", op="INSERT",
        record_id=invite["id"], before=None,
        after=redact_secrets(invite, INVITE_SECRET_KEYS),
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    # Bug #47 (2026-05-14) -- send the invite email via SendGrid (or
    # whatever EMAIL_PROVIDER selects). Sequence: invite row -> audit
    # op -> email send -> response. Email failure NEVER blocks the
    # response -- the invite_code is already persisted and the admin
    # can fall back to copy-paste the invite link from the UI.
    email_result, invite_link = await _send_invite_email_and_audit(db, ctx, request, invite)

    return {
        "invite": invite,
        "already_existed": False,
        "invite_link": invite_link,
        # Bug #47: surface email-send status to the frontend so the
        # Team page can toast "Invite sent" vs "Invite created -- copy
        # the link manually". The invite link itself remains visible
        # in the Invites list regardless.
        "email_sent": bool(email_result.get("sent")),
        "email_error": email_result.get("error"),
    }


@team_router.get("/invites")
async def list_invites(ctx: dict = Depends(require_tenant)):
    """Day-1 fix (audit findings 13/17): only admins and PMs see invites,
    and only admins see the redeemable invite code (anyone holding a code
    plus the invited email could claim that invite, including an admin
    one). Other roles get an empty list rather than a 403, because the
    Team page loads members and invites together and a 403 would blank
    the whole page (members list included) for the super."""
    role = (ctx["user"].get("role") or "").strip().lower()
    if role not in ("admin", "pm"):
        return []
    db = get_db()
    docs = await db.team_invites.find(
        {"tenant_id": ctx["tenant_id"]}, {"_id": 0},
    ).sort("invited_at", -1).to_list(200)
    for d in docs:
        # Audit finding 26: a pending invite past its expiry used to show
        # as "pending" with a dead code until someone tried it.
        if d.get("status") == "pending" and _invite_is_expired(d):
            d["status"] = "expired"
        if not is_full_admin(ctx):
            d.pop("code", None)
    return docs


@team_router.delete("/invites/{invite_id}")
async def revoke_invite(invite_id: str, ctx: dict = Depends(require_tenant)):
    require_full_admin(ctx, "Only an admin with access to all projects can revoke invites")
    db = get_db()
    res = await db.team_invites.update_one(
        {"id": invite_id, "tenant_id": ctx["tenant_id"], "status": "pending"},
        {"$set": {"status": "revoked", "revoked_at": utc_now().isoformat(), "revoked_by": ctx["user"]["id"]}},
    )
    if not res.matched_count:
        raise HTTPException(404, "Pending invite not found")
    return {"ok": True}


# 2026-11 security round: an explicit list of the member fields the Team
# page uses (id, email, full_name, role, created_at) plus harmless profile
# fields. Everything else on the user record -- password hash, reset-token
# hash and stamps, token_version -- stays on the server.
_MEMBER_FIELDS = (
    "id", "email", "full_name", "role", "tenant_id", "created_at",
    "title", "phone", "status", "active_project_id",
)
# Added by list_members from the access records (2026-11-16), not copied
# from the user record.
_MEMBER_ACCESS_FIELDS = ("access_scope", "project_count", "project_ids")


@team_router.get("/members")
async def list_members(ctx: dict = Depends(require_tenant)):
    """2026-11-16: each member also carries access_scope ("all" or
    "assigned") and project_count; all-projects admins also get project_ids
    (for the edit dialog)."""
    db = get_db()
    projection = {"_id": 0, **{f: 1 for f in _MEMBER_FIELDS}, "access_scope": 1}
    members = await db.users.find(
        {"tenant_id": ctx["tenant_id"]}, projection,
    ).sort("created_at", 1).to_list(500)
    ids = [m.get("id") for m in members if m.get("id")]
    scopes: dict = {}
    projects: dict = {}
    try:
        rows = await db.tenant_users.find(
            {"tenant_id": ctx["tenant_id"], "user_id": {"$in": ids}},
            {"_id": 0, "user_id": 1, "access_scope": 1},
        ).to_list(length=None)
        scopes = {r.get("user_id"): r.get("access_scope") for r in rows}
        pm_rows = await db.project_members.find(
            {"tenant_id": ctx["tenant_id"], "user_id": {"$in": ids}},
            {"_id": 0, "user_id": 1, "project_id": 1},
        ).to_list(length=None)
        for r in pm_rows:
            projects.setdefault(r.get("user_id"), []).append(r.get("project_id"))
    except Exception:
        pass
    full = is_full_admin(ctx)
    out = []
    for m in members:
        # Filter again in Python as well, so nothing else can slip through.
        row = {k: v for k, v in m.items() if k in _MEMBER_FIELDS}
        uid = m.get("id")
        row["access_scope"] = effective_access_scope(
            m.get("role"), scopes.get(uid) or m.get("access_scope"))
        row["project_count"] = len(projects.get(uid) or [])
        if full:
            row["project_ids"] = sorted(p for p in (projects.get(uid) or []) if p)
        out.append(row)
    return out


@team_router.patch("/members/{user_id}/access")
async def update_member_access(user_id: str, payload: MemberAccessUpdate,
                               ctx: dict = Depends(require_tenant)):
    """Change a member's role, an admin's scope ("all" / "assigned") and/or
    the member's project list. All-projects admins only. Refuses (409) to
    leave the company without an admin who has access to all projects."""
    require_full_admin(ctx, "Only an admin with access to all projects can change team access")
    db = get_db()
    tid = ctx["tenant_id"]
    target = await _is_company_member(db, tid, user_id)
    if not target:
        raise HTTPException(404, "Member not found")
    is_home = target.get("tenant_id") == tid
    membership = await db.tenant_users.find_one(
        {"tenant_id": tid, "user_id": user_id}, {"_id": 0},
    ) or {}
    old_role = ((target.get("role") if is_home else membership.get("role")) or "").strip().lower()
    old_scope = effective_access_scope(
        old_role, membership.get("access_scope") or (target.get("access_scope") if is_home else None))
    new_role = (payload.role or old_role).strip().lower()
    if payload.access_scope is not None:
        new_scope = effective_access_scope(new_role, payload.access_scope)
    elif new_role == "admin" and old_role != "admin":
        new_scope = "all"  # a new admin starts with every project (the default)
    else:
        new_scope = effective_access_scope(new_role, old_scope)
    await ensure_keeps_full_admin(db, tid, user_id, new_role=new_role, new_scope=new_scope)
    new_projects = None
    if payload.project_ids is not None:
        new_projects = await _validated_project_ids(db, ctx, payload.project_ids)
    if new_role == "admin" and new_scope == "assigned":
        if new_projects is not None:
            has_projects = bool(new_projects)
        else:
            has_projects = bool(await db.project_members.find_one(
                {"tenant_id": tid, "user_id": user_id}, {"_id": 0, "id": 1}))
        if not has_projects:
            raise HTTPException(422, "An admin limited to specific projects needs at least one project. Tick a project, or choose All projects.")

    sets = {"access_scope": new_scope}
    if new_role != old_role:
        sets["role"] = new_role
    if is_home:
        await db.users.update_one({"id": user_id, "tenant_id": tid}, {"$set": sets})
    await db.tenant_users.update_one(
        {"tenant_id": tid, "user_id": user_id}, {"$set": sets},
    )
    was_full = old_role == "admin" and old_scope == "all"
    if was_full and not (new_role == "admin" and new_scope == "all"):
        # Check again now that the change is saved, in case another admin
        # was limited or removed at the same moment; if nobody with access
        # to all projects is left, put this member back and refuse.
        if not await has_full_admin(db, tid):
            def _restore(original: dict) -> dict:
                back_set = {k: original[k] for k in sets if original.get(k) is not None}
                back_unset = {k: "" for k in sets if original.get(k) is None}
                upd: dict = {}
                if back_set:
                    upd["$set"] = back_set
                if back_unset:
                    upd["$unset"] = back_unset
                return upd
            if is_home:
                await db.users.update_one({"id": user_id, "tenant_id": tid}, _restore(target))
            if membership:
                await db.tenant_users.update_one(
                    {"tenant_id": tid, "user_id": user_id}, _restore(membership))
            raise HTTPException(409, LAST_FULL_ADMIN)
    added: list = []
    removed: list = []
    if new_projects is not None:
        current = await db.project_members.find(
            {"tenant_id": tid, "user_id": user_id}, {"_id": 0, "project_id": 1, "id": 1},
        ).to_list(length=None)
        keep = set(new_projects)
        for row in current:
            if row.get("project_id") not in keep:
                await db.project_members.delete_one({"tenant_id": tid, "id": row.get("id")})
                removed.append(row.get("project_id"))
        added = await _assign_projects(db, tid, user_id, new_role, new_projects,
                                       added_by=ctx["user"]["id"])
    await write_audit(
        tenant_id=tid, table_name="tenant_users", op="MEMBER_ACCESS_CHANGED",
        record_id=user_id,
        before={"role": old_role, "access_scope": old_scope},
        after={"role": new_role, "access_scope": new_scope,
               "projects_added": added, "projects_removed": removed},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    count = await db.project_members.count_documents({"tenant_id": tid, "user_id": user_id})
    return {"ok": True, "user_id": user_id, "role": new_role, "access_scope": new_scope,
            "project_count": count, "projects_added": added, "projects_removed": removed}


@team_router.delete("/members/{user_id}")
async def remove_member(user_id: str, ctx: dict = Depends(require_tenant)):
    require_full_admin(ctx, "Only an admin with access to all projects can remove members")
    if user_id == ctx["user"]["id"]:
        raise HTTPException(400, "Cannot remove yourself; ask another admin")
    db = get_db()
    user = await db.users.find_one({"id": user_id, "tenant_id": ctx["tenant_id"]}, {"_id": 0, "id": 1})
    if not user:
        raise HTTPException(404, "Member not found")
    target_is_full = await ensure_keeps_full_admin(db, ctx["tenant_id"], user_id, removing=True)
    if target_is_full:
        # Mark the admin as being removed (no longer counted), then check
        # again, so two admins removing each other at the same moment cannot
        # leave the company with no admin who has access to all projects.
        await db.users.update_one({"id": user_id, "tenant_id": ctx["tenant_id"]},
                                  {"$set": {"removal_pending": True}})
        if not await has_full_admin(db, ctx["tenant_id"]):
            await db.users.update_one({"id": user_id, "tenant_id": ctx["tenant_id"]},
                                      {"$unset": {"removal_pending": ""}})
            raise HTTPException(409, LAST_FULL_ADMIN)
    await db.tenant_users.update_many({"user_id": user_id, "tenant_id": ctx["tenant_id"]},
                                      {"$set": {"status": "revoked"}})
    # Retire the removed member's sign-in tokens. Deleting the record next
    # already blocks them; this keeps that true even if removal ever
    # becomes a soft delete.
    await db.users.update_one({"id": user_id, "tenant_id": ctx["tenant_id"]},
                              {"$inc": {"token_version": 1}})
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
    require_full_admin(ctx, "Only an admin with access to all projects can reset member passwords")
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
                   "password_reset_by": ctx["user"]["id"]},
         # 2026-11 security round: bumping token_version signs the member
         # out everywhere -- every token issued before the reset stops working.
         "$inc": {"token_version": 1}},
    )
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
        "note": "User must sign in again with the new password. Every "
                "earlier sign-in for this user has been signed out.",
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
    # The audit log's per-company chain head goes with the log itself, so
    # nothing keeps pointing at rows that no longer exist.
    "audit_chain_heads",
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
    # 2026-11-16: closing the company is company-wide, so an admin limited
    # to specific projects cannot do it.
    require_full_admin(ctx, "Only an admin with access to all projects can close the account")
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
        record_id=tenant_id, before=redact_secrets(tenant), after={},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # 2026-11 security round: sign out everyone attached to this company.
    # Users whose home is this company are deleted below; members from
    # other companies keep their accounts, but their memberships here are
    # closed and their old tokens retired.
    try:
        member_rows = await db.tenant_users.find(
            {"tenant_id": tenant_id}, {"_id": 0, "user_id": 1},
        ).to_list(length=None)
        member_ids = {r.get("user_id") for r in member_rows if r.get("user_id")}
        home_rows = await db.users.find(
            {"tenant_id": tenant_id}, {"_id": 0, "id": 1},
        ).to_list(length=None)
        member_ids |= {r.get("id") for r in home_rows if r.get("id")}
        if member_ids:
            await db.users.update_many(
                {"id": {"$in": sorted(member_ids)}}, {"$inc": {"token_version": 1}},
            )
        await db.tenant_users.update_many(
            {"tenant_id": tenant_id}, {"$set": {"status": "revoked"}},
        )
    except Exception:
        # Never leave a half-closed account because of this step; the
        # deletes below still remove the home users and their tokens.
        pass
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
_RESET_EMAIL_TASKS: set = set()


class ForgotPasswordRequest(StrictModel):
    email: str = Field(..., min_length=3, max_length=320)


class ResetPasswordRequest(StrictModel):
    token: str = Field(..., min_length=20, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=200)


def _hash_reset_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _frontend_base_url(request: Request) -> str:
    """Resolve the base URL for password-reset / invite emails.

    2026-06-XX (Abhi prod-feedback): a stale forked-session preview URL
    (cluster-2.preview.emergentcf.cloud) leaked into an email once and
    produced a 403 Forbidden the next day when that preview pod was
    torn down. The old code trusted Origin / Referer verbatim, which
    meant ANY defunct preview host still open in some cached tab could
    silently poison a live reset link. Fix: only accept the request
    Origin if it matches one of the env-configured allowed hosts;
    otherwise fall back to INVITE_BASE_URL (the authoritative one set
    by the deployment).

    ALLOWED_FRONTEND_HOSTS may be a comma-separated env var listing the
    prod domain + any staging domains. INVITE_BASE_URL is always
    accepted (as it's the deployment's own answer). REACT_APP_BACKEND_URL
    is also accepted for local/dev parity.
    """
    from urllib.parse import urlparse

    def _norm(u: str) -> str:
        u = (u or "").strip().rstrip("/")
        if u.endswith("/api"):
            u = u[:-4]
        return u

    allowed: set[str] = set()
    for key in ("INVITE_BASE_URL", "REACT_APP_BACKEND_URL", "PRODUCTION_URL"):
        v = _norm(os.environ.get(key, ""))
        if v:
            allowed.add(v)
    for host in (os.environ.get("ALLOWED_FRONTEND_HOSTS") or "").split(","):
        v = _norm(host)
        if v:
            allowed.add(v)

    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    origin_base = ""
    if origin:
        try:
            parsed = urlparse(origin)
            if parsed.scheme and parsed.netloc:
                origin_base = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            origin_base = ""

    # Only trust the request's origin if it's on the allowlist -- this
    # prevents defunct / spoofed hosts from poisoning email links.
    if origin_base and origin_base in allowed:
        return origin_base

    # Fallback: the deployment-configured URL. Ordered so INVITE_BASE_URL
    # (explicitly set for emails) wins over REACT_APP_BACKEND_URL.
    return (
        _norm(os.environ.get("INVITE_BASE_URL", ""))
        or _norm(os.environ.get("REACT_APP_BACKEND_URL", ""))
        or ""
    )


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, request: Request):
    """Always 200 -- account enumeration is prevented by returning the
    same shape whether or not the email matches a user. Email delivery
    is best-effort; failure is audited but never returned to the caller.
    """
    db = get_db()
    email_norm = (payload.email or "").strip().lower()
    # 2026-11 security round: limit reset requests. Every request counts,
    # whether or not the email exists, so a 429 reveals nothing: 5 per
    # (email, address), 20 per email from all addresses, and 50 per client
    # address in 15 minutes. The (email, address) bucket means a stranger
    # elsewhere cannot block the real person's reset with 5 requests.
    ip = client_ip(request)
    email_key = f"forgot:acct:{email_norm}"
    email_ip_key = f"forgot:acctip:{email_norm}|{ip}"
    ip_key = f"forgot:ip:{ip}"
    await check_throttle([(ip_key, IP_FAILURE_LIMIT)])
    await reserve_attempt([(email_ip_key, 5), (email_key, 20)])
    await record_failure([ip_key])
    user = await db.users.find_one({"email": email_norm}, {"_id": 0})

    plain_token = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(plain_token)
    expires_at = utc_now() + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES)

    if user:
        # 2026-06-XX -- explicit prior-token invalidation. The $set below
        # already overwrites password_reset_token_hash so any previously
        # emailed link stops working the instant this request lands.
        # We detect a still-live prior token here purely so the audit
        # log records the invalidation (security-review visibility).
        prior_hash = user.get("password_reset_token_hash")
        prior_expires_at_raw = user.get("password_reset_expires_at")
        prior_still_live = False
        if prior_hash and prior_expires_at_raw:
            try:
                _raw = prior_expires_at_raw
                if isinstance(_raw, str) and _raw.endswith("Z"):
                    _raw = _raw[:-1] + "+00:00"
                _exp = datetime.fromisoformat(_raw) if isinstance(_raw, str) else _raw
                if _exp.tzinfo is None:
                    _exp = _exp.replace(tzinfo=timezone.utc)
                prior_still_live = _exp > utc_now()
            except Exception:
                prior_still_live = False

        await db.users.update_one(
            {"id": user["id"]},
            {"$set": {
                "password_reset_token_hash": token_hash,
                "password_reset_expires_at": expires_at.isoformat(),
                "password_reset_requested_at": utc_now().isoformat(),
            }},
        )

        if prior_still_live:
            try:
                await write_audit(
                    tenant_id=user.get("tenant_id"),
                    table_name="users", op="PASSWORD_RESET_TOKEN_INVALIDATED",
                    record_id=user["id"],
                    before={"reason": "superseded_by_new_request"},
                    after={"email": user["email"]},
                    actor_id=user["id"],
                    request_id=str(request.headers.get("x-request-id") or ""),
                )
            except Exception:
                pass
        base_url = _frontend_base_url(request)
        reset_link = (
            f"{base_url}/reset-password?token={plain_token}" if base_url
            else f"/reset-password?token={plain_token}"
        )
        request_id = str(request.headers.get("x-request-id") or "")

        async def _send_and_audit():
            # Sent off the request path: the email provider call is a
            # blocking network call (it would hold up other requests), and a
            # known email must not answer measurably slower than an unknown
            # one.
            try:
                email_result = await asyncio.to_thread(
                    send_password_reset_email,
                    recipient_email=user["email"],
                    recipient_name=user.get("full_name"),
                    reset_token=plain_token,
                    reset_link=reset_link,
                    expires_minutes=_RESET_TOKEN_TTL_MINUTES,
                )
            except Exception as exc:  # never lose the audit row
                email_result = {"sent": False, "error": str(exc)[:200]}
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
                    request_id=request_id,
                )
            except Exception:
                pass

        _task = asyncio.create_task(_send_and_audit())
        _RESET_EMAIL_TASKS.add(_task)  # keep a reference until it finishes
        _task.add_done_callback(_RESET_EMAIL_TASKS.discard)

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
            # Sign out every earlier session (2026-11 security round).
            "$inc": {"token_version": 1},
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
