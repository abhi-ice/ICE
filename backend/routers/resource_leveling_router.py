"""Bucket H Phase 2 (2026-05-08) -- Resource Leveling router.

8 endpoints (5 GET + 3 POST) backing the cross-project dashboard.
All tenant-scoped via require_tenant; permission tiers per
Section 2.4 enforced via role_required + per-endpoint scope checks.

The service module (services/resource_leveling.py) owns the math.
This router does:
  - request validation (date range cap, headcount sign, etc.)
  - permission gating (super sees own project; PM/admin see all)
  - tenant scoping
  - structured error responses
  - audit-logged mutations

Endpoints
---------
GET  /api/resource-leveling/dashboard
GET  /api/resource-leveling/crews/{crew_id}/calendar
GET  /api/resource-leveling/projects/{project_id}/demand
GET  /api/resource-leveling/gaps
GET  /api/resource-leveling/suggestions/{gap_id}
POST /api/resource-leveling/capacity
POST /api/resource-leveling/recompute
POST /api/resource-leveling/commit-reallocation
"""
# Bug #44 fix (2026-05-13): see photo_tray_router.py for rationale.
import logging
import time
from datetime import date, timedelta, datetime, timezone
from typing import Optional, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import Field

from database import get_db, tenant_filter
from services.site_time import project_today
from models import (
    CrewCapacity, CrewCapacityCreate, CrewCapacityUpdate,
    CrewDemand, GapRow, StrictModel, gen_id, utc_now,
)
from auth import (
    require_tenant, role_required,
    get_accessible_project_ids, has_project_access,
)
from audit import write_audit
from services import resource_leveling as svc

log = logging.getLogger("resource_leveling_router")

router = APIRouter(prefix="/resource-leveling", tags=["resource-leveling"])


# =====================================================================
# Permission helpers (Section 2.4)
# =====================================================================
# Role tiers:
#   admin   -> all projects, all crews, write all
#   pm      -> all projects in tenant, all crews, write
#   super   -> own project only, own crews only, read only
#   foreman -> own crew only, read only
#   viewer  -> read only across all
#
# "Own project" / "own crew" don't have explicit per-record membership
# tables in the current data model -- tenant_users only carries
# tenant-level role. Phase 2 implements the role-tier RBAC; per-record
# project assignment becomes a Phase 5 polish item (the spec calls it
# out as "PM sees cross-project for projects they manage" which today
# means tenant-wide for PM+admin since there's no project-membership
# table). We document this explicitly rather than fake it.

ROLE_READ = {
    "admin", "pm", "super", "foreman", "viewer", "px", "pe",
    # Bucket J Phase 2 (2026-05-11) -- specialty roles get full read
    # on the resource leveling dashboard (their workflows depend on
    # crew calendar + project demand context).
    "submittal_tech", "scheduling_engineer",
}
ROLE_WRITE_CAPACITY = {"admin", "pm", "px"}
# Reallocation commit: scheduling_engineer + submittal_tech can VIEW
# but cannot commit (per Bucket J spec). admin / pm / px-tier only.
ROLE_WRITE_REALLOCATION = {"admin", "pm", "px"}
# Recompute access: scheduling_engineer joins recompute (core workflow)
# but stays out of commit. submittal_tech is read-only.
ROLE_RECOMPUTE = {"admin", "pm", "px", "scheduling_engineer", "pe"}


def _user_role(ctx: dict) -> str:
    return (ctx.get("user") or {}).get("role") or ""


def _enforce_read(ctx: dict) -> None:
    if _user_role(ctx) not in ROLE_READ:
        raise HTTPException(403, f"Role {_user_role(ctx)!r} cannot read resource leveling")


def _enforce_write_capacity(ctx: dict) -> None:
    if _user_role(ctx) not in ROLE_WRITE_CAPACITY:
        raise HTTPException(
            403,
            f"Role {_user_role(ctx)!r} cannot override crew capacity "
            f"(admin or pm required)",
        )


def _enforce_write_reallocation(ctx: dict) -> None:
    if _user_role(ctx) not in ROLE_WRITE_REALLOCATION:
        raise HTTPException(
            403,
            f"Role {_user_role(ctx)!r} cannot commit crew reallocations "
            f"(admin or pm required)",
        )


