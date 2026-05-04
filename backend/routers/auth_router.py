"""Auth router: register, login, me. Demo seed creates ICE tenant + admin user."""
import os
from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timezone
from database import get_db
from models import (
    UserRegister, UserLogin, User, Tenant, TokenResponse, gen_id, utc_now
)
from auth import hash_password, verify_password, create_token, require_tenant
from audit import write_audit

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
async def register(payload: UserRegister):
    db = get_db()
    existing = await db.users.find_one({"email": payload.email}, {"_id": 0})
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    # Find or create tenant by slug
    tenant = await db.tenants.find_one({"slug": payload.tenant_slug}, {"_id": 0})
    if not tenant:
        tenant = {
            "id": gen_id(),
            "legal_name": payload.tenant_slug.upper(),
            "display_name": payload.tenant_slug.title(),
            "slug": payload.tenant_slug,
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
        "email": payload.email,
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

    user_view = {k: v for k, v in user_doc.items() if k != "password_hash"}
    token = create_token(user_id, tenant["id"], payload.email)
    return TokenResponse(access_token=token, user=User(**user_view), tenant=Tenant(**tenant))


@router.post("/login", response_model=TokenResponse)
async def login(payload: UserLogin):
    db = get_db()
    user = await db.users.find_one({"email": payload.email}, {"_id": 0})
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
    """List all tenants this user is a member of (for tenant switcher)."""
    db = get_db()
    memberships = await db.tenant_users.find({"user_id": ctx["user"]["id"], "status": "active"}, {"_id": 0}).to_list(20)
    tenant_ids = [m["tenant_id"] for m in memberships]
    if ctx["user"].get("tenant_id") and ctx["user"]["tenant_id"] not in tenant_ids:
        tenant_ids.append(ctx["user"]["tenant_id"])
    tenants = await db.tenants.find({"id": {"$in": tenant_ids}}, {"_id": 0}).to_list(50)
    return [Tenant(**t) for t in tenants]
