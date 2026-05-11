"""Bucket G Phase 2 (2026-05-11) -- Jigsaw Pre-fill UX HTTP layer.

Exposes the field_suggestions service module as REST endpoints. Every
endpoint is RBAC-scoped:
  - cross-tenant URL manipulation -> 404
  - per-project access enforced via auth.has_project_access
  - confirm/reject additionally check that user_role is in the row's
    authorized_confirmer_roles (the service does the final check; the
    router checks early so unauthorized callers get a clean 403 with
    the allowed-roles list in the body)

Endpoints
---------
POST   /api/field-suggestions                      admin/system manual create
GET    /api/entities/{entity_type}/{entity_id}/suggestions   per-entity feed
POST   /api/field-suggestions/{id}/confirm         per-row accept
POST   /api/field-suggestions/{id}/reject          per-row reject (reason 10+)
POST   /api/field-suggestions/bulk-confirm         bulk accept (cap 50)
POST   /api/field-suggestions/bulk-reject          bulk reject + reason
GET    /api/users/current/pending-suggestions      review queue feed
GET    /api/projects/{project_id}/suggestion-stats per-source acceptance rates
POST   /api/field-suggestions/sweep-deleted-entities  admin sweep (edge case 2)
"""
from __future__ import annotations

import logging
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_db, tenant_filter
from auth import require_tenant, has_project_access
from audit import write_audit
from models import (
    FieldSuggestionCreate, FieldSuggestionRejectPayload,
    FieldSuggestionBulkConfirmPayload, FieldSuggestionBulkRejectPayload,
    FIELD_SUGGESTION_DEFAULT_ROLES,
)
from services import field_suggestions as fs


log = logging.getLogger("field_suggestions_router")

router = APIRouter(tags=["field-suggestions"])


# Role guard sets. Only admin can manually POST a suggestion (AI
# producers call the service module directly, not via this endpoint).
# Stats endpoint is admin/PM/PX -- mirrors photo-ai-stats permissions.
_ROLE_MANUAL_CREATE = {"admin"}
_ROLE_VIEW_STATS = {"admin", "pm", "px"}
_ROLE_ADMIN_SWEEP = {"admin"}


def _user_role(ctx: dict) -> str:
    return (ctx["user"].get("role") or "").lower()


def _http_status_from_service(res: dict, default: int = 422) -> int:
    """Service module returns dicts with optional 'http_status' on
    failures. Map to the right HTTP code, falling back to 422."""
    return int(res.get("http_status") or default)


# =====================================================================
# Entity-type registry: which projects to check membership against.
# All registered entity types live under a project. We resolve the
# project_id from the entity row so the RBAC check is honest.
# =====================================================================

# Maps entity_type -> Mongo collection name. For RBAC we look up the
# entity row, read its project_id, and check has_project_access.
_ENTITY_COLLECTION: dict[str, str] = {
    "daily_report":          "daily_reports",
    "daily_report_activity": "daily_report_activities",
    "delay":                 "delays",
    "rfi":                   "rfis",
    "qc_issue":              "qc_issues",
    "safety_event":          "safety_events",
    "coordination_event":    "coordination_events",
    "delivery":              "deliveries",
    "photo_tag":             "photos",
    "submittal":             "submittals",
    "schedule_activity":     "tasks",
}


async def _resolve_project_id_for_entity(
    *, tenant_id: str, entity_type: str, entity_id: str,
) -> Optional[str]:
    """Look up the entity to find its project_id. Returns None if the
    entity is missing OR cross-tenant -- caller surfaces 404 in that
    case. daily_report_activities don't carry tenant_id; we walk
    through the parent daily_report instead."""
    db = get_db()
    coll = _ENTITY_COLLECTION.get(entity_type)
    if not coll:
        return None
    if entity_type == "daily_report_activity":
        # Activity rows don't carry tenant_id or project_id directly;
        # walk via daily_report -> project_id, tenant-scoped.
        act = await db.daily_report_activities.find_one(
            {"id": entity_id},
            {"_id": 0, "daily_report_id": 1},
        )
        if not act:
            return None
        dr = await db.daily_reports.find_one(
            tenant_filter(tenant_id, {"id": act.get("daily_report_id")}),
            {"_id": 0, "project_id": 1},
        )
        return (dr or {}).get("project_id")
    row = await db[coll].find_one(
        tenant_filter(tenant_id, {"id": entity_id}),
        {"_id": 0, "project_id": 1},
    )
    return (row or {}).get("project_id")


