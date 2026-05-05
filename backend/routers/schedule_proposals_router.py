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
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from typing import Optional
from datetime import datetime, date
from database import get_db, tenant_filter
from models import (
    ScheduleProposal, ScheduleProposalDecision, gen_id, utc_now,
)
from auth import require_tenant
from audit import write_audit
from services.schedule_parser import parse_schedule_file
from services.schedule_diff import compute_schedule_diff
from services.schedule_ai_review import review_schedule_diff
from services.schedule_proposal import parsed_to_task_create_payload, validate_payload
from services.cpm import compute_cpm

router = APIRouter(tags=["schedule-proposals"])


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

    # Snapshot current tasks for diff
    current_tasks = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "planned_start": 1,
         "planned_finish": 1, "duration_days": 1, "predecessors": 1, "pct_complete": 1},
    ).to_list(5000)

    diff = compute_schedule_diff(current_tasks, parsed_rows)

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
        {"_id": 0, "parsed_rows": 0},  # parsed_rows withheld until apply
    )
    if not doc:
        raise HTTPException(404, "Proposal not found")
    return doc


# ============================================================
# Approve — applies parsed_rows to tasks collection
# ============================================================
@router.post("/schedule-proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    payload: ScheduleProposalDecision,
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
    diff = proposal.get("diff", {})

    created = 0
    updated = 0
    failed: list[dict] = []
    for row in parsed_rows:
        try:
            payload_dict = parsed_to_task_create_payload(row, project_id)
            tc = validate_payload(payload_dict)
        except Exception as e:
            failed.append({"row": row.get("activity_code"), "error": str(e)})
            continue

        existing = await db.tasks.find_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id, "activity_code": tc.activity_code}),
            {"_id": 0},
        )
        if existing:
            updates = tc.model_dump(mode="json", exclude_unset=False)
            updates.pop("project_id", None)
            await db.tasks.update_one(
                tenant_filter(ctx["tenant_id"], {"id": existing["id"]}),
                {"$set": updates},
            )
            updated += 1
        else:
            doc = tc.model_dump(mode="json")
            doc.update({
                "id": gen_id(), "tenant_id": ctx["tenant_id"],
                "status": "not_started",
                "actual_start": None, "actual_finish": None,
                "actual_hours": 0, "actual_quantity": 0,
                "pct_complete": 0, "prev_pct_complete": 0,
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

    applied = {"created": created, "updated": updated, "deleted": deleted_count, "failed": failed}

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
    try:
        await compute_cpm(ctx["tenant_id"], project_id)
    except Exception:
        pass

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
