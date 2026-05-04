"""Google OAuth (Emergent-managed) router.

Coexists with the existing JWT email/password auth. Both auth paths return
the same User+Tenant pair; the auth-context dependency accepts either:
  - Cookie:  session_token=<token>   (Google flow)
  - Header:  Authorization: Bearer <jwt>   (email/password flow)
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
import requests
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from database import get_db
from models import StrictModel, User, Tenant, gen_id, utc_now
from seed import SEED_TENANT_SLUG  # default tenant for first-time Google users

router = APIRouter(prefix="/auth", tags=["auth-google"])

EMERGENT_SESSION_DATA_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
SESSION_COOKIE_NAME = "session_token"
SESSION_TTL_DAYS = 7


class GoogleSessionExchange(StrictModel):
    session_id: str


@router.post("/google/session")
async def google_session_exchange(
    payload: GoogleSessionExchange,
    response: Response,
):
    """Exchange URL-fragment session_id for an httpOnly cookie.

    Frontend reads `#session_id=...` and POSTs it here. Backend calls
    Emergent /session-data with X-Session-ID header, gets the user payload,
    creates or updates the user, opens a 7-day session_token cookie.
    """
    db = get_db()
    try:
        r = requests.get(
            EMERGENT_SESSION_DATA_URL,
            headers={"X-Session-ID": payload.session_id},
            timeout=20,
        )
        r.raise_for_status()
        info = r.json()
    except Exception as e:
        raise HTTPException(401, f"Session exchange failed: {e}")

    email = (info.get("email") or "").lower()
    name = info.get("name") or email.split("@")[0]
    picture = info.get("picture")
    session_token = info.get("session_token")
    if not email or not session_token:
        raise HTTPException(401, "Invalid session payload")

    # Find/auto-provision user
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user:
        # auto-provision into seeded ICE tenant (default)
        tenant = await db.tenants.find_one({"slug": SEED_TENANT_SLUG}, {"_id": 0})
        if not tenant:
            raise HTTPException(500, "Default tenant missing")
        user_id = gen_id()
        user = {
            "id": user_id,
            "email": email,
            "full_name": name,
            "picture": picture,
            "tenant_id": tenant["id"],
            "role": "pm",
            "auth_provider": "google",
            "password_hash": None,
            "created_at": utc_now().isoformat(),
        }
        await db.users.insert_one(user.copy())
        await db.tenant_users.insert_one({
            "id": gen_id(),
            "user_id": user_id,
            "tenant_id": tenant["id"],
            "role": "pm",
            "status": "active",
            "created_at": utc_now().isoformat(),
        })
    else:
        # update profile picture / name on every login
        await db.users.update_one(
            {"id": user["id"]},
            {"$set": {"picture": picture, "full_name": name, "auth_provider": "google"}},
        )

    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    await db.user_sessions.insert_one({
        "id": gen_id(),
        "user_id": user["id"],
        "session_token": session_token,
        "expires_at": expires_at.isoformat(),
        "created_at": utc_now().isoformat(),
    })

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )

    tenant = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0})
    user_view = {k: v for k, v in user.items() if k not in ("password_hash",)}
    return {
        "user": User(**user_view).model_dump(mode="json"),
        "tenant": Tenant(**tenant).model_dump(mode="json"),
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    db = get_db()
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        await db.user_sessions.delete_many({"session_token": token})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}
