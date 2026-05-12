"""Field events: delays, safety events, QC issues, photos. Master prompt §4.7."""
from datetime import date as _date, datetime as _datetime
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from database import get_db, tenant_filter
from models import (
    Delay, DelayCreate, DelayUpdate, SafetyEvent, SafetyEventCreate, SafetyEventUpdate,
    QcIssue, QcIssueCreate, QcIssueUpdate, QcReworkOutcome,
    Photo, PhotoCreate, PhotoUpdate, PhotoPinPayload,
    gen_id, utc_now,
)
from auth import require_tenant
from audit import write_audit

router = APIRouter(tags=["field-events"])


# --- Delays ---

async def _resolve_task_impacts(db, tenant_id: str, payload_dict: dict) -> tuple[list[str], list[dict]]:
    """Build the linked_task_ids + per_task_impacts arrays for a delay
    create/update. Snapshots each task's float at logging time and
    derives consumes_float_only / pushes_critical_path. Returns
    (linked_task_ids, per_task_impacts)."""
    impacts_in: list[dict] = list(payload_dict.get("per_task_impacts") or [])
    explicit_ids: list[str] = list(payload_dict.get("linked_task_ids") or [])

    # Backward-compat: if the caller sent the legacy single schedule_activity_id
    # + scalar delay_days but no per_task_impacts, synthesize one.
    if not impacts_in and not explicit_ids and payload_dict.get("schedule_activity_id"):
        explicit_ids = [payload_dict["schedule_activity_id"]]
        impacts_in = [{
            "task_id": payload_dict["schedule_activity_id"],
            "projected_impact_days": float(payload_dict.get("delay_days") or 0),
        }]

    # Union of ids referenced in either array
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for tid in explicit_ids + [i.get("task_id") for i in impacts_in if i.get("task_id")]:
        if tid and tid not in seen:
            seen.add(tid)
            ordered_ids.append(tid)

    if not ordered_ids:
        return [], []

    # Pull current float / critical-path snapshot for the referenced tasks
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"id": {"$in": ordered_ids}}),
        {"_id": 0, "id": 1, "total_float_days": 1, "is_critical_path": 1},
    ).to_list(len(ordered_ids))
    by_id = {t["id"]: t for t in tasks}

    impacts_by_id = {i["task_id"]: i for i in impacts_in if i.get("task_id")}
    out: list[dict] = []
    for tid in ordered_ids:
        t = by_id.get(tid) or {}
        proj = float((impacts_by_id.get(tid) or {}).get("projected_impact_days") or 0)
        f = t.get("total_float_days")
        f_val = float(f) if f is not None else None
        consumes = f_val is not None and proj <= f_val
        pushes_cp = (not consumes) and bool(t.get("is_critical_path"))
        out.append({
            "task_id": tid,
            "projected_impact_days": proj,
            "float_at_logging_days": f_val,
            "consumes_float_only": consumes,
            "pushes_critical_path": pushes_cp,
        })
    return ordered_ids, out


def _enrich_delay_for_read(d: dict) -> dict:
    """Compute duration_days from logged/resolved dates. Mongo dicts arrive
    with ISO date strings, so parse and produce an int day count."""
    logged = d.get("logged_date") or d.get("date")
    resolved = d.get("resolved_date")
    if logged and resolved:
        try:
            ld = _date.fromisoformat(str(logged)[:10])
            rd = _date.fromisoformat(str(resolved)[:10])
            d["duration_days"] = max(0, (rd - ld).days)
        except Exception:
            d["duration_days"] = None
    elif logged and d.get("status") in ("open", "in_progress"):
        try:
            ld = _date.fromisoformat(str(logged)[:10])
            d["duration_days"] = max(0, (_date.today() - ld).days)
        except Exception:
            d["duration_days"] = None
    return d


