"""Tasks (activities) router. Includes bulk-delete and CPM trigger.

Master prompt §20.4: Activity-code is the primary ref. Predecessors reference
activity_code, NOT hierarchical wbs.
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Response
from datetime import date, timedelta
from typing import Optional
from pydantic import BaseModel, ConfigDict
from database import get_db, tenant_filter
from models import Task, TaskCreate, TaskUpdate, gen_id, utc_now, StrictModel
from auth import require_tenant
from audit import write_audit
from services.cpm import compute_cpm

router = APIRouter(tags=["tasks"])


class BulkDeletePayload(StrictModel):
    task_ids: list[str]


# Bucket H Phase 4 hook -- helper that triggers a resource-leveling
# demand recompute for the date span of one task. Idempotent + best-
# effort; failures don't propagate to the calling handler.
def _affected_dates_for_task(task: dict) -> list:
    """Return the list of dates between planned_start and planned_finish
    inclusive, or [] if either is missing or malformed."""
    from datetime import date as _date
    ps = task.get("planned_start")
    pf = task.get("planned_finish")
    if not ps or not pf:
        return []
    try:
        s = _date.fromisoformat(str(ps)[:10])
        e = _date.fromisoformat(str(pf)[:10])
    except Exception:
        return []
    if e < s:
        return []
    out = []
    d = s
    while d <= e:
        out.append(d)
        d += __import__("datetime").timedelta(days=1)
    return out


async def _bg_recompute_demand_dates(tenant_id: str, project_id: str, dates: list):
    """BackgroundTask body: calls the leveling service. Audit-logged."""
    try:
        from services.resource_leveling import recompute_demand_for_dates
        from audit import write_audit
        result = await recompute_demand_for_dates(
            tenant_id=tenant_id, project_id=project_id, dates=dates,
        )
        await write_audit(
            tenant_id=tenant_id, table_name="crew_demands",
            op="RESOURCE_LEVELING_RECOMPUTED",
            record_id=project_id, before=None,
            after={"trigger": "task_mutation",
                    "project_id": project_id,
                    "date_count": len(dates), "result": result},
            actor_id=None, request_id=f"task_mut_{project_id}",
        )
    except Exception:
        pass


@router.post("/tasks", response_model=Task)
async def create_task(payload: TaskCreate, bg: BackgroundTasks = None,
                       ctx: dict = Depends(require_tenant)):
    # Bucket J Phase 2 (2026-05-11) -- scheduling_engineer + pm + pe
    # + admin are the write tier for schedule activities. super +
    # foreman are read-only on tasks per spec.
    from auth import role_can_write_schedule
    if not role_can_write_schedule((ctx["user"].get("role") or "").lower()):
        raise HTTPException(
            403,
            "Only scheduling_engineer / pm / pe / admin can create tasks",
        )
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    doc = payload.model_dump(mode="json")
    now_iso = utc_now().isoformat()
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "status": "not_started",
        "actual_start": None,
        "actual_finish": None,
        "actual_hours": 0,
        "actual_quantity": 0,
        "pct_complete": 0,
        "prev_pct_complete": 0,
        "is_critical_path": False,
        "total_float_days": None,
        "lookahead_status": "not_in_window",
        "created_at": now_iso,
        # Bucket C1 Phase 1 — initialize sync-layer fields so freshly
        # created tasks already have a token for stale-write detection.
        "updated_at": now_iso,
        "actual_hours_to_date": 0,
        "current_crew_id": None,
        "crew_assignment_history": [],
        "last_synced_from_dr": None,
        "external_modified_after_sync": False,
    })
    await db.tasks.insert_one(doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tasks", op="INSERT",
        record_id=doc["id"], before=None, after=doc,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Bucket H Phase 4 -- if the task declares trade demand, recompute
    # the affected dates' crew_demands. Tasks without required_trade
    # or required_headcount produce no demand, so the hook is a no-op
    # for them (recompute_demand_for_dates handles the missing fields).
    if doc.get("required_trade") and int(doc.get("required_headcount") or 0) > 0:
        dates = _affected_dates_for_task(doc)
        if dates:
            bg.add_task(_bg_recompute_demand_dates,
                         ctx["tenant_id"], doc["project_id"], dates)
    return Task(**doc)


@router.get("/projects/{project_id}/tasks", response_model=list[Task])
async def list_tasks(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    # Sort by `sequence` (preserves source-file order from the schedule import)
    # then by planned_start (CSV/legacy rows without a sequence). Tasks with
    # no sequence (legacy or hand-created) sort AFTER imported ones because
    # MongoDB sorts None ascending — we use a high default at write time.
    docs = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0},
    ).sort([("sequence", 1), ("planned_start", 1), ("activity_code", 1)]).to_list(10000)

    # Bucket C1 Phase 2 — read-time status refresh. There's no scheduler
    # in the runtime, so a task that was 'on_track' yesterday and crossed
    # planned_pct(today) into 'behind' won't update otherwise. Cheap
    # because compute_and_persist_status is idempotent ($set only fires
    # if a value moved). For projects with hundreds of tasks this adds a
    # few hundred no-op queries on each list call — acceptable for ICE-
    # scale (<500 activities per project per Bucket C1 design).
    from services.task_status import compute_and_persist_status
    refreshed = []
    for d in docs:
        new_fields = await compute_and_persist_status(
            db, ctx["tenant_id"], d["id"],
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
        if new_fields:
            d.update({k: v for k, v in new_fields.items() if k != "updated_at"})
        refreshed.append(d)
    return [Task(**d) for d in refreshed]


@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": task_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Task not found")
    return Task(**doc)


@router.patch("/tasks/{task_id}", response_model=Task)
async def update_task(task_id: str, payload: TaskUpdate, bg: BackgroundTasks = None, ctx: dict = Depends(require_tenant)):
    # Bucket J Phase 2 -- schedule write gate.
    from auth import role_can_write_schedule
    if not role_can_write_schedule((ctx["user"].get("role") or "").lower()):
        raise HTTPException(
            403,
            "Only scheduling_engineer / pm / pe / admin can edit tasks",
        )
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        return await get_task(task_id, ctx)
    before = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": task_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Task not found")

    # If pct_complete is changing, snapshot prev_pct (set BY THE APPLIER, NOT by TaskCreate)
    if "pct_complete" in updates and updates["pct_complete"] != before.get("pct_complete"):
        updates["prev_pct_complete"] = before.get("pct_complete", 0)

    # Bucket C1 Phase 1 — stamp updated_at so the daily-report sync layer
    # can detect stale writes against this task.
    updates["updated_at"] = utc_now().isoformat()

    await db.tasks.update_one(tenant_filter(ctx["tenant_id"], {"id": task_id}), {"$set": updates})
    after = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": task_id}), {"_id": 0})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tasks", op="UPDATE",
        record_id=task_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Trigger CPM recompute (debounced via BackgroundTasks — substitution for Celery debounce)
    if any(k in updates for k in ("planned_start", "planned_finish", "duration_days", "predecessors")):
        bg.add_task(compute_cpm, ctx["tenant_id"], before["project_id"])
    # Bucket H Phase 4 -- recompute demand if any demand-affecting field
    # moved. Union of pre + post date ranges so dates that USED to have
    # demand but no longer do also get cleared.
    demand_keys = {"required_trade", "required_headcount", "required_hours",
                    "planned_start", "planned_finish", "is_critical_path",
                    "status"}
    if any(k in updates for k in demand_keys):
        union_dates = set(_affected_dates_for_task(before))
        union_dates.update(_affected_dates_for_task(after))
        if union_dates:
            bg.add_task(_bg_recompute_demand_dates,
                         ctx["tenant_id"], before["project_id"],
                         sorted(union_dates))

    # Auto-release escrow if a marketplace bid was tied to this activity and
    # pct_complete just crossed the milestone (master prompt §19).
    if "pct_complete" in updates:
        bg.add_task(_auto_release_for_task, ctx["tenant_id"], task_id, ctx["user"]["id"])
        # Real-time fan-out for any open Gantt views
        try:
            from routers.ws_router import broadcast
            await broadcast(ctx["tenant_id"], {
                "kind": "task_progress",
                "payload": {"task_id": task_id, "pct_complete": updates["pct_complete"]},
            })
        except Exception:
            pass
    return Task(**after)


async def _auto_release_for_task(tenant_id: str, task_id: str, actor_id: str):
    """Background helper invoked when a task's pct_complete advances. Releases
    any funded marketplace escrow whose milestone has just been reached."""
    db = get_db()
    task = await db.tasks.find_one(tenant_filter(tenant_id, {"id": task_id}), {"_id": 0})
    if not task:
        return
    cur = float(task.get("pct_complete") or 0)
    funded = await db.marketplace_responses.find(
        {"escrow_status": "funded", "schedule_activity_id": task_id}, {"_id": 0},
    ).to_list(50)
    if not funded:
        return
    from routers.iter7_router import release_payment
    for r in funded:
        target = float(r.get("milestone_pct") or 50)
        if cur >= target:
            try:
                fake_ctx = {"tenant_id": tenant_id, "user": {"id": actor_id, "role": "admin"}, "request_id": "auto_release"}
                await release_payment(r["id"], fake_ctx)  # type: ignore
            except Exception:
                pass


@router.post("/projects/{project_id}/tasks/bulk-delete")
async def bulk_delete(project_id: str, payload: BulkDeletePayload, bg: BackgroundTasks = None,
                       ctx: dict = Depends(require_tenant)):
    db = get_db()
    # Bucket H Phase 4 -- collect affected dates before the soft-delete
    # so the demand recompute hook can clean up cleared rows.
    before_tasks = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id,
                                            "id": {"$in": payload.task_ids}}),
        {"_id": 0, "id": 1, "planned_start": 1, "planned_finish": 1,
         "required_trade": 1, "required_headcount": 1},
    ).to_list(length=None)
    res = await db.tasks.update_many(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "id": {"$in": payload.task_ids}}),
        {"$set": {"status": "deleted"}},
    )
    union_dates: set = set()
    for t in before_tasks:
        if t.get("required_trade") and int(t.get("required_headcount") or 0) > 0:
            union_dates.update(_affected_dates_for_task(t))
    if union_dates:
        bg.add_task(_bg_recompute_demand_dates,
                     ctx["tenant_id"], project_id, sorted(union_dates))
    return {"deleted": res.modified_count}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, bg: BackgroundTasks = None, ctx: dict = Depends(require_tenant)):
    # Bucket J Phase 2 -- schedule write gate.
    from auth import role_can_write_schedule
    if not role_can_write_schedule((ctx["user"].get("role") or "").lower()):
        raise HTTPException(
            403,
            "Only scheduling_engineer / pm / pe / admin can delete tasks",
        )
    db = get_db()
    before = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": task_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Task not found")
    await db.tasks.update_one(tenant_filter(ctx["tenant_id"], {"id": task_id}), {"$set": {"status": "deleted"}})
    from audit import write_audit
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tasks", op="DELETE",
        record_id=task_id, before=before, after={"status": "deleted"},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Bucket H Phase 4 -- clear demand on the deleted task's date span.
    if before.get("required_trade") and int(before.get("required_headcount") or 0) > 0:
        dates = _affected_dates_for_task(before)
        if dates:
            bg.add_task(_bg_recompute_demand_dates,
                         ctx["tenant_id"], before["project_id"], dates)
    return {"ok": True}


@router.post("/projects/{project_id}/critical-path/recompute")
async def recompute_cpm(project_id: str, ctx: dict = Depends(require_tenant)):
    return await compute_cpm(ctx["tenant_id"], project_id)


@router.get("/projects/{project_id}/lookahead", response_model=list[Task])
async def lookahead(
    project_id: str,
    days: int = 14,
    start: Optional[date] = None,
    ctx: dict = Depends(require_tenant),
):
    """Master prompt §3: '2WLA is a date-bounded query, not a separate table.'"""
    db = get_db()
    start_d = start or date.today()
    end_d = start_d + timedelta(days=days)
    docs = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "status": {"$ne": "deleted"},
            "$or": [
                {"planned_start": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()}},
                {"planned_finish": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()}},
                {"$and": [
                    {"planned_start": {"$lte": start_d.isoformat()}},
                    {"planned_finish": {"$gte": end_d.isoformat()}},
                ]},
            ],
        }),
        {"_id": 0},
    ).sort("planned_start", 1).to_list(500)
    # Mark them in_window for downstream consumers
    for d in docs:
        if d.get("lookahead_status") != "in_window":
            await db.tasks.update_one(
                tenant_filter(ctx["tenant_id"], {"id": d["id"]}),
                {"$set": {"lookahead_status": "in_window"}},
            )
            d["lookahead_status"] = "in_window"
    return [Task(**d) for d in docs]


# =====================================================================
# Bug #42 + #43 (2026-05-12) -- Tasks + Look Ahead operational exports.
# Same audit-honest pattern as Bucket K (services/exhibit_exports.py):
# router writes the audit BEFORE returning the file bytes; service
# module owns the gather + render.
# =====================================================================

async def _verify_project_access(ctx: dict, project_id: str) -> dict:
    """Tenant + project guard. Returns the project doc on success;
    raises 404 on cross-tenant or missing project. No project-RBAC
    check beyond tenant -- exports are operational, every project
    member can pull them. (Audit logs who pulled what.)"""
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1, "name": 1, "contract_number": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    return proj


@router.get("/projects/{project_id}/tasks/export")
async def export_tasks(
    project_id: str,
    format: str = Query(..., pattern="^(xlsx|pdf)$"),
    ctx: dict = Depends(require_tenant),
):
    """Stream the master-task table as XLSX or PDF. Fires
    TASKS_EXPORTED audit on the project record with format +
    task_count + actor_id in the payload."""
    await _verify_project_access(ctx, project_id)
    from services.tasks_exports import render_tasks_export
    try:
        blob, ct, fn, task_count = await render_tasks_export(
            tenant_id=ctx["tenant_id"], project_id=project_id, fmt=format,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not blob:
        # Cross-tenant or missing project (service returned empty).
        raise HTTPException(404, "Project not found")
    # Audit BEFORE returning -- chain must show the export attempt
    # even on the rare case where Response delivery fails after this.
    try:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="projects",
            op="TASKS_EXPORTED",
            record_id=project_id, before=None,
            after={
                "format": format,
                "task_count": task_count,
                "byte_count": len(blob),
                "actor_id": ctx["user"]["id"],
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    except Exception:
        # Audit failure must NOT block the export (matches Bucket K
        # exhibit-exports_router pattern). The audit chain integrity
        # check would surface the gap on the next verify-chain run.
        pass
    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    return Response(content=blob, media_type=ct, headers=headers)


@router.get("/projects/{project_id}/lookahead/export")
async def export_lookahead(
    project_id: str,
    format: str = Query(..., pattern="^(xlsx|pdf)$"),
    days: int = Query(default=14, ge=1, le=90),
    ctx: dict = Depends(require_tenant),
):
    """Stream the look-ahead window as XLSX or PDF. Fires
    LOOK_AHEAD_EXPORTED audit on the project record with format +
    task_count + at_risk_count + window_start + window_end in the
    payload. `days` query param controls the window length (default
    14 -- matches the frontend Lookahead.jsx default)."""
    await _verify_project_access(ctx, project_id)
    from services.tasks_exports import render_lookahead_export
    try:
        blob, ct, fn, meta = await render_lookahead_export(
            tenant_id=ctx["tenant_id"], project_id=project_id,
            fmt=format, days=days,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not blob:
        raise HTTPException(404, "Project not found")
    try:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="projects",
            op="LOOK_AHEAD_EXPORTED",
            record_id=project_id, before=None,
            after={
                "format": format,
                "task_count": meta.get("count", 0),
                "at_risk_count": meta.get("at_risk_count", 0),
                "window_start": meta.get("window_start"),
                "window_end": meta.get("window_end"),
                "days": meta.get("days", days),
                "byte_count": len(blob),
                "actor_id": ctx["user"]["id"],
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    except Exception:
        pass
    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    return Response(content=blob, media_type=ct, headers=headers)
