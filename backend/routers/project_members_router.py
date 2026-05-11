"""Bucket H Phase 3 (2026-05-08) -- project_members router.

Backs the new Project Access management page. CRUD + audit for the
per-project membership rows that drive RBAC scoping on Resource
Leveling + (future) every other surface that needs project-level
access control.

Endpoints
---------
GET    /api/projects/{project_id}/members
POST   /api/projects/{project_id}/members
PATCH  /api/projects/{project_id}/members/{user_id}
DELETE /api/projects/{project_id}/members/{user_id}

Permission rules
----------------
- admin: full CRUD on any project's members in their tenant.
- pm:    full CRUD on members of projects where THEY are a member
         with role_on_project=pm (can grant/revoke access to their
         own projects). Cannot manage members on projects they
         aren't PM of.
- others: 403.

Audit ops
---------
- PROJECT_MEMBER_ADDED
- PROJECT_MEMBER_REMOVED
- PROJECT_MEMBER_ROLE_CHANGED
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from database import get_db, tenant_filter
from models import (
    ProjectMember, ProjectMemberCreate, ProjectMemberUpdate,
    gen_id, utc_now,
)
from auth import require_tenant, has_project_access, can_manage_project_members
from audit import write_audit

router = APIRouter(tags=["project-members"])


async def _enforce_manage(ctx: dict, project_id: str) -> None:
    """Caller must be admin OR a PM on the target project."""
    role = (ctx["user"].get("role") or "").lower()
    if not can_manage_project_members(role):
        raise HTTPException(
            403,
            f"Role {role!r} cannot manage project members "
            f"(admin or pm required)",
        )
    if role == "admin":
        return
    # PM must specifically be a PM on THIS project (not just any
    # project). Avoid relying on has_project_access since that
    # accepts {pm} for pm-tier callers but doesn't enforce that the
    # actor is registered as pm specifically.
    db = get_db()
    own = await db.project_members.find_one(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "user_id": ctx["user"]["id"], "role_on_project": "pm"},
        {"_id": 0, "id": 1},
    )
    if not own:
        raise HTTPException(
            403,
            "You must be a PM on this project to manage its members",
        )


# =====================================================================
# GET /api/projects/{project_id}/members
# =====================================================================

@router.get("/projects/{project_id}/members")
async def list_project_members(
    project_id: str, ctx: dict = Depends(require_tenant),
):
    """List members of a project. Anyone with read access to the
    project can see the member list (so foremen and supers can see
    who else is on their project)."""
    # Confirm project exists in tenant (leak-resistant 404 on cross-
    # tenant project_id).
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1, "name": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    # RBAC: caller must have access to the project to see members.
    role = (ctx["user"].get("role") or "").lower()
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=project_id,
        )
        if not ok:
            raise HTTPException(404, "Project not found")
    members = await db.project_members.find(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id},
        {"_id": 0},
    ).sort("added_at", 1).to_list(length=None)
    # Enrich with user display info.
    user_ids = [m["user_id"] for m in members]
    user_docs = await db.users.find(
        {"id": {"$in": user_ids}},
        {"_id": 0, "id": 1, "email": 1, "full_name": 1, "picture": 1},
    ).to_list(length=len(user_ids) or 1)
    by_uid = {u["id"]: u for u in user_docs}
    enriched = []
    for m in members:
        u = by_uid.get(m["user_id"]) or {}
        enriched.append({
            **m,
            "user_email": u.get("email"),
            "user_full_name": u.get("full_name"),
            "user_picture": u.get("picture"),
        })
    return {"project_id": project_id, "project_name": proj.get("name"),
            "members": enriched}


# =====================================================================
# POST /api/projects/{project_id}/members
# =====================================================================

@router.post("/projects/{project_id}/members", response_model=ProjectMember)
async def add_project_member(
    project_id: str, payload: ProjectMemberCreate,
    ctx: dict = Depends(require_tenant),
):
    """Add a tenant_user as a project member with a role_on_project.
    Idempotent: re-adding the same user with the same role is a
    no-op (returns the existing row). Adding with a different role
    is rejected -- caller should PATCH instead."""
    # Body project_id must match path.
    if payload.project_id != project_id:
        raise HTTPException(
            422, f"path project_id {project_id} != body {payload.project_id}",
        )
    await _enforce_manage(ctx, project_id)
    db = get_db()
    # Validate project + target user both exist in tenant.
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    target_user = await db.users.find_one(
        {"id": payload.user_id}, {"_id": 0, "id": 1, "tenant_id": 1, "email": 1},
    )
    if not target_user:
        raise HTTPException(404, "Target user not found")
    # Cross-tenant guard: the target user's tenant_user membership
    # must exist in this tenant.
    tu = await db.tenant_users.find_one(
        {"tenant_id": ctx["tenant_id"], "user_id": payload.user_id,
         "status": "active"},
        {"_id": 0, "id": 1},
    )
    if not tu:
        raise HTTPException(
            422,
            "Target user is not an active member of this tenant. "
            "Invite them to the tenant first.",
        )
    # Idempotency / conflict.
    existing = await db.project_members.find_one(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "user_id": payload.user_id},
        {"_id": 0},
    )
    if existing:
        if existing.get("role_on_project") == payload.role_on_project:
            return ProjectMember(**existing)
        raise HTTPException(
            409,
            f"User already a member of this project with role "
            f"{existing.get('role_on_project')!r}. PATCH to change role.",
        )
    now = utc_now()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "user_id": payload.user_id,
        "role_on_project": payload.role_on_project,
        "added_at": now.isoformat(),
        "added_by": ctx["user"]["id"],
    }
    try:
        await db.project_members.insert_one(doc.copy())
    except Exception as e:
        # Could be a unique-index race; recover by reading the existing row.
        existing = await db.project_members.find_one(
            {"tenant_id": ctx["tenant_id"], "project_id": project_id,
             "user_id": payload.user_id},
            {"_id": 0},
        )
        if existing:
            return ProjectMember(**existing)
        raise HTTPException(500, f"insert failed: {type(e).__name__}")
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="project_members",
        op="PROJECT_MEMBER_ADDED",
        record_id=doc["id"], before=None, after={
            "project_id": project_id,
            "user_id": payload.user_id,
            "user_email": target_user.get("email"),
            "role_on_project": payload.role_on_project,
            "added_by": ctx["user"]["id"],
        },
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return ProjectMember(**doc)


# =====================================================================
# PATCH /api/projects/{project_id}/members/{user_id}
# =====================================================================

@router.patch("/projects/{project_id}/members/{user_id}", response_model=ProjectMember)
async def update_project_member_role(
    project_id: str, user_id: str, payload: ProjectMemberUpdate,
    ctx: dict = Depends(require_tenant),
):
    """Change a user's role_on_project. Idempotent if role unchanged."""
    await _enforce_manage(ctx, project_id)
    db = get_db()
    existing = await db.project_members.find_one(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "user_id": user_id},
        {"_id": 0},
    )
    if not existing:
        raise HTTPException(404, "Project membership not found")
    if existing.get("role_on_project") == payload.role_on_project:
        return ProjectMember(**existing)
    before_role = existing.get("role_on_project")
    await db.project_members.update_one(
        {"id": existing["id"]},
        {"$set": {"role_on_project": payload.role_on_project}},
    )
    after = {**existing, "role_on_project": payload.role_on_project}
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="project_members",
        op="PROJECT_MEMBER_ROLE_CHANGED",
        record_id=existing["id"],
        before={"role_on_project": before_role},
        after={
            "project_id": project_id, "user_id": user_id,
            "role_on_project": payload.role_on_project,
            "changed_by": ctx["user"]["id"],
        },
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return ProjectMember(**after)


