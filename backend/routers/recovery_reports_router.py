"""Bucket I (2026-05-11) -- Recovery Report Generator HTTP layer.

7 endpoints, all tenant-scoped + project-RBAC-checked:

  POST   /recovery-reports/generate              BG generate
  GET    /recovery-reports/{id}                  full payload
  GET    /projects/{id}/recovery-reports         list w/ filters
  POST   /recovery-reports/{id}/finalize         PM/admin
  POST   /recovery-reports/{id}/archive          PM/admin
  GET    /recovery-reports/{id}/export           PDF / DOCX / JSON
  DELETE /recovery-reports/{id}                  admin soft-delete
"""
# Bug #44 fix (2026-05-13): see photo_tray_router.py for rationale.
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response,
)

from database import get_db, tenant_filter
from auth import require_tenant, has_project_access
from audit import write_audit
from models import RecoveryReportCreate, RecoveryReport
from services import recovery_report as rr

log = logging.getLogger("recovery_reports_router")
router = APIRouter(tags=["recovery-reports"])


_ROLE_FINALIZE = {"admin", "pm"}
_ROLE_ARCHIVE = {"admin", "pm"}
_ROLE_DELETE = {"admin"}
_ROLE_GENERATE = {"admin", "pm", "super"}
_ROLE_VIEW = {"admin", "pm", "super", "px", "pe", "foreman", "viewer"}


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


async def _load_report(ctx: dict, report_id: str) -> dict:
    db = get_db()
    row = await db.recovery_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": report_id}),
        {"_id": 0},
    )
    if not row:
        raise HTTPException(404, "Report not found")
    role = _user_role(ctx)
    if role != "admin":
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=row.get("project_id") or "",
        )
        if not ok:
            raise HTTPException(404, "Report not found")
    return row


def _resolve_default_period(report_type: str) -> tuple[Optional[date], Optional[date]]:
    """Per spec: period_start/end optional -- derive from report_type."""
    today = datetime.now(timezone.utc).date()
    if report_type == "weekly_variance":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if report_type == "friday_recovery":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=4)
    if report_type == "daily_variance":
        return today, today
    if report_type == "project_health_summary":
        return None, None
    # custom -- caller must supply.
    return None, None


# =====================================================================
# POST /api/recovery-reports/generate
# =====================================================================

