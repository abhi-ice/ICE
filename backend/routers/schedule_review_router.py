"""DR activity picker, carried-forward delays, and the admin Schedule Review
screen (AI activity names + AI logic-change proposals)."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from audit import write_audit
from auth import require_tenant, role_required
from database import get_db, tenant_filter
from models import StrictModel, utc_now, pred_code
from services.schedule_feedback import (
    PAPERWORK, OPEN_DELAY_STATUSES, open_delays_for_task, generate_name_suggestions,
    apply_logic_proposal, _spawn,
)

router = APIRouter(tags=["schedule-review"])
REVIEW_ROLES = ["pm", "pe", "scheduling_engineer"]


async def _dr_or_404(db, ctx, dr_id: str) -> dict:
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": dr_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    return dr


@router.get("/daily-reports/{daily_report_id}/activity-picker")
async def activity_picker(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Every open FIELD-WORK activity of the project (paperwork excluded),
    not already on this report, with its heading path and open delays."""
    db = get_db()
    dr = await _dr_or_404(db, ctx, daily_report_id)
    tasks = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": dr["project_id"], "status": {"$nin": ["deleted", "complete"]},
            "work_category": {"$nin": list(PAPERWORK)}}),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "display_name": 1,
         "section_path": 1, "wbs": 1, "planned_start": 1, "planned_finish": 1,
         "projected_start_date": 1, "projected_finish_date": 1, "pct_complete": 1,
         "trade_code": 1},
    ).to_list(5000)
    in_grid = {r["schedule_activity_id"] for r in await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id}, {"_id": 0, "schedule_activity_id": 1}).to_list(1000)}
    delays = await db.delays.find(
        tenant_filter(ctx["tenant_id"], {"project_id": dr["project_id"],
                                         "status": {"$in": OPEN_DELAY_STATUSES}}),
        {"_id": 0, "linked_task_ids": 1},
    ).to_list(2000)
    delayed: dict = {}
    for d in delays:
        for t in d.get("linked_task_ids") or []:
            delayed[t] = delayed.get(t, 0) + 1
    out = []
    for t in tasks:
        if t["id"] in in_grid:
            continue
        t["open_delay_count"] = delayed.get(t["id"], 0)
        t["name"] = t.get("display_name") or t.get("description")
        out.append(t)
    out.sort(key=lambda t: (str(t.get("wbs") or ""), str(t.get("activity_code") or "")))
    return out


@router.get("/tasks/{task_id}/open-delays")
async def task_open_delays(task_id: str, ctx: dict = Depends(require_tenant)):
    return await open_delays_for_task(get_db(), ctx["tenant_id"], task_id)


