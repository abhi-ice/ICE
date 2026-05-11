"""Bucket E Phase 2 (2026-05-11) -- Submittal SLA HTTP layer.

Exposes the submittal_sla service module as REST endpoints. Every
endpoint is tenant-scoped + project-RBAC-checked via the existing
auth helpers; cross-tenant URL manipulation returns 404.

Endpoints
---------
POST   /api/submittals/{id}/submit-to-ae
POST   /api/submittals/{id}/record-review-event
POST   /api/submittals/{id}/pause-sla
POST   /api/submittals/{id}/resume-sla
POST   /api/submittals/{id}/assign-reviewer
POST   /api/submittals/recompute-aging          (admin BG)
POST   /api/submittals/detect-breaches          (admin BG)
GET    /api/submittals/{id}/review-events
GET    /api/projects/{id}/submittals/aging-summary
GET    /api/projects/{id}/submittals/overdue
"""
from __future__ import annotations

import logging
from typing import Optional, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from database import get_db, tenant_filter
from auth import require_tenant, has_project_access
from audit import write_audit
from models import (
    StrictModel, SubmittalReviewEventCreate, SUBMITTAL_DEFAULT_SLA_DAYS,
)
from pydantic import Field
from services import submittal_sla as sla


log = logging.getLogger("submittal_sla_router")
router = APIRouter(tags=["submittal-sla"])


# ---------------------------------------------------------------------
# Role tiers. Most workflow actions allowed for super/pm/admin (the
# super may submit + record receipts; the pm + admin assign reviewers
# + pause SLA). detect_breaches + recompute-aging are admin-only since
# they touch every row.
# ---------------------------------------------------------------------
_ROLE_RECORD = {"admin", "pm", "super"}
_ROLE_ADMIN_BG = {"admin"}
_ROLE_VIEW = {"admin", "pm", "super", "px", "pe", "foreman", "viewer"}


def _user_role(ctx: dict) -> str:
    return (ctx["user"].get("role") or "").lower()


async def _load_submittal(ctx: dict, submittal_id: str) -> dict:
    """Fetch + RBAC-check a submittal. Raises 404 on missing or cross-
    tenant. The router lets get_db -> tenant_filter handle the tenant
    isolation; the project-access check is layered on top."""
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": submittal_id}),
        {"_id": 0},
    )
    if not sub:
        raise HTTPException(404, "Submittal not found")
    role = _user_role(ctx)
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=sub.get("project_id") or "",
        )
        if not ok:
            raise HTTPException(404, "Submittal not found")
    return sub


async def _check_project_access(ctx: dict, project_id: str) -> None:
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    role = _user_role(ctx)
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=project_id,
        )
        if not ok:
            raise HTTPException(404, "Project not found")


def _http_status_from_service(res: dict, default: int = 422) -> int:
    return int(res.get("http_status") or default)


# =====================================================================
# (1) POST /api/submittals/{id}/submit-to-ae
# =====================================================================