# =====================================================================
# Range parsing (Section 2.5: edge case "date range > 180 days")
# =====================================================================

def _parse_range(start: Optional[str], end: Optional[str],
                  default_window_days: int = 14) -> tuple[date, date]:
    today = project_today()
    if not start and not end:
        return today, today + timedelta(days=default_window_days - 1)
    try:
        s = date.fromisoformat(start) if start else today
        e = date.fromisoformat(end) if end else (s + timedelta(days=default_window_days - 1))
    except ValueError as exc:
        raise HTTPException(422, f"date format must be YYYY-MM-DD: {exc}")
    if e < s:
        raise HTTPException(422, f"date range invalid: end {e} before start {s}")
    span = (e - s).days + 1
    if span > svc.MAX_RANGE_DAYS:
        raise HTTPException(
            400,
            f"date range too wide ({span} days; max {svc.MAX_RANGE_DAYS}). "
            f"Paginate or split the request.",
        )
    return s, e


def _parse_csv(v: Optional[str]) -> Optional[list[str]]:
    if not v:
        return None
    parts = [p.strip() for p in v.split(",") if p.strip()]
    return parts or None


# =====================================================================
# 1. Cross-project dashboard
# =====================================================================

@router.get("/dashboard")
async def get_dashboard(
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    trade: Optional[str] = None,             # CSV trades
    project_id: Optional[str] = None,        # CSV projects
    ctx: dict = Depends(require_tenant),
):
    """Returns the heat map payload: {trades, dates, cells, summary}.
    Default window = next 14 days. Tenant-scoped automatically; per-
    project RBAC narrows to the caller's accessible projects."""
    _enforce_read(ctx)
    # Bucket H Phase 5 -- timing log per spec Section 5.2. Records
    # window span + payload size so deploy-side perf can be tracked.
    t0 = time.perf_counter()
    start, end = _parse_range(date_range_start, date_range_end, default_window_days=14)
    requested_filter = _parse_csv(project_id)
    accessible = await get_accessible_project_ids(
        tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
        user_role=(ctx["user"].get("role") or ""),
    )
    # Build effective filter: intersect requested filter with accessible
    # set. None (admin) = no scoping; explicit empty = no access.
    if accessible is None:
        effective_filter = requested_filter
    else:
        if not accessible:
            # Scoped user with no project memberships -> empty dashboard.
            return {"trades": [], "dates": [], "cells": {},
                    "summary": {"total_cells": 0, "shortage_cells": 0,
                                  "surplus_cells": 0, "balanced_cells": 0,
                                  "cp_shortage_cells": 0,
                                  "trades_in_scope": 0,
                                  "date_range_start": start.isoformat(),
                                  "date_range_end": end.isoformat(),
                                  "rbac_scope_empty": True}}
        if requested_filter:
            allowed_set = set(accessible)
            effective_filter = [p for p in requested_filter if p in allowed_set]
            if not effective_filter:
                # User filtered to projects they can't see.
                raise HTTPException(403, "No accessible projects in the requested filter")
        else:
            effective_filter = accessible
    try:
        result = await svc.get_cross_project_view(
            tenant_id=ctx["tenant_id"], start=start, end=end,
            trade_filter=_parse_csv(trade),
            project_filter=effective_filter,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # Log latency + cardinality so deploy-side perf can be tracked.
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    window_days = (end - start).days + 1
    trade_count = len(result.get("trades") or [])
    cell_count = (result.get("summary") or {}).get("total_cells", 0)
    log.info(
        "dashboard tenant=%s window_days=%d trades=%d cells=%d ms=%d",
        ctx["tenant_id"][:8], window_days, trade_count, cell_count, elapsed_ms,
    )
    # Echo timing in the payload so the UI can render it (optional;
    # frontend may surface as a small debug pill in dev mode).
    result["_meta"] = {"elapsed_ms": elapsed_ms,
                        "window_days": window_days}
    return result


# =====================================================================
# 2. Crew calendar drill-down
# =====================================================================

@router.get("/crews/{crew_id}/calendar")
async def get_crew_calendar(
    crew_id: str,
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    _enforce_read(ctx)
    start, end = _parse_range(date_range_start, date_range_end, default_window_days=14)
    # Per-project RBAC: if the crew's home_project_id is set, gate on
    # the user's access to that project. Crews without a home project
    # are visible to any role that has at least one project membership
    # (or to admins). This keeps cross-project leveling visibility for
    # PMs while restricting foremen to their own crew's project.
    db = get_db()
    crew = await db.crews.find_one(
        tenant_filter(ctx["tenant_id"], {"id": crew_id}),
        {"_id": 0, "id": 1, "home_project_id": 1},
    )
    if not crew:
        raise HTTPException(404, "Crew not found")
    home_pid = crew.get("home_project_id")
    if home_pid:
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=(ctx["user"].get("role") or ""),
            project_id=home_pid,
        )
        if not ok:
            # Leak-resistant 404 (don't confirm crew exists if you
            # can't access it).
            raise HTTPException(404, "Crew not found")
    else:
        accessible = await get_accessible_project_ids(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=(ctx["user"].get("role") or ""),
        )
        # Scoped user with no memberships -> denied.
        if accessible is not None and not accessible:
            raise HTTPException(404, "Crew not found")
    try:
        return await svc.get_crew_commitment_breakdown(
            tenant_id=ctx["tenant_id"], crew_id=crew_id,
            start=start, end=end,
        )
    except LookupError:
        raise HTTPException(404, "Crew not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# =====================================================================
# 3. Project demand breakdown
# =====================================================================

@router.get("/projects/{project_id}/demand")
async def get_project_demand(
    project_id: str,
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    _enforce_read(ctx)
    start, end = _parse_range(date_range_start, date_range_end, default_window_days=14)
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1, "name": 1, "status": 1},
    )
    if not proj:
        # Tenant-leak-resistant 404.
        raise HTTPException(404, "Project not found")
    # Per-project RBAC. Cross-membership returns 404 (not 403) so we
    # don't leak project existence to non-members.
    ok = await has_project_access(
        tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
        user_role=(ctx["user"].get("role") or ""),
        project_id=project_id,
    )
    if not ok:
        raise HTTPException(404, "Project not found")
    try:
        breakdown = await svc.get_project_demand_breakdown(
            tenant_id=ctx["tenant_id"], project_id=project_id,
            start=start, end=end,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    breakdown["project_name"] = proj.get("name")
    breakdown["project_status"] = proj.get("status")
    return breakdown


# =====================================================================
# 3b. Crew utilization rollup (Phase 4 fix -- replaces placeholder zeros)
# =====================================================================

@router.get("/crew-utilization-rollup")
async def get_utilization_rollup(
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    """Per-crew aggregates over the date window. Returns one row per
    crew (filtered by RBAC) with utilization%, over/under days,
    committed/available hour totals."""
    _enforce_read(ctx)
    start, end = _parse_range(date_range_start, date_range_end, default_window_days=14)
    accessible = await get_accessible_project_ids(
        tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
        user_role=(ctx["user"].get("role") or ""),
    )
    # If user is scoped with no memberships, return empty.
    if accessible is not None and not accessible:
        return []
    try:
        return await svc.get_crew_utilization_rollup(
            tenant_id=ctx["tenant_id"], start=start, end=end,
            accessible_project_ids=accessible,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# =====================================================================
# 4. Gap list (severity-sorted, paginated)
# =====================================================================

@router.get("/gaps")
async def list_gaps(
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    severity: Optional[str] = None,        # CSV: minor,moderate,major
    trade: Optional[str] = None,           # CSV trades
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: dict = Depends(require_tenant),
):
    _enforce_read(ctx)
    start, end = _parse_range(date_range_start, date_range_end, default_window_days=14)
    sev_list = _parse_csv(severity)
    if sev_list:
        bad = [s for s in sev_list if s not in ("minor", "moderate", "major")]
        if bad:
            raise HTTPException(
                422, f"unknown severity value(s): {bad}. "
                     f"Must be one of minor, moderate, major",
            )
    # Per-project RBAC: gaps are computed across the user's accessible
    # projects. The list_gaps service doesn't yet accept a project
    # filter (it walks supply tenant-wide), so we post-filter on
    # contributing_project_ids if the user is scoped.
    accessible = await get_accessible_project_ids(
        tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
        user_role=(ctx["user"].get("role") or ""),
    )
    try:
        result = await svc.list_gaps(
            tenant_id=ctx["tenant_id"], start=start, end=end,
            severity_filter=sev_list,
            trade_filter=_parse_csv(trade),
            limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if accessible is None:
        return result
    if not accessible:
        return {"total": 0, "offset": offset, "limit": limit, "rows": [],
                 "rbac_scope_empty": True}
    allowed = set(accessible)
    filtered_rows = [
        r for r in result["rows"]
        if not r.get("contributing_project_ids")
        or any(p in allowed for p in r["contributing_project_ids"])
    ]
    return {"total": len(filtered_rows), "offset": offset, "limit": limit,
            "rows": filtered_rows}


# =====================================================================
# 5. Gap resolution suggestions
# =====================================================================

@router.get("/suggestions/{gap_id}")
async def gap_suggestions(gap_id: str, ctx: dict = Depends(require_tenant)):
    """gap_id encoding: '<trade>__<YYYY-MM-DD>' (see svc.parse_gap_id).
    Returns the gap row + rule-based resolution options."""
    _enforce_read(ctx)
    try:
        result = await svc.find_gap_resolution_options(
            tenant_id=ctx["tenant_id"], gap_id=gap_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    # Per-project RBAC: scoped users only see suggestions on gaps
    # whose contributing projects they can access. If none of the
    # gap's contributing projects intersect the user's access set,
    # 404 (leak-resistant).
    accessible = await get_accessible_project_ids(
        tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
        user_role=(ctx["user"].get("role") or ""),
    )
    if accessible is None:
        return result
    if not accessible:
        raise HTTPException(404, "Gap not found")
    contrib = result.get("gap", {}).get("contributing_project_ids") or []
    if contrib and not any(p in set(accessible) for p in contrib):
        raise HTTPException(404, "Gap not found")
    return result


# =====================================================================
# 6. Manual capacity override
# =====================================================================

class CapacityOverridePayload(StrictModel):
    """Override capacity for a date range. headcount-based override
    sets total_headcount; available is recomputed (total - committed).
    To override availability directly, set total and committed."""
    crew_id: str
    date_range_start: date
    date_range_end: date
    total_headcount: int = Field(..., ge=0)
    committed_headcount: Optional[int] = Field(default=None, ge=0)
    hours_per_person: Optional[float] = Field(default=None, ge=0)
    is_holiday: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=500)


@router.post("/capacity")
async def override_capacity(
    payload: CapacityOverridePayload, ctx: dict = Depends(require_tenant),
):
    _enforce_write_capacity(ctx)
    if payload.date_range_end < payload.date_range_start:
        raise HTTPException(422, "date_range_end before date_range_start")
    span = (payload.date_range_end - payload.date_range_start).days + 1
    if span > svc.MAX_RANGE_DAYS:
        raise HTTPException(400,
            f"date range too wide ({span} days; max {svc.MAX_RANGE_DAYS})",
        )
    db = get_db()
    crew = await db.crews.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.crew_id}),
        {"_id": 0, "id": 1, "trade": 1, "company_name": 1, "home_project_id": 1},
    )
    if not crew:
        raise HTTPException(404, "Crew not found")
    # Per-project RBAC: a PM can only override capacity on crews
    # whose home_project_id they manage. Admins skip the check.
    home_pid = crew.get("home_project_id")
    if home_pid:
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=(ctx["user"].get("role") or ""),
            project_id=home_pid,
        )
        if not ok:
            raise HTTPException(404, "Crew not found")
    now_iso = utc_now().isoformat()
    written = 0
    rows_before: list[dict] = []
    rows_after: list[dict] = []
    d = payload.date_range_start
    while d <= payload.date_range_end:
        existing = await db.crew_capacity.find_one(
            tenant_filter(ctx["tenant_id"], {
                "crew_id": payload.crew_id, "date": d.isoformat(),
            }),
            {"_id": 0},
        )
        committed = (
            payload.committed_headcount if payload.committed_headcount is not None
            else int((existing or {}).get("committed_headcount") or 0)
        )
        if committed > payload.total_headcount:
            raise HTTPException(
                422,
                f"committed ({committed}) exceeds total ({payload.total_headcount}) on {d}. "
                f"Reduce committed via reallocation or raise total.",
            )
        new_doc = {
            "id": existing["id"] if existing else gen_id(),
            "tenant_id": ctx["tenant_id"],
            "crew_id": payload.crew_id,
            "trade": crew.get("trade") or "(unknown)",
            "company_name": crew.get("company_name"),
            "date": d.isoformat(),
            "total_headcount": payload.total_headcount,
            "committed_headcount": committed,
            "available_headcount": max(0, payload.total_headcount - committed),
            "hours_per_person": (
                payload.hours_per_person
                if payload.hours_per_person is not None
                else float((existing or {}).get("hours_per_person") or 8.0)
            ),
            "is_holiday": (
                payload.is_holiday
                if payload.is_holiday is not None
                else bool((existing or {}).get("is_holiday") or (d.weekday() >= 5))
            ),
            "notes": payload.notes if payload.notes is not None else (existing or {}).get("notes"),
            "computed_at": now_iso,
            "computed_from": "manual_entry",
        }
        if existing:
            rows_before.append(existing)
            await db.crew_capacity.update_one({"id": existing["id"]}, {"$set": new_doc})
        else:
            await db.crew_capacity.insert_one(new_doc.copy())
        rows_after.append(new_doc)
        written += 1
        d += timedelta(days=1)
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="crew_capacity",
        op="CREW_CAPACITY_OVERRIDE",
        record_id=payload.crew_id,
        before={"rows_affected": len(rows_before), "sample": rows_before[:3]},
        after={
            "crew_id": payload.crew_id,
            "date_range_start": payload.date_range_start.isoformat(),
            "date_range_end": payload.date_range_end.isoformat(),
            "rows_written": written,
            "total_headcount": payload.total_headcount,
            "notes": payload.notes,
        },
        actor_id=ctx["user"]["id"],
        request_id=ctx["request_id"],
    )
    return {"ok": True, "rows_written": written, "crew_id": payload.crew_id}