@router.post("/delays", response_model=Delay)
async def create_delay(payload: DelayCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    # Addition 5 (2026-05-12) -- "add to locked DR" flow. Strip the
    # add_reason out of the record (it's metadata, stamped by
    # stamp_add_metadata) and pass it to enforce_locked_dr_add_policy.
    add_reason = doc.pop("add_reason", None)
    # Bucket D Corrective (2026-05-08) -- the dr-activity linkage that
    # used to be validated here was a wrong-scope build. Canonical
    # linkage is linked_task_ids (master schedule). _resolve_task_impacts
    # below pulls/synthesizes the list (including legacy
    # schedule_activity_id back-compat); we then validate min-1 +
    # cross-project against tasks via the master-schedule helper.
    linked_ids, impacts = await _resolve_task_impacts(db, ctx["tenant_id"], doc)
    from services.dr_activity_linkage import validate_linked_task_ids
    linked_ids = await validate_linked_task_ids(
        tenant_id=ctx["tenant_id"],
        project_id=doc["project_id"],
        task_ids=linked_ids,
        required=True,
    )
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "status": doc.get("status") or "open",
        "linked_task_ids": linked_ids,
        "per_task_impacts": impacts,
        "logged_date": doc.get("date"),
        "resolved_date": None,
        "resolved_note": None,
        "resolved_by_user_id": None,
        "ai_report": None,
        "ai_report_generated_at": None,
        "reported_by": ctx["user"]["id"], "reported_at": utc_now().isoformat(),
        "created_at": utc_now().isoformat(),
    })
    # Addition 5 -- on direct-to-locked-DR add (NOT supplement path),
    # check the tiered add policy. Supplements still go through the
    # supplement_parent path below.
    is_locked_add = False
    if not doc.get("is_supplement") and doc.get("daily_report_id"):
        from services.edit_permissions import enforce_locked_dr_add_policy
        _dr, is_locked_add = await enforce_locked_dr_add_policy(
            ctx=ctx, daily_report_id=doc["daily_report_id"],
            add_reason=add_reason, event_type="delay",
        )
    # Bucket D Phase 4 -- supplement metadata + audit (no-op for normal
    # creates; raises 422 + writes SUPPLEMENT_FILED for supplements).
    from services.dr_activity_linkage import (
        stamp_supplement_metadata, validate_supplement_parent_locked,
    )
    if doc.get("is_supplement") and doc.get("parent_daily_report_id"):
        await validate_supplement_parent_locked(
            tenant_id=ctx["tenant_id"],
            parent_daily_report_id=doc["parent_daily_report_id"],
        )
    sup_audit = stamp_supplement_metadata(doc, actor_id=ctx["user"]["id"], fact_type="delay")
    await db.delays.insert_one(doc.copy())
    await write_audit(tenant_id=ctx["tenant_id"], table_name="delays", op="INSERT",
                      record_id=doc["id"], before=None, after=doc,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if sup_audit:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="delays", op="SUPPLEMENT_FILED",
            record_id=doc["id"], before=None, after=sup_audit,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    # Addition 5 -- stamp post_submit_added + write EVENT_ADDED_POST_SUBMIT
    # audit. Runs after INSERT so the audit captures the persisted row.
    if is_locked_add:
        from services.edit_permissions import stamp_add_metadata
        await stamp_add_metadata(
            db=db, collection="delays", record_id=doc["id"],
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            add_reason=add_reason or "", after=doc,
            request_id=ctx["request_id"],
        )
    # Bucket C1 Phase 2 — opening a delay flips has_open_blocker on every
    # linked task and may shift its computed_status to 'blocked'.
    # Bucket C1 Phase 4 — and pushes downstream projected_finish dates.
    from services.task_status import compute_and_persist_status, compute_projected_finish_dates
    for tid in (linked_ids or []):
        await compute_and_persist_status(db, ctx["tenant_id"], tid,
                                          actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if linked_ids:
        await compute_projected_finish_dates(db, ctx["tenant_id"], doc["project_id"],
                                              actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return Delay(**_enrich_delay_for_read(doc))


@router.get("/projects/{project_id}/delays", response_model=list[Delay])
async def list_delays(project_id: str,
                      affected_activity_id: Optional[str] = None,
                      ctx: dict = Depends(require_tenant)):
    db = get_db()
    # Filter soft-deleted leftovers from before the hard-delete fix
    # (status='deleted' was set by the old /crud/delays/{id} DELETE path).
    f = {"project_id": project_id, "status": {"$ne": "deleted"}}
    if affected_activity_id:
        # Bucket D Corrective -- gallery filter now matches against
        # master-schedule task ids (linked_task_ids), not the dropped
        # per-DR linked_dr_activity_ids field.
        f["linked_task_ids"] = affected_activity_id
    docs = await db.delays.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("date", -1).to_list(500)
    return [Delay(**_enrich_delay_for_read(d)) for d in docs]


@router.get("/delays/{delay_id}", response_model=Delay)
async def get_delay(delay_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    d = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})
    if not d:
        raise HTTPException(404, "Delay not found")
    return Delay(**_enrich_delay_for_read(d))


@router.patch("/delays/{delay_id}", response_model=Delay)
async def update_delay(delay_id: str, payload: DelayUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    before = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Delay not found")
    raw = payload.model_dump(mode="json", exclude_unset=True)
    edit_reason = raw.pop("edit_reason", None)  # not a record field
    updates: dict = {k: v for k, v in raw.items() if v is not None}

    # Addition 4 -- locked-DR edit policy (tiered permissions + reason
    # + post-submit audit). On draft DRs returns is_locked_edit=False
    # and we skip the audit ceremony.
    from services.edit_permissions import (
        enforce_locked_dr_edit_policy, stamp_edit_metadata,
    )
    _parent_dr, is_locked_edit = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before.get("daily_report_id"),
        edit_reason=edit_reason, event_type="delay", event_id=delay_id,
    )

    # Bucket D Corrective -- the dr-activity re-validation that used to
    # live here is gone (field dropped). linked_task_ids re-validation
    # against master schedule happens after _resolve_task_impacts via
    # validate_linked_task_ids below.

    # If linked_task_ids OR per_task_impacts is in the patch, recompute the
    # snapshot — keep logic in one place rather than two slightly-different
    # branches.
    if "linked_task_ids" in updates or "per_task_impacts" in updates:
        merged = {
            "linked_task_ids": updates.get("linked_task_ids", before.get("linked_task_ids", [])),
            "per_task_impacts": updates.get("per_task_impacts", before.get("per_task_impacts", [])),
        }
        new_ids, new_impacts = await _resolve_task_impacts(db, ctx["tenant_id"], merged)
        # Bucket D Corrective -- validate the resolved task ids against
        # master schedule (cross-project safety + min-1 invariant).
        from services.dr_activity_linkage import validate_linked_task_ids
        new_ids = await validate_linked_task_ids(
            tenant_id=ctx["tenant_id"],
            project_id=before["project_id"],
            task_ids=new_ids,
            required=True,
        )
        updates["linked_task_ids"] = new_ids
        updates["per_task_impacts"] = new_impacts

    # Status transition → resolved: stamp resolver fields if not provided.
    # Note (Item 1, 2026-05-06): we deliberately do NOT auto-fill
    # resolved_date from utc_now().date() — the server's UTC date can be
    # tomorrow relative to the user (e.g. EST 8 PM = next-day UTC), which
    # caused same-day resolutions to render as 1 day open. The frontend
    # always supplies resolved_date from a date picker prefilled with the
    # user's LOCAL today, so the field arrives correct. Resolved_by we
    # still stamp here because it isn't user-input.
    if updates.get("status") == "resolved":
        if "resolved_by_user_id" not in updates:
            updates["resolved_by_user_id"] = ctx["user"]["id"]
    # Any AI-report write also stamps generated_at
    if "ai_report" in updates:
        updates["ai_report_generated_at"] = utc_now().isoformat()

    await db.delays.update_one(
        tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"$set": updates},
    )
    after = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})

    # Bucket C1 Phase 5 — when a delay transitions to resolved, compute
    # actual-vs-projected impact per linked task and stamp a release/
    # absorb summary on the delay so the AI report and detail page can
    # surface it. Pure calculation; the projected_finish recompute below
    # picks up the resolved state (excluded from the open-delay sum).
    if updates.get("status") == "resolved" and before.get("status") != "resolved":
        await _stamp_resolution_release(db, ctx["tenant_id"], delay_id, after)
        # Bucket C2 Phase 3 — if a mitigation was chosen for this delay,
        # flip it to 'executed' so the frontend knows to prompt for
        # actuals. The transition is silent — no audit entry needed
        # because the user-facing OUTCOME_RECORD audit fires when actuals
        # are recorded via /mitigations/{id}/record-outcome.
        if after.get("chosen_mitigation_id"):
            await db.mitigations.update_one(
                {"tenant_id": ctx["tenant_id"], "id": after["chosen_mitigation_id"],
                 "decision_status": "chosen"},
                {"$set": {"decision_status": "executed",
                           "updated_at": utc_now().isoformat()}},
            )
        after = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})

    await write_audit(tenant_id=ctx["tenant_id"], table_name="delays", op="UPDATE",
                      record_id=delay_id, before=before, after=after,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])

    # Addition 4 -- if this was a locked-DR edit, stamp edit metadata +
    # write the EVENT_EDITED_POST_SUBMIT audit entry alongside the
    # regular UPDATE audit so TIA Section 12 surfaces edits prominently.
    if is_locked_edit:
        after = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})
        await stamp_edit_metadata(
            db=db, collection="delays", record_id=delay_id,
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            edit_reason=edit_reason, before=before, after=after,
            request_id=ctx["request_id"],
        )

    # Bucket C1 Phase 2 + 4 — recompute status for the union of OLD and
    # NEW linked tasks (in case linkage changed) AND walk projected_finish
    # forward across the project (a status change on one task can shift
    # downstream activities even if their own linkage didn't move).
    from services.task_status import compute_and_persist_status, compute_projected_finish_dates
    affected_task_ids = list({*(before.get("linked_task_ids") or []),
                               *(after.get("linked_task_ids") or [])})
    for tid in affected_task_ids:
        await compute_and_persist_status(db, ctx["tenant_id"], tid,
                                          actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if affected_task_ids:
        await compute_projected_finish_dates(db, ctx["tenant_id"], after["project_id"],
                                              actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return Delay(**_enrich_delay_for_read(after))


async def _stamp_resolution_release(db, tenant_id: str, delay_id: str, delay: dict) -> None:
    """Bucket C1 Phase 5 — at resolution time, compute actual elapsed days
    vs each linked task's projected_impact_days, then stash the breakdown
    on the delay record. Frontend Delay detail page + AI report read this.
    Schedule float restoration happens automatically because the
    subsequent compute_projected_finish_dates call no longer counts this
    delay in its open-delay sum (status='resolved' excludes it)."""
    logged = delay.get("logged_date") or delay.get("date")
    resolved = delay.get("resolved_date")
    actual_days = 0
    if logged and resolved:
        try:
            ld = _date.fromisoformat(str(logged)[:10])
            rd = _date.fromisoformat(str(resolved)[:10])
            actual_days = max(0, (rd - ld).days)
        except Exception:
            actual_days = 0

    cpm_impact: list[dict] = []
    released: list[str] = []
    for imp in (delay.get("per_task_impacts") or []):
        proj = float(imp.get("projected_impact_days") or 0)
        # Released float = projected - actual (clamped at 0). Schedule
        # absorbs min(actual, projected); the remainder is restored.
        absorbed = min(float(actual_days), proj)
        restored = max(0.0, proj - float(actual_days))
        cpm_impact.append({
            "task_id": imp.get("task_id"),
            "projected_impact_days": proj,
            "actual_days_open": actual_days,
            "absorbed_days": absorbed,
            "restored_float_days": restored,
            "projected_finish_shift_days": absorbed,
        })
        if restored > 0 and imp.get("task_id"):
            released.append(imp["task_id"])

    await db.delays.update_one(
        {"id": delay_id},
        {"$set": {
            "cpm_impact_summary": cpm_impact,
            "released_float_on_resolution": released,
        }},
    )


@router.delete("/delays/{delay_id}")
async def delete_delay(delay_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    before = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Delay not found")
    await db.delays.delete_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}))
    await write_audit(tenant_id=ctx["tenant_id"], table_name="delays", op="DELETE",
                      record_id=delay_id, before=before, after=None,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    # Bucket C1 Phase 2 + 4 — deleting a delay may unblock its linked
    # tasks AND shift downstream projected_finish dates back in.
    from services.task_status import compute_and_persist_status, compute_projected_finish_dates
    for tid in (before.get("linked_task_ids") or []):
        await compute_and_persist_status(db, ctx["tenant_id"], tid,
                                          actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if before.get("linked_task_ids"):
        await compute_projected_finish_dates(db, ctx["tenant_id"], before["project_id"],
                                              actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return {"ok": True}


@router.post("/delays/{delay_id}/generate-ai-report", response_model=Delay)
async def generate_delay_ai_report(delay_id: str, ctx: dict = Depends(require_tenant)):
    """Generate (or regenerate) the AI narrative for this delay and persist
    it on the delay record. Idempotent — overwrites previous report."""
    from services.ai_agents import generate_delay_report
    db = get_db()
    delay = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})
    if not delay:
        raise HTTPException(404, "Delay not found")
    delay = _enrich_delay_for_read(delay)
    report = await generate_delay_report(tenant_id=ctx["tenant_id"], delay=delay)
    await db.delays.update_one(
        tenant_filter(ctx["tenant_id"], {"id": delay_id}),
        {"$set": {"ai_report": report, "ai_report_generated_at": utc_now().isoformat()}},
    )
    after = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})
    return Delay(**_enrich_delay_for_read(after))