@router.get("/daily-reports/{daily_report_id}/carried-delays")
async def carried_delays(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Open delays logged on EARLIER reports of this project -- they show on
    every later report until someone resolves them."""
    db = get_db()
    dr = await _dr_or_404(db, ctx, daily_report_id)
    docs = await db.delays.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": dr["project_id"], "status": {"$in": OPEN_DELAY_STATUSES},
            "daily_report_id": {"$ne": daily_report_id},
            "$or": [{"logged_date": {"$lt": dr["report_date"]}},
                    {"logged_date": None, "date": {"$lt": dr["report_date"]}}]}),
        {"_id": 0, "id": 1, "description": 1, "reason_category": 1, "status": 1,
         "logged_date": 1, "date": 1, "linked_task_ids": 1, "per_task_impacts": 1,
         "impact_start_date": 1},
    ).sort("logged_date", 1).to_list(200)
    task_ids = list({t for d in docs for t in (d.get("linked_task_ids") or [])})
    names = {t["id"]: t.get("display_name") or t.get("description") for t in await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"id": {"$in": task_ids}}),
        {"_id": 0, "id": 1, "display_name": 1, "description": 1}).to_list(len(task_ids) or 1)}
    for d in docs:
        d["impact_days"] = max([float(i.get("projected_impact_days") or 0)
                                for i in (d.get("per_task_impacts") or [])] or [0])
        d["activity_names"] = [names.get(t) for t in d.get("linked_task_ids") or [] if names.get(t)]
        d.pop("per_task_impacts", None)
    return docs


# ------------------------------------------------------------- activity names
@router.get("/projects/{project_id}/activity-names")
async def list_name_suggestions(project_id: str, status: str = "pending",
                                ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    docs = await get_db().activity_name_suggestions.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": status}),
        {"_id": 0}).sort("activity_code", 1).to_list(5000)
    return docs


@router.post("/projects/{project_id}/activity-names/generate")
async def generate_names(project_id: str, ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    await db.projects.update_one(tenant_filter(ctx["tenant_id"], {"id": project_id}),
                                 {"$set": {"activity_names_job": {"status": "running",
                                                                  "started_at": utc_now().isoformat()}}})

    async def run():
        try:
            res = await generate_name_suggestions(db, ctx["tenant_id"], project_id)
            job = {"status": "done", **res}
        except Exception as exc:
            job = {"status": "failed", "error": str(exc)[:300]}
        job["finished_at"] = utc_now().isoformat()
        await db.projects.update_one(tenant_filter(ctx["tenant_id"], {"id": project_id}),
                                     {"$set": {"activity_names_job": job}})
    _spawn(run())
    return {"status": "running"}


@router.get("/projects/{project_id}/activity-names/job")
async def names_job(project_id: str, ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    p = await get_db().projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}),
                                         {"_id": 0, "activity_names_job": 1}) or {}
    return p.get("activity_names_job") or {"status": "idle"}


class NameDecision(StrictModel):
    id: str
    name: Optional[str] = Field(default=None, max_length=120)


class NameDecisions(StrictModel):
    items: List[NameDecision]


@router.post("/activity-name-suggestions/approve")
async def approve_names(payload: NameDecisions, ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    db = get_db()
    done = 0
    for it in payload.items:
        s = await db.activity_name_suggestions.find_one(
            tenant_filter(ctx["tenant_id"], {"id": it.id}), {"_id": 0})
        if not s:
            continue
        name = (it.name or s["suggested_name"]).strip()
        if not name:
            continue
        await db.tasks.update_one(tenant_filter(ctx["tenant_id"], {"id": s["task_id"]}),
                                  {"$set": {"display_name": name}})
        await db.activity_name_suggestions.update_one(
            tenant_filter(ctx["tenant_id"], {"id": it.id}),
            {"$set": {"status": "approved", "approved_name": name,
                      "decided_by": ctx["user"]["id"], "decided_at": utc_now().isoformat()}})
        done += 1
    await write_audit(tenant_id=ctx["tenant_id"], table_name="tasks", op="ACTIVITY_NAMES_APPROVED",
                      record_id=None, before=None, after={"count": done},
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return {"approved": done}


@router.post("/activity-name-suggestions/{suggestion_id}/reject")
async def reject_name(suggestion_id: str, ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    await get_db().activity_name_suggestions.update_one(
        tenant_filter(ctx["tenant_id"], {"id": suggestion_id}),
        {"$set": {"status": "rejected", "decided_by": ctx["user"]["id"],
                  "decided_at": utc_now().isoformat()}})
    return {"ok": True}


# ------------------------------------------------------------- logic proposals
@router.get("/projects/{project_id}/logic-proposals")
async def list_logic_proposals(project_id: str, status: str = "pending",
                               ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    return await get_db().logic_proposals.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": status}),
        {"_id": 0}).sort("created_at", -1).to_list(500)


class PredIn(StrictModel):
    code: str
    type: str = "FS"
    lag_days: int = 0


class LogicDecision(StrictModel):
    predecessors: Optional[List[PredIn]] = None
    note: Optional[str] = Field(default=None, max_length=500)


@router.post("/logic-proposals/{proposal_id}/approve")
async def approve_logic(proposal_id: str, payload: LogicDecision, ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    db = get_db()
    p = await db.logic_proposals.find_one(tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0})
    if not p or p["status"] != "pending":
        raise HTTPException(404, "Proposal not found or already decided")
    preds = ([x.model_dump() for x in payload.predecessors] if payload.predecessors is not None
             else p.get("suggested_predecessors") or [])
    for x in preds:
        if x["type"].upper() not in {"FS", "SS", "FF", "SF"}:
            raise HTTPException(422, f"Bad relationship type {x['type']}")
        x["type"] = x["type"].upper()
    proj_tasks = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": p["project_id"], "status": {"$ne": "deleted"}}),
        {"_id": 0, "activity_code": 1, "predecessors": 1}).to_list(20000)
    graph = {t["activity_code"]: [pred_code(x) for x in (t.get("predecessors") or []) if pred_code(x)]
             for t in proj_tasks}
    for x in preds:
        if x["code"] == p["activity_code"]:
            raise HTTPException(422, "An activity can't be its own predecessor")
        if x["code"] not in graph:
            raise HTTPException(422, f"Activity {x['code']} isn't in this project's schedule")
    graph[p["activity_code"]] = [x["code"] for x in preds]
    seen, stack = set(), [x["code"] for x in preds]
    while stack:
        c = stack.pop()
        if c == p["activity_code"]:
            raise HTTPException(422, "That change would create a loop in the schedule logic")
        if c not in seen:
            seen.add(c)
            stack.extend(graph.get(c, []))
    task_before = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": p["task_id"]}), {"_id": 0})
    await apply_logic_proposal(db, ctx["tenant_id"], p, preds)
    await db.logic_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}),
        {"$set": {"status": "approved", "applied_predecessors": preds, "note": payload.note,
                  "decided_by": ctx["user"]["id"], "decided_at": utc_now().isoformat()}})
    await write_audit(tenant_id=ctx["tenant_id"], table_name="tasks", op="LOGIC_CHANGE_APPROVED",
                      record_id=p["task_id"],
                      before={"predecessors": (task_before or {}).get("predecessors")},
                      after={"predecessors": preds, "proposal_id": proposal_id},
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    from services.cpm import auto_recompute_after_event
    await auto_recompute_after_event(ctx["tenant_id"], p["project_id"],
                                     actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return {"ok": True, "predecessors": preds}


@router.post("/logic-proposals/{proposal_id}/reject")
async def reject_logic(proposal_id: str, payload: LogicDecision, ctx: dict = Depends(require_tenant)):
    role_required(ctx, REVIEW_ROLES)
    res = await get_db().logic_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id, "status": "pending"}),
        {"$set": {"status": "rejected", "note": payload.note, "decided_by": ctx["user"]["id"],
                  "decided_at": utc_now().isoformat()}})
    if not res.matched_count:
        raise HTTPException(404, "Proposal not found or already decided")
    return {"ok": True}
