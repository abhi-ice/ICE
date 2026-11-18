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
from auth import require_tenant, role_required, redact_secrets, INVITE_SECRET_KEYS
from services.project_scope import require_project_if_given
from services.project_access import (
    check_project_access, check_record_access, project_scope_filter,
    require_full_admin,
)
from audit import write_audit, verify_chain
from services.schedule_parser import parse_schedule_file
from services.schedule_proposal import parsed_to_task_create_payload, validate_payload
from services.ai_agents import copilot_answer, recovery_suggestions, rfi_draft
from services.cpm import compute_cpm

router = APIRouter(tags=["system"])

# Per-project access (2026-11-16). Routes with a project id or a project
# record id in the URL or the project_id query parameter are checked by the
# route-level dependency (services/project_access.enforce_project_scope).
# The handlers below add the checks for ids sent in the body and limit the
# cross-project lists and counts. Routes marked
# "# project-scope: tenant-level" are company-wide on purpose.


# --- Approvals ---
@router.post("/approvals", response_model=Approval)
async def create_approval(payload: ApprovalCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    # The project must belong to the caller's company (404 otherwise).
    await require_project_if_given(ctx, payload.project_id)
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
    # Per-project access (switch on): only the caller's projects.
    f = await project_scope_filter(ctx, f)
    docs = await db.approvals.find(f, {"_id": 0}).sort("proposed_at", -1).to_list(500)
    return [Approval(**d) for d in docs]


@router.get("/approvals/pending-count")
async def pending_count(ctx: dict = Depends(require_tenant)):
    """Followup-fix 2 (2026-05-07): defensive try/except so a missing
    approvals collection or transient DB hiccup returns {count: 0}
    instead of a 500. Sidebar polls every 60s."""
    try:
        db = get_db()
        # Same projects as the list above.
        f = await project_scope_filter(ctx, tenant_filter(ctx["tenant_id"], {"status": "pending"}))
        n = await db.approvals.count_documents(f)
        return {"count": n}
    except Exception:
        return {"count": 0}


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
# project-scope: tenant-level (company-wide audit log; all-projects admins only)
@router.get("/audit-log")
async def list_audit(
    limit: int = 200,
    op: Optional[str] = None,          # CSV: filter by op label(s)
    table_name: Optional[str] = None,  # exact match
    ctx: dict = Depends(require_tenant),
):
    """Bucket H Phase 5 -- op + table filters for the Audit Log page.
    Both optional; default is "last 200 entries this tenant." Tenant-
    scoped via tenant_filter() so cross-tenant entries never leak.

    2026-11 security round: admins only (the log records every change in
    the company, including team invites), and secret fields (invite codes,
    tokens, PINs, password / token hashes) are masked on the way out.
    Older rows in the database still hold them; they are hidden here
    instead of rewriting live records.

    2026-11-16: the log covers every project in the company, so an admin
    limited to specific projects cannot read it."""
    role_required(ctx, ["admin"])
    require_full_admin(ctx)
    db = get_db()
    q = dict(tenant_filter(ctx["tenant_id"]))
    if op:
        ops = [p.strip() for p in op.split(",") if p.strip()]
        if ops:
            q["op"] = {"$in": ops} if len(ops) > 1 else ops[0]
    if table_name:
        q["table_name"] = table_name
    if limit < 1:
        limit = 200
    if limit > 1000:
        limit = 1000
    docs = await db.audit_log.find(q, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    for d in docs:
        # On invite rows "code" is the redeemable invite code; elsewhere it
        # is an ordinary field (cost code, room code) and stays visible.
        extra = INVITE_SECRET_KEYS if d.get("table_name") == "team_invites" else ()
        for field in ("before_data", "after_data"):
            if d.get(field):
                d[field] = redact_secrets(d[field], extra)
    return docs


# project-scope: tenant-level (company-wide audit log; all-projects admins only)
@router.get("/audit-log/verify")
async def verify_audit_chain(ctx: dict = Depends(require_tenant)):
    # Admins only, like the log itself (2026-11 security round); since
    # 2026-11-16 only admins with access to all projects.
    role_required(ctx, ["admin"])
    require_full_admin(ctx)
    return await verify_chain(ctx["tenant_id"])


# --- Imports ---
# NOTE: POST /imports/schedule was moved to schedule_proposals_router.py.
# Every schedule upload is now a *proposal* that must be reviewed + approved
# by a PM/Admin before rows commit to the tasks collection.


@router.get("/imports")
async def list_imports(ctx: dict = Depends(require_tenant)):
    db = get_db()
    # Per-project access (switch on): only imports for the caller's projects.
    f = await project_scope_filter(ctx, tenant_filter(ctx["tenant_id"]))
    docs = await db.imports.find(f, {"_id": 0}).sort("created_at", -1).to_list(100)
    return docs


# --- AI ---
@router.post("/ai/copilot", response_model=CopilotResponse)
async def ai_copilot(payload: CopilotMessage, ctx: dict = Depends(require_tenant)):
    """Tool-using copilot — can READ + WRITE to the project DB. Strips
    markdown from the LLM reply (strict requirement). Each turn audited
    in copilot_messages with the JSON action plan + executed tool results.
    """
    from services.copilot_tools import execute_copilot_turn
    # A given project must belong to the caller's company (404 otherwise),
    # so the assistant never reads or writes another company's project.
    await require_project_if_given(ctx, payload.project_id)
    # The daily report in focus (body) must be on a project the caller is
    # assigned to as well. Having a report open is not a change, so it is
    # checked as a read: someone who may only read the report can still ask
    # about it. Each write tool checks the write rules itself (project
    # assignment, read-only roles, draft author, locked report).
    if payload.daily_report_id:
        await check_record_access(ctx, "daily_report", payload.daily_report_id, write=False)
    # No AI key: plain message, and no assistant turn is saved (before
    # 2026-11-16 the raw key error was saved and shown as the reply).
    from services.ai_unavailable import ensure_ai_available, COPILOT_UNAVAILABLE_MESSAGE
    ensure_ai_available(COPILOT_UNAVAILABLE_MESSAGE)
    session_id = payload.session_id or f"copilot-{ctx['user']['id']}"
    # access_ctx: the caller's sign-in, so every assistant tool only reads
    # and changes projects the caller may use (the model cannot widen it).
    res = await execute_copilot_turn(
        tenant_id=ctx["tenant_id"],
        project_id=payload.project_id,
        daily_report_id=payload.daily_report_id,
        user_id=ctx["user"]["id"],
        user_role=(ctx["user"].get("role") or "viewer"),
        session_id=session_id,
        message=payload.message,
        access_ctx=ctx,
    )
    return CopilotResponse(
        session_id=session_id,
        response=res["reply"],
        # Sources the assistant cited from "search everything" (empty while
        # that switch is off).
        citations=res.get("citations") or [],
        usage={"actions": len(res.get("actions_executed", [])),
                "tool_results": res.get("tool_results", [])},
    )


# project-scope: tenant-level (the caller's own assistant history)
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


# project-scope: tenant-level (the caller's own assistant history)
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
    # The project and target activity come in the body.
    await check_project_access(ctx, payload.project_id, write=True)
    await check_record_access(ctx, "task", payload.target_activity_id, write=True)
    # No AI key: plain 422 "ai_unavailable", nothing saved.
    from services.ai_unavailable import ensure_ai_available, RECOVERY_UNAVAILABLE_MESSAGE
    ensure_ai_available(RECOVERY_UNAVAILABLE_MESSAGE)
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
    # The project (body) must be one the caller is assigned to. The RFI
    # shell below is only used when it is on that same project.
    await check_project_access(ctx, payload.project_id, write=True)
    # No AI key: plain 422 "ai_unavailable", nothing saved.
    from services.ai_unavailable import (
        AiUnavailableError, ensure_ai_available, RFI_DRAFT_UNAVAILABLE_MESSAGE,
    )
    ensure_ai_available(RFI_DRAFT_UNAVAILABLE_MESSAGE)
    out = await rfi_draft(
        tenant_id=ctx["tenant_id"],
        project_id=payload.project_id,
        subject=payload.subject,
        context=payload.context,
        schedule_activity_id=payload.schedule_activity_id,
    )
    # Bucket G Phase 2 -- when an existing RFI shell id is supplied,
    # create field suggestions on that RFI so the form lights up
    # with the Jigsaw pre-fill pattern. Best-effort; failures here
    # never break the draft response.
    if out.get("ok") is False or not (out.get("draft") or "").strip():
        # The AI call failed: no draft, and no suggestion is created.
        raise AiUnavailableError(RFI_DRAFT_UNAVAILABLE_MESSAGE)
    if payload.rfi_id and out.get("draft"):
        try:
            from services import field_suggestions as fs
            db = get_db()
            rfi = await db.rfis.find_one(
                tenant_filter(ctx["tenant_id"], {"id": payload.rfi_id}),
                {"_id": 0, "id": 1, "project_id": 1, "body": 1,
                 "subject": 1, "linked_task_ids": 1},
            )
            if rfi and rfi.get("project_id") == payload.project_id:
                # Suggest the generated body if the RFI body is empty.
                if not (rfi.get("body") or "").strip():
                    await fs.create_suggestion(
                        tenant_id=ctx["tenant_id"],
                        project_id=payload.project_id,
                        entity_type="rfi", entity_id=payload.rfi_id,
                        field_path="body",
                        suggested_value=out["draft"],
                        suggested_by="ai_rfi_drafter",
                        source_context=f"rfi_draft_session={ctx['request_id']}",
                        actor_id=ctx["user"]["id"],
                        request_id=ctx["request_id"],
                    )
                # If the caller provided a subject AND the RFI has no
                # subject yet, surface the caller-typed subject as a
                # suggestion (allows the form to still light-green
                # confirm the subject the user typed in the drafter
                # dialog before saving).
                if payload.subject and not (rfi.get("subject") or "").strip():
                    await fs.create_suggestion(
                        tenant_id=ctx["tenant_id"],
                        project_id=payload.project_id,
                        entity_type="rfi", entity_id=payload.rfi_id,
                        field_path="subject",
                        suggested_value=payload.subject,
                        suggested_by="ai_rfi_drafter",
                        source_context=f"rfi_draft_session={ctx['request_id']}",
                        actor_id=ctx["user"]["id"],
                        request_id=ctx["request_id"],
                    )
                # Schedule activity -> linked_task_ids suggestion when
                # the RFI has none.
                if (payload.schedule_activity_id
                        and not (rfi.get("linked_task_ids") or [])):
                    await fs.create_suggestion(
                        tenant_id=ctx["tenant_id"],
                        project_id=payload.project_id,
                        entity_type="rfi", entity_id=payload.rfi_id,
                        field_path="linked_task_ids",
                        suggested_value=[payload.schedule_activity_id],
                        suggested_by="ai_rfi_drafter",
                        source_context=f"rfi_draft_session={ctx['request_id']}",
                        actor_id=ctx["user"]["id"],
                        request_id=ctx["request_id"],
                    )
        except Exception as exc:
            import logging
            logging.getLogger("system_router").warning(
                "ai_rfi_drafter field_suggestion producer failed: %s", exc,
            )
    return out


# --- Dashboard summary ---
@router.get("/dashboard/summary")
async def dashboard_summary(
    project_id: str | None = None,
    ctx: dict = Depends(require_tenant),
):
    """pt.32: KPI band swap — Field issues + Pending approvals dropped
    in favor of Open delays + Pending deliveries (more operationally
    relevant for the federal-construction supers using this dashboard).
    Old fields kept on the response for any other surface still wired
    to them so nothing else breaks.

    pt.34 (2026-05-26): project-scoping. The frontend dashboard sits
    above the project dropdown, so when a project is picked we must
    narrow every per-project KPI (RFIs, delays, deliveries, safety,
    work items, approvals, schedule health) to that project. The
    "Active projects" tile stays tenant-wide on purpose — it counts
    how many projects you have running, which doesn't have a per-
    project meaning. When no project_id is supplied (e.g. an "All
    projects" view or first load before activeId resolves) we fall
    back to the prior tenant-wide aggregates so nothing breaks for
    callers that haven't passed the param yet.
    """
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    # `pf` (project filter) narrows tenant scope to one project when
    # the dashboard is bound to a specific project_id.
    pf = {**f}
    if project_id:
        # Checked by the route-level dependency (project_id query parameter).
        pf["project_id"] = project_id
    else:
        # Per-project access (switch on): the "all projects" view only
        # counts the caller's projects.
        pf = await project_scope_filter(ctx, pf)
    # Active projects: the caller's projects only (switch on).
    f = await project_scope_filter(ctx, f, field="id")
    # pt.35 (2026-05-26): "Active projects" was previously $in: [active,
    # mobilization] which excluded "planning" + "closeout" -- so a brand
    # new project (status defaults to "planning" per models.py:204) didn't
    # count even though the user was actively running it. The semantic
    # meaning of the KPI is "projects you're currently working on", which
    # is everything that isn't terminally complete or cancelled. Switched
    # to the same exclude-list pattern used by delays / deliveries /
    # work_items elsewhere on this endpoint.
    active_projects = await db.projects.count_documents({**f, "status": {"$nin": ["complete", "cancelled"]}})
    open_rfis = await db.rfis.count_documents({**pf, "status": {"$in": ["sent", "in_review"]}})
    pending_approvals = await db.approvals.count_documents({**pf, "status": "pending"})
    open_work_items = await db.work_items.count_documents({**pf, "status": {"$nin": ["closed", "resolved"]}})
    # pt.35: safety_events query was status == "open" (exact match), which
    # missed any event that drifted to a non-default status during edits.
    # SafetyEvent.status defaults to "open" (models.py:1954) and only
    # transitions to "closed" via STATUS_TRANSITION_CONFIGS["safety"], so
    # the established convention (copilot_tools.py:376) is "not closed".
    # The /projects/{id}/safety-events list endpoint also filters out
    # status == "deleted" -- mirror that here so soft-deleted events
    # don't inflate the KPI.
    open_safety = await db.safety_events.count_documents({**pf, "status": {"$nin": ["closed", "deleted"]}})
    open_delays = await db.delays.count_documents({**pf, "status": {"$nin": ["resolved", "closed"]}})
    pending_deliveries = await db.deliveries.count_documents({**pf, "status": {"$nin": ["delivered", "received", "rejected"]}})
    # schedule health: % of critical-path tasks with pct_complete < expected
    # Unplanned field work is not part of the schedule (2026-11-18).
    cp_tasks = await db.tasks.find({**pf, "is_critical_path": True, "status": {"$ne": "deleted"}, "created_in_field": {"$ne": True}}, {"_id": 0, "pct_complete": 1}).to_list(2000)
    schedule_health = 100
    if cp_tasks:
        avg_pct = sum(float(t.get("pct_complete") or 0) for t in cp_tasks) / len(cp_tasks)
        schedule_health = int(round(avg_pct))
    return {
        "active_projects": active_projects,
        "open_rfis": open_rfis,
        "pending_approvals": pending_approvals,    # kept for backwards-compat
        "open_work_items": open_work_items,         # kept for backwards-compat
        "open_safety_events": open_safety,
        "open_delays": open_delays,
        "pending_deliveries": pending_deliveries,
        "schedule_health_pct": schedule_health,
        "scoped_to_project_id": project_id,        # echo for debugging
    }


# ============================================================
# Bucket C3c Phase 2 — Cron-style entrypoint for nightly cascade recompute
# ============================================================
# project-scope: tenant-level (walks every project; all-projects admins only)
@router.post("/cron/cascade-recompute")
async def cron_cascade_recompute(ctx: dict = Depends(require_tenant)):
    """Walks every active project for the caller's tenant and recomputes
    cascade snapshots for all open gating entities. Designed to be hit
    once a day by an external scheduler (Railway cron / GitHub Actions /
    a curl in the deploy host) — tenant-scoped so multi-tenant deploys
    schedule one job per tenant.

    No system-wide cross-tenant variant on purpose: each tenant's data
    stays isolated. The on-demand /projects/{id}/cascade-dashboard/refresh
    endpoint covers immediate-need recomputes.

    2026-11-16: walks every project in the company, so only an admin with
    access to all projects may start it (it used to accept any signed-in
    user)."""
    from services.project_access import require_full_admin
    require_full_admin(ctx)
    from services.cascade_engine import recompute_all_cascades
    db = get_db()
    proj_ids = await db.projects.find(
        tenant_filter(ctx["tenant_id"], {}), {"_id": 0, "id": 1},
    ).to_list(2000)
    aggregate = {"projects_walked": 0, "open_gates": 0, "computed": 0, "notified": 0}
    per_project: list[dict] = []
    for p in proj_ids:
        try:
            r = await recompute_all_cascades(ctx["tenant_id"], p["id"])
            per_project.append({"project_id": p["id"], **r})
            aggregate["projects_walked"] += 1
            aggregate["open_gates"] += r.get("open_gates", 0)
            aggregate["computed"] += r.get("computed", 0)
            aggregate["notified"] += r.get("notified", 0)
        except Exception as e:
            per_project.append({"project_id": p["id"], "error": str(e)[:200]})
    return {**aggregate, "per_project": per_project}