# =====================================================================
# 7. Recompute (BackgroundTask if >7 days)
# =====================================================================

class RecomputePayload(StrictModel):
    """Trigger recompute of supply / demand / gaps for a date window.
    Optional project_id narrows demand recompute; without it we
    recompute every project + crew in the tenant. Capacity recompute
    is always tenant-wide (crews don't have per-project scope)."""
    project_id: Optional[str] = None
    crew_id: Optional[str] = None
    date_range_start: Optional[date] = None
    date_range_end: Optional[date] = None


async def _run_full_recompute(tenant_id: str, project_id: Optional[str],
                                crew_id: Optional[str]) -> dict:
    """Internal: walks crews + projects in scope and refreshes
    capacity / demand. Idempotent."""
    db = get_db()
    capacity_results: dict = {"written": 0, "zeroed": 0, "crews_processed": 0}
    demand_results: dict = {"cleared": 0, "added": 0, "projects_processed": 0}
    if crew_id:
        r = await svc.recompute_capacity_for_crew(tenant_id=tenant_id, crew_id=crew_id)
        capacity_results["written"] += r["written"]
        capacity_results["zeroed"] += r["zeroed"]
        capacity_results["crews_processed"] = 1
    else:
        crew_ids = [c["id"] async for c in db.crews.find(
            {"tenant_id": tenant_id}, {"_id": 0, "id": 1},
        )]
        for cid in crew_ids:
            r = await svc.recompute_capacity_for_crew(tenant_id=tenant_id, crew_id=cid)
            capacity_results["written"] += r["written"]
            capacity_results["zeroed"] += r["zeroed"]
        capacity_results["crews_processed"] = len(crew_ids)
    if project_id:
        r = await svc.recompute_demand_for_project(tenant_id=tenant_id, project_id=project_id)
        demand_results["cleared"] += r["cleared"]
        demand_results["added"] += r["added"]
        demand_results["projects_processed"] = 1
    else:
        proj_ids = [p["id"] async for p in db.projects.find(
            {"tenant_id": tenant_id}, {"_id": 0, "id": 1},
        )]
        for pid in proj_ids:
            r = await svc.recompute_demand_for_project(tenant_id=tenant_id, project_id=pid)
            demand_results["cleared"] += r["cleared"]
            demand_results["added"] += r["added"]
        demand_results["projects_processed"] = len(proj_ids)
    return {"capacity": capacity_results, "demand": demand_results}


