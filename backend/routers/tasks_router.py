"""Tasks (activities) router. Includes bulk-delete and CPM trigger.

Master prompt §20.4: Activity-code is the primary ref. Predecessors reference
activity_code, NOT hierarchical wbs.
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
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


@router.post("/tasks", response_model=Task)
async def create_task(payload: TaskCreate, ctx: dict = Depends(require_tenant)):
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
    return [Task(**d) for d in docs]


@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": task_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Task not found")
    return Task(**doc)


@router.patch("/tasks/{task_id}", response_model=Task)
async def update_task(task_id: str, payload: TaskUpdate, bg: BackgroundTasks, ctx: dict = Depends(require_tenant)):
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
async def bulk_delete(project_id: str, payload: BulkDeletePayload, ctx: dict = Depends(require_tenant)):
    db = get_db()
    res = await db.tasks.update_many(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "id": {"$in": payload.task_ids}}),
        {"$set": {"status": "deleted"}},
    )
    return {"deleted": res.modified_count}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, ctx: dict = Depends(require_tenant)):
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
