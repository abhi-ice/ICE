"""Bucket K (2026-05-11) -- standalone exhibit export endpoints.

2 endpoints:
  GET /api/projects/{id}/audit-extract/export
  GET /api/projects/{id}/photo-evidence-pack/export

Both admin/PM only. Tenant + project-RBAC scoped. Cross-tenant -> 404.
1-year date-range cap. Hash chain integrity surfaced in audit
extract; chain-of-custody hash surfaced in photo pack.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from database import get_db, tenant_filter
from auth import require_tenant, has_project_access
from audit import write_audit
from services import exhibit_exports

log = logging.getLogger("exhibit_exports_router")
router = APIRouter(tags=["exhibit-exports"])


_ROLE_EXPORT = {"admin", "pm"}


def _user_role(ctx: dict) -> str:
    return (ctx["user"].get("role") or "").lower()


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


def _enforce_date_range_cap(start: Optional[str], end: Optional[str]) -> None:
    """Spec: reject date ranges > 1 year with 400."""
    if not start or not end:
        return
    try:
        s = date.fromisoformat(start[:10])
        e = date.fromisoformat(end[:10])
    except Exception:
        raise HTTPException(422, "Invalid date format (ISO YYYY-MM-DD)")
    if (e - s).days > 366:
        raise HTTPException(
            400,
            "Date range exceeds 1 year. Export in smaller windows "
            "or contact admin for a bulk export.",
        )


# =====================================================================
# Audit Extract export
# =====================================================================

@router.get("/projects/{project_id}/audit-extract/export")
async def export_audit_extract(
    project_id: str,
    format: str = Query(..., pattern="^(csv|json|docx)$"),
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    op_filter: Optional[str] = Query(
        default=None,
        description="Comma-separated list of audit ops to include.",
    ),
    actor_filter: Optional[str] = Query(
        default=None,
        description="Comma-separated list of user_ids to include.",
    ),
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_EXPORT:
        raise HTTPException(403, "Admins / PMs only")
    await _check_project_access(ctx, project_id)
    _enforce_date_range_cap(date_range_start, date_range_end)

    ops = [s.strip() for s in (op_filter or "").split(",") if s.strip()] or None
    actors = [s.strip() for s in (actor_filter or "").split(",") if s.strip()] or None

    try:
        blob, ct, fn = await exhibit_exports.render_audit_extract(
            tenant_id=ctx["tenant_id"], project_id=project_id,
            fmt=format,
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            op_filter=ops, actor_filter=actors,
            entity_type=entity_type, entity_id=entity_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    # Audit the export action so counsel + admin can later see who
    # pulled what filters.
    try:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="audit_log",
            op="AUDIT_EXTRACT_EXPORTED",
            record_id=project_id, before=None,
            after={
                "format": format,
                "date_range_start": date_range_start,
                "date_range_end": date_range_end,
                "op_filter_count": len(ops or []),
                "actor_filter_count": len(actors or []),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "byte_count": len(blob),
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    except Exception:
        pass

    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    return Response(content=blob, media_type=ct, headers=headers)


# =====================================================================
# Photo Evidence Pack export
# =====================================================================

@router.get("/projects/{project_id}/photo-evidence-pack/export")
async def export_photo_evidence_pack(
    project_id: str,
    format: str = Query(..., pattern="^(pdf|docx|zip)$"),
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    photo_class: Optional[str] = None,
    evidentiary_only: bool = Query(default=False),
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_EXPORT:
        raise HTTPException(403, "Admins / PMs only")
    await _check_project_access(ctx, project_id)
    _enforce_date_range_cap(date_range_start, date_range_end)

    try:
        blob, ct, fn = await exhibit_exports.render_photo_evidence_pack(
            tenant_id=ctx["tenant_id"], project_id=project_id,
            fmt=format,
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            photo_class=photo_class,
            evidentiary_only=evidentiary_only,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    try:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="photos",
            op="PHOTO_EVIDENCE_PACK_EXPORTED",
            record_id=project_id, before=None,
            after={
                "format": format,
                "date_range_start": date_range_start,
                "date_range_end": date_range_end,
                "photo_class": photo_class,
                "evidentiary_only": evidentiary_only,
                "byte_count": len(blob),
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    except Exception:
        pass

    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    return Response(content=blob, media_type=ct, headers=headers)