# =====================================================================
# DELETE /api/projects/{project_id}/members/{user_id}
# =====================================================================

@router.delete("/projects/{project_id}/members/{user_id}")
async def remove_project_member(
    project_id: str, user_id: str,
    ctx: dict = Depends(require_tenant),
):
    """Remove a user from a project. Audit-logged. A PM cannot remove
    themselves from a project (avoids self-locking-out). Admins
    can remove anyone including other admins."""
    await _enforce_manage(ctx, project_id)
    if user_id == ctx["user"]["id"]:
        role = (ctx["user"].get("role") or "").lower()
        if role != "admin":
            raise HTTPException(
                400,
                "You can't remove yourself from a project you manage. "
                "Ask another admin or PM to do it.",
            )
    db = get_db()
    existing = await db.project_members.find_one(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "user_id": user_id},
        {"_id": 0},
    )
    if not existing:
        raise HTTPException(404, "Project membership not found")
    await db.project_members.delete_one({"id": existing["id"]})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="project_members",
        op="PROJECT_MEMBER_REMOVED",
        record_id=existing["id"], before=existing, after=None,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True, "removed_user_id": user_id, "project_id": project_id}


# =====================================================================
# GET /api/projects/{project_id}/eligible-members
# =====================================================================
# Helper for the Add Member dialog: list tenant_users who are NOT
# already members. Returns id + email + name so the picker can
# render a usable dropdown.

@router.get("/projects/{project_id}/eligible-members")
async def list_eligible_members(
    project_id: str, ctx: dict = Depends(require_tenant),
):
    await _enforce_manage(ctx, project_id)
    db = get_db()
    existing_members = await db.project_members.find(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id},
        {"_id": 0, "user_id": 1},
    ).to_list(length=None)
    existing_ids = {m["user_id"] for m in existing_members}
    tenant_users = await db.tenant_users.find(
        {"tenant_id": ctx["tenant_id"], "status": "active"},
        {"_id": 0, "user_id": 1, "role": 1},
    ).to_list(length=None)
    eligible_ids = [tu["user_id"] for tu in tenant_users
                     if tu["user_id"] not in existing_ids]
    if not eligible_ids:
        return []
    users = await db.users.find(
        {"id": {"$in": eligible_ids}},
        {"_id": 0, "id": 1, "email": 1, "full_name": 1, "picture": 1, "role": 1},
    ).to_list(length=len(eligible_ids))
    return users
