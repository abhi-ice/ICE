"""Schedule proposal router.

Every schedule upload becomes a proposal that PMs/admins review and approve
before rows commit to the `tasks` collection. Federal-contract compliance
requires a baseline-change audit trail.

Endpoints:
  POST   /api/imports/schedule              — replaces direct-apply (creates proposal)
  GET    /api/projects/{pid}/schedule-proposals
  GET    /api/schedule-proposals/{id}
  POST   /api/schedule-proposals/{id}/approve   (admin/pm)
  POST   /api/schedule-proposals/{id}/reject    (admin/pm)
  POST   /api/schedule-proposals/{id}/refresh-ai-review
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from typing import Optional
from datetime import datetime, date
from database import get_db, tenant_filter
from models import (
    ScheduleProposal, ScheduleProposalDecision, StrictModel, gen_id, utc_now,
)
from auth import require_tenant
from audit import write_audit
from services.schedule_parser import parse_schedule_file
from services.schedule_diff import compute_schedule_diff, auto_diff_on_approval
from services.schedule_ai_review import review_schedule_diff
from services.schedule_proposal import parsed_to_task_create_payload, validate_payload
from services.schedule_summary_filter import detect_summary_rows, visible_rows
from services.schedule_work_category import (
    tag_work_categories, set_row_work_category, WORK_CATEGORIES,
)
from services.cpm import compute_cpm

router = APIRouter(tags=["schedule-proposals"])

# Day-1 round 2 (2026-10-25) -- task fields whose change on an approved
# re-import moves the human-edit lock (tasks.progress_lock_at) that the
# daily-report activity grid checks. Same idea as tasks_router.
_REIMPORT_LOCK_FIELDS = (
    "pct_complete", "status", "actual_start", "actual_finish",
    "planned_start", "planned_finish", "duration_days", "predecessors",
)


def _is_pm_or_admin(role: Optional[str]) -> bool:
    return (role or "").lower() in ("admin", "pm")


# ============================================================
# Upload → create proposal
# ============================================================
@router.post("/imports/schedule")
async def upload_schedule_proposal(
    project_id: str = Form(...),
    file: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
):
    """Parse the uploaded file, compute a diff vs the current schedule,
    run a Claude review, and PERSIST the result as a proposal with
    status='pending_review'. The tasks collection is NOT touched here.

    A PM or admin must then call /approve to commit, or /reject to discard.
    """
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    content = await file.read()
    if not content:
        raise HTTPException(400, "Uploaded file is empty.")
    parsed_rows, parse_errors = parse_schedule_file(file.filename, content)
    if parse_errors and not parsed_rows:
        raise HTTPException(400, "; ".join(parse_errors)[:512])

    # Coerce datetime.date into ISO strings — pymongo/BSON can encode datetime
    # but NOT date. The downstream applier accepts both.
    def _norm_row(r: dict) -> dict:
        out = {}
        for k, v in r.items():
            if isinstance(v, date) and not isinstance(v, datetime):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out
    parsed_rows = [_norm_row(r) for r in parsed_rows]

    # Heuristic filter — flag heading/summary rows BEFORE the diff. Free,
    # no LLM. The user can toggle individual rows or chat with Claude on
    # the proposal page to adjust.
    parsed_rows = detect_summary_rows(parsed_rows)
    visible = visible_rows(parsed_rows)

    # Snapshot current tasks for diff
    current_tasks = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "planned_start": 1,
         "planned_finish": 1, "duration_days": 1, "predecessors": 1, "pct_complete": 1},
    ).to_list(5000)

    diff = compute_schedule_diff(current_tasks, visible)

    # Final BSON-safety pass on the diff (dates → ISO strings recursively)
    def _scrub(o):
        if isinstance(o, dict):
            return {k: _scrub(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_scrub(v) for v in o]
        if isinstance(o, date) and not isinstance(o, datetime):
            return o.isoformat()
        return o
    diff = _scrub(diff)

    # Run Claude review (best-effort — falls back to heuristic if LLM unavailable)
    ai_review = await review_schedule_diff(diff, project_name=proj.get("name", ""))

    # Feature F1 (2026-05-17) -- AI-tag every parsed task with a
    # work_category. Runs AFTER parsing + diff, BEFORE the proposal is
    # persisted/reviewed. `visible` rows are the SAME dict objects that
    # sit in `parsed_rows` (visible_rows is a filter, not a copy), so
    # the tags land on parsed_rows and persist with the proposal.
    # tag_work_categories never raises -- on any AI failure every task
    # defaults to field_work and is flagged for review.
    work_category_tagging = await tag_work_categories(
        visible, project_name=proj.get("name", ""),
    )

    proposal = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "filename": file.filename,
        "kind": "schedule",
        "status": "pending_review",
        "parsed_rows": parsed_rows,
        "diff": diff,
        "ai_review": ai_review,
        "work_category_tagging": work_category_tagging,
        "uploaded_by": ctx["user"]["id"],
        "uploaded_at": utc_now().isoformat(),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_notes": None,
        "applied_result": None,
        "parse_warnings": parse_errors or [],
    }
    await db.schedule_proposals.insert_one(proposal.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="schedule_proposals", op="INSERT",
        record_id=proposal["id"], before=None,
        after={k: v for k, v in proposal.items() if k != "parsed_rows"},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    # Strip parsed_rows from the response payload — they are internal,
    # the PM only needs the diff to make a decision.
    return {
        "id": proposal["id"],
        "status": proposal["status"],
        "filename": file.filename,
        "row_count": len(parsed_rows),
        "diff": diff,
        "ai_review": ai_review,
        "work_category_tagging": work_category_tagging,
        "parse_warnings": parse_errors or [],
        "requires_approval_by": "pm_or_admin",
    }


# ============================================================
# List + detail
# ============================================================
@router.get("/projects/{project_id}/schedule-proposals")
async def list_proposals(project_id: str, status: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    q = {"project_id": project_id}
    if status:
        q["status"] = status
    docs = await db.schedule_proposals.find(
        tenant_filter(ctx["tenant_id"], q),
        {"_id": 0, "parsed_rows": 0},
    ).sort("uploaded_at", -1).to_list(200)
    return docs


@router.get("/schedule-proposals/{proposal_id}")
async def get_proposal(proposal_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}),
        {"_id": 0},  # include parsed_rows so the UI can show heading checkboxes
    )
    if not doc:
        raise HTTPException(404, "Proposal not found")
    return doc


# ============================================================
# Approve — applies parsed_rows to tasks collection
# ============================================================
async def _bg_diff_and_audit(tenant_id: str, project_id: str, proposal_id: str,
                              actor_id: Optional[str], request_id: Optional[str]) -> None:
    """Bucket C3c Phase 1 — runs after the approve_proposal request returns.
    Computes the version-to-version diff against the prior approved
    proposal and writes an audit entry. Best-effort — never raises into
    the caller because this is a BackgroundTask."""
    try:
        row = await auto_diff_on_approval(tenant_id, project_id, proposal_id)
    except Exception as e:
        # Surface to audit so operators can find it; don't crash the BG task.
        try:
            await write_audit(
                tenant_id=tenant_id, table_name="schedule_update_diffs",
                op="SCHEDULE_DIFF_GENERATION_FAILED",
                record_id=proposal_id, before=None,
                after={"error": str(e)[:500], "later_schedule_version_id": proposal_id},
                actor_id=actor_id, request_id=request_id,
            )
        except Exception:
            pass
        return
    if not row:
        return
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="schedule_update_diffs",
            op="SCHEDULE_DIFF_GENERATED",
            record_id=row["id"], before=None,
            after={
                "earlier_schedule_version_id": row["earlier_schedule_version_id"],
                "later_schedule_version_id": row["later_schedule_version_id"],
                "activities_added_count": row.get("activities_added_count", 0),
                "activities_removed_count": row.get("activities_removed_count", 0),
                "activities_modified_count": row.get("activities_modified_count", 0),
                "cp_changes_count": row.get("cp_changes_count", 0),
            },
            actor_id=actor_id, request_id=request_id,
        )
    except Exception:
        pass


@router.post("/schedule-proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    payload: ScheduleProposalDecision,
    bg: BackgroundTasks,
    ctx: dict = Depends(require_tenant),
):
    if not _is_pm_or_admin(ctx["user"].get("role")):
        raise HTTPException(403, "Only PM or Admin can approve schedule proposals")

    db = get_db()
    proposal = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0},
    )
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if proposal["status"] != "pending_review":
        raise HTTPException(409, f"Proposal already {proposal['status']}")

    project_id = proposal["project_id"]
    parsed_rows = proposal.get("parsed_rows") or []
    rows_to_apply = visible_rows(parsed_rows)
    diff = proposal.get("diff", {})

    created = 0
    updated = 0
    failed: list[dict] = []
    migrated_progress = 0   # pt.76: tasks that carried real % / actuals / status
    blocked_imported = 0    # pt.76: source rows flagged "blocked" (need a delay)
    for idx, row in enumerate(rows_to_apply):
        try:
            payload_dict = parsed_to_task_create_payload(row, project_id)
            payload_dict["sequence"] = idx  # preserve source-file ordering
            tc = validate_payload(payload_dict)
        except Exception as e:
            failed.append({"row": row.get("activity_code"), "error": str(e)})
            continue

        # pt.76 -- progress-aware import. Derive the migrated execution state
        # from whatever the source schedule provided. When NONE is present the
        # task is created/updated as a zeroed baseline (historical behavior).
        imp = tc.model_dump(mode="json")
        imp_pct = imp.get("pct_complete")
        imp_status = imp.get("status")
        imp_as = imp.get("actual_start")
        imp_af = imp.get("actual_finish")
        has_progress = any(v is not None for v in (imp_pct, imp_status, imp_as, imp_af))
        # status=complete forces 100%; else use the given % (default 0).
        mig_pct = 100.0 if imp_status == "complete" else (float(imp_pct) if imp_pct is not None else 0.0)
        # An actual start means the work began even at 0% (MS Project leaves
        # % complete at 0 when only an actual start is entered).
        mig_status = imp_status or (
            "complete" if mig_pct >= 100
            else "in_progress" if (mig_pct > 0 or imp_as) else "not_started"
        )

        existing = await db.tasks.find_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id, "activity_code": tc.activity_code}),
            {"_id": 0},
        )
        # MS Project pulls carry the file's progress only for tasks ICE had not
        # seen when the pull ran. If the task exists by approval time, ICE's
        # own progress wins -- never overwrite it with the file's.
        if (has_progress and existing and existing.get("status") != "deleted"
                and proposal.get("source") == "ms_project_pull"):
            has_progress = False
        if has_progress:
            migrated_progress += 1
        if imp_status == "blocked":
            blocked_imported += 1

        if existing:
            updates = tc.model_dump(mode="json", exclude_unset=False)
            updates.pop("project_id", None)
            # pt.76 -- NEVER let a baseline-only re-import (no progress columns)
            # wipe a task's existing execution state: drop the (None) progress
            # keys first, then re-apply only when the import actually carried
            # progress, or restore a soft-deleted row to a clean baseline.
            for _k in ("pct_complete", "actual_start", "actual_finish", "status"):
                updates.pop(_k, None)
            if existing.get("status") == "deleted" and not has_progress:
                # Previously soft-deleted + no imported progress -> RESTORE to a
                # clean baseline (otherwise the Tasks list, which filters
                # status='deleted', would show nothing despite a success).
                updates.update({
                    "status": "not_started", "pct_complete": 0, "prev_pct_complete": 0,
                    "actual_start": None, "actual_finish": None,
                    "actual_hours": 0, "actual_quantity": 0,
                })
                updates.pop("deleted_at", None)
                updates.pop("deleted_by_proposal_id", None)
            elif has_progress:
                # Re-import carrying progress -> migrate the execution state
                # (also un-deletes a soft-deleted row via the $unset below).
                updates.update({
                    "status": mig_status, "pct_complete": mig_pct,
                    "prev_pct_complete": mig_pct,
                    "actual_start": imp_as, "actual_finish": imp_af,
                })
            # Day-1 round 2 (2026-10-25) -- an approved re-import is a
            # person changing this task's schedule data: stamp updated_at and
            # the human-edit lock (same value) so a daily-report grid save
            # made against the pre-import task is caught as a real conflict.
            # The lock only moves when the import actually CHANGES this
            # task's progress/schedule values -- a full re-import rewrites
            # every row, and an unchanged row is no conflict for a super
            # who has it open in the daily report.
            _reimport_at = utc_now().isoformat()
            updates["updated_at"] = _reimport_at
            if any(k in updates and updates[k] != existing.get(k)
                   for k in _REIMPORT_LOCK_FIELDS):
                updates["progress_lock_at"] = _reimport_at
            await db.tasks.update_one(
                tenant_filter(ctx["tenant_id"], {"id": existing["id"]}),
                {"$set": updates,
                 "$unset": {"deleted_at": "", "deleted_by_proposal_id": ""}},
            )
            updated += 1
        else:
            doc = tc.model_dump(mode="json")
            doc.update({
                "id": gen_id(), "tenant_id": ctx["tenant_id"],
                "status": mig_status,
                "actual_start": imp_as, "actual_finish": imp_af,
                "actual_hours": 0, "actual_quantity": 0,
                "pct_complete": mig_pct, "prev_pct_complete": mig_pct,
                "is_critical_path": False, "total_float_days": None,
                "lookahead_status": "not_in_window",
                "created_at": utc_now().isoformat(),
            })
            await db.tasks.insert_one(doc.copy())
            created += 1

    # Drop activities that the new schedule no longer contains.
    deleted_count = 0
    for d in diff.get("dropped", []):
        ac = d.get("activity_code")
        if not ac:
            continue
        # Soft-delete to preserve audit linkage on tasks referenced elsewhere
        res = await db.tasks.update_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id, "activity_code": ac}),
            {"$set": {"status": "deleted", "deleted_at": utc_now().isoformat(),
                      "deleted_by_proposal_id": proposal_id}},
        )
        if res.modified_count:
            deleted_count += 1

    applied = {"created": created, "updated": updated, "deleted": deleted_count,
               "failed": failed,
               # pt.76 -- progress-aware import telemetry
               "migrated_progress": migrated_progress, "blocked_imported": blocked_imported}

    new_status_doc = {
        "status": "approved",
        "reviewed_by": ctx["user"]["id"],
        "reviewed_at": utc_now().isoformat(),
        "review_notes": payload.notes,
        "applied_result": applied,
    }
    await db.schedule_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}),
        {"$set": new_status_doc},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="schedule_proposals", op="UPDATE",
        record_id=proposal_id, before={"status": "pending_review"},
        after={"status": "approved", "applied_result": applied,
               "reviewed_by": ctx["user"]["id"]},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Recompute critical path now that the schedule changed.
    # CPM Mode 2 (2026-06-29): MS Project is authoritative for the computed
    # schedule on a mode2 project, so skip our compute_cpm when the proposal is
    # an ms_project_pull OR the project is on schedule_mode="mode2" (a file
    # uploaded onto a mode2 project) — our recompute would overwrite Project's
    # dates. The default Mode-1 ("file_upload" on a mode1 project) path is
    # unchanged, and with FF_SCHEDULE_MODE_2 off this is byte-identical.
    # Enterprise CPM ("Diagram 2", 2026-06-29): on an enterprise-engine project
    # OUR enterprise engine owns the schedule columns, so a file_upload applied
    # there must recompute through the engine-aware hook (auto_recompute_after_
    # event -> EnterpriseCpmEngine), NOT bare compute_cpm. mode2/ms_project_pull
    # still SKIP our recompute entirely (MS Project authoritative). With both
    # flags off this is byte-identical (plain compute_cpm).
    _skip_cpm = proposal.get("source") == "ms_project_pull"
    _route_enterprise = False
    if not _skip_cpm:
        from services.feature_flags import (
            schedule_mode_2_enabled, schedule_mode_for_project,
            enterprise_cpm_enabled, schedule_engine_for_project,
        )
        if schedule_mode_2_enabled() or enterprise_cpm_enabled():
            _proj = await db.projects.find_one(
                tenant_filter(ctx["tenant_id"], {"id": project_id}),
                {"_id": 0, "schedule_mode": 1, "schedule_engine": 1}) or {}
            if schedule_mode_2_enabled() and schedule_mode_for_project(_proj) == "mode2":
                _skip_cpm = True
            elif enterprise_cpm_enabled() and schedule_engine_for_project(_proj) == "enterprise":
                _route_enterprise = True
    if _route_enterprise:
        try:
            from services.cpm import auto_recompute_after_event
            await auto_recompute_after_event(ctx["tenant_id"], project_id)
        except Exception:
            pass
    elif not _skip_cpm:
        try:
            await compute_cpm(ctx["tenant_id"], project_id)
        except Exception:
            pass

    # Bucket H Phase 4 hook -- the schedule import is a major demand
    # event for resource leveling. Walks every task on the project and
    # rebuilds the next-90-day crew_demands window. BackgroundTask
    # because the rebuild can touch a few hundred tasks on a large
    # project; the approval response should stay fast.
    async def _bg_recompute_demand(tenant_id: str, pid: str):
        try:
            from services.resource_leveling import recompute_demand_for_project
            from audit import write_audit
            result = await recompute_demand_for_project(
                tenant_id=tenant_id, project_id=pid,
            )
            await write_audit(
                tenant_id=tenant_id, table_name="crew_demands",
                op="RESOURCE_LEVELING_RECOMPUTED",
                record_id=pid, before=None,
                after={"trigger": "schedule_import",
                        "project_id": pid, "result": result},
                actor_id=None, request_id=f"sched_import_{pid}",
            )
        except Exception:
            pass
    bg.add_task(_bg_recompute_demand, ctx["tenant_id"], project_id)

    # Bucket G Phase 2 hook -- rule-based trade inference. Scan the
    # newly-imported tasks for trade-keyword matches and drop Jigsaw
    # FieldSuggestion rows for required_trade + required_headcount on
    # every match. PM bulk-confirms in the Review Queue UI (Phase 3).
    # BackgroundTask so the approval response stays fast on large
    # schedules; the suggestions are best-effort (failures don't roll
    # back the approval).
    async def _bg_infer_trades(tenant_id: str, pid: str, request_id: str,
                                  actor_id: str):
        try:
            from services.schedule_trade_inference import infer_trades_for_project
            out = await infer_trades_for_project(
                tenant_id=tenant_id, project_id=pid,
                actor_id=actor_id, request_id=request_id,
            )
            await write_audit(
                tenant_id=tenant_id, table_name="field_suggestions",
                op="FIELD_SUGGESTIONS_SCHEDULE_TRADE_INFERENCE",
                record_id=pid, before=None,
                after={"trigger": "schedule_import",
                        "project_id": pid, "result": out},
                actor_id=actor_id, request_id=request_id,
            )
        except Exception:
            import logging
            logging.getLogger("schedule_proposals_router").warning(
                "schedule_trade_inference background task failed",
                exc_info=True,
            )
    bg.add_task(
        _bg_infer_trades, ctx["tenant_id"], project_id,
        ctx["request_id"], ctx["user"]["id"],
    )

    # Bucket C3c Phase 1 — schedule version diff against prior approved
    # proposal. Runs as BackgroundTask so the approval response stays fast
    # (diff walks both schedules and writes audit; can take seconds on
    # large projects). Idempotent so retries are safe.
    bg.add_task(
        _bg_diff_and_audit,
        ctx["tenant_id"], project_id, proposal_id,
        ctx["user"]["id"], ctx["request_id"],
    )

    # Also persist a lightweight import history row so /imports list stays accurate.
    await db.imports.insert_one({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "project_id": project_id, "kind": "schedule",
        "filename": proposal["filename"], "row_count": len(parsed_rows),
        "created": created, "updated": updated, "failed": failed,
        "errors": [], "imported_by": ctx["user"]["id"],
        "schedule_proposal_id": proposal_id,
        "created_at": utc_now().isoformat(),
    })

    return {"ok": True, "status": "approved", "applied": applied}


# ============================================================
# Reject
# ============================================================
@router.post("/schedule-proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    payload: ScheduleProposalDecision,
    ctx: dict = Depends(require_tenant),
):
    if not _is_pm_or_admin(ctx["user"].get("role")):
        raise HTTPException(403, "Only PM or Admin can reject schedule proposals")
    db = get_db()
    proposal = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0, "status": 1},
    )
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if proposal["status"] != "pending_review":
        raise HTTPException(409, f"Proposal already {proposal['status']}")
    await db.schedule_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}),
        {"$set": {
            "status": "rejected",
            "reviewed_by": ctx["user"]["id"],
            "reviewed_at": utc_now().isoformat(),
            "review_notes": payload.notes,
        }},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="schedule_proposals", op="UPDATE",
        record_id=proposal_id, before={"status": "pending_review"},
        after={"status": "rejected", "reviewed_by": ctx["user"]["id"]},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True, "status": "rejected"}


# ============================================================
# Refresh AI review (e.g., LLM was unavailable initially)
# ============================================================
@router.post("/schedule-proposals/{proposal_id}/refresh-ai-review")
async def refresh_ai_review(proposal_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    proposal = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0},
    )
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal["project_id"]}), {"_id": 0, "name": 1},
    ) or {}
    review = await review_schedule_diff(proposal.get("diff", {}), project_name=proj.get("name", ""))
    await db.schedule_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}),
        {"$set": {"ai_review": review}},
    )
    return {"ai_review": review}


# ============================================================
# Toggle individual rows in/out of the proposal (free, no LLM)
# ============================================================
class TogglePayload(StrictModel):
    activity_codes: list[str]
    excluded: bool


async def _recompute_diff_and_save(db, proposal: dict, ctx: dict):
    """After any mutation of parsed_rows, recompute the diff against the
    current tasks collection so the UI is always consistent. AI review is
    NOT auto-rerun (saves credits) — user clicks 'Re-run review' if they want.
    """
    project_id = proposal["project_id"]
    current_tasks = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "planned_start": 1,
         "planned_finish": 1, "duration_days": 1, "predecessors": 1, "pct_complete": 1},
    ).to_list(5000)
    new_diff = compute_schedule_diff(current_tasks, visible_rows(proposal["parsed_rows"]))
    # date scrub for BSON safety
    from datetime import date as _date, datetime as _datetime
    def _scrub(o):
        if isinstance(o, dict): return {k: _scrub(v) for k, v in o.items()}
        if isinstance(o, list): return [_scrub(v) for v in o]
        if isinstance(o, _date) and not isinstance(o, _datetime):
            return o.isoformat()
        return o
    new_diff = _scrub(new_diff)
    await db.schedule_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal["id"]}),
        {"$set": {"parsed_rows": proposal["parsed_rows"], "diff": new_diff}},
    )
    return new_diff


@router.post("/schedule-proposals/{proposal_id}/toggle-rows")
async def toggle_rows(
    proposal_id: str,
    payload: TogglePayload,
    ctx: dict = Depends(require_tenant),
):
    """Mark/unmark one or more rows as summary headings. Recomputes diff."""
    db = get_db()
    proposal = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0},
    )
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if proposal["status"] != "pending_review":
        raise HTTPException(409, f"Proposal already {proposal['status']}")

    codes = set(str(c) for c in payload.activity_codes)
    flagged = 0
    for r in proposal["parsed_rows"]:
        if str(r.get("activity_code")) in codes:
            r["excluded_as_summary"] = bool(payload.excluded)
            r["summary_reason"] = "user-marked" if payload.excluded else None
            flagged += 1
    new_diff = await _recompute_diff_and_save(db, proposal, ctx)
    return {"updated": flagged, "diff": new_diff}


# ============================================================
# Feature F1 (2026-05-17) -- work-category tag review.
# The schedule-import AI tagging step (services.schedule_work_category)
# classifies every parsed task. These two endpoints let the reviewer
# correct a tag or re-run the AI with extra context. Tags are METADATA
# -- they do NOT affect the diff, so neither endpoint recomputes it.
# ============================================================
class WorkCategoryOverride(StrictModel):
    activity_code: str
    work_category: str


@router.post("/schedule-proposals/{proposal_id}/set-work-category")
async def set_work_category(
    proposal_id: str,
    payload: WorkCategoryOverride,
    ctx: dict = Depends(require_tenant),
):
    """Reviewer override of one task's work_category tag. Marks the row
    as a user override so a later AI re-review can preserve it."""
    if payload.work_category not in WORK_CATEGORIES:
        raise HTTPException(
            422, f"work_category must be one of {sorted(WORK_CATEGORIES)}",
        )
    db = get_db()
    proposal = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0},
    )
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if proposal["status"] != "pending_review":
        raise HTTPException(409, f"Proposal already {proposal['status']}")
    rows = proposal.get("parsed_rows") or []
    found = set_row_work_category(
        rows, payload.activity_code, payload.work_category,
    )
    if not found:
        raise HTTPException(
            404, f"Activity {payload.activity_code} not in proposal",
        )
    await db.schedule_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}),
        {"$set": {"parsed_rows": rows}},
    )
    return {"ok": True, "activity_code": payload.activity_code,
            "work_category": payload.work_category, "source": "user"}


class RetagPayload(StrictModel):
    # Extra context the reviewer wants the model to consider.
    context_note: str = ""
    # When True, discard manual overrides and let the AI redo every
    # task. Default False -- manual overrides are preserved.
    redo_all: bool = False


@router.post("/schedule-proposals/{proposal_id}/retag-work-categories")
async def retag_work_categories(
    proposal_id: str,
    payload: RetagPayload,
    ctx: dict = Depends(require_tenant),
):
    """Ask the AI to re-review the work-category tags, optionally with a
    reviewer context note. Manual overrides are preserved unless
    redo_all is set. Never fails the request -- on AI error every
    non-preserved task defaults to field_work + needs_review."""
    db = get_db()
    proposal = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0},
    )
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if proposal["status"] != "pending_review":
        raise HTTPException(409, f"Proposal already {proposal['status']}")
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal["project_id"]}),
        {"_id": 0, "name": 1},
    ) or {}
    rows = proposal.get("parsed_rows") or []
    # Tag only the real (non-summary) tasks -- the same set the diff +
    # applier use. tag_work_categories mutates the row dicts in place.
    tagging = await tag_work_categories(
        visible_rows(rows),
        project_name=proj.get("name", ""),
        context_note=payload.context_note or "",
        skip_user_overrides=not payload.redo_all,
    )
    await db.schedule_proposals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}),
        {"$set": {"parsed_rows": rows, "work_category_tagging": tagging}},
    )
    return {"ok": True, "work_category_tagging": tagging}


# ============================================================
# Chat — let the user converse with Claude about the parsed rows.
# Each chat turn = ONE LLM call. No memory between turns (pass full context).
# Claude returns a JSON action plan: which activity_codes to exclude/include.
# ============================================================
class ChatPayload(StrictModel):
    message: str


@router.post("/schedule-proposals/{proposal_id}/chat")
async def chat_with_proposal(
    proposal_id: str,
    payload: ChatPayload,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    proposal = await db.schedule_proposals.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proposal_id}), {"_id": 0},
    )
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if proposal["status"] != "pending_review":
        raise HTTPException(409, f"Proposal already {proposal['status']}")

    # Build a compact view of the rows for the LLM
    rows_view = [
        {
            "activity_code": r.get("activity_code"),
            "description": r.get("description"),
            "wbs": r.get("wbs"),
            "duration_days": r.get("duration_days"),
            "excluded": bool(r.get("excluded_as_summary")),
            "exclude_reason": r.get("summary_reason"),
        }
        for r in proposal["parsed_rows"]
    ]

    import os
    import json
    import re
    from emergentintegrations.llm.chat import LlmChat, UserMessage

    SYS = (
        "You are reviewing a parsed construction schedule. The user wants to mark "
        "summary/heading rows so they don't get applied as tasks. You must reply "
        "with a SINGLE JSON object — no markdown, no code fences, no surrounding text. "
        "Schema:\n"
        "{\n"
        '  "exclude": ["activity_code", ...],\n'
        '  "include": ["activity_code", ...],\n'
        '  "reply": "1-2 sentence plain English reply to show the user."\n'
        "}\n"
        "Rules:\n"
        "- Only refer to activity_codes that exist in the rows array.\n"
        "- Use exclude to mark a row as a heading. Use include to UN-mark.\n"
        "- If the user asked a question (no mutation), return both arrays empty and answer in reply.\n"
        "- Reply must contain no #, *, |, or backtick characters."
    )
    api_key = os.environ.get("EMERGENT_LLM_KEY", "")
    if not api_key:
        return {"reply": "AI is unavailable (no API key configured). Use the checkbox toggles instead.",
                "exclude": [], "include": [], "diff": proposal.get("diff")}
    chat = LlmChat(
        api_key=api_key,
        session_id=f"sched-chat-{proposal_id}-{int(__import__('time').time())}",
        system_message=SYS,
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")

    user_text = (
        f"User message: {payload.message}\n\n"
        f"Rows ({len(rows_view)}):\n" + json.dumps(rows_view, default=str)
    )
    try:
        raw = await chat.send_message(UserMessage(text=user_text))
    except Exception as e:
        return {"reply": f"AI error: {str(e)[:200]}", "exclude": [], "include": [],
                "diff": proposal.get("diff")}

    raw = str(raw).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE)
    try:
        plan = json.loads(cleaned)
    except Exception:
        m = re.search(r"\{[\s\S]+\}", cleaned)
        plan = json.loads(m.group(0)) if m else {"reply": "Sorry, I could not parse my own answer.", "exclude": [], "include": []}

    exclude = set(str(c) for c in (plan.get("exclude") or []))
    include = set(str(c) for c in (plan.get("include") or []))
    valid_codes = {str(r.get("activity_code")) for r in proposal["parsed_rows"]}
    exclude &= valid_codes
    include &= valid_codes

    mutated = 0
    for r in proposal["parsed_rows"]:
        ac = str(r.get("activity_code"))
        if ac in exclude:
            r["excluded_as_summary"] = True
            r["summary_reason"] = "AI-marked"
            mutated += 1
        elif ac in include:
            r["excluded_as_summary"] = False
            r["summary_reason"] = None
            mutated += 1

    new_diff = await _recompute_diff_and_save(db, proposal, ctx) if mutated else proposal.get("diff")
    reply = re.sub(r"[`*#|]+", "", str(plan.get("reply") or ""))
    return {
        "reply": reply.strip() or "Done.",
        "exclude": list(exclude),
        "include": list(include),
        "mutated": mutated,
        "diff": new_diff,
    }