@router.post("/recompute", status_code=202)
async def trigger_recompute(
    payload: RecomputePayload,
    bg: BackgroundTasks,
    ctx: dict = Depends(require_tenant),
):
    # Bucket J Phase 2 -- recompute tier (scheduling_engineer joins).
    role = _user_role(ctx).lower()
    if role not in ROLE_RECOMPUTE:
        raise HTTPException(
            403,
            f"Role {role!r} cannot trigger recompute "
            f"(scheduling_engineer / pm / pe / admin only)",
        )
    # If a date range is supplied AND >7 days, hand off to background.
    # Otherwise execute inline so the dashboard can refresh immediately.
    inline = True
    if payload.date_range_start and payload.date_range_end:
        span = (payload.date_range_end - payload.date_range_start).days + 1
        if span > 7:
            inline = False
        if span > svc.MAX_RANGE_DAYS:
            raise HTTPException(400,
                f"date range too wide ({span} days; max {svc.MAX_RANGE_DAYS})",
            )

    job_id = gen_id()
    now_iso = utc_now().isoformat()
    if inline:
        result = await _run_full_recompute(
            ctx["tenant_id"], payload.project_id, payload.crew_id,
        )
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="system",
            op="RESOURCE_LEVELING_RECOMPUTED",
            record_id=job_id, before=None,
            after={
                "mode": "inline", "result": result,
                "project_id": payload.project_id, "crew_id": payload.crew_id,
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
        return {"job_id": job_id, "status": "complete", "mode": "inline",
                 "result": result, "started_at": now_iso}
    # Background mode.
    async def _bg_task(tenant_id, pid, cid, jid, actor):
        try:
            result = await _run_full_recompute(tenant_id, pid, cid)
            await write_audit(
                tenant_id=tenant_id, table_name="system",
                op="RESOURCE_LEVELING_RECOMPUTED",
                record_id=jid, before=None,
                after={"mode": "background", "result": result,
                        "project_id": pid, "crew_id": cid},
                actor_id=actor, request_id=f"bg_{jid}",
            )
        except Exception as exc:
            await write_audit(
                tenant_id=tenant_id, table_name="system",
                op="RESOURCE_LEVELING_RECOMPUTE_FAILED",
                record_id=jid, before=None,
                after={"error": f"{type(exc).__name__}: {exc}"},
                actor_id=actor, request_id=f"bg_{jid}",
            )
    bg.add_task(_bg_task, ctx["tenant_id"], payload.project_id,
                 payload.crew_id, job_id, ctx["user"]["id"])
    return {"job_id": job_id, "status": "scheduled", "mode": "background",
             "started_at": now_iso}


# =====================================================================
# 8. Commit reallocation
# =====================================================================

class ReallocationPayload(StrictModel):
    crew_id: str
    from_project_id: Optional[str] = None
    to_project_id: str
    date_range_start: date
    date_range_end: date
    headcount: int = Field(..., ge=1)
    reason: Optional[str] = Field(default=None, max_length=500)


@router.post("/commit-reallocation")
async def commit_reallocation(
    payload: ReallocationPayload, ctx: dict = Depends(require_tenant),
):
    _enforce_write_reallocation(ctx)
    # Per-project RBAC: caller must have access to BOTH source and
    # target projects. Source can be None for a pool-to-project move.
    role = (ctx["user"].get("role") or "")
    for pid in filter(None, [payload.from_project_id, payload.to_project_id]):
        ok = await has_project_access(
            tenant_id=ctx["tenant_id"], user_id=ctx["user"]["id"],
            user_role=role, project_id=pid,
        )
        if not ok:
            raise HTTPException(404, "Project not found")
    try:
        result = await svc.commit_reallocation(
            tenant_id=ctx["tenant_id"], crew_id=payload.crew_id,
            from_project_id=payload.from_project_id,
            to_project_id=payload.to_project_id,
            start=payload.date_range_start, end=payload.date_range_end,
            headcount=payload.headcount, reason=payload.reason,
            actor_id=ctx["user"]["id"],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="crew_capacity",
        op="CREW_REALLOCATED",
        record_id=payload.crew_id, before=None, after=result,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True, **result}