@router.get("/delays/{delay_id}/ai-report/pdf")
async def export_delay_ai_report_pdf(delay_id: str, ctx: dict = Depends(require_tenant)):
    """Stream a WeasyPrint PDF of the AI delay report. 422 if the report
    has not been generated yet — UI should generate first."""
    from fastapi import Response
    from weasyprint import HTML
    db = get_db()
    delay = await db.delays.find_one(tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0})
    if not delay:
        raise HTTPException(404, "Delay not found")
    if not delay.get("ai_report"):
        raise HTTPException(422, "AI report not generated yet — call /generate-ai-report first")
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": delay["project_id"]}),
        {"_id": 0, "name": 1},
    ) or {}

    def esc(s):
        return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    delay = _enrich_delay_for_read(delay)

    # Item 3 (2026-05-06) — render WBS code + description for linked tasks
    # rather than a truncated UUID. Falls back to activity_code if wbs is
    # missing, and to the truncated id if neither is set (impossible in
    # practice but cheap to guard).
    impacts = delay.get("per_task_impacts") or []
    task_ids = [i["task_id"] for i in impacts if i.get("task_id")]
    task_lookup: dict[str, dict] = {}
    if task_ids:
        task_docs = await db.tasks.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": task_ids}}),
            {"_id": 0, "id": 1, "wbs": 1, "activity_code": 1, "description": 1},
        ).to_list(len(task_ids))
        task_lookup = {t["id"]: t for t in task_docs}

    def _task_label(task_id: str) -> str:
        t = task_lookup.get(task_id) or {}
        code = t.get("wbs") or t.get("activity_code") or (task_id[:8] if task_id else "?")
        desc = t.get("description") or ""
        return f"{code} — {desc}" if desc else code

    impact_rows = "".join(
        f"<tr><td>{esc(_task_label(i.get('task_id') or ''))}</td>"
        f"<td class='num'>{esc(i.get('projected_impact_days'))}</td>"
        f"<td class='num'>{esc(i.get('float_at_logging_days'))}</td>"
        f"<td>{('within float' if i.get('consumes_float_only') else 'pushes CP' if i.get('pushes_critical_path') else 'exceeds non-CP float')}</td></tr>"
        for i in impacts
    ) or "<tr><td colspan='4' class='muted'>No linked tasks.</td></tr>"

    body_html = esc(delay.get("ai_report") or "").replace("\n", "<br/>")

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><style>
@page {{ size: Letter; margin: 0.75in; }}
body {{ font-family: 'Helvetica', sans-serif; color: #0f172a; font-size: 10pt; }}
h1 {{ font-size: 17pt; margin: 0; }}
.meta {{ margin-top: 4px; font-size: 9pt; color: #475569; }}
.banner {{ background: #f1f5f9; border-left: 3px solid #d97706; padding: 8px 12px; margin: 12px 0; font-size: 9pt; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 9pt; }}
th {{ background: #1e293b; color: white; padding: 6px; text-align: left; font-weight: 600; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #e2e8f0; }}
.code {{ font-family: monospace; font-size: 8pt; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.muted {{ color: #64748b; }}
h2 {{ font-size: 11pt; margin: 18px 0 6px; color: #1e293b; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; }}
.report {{ font-size: 10pt; line-height: 1.55; margin-top: 6px; }}
</style></head><body>
<h1>AI Delay Report — {esc(proj.get('name') or delay.get('project_id'))}</h1>
<div class='meta'>Delay logged: <b>{esc(delay.get('logged_date') or delay.get('date'))}</b>
  · severity <b>{esc(delay.get('severity'))}</b>
  · status <b>{esc(delay.get('status'))}</b>
  · {esc(delay.get('duration_days') or 0)}d open</div>
<div class='banner'><b>Description:</b> {esc(delay.get('description'))}</div>

<h2>Linked tasks · projected impact at logging</h2>
<table>
<thead><tr><th>Task</th><th>Impact (d)</th><th>Float at log (d)</th><th>Verdict</th></tr></thead>
<tbody>{impact_rows}</tbody>
</table>

<h2>AI analysis</h2>
<div class='report'>{body_html}</div>

<div class='meta' style='margin-top:24px;font-size:8pt;color:#94a3b8;'>
  Generated {esc(delay.get('ai_report_generated_at'))} · ICE Schedule Ops
</div>
</body></html>"""
    pdf = HTML(string=html).write_pdf()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="delay_{delay_id[:8]}_ai_report.pdf"'},
    )


@router.get("/daily-reports/{daily_report_id}/delay-reminders", response_model=list[Delay])
async def list_delay_reminders(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Open/in-progress delays whose linked_task_ids intersect with the
    grid for this daily report. Returns the delay records themselves so
    the frontend can render a banner card list. Empty list = no banner."""
    db = get_db()
    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}),
        {"_id": 0, "id": 1, "project_id": 1},
    )
    if not dr:
        raise HTTPException(404, "Daily report not found")
    grid = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id},
        {"_id": 0, "schedule_activity_id": 1},
    ).to_list(500)
    grid_task_ids = {r["schedule_activity_id"] for r in grid if r.get("schedule_activity_id")}
    if not grid_task_ids:
        return []
    open_delays = await db.delays.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": dr["project_id"],
            "status": {"$in": ["open", "in_progress"]},
            "linked_task_ids": {"$in": list(grid_task_ids)},
        }),
        {"_id": 0},
    ).sort("date", -1).to_list(200)
    return [Delay(**_enrich_delay_for_read(d)) for d in open_delays]


# --- Safety ---
@router.post("/safety-events", response_model=SafetyEvent)
async def create_safety(payload: SafetyEventCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    # Addition 5 (2026-05-12) -- "add to locked DR" flow.
    add_reason = doc.pop("add_reason", None)
    # Bucket D Corrective -- master-schedule task linkage OPTIONAL for
    # safety (some events are area-wide and don't tie to a specific
    # task). Validation just confirms ids resolve to project tasks if
    # provided.
    from services.dr_activity_linkage import validate_linked_task_ids
    doc["linked_task_ids"] = await validate_linked_task_ids(
        tenant_id=ctx["tenant_id"],
        project_id=doc["project_id"],
        task_ids=doc.get("linked_task_ids") or [],
        required=False,
    )
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "open",
        "reported_by": ctx["user"]["id"], "reported_at": utc_now().isoformat(),
        "created_at": utc_now().isoformat(),
    })
    # Addition 5 -- locked-DR add policy (skipped on supplement path).
    is_locked_add = False
    if not doc.get("is_supplement") and doc.get("daily_report_id"):
        from services.edit_permissions import enforce_locked_dr_add_policy
        _dr, is_locked_add = await enforce_locked_dr_add_policy(
            ctx=ctx, daily_report_id=doc["daily_report_id"],
            add_reason=add_reason, event_type="safety",
        )
    # Bucket D Phase 4 -- supplement metadata + audit.
    from services.dr_activity_linkage import (
        stamp_supplement_metadata, validate_supplement_parent_locked,
    )
    if doc.get("is_supplement") and doc.get("parent_daily_report_id"):
        await validate_supplement_parent_locked(
            tenant_id=ctx["tenant_id"],
            parent_daily_report_id=doc["parent_daily_report_id"],
        )
    sup_audit = stamp_supplement_metadata(doc, actor_id=ctx["user"]["id"], fact_type="safety_event")
    await db.safety_events.insert_one(doc.copy())
    await write_audit(tenant_id=ctx["tenant_id"], table_name="safety_events", op="INSERT",
                      record_id=doc["id"], before=None, after=doc,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if sup_audit:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="safety_events", op="SUPPLEMENT_FILED",
            record_id=doc["id"], before=None, after=sup_audit,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    if is_locked_add:
        from services.edit_permissions import stamp_add_metadata
        await stamp_add_metadata(
            db=db, collection="safety_events", record_id=doc["id"],
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            add_reason=add_reason or "", after=doc,
            request_id=ctx["request_id"],
        )
    return SafetyEvent(**doc)


@router.get("/projects/{project_id}/safety-events", response_model=list[SafetyEvent])
async def list_safety(project_id: str,
                       affected_activity_id: Optional[str] = None,
                       ctx: dict = Depends(require_tenant)):
    db = get_db()
    # Filter soft-deleted leftovers from the old /crud/ DELETE path.
    f = {"project_id": project_id, "status": {"$ne": "deleted"}}
    if affected_activity_id:
        # Bucket D Corrective -- gallery filter on master-schedule tasks.
        f["linked_task_ids"] = affected_activity_id
    docs = await db.safety_events.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("date", -1).to_list(500)
    return [SafetyEvent(**d) for d in docs]


# Addition 4 -- post-submit edit endpoint for safety_events. Same
# locked-DR edit policy + audit pattern as delays/rfis/photos.
@router.patch("/safety-events/{event_id}", response_model=SafetyEvent)
async def update_safety_event(
    event_id: str, payload: SafetyEventUpdate,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    before = await db.safety_events.find_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Safety event not found")
    raw = payload.model_dump(mode="json", exclude_unset=True)
    edit_reason = raw.pop("edit_reason", None)
    updates = {k: v for k, v in raw.items() if v is not None}
    if not updates and not edit_reason:
        return SafetyEvent(**before)
    from services.edit_permissions import (
        enforce_locked_dr_edit_policy, stamp_edit_metadata,
    )
    _parent_dr, is_locked_edit = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before.get("daily_report_id"),
        edit_reason=edit_reason, event_type="safety", event_id=event_id,
    )
    if "linked_task_ids" in updates:
        # Bucket D Corrective -- master-schedule re-validation on PATCH.
        from services.dr_activity_linkage import validate_linked_task_ids
        updates["linked_task_ids"] = await validate_linked_task_ids(
            tenant_id=ctx["tenant_id"],
            project_id=before["project_id"],
            task_ids=updates["linked_task_ids"],
            required=False,
        )
    await db.safety_events.update_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"$set": updates},
    )
    after = await db.safety_events.find_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="safety_events", op="UPDATE",
        record_id=event_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if is_locked_edit:
        after = await db.safety_events.find_one(
            tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
        )
        await stamp_edit_metadata(
            db=db, collection="safety_events", record_id=event_id,
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            edit_reason=edit_reason, before=before, after=after,
            request_id=ctx["request_id"],
        )
    return SafetyEvent(**after)


# --- QC ---
@router.post("/qc-issues", response_model=QcIssue)
async def create_qc(payload: QcIssueCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    # Addition 5 (2026-05-12) -- "add to locked DR" flow.
    add_reason = doc.pop("add_reason", None)
    # Bucket D Corrective -- master-schedule task linkage required (QC
    # drives the rework cascade in Phase 5; the linkage is the cascade's
    # seed). Cascade reads doc["linked_task_ids"] downstream.
    from services.dr_activity_linkage import validate_linked_task_ids
    doc["linked_task_ids"] = await validate_linked_task_ids(
        tenant_id=ctx["tenant_id"],
        project_id=doc["project_id"],
        task_ids=doc.get("linked_task_ids") or [],
        required=True,
    )
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "open",
        "reported_by": ctx["user"]["id"], "reported_at": utc_now().isoformat(),
        "resolved_at": None, "resolution_notes": None,
        "created_at": utc_now().isoformat(),
    })
    # Addition 5 -- locked-DR add policy (skipped on supplement path).
    is_locked_add = False
    if not doc.get("is_supplement") and doc.get("daily_report_id"):
        from services.edit_permissions import enforce_locked_dr_add_policy
        _dr, is_locked_add = await enforce_locked_dr_add_policy(
            ctx=ctx, daily_report_id=doc["daily_report_id"],
            add_reason=add_reason, event_type="qc",
        )
    # Bucket D Phase 5 -- if severity=rework_required, super-entered
    # rework_duration_hours is required. Stamp the estimate metadata.
    if doc.get("severity") == "rework_required":
        if not doc.get("rework_duration_hours") or float(doc.get("rework_duration_hours") or 0) <= 0:
            raise HTTPException(
                422, "rework_duration_hours required when severity='rework_required'",
            )
        if not (doc.get("rework_duration_basis") or "").strip():
            raise HTTPException(
                422, "rework_duration_basis required when severity='rework_required'",
            )
        doc["rework_estimated_at"] = utc_now().isoformat()
        doc["rework_estimated_by"] = ctx["user"]["id"]
    # Bucket D Phase 4 -- supplement metadata + audit.
    from services.dr_activity_linkage import (
        stamp_supplement_metadata, validate_supplement_parent_locked,
    )
    if doc.get("is_supplement") and doc.get("parent_daily_report_id"):
        await validate_supplement_parent_locked(
            tenant_id=ctx["tenant_id"],
            parent_daily_report_id=doc["parent_daily_report_id"],
        )
    sup_audit = stamp_supplement_metadata(doc, actor_id=ctx["user"]["id"], fact_type="qc_issue")
    await db.qc_issues.insert_one(doc.copy())
    await write_audit(tenant_id=ctx["tenant_id"], table_name="qc_issues", op="INSERT",
                      record_id=doc["id"], before=None, after=doc,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if sup_audit:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="qc_issues", op="SUPPLEMENT_FILED",
            record_id=doc["id"], before=None, after=sup_audit,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    if is_locked_add:
        from services.edit_permissions import stamp_add_metadata
        await stamp_add_metadata(
            db=db, collection="qc_issues", record_id=doc["id"],
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            add_reason=add_reason or "", after=doc,
            request_id=ctx["request_id"],
        )
    # Bucket D Phase 5 -- trigger the rework cascade if severity asks
    # for it. The synthetic delay write + CPM cascade run inside the
    # service; this endpoint just kicks it off.
    if doc.get("severity") == "rework_required":
        from services.qc_rework_cascade import create_synthetic_delay_for_qc
        try:
            synthetic = await create_synthetic_delay_for_qc(
                tenant_id=ctx["tenant_id"], qc_issue=doc,
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
            if synthetic:
                doc["linked_synthetic_delay_id"] = synthetic["id"]
        except Exception as e:
            # Don't fail the QC create if the cascade hiccups; the QC
            # row exists and a follow-up trigger can re-run the cascade.
            await write_audit(
                tenant_id=ctx["tenant_id"], table_name="qc_issues",
                op="QC_REWORK_CASCADE_FAILED",
                record_id=doc["id"], before=None,
                after={"error": str(e)[:500]},
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
    return QcIssue(**doc)


# Bucket D Phase 5 -- AI benchmark suggestion + outcome capture.
@router.get("/projects/{project_id}/qc-issues/rework-benchmark")
async def qc_rework_benchmark(project_id: str, ctx: dict = Depends(require_tenant)):
    """Returns a calibration RAG suggestion for rework hours based on
    prior outcomes recorded on this project. Pure advisory -- super's
    estimate stays authoritative."""
    from services.qc_rework_cascade import suggest_ai_benchmark
    return await suggest_ai_benchmark(ctx["tenant_id"], project_id)


@router.post("/qc-issues/{qc_id}/record-rework-outcome")
async def record_qc_rework_outcome(
    qc_id: str, payload: QcReworkOutcome,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    qc = await db.qc_issues.find_one(
        tenant_filter(ctx["tenant_id"], {"id": qc_id}), {"_id": 0},
    )
    if not qc:
        raise HTTPException(404, "QC issue not found")
    if qc.get("severity") != "rework_required":
        raise HTTPException(409, "QC issue is not flagged rework_required")
    from services.qc_rework_cascade import record_rework_outcome
    return await record_rework_outcome(
        tenant_id=ctx["tenant_id"], qc_issue=qc,
        actual_hours=payload.rework_actual_hours,
        notes=payload.rework_outcome_notes,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )


@router.get("/projects/{project_id}/qc-issues", response_model=list[QcIssue])
async def list_qc(project_id: str,
                   affected_activity_id: Optional[str] = None,
                   ctx: dict = Depends(require_tenant)):
    db = get_db()
    # Filter soft-deleted leftovers from the old /crud/ DELETE path.
    f = {"project_id": project_id, "status": {"$ne": "deleted"}}
    if affected_activity_id:
        # Bucket D Corrective -- gallery filter on master-schedule tasks.
        f["linked_task_ids"] = affected_activity_id
    docs = await db.qc_issues.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("date", -1).to_list(500)
    return [QcIssue(**d) for d in docs]


# Addition 4 -- post-submit edit endpoint for qc_issues. Note: the
# Phase 5 cascade is one-shot at create time (severity=rework_required
# triggers synthetic delay), so this endpoint does NOT re-trigger the
# cascade if severity is patched after create. Admin-only severity
# flips that need a fresh cascade should delete the issue and recreate.
@router.patch("/qc-issues/{qc_id}", response_model=QcIssue)
async def update_qc_issue(
    qc_id: str, payload: QcIssueUpdate,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    before = await db.qc_issues.find_one(
        tenant_filter(ctx["tenant_id"], {"id": qc_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "QC issue not found")
    raw = payload.model_dump(mode="json", exclude_unset=True)
    edit_reason = raw.pop("edit_reason", None)
    updates = {k: v for k, v in raw.items() if v is not None}
    if not updates and not edit_reason:
        return QcIssue(**before)
    from services.edit_permissions import (
        enforce_locked_dr_edit_policy, stamp_edit_metadata,
    )
    _parent_dr, is_locked_edit = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before.get("daily_report_id"),
        edit_reason=edit_reason, event_type="qc", event_id=qc_id,
    )
    if "linked_task_ids" in updates:
        # Bucket D Corrective -- master-schedule re-validation on PATCH.
        from services.dr_activity_linkage import validate_linked_task_ids
        updates["linked_task_ids"] = await validate_linked_task_ids(
            tenant_id=ctx["tenant_id"],
            project_id=before["project_id"],
            task_ids=updates["linked_task_ids"],
            required=True,
        )
    await db.qc_issues.update_one(
        tenant_filter(ctx["tenant_id"], {"id": qc_id}), {"$set": updates},
    )
    after = await db.qc_issues.find_one(
        tenant_filter(ctx["tenant_id"], {"id": qc_id}), {"_id": 0},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="qc_issues", op="UPDATE",
        record_id=qc_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if is_locked_edit:
        after = await db.qc_issues.find_one(
            tenant_filter(ctx["tenant_id"], {"id": qc_id}), {"_id": 0},
        )
        await stamp_edit_metadata(
            db=db, collection="qc_issues", record_id=qc_id,
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            edit_reason=edit_reason, before=before, after=after,
            request_id=ctx["request_id"],
        )
    return QcIssue(**after)


# --- Photos ---
def _compute_evidentiary(photo_class: str, pin_x, pin_y, drawing_id) -> bool:
    """Bucket C3b Phase 4 — evidentiary if EITHER:
       (a) class is in the evidentiary set (discovery / damage / quality), OR
       (b) the photo carries a drawing pin.

    Followup-fix 8 (2026-05-07): originally written with AND semantics
    (required class AND pin together) which downgraded discovery/damage/
    quality photos to evidentiary=false until they were pinned, contra
    the C3b spec. Switched to OR per the spec."""
    from models import EVIDENTIARY_CLASSES
    if photo_class in EVIDENTIARY_CLASSES:
        return True
    has_pin = (pin_x is not None and pin_y is not None) or bool(drawing_id)
    return has_pin


async def _hash_storage_object(storage_path: str) -> Optional[str]:
    """Pull bytes from storage and SHA-256 them. Returns None on any
    storage hiccup so the photo create still succeeds — sha256 backfill
    can recompute later via 003_photo_evidentiary_backfill.py."""
    if not storage_path:
        return None
    try:
        from services.storage import get_object
        import hashlib
        data, _ct = get_object(storage_path)
        return hashlib.sha256(data).hexdigest()
    except Exception:
        return None


@router.post("/photos", response_model=Photo)
async def create_photo(payload: PhotoCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    from datetime import timezone as _tz
    doc = payload.model_dump(mode="json")
    now = utc_now()
    sha256 = await _hash_storage_object(payload.storage_path)

    # Bucket C3b Phase 4 — sha256 collision check. Real-world chance is
    # negligible but we surface it as a 409 with the existing photo's id
    # so the user can confirm it's an intentional re-upload (or skip).
    if sha256:
        existing = await db.photos.find_one(
            tenant_filter(ctx["tenant_id"], {
                "project_id": payload.project_id,
                "sha256_hash": sha256,
            }),
            {"_id": 0, "id": 1},
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_photo",
                    "message": "A photo with the same SHA-256 already exists in this project.",
                    "existing_photo_id": existing["id"],
                },
            )

    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "thumbnail_path": None,
        "captured_at": now.isoformat(),
        "captured_by": ctx["user"]["id"],
        "visual_fingerprint_match_id": None,
        "visual_fingerprint_match_confidence": None,
        "visual_fingerprint_overridden": False,
        "created_at": now.isoformat(),
        # Phase 4 evidentiary fields
        "sha256_hash": sha256,
        "device_timestamp": None,  # populated by extract-exif background call
        "server_received_timestamp": now.isoformat(),
        "timestamp_mismatch_flag": False,
        "exif_preserved": True,
        "evidentiary_flag": _compute_evidentiary(
            doc.get("photo_class") or "progress",
            doc.get("pin_x"), doc.get("pin_y"), doc.get("pin_drawing_id"),
        ),
    })
    await db.photos.insert_one(doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photos",
        op="PHOTO_CREATE", record_id=doc["id"],
        before=None, after=doc,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return Photo(**doc)


@router.patch("/photos/{photo_id}", response_model=Photo)
async def update_photo(photo_id: str, payload: PhotoUpdate, ctx: dict = Depends(require_tenant)):
    """Caption + class + tags editable. File replacement intentionally
    NOT supported (tamper-evidence). Class change emits its own audit op
    so reviewers can see when an evidentiary classification flipped."""
    db = get_db()
    before = await db.photos.find_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Photo not found")
    raw = payload.model_dump(mode="json", exclude_unset=True)
    edit_reason = raw.pop("edit_reason", None)
    updates = {k: v for k, v in raw.items() if v is not None}
    if not updates and not edit_reason:
        return Photo(**before)
    # Addition 4 -- locked-DR edit policy.
    from services.edit_permissions import (
        enforce_locked_dr_edit_policy, stamp_edit_metadata,
    )
    _parent_dr, is_locked_edit = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before.get("daily_report_id"),
        edit_reason=edit_reason, event_type="photo", event_id=photo_id,
    )
    # Recompute evidentiary_flag if class changed.
    if "photo_class" in updates:
        updates["evidentiary_flag"] = _compute_evidentiary(
            updates["photo_class"],
            before.get("pin_x"), before.get("pin_y"), before.get("pin_drawing_id"),
        )
    await db.photos.update_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"$set": updates},
    )
    after = await db.photos.find_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0},
    )
    op = "PHOTO_CLASS_CHANGE" if "photo_class" in updates and updates["photo_class"] != before.get("photo_class") else "PHOTO_UPDATE"
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photos", op=op, record_id=photo_id,
        before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Addition 4 -- post-submit edit audit + metadata stamp.
    if is_locked_edit:
        after = await db.photos.find_one(
            tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0},
        )
        await stamp_edit_metadata(
            db=db, collection="photos", record_id=photo_id,
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            edit_reason=edit_reason, before=before, after=after,
            request_id=ctx["request_id"],
        )
    return Photo(**after)


@router.post("/photos/{photo_id}/pin", response_model=Photo)
async def pin_photo(photo_id: str, payload: PhotoPinPayload, ctx: dict = Depends(require_tenant)):
    """Set or clear the drawing pin on a photo. Refreshes evidentiary_flag
    based on current photo_class + new pin state."""
    db = get_db()
    before = await db.photos.find_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Photo not found")
    raw = payload.model_dump(mode="json", exclude_unset=True)
    updates = {k: v for k, v in raw.items() if v is not None}
    # Recompute evidentiary_flag using NEW pin values where provided.
    new_x = updates.get("pin_x", before.get("pin_x"))
    new_y = updates.get("pin_y", before.get("pin_y"))
    new_drawing = updates.get("pin_drawing_id", before.get("pin_drawing_id"))
    updates["evidentiary_flag"] = _compute_evidentiary(
        before.get("photo_class") or "progress", new_x, new_y, new_drawing,
    )
    await db.photos.update_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"$set": updates},
    )
    after = await db.photos.find_one(
        tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="photos",
        op="PHOTO_PIN_SET", record_id=photo_id,
        before={k: before.get(k) for k in ("pin_x", "pin_y", "pin_drawing_id", "drawing_pin_id", "evidentiary_flag")},
        after={k: updates.get(k, before.get(k)) for k in ("pin_x", "pin_y", "pin_drawing_id", "drawing_pin_id", "evidentiary_flag")},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return Photo(**after)


@router.get("/projects/{project_id}/photos", response_model=list[Photo])
async def list_photos(project_id: str, location_id: Optional[str] = None,
                      schedule_activity_id: Optional[str] = None,
                      photo_class: Optional[str] = None,
                      evidentiary_only: bool = False,
                      affected_activity_id: Optional[str] = None,
                      tagged_to: Optional[str] = None,
                      ctx: dict = Depends(require_tenant)):
    """Bucket D Phase 3 -- gallery filters added:
       affected_activity_id  filter to rows where the daily-report
                             activity (or any indirect-link via tagged
                             event) matches.
       tagged_to             one of activity|delay|rfi|qc|safety|
                             coordination|delivery|drawing -- filters
                             to photos whose XOR event-ref is set in
                             that bucket."""
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"project_id": project_id})
    if location_id:
        f["location_id"] = location_id
    if schedule_activity_id:
        f["schedule_activity_id"] = schedule_activity_id
    if photo_class:
        f["photo_class"] = photo_class
    if evidentiary_only:
        f["evidentiary_flag"] = True
    indirect_event_ids: dict[str, list[str]] = {}
    if affected_activity_id:
        # Direct tag OR indirect via a tagged event whose
        # linked_task_ids contains the master-schedule task id. Pre-
        # collect event ids for each indirect collection so the photo
        # query can $or-match in one shot. (Bucket D Corrective: scope
        # is now master schedule, not per-DR activity rows.)
        for coll, ref_field in [("delays", "delay_id"),
                                  ("rfis", "rfi_id"),
                                  ("qc_issues", "qc_issue_id"),
                                  ("safety_events", "safety_event_id"),
                                  ("coordination_events", "coordination_event_id"),
                                  ("deliveries", "delivery_id")]:
            ids = await db[coll].find(
                tenant_filter(ctx["tenant_id"], {
                    "project_id": project_id,
                    "linked_task_ids": affected_activity_id,
                }),
                {"_id": 0, "id": 1},
            ).to_list(length=None)
            if ids:
                indirect_event_ids[ref_field] = [r["id"] for r in ids]
        # Build $or branches: direct + each indirect FK match.
        or_branches: list[dict] = [{"daily_report_activity_id": affected_activity_id}]
        for ref_field, id_list in indirect_event_ids.items():
            or_branches.append({ref_field: {"$in": id_list}})
        # If only one branch, $or with a single elem is wasteful; use
        # the branch directly.
        if len(or_branches) == 1:
            f.update(or_branches[0])
        else:
            f["$or"] = or_branches
    if tagged_to:
        ref_map = {
            "activity": "daily_report_activity_id", "delay": "delay_id",
            "rfi": "rfi_id", "qc": "qc_issue_id", "safety": "safety_event_id",
            "coordination": "coordination_event_id",
            "delivery": "delivery_id", "drawing": "drawing_pin_id",
        }
        ref_field = ref_map.get(tagged_to)
        if ref_field:
            f[ref_field] = {"$type": "string"}
    docs = await db.photos.find(f, {"_id": 0}).sort("captured_at", -1).to_list(500)
    # Bucket D Task 2 -- enrich supplement rows with parent DR date.
    parent_dr_ids = list({d["parent_daily_report_id"] for d in docs
                          if d.get("is_supplement") and d.get("parent_daily_report_id")})
    if parent_dr_ids:
        parent_drs = await db.daily_reports.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": parent_dr_ids}}),
            {"_id": 0, "id": 1, "report_date": 1},
        ).to_list(length=None)
        date_map = {d["id"]: d["report_date"] for d in parent_drs}
        for d in docs:
            pid = d.get("parent_daily_report_id")
            if pid and pid in date_map:
                d["parent_daily_report_date"] = date_map[pid]
    return [Photo(**d) for d in docs]