class _SubmitToAePayload(StrictModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


@router.post("/submittals/{submittal_id}/submit-to-ae")
async def submit_to_ae(
    submittal_id: str, payload: Optional[_SubmitToAePayload] = None,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_RECORD:
        raise HTTPException(
            403, "Only super/pm/admin can submit submittals to A/E",
        )
    await _load_submittal(ctx, submittal_id)
    res = await sla.record_review_event(
        tenant_id=ctx["tenant_id"], submittal_id=submittal_id,
        event_type="submitted_to_ae",
        performed_by=ctx["user"]["id"],
        notes=(payload.notes if payload else None),
        request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(_http_status_from_service(res), detail=res)
    return res


# =====================================================================
# (2) POST /api/submittals/{id}/record-review-event  -- generic recorder
# =====================================================================

@router.post("/submittals/{submittal_id}/record-review-event")
async def record_review_event_endpoint(
    submittal_id: str, payload: SubmittalReviewEventCreate,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_RECORD:
        raise HTTPException(
            403, "Only super/pm/admin can record submittal review events",
        )
    if payload.submittal_id != submittal_id:
        raise HTTPException(
            422, "submittal_id in path must match payload",
        )
    await _load_submittal(ctx, submittal_id)
    res = await sla.record_review_event(
        tenant_id=ctx["tenant_id"], submittal_id=submittal_id,
        event_type=payload.event_type,
        performed_by=ctx["user"]["id"], notes=payload.notes,
        performed_at=payload.performed_at,
        request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(_http_status_from_service(res), detail=res)
    return res


# =====================================================================
# (3) POST /api/submittals/{id}/pause-sla
# =====================================================================

class _PausePayload(StrictModel):
    reason: str = Field(..., min_length=5, max_length=500)


@router.post("/submittals/{submittal_id}/pause-sla")
async def pause_sla_endpoint(
    submittal_id: str, payload: _PausePayload,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in {"admin", "pm"}:
        raise HTTPException(
            403, "Only PM / admin can pause the SLA clock",
        )
    await _load_submittal(ctx, submittal_id)
    res = await sla.pause_sla(
        tenant_id=ctx["tenant_id"], submittal_id=submittal_id,
        reason=payload.reason, actor_id=ctx["user"]["id"],
        request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(_http_status_from_service(res), detail=res)
    return res


# =====================================================================
# (4) POST /api/submittals/{id}/resume-sla
# =====================================================================

@router.post("/submittals/{submittal_id}/resume-sla")
async def resume_sla_endpoint(
    submittal_id: str, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in {"admin", "pm"}:
        raise HTTPException(
            403, "Only PM / admin can resume the SLA clock",
        )
    await _load_submittal(ctx, submittal_id)
    res = await sla.resume_sla(
        tenant_id=ctx["tenant_id"], submittal_id=submittal_id,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(_http_status_from_service(res), detail=res)
    return res


# =====================================================================
# (5) POST /api/submittals/{id}/assign-reviewer
# =====================================================================

class _AssignReviewerPayload(StrictModel):
    reviewer_user_id: str


@router.post("/submittals/{submittal_id}/assign-reviewer")
async def assign_reviewer_endpoint(
    submittal_id: str, payload: _AssignReviewerPayload,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in {"admin", "pm"}:
        raise HTTPException(
            403, "Only PM / admin can assign submittal reviewers",
        )
    await _load_submittal(ctx, submittal_id)
    res = await sla.assign_reviewer(
        tenant_id=ctx["tenant_id"], submittal_id=submittal_id,
        reviewer_user_id=payload.reviewer_user_id,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(_http_status_from_service(res), detail=res)
    return res


# =====================================================================
# (6) POST /api/submittals/recompute-aging  (admin BG)
# =====================================================================

class _RecomputePayload(StrictModel):
    project_id: Optional[str] = None


@router.post("/submittals/recompute-aging")
async def recompute_aging_endpoint(
    bg: BackgroundTasks,
    payload: Optional[_RecomputePayload] = None,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_ADMIN_BG:
        raise HTTPException(403, "Admins only")
    target_project = payload.project_id if payload else None
    if target_project:
        await _check_project_access(ctx, target_project)

    async def _bg(tenant_id, project_id, actor_id, request_id):
        try:
            await sla.recompute_aging_bulk(
                tenant_id=tenant_id, project_id=project_id,
                actor_id=actor_id, request_id=request_id,
            )
        except Exception as exc:
            log.warning("recompute_aging_bulk BG failed: %s", exc)

    bg.add_task(
        _bg, ctx["tenant_id"], target_project,
        ctx["user"]["id"], ctx["request_id"],
    )
    return {"ok": True, "queued": True, "project_id": target_project}


# =====================================================================
# (7) POST /api/submittals/detect-breaches  (admin BG)
# =====================================================================

@router.post("/submittals/detect-breaches")
async def detect_breaches_endpoint(
    bg: BackgroundTasks,
    payload: Optional[_RecomputePayload] = None,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_ADMIN_BG:
        raise HTTPException(403, "Admins only")
    target_project = payload.project_id if payload else None
    if target_project:
        await _check_project_access(ctx, target_project)

    async def _bg(tenant_id, project_id, actor_id, request_id):
        try:
            await sla.detect_breaches(
                tenant_id=tenant_id, project_id=project_id,
                actor_id=actor_id, request_id=request_id,
            )
        except Exception as exc:
            log.warning("detect_breaches BG failed: %s", exc)

    bg.add_task(
        _bg, ctx["tenant_id"], target_project,
        ctx["user"]["id"], ctx["request_id"],
    )
    return {"ok": True, "queued": True, "project_id": target_project}


# =====================================================================
# (8) GET /api/submittals/{id}/review-events
# =====================================================================

@router.get("/submittals/{submittal_id}/review-events")
async def list_review_events(
    submittal_id: str, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_VIEW:
        raise HTTPException(403, "Forbidden")
    await _load_submittal(ctx, submittal_id)
    db = get_db()
    rows = await db.submittal_review_events.find(
        tenant_filter(ctx["tenant_id"], {"submittal_id": submittal_id}),
        {"_id": 0},
    ).sort("performed_at", 1).to_list(length=None)
    return {"submittal_id": submittal_id, "events": rows,
            "count": len(rows)}


# =====================================================================
# (9) GET /api/projects/{id}/submittals/aging-summary
# =====================================================================

@router.get("/projects/{project_id}/submittals/aging-summary")
async def aging_summary_endpoint(
    project_id: str, days: int = Query(default=90, ge=1, le=365),
    bypass_cache: bool = Query(default=False),
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_VIEW:
        raise HTTPException(403, "Forbidden")
    await _check_project_access(ctx, project_id)
    return await sla.get_aging_summary(
        tenant_id=ctx["tenant_id"], project_id=project_id,
        days=days, bypass_cache=bypass_cache,
    )


# =====================================================================
# (10) GET /api/projects/{id}/submittals/overdue
# =====================================================================

@router.get("/projects/{project_id}/submittals/overdue")
async def overdue_list_endpoint(
    project_id: str, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_VIEW:
        raise HTTPException(403, "Forbidden")
    await _check_project_access(ctx, project_id)
    db = get_db()
    rows = await db.submittals.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "is_overdue": True,
            "status": {"$nin": ["approved", "approved_as_noted",
                                  "rejected", "closed"]},
        }),
        {"_id": 0},
    ).sort("overdue_days", -1).to_list(length=500)
    return {"project_id": project_id,
            "overdue_count": len(rows),
            "submittals": rows}
