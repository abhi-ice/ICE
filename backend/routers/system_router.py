"""Approvals, audit log, imports, AI agents."""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from typing import Optional
from database import get_db, tenant_filter
from models import (
    Approval, ApprovalCreate, ApprovalDecision,
    CopilotMessage, CopilotResponse,
    RecoveryRequest, RfiDraftRequest,
    gen_id, utc_now, StrictModel,
)
from auth import require_tenant
from audit import write_audit, verify_chain
from services.schedule_parser import parse_schedule_file
from services.schedule_proposal import parsed_to_task_create_payload, validate_payload
from services.ai_agents import copilot_answer, recovery_suggestions, rfi_draft
from services.cpm import compute_cpm

router = APIRouter(tags=["system"])


# --- Approvals ---
@router.post("/approvals", response_model=Approval)
async def create_approval(payload: ApprovalCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "proposed_by": ctx["user"]["id"], "proposed_at": utc_now().isoformat(),
        "status": "pending", "decisions": [],
        "final_decided_at": None, "final_decided_by": None,
        "created_at": utc_now().isoformat(),
    })
    await db.approvals.insert_one(doc.copy())
    return Approval(**doc)


@router.get("/approvals", response_model=list[Approval])
async def list_approvals(status_filter: Optional[str] = "pending", ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    if status_filter:
        f["status"] = status_filter
    docs = await db.approvals.find(f, {"_id": 0}).sort("proposed_at", -1).to_list(500)
    return [Approval(**d) for d in docs]


@router.get("/approvals/pending-count")
async def pending_count(ctx: dict = Depends(require_tenant)):
    db = get_db()
    n = await db.approvals.count_documents(tenant_filter(ctx["tenant_id"], {"status": "pending"}))
    return {"count": n}


@router.post("/approvals/{approval_id}/decide", response_model=Approval)
async def decide_approval(approval_id: str, payload: ApprovalDecision, ctx: dict = Depends(require_tenant)):
    db = get_db()
    before = await db.approvals.find_one(tenant_filter(ctx["tenant_id"], {"id": approval_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Approval not found")
    decisions = before.get("decisions", [])
    decisions.append({
        "user_id": ctx["user"]["id"],
        "decision": payload.decision,
        "notes": payload.notes,
        "decided_at": utc_now().isoformat(),
    })
    quorum = before.get("quorum_required", 1)
    approves = sum(1 for d in decisions if d["decision"] == "approved")
    rejects = sum(1 for d in decisions if d["decision"] == "rejected")
    if rejects > 0:
        new_status = "rejected"
    elif approves >= quorum:
        new_status = "approved"
    else:
        new_status = "pending"
    updates = {"decisions": decisions, "status": new_status}
    if new_status != "pending":
        updates["final_decided_at"] = utc_now().isoformat()
        updates["final_decided_by"] = ctx["user"]["id"]
    await db.approvals.update_one(tenant_filter(ctx["tenant_id"], {"id": approval_id}), {"$set": updates})
    after = await db.approvals.find_one(tenant_filter(ctx["tenant_id"], {"id": approval_id}), {"_id": 0})
    await write_audit(tenant_id=ctx["tenant_id"], table_name="approvals", op="UPDATE",
                      record_id=approval_id, before=before, after=after,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return Approval(**after)


# --- Audit log ---
@router.get("/audit-log")
async def list_audit(limit: int = 200, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.audit_log.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    return docs


@router.get("/audit-log/verify")
async def verify_audit_chain(ctx: dict = Depends(require_tenant)):
    return await verify_chain(ctx["tenant_id"])


# --- Imports ---
# NOTE: POST /imports/schedule was moved to schedule_proposals_router.py.
# Every schedule upload is now a *proposal* that must be reviewed + approved
# by a PM/Admin before rows commit to the tasks collection.


@router.get("/imports")
async def list_imports(ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.imports.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).sort("created_at", -1).to_list(100)
    return docs


# --- AI ---
@router.post("/ai/copilot", response_model=CopilotResponse)
async def ai_copilot(payload: CopilotMessage, ctx: dict = Depends(require_tenant)):
    """Tool-using copilot — can READ + WRITE to the project DB. Strips
    markdown from the LLM reply (strict requirement). Each turn audited
    in copilot_messages with the JSON action plan + executed tool results.
    """
    from services.copilot_tools import execute_copilot_turn
    session_id = payload.session_id or f"copilot-{ctx['user']['id']}"
    res = await execute_copilot_turn(
        tenant_id=ctx["tenant_id"],
        project_id=payload.project_id,
        daily_report_id=payload.daily_report_id,
        user_id=ctx["user"]["id"],
        user_role=(ctx["user"].get("role") or "viewer"),
        session_id=session_id,
        message=payload.message,
    )
    return CopilotResponse(
        session_id=session_id,
        response=res["reply"],
        citations=[],
        usage={"actions": len(res.get("actions_executed", [])),
                "tool_results": res.get("tool_results", [])},
    )


@router.get("/ai/copilot/sessions")
async def list_copilot_sessions(ctx: dict = Depends(require_tenant)):
    db = get_db()
    pipeline = [
        {"$match": tenant_filter(ctx["tenant_id"], {"user_id": ctx["user"]["id"]})},
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id": "$session_id",
            "last_message_at": {"$first": "$created_at"},
            "preview": {"$first": "$request_text"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"last_message_at": -1}},
        {"$limit": 50},
    ]
    out = []
    async for d in db.copilot_messages.aggregate(pipeline):
        out.append({
            "session_id": d["_id"],
            "last_message_at": d["last_message_at"],
            "preview": d["preview"],
            "count": d["count"],
        })
    return out


@router.get("/ai/copilot/sessions/{session_id}")
async def get_copilot_session(session_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.copilot_messages.find(
        tenant_filter(ctx["tenant_id"], {"session_id": session_id, "user_id": ctx["user"]["id"]}),
        {"_id": 0},
    ).sort("created_at", 1).to_list(500)
    return docs


@router.post("/ai/recovery")
async def ai_recovery(payload: RecoveryRequest, ctx: dict = Depends(require_tenant)):
    return await recovery_suggestions(
        tenant_id=ctx["tenant_id"],
        project_id=payload.project_id,
        target_activity_id=payload.target_activity_id,
        trigger=payload.trigger,
    )


@router.get("/ai/recovery")
async def list_recovery(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.ai_recovery_suggestions.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("generated_at", -1).limit(50).to_list(50)
    return docs


@router.post("/ai/rfi-draft")
async def ai_rfi_draft(payload: RfiDraftRequest, ctx: dict = Depends(require_tenant)):
    return await rfi_draft(
        tenant_id=ctx["tenant_id"],
        project_id=payload.project_id,
        subject=payload.subject,
        context=payload.context,
        schedule_activity_id=payload.schedule_activity_id,
    )


# --- Dashboard summary ---
@router.get("/dashboard/summary")
async def dashboard_summary(ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    active_projects = await db.projects.count_documents({**f, "status": {"$in": ["active", "mobilization"]}})
    open_rfis = await db.rfis.count_documents({**f, "status": {"$in": ["sent", "in_review"]}})
    pending_approvals = await db.approvals.count_documents({**f, "status": "pending"})
    open_work_items = await db.work_items.count_documents({**f, "status": {"$nin": ["closed", "resolved"]}})
    open_safety = await db.safety_events.count_documents({**f, "status": "open"})
    # schedule health: % of critical-path tasks with pct_complete < expected
    cp_tasks = await db.tasks.find({**f, "is_critical_path": True, "status": {"$ne": "deleted"}}, {"_id": 0, "pct_complete": 1}).to_list(2000)
    schedule_health = 100
    if cp_tasks:
        avg_pct = sum(float(t.get("pct_complete") or 0) for t in cp_tasks) / len(cp_tasks)
        schedule_health = int(round(avg_pct))
    return {
        "active_projects": active_projects,
        "open_rfis": open_rfis,
        "pending_approvals": pending_approvals,
        "open_work_items": open_work_items,
        "open_safety_events": open_safety,
        "schedule_health_pct": schedule_health,
    }
