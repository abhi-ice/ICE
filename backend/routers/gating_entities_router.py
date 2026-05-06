"""Bucket C3a — gating entities read API.

Phase 2: dropdown endpoint (open gates by task) for the daily-report
constraint reference picker.
Phase 4: detail page endpoint + affected-daily-reports endpoint.
Phase 5: cascade-nullification on underlying entity delete (driven from
crud_delete in iter8_router; this file only owns the read paths and the
service helpers it imports).

All endpoints are tenant-scoped.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from typing import Optional, List

from database import get_db, tenant_filter
from models import GatingEntity
from auth import require_tenant
from services.gating_entities import compute_age_fields, OPEN_STATUSES_PROXY
from services.cascade_engine import recompute_all_cascades


router = APIRouter(tags=["gating-entities"])


# Statuses that count as "still gating" — used by both the dropdown and
# the daily-report constraint pre-population. Mirrors OPEN_STATUSES_PROXY
# in services.gating_entities so non-router services can share the set.
OPEN_STATUSES = OPEN_STATUSES_PROXY


async def _enrich(ge: dict) -> dict:
    """Merge computed age + over-SLA fields into a gating entity dict."""
    age = await compute_age_fields(ge)
    return {**ge, **age}


@router.get("/projects/{project_id}/gating-entities")
async def list_gating_entities(
    project_id: str,
    task_id: Optional[str] = Query(None, description="Filter to entities gating this task (in gates_task_ids)"),
    status: Optional[str] = Query(None, description="Comma-separated list, e.g. open,under_review"),
    open_only: bool = Query(False, description="Shortcut for status in OPEN_STATUSES"),
    ctx: dict = Depends(require_tenant),
):
    """Generic list — used by the daily-report constraint dropdown
    (filtered by task_id + open_only) and by future dashboards (filtered
    by status). Returns up to 200 rows; sorted most-recent submitted_at
    first so the dropdown shows the freshest gates."""
    db = get_db()
    f: dict = {"project_id": project_id}
    if task_id:
        f["gates_task_ids"] = task_id
    if open_only:
        f["status"] = {"$in": list(OPEN_STATUSES)}
    elif status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if wanted:
            f["status"] = {"$in": wanted}
    docs = await db.gating_entities.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("submitted_at", -1).to_list(200)
    enriched = [await _enrich(d) for d in docs]
    return [GatingEntity(**d).model_dump(mode="json") for d in enriched]


# Map gating entity_type to the underlying collection name. Only the
# three with C3a Phase 1 wiring; bulletin / coordination / mop / hot_work
# resolved here when their entity types land.
_UNDERLYING_COLLECTION = {
    "rfi": "rfis",
    "submittal": "submittals",
    "field_issue": "work_items",
}


@router.get("/gating-entities/{ge_id}")
async def get_gating_entity(ge_id: str, ctx: dict = Depends(require_tenant)):
    """Phase 4 — returns the gating entity row enriched with computed
    age fields, the underlying entity record (rfi / submittal / work_item),
    and the linked construction tasks (id / wbs / activity_code /
    description / pct_complete / projected_finish_date)."""
    db = get_db()
    ge = await db.gating_entities.find_one(
        tenant_filter(ctx["tenant_id"], {"id": ge_id}), {"_id": 0},
    )
    if not ge:
        raise HTTPException(404, "Gating entity not found")
    ge = await _enrich(ge)

    # Pull underlying entity (so the page can show subject / response /
    # status / the entity-specific stuff that doesn't live on gating_entities).
    underlying = None
    coll = _UNDERLYING_COLLECTION.get(ge["entity_type"])
    if coll:
        underlying = await db[coll].find_one(
            tenant_filter(ctx["tenant_id"], {"id": ge["entity_id"]}), {"_id": 0},
        )

    # Linked construction tasks.
    tasks: list[dict] = []
    if ge.get("gates_task_ids"):
        tasks = await db.tasks.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": ge["gates_task_ids"]}}),
            {"_id": 0, "id": 1, "wbs": 1, "activity_code": 1, "description": 1,
             "pct_complete": 1, "computed_status": 1, "projected_finish_date": 1,
             "planned_finish": 1, "is_critical_path": 1},
        ).to_list(len(ge["gates_task_ids"]))

    return {
        "gating_entity": GatingEntity(**ge).model_dump(mode="json"),
        "underlying": underlying,
        "linked_tasks": tasks,
    }


@router.get("/gating-entities/{ge_id}/affected-daily-reports")
async def affected_daily_reports(ge_id: str, ctx: dict = Depends(require_tenant)):
    """Phase 4 — every daily_report_activities row that recorded a
    constraint pointing at this gating entity. Returns rows enriched
    with their parent daily_report's report_date so the page can sort
    chronologically and surface the cumulative production_impact_hours."""
    db = get_db()
    ge = await db.gating_entities.find_one(
        tenant_filter(ctx["tenant_id"], {"id": ge_id}), {"_id": 0, "id": 1, "tenant_id": 1},
    )
    if not ge:
        raise HTTPException(404, "Gating entity not found")
    rows = await db.daily_report_activities.find(
        {"constraint_reference_entity_id": ge_id},
        {"_id": 0, "id": 1, "daily_report_id": 1, "schedule_activity_id": 1,
         "activity_code": 1, "activity_description": 1, "constraint_type": 1,
         "constraint_auto_filled": 1, "production_impact_hours": 1,
         "pct_complete_start_of_day": 1, "pct_complete_end_of_day": 1,
         "hours_worked": 1, "sync_state": 1},
    ).to_list(2000)

    # Tenant guard via parent DR + denormalize report_date.
    dr_ids = list({r["daily_report_id"] for r in rows if r.get("daily_report_id")})
    drs: dict[str, dict] = {}
    if dr_ids:
        dr_docs = await db.daily_reports.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": dr_ids}}),
            {"_id": 0, "id": 1, "report_date": 1, "status": 1},
        ).to_list(len(dr_ids))
        drs = {d["id"]: d for d in dr_docs}

    enriched: list[dict] = []
    total_hours = 0.0
    for r in rows:
        dr = drs.get(r.get("daily_report_id"))
        if not dr:
            # Cross-tenant or orphan — skip
            continue
        r["report_date"] = dr.get("report_date")
        r["dr_status"] = dr.get("status")
        try:
            total_hours += float(r.get("production_impact_hours") or 0)
        except Exception:
            pass
        enriched.append(r)
    enriched.sort(key=lambda r: r.get("report_date") or "", reverse=True)

    return {
        "gating_entity_id": ge_id,
        "row_count": len(enriched),
        "total_production_impact_hours": round(total_hours, 2),
        "rows": enriched,
    }


# ============================================================
# Bucket C3c Phase 2 — Cascade dashboard
# ============================================================
@router.get("/projects/{project_id}/cascade-dashboard")
async def cascade_dashboard(project_id: str, ctx: dict = Depends(require_tenant)):
    """Latest cascade_snapshots for every still-open gating_entity in the
    project, joined with the gating_entity row for display. Sorted by
    projected CP impact at N=10 days (worst first). Returns empty rows
    list if no snapshots exist yet — UI prompts user to hit Refresh."""
    db = get_db()
    open_ges = await db.gating_entities.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "status": {"$in": list(OPEN_STATUSES)},
        }),
        {"_id": 0},
    ).to_list(2000)
    if not open_ges:
        return {"rows": [], "last_computed_at": None}

    ge_by_id = {g["id"]: g for g in open_ges}

    # Latest snapshot per gating_entity. Mongo doesn't have window funcs;
    # do it with an aggregation. Cap to recent snapshots so this stays
    # cheap even after months of nightly runs.
    pipeline = [
        {"$match": {
            "tenant_id": ctx["tenant_id"],
            "project_id": project_id,
            "gating_entity_id": {"$in": list(ge_by_id.keys())},
        }},
        {"$sort": {"computed_at": -1}},
        {"$group": {"_id": "$gating_entity_id", "snap": {"$first": "$$ROOT"}}},
    ]
    docs = await db.cascade_snapshots.aggregate(pipeline).to_list(5000)

    rows: list[dict] = []
    last_computed_at: Optional[str] = None
    for d in docs:
        snap = d.get("snap") or {}
        snap.pop("_id", None)
        ge = ge_by_id.get(snap.get("gating_entity_id"))
        if not ge:
            continue
        snap["entity_label"] = ge.get("display_label") or ge.get("entity_number")
        snap["entity_type"] = ge.get("entity_type")
        snap["entity_id"] = ge.get("entity_id")
        snap["sla_working_days"] = ge.get("sla_working_days")
        rows.append(snap)
        if last_computed_at is None or (snap.get("computed_at") or "") > last_computed_at:
            last_computed_at = snap.get("computed_at")

    rows.sort(key=lambda r: float(r.get("projected_cp_impact_at_n10_days") or 0), reverse=True)
    return {"rows": rows, "last_computed_at": last_computed_at}


@router.post("/projects/{project_id}/cascade-dashboard/refresh")
async def cascade_dashboard_refresh(
    project_id: str, bg: BackgroundTasks, ctx: dict = Depends(require_tenant),
):
    """Kick off an immediate recompute of every open gating_entity's
    cascade for this project. Runs synchronously when the gate count is
    small (returns the result), otherwise hands off to BackgroundTasks
    and returns 202."""
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    open_count = await db.gating_entities.count_documents(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "status": {"$in": list(OPEN_STATUSES)},
        }),
    )
    if open_count <= 25:
        result = await recompute_all_cascades(ctx["tenant_id"], project_id)
        return {"mode": "sync", **result}
    bg.add_task(recompute_all_cascades, ctx["tenant_id"], project_id)
    return {"mode": "background", "open_gates": open_count}


@router.get("/gating-entities/{ge_id}/cascade-history")
async def cascade_history(ge_id: str, limit: int = 50, ctx: dict = Depends(require_tenant)):
    """Time-series of cascade snapshots for a single gating entity.
    Useful on the GatingEntityDetail page to show how projected impact
    has evolved as the gate has stayed open."""
    db = get_db()
    docs = await db.cascade_snapshots.find(
        tenant_filter(ctx["tenant_id"], {"gating_entity_id": ge_id}),
        {"_id": 0},
    ).sort("computed_at", -1).limit(min(limit, 500)).to_list(500)
    return {"rows": docs}