@router.post("/recovery-reports/generate", status_code=202)
async def generate_report(
    payload: RecoveryReportCreate, bg: BackgroundTasks,
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_GENERATE:
        raise HTTPException(403, "Only super/pm/admin can generate reports")
    await _check_project_access(ctx, payload.project_id)
    period_start = payload.period_start
    period_end = payload.period_end
    if period_start is None and period_end is None:
        period_start, period_end = _resolve_default_period(payload.report_type)
    # Custom report_type must provide both period bounds.
    if payload.report_type == "custom":
        if not period_start or not period_end:
            raise HTTPException(
                422, "custom report_type requires period_start + period_end",
            )

    # Idempotency: if a non-archived report exists for the same
    # (tenant, project, type, idempotency_key OR period), return its
    # id immediately. Concurrent generate calls on the same key
    # collide cleanly.
    db = get_db()
    if payload.idempotency_key:
        existing = await db.recovery_reports.find_one(
            tenant_filter(ctx["tenant_id"], {
                "project_id": payload.project_id,
                "report_type": payload.report_type,
                "idempotency_key": payload.idempotency_key,
            }),
            {"_id": 0, "id": 1, "status": 1},
        )
        if existing:
            return {"ok": True, "report_id": existing["id"],
                    "deduped": True, "status": existing.get("status")}

    # Reserve a synthetic id so the BG task can write the row + the
    # client can poll. Generators handle their own insertion with
    # an idempotency_key fallback (we re-use the key the caller
    # passed, OR synthesize one when absent so concurrent same-period
    # calls don't double-write).
    idem = payload.idempotency_key or (
        f"{payload.report_type}|{period_start}|{period_end}"
        if (period_start or period_end) else f"{payload.report_type}|adhoc"
    )

    # Pre-insert a draft skeleton so the endpoint can return 202
    # with a usable id immediately. The BG task fills the body.
    from models import gen_id, utc_now
    placeholder_id = gen_id()
    placeholder = {
        "id": placeholder_id,
        "tenant_id": ctx["tenant_id"],
        "project_id": payload.project_id,
        "report_type": payload.report_type,
        "generated_at": utc_now().isoformat(),
        "generated_by": ctx["user"]["id"],
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "data_snapshot": {}, "ai_narrative": None,
        "recovery_actions": [], "variance_summary": {},
        "exhibit_artifacts": [],
        "status": "draft",
        "finalized_by": None, "finalized_at": None,
        "archived_by": None, "archived_at": None,
        "audit_op_label": "Recovery Report (generating...)",
        "is_degraded": False, "degraded_reason": None,
        "ai_cost_estimate_usd": None,
        # Bug #37 fix (2026-05-13): do NOT carry `idempotency_key` on the
        # placeholder. Generator's _persist_report runs its own
        # idempotency lookup keyed on (tenant, project, type, idem_key);
        # if the placeholder shared the same key the generator would
        # match the placeholder + short-circuit, and the BG task would
        # then delete the only row that ever existed. Leaving idem off
        # the placeholder lets the generator's check correctly find
        # "no prior" on the FIRST generation attempt and insert the
        # real row; on a legit re-generation the prior REAL row (which
        # DOES carry idem) is found and returned. Placeholder still
        # has _placeholder=True so the BG task can locate it for the
        # success-path delete by placeholder_id.
        "_placeholder": True,
    }
    await db.recovery_reports.insert_one(placeholder.copy())

    async def _bg(report_type: str, project_id: str, ps, pe,
                   actor_id: str, request_id: str, placeholder_id: str,
                   idem: str):
        try:
            if report_type == "weekly_variance":
                out = await rr.generate_weekly_variance(
                    tenant_id=ctx["tenant_id"], project_id=project_id,
                    period_start=ps, period_end=pe,
                    actor_id=actor_id, request_id=request_id,
                    idempotency_key=idem,
                )
            elif report_type == "friday_recovery":
                out = await rr.generate_friday_recovery(
                    tenant_id=ctx["tenant_id"], project_id=project_id,
                    period_start=ps, period_end=pe,
                    actor_id=actor_id, request_id=request_id,
                    idempotency_key=idem,
                )
            elif report_type == "daily_variance":
                out = await rr.generate_daily_variance(
                    tenant_id=ctx["tenant_id"], project_id=project_id,
                    target_date=ps or datetime.now(timezone.utc).date(),
                    actor_id=actor_id, request_id=request_id,
                    idempotency_key=idem,
                )
            elif report_type == "project_health_summary":
                out = await rr.generate_project_health_summary(
                    tenant_id=ctx["tenant_id"], project_id=project_id,
                    actor_id=actor_id, request_id=request_id,
                    idempotency_key=idem,
                )
            elif report_type == "custom":
                # Custom = pull the same shape as weekly_variance over
                # the caller's window.
                out = await rr.generate_weekly_variance(
                    tenant_id=ctx["tenant_id"], project_id=project_id,
                    period_start=ps, period_end=pe,
                    actor_id=actor_id, request_id=request_id,
                    idempotency_key=idem,
                )
                # Re-label so the audit_op_label reflects "custom".
                await get_db().recovery_reports.update_one(
                    {"id": out["id"]},
                    {"$set": {"report_type": "custom",
                               "audit_op_label":
                                 f"Custom Report {ps}/{pe}"}},
                )
                out["report_type"] = "custom"
            else:
                raise ValueError(f"unknown report_type: {report_type}")
            # Remove placeholder; the generator inserted the real row.
            # (Placeholder id != generator-inserted id because the
            # generator allocates its own gen_id.)
            await get_db().recovery_reports.delete_one(
                {"id": placeholder_id, "tenant_id": ctx["tenant_id"]},
            )
        except Exception as exc:
            log.warning("recovery report BG gen failed: %s", exc, exc_info=True)
            # Mark placeholder as failed.
            await get_db().recovery_reports.update_one(
                {"id": placeholder_id},
                {"$set": {"status": "archived",
                           "is_degraded": True,
                           "degraded_reason": f"generation failed: {type(exc).__name__}"}},
            )
    bg.add_task(
        _bg, payload.report_type, payload.project_id,
        period_start, period_end,
        ctx["user"]["id"], ctx["request_id"], placeholder_id, idem,
    )
    return {
        "ok": True, "report_id": placeholder_id, "status": "generating",
        "report_type": payload.report_type,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
    }


# =====================================================================
# GET /api/recovery-reports/{id}
# =====================================================================

@router.get("/recovery-reports/{report_id}")
async def get_report(
    report_id: str, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_VIEW:
        raise HTTPException(403, "Forbidden")
    return await _load_report(ctx, report_id)


# =====================================================================
# GET /api/projects/{id}/recovery-reports
# =====================================================================

@router.get("/projects/{project_id}/recovery-reports")
async def list_project_reports(
    project_id: str,
    type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    date_range_start: Optional[str] = Query(default=None),
    date_range_end: Optional[str] = Query(default=None),
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_VIEW:
        raise HTTPException(403, "Forbidden")
    await _check_project_access(ctx, project_id)
    db = get_db()
    q: dict = {"project_id": project_id}
    if type:
        q["report_type"] = type
    if status:
        q["status"] = status
    if date_range_start or date_range_end:
        ts: dict = {}
        if date_range_start:
            ts["$gte"] = date_range_start
        if date_range_end:
            ts["$lte"] = date_range_end + "T23:59:59Z"
        q["generated_at"] = ts
    rows = await db.recovery_reports.find(
        tenant_filter(ctx["tenant_id"], q),
        {"_id": 0},
    ).sort("generated_at", -1).limit(200).to_list(length=200)
    return {"project_id": project_id, "count": len(rows),
            "reports": rows}


# =====================================================================
# POST /api/recovery-reports/{id}/finalize
# =====================================================================

@router.post("/recovery-reports/{report_id}/finalize")
async def finalize_endpoint(
    report_id: str, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_FINALIZE:
        raise HTTPException(403, "PM / admin only can finalize")
    await _load_report(ctx, report_id)
    res = await rr.finalize_report(
        tenant_id=ctx["tenant_id"], report_id=report_id,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(
            int(res.get("http_status") or 422),
            detail=res,
        )
    return res


# =====================================================================
# POST /api/recovery-reports/{id}/archive
# =====================================================================

@router.post("/recovery-reports/{report_id}/archive")
async def archive_endpoint(
    report_id: str, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_ARCHIVE:
        raise HTTPException(403, "PM / admin only can archive")
    await _load_report(ctx, report_id)
    res = await rr.archive_report(
        tenant_id=ctx["tenant_id"], report_id=report_id,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(
            int(res.get("http_status") or 422),
            detail=res,
        )
    return res


# =====================================================================
# GET /api/recovery-reports/{id}/export?format=pdf|docx|json
# =====================================================================

@router.get("/recovery-reports/{report_id}/export")
async def export_report(
    report_id: str,
    format: str = Query(..., pattern="^(pdf|docx|json)$"),
    ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_VIEW:
        raise HTTPException(403, "Forbidden")
    row = await _load_report(ctx, report_id)
    # Spec edge: export of draft report -> 422 with reason. Allow
    # JSON for drafts (the UI uses it for preview); PDF/DOCX require
    # finalization so counsel isn't handed an in-flight artifact.
    if format in ("pdf", "docx") and row.get("status") == "draft":
        raise HTTPException(
            422,
            "Finalize the report before exporting PDF / DOCX. "
            "Draft -> JSON export only.",
        )
    try:
        blob, ct, fn = await rr.render_format(
            tenant_id=ctx["tenant_id"], report_id=report_id,
            fmt=format, actor_id=ctx["user"]["id"],
            request_id=ctx["request_id"],
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    return Response(content=blob, media_type=ct, headers=headers)


# =====================================================================
# DELETE /api/recovery-reports/{id}  (admin soft-delete)
# =====================================================================

@router.delete("/recovery-reports/{report_id}")
async def delete_endpoint(
    report_id: str, ctx: dict = Depends(require_tenant),
):
    if _user_role(ctx) not in _ROLE_DELETE:
        raise HTTPException(403, "Admins only")
    await _load_report(ctx, report_id)
    res = await rr.delete_report(
        tenant_id=ctx["tenant_id"], report_id=report_id,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if not res.get("ok"):
        raise HTTPException(
            int(res.get("http_status") or 422),
            detail=res,
        )
    return res