async def _check_entity_access(
    *, ctx: dict, entity_type: str, entity_id: str,
) -> str:
    """Resolve project_id, enforce per-project access. Returns
    project_id on success. Raises 404 on missing or cross-tenant,
    403 on access denied (treated as 404 to avoid leaking which IDs
    exist in other projects)."""
    project_id = await _resolve_project_id_for_entity(
        tenant_id=ctx["tenant_id"],
        entity_type=entity_type, entity_id=entity_id,
    )
    if not project_id:
        raise HTTPException(404, "Entity not found")
    role = _user_role(ctx)
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=project_id,
        )
        if not ok:
            # 404 not 403 -- don't tell the caller the entity exists.
            raise HTTPException(404, "Entity not found")
    return project_id


# =====================================================================
# POST /api/field-suggestions  (manual admin/system create)
# =====================================================================

@router.post("/field-suggestions")
async def create_suggestion_endpoint(
    payload: FieldSuggestionCreate, ctx: dict = Depends(require_tenant),
):
    """Admin / system manual create. AI producers call the service
    module directly (bypassing HTTP); this endpoint is for admin
    tooling + tests."""
    if _user_role(ctx) not in _ROLE_MANUAL_CREATE:
        raise HTTPException(
            403, "Admins only -- AI producers call the service module directly",
        )
    # Confirm entity exists + caller has access.
    await _check_entity_access(
        ctx=ctx, entity_type=payload.entity_type,
        entity_id=payload.entity_id,
    )
    res = await fs.create_suggestion(
        tenant_id=ctx["tenant_id"], project_id=payload.project_id,
        entity_type=payload.entity_type, entity_id=payload.entity_id,
        field_path=payload.field_path,
        suggested_value=payload.suggested_value,
        suggested_by=payload.suggested_by,
        confidence_score=payload.confidence_score,
        authorized_confirmer_roles=payload.authorized_confirmer_roles,
        source_context=payload.source_context,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(422, res.get("reason") or "create failed")
    return res


# =====================================================================
# GET /api/entities/{entity_type}/{entity_id}/suggestions
# =====================================================================

@router.get("/entities/{entity_type}/{entity_id}/suggestions")
async def list_suggestions_for_entity(
    entity_type: str, entity_id: str,
    include_terminal: bool = False,
    ctx: dict = Depends(require_tenant),
):
    if entity_type not in _ENTITY_COLLECTION:
        raise HTTPException(422, f"Unknown entity_type: {entity_type!r}")
    await _check_entity_access(
        ctx=ctx, entity_type=entity_type, entity_id=entity_id,
    )
    rows = await fs.get_suggestions_for_entity(
        tenant_id=ctx["tenant_id"],
        entity_type=entity_type, entity_id=entity_id,
        include_terminal=include_terminal,
    )
    return {"suggestions": rows, "count": len(rows)}


# =====================================================================
# POST /api/field-suggestions/{id}/confirm
# =====================================================================

@router.post("/field-suggestions/{suggestion_id}/confirm")
async def confirm_suggestion_endpoint(
    suggestion_id: str, ctx: dict = Depends(require_tenant),
):
    # Read the row + verify caller has project access to its entity.
    row = await fs.get_suggestion(
        tenant_id=ctx["tenant_id"], suggestion_id=suggestion_id,
    )
    if not row:
        raise HTTPException(404, "Suggestion not found")
    await _check_entity_access(
        ctx=ctx, entity_type=row["entity_type"], entity_id=row["entity_id"],
    )
    res = await fs.confirm_suggestion(
        tenant_id=ctx["tenant_id"], suggestion_id=suggestion_id,
        user_id=ctx["user"]["id"], user_role=_user_role(ctx),
        request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(
            _http_status_from_service(res, default=422),
            detail={"reason": res.get("reason"),
                     "current_status": res.get("current_status"),
                     "allowed_roles": res.get("allowed_roles")},
        )
    return res


# =====================================================================
# POST /api/field-suggestions/{id}/reject
# =====================================================================

@router.post("/field-suggestions/{suggestion_id}/reject")
async def reject_suggestion_endpoint(
    suggestion_id: str, payload: FieldSuggestionRejectPayload,
    ctx: dict = Depends(require_tenant),
):
    row = await fs.get_suggestion(
        tenant_id=ctx["tenant_id"], suggestion_id=suggestion_id,
    )
    if not row:
        raise HTTPException(404, "Suggestion not found")
    await _check_entity_access(
        ctx=ctx, entity_type=row["entity_type"], entity_id=row["entity_id"],
    )
    res = await fs.reject_suggestion(
        tenant_id=ctx["tenant_id"], suggestion_id=suggestion_id,
        user_id=ctx["user"]["id"], user_role=_user_role(ctx),
        reason=payload.reason, request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(
            _http_status_from_service(res, default=422),
            detail={"reason": res.get("reason"),
                     "current_status": res.get("current_status"),
                     "allowed_roles": res.get("allowed_roles")},
        )
    return res


# =====================================================================
# POST /api/field-suggestions/bulk-confirm
# POST /api/field-suggestions/bulk-reject
# Both cap at 50 ids per call (enforced by Pydantic + service).
# =====================================================================

@router.post("/field-suggestions/bulk-confirm")
async def bulk_confirm_endpoint(
    payload: FieldSuggestionBulkConfirmPayload,
    ctx: dict = Depends(require_tenant),
):
    """Bulk accept. We DON'T pre-check entity access per row -- the
    service module gates each confirm individually, and the service
    layer is tenant-scoped via tenant_filter so cross-tenant ids
    auto-skip with 'not_found'. This keeps the bulk path O(n) instead
    of O(n) per-row project lookups."""
    res = await fs.bulk_confirm(
        tenant_id=ctx["tenant_id"],
        suggestion_ids=payload.suggestion_ids,
        user_id=ctx["user"]["id"], user_role=_user_role(ctx),
        request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(422, res.get("reason") or "bulk confirm failed")
    return {
        "ok": True,
        "success_count": len(res.get("confirmed") or []),
        "failure_count": len(res.get("skipped") or []),
        "confirmed_ids": res.get("confirmed", []),
        "failure_details": res.get("skipped", []),
    }


@router.post("/field-suggestions/bulk-reject")
async def bulk_reject_endpoint(
    payload: FieldSuggestionBulkRejectPayload,
    ctx: dict = Depends(require_tenant),
):
    res = await fs.bulk_reject(
        tenant_id=ctx["tenant_id"],
        suggestion_ids=payload.suggestion_ids,
        reason=payload.reason,
        user_id=ctx["user"]["id"], user_role=_user_role(ctx),
        request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(422, res.get("reason") or "bulk reject failed")
    return {
        "ok": True,
        "success_count": len(res.get("rejected") or []),
        "failure_count": len(res.get("skipped") or []),
        "rejected_ids": res.get("rejected", []),
        "failure_details": res.get("skipped", []),
    }


# =====================================================================
# GET /api/users/current/pending-suggestions
# Review queue feed. Cursor-paginated, role-filtered (server-side),
# sortable. Phase 5 wraps a 60s cache layer.
# =====================================================================

_VALID_SORT_FIELDS = {"created_at", "entity_type", "confidence_score"}


@router.get("/users/current/pending-suggestions")
async def get_pending_suggestions_for_current_user(
    project_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    sort_by: str = "created_at",
    limit: int = Query(default=100, ge=1, le=500),
    cursor: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    if sort_by not in _VALID_SORT_FIELDS:
        raise HTTPException(
            422,
            f"sort_by must be one of {sorted(_VALID_SORT_FIELDS)}",
        )
    if entity_type and entity_type not in _ENTITY_COLLECTION:
        raise HTTPException(422, f"Unknown entity_type: {entity_type!r}")
    # If project_id supplied, enforce access.
    if project_id and _user_role(ctx) != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=_user_role(ctx), project_id=project_id,
        )
        if not ok:
            raise HTTPException(404, "Project not found")
    out = await fs.get_pending_suggestions_for_user(
        tenant_id=ctx["tenant_id"],
        user_role=_user_role(ctx),
        project_id=project_id, entity_type=entity_type,
        limit=limit, cursor=cursor,
    )
    # Apply alternate sort orders client-side over the page so the
    # cursor pagination contract stays based on created_at desc (the
    # underlying index). Switching the index per sort would multiply
    # index count; sorting in-page is fine for review-queue scale.
    rows = out.get("rows") or []
    if sort_by == "entity_type":
        rows = sorted(rows, key=lambda r: (r.get("entity_type") or ""))
    elif sort_by == "confidence_score":
        rows = sorted(rows,
                       key=lambda r: -(r.get("confidence_score") or 0))
    out["rows"] = rows
    out["sort_by"] = sort_by
    return out


# =====================================================================
# GET /api/projects/{project_id}/suggestion-stats
# Per-source acceptance rates for the AI Usage dashboard (Phase 5 will
# render this; we ship the endpoint now so the Phase 3 Review Queue
# can preview).
# =====================================================================

@router.get("/projects/{project_id}/suggestion-stats")
async def get_project_suggestion_stats(
    project_id: str, days: int = Query(default=30, ge=1, le=365),
    ctx: dict = Depends(require_tenant),
):
    role = _user_role(ctx)
    if role not in _ROLE_VIEW_STATS:
        raise HTTPException(
            403, f"Role {role!r} cannot view suggestion stats (admin/pm only)",
        )
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=project_id,
        )
        if not ok:
            raise HTTPException(404, "Project not found")

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await db.field_suggestions.find(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "created_at": {"$gte": cutoff}},
        {"_id": 0, "id": 1, "status": 1, "suggested_by": 1,
         "entity_type": 1, "confidence_score": 1, "created_at": 1},
    ).to_list(length=None)

    # Per-source roll-up. "acceptance_rate" excludes superseded +
    # expired since those aren't user decisions.
    by_source: dict[str, dict] = {}
    by_entity: dict[str, dict] = {}
    overall = {"created": 0, "confirmed": 0, "rejected": 0,
                "pending": 0, "superseded": 0, "expired": 0}
    conf_sum = 0.0
    conf_n = 0
    for r in rows:
        s = r.get("suggested_by") or "(unknown)"
        et = r.get("entity_type") or "(unknown)"
        status = r.get("status") or "pending"
        overall["created"] += 1
        overall[status] = overall.get(status, 0) + 1
        bs = by_source.setdefault(s, {
            "source": s, "created": 0, "confirmed": 0,
            "rejected": 0, "pending": 0,
            "superseded": 0, "expired": 0,
        })
        bs["created"] += 1
        bs[status] = bs.get(status, 0) + 1
        be = by_entity.setdefault(et, {
            "entity_type": et, "created": 0, "confirmed": 0,
            "rejected": 0, "pending": 0,
        })
        be["created"] += 1
        be[status] = be.get(status, 0) + 1
        c = r.get("confidence_score")
        if isinstance(c, (int, float)):
            conf_sum += float(c); conf_n += 1

    def _accept_rate(b: dict) -> float:
        decided = (b.get("confirmed", 0) + b.get("rejected", 0))
        return round(b["confirmed"] / decided, 3) if decided else 0.0

    by_source_list = sorted(
        [{**v, "acceptance_rate": _accept_rate(v)} for v in by_source.values()],
        key=lambda x: -x["created"],
    )
    by_entity_list = sorted(
        [{**v, "acceptance_rate": _accept_rate(v)} for v in by_entity.values()],
        key=lambda x: -x["created"],
    )
    return {
        "project_id": project_id,
        "window_days": days,
        "overall": {**overall, "acceptance_rate": _accept_rate(overall)},
        "by_source": by_source_list,
        "by_entity_type": by_entity_list,
        "avg_confidence_score": round(conf_sum / conf_n, 3) if conf_n else 0.0,
    }


# =====================================================================
# POST /api/field-suggestions/sweep-expired  (admin)
# Phase 5 manual trigger for the 30-day expiration sweep. The
# lifespan path runs this on boot + every 6 hours; this endpoint
# is the on-demand admin override for testing / one-shot runs.
# =====================================================================

@router.post("/field-suggestions/sweep-expired")
async def sweep_expired_endpoint(
    older_than_days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=1000, ge=1, le=5000),
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_ADMIN_SWEEP:
        raise HTTPException(403, "Admins only")
    # Tenant-scoped sweep -- only this tenant's rows get touched.
    # The lifespan path runs an unscoped variant.
    res = await fs.expire_stale_pending(
        tenant_id=ctx["tenant_id"],
        older_than_days=older_than_days, limit=limit,
    )
    return res


# Bucket G Phase 5 -- per-source acceptance-rate stats. Admin/PM
# only. Feeds the Settings -> AI Usage tab card that shows which
# producers are actually getting their suggestions accepted.
@router.get("/tenants/current/suggestion-source-stats")
async def get_source_acceptance_stats_endpoint(
    days: int = Query(default=30, ge=1, le=365),
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_VIEW_STATS:
        raise HTTPException(
            403, "Admins / PMs only -- per-source acceptance is sensitive",
        )
    return await fs.get_source_acceptance_stats(
        tenant_id=ctx["tenant_id"], days=days,
    )


# =====================================================================
# POST /api/field-suggestions/sweep-deleted-entities  (admin)
# Edge case #2 from the spec: when a suggestion's underlying entity
# is deleted, the suggestion is auto-rejected with reason="entity_
# deleted". Phase 5 will wire this to a daily background job; Phase
# 2 ships the endpoint for admin manual invocation + as the spec-
# required code path.
# =====================================================================

@router.post("/field-suggestions/sweep-deleted-entities")
async def sweep_deleted_entities_endpoint(
    limit: int = Query(default=500, ge=1, le=5000),
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_ADMIN_SWEEP:
        raise HTTPException(403, "Admins only")
    res = await sweep_orphaned_suggestions(
        tenant_id=ctx["tenant_id"], limit=limit,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return res


async def sweep_orphaned_suggestions(
    *, tenant_id: str, limit: int = 500,
    actor_id: Optional[str] = None, request_id: Optional[str] = None,
) -> dict:
    """Walk pending suggestions; for each, look up the underlying
    entity. If the entity is gone (or, for soft-delete-aware
    collections like tasks, has status="deleted"), flip the
    suggestion to rejected with reason="entity_deleted".

    Defined here rather than in services/field_suggestions.py because
    it needs the entity-collection map -- which is router-layer
    concern (the service is collection-agnostic by design).
    """
    db = get_db()
    rows = await db.field_suggestions.find(
        {"tenant_id": tenant_id, "status": "pending"},
        {"_id": 0, "id": 1, "entity_type": 1, "entity_id": 1,
         "field_path": 1, "project_id": 1},
    ).limit(max(1, min(int(limit), 5000))).to_list(length=None)

    out = {"swept": len(rows), "rejected_orphans": 0,
           "still_valid": 0, "failures": 0}
    # Group by (entity_type, entity_id) so we batch the existence
    # checks per collection.
    by_coll: dict[str, list[dict]] = {}
    for r in rows:
        et = r.get("entity_type")
        coll = _ENTITY_COLLECTION.get(et or "")
        if not coll:
            out["failures"] += 1
            continue
        by_coll.setdefault(coll, []).append(r)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    for coll, suggs in by_coll.items():
        ids = list({s["entity_id"] for s in suggs if s.get("entity_id")})
        if not ids:
            continue
        # daily_report_activities don't carry tenant_id; check by id only.
        if coll == "daily_report_activities":
            q = {"id": {"$in": ids}}
        else:
            q = {"tenant_id": tenant_id, "id": {"$in": ids}}
        # Some collections soft-delete (tasks: status="deleted").
        # Treat soft-deleted as orphan.
        existing_rows = await db[coll].find(
            q, {"_id": 0, "id": 1, "status": 1, "deleted_at": 1},
        ).to_list(length=None)
        alive_ids: set = set()
        for er in existing_rows:
            st = (er.get("status") or "").lower()
            if st in ("deleted", "archived") or er.get("deleted_at"):
                continue
            alive_ids.add(er["id"])
        # Mark orphans rejected.
        for s in suggs:
            eid = s.get("entity_id")
            if not eid:
                continue
            if eid in alive_ids:
                out["still_valid"] += 1
                continue
            # Orphan -> reject with system reason.
            try:
                res = await db.field_suggestions.update_one(
                    {"id": s["id"], "status": "pending"},
                    {"$set": {"status": "rejected",
                               "rejected_by": None,
                               "rejected_at": now_iso,
                               "rejection_reason": "entity_deleted",
                               "updated_at": now_iso}},
                )
                if res.matched_count:
                    out["rejected_orphans"] += 1
                    try:
                        await write_audit(
                            tenant_id=tenant_id,
                            table_name="field_suggestions",
                            op="FIELD_SUGGESTION_REJECTED",
                            record_id=s["id"], before=None,
                            after={"entity_type": s.get("entity_type"),
                                    "entity_id": eid,
                                    "field_path": s.get("field_path"),
                                    "reason": "entity_deleted",
                                    "swept_by_system": True},
                            actor_id=actor_id,
                            request_id=request_id or f"fs_orphan_{s['id']}",
                        )
                    except Exception:
                        pass
            except Exception as exc:
                out["failures"] += 1
                log.warning("orphan sweep failed for %s: %s",
                             s.get("id"), exc)
    return out
