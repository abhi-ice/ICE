"""Daily-report router. Master prompt §10: pre-populate from 2WLA at 7am."""
import re
import requests
from fastapi import APIRouter, Depends, HTTPException
from datetime import date, datetime, timedelta
from typing import Optional
from pydantic import Field
from database import get_db, tenant_filter
from services.site_time import project_today_iso_for
from models import (
    DailyReport, DailyReportCreate, DailyReportUpdate,
    DailyReportActivity, DailyReportActivityUpdate,
    DailyPresence, DailyPresenceCreate, DailyPresenceUpdate,
    UnplannedActivityCreate,
    EDIT_REASON_MIN_LEN,
    gen_id, utc_now, StrictModel,
)
from auth import require_tenant
from audit import write_audit

router = APIRouter(tags=["daily-reports"])


# Bucket D Phase 4 -- supplements appendix. Returns every event with
# parent_daily_report_id == this DR, grouped by collection. The DR
# detail view renders this below the main panels so the parent shows
# its supplement trail.
@router.get("/daily-reports/{daily_report_id}/supplements")
async def list_supplements_for_dr(
    daily_report_id: str, ctx: dict = Depends(require_tenant),
):
    db = get_db()
    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}),
        {"_id": 0, "id": 1, "report_date": 1, "status": 1},
    )
    if not dr:
        raise HTTPException(404, "Daily report not found")
    out: dict = {"daily_report_id": daily_report_id, "report_date": dr.get("report_date")}
    coll_map = {
        "delays": "delays", "rfis": "rfis", "qc_issues": "qc_issues",
        "safety_events": "safety_events", "deliveries": "deliveries",
        "coordination_events": "coordination_events", "photos": "photos",
        "daily_report_activities": "daily_report_activities",
    }
    for key, coll in coll_map.items():
        rows = await db[coll].find(
            {"parent_daily_report_id": daily_report_id},
            {"_id": 0},
        ).sort("supplement_filed_at", -1).to_list(length=200)
        out[key] = rows
    return out


# Bucket D Phase 3 -- gallery views show "affected activity" chips.
# Originally event collections stored linked_dr_activity_ids (per-DR
# WBS row ids); the Bucket D Corrective (2026-05-08) renamed this to
# linked_task_ids (master schedule scope). This batch endpoint still
# works against daily_report_activities for chip rendering of the per-
# DR grid view; gallery chips on event rows now resolve via the master
# `tasks` collection in the frontend.
@router.get("/projects/{project_id}/dr-activity-chips")
async def dr_activity_chips(
    project_id: str, ids: str = "", ctx: dict = Depends(require_tenant),
):
    """ids is a comma-separated list of daily_report_activities.id values.
    Returns [{id, daily_report_id, dr_date, activity_code,
    activity_description, schedule_activity_id}]. Capped at 500 ids per
    call -- the frontend pages cap at <100 visible rows so this is
    well within range."""
    db = get_db()
    parts = [s.strip() for s in (ids or "").split(",") if s.strip()][:500]
    if not parts:
        return []
    rows = await db.daily_report_activities.find(
        {"id": {"$in": parts}},
        {"_id": 0, "id": 1, "daily_report_id": 1, "schedule_activity_id": 1,
         "activity_code": 1, "activity_description": 1},
    ).to_list(length=None)
    # Tenant-guard via parent DR + denormalize report_date.
    dr_ids = list({r.get("daily_report_id") for r in rows if r.get("daily_report_id")})
    drs: dict[str, dict] = {}
    if dr_ids:
        dr_docs = await db.daily_reports.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": dr_ids}}),
            {"_id": 0, "id": 1, "report_date": 1},
        ).to_list(length=None)
        drs = {d["id"]: d for d in dr_docs}
    out: list[dict] = []
    for r in rows:
        dr = drs.get(r.get("daily_report_id"))
        if not dr:
            # Cross-tenant or orphan -- skip.
            continue
        out.append({
            "id": r["id"],
            "daily_report_id": r["daily_report_id"],
            "dr_date": dr.get("report_date"),
            "activity_code": r.get("activity_code"),
            "activity_description": r.get("activity_description"),
            "schedule_activity_id": r.get("schedule_activity_id"),
        })
    return out


def _scheduled_window_expr(report_date_iso: str) -> dict:
    """Single-date contains-check via the shared slip-aware helper (pt.72):
    a contains-check is just overlap(day, day)."""
    from services.schedule_filters import effective_schedule_overlap
    return effective_schedule_overlap(report_date_iso, report_date_iso)


async def _prepopulate_grid(tenant_id: str, project_id: str, daily_report_id: str, report_date: date):
    """Find activities scheduled for the day → create grid rows. A task is
    'scheduled for this day' iff its EFFECTIVE window (projected dates when
    slipped, else planned) contains report_date — see _scheduled_window_expr.
    Pre-fill pct_complete_start_of_day from yesterday's end_of_day or
    task.pct_complete.

    Paperwork tasks (preconstruction_paperwork, construction_paperwork,
    closeout_paperwork) ARE pre-populated alongside field-work rows --
    the DR UI renders them in dedicated read-only reference sections
    ("Before · Reference") so the super can see what paperwork is
    happening today without acting on it. The exported PDF filters
    them back out (iter6_router._grid_section_for == 2).
    """
    db = get_db()
    rd_iso = report_date.isoformat()
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$nin": ["deleted", "complete"]},
            "$expr": _scheduled_window_expr(rd_iso),
        }),
        {"_id": 0},
    ).limit(200).to_list(200)

    yesterday = (report_date - timedelta(days=1)).isoformat()
    # Existing rows we already have so we don't double-insert on refresh
    existing = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id},
        {"_id": 0, "schedule_activity_id": 1, "position": 1},
    ).to_list(500)
    existing_ids = {r["schedule_activity_id"] for r in existing}
    next_pos = max((r["position"] for r in existing), default=-1) + 1

    inserted = 0
    for t in tasks:
        if t["id"] in existing_ids:
            continue
        prev_dr = await db.daily_reports.find_one(
            tenant_filter(tenant_id, {"project_id": project_id, "report_date": yesterday}),
            {"_id": 0, "id": 1},
        )
        prev_pct = float(t.get("pct_complete") or 0)
        if prev_dr:
            prev_row = await db.daily_report_activities.find_one(
                {"daily_report_id": prev_dr["id"], "schedule_activity_id": t["id"]},
                {"_id": 0, "pct_complete_end_of_day": 1},
            )
            if prev_row and prev_row.get("pct_complete_end_of_day") is not None:
                prev_pct = float(prev_row["pct_complete_end_of_day"])

        loc_name = None
        if t.get("location_id"):
            loc = await db.locations.find_one(tenant_filter(tenant_id, {"id": t["location_id"]}), {"_id": 0, "name": 1})
            if loc:
                loc_name = loc["name"]

        crew_id = t.get("crew_id")
        target_size = 0
        if crew_id:
            crew = await db.crews.find_one(tenant_filter(tenant_id, {"id": crew_id}), {"_id": 0, "target_size": 1})
            if crew:
                target_size = int(crew.get("target_size") or 0)

        row = {
            "id": gen_id(),
            "daily_report_id": daily_report_id,
            "schedule_activity_id": t["id"],
            "position": next_pos,
            "pct_complete_start_of_day": prev_pct,
            "pct_complete_end_of_day": prev_pct,
            "crew_count": target_size,
            "crew_id": crew_id,
            "hours_worked": 0,
            "delay_minutes": 0,
            "delay_reason": None,
            "weather_impact_minutes": 0,
            "notes": None,
            "status_at_end_of_day": t.get("status", "not_started"),
            "activity_code": t["activity_code"],
            "activity_description": t["description"],
            "location_name": loc_name,
            "trade_code": t.get("trade_code"),
        }
        await db.daily_report_activities.insert_one(row.copy())
        next_pos += 1
        inserted += 1
    return inserted


@router.post("/daily-reports", response_model=DailyReport)
async def create_or_get_daily_report(payload: DailyReportCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    existing = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {
            "project_id": payload.project_id,
            "report_date": payload.report_date.isoformat(),
        }),
        {"_id": 0},
    )
    if existing:
        return DailyReport(**existing)
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "status": "draft",
        "prepared_by": ctx["user"]["id"],
        "submitted_by": None,
        "submitted_at": None,
        "approved_by": None,
        "approved_at": None,
        "created_at": utc_now().isoformat(),
    })
    await db.daily_reports.insert_one(doc.copy())
    # pre-populate grid
    await _prepopulate_grid(ctx["tenant_id"], payload.project_id, doc["id"], payload.report_date)
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_reports", op="INSERT",
        record_id=doc["id"], before=None, after=doc,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return DailyReport(**doc)


@router.post("/daily-reports/{daily_report_id}/refresh-grid")
async def refresh_grid(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Re-run pre-populate against the current schedule. Picks up any
    activity whose [planned_start, planned_finish] now contains the report
    date but isn't yet in the grid. Idempotent — never duplicates rows."""
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    if dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")
    inserted = await _prepopulate_grid(
        tenant_id=ctx["tenant_id"], project_id=dr["project_id"],
        daily_report_id=daily_report_id, report_date=date.fromisoformat(dr["report_date"]),
    )
    return {"inserted": inserted}


@router.get("/projects/{project_id}/daily-reports", response_model=list[DailyReport])
async def list_daily_reports(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.daily_reports.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("report_date", -1).to_list(200)
    return [DailyReport(**d) for d in docs]


@router.delete("/daily-reports/{daily_report_id}")
async def delete_daily_report(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Item 8 (2026-05-06) — hard-delete a draft daily report and cascade.

    Forbidden once status != 'draft' (submitted/approved reports are part
    of the federal-contract record and must stay).

    Cascade:
      - hard delete daily_report_activities + daily_presence for this DR
      - null out daily_report_id on linked field-event records (delays,
        qc_issues, safety_events, rfis, photos, voice_annotations) so
        those independent records survive but stop pointing at a ghost.
    """
    db = get_db()
    before = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Daily report not found")
    if before.get("status") != "draft":
        raise HTTPException(
            403, f"Cannot delete a {before.get('status')} daily report — only drafts can be deleted",
        )

    # Bucket C3b Phase 5 — record what we're about to cascade-delete so
    # the audit chain captures the presence rows' final state. Without
    # this, the rows are gone with no trail of who they belonged to.
    presence_to_delete = await db.daily_presence.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}),
        {"_id": 0},
    ).to_list(2000)

    await db.daily_report_activities.delete_many({"daily_report_id": daily_report_id})
    await db.daily_presence.delete_many(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}),
    )
    for p in presence_to_delete:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="daily_presence",
            op="CASCADE_DELETE_FROM_DR",
            record_id=p["id"], before=p, after=None,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )

    # Null out daily_report_id on independent linked records — they live
    # in their own collections and shouldn't disappear with the DR.
    cascade_collections = ["delays", "qc_issues", "safety_events", "rfis",
                            "photos", "voice_annotations"]
    for coll in cascade_collections:
        await db[coll].update_many(
            tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}),
            {"$set": {"daily_report_id": None}},
        )

    # Bucket C3b Phase 5 — coordination_events live alongside the DR via
    # linked_daily_report_id (set by the quick-log on the DR header). The
    # event itself outlives the DR (it's a phone call / RFI exchange) so
    # null the link rather than delete the row.
    await db.coordination_events.update_many(
        tenant_filter(ctx["tenant_id"], {"linked_daily_report_id": daily_report_id}),
        {"$set": {"linked_daily_report_id": None}},
    )

    # Bucket C1 Phase 6 — clear the sync-layer marker on any task whose
    # last_synced_from_dr points at this DR. The task data persists
    # (pct_complete, status, current_crew_id all stay) — only the
    # "this came from DR X" pointer is cleared. Status recompute below
    # picks up the new state.
    affected_task_docs = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"last_synced_from_dr.daily_report_id": daily_report_id}),
        {"_id": 0, "id": 1},
    ).to_list(2000)
    affected_task_ids = [t["id"] for t in affected_task_docs]
    if affected_task_ids:
        await db.tasks.update_many(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": affected_task_ids}}),
            {"$set": {"last_synced_from_dr": None,
                      "updated_at": utc_now().isoformat()}},
        )

    await db.daily_reports.delete_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}),
    )

    # Recompute status for the affected tasks (now they're un-blocked
    # from any delays whose daily_report_id we just nulled out).
    if affected_task_ids:
        from services.task_status import compute_and_persist_status
        for tid in affected_task_ids:
            await compute_and_persist_status(
                db, ctx["tenant_id"], tid,
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_reports", op="DELETE",
        record_id=daily_report_id, before=before, after=None,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True,
            "cascade_unlinked": cascade_collections + ["coordination_events"],
            "cascade_deleted": {"daily_presence": len(presence_to_delete)}}


@router.get("/daily-reports/{daily_report_id}", response_model=DailyReport)
async def get_daily_report(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Daily report not found")
    return DailyReport(**doc)


async def _enrich_grid_rows(db, tenant_id: str, rows: list) -> list:
    """Features F2/F3/F4 (2026-05-18) -- stamp the DR Activity Grid view
    fields onto each grid row from its linked task. One $in query, not
    per-row lookups.

      work_category      -- F1 tag; the grid sections rows off it.
      open_blocker_count -- F3 blocker badge (the persisted count the
                            Tasks page already shows -- task_status.py).
      auto_status        -- F4 automatic status.
      task_updated_at    -- optimistic-lock token (pre-existing).

    METADATA / VIEW ONLY -- none of these are persisted back to the
    daily_report_activities collection or read by the CPM engine.
    Mutates `rows` in place and returns it."""
    from services.dr_activity_grid import enrich_grid_row
    from services.feature_flags import slider_gating_enabled
    task_ids = [r["schedule_activity_id"] for r in rows if r.get("schedule_activity_id")]
    by_id: dict = {}
    gate_by_task_id: dict = {}
    if task_ids:
        # CPM B1 (2026-06-12) -- when FF_SLIDER_GATING is on, also pull the
        # fields the slider gate needs (predecessors + pct + status); the
        # original lean projection otherwise. Additive: the existing view
        # fields are unchanged either way.
        gating = slider_gating_enabled()
        projection = {"_id": 0, "id": 1, "updated_at": 1, "work_category": 1,
                      "open_blocker_count": 1, "open_blocker_summary": 1}
        if gating:
            projection.update({"predecessors": 1, "pct_complete": 1,
                               "status": 1, "activity_code": 1, "project_id": 1})
        tasks = await db.tasks.find(
            tenant_filter(tenant_id, {"id": {"$in": task_ids}}),
            projection,
        ).to_list(len(task_ids))
        by_id = {t["id"]: t for t in tasks}
        if gating and tasks:
            gate_by_task_id = await _compute_grid_slider_gates(db, tenant_id, tasks)
    for r in rows:
        enrich_grid_row(r, by_id.get(r.get("schedule_activity_id")))
        # CPM B1 -- stamp the read-time slider gate (None when the flag is
        # off, so the frontend slider keeps its pre-B1 free [0,100] behaviour).
        if gate_by_task_id:
            g = gate_by_task_id.get(r.get("schedule_activity_id"))
            if g is not None:
                r["slider_gate"] = g
    return rows


async def _compute_grid_slider_gates(db, tenant_id: str, tasks: list) -> dict:
    """CPM B1 (2026-06-12) -- {task_id -> slider gate} for the grid's linked
    tasks. One extra $in query for the predecessor states, then the pure
    compute_slider_gate. Called only when FF_SLIDER_GATING is on."""
    from services.slider_gating import compute_slider_gate
    from models import pred_code as _pc
    pred_codes: set = set()
    project_id = None
    for t in tasks:
        project_id = project_id or t.get("project_id")
        for p in (t.get("predecessors") or []):
            c = _pc(p)
            if c:
                pred_codes.add(c)
    pred_states: dict = {}
    if pred_codes and project_id:
        pred_rows = await db.tasks.find(
            tenant_filter(tenant_id, {
                "project_id": project_id,
                "activity_code": {"$in": list(pred_codes)},
                "status": {"$ne": "deleted"},
            }),
            {"_id": 0, "activity_code": 1, "pct_complete": 1, "status": 1},
        ).to_list(len(pred_codes))
        pred_states = {
            r["activity_code"]: r for r in pred_rows if r.get("activity_code")
        }
    return {t["id"]: compute_slider_gate(t, pred_states) for t in tasks if t.get("id")}


@router.get("/daily-reports/{daily_report_id}/grid", response_model=list[DailyReportActivity])
async def get_grid(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    # ensure tenant ownership
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0, "id": 1})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    rows = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id}, {"_id": 0},
    ).sort("position", 1).to_list(200)
    await _enrich_grid_rows(db, ctx["tenant_id"], rows)
    return [DailyReportActivity(**r) for r in rows]


@router.get("/daily-reports/{daily_report_id}/available-tasks")
async def list_available_tasks(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Return tasks PLANNED for the report's date that are NOT already in
    the grid. This is the user-requested 'date-filtered task dropdown'."""
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    report_date = dr["report_date"]
    project_id = dr["project_id"]
    # tasks scheduled on this date — effective (slip-aware) window, same rule
    # as _prepopulate_grid so the dropdown and the grid agree.
    candidates = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "status": {"$nin": ["deleted", "complete"]},
            "$expr": _scheduled_window_expr(report_date),
        }),
        {"_id": 0},
    ).limit(200).to_list(200)
    # exclude ones already in grid
    existing = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id}, {"_id": 0, "schedule_activity_id": 1},
    ).to_list(500)
    existing_ids = {r["schedule_activity_id"] for r in existing}
    out = [
        {
            "id": t["id"],
            "activity_code": t["activity_code"],
            "description": t["description"],
            "trade_code": t.get("trade_code"),
            "location_id": t.get("location_id"),
            "planned_start": t.get("planned_start"),
            "planned_finish": t.get("planned_finish"),
            "pct_complete": t.get("pct_complete"),
        }
        for t in candidates if t["id"] not in existing_ids
    ]
    return out


class AddActivityRow(StrictModel):
    schedule_activity_id: str
    # Bucket D Addition 5 (2026-05-12) -- "add to locked DR" flow.
    # Required (min EDIT_REASON_MIN_LEN chars) if the parent DR is
    # locked AND the caller isn't admin/PM.
    add_reason: Optional[str] = Field(default=None, max_length=500)


@router.post("/daily-reports/{daily_report_id}/grid", response_model=DailyReportActivity)
async def add_grid_row(daily_report_id: str, payload: AddActivityRow, ctx: dict = Depends(require_tenant)):
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    # Addition 5 (2026-05-12) -- "add to locked DR" flow. Tiered
    # permission + add_reason if the DR is submitted/approved.
    from services.edit_permissions import enforce_locked_dr_add_policy
    _dr, is_locked_add = await enforce_locked_dr_add_policy(
        ctx=ctx, daily_report_id=daily_report_id,
        add_reason=payload.add_reason, event_type="activity",
    )
    if await db.daily_report_activities.find_one({"daily_report_id": daily_report_id, "schedule_activity_id": payload.schedule_activity_id}, {"_id": 0}):
        raise HTTPException(409, "Activity already in grid")
    t = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.schedule_activity_id}), {"_id": 0})
    if not t:
        raise HTTPException(404, "Task not found")
    pos_max = await db.daily_report_activities.find({"daily_report_id": daily_report_id}, {"position": 1}).sort("position", -1).limit(1).to_list(1)
    next_pos = (pos_max[0]["position"] + 1) if pos_max else 0
    loc_name = None
    if t.get("location_id"):
        loc = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": t["location_id"]}), {"_id": 0, "name": 1})
        if loc:
            loc_name = loc["name"]
    target_size = 0
    if t.get("crew_id"):
        crew = await db.crews.find_one(tenant_filter(ctx["tenant_id"], {"id": t["crew_id"]}), {"_id": 0, "target_size": 1})
        if crew:
            target_size = int(crew.get("target_size") or 0)
    row = {
        "id": gen_id(),
        "daily_report_id": daily_report_id,
        "schedule_activity_id": t["id"],
        "position": next_pos,
        "pct_complete_start_of_day": float(t.get("pct_complete") or 0),
        "pct_complete_end_of_day": float(t.get("pct_complete") or 0),
        "crew_count": target_size,
        "crew_id": t.get("crew_id"),
        "hours_worked": 0,
        "delay_minutes": 0,
        "delay_reason": None,
        "weather_impact_minutes": 0,
        "notes": None,
        "status_at_end_of_day": t.get("status", "not_started"),
        "activity_code": t["activity_code"],
        "activity_description": t["description"],
        "location_name": loc_name,
        "trade_code": t.get("trade_code"),
    }
    await db.daily_report_activities.insert_one(row.copy())
    if is_locked_add:
        from services.edit_permissions import stamp_add_metadata
        await stamp_add_metadata(
            db=db, collection="daily_report_activities", record_id=row["id"],
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            add_reason=payload.add_reason or "", after=row,
            request_id=ctx["request_id"],
        )
    return DailyReportActivity(**row)


@router.delete("/daily-report-activities/{row_id}")
async def remove_grid_row(row_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    row = await db.daily_report_activities.find_one({"id": row_id}, {"_id": 0})
    if not row:
        raise HTTPException(404, "Row not found")
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": row["daily_report_id"]}), {"_id": 0, "id": 1})
    if not dr:
        raise HTTPException(404, "Row not found")
    await db.daily_report_activities.delete_one({"id": row_id})
    return {"ok": True}


@router.patch("/daily-report-activities/{row_id}", response_model=DailyReportActivity)
async def update_grid_row(row_id: str, payload: DailyReportActivityUpdate, ctx: dict = Depends(require_tenant)):
    """Bucket C1 Phase 1 — write-back foundation.

    Beyond updating the grid row, this PATCH now also syncs the linked
    task: pct_complete, status, hours, current crew. The write is marked
    sync_state='provisional' until the parent daily report is submitted
    (then submit_daily_report flips every row's task entry to 'confirmed').

    Stale-write detection: the frontend may send `task_last_modified_at`
    captured at grid load. If the task has been modified externally
    since then, return 409 with the latest task data so the frontend
    can show "Schedule updated — refresh row" rather than silently
    overwriting more authoritative edits.
    """
    db = get_db()
    raw = payload.model_dump(mode="json")
    # Optimistic-lock token is a control field — pop before writing the row.
    client_task_lmt = raw.pop("task_last_modified_at", None)
    # Addition 5 (2026-05-12) -- edit_reason is metadata, not a row field.
    edit_reason = raw.pop("edit_reason", None)
    # CPM B1 (2026-06-12) -- slider-gate override reason is a control field,
    # not a row field (popped like edit_reason). Only consumed when
    # FF_SLIDER_GATING blocks an EOD% increase past the predecessor gate.
    slider_override_reason = raw.pop("slider_gate_override_reason", None)
    updates = {k: v for k, v in raw.items() if v is not None}

    before = await db.daily_report_activities.find_one({"id": row_id}, {"_id": 0})
    if not before:
        raise HTTPException(404, "Row not found")
    # tenant guard via parent daily report
    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": before["daily_report_id"]}),
        {"_id": 0, "id": 1, "status": 1, "report_date": 1, "project_id": 1,
         "submitted_by": 1, "submitted_at": 1},
    )
    if not dr:
        raise HTTPException(404, "Row not found")
    # Bucket D Addition 5 (2026-05-12) -- locked-DR edit policy. The
    # prior hard 403 on submitted/approved is replaced with the same
    # tiered permission flow used for event edits: 24h self-edit on
    # own DR, then edit_request workflow. Activity-grid edits fire
    # ACTIVITY_GRID_EDITED_POST_SUBMIT (distinct from EVENT_EDITED_
    # POST_SUBMIT) so counsel can filter grid edits separately.
    from services.edit_permissions import (
        enforce_locked_dr_edit_policy, stamp_edit_metadata,
    )
    # Bug #49 (2026-05-13): activity grid is intentionally NOT in
    # STATUS_TRANSITION_CONFIGS -- per-DR activity rows don't have
    # a resolve/close flow. effective_edit_reason == edit_reason here.
    _parent_dr, is_locked_edit, effective_edit_reason = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before["daily_report_id"],
        edit_reason=edit_reason, event_type="activity", event_id=row_id,
    )

    # Bucket C3a Phase 2 — constraint validation. Cross-field rules
    # StrictModel can't express alone. We let the user effectively
    # CHANGE the constraint type by reading from `updates` first and
    # falling back to the row's existing value when the patch only
    # supplies one of the related fields.
    if "constraint_type" in updates:
        ct = updates["constraint_type"]
        if ct in (None, "not_constrained"):
            # Clearing the constraint also clears its dependents so the
            # row doesn't carry stale ref/desc data.
            updates["constraint_reference_entity_type"] = None
            updates["constraint_reference_entity_id"] = None
            updates["constraint_other_description"] = None
        elif ct == "other":
            desc = updates.get("constraint_other_description") or before.get("constraint_other_description")
            if not (desc and str(desc).strip()):
                raise HTTPException(422, "constraint_other_description required when constraint_type is 'other'.")
            # Other has no ref entity.
            updates["constraint_reference_entity_type"] = None
            updates["constraint_reference_entity_id"] = None
        else:
            ref_id = updates.get("constraint_reference_entity_id") or before.get("constraint_reference_entity_id")
            if not ref_id:
                raise HTTPException(422, f"constraint_reference_entity_id required when constraint_type is '{ct}'.")
            ge = await db.gating_entities.find_one(
                tenant_filter(ctx["tenant_id"], {"id": ref_id}),
                {"_id": 0, "id": 1, "entity_type": 1},
            )
            if not ge:
                raise HTTPException(422, f"constraint_reference_entity_id {ref_id} not found in gating_entities.")
            # Snapshot entity_type so reads don't need a second lookup.
            updates["constraint_reference_entity_type"] = ge.get("entity_type")
            updates["constraint_other_description"] = None
        # User-set constraint always clears the auto_filled flag.
        updates["constraint_auto_filled"] = False

    # Look up the linked task NOW so stale-write detection runs before
    # we touch the grid row.
    task_before = await db.tasks.find_one(
        tenant_filter(ctx["tenant_id"], {"id": before["schedule_activity_id"]}),
        {"_id": 0},
    )
    if client_task_lmt and task_before and task_before.get("updated_at"):
        # Both ISO strings — string compare works because ISO is sortable.
        # Also tolerates client sending a datetime object that Pydantic
        # serialised to ISO via model_dump(mode="json").
        try:
            server_lmt = str(task_before["updated_at"])
            if server_lmt > str(client_task_lmt):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_write",
                        "message": "Schedule was updated externally — refresh the row.",
                        "task_id": task_before["id"],
                        "current_task": _task_for_409(task_before),
                    },
                )
        except HTTPException:
            raise
        except Exception:
            # If timestamp comparison fails for any weird reason, fall
            # through to normal write rather than blocking the user.
            pass

    # CPM B1 (2026-06-12) -- EOD-slider gate enforcement. FF_SLIDER_GATING.
    # A successor cannot earn progress its predecessors don't yet allow
    # (Sid's pinned table -- FS lock < 100%, SS lock until started, FF cap
    # ~95%, SF cap ~99%, most-restrictive predecessor wins). Only an
    # INCREASE past the gate is blocked; lowering progress or staying within
    # the cap is always fine. A PM/Admin may override with a logged reason.
    # Runs BEFORE any row/task write so a blocked edit is fully rejected.
    # No-op when the flag is off (pre-B1 behaviour, byte-identical).
    from services.feature_flags import slider_gating_enabled
    if (slider_gating_enabled()
            and "pct_complete_end_of_day" in updates
            and task_before is not None):
        new_eod = float(updates["pct_complete_end_of_day"])
        old_eod = float(before.get("pct_complete_end_of_day") or 0)
        if new_eod > old_eod:  # only constrain increases
            from services.slider_gating import evaluate_eod_gate
            from models import pred_code as _pred_code
            pred_codes = [c for c in (
                _pred_code(p) for p in (task_before.get("predecessors") or [])
            ) if c]
            pred_states: dict = {}
            if pred_codes:
                pred_rows = await db.tasks.find(
                    tenant_filter(ctx["tenant_id"], {
                        "project_id": task_before.get("project_id"),
                        "activity_code": {"$in": pred_codes},
                        "status": {"$ne": "deleted"},
                    }),
                    {"_id": 0, "activity_code": 1, "pct_complete": 1, "status": 1},
                ).to_list(len(pred_codes))
                pred_states = {
                    r["activity_code"]: r for r in pred_rows if r.get("activity_code")
                }
            decision = evaluate_eod_gate(
                task_before, pred_states,
                old_eod=old_eod, new_eod=new_eod,
                role=ctx["user"].get("role"),
                override_reason=slider_override_reason,
            )
            if decision["action"] == "override":
                # Authorised PM/Admin override -- allow the edit + audit it.
                gate = decision["gate"]
                await write_audit(
                    tenant_id=ctx["tenant_id"],
                    table_name="daily_report_activities",
                    op="SLIDER_GATE_OVERRIDE", record_id=row_id,
                    before={"max_pct": gate["max_pct"],
                            "relationship": gate["relationship"],
                            "binding_pred_code": gate["binding_pred_code"]},
                    after={"requested_eod": new_eod,
                           "reason": str(slider_override_reason).strip()},
                    actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
                )
            elif decision["action"] == "block":
                gate = decision["gate"]
                raise HTTPException(status_code=422, detail={
                    "error": "slider_gate_blocked",
                    "message": gate["reason"] or (
                        "This activity's progress is gated by its predecessors."
                    ),
                    "relationship": gate["relationship"],
                    "binding_pred_code": gate["binding_pred_code"],
                    "max_pct": gate["max_pct"],
                    "requested": new_eod,
                })

    # 1. Update the grid row itself (existing behaviour) and stamp the
    #    sync_state metadata so the UI can show the provisional indicator.
    now_iso = utc_now().isoformat()
    row_updates = dict(updates)
    # Feature F4 (2026-05-18) -- the row's end-of-day status is AUTO-
    # computed from the linked task's open-blocker count + the row's
    # EOD%; it is no longer a manually-set field (auto overrides
    # manual). Recomputed on every PATCH so a progress edit immediately
    # flips the stored status. The /grid + /context reads recompute
    # auto_status live on top of this for real-time display.
    from services.dr_activity_grid import compute_auto_status
    _eod_for_status = updates.get(
        "pct_complete_end_of_day", before.get("pct_complete_end_of_day", 0),
    )
    row_updates["status_at_end_of_day"] = compute_auto_status(
        (task_before or {}).get("open_blocker_count", 0), _eod_for_status,
    )
    row_updates["sync_state"] = "provisional"
    row_updates["last_sync_at"] = now_iso
    row_updates["task_external_modified_warning"] = False
    await db.daily_report_activities.update_one({"id": row_id}, {"$set": row_updates})
    after = await db.daily_report_activities.find_one({"id": row_id}, {"_id": 0})

    # 2. Audit the row update (Item 7 fix from earlier — kept).
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_report_activities", op="UPDATE",
        record_id=row_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    # 3. Sync write-back to the linked task. Builds a single $set so the
    #    task gets one stamp, one audit entry, one task_sync_events row.
    task_id = before["schedule_activity_id"]
    task_set: dict = {"updated_at": now_iso, "external_modified_after_sync": False}
    history_push: Optional[dict] = None

    if "pct_complete_end_of_day" in updates:
        new_pct = float(updates["pct_complete_end_of_day"])
        task_set["prev_pct_complete"] = before.get("pct_complete_end_of_day", 0)
        task_set["pct_complete"] = new_pct
        # Existing simple status mapping — Phase 2 will replace this with
        # the computed_status state machine.
        task_set["status"] = "complete" if new_pct >= 100 else "in_progress"
    if "status_at_end_of_day" in updates:
        # Map the row's per-day status to the task's lifecycle status.
        # 'not_started' / 'blocked' from the grid mean the task isn't
        # finished — keep it in_progress unless EOD% above bumped it.
        row_status = updates["status_at_end_of_day"]
        if row_status == "complete":
            task_set["status"] = "complete"
        elif row_status == "blocked":
            task_set["status"] = "in_progress"
    if "hours_worked" in updates:
        delta = float(updates["hours_worked"]) - float(before.get("hours_worked") or 0)
        if delta:
            task_set["actual_hours_to_date"] = float(task_before.get("actual_hours_to_date") or 0) + delta
    crew_picked_for_presence = None
    if "linked_crew_id" in updates:
        new_crew = updates["linked_crew_id"]
        if new_crew != (task_before or {}).get("current_crew_id"):
            task_set["current_crew_id"] = new_crew
            history_push = {
                "crew_id": new_crew,
                "daily_report_id": before["daily_report_id"],
                "assigned_date": dr.get("report_date"),
            }
            # Bucket C3b Phase 3 — auto-create a daily_presence row
            # tying this trade to the activity at first shift, no zone.
            # User can refine zone/shift on the detailed roster later.
            crew_picked_for_presence = new_crew
    # Always stamp the last-synced marker so the UI can show provisional.
    task_set["last_synced_from_dr"] = {
        "daily_report_id": before["daily_report_id"],
        "daily_report_date": dr.get("report_date"),
        "synced_at": now_iso,
        "sync_state": "provisional" if dr.get("status") == "draft" else "confirmed",
    }

    update_doc: dict = {"$set": task_set}
    if history_push is not None:
        update_doc["$push"] = {"crew_assignment_history": history_push}

    # Only fire if there's something meaningful to write (always the case
    # because last_synced_from_dr is always set, but be explicit).
    if task_before:
        await db.tasks.update_one(
            tenant_filter(ctx["tenant_id"], {"id": task_id}),
            update_doc,
        )
        task_after = await db.tasks.find_one(
            tenant_filter(ctx["tenant_id"], {"id": task_id}), {"_id": 0},
        )
        # 4. Audit the task update too — separate entry on the same hash chain.
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="tasks", op="SYNC",
            record_id=task_id, before=task_before, after=task_after,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
        # 5. task_sync_events — diagnostic-grade record per write-back.
        fields_updated = {
            k: {"before": task_before.get(k), "after": task_set.get(k)}
            for k in task_set if k != "updated_at"
        }
        await db.task_sync_events.insert_one({
            "id": gen_id(),
            "tenant_id": ctx["tenant_id"],
            "project_id": dr.get("project_id") or task_before.get("project_id"),
            "task_id": task_id,
            "daily_report_id": before["daily_report_id"],
            "daily_report_activity_id": row_id,
            "triggered_by_user_id": ctx["user"]["id"],
            "triggered_at": now_iso,
            "sync_state": task_set["last_synced_from_dr"]["sync_state"],
            "fields_updated": fields_updated,
            # Phase 2/4 will populate these — Phase 1 leaves them empty.
            "cpm_recomputed": False,
            "cpm_changes": [],
        })

        # Bucket C1 Phase 2 — recompute computed_status / blocker fields
        # for the linked task. Idempotent; only writes if any value moved.
        from services.task_status import compute_and_persist_status
        await compute_and_persist_status(
            db, ctx["tenant_id"], task_id,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )

        # CPM B2 (2026-06-12) -- progress-driven cascade trigger.
        # The EOD% write above just moved the linked task's pct_complete;
        # re-flow the schedule so a task now behind its planned percent slips
        # its finish and cascades downstream (compute_cpm -> projected dates
        # -> remaining float), the same engine a logged delay drives.
        # auto_recompute_after_event wraps each step in try/except, so a CPM
        # hiccup never blocks the grid edit.
        # Engine-aware (2026-07-02): the mode2 (MS Project) and enterprise
        # engines are progress-aware natively, so a project on either
        # recomputes regardless of FF_PROGRESS_CASCADE -- the B2 flag only
        # gates the impact-days linear cascade. Project read (cheap
        # projection) only happens when the EOD% actually changed; with all
        # three flags off nothing runs (pre-B2 behaviour).
        if "pct_complete_end_of_day" in updates:
            from services.feature_flags import progress_recompute_enabled_for
            _dr_project_id = dr.get("project_id") or task_before.get("project_id")
            _proj = await db.projects.find_one(
                tenant_filter(ctx["tenant_id"], {"id": _dr_project_id}),
                {"_id": 0, "schedule_mode": 1, "schedule_engine": 1}) or {}
            if progress_recompute_enabled_for(_proj):
                from services.cpm import auto_recompute_after_event
                await auto_recompute_after_event(
                    ctx["tenant_id"], _dr_project_id,
                    actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
                )

        # Bucket C3b Phase 3 — auto-create a daily_presence row tying
        # the picked crew to the activity at first shift. Idempotent on
        # (daily_report_id, schedule_activity_id, sub_id) so re-picking
        # the same crew doesn't duplicate.
        if crew_picked_for_presence:
            try:
                existing = await db.daily_presence.find_one(
                    {"daily_report_id": before["daily_report_id"],
                     "schedule_activity_id": task_id,
                     "sub_id": crew_picked_for_presence},
                    {"_id": 0, "id": 1},
                )
                if not existing:
                    # Pull trade + company from the daily_presence aggregate
                    # row that the user added to the crew presence panel.
                    src = await db.daily_presence.find_one(
                        {"id": crew_picked_for_presence},
                        {"_id": 0, "trade": 1, "company_name": 1},
                    ) or {}
                    presence_doc = _presence_doc_with_derived({
                        "id": gen_id(),
                        "tenant_id": ctx["tenant_id"],
                        "project_id": dr.get("project_id"),
                        "daily_report_id": before["daily_report_id"],
                        "trade": src.get("trade") or (task_before.get("trade_code") if task_before else "general"),
                        "sub_id": crew_picked_for_presence,
                        "company_name": src.get("company_name"),
                        "sub_name": src.get("company_name"),
                        "headcount": int(updates.get("crew_count") or after.get("crew_count") or 0),
                        "hours_per_person": 8.0,
                        "shift": "first",
                        "schedule_activity_id": task_id,
                        "zone_id": None,
                        "zone_name": None,
                        "notes": "Auto-created from activity grid crew dropdown",
                        "created_by": ctx["user"]["id"],
                        "created_at": now_iso,
                    })
                    await db.daily_presence.insert_one(presence_doc.copy())
                    # Bucket C3b Phase 5 — audit the auto-create with a
                    # tag in `op` so it's distinguishable from an explicit
                    # roster INSERT in audit replays.
                    await write_audit(
                        tenant_id=ctx["tenant_id"], table_name="daily_presence",
                        op="AUTO_INSERT_FROM_GRID",
                        record_id=presence_doc["id"], before=None, after=presence_doc,
                        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
                    )
            except Exception:
                # Auto-create is best-effort — sync write-back already
                # wrote the linked_crew_id; presence row failure shouldn't
                # block the activity grid edit.
                pass

        # productivity observation (master prompt §16) — unchanged from
        # before this commit; lives here so it fires on the same EOD%
        # transition as the sync write-back.
        if "pct_complete_end_of_day" in updates and (updates.get("hours_worked") or after.get("hours_worked")):
            t = task_after or task_before
            await db.productivity_observations.insert_one({
                "id": gen_id(),
                "tenant_id": ctx["tenant_id"],
                "project_id": dr.get("project_id") or (t.get("project_id") if t else None),
                "schedule_activity_id": task_id,
                "trade": t.get("trade_code") if t else None,
                "location_id": t.get("location_id") if t else None,
                "crew_id": after.get("crew_id"),
                "date": await project_today_iso_for(db, ctx["tenant_id"], dr.get("project_id") or (t.get("project_id") if t else None)),
                "hours_worked": float(after.get("hours_worked") or 0),
                "pct_delta": float(updates["pct_complete_end_of_day"]) - float(before.get("pct_complete_end_of_day", 0)),
                "created_at": now_iso,
            })

    # Bucket D Addition 5 (2026-05-12) -- post-submit grid edit audit.
    # Fires ACTIVITY_GRID_EDITED_POST_SUBMIT (NOT EVENT_EDITED_POST_SUBMIT)
    # so counsel can separately filter activity grid edits from event edits
    # in the audit log dropdown. Only fires when the parent DR was locked.
    if is_locked_edit:
        await stamp_edit_metadata(
            db=db, collection="daily_report_activities",
            record_id=row_id, tenant_id=ctx["tenant_id"],
            # Bug #49: effective_edit_reason == edit_reason here
            # (activity grid not in STATUS_TRANSITION_CONFIGS).
            actor_id=ctx["user"]["id"], edit_reason=effective_edit_reason,
            before=before, after=after,
            request_id=ctx["request_id"],
            audit_op="ACTIVITY_GRID_EDITED_POST_SUBMIT",
        )

    return DailyReportActivity(**after)


def _task_for_409(t: dict) -> dict:
    """Pick the fields the frontend needs to repaint a row after a stale
    write detection. Trimmed so we don't ship the entire task doc."""
    return {
        "id": t.get("id"),
        "activity_code": t.get("activity_code"),
        "wbs": t.get("wbs"),
        "description": t.get("description"),
        "pct_complete": t.get("pct_complete"),
        "status": t.get("status"),
        "current_crew_id": t.get("current_crew_id"),
        "actual_hours_to_date": t.get("actual_hours_to_date"),
        "updated_at": t.get("updated_at"),
    }


class SubmitPayload(StrictModel):
    notes: Optional[str] = None
    # Addition 3 -- no-work-day acknowledgment fields. Required when the
    # DR has zero activities AND zero events; otherwise ignored.
    is_no_work_day: bool = False
    no_work_reason: Optional[str] = None  # NoWorkReason literal validated below
    no_work_note: Optional[str] = None


# Addition 3 -- the event collections that count as "did anything
# happen on this day". Activities are counted separately because
# they can be auto-prepopulated from the schedule grid.
_NO_WORK_EVENT_COLLECTIONS = (
    "delays", "rfis", "qc_issues", "safety_events",
    "deliveries", "photos",
)
_NO_WORK_REASON_VALUES = {
    "weather", "holiday", "scheduled_non_work",
    "crew_unavailable", "tenant_restriction", "other",
}


async def _dr_is_empty(db, daily_report_id: str, tenant_id: str) -> bool:
    """True if the DR has no activity rows AND no events. Coordination
    events use linked_daily_report_id (different field name)."""
    has_activities = await db.daily_report_activities.count_documents(
        {"daily_report_id": daily_report_id}, limit=1,
    )
    if has_activities:
        return False
    for coll in _NO_WORK_EVENT_COLLECTIONS:
        n = await db[coll].count_documents(
            tenant_filter(tenant_id, {"daily_report_id": daily_report_id}),
            limit=1,
        )
        if n:
            return False
    coord = await db.coordination_events.count_documents(
        tenant_filter(tenant_id, {"linked_daily_report_id": daily_report_id}),
        limit=1,
    )
    if coord:
        return False
    return True


@router.post("/daily-reports/{daily_report_id}/submit", response_model=DailyReport)
async def submit_daily_report(daily_report_id: str, payload: SubmitPayload, ctx: dict = Depends(require_tenant)):
    db = get_db()
    before = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Daily report not found")

    # Addition 3 + Test 2G follow-up (2026-05-12) -- empty DR requires
    # explicit no-work-day ack. Returns BOTH the legacy `error` key
    # (preserved for existing clients) AND the new `error_code` key
    # (matches the Test 2G spec naming "_confirmation" suffix). The
    # frontend already accepts either; new clients should use the
    # `error_code` key for new branches.
    if await _dr_is_empty(db, daily_report_id, ctx["tenant_id"]):
        if not payload.is_no_work_day:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "empty_dr_requires_no_work_day",
                    "error_code": "empty_dr_requires_no_work_day_confirmation",
                    "message": (
                        "This daily report has no activities or events. "
                        "Submit it explicitly as a no-work day with a "
                        "reason, or add at least one activity / event "
                        "first."
                    ),
                    "valid_no_work_reasons": sorted(_NO_WORK_REASON_VALUES),
                    "suggested_action": (
                        "Open the no-work-day modal and pick a reason "
                        "(weather / holiday / scheduled_non_work / "
                        "crew_unavailable / tenant_restriction / other), "
                        "then re-submit with is_no_work_day=true."
                    ),
                },
            )
        if payload.no_work_reason not in _NO_WORK_REASON_VALUES:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_no_work_reason",
                    "message": f"no_work_reason must be one of: "
                                f"{', '.join(sorted(_NO_WORK_REASON_VALUES))}",
                },
            )
        if payload.no_work_reason == "other" and not (payload.no_work_note or "").strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "no_work_note_required_when_other",
                    "message": "no_work_note is required when no_work_reason='other'.",
                },
            )
    elif payload.is_no_work_day:
        # Caller flagged it as no-work but the DR isn't actually empty;
        # refuse to flip the flag rather than silently accept the
        # contradiction (corrupts TIA Section 11).
        raise HTTPException(
            status_code=422,
            detail={
                "error": "is_no_work_day_with_recorded_work",
                "message": (
                    "Cannot submit as no-work day: the daily report has "
                    "recorded activities or events. Either remove them "
                    "or submit normally."
                ),
            },
        )

    # Feature F3 (2026-05-18) -- submit-time constraint validation
    # REMOVED. The Constraints column has been removed from the DR
    # Activity Grid: it was redundant with the F3 blocker badge, which
    # surfaces the same open RFIs / delays / over-SLA submittals from
    # the linked task. A zero-progress row therefore no longer requires
    # an explicit constraint to submit -- "why a row didn't move" is
    # read off the blocker badge instead. The constraint_* fields stay
    # on the daily_report_activities model so historical (pre-F3) rows
    # keep their data; only the submit gate is dropped.

    updates = {
        "status": "submitted",
        "submitted_by": ctx["user"]["id"],
        "submitted_at": utc_now().isoformat(),
    }
    if payload.notes:
        updates["notes"] = payload.notes
    # Addition 3 -- persist the no-work ack so TIA Section 11
    # (Calendar Reconciliation) and Section 12 (Audit Log Extract)
    # can render it later. Stamping happens here even on non-empty
    # DRs because the contradiction case (is_no_work_day=true on a
    # non-empty DR) was rejected above.
    if payload.is_no_work_day:
        updates["is_no_work_day"] = True
        updates["no_work_reason"] = payload.no_work_reason
        updates["no_work_note"] = (payload.no_work_note or "").strip() or None
    await db.daily_reports.update_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"$set": updates}
    )

    # Bucket H Phase 5 -- lock presence-actualized capacity rows for
    # this DR's date so future leveling recomputes don't overwrite
    # field-attested truth. presence_locked=True flag on each row;
    # recompute_capacity_for_crew skips locked rows.
    try:
        from datetime import date as _date
        from services.resource_leveling import lock_capacity_for_dr_submit
        dr_date_str = before.get("report_date")
        if dr_date_str:
            dr_date_parsed = _date.fromisoformat(str(dr_date_str)[:10])
            lock_res = await lock_capacity_for_dr_submit(
                tenant_id=ctx["tenant_id"],
                project_id=before.get("project_id"),
                dr_date=dr_date_parsed,
            )
            if lock_res.get("locked_count"):
                await write_audit(
                    tenant_id=ctx["tenant_id"], table_name="crew_capacity",
                    op="CAPACITY_ACTUALIZED_FROM_PRESENCE",
                    record_id=daily_report_id, before=None,
                    after={"action": "locked_on_dr_submit",
                            "dr_date": dr_date_str,
                            "rows_locked": lock_res["locked_count"]},
                    actor_id=ctx["user"]["id"],
                    request_id=ctx["request_id"],
                )
    except Exception:
        # Lock failure doesn't undo the submit. The capacity row
        # remains mutable; the next admin reallocation can re-lock.
        pass

    # Bucket C1 Phase 1 — flip every provisional sync mark to confirmed.
    # This makes the write-backs that landed during the day part of the
    # field-attested record once the super submits the report.
    confirm_at = utc_now().isoformat()
    await db.daily_report_activities.update_many(
        {"daily_report_id": daily_report_id},
        {"$set": {"sync_state": "confirmed", "last_sync_at": confirm_at}},
    )
    # Same flip on tasks whose last_synced_from_dr points at this report.
    await db.tasks.update_many(
        tenant_filter(ctx["tenant_id"], {"last_synced_from_dr.daily_report_id": daily_report_id}),
        {"$set": {
            "last_synced_from_dr.sync_state": "confirmed",
            "updated_at": confirm_at,
        }},
    )

    after = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_reports", op="UPDATE",
        record_id=daily_report_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Addition 3 -- separate audit op when the submit was a no-work day,
    # so audit-log extracts and TIA Section 12 surface these distinctly
    # from regular submits.
    if payload.is_no_work_day:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="daily_reports",
            op="NO_WORK_DAY_SUBMITTED",
            record_id=daily_report_id, before=None,
            after={
                "report_date": after.get("report_date"),
                "no_work_reason": after.get("no_work_reason"),
                "no_work_note": after.get("no_work_note"),
            },
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    return DailyReport(**after)


@router.get("/projects/{project_id}/weekly-rollup")
async def weekly_rollup(project_id: str, week_starting: date, ctx: dict = Depends(require_tenant)):
    """Master prompt §10.4: auto-generated from 7 days of dailies."""
    db = get_db()
    start = week_starting
    end = start + timedelta(days=6)
    drs = await db.daily_reports.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "report_date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0},
    ).to_list(7)
    dr_ids = [d["id"] for d in drs]
    rows = await db.daily_report_activities.find({"daily_report_id": {"$in": dr_ids}}, {"_id": 0}).to_list(2000)

    total_hours = sum(float(r.get("hours_worked") or 0) for r in rows)
    delay_minutes = sum(int(r.get("delay_minutes") or 0) for r in rows)
    weather_minutes = sum(int(r.get("weather_impact_minutes") or 0) for r in rows)
    by_trade: dict[str, float] = {}
    for r in rows:
        tr = r.get("trade_code") or "UNK"
        by_trade[tr] = by_trade.get(tr, 0) + float(r.get("hours_worked") or 0)

    completed_planned = sum(1 for r in rows if (r.get("pct_complete_end_of_day") or 0) >= 100)
    total_planned = len(rows)
    ppc = round((completed_planned / total_planned) * 100, 1) if total_planned else 0

    return {
        "week_starting": start.isoformat(),
        "week_ending": end.isoformat(),
        "daily_report_count": len(drs),
        "total_hours_worked": total_hours,
        "delay_minutes": delay_minutes,
        "weather_impact_minutes": weather_minutes,
        "hours_by_trade": by_trade,
        "ppc": ppc,
        "activities_completed": completed_planned,
        "total_planned_activities": total_planned,
    }


@router.get("/daily-reports/{daily_report_id}/pdf")
async def export_daily_pdf(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Master prompt §10.5: stream bytes directly. NO S3 presigned URLs.

    SUBSTITUTION: ReportLab not pre-installed. We stream a clean text/plain
    representation that the frontend offers as a download. The Railway
    deployment uses ReportLab/WeasyPrint for true PDF.
    """
    from fastapi import Response
    # CPM Part G (2026-05-15) -- "Date:" line in the export body
    # renders MM/DD/YYYY via the shared backend chokepoint. The
    # filename below stays ISO YYYY-MM-DD (no slashes in filenames;
    # ISO sorts chronologically in file managers).
    from services.date_format import format_date_mmddyyyy
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    rows = await db.daily_report_activities.find({"daily_report_id": daily_report_id}, {"_id": 0}).sort("position", 1).to_list(200)
    # Feature DR Header Auto-Population (2026-05-20) -- need the full
    # project record so the formal contract header (client / station /
    # contract # / contractor) prints alongside the project name.
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": dr["project_id"]}), {"_id": 0},
    )
    # Signer is the canonical submitter when the DR has been submitted,
    # else the preparer, else (last resort) the exporter -- so the
    # signature line doesn't change per-viewer when re-exported later.
    signer_id = dr.get("submitted_by") or dr.get("prepared_by")
    signer_name = ""
    if signer_id:
        _u = await db.users.find_one(
            tenant_filter(ctx["tenant_id"], {"id": signer_id}),
            {"_id": 0, "full_name": 1},
        )
        signer_name = (_u or {}).get("full_name", "") or ""
    if not signer_name:
        signer_name = (ctx.get("user") or {}).get("full_name", "") or ""

    proj = proj or {}
    lines = [
        f"DAILY REPORT — {proj.get('name') or dr['project_id']}",
        # Feature DR Header Auto-Population (2026-05-20).
        f"Client:           {proj.get('client_name') or ''}",
        f"Station:          {proj.get('station_id') or ''}",
        f"Project title:    {proj.get('name') or ''}",
        f"Contract number:  {proj.get('contract_number') or ''}",
        f"Contractor:       {proj.get('contractor_name') or ''}",
        f"Signature:        {signer_name}",
        f"Date: {format_date_mmddyyyy(dr['report_date'])}",
        f"Status: {dr['status']}",
        f"Weather: {dr.get('weather_conditions') or 'n/a'}, "
        f"high {dr.get('weather_temp_high_f')}F low {dr.get('weather_temp_low_f')}F, "
        f"precip {dr.get('weather_precipitation_in') or 0}in",
        "-" * 80,
        "ACTIVITY GRID",
        "-" * 80,
    ]
    for r in rows:
        lines.append(
            f"[{r.get('activity_code')}] {r.get('activity_description')[:60]}  "
            f"{r.get('pct_complete_start_of_day')}% → {r.get('pct_complete_end_of_day')}%  "
            f"crew={r.get('crew_count')} hrs={r.get('hours_worked')}  delay={r.get('delay_minutes')}min"
        )
    lines.append("-" * 80)
    if dr.get("notes"):
        lines.append("NOTES:")
        lines.append(dr["notes"])
    body = "\n".join(lines).encode("utf-8")

    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="daily_report_{dr["report_date"]}.txt"'},
    )


# ============================================================
# Feature Client DR Template Export (2026-05-20)
# Fill the project's uploaded client template with this DR's data
# via openpyxl, stream the resulting xlsx back as an attachment.
# Returns 404 if no template is uploaded for the project, 400 if
# the uploaded template is a PDF (engine is xlsx-only this sprint).
# ============================================================
@router.get("/daily-reports/{daily_report_id}/export/client-template")
async def export_dr_via_client_template(
    daily_report_id: str,
    ctx: dict = Depends(require_tenant),
):
    from fastapi import Response
    db = get_db()
    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0},
    )
    if not dr:
        raise HTTPException(404, "Daily report not found")
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": dr["project_id"]}), {"_id": 0},
    )
    if not proj or not proj.get("dr_template_file_url"):
        raise HTTPException(
            404, "No client template uploaded for this project.",
        )
    template_name = proj.get("dr_template_file_name") or "template"
    ext = template_name.rsplit(".", 1)[-1].lower() if "." in template_name else ""
    if ext == "pdf":
        raise HTTPException(
            400,
            "PDF template export not yet supported -- please upload an "
            "xlsx template.",
        )

    # Activity rows from the DR grid.
    activities = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id}, {"_id": 0},
    ).sort("position", 1).to_list(100)

    # Feature Client DR Template Export (2026-05-20, AI rewrite) --
    # the AI engine also reasons about crew-presence and delay rows
    # so a template with a manpower table or delays section gets
    # filled correctly. Soft-skip rows that aren't there.
    presence = await db.daily_presence.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}),
        {"_id": 0},
    ).sort("trade", 1).to_list(200)
    delays = await db.delays.find(
        tenant_filter(
            ctx["tenant_id"],
            {"daily_report_id": daily_report_id, "status": {"$ne": "deleted"}},
        ),
        {"_id": 0},
    ).sort("reported_at", -1).to_list(100)

    # Signer precedence -- same as the PDF exports (submitted_by ->
    # prepared_by -> current exporter).
    signer_id = dr.get("submitted_by") or dr.get("prepared_by")
    signer_name = ""
    if signer_id:
        _u = await db.users.find_one(
            tenant_filter(ctx["tenant_id"], {"id": signer_id}),
            {"_id": 0, "full_name": 1},
        )
        signer_name = (_u or {}).get("full_name", "") or ""
    if not signer_name:
        signer_name = (ctx.get("user") or {}).get("full_name", "") or ""

    # AI-powered injection. DrTemplateExportError -> 502 with the AI's
    # error attached. No fallback to a heuristic engine (per spec).
    from services.storage import get_object
    from services.dr_template_export import (
        inject_dr_into_template,
        DrTemplateExportError,
    )
    template_bytes, _ct = get_object(proj["dr_template_file_url"])
    try:
        filled_bytes = await inject_dr_into_template(
            template_bytes,
            dr=dr, project=proj, activities=activities,
            presence=presence, delays=delays,
            signer_name=signer_name,
            tenant_id=ctx["tenant_id"],
            request_id=ctx["request_id"],
        )
    except DrTemplateExportError as e:
        raise HTTPException(502, str(e))

    safe_proj = (proj.get("name") or "project").replace("/", "-").replace("\\", "-")
    out_name = f"DR_{dr.get('report_date') or 'unknown'}_{safe_proj}.xlsx"
    return Response(
        content=filled_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "content-disposition": f'attachment; filename="{out_name}"',
        },
    )


# ============================================================
# PATCH daily report (weather, renovation flags, notes)
# ============================================================
@router.patch("/daily-reports/{daily_report_id}", response_model=DailyReport)
async def patch_daily_report(daily_report_id: str, payload: DailyReportUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    before = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Daily report not found")
    if before.get("status") == "submitted":
        raise HTTPException(409, "Daily report is locked (submitted). Cannot edit.")
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        return DailyReport(**before)
    await db.daily_reports.update_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"$set": updates},
    )
    after = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_reports", op="UPDATE",
        record_id=daily_report_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return DailyReport(**after)


# ============================================================
# Weather auto-fill (Open-Meteo geocoding + forecast)
# ============================================================
def _geocode(city: Optional[str], state: Optional[str], postal: Optional[str] = None) -> Optional[tuple]:
    """Open-Meteo geocoding API — free, no key. Strategy:
       - Primary: search by city name, prefer the result whose admin1 matches state.
       - Fallback: search 'city, state' as one string.
    Returns (lat, lng) or None.
    """
    state_aliases = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
        "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
        "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
        "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
        "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
        "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
        "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
        "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
        "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
        "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
        "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    }
    state_full = state_aliases.get((state or "").upper(), state or "")

    def _try(name: str, admin1: Optional[str] = None):
        try:
            params = {"name": name[:80], "count": 5, "language": "en", "format": "json"}
            r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params=params, timeout=5)
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
            if admin1:
                for res in results:
                    if (res.get("admin1") or "").lower() == admin1.lower() and (res.get("country_code") or "US") == "US":
                        return float(res["latitude"]), float(res["longitude"])
            for res in results:
                if (res.get("country_code") or "US") == "US":
                    return float(res["latitude"]), float(res["longitude"])
            if results:
                return float(results[0]["latitude"]), float(results[0]["longitude"])
        except Exception:
            return None
        return None

    if city:
        coords = _try(city, state_full)
        if coords:
            return coords
    if city and state_full:
        coords = _try(f"{city}, {state_full}")
        if coords:
            return coords
    if postal:
        coords = _try(postal)
        if coords:
            return coords
    return None


def _fetch_weather(lat: float, lng: float, target_date: str) -> Optional[dict]:
    """Open-Meteo forecast for a specific date (today or near-future).
    Falls back to today's snapshot if target_date is out-of-range."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lng,
                "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code,wind_speed_10m_max",
                "start_date": target_date, "end_date": target_date,
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "auto",
            },
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    cur = data.get("current", {}) or {}
    daily = data.get("daily", {}) or {}
    code_map = {
        0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow",
        80: "showers", 81: "showers", 82: "heavy showers",
        95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
    }
    code = (daily.get("weather_code") or [cur.get("weather_code", 0)])[0]
    return {
        "weather_temp_high_f": (daily.get("temperature_2m_max") or [None])[0],
        "weather_temp_low_f": (daily.get("temperature_2m_min") or [None])[0],
        "weather_conditions": code_map.get(code, "unknown"),
        "weather_precipitation_in": (daily.get("precipitation_sum") or [0])[0] or 0,
        "weather_wind_mph": (daily.get("wind_speed_10m_max") or [cur.get("wind_speed_10m")])[0],
    }


@router.post("/daily-reports/{daily_report_id}/weather-autofill", response_model=DailyReport)
async def weather_autofill(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Geocode the project's city/state, fetch Open-Meteo forecast for the
    report_date, save into the DR doc. Idempotent — re-runs overwrite.

    2026-08-XX (prod-bug-fix) -- _geocode + _fetch_weather use synchronous
    `requests.get`. Running them inline blocked the event loop for up to
    18s per call and, under concurrent DR opens on production, stacked up
    long enough for Cloudflare's origin-timeout to fire (520/522 HTML
    error page that leaked into the frontend toast). Fix:
      1. Offload both calls onto the FastAPI thread-pool so the event
         loop stays responsive.
      2. Cap the whole autofill at 12s via asyncio.wait_for -- Open-Meteo
         is usually <500ms, so anything longer is a lost cause; return
         a clean 504 the frontend can toast cleanly.
    """
    from starlette.concurrency import run_in_threadpool
    import asyncio as _asyncio
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    if dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": dr["project_id"]}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    addr_parts = [proj.get("city"), proj.get("state"), proj.get("postal_code")]
    try:
        coords = await _asyncio.wait_for(
            run_in_threadpool(
                _geocode, proj.get("city"), proj.get("state"), proj.get("postal_code"),
            ),
            timeout=6.0,
        )
    except _asyncio.TimeoutError:
        raise HTTPException(504, "Geocoding provider slow to answer — try Refresh in a moment")
    if not coords:
        addr = ", ".join([p for p in addr_parts if p]).strip(", ")
        raise HTTPException(422, f"Could not geocode project address ({addr or 'no city/state on project'})")
    try:
        weather = await _asyncio.wait_for(
            run_in_threadpool(_fetch_weather, coords[0], coords[1], dr["report_date"]),
            timeout=6.0,
        )
    except _asyncio.TimeoutError:
        raise HTTPException(504, "Weather provider slow to answer — try Refresh in a moment")
    if not weather:
        raise HTTPException(502, "Weather provider unreachable")
    weather["weather_fetched_at"] = utc_now().isoformat()
    await db.daily_reports.update_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"$set": weather},
    )
    after = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    return DailyReport(**after)


# ============================================================
# Crew presence (per-trade headcount + hours/person)
# ============================================================
@router.get("/daily-reports/{daily_report_id}/presence", response_model=list[DailyPresence])
async def list_presence(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0, "id": 1})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    docs = await db.daily_presence.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0},
    ).sort("trade", 1).to_list(200)
    return [DailyPresence(**d) for d in docs]


def _presence_doc_with_derived(doc: dict) -> dict:
    """Bucket C3b Phase 3 — sub_name/company_name reconciliation +
    total_hours computation. Done both at create and patch so the
    aggregate views can sum total_hours directly without re-multiplying."""
    sub_name = doc.get("sub_name")
    if sub_name and not doc.get("company_name"):
        doc["company_name"] = sub_name
    elif doc.get("company_name") and not sub_name:
        doc["sub_name"] = doc["company_name"]
    headcount = float(doc.get("headcount") or 0)
    hours = float(doc.get("hours_per_person") or 0)
    doc["total_hours"] = round(headcount * hours, 2)
    return doc


@router.post("/daily-reports/{daily_report_id}/presence", response_model=DailyPresence)
async def create_presence(daily_report_id: str, payload: DailyPresenceCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    if dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")
    if payload.daily_report_id != daily_report_id:
        raise HTTPException(400, "daily_report_id mismatch")
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        # Bucket C3b Phase 3 — denormalize project_id so the new
        # (tenant, project, *) indexes work for cross-DR aggregations.
        "project_id": dr.get("project_id"),
        "created_by": ctx["user"]["id"], "created_at": utc_now().isoformat(),
    })
    doc = _presence_doc_with_derived(doc)
    await db.daily_presence.insert_one(doc.copy())
    # Bucket C3b Phase 5 — audit every presence mutation so the federal
    # contract trail captures who logged crew, when, and at what counts.
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_presence", op="INSERT",
        record_id=doc["id"], before=None, after=doc,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Bucket H Phase 5 -- presence-actualized capacity flip (pulled
    # forward from Phase 4 deferral #7). What actually showed up on
    # site is the source of truth for committed_headcount + computed_
    # from on the matching crew's capacity row for this date. Picks a
    # representative crew (home_project_id match > general pool >
    # first-trade-match); no-op if no crew matches. Locked-flip
    # happens on DR submit -- this write keeps the row mutable so
    # presence corrections during the day still go through.
    try:
        from datetime import date as _date
        from services.resource_leveling import actualize_capacity_from_presence
        from audit import write_audit as _wa
        dr_date_str = dr.get("report_date")
        if dr_date_str and doc.get("trade") and (doc.get("headcount") or 0) > 0:
            dr_date_parsed = _date.fromisoformat(str(dr_date_str)[:10])
            actual_res = await actualize_capacity_from_presence(
                tenant_id=ctx["tenant_id"],
                project_id=dr.get("project_id"),
                presence_date=dr_date_parsed,
                trade=doc["trade"],
                headcount=int(doc["headcount"]),
                company_name=doc.get("company_name"),
                locked=False,
                actor_id=ctx["user"]["id"],
            )
            if actual_res.get("updated_count"):
                await _wa(
                    tenant_id=ctx["tenant_id"], table_name="crew_capacity",
                    op="CAPACITY_ACTUALIZED_FROM_PRESENCE",
                    record_id=actual_res.get("picked_crew_id") or doc["id"],
                    before=actual_res.get("before"),
                    after={
                        **(actual_res.get("after") or {}),
                        "source_presence_id": doc["id"],
                        "source_daily_report_id": daily_report_id,
                        "trade": doc["trade"],
                        "actualized_headcount": int(doc["headcount"]),
                    },
                    actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
                )
    except Exception:
        # Don't fail the presence write if the actualization hook hiccups.
        # The next leveling recompute will re-derive from default.
        pass
    return DailyPresence(**doc)


# Followup-fix 5 (2026-05-07): URL alias under the /daily-presence
# path to match the PATCH/DELETE convention. Also lets a top-level
# POST /daily-presence (with daily_report_id in the body) work for
# legacy/older frontend builds. Both routes delegate to create_presence
# above so the audit + denormalization + total_hours derivation stay
# in one place. User reported 404 on this surface; URL-source confusion
# across deploy versions was the most likely cause.
@router.post("/daily-reports/{daily_report_id}/daily-presence", response_model=DailyPresence)
async def create_presence_alias(
    daily_report_id: str, payload: DailyPresenceCreate,
    ctx: dict = Depends(require_tenant),
):
    return await create_presence(daily_report_id, payload, ctx)


@router.post("/daily-presence", response_model=DailyPresence)
async def create_presence_toplevel(
    payload: DailyPresenceCreate, ctx: dict = Depends(require_tenant),
):
    if not payload.daily_report_id:
        raise HTTPException(400, "daily_report_id required in body for top-level POST /daily-presence")
    return await create_presence(payload.daily_report_id, payload, ctx)


@router.patch("/daily-presence/{presence_id}", response_model=DailyPresence)
async def patch_presence(presence_id: str, payload: DailyPresenceUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.daily_presence.find_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Presence row not found")
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": doc["daily_report_id"]}), {"_id": 0, "status": 1, "project_id": 1})
    if dr and dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")
    raw = payload.model_dump(mode="json", exclude_unset=True)
    updates = {k: v for k, v in raw.items() if v is not None}
    if updates:
        # Recompute total_hours when headcount or hours_per_person moves.
        if "headcount" in updates or "hours_per_person" in updates:
            new_hc = updates.get("headcount", doc.get("headcount") or 0)
            new_hp = updates.get("hours_per_person", doc.get("hours_per_person") or 0)
            updates["total_hours"] = round(float(new_hc) * float(new_hp), 2)
        # Backfill project_id if missing on legacy rows.
        if not doc.get("project_id") and dr and dr.get("project_id"):
            updates["project_id"] = dr["project_id"]
        # Mirror sub_name/company_name when one is patched.
        if "sub_name" in updates and "company_name" not in updates:
            updates["company_name"] = updates["sub_name"]
        elif "company_name" in updates and "sub_name" not in updates:
            updates["sub_name"] = updates["company_name"]
        await db.daily_presence.update_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"$set": updates})
    after = await db.daily_presence.find_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"_id": 0})
    # Bucket C3b Phase 5 — audit on every PATCH, even no-op (so the chain
    # records that someone hit the endpoint), but include a meaningful
    # diff via before/after.
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_presence", op="UPDATE",
        record_id=presence_id, before=doc, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return DailyPresence(**after)


@router.delete("/daily-presence/{presence_id}")
async def delete_presence(presence_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.daily_presence.find_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Presence row not found")
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": doc["daily_report_id"]}), {"_id": 0, "status": 1})
    if dr and dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")
    await db.daily_presence.delete_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}))
    # Bucket C3b Phase 5 — audit the hard-delete (presence has no soft
    # delete; deletion erases the row, so the audit is the only trail).
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_presence", op="DELETE",
        record_id=presence_id, before=doc, after=None,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True}


# ============================================================
# Unplanned activity creation from the field (super)
# ============================================================
@router.post("/daily-reports/{daily_report_id}/unplanned-activity")
async def create_unplanned_activity(daily_report_id: str, payload: UnplannedActivityCreate, ctx: dict = Depends(require_tenant)):
    """Field super creates a task NOT in the master schedule — the platform
    records it with `created_in_field=true` and `requires_pm_review=true`
    so the PM can review/promote/merge into the master schedule. The new
    task is automatically added to the DR grid."""
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    # Addition 5 (2026-05-12) -- "add to locked DR" flow. The prior
    # 409 reject on submitted DRs is replaced with the tiered add
    # policy. Unplanned activities can now be added to locked DRs
    # with an add_reason (or admin/PM directly).
    from services.edit_permissions import enforce_locked_dr_add_policy
    _dr, is_locked_add = await enforce_locked_dr_add_policy(
        ctx=ctx, daily_report_id=daily_report_id,
        add_reason=payload.add_reason, event_type="activity",
    )

    # Auto-assign next activity_code (same convention as copilot/_tool_create_task)
    last = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": dr["project_id"]}),
        {"_id": 0, "activity_code": 1},
    ).sort("activity_code", -1).limit(50).to_list(50)
    next_n = 1
    for r in last:
        m = re.search(r"(\d+)$", r.get("activity_code", "") or "")
        if m:
            next_n = max(next_n, int(m.group(1)) + 1)
    activity_code = f"AF{next_n:04d}"  # AF prefix = Activity-from-Field

    today_iso = dr["report_date"]
    dur = float(payload.duration_days or 1)
    finish = (date.fromisoformat(today_iso) + timedelta(days=int(dur))).isoformat()
    task_doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": dr["project_id"],
        "activity_code": activity_code,
        "description": payload.description,
        "wbs": None,
        "trade_code": payload.trade_code,
        "location_id": payload.location_id,
        "crew_id": payload.crew_id,
        "status": "in_progress",
        "planned_start": today_iso,
        "planned_finish": finish,
        "duration_days": dur,
        "budgeted_hours": None,
        "actual_hours": 0,
        "budgeted_quantity": None,
        "actual_quantity": 0,
        "quantity_unit": None,
        "pct_complete": 0,
        "prev_pct_complete": 0,
        "is_milestone": False,
        "is_critical_path": False,
        "lookahead_status": "in_window",
        "predecessors": [],
        "created_in_field": True,
        "requires_pm_review": True,
        "created_from_daily_report_id": daily_report_id,
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.tasks.insert_one(task_doc.copy())

    # Also push it to the DR grid
    pos_max = await db.daily_report_activities.find({"daily_report_id": daily_report_id}, {"position": 1}).sort("position", -1).limit(1).to_list(1)
    next_pos = (pos_max[0]["position"] + 1) if pos_max else 0
    loc_name = None
    if payload.location_id:
        loc = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.location_id}), {"_id": 0, "name": 1})
        if loc:
            loc_name = loc["name"]
    target_size = 0
    if payload.crew_id:
        crew = await db.crews.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.crew_id}), {"_id": 0, "target_size": 1})
        if crew:
            target_size = int(crew.get("target_size") or 0)
    row = {
        "id": gen_id(),
        "daily_report_id": daily_report_id,
        "schedule_activity_id": task_doc["id"],
        "position": next_pos,
        "pct_complete_start_of_day": 0,
        "pct_complete_end_of_day": 0,
        "crew_count": target_size,
        "crew_id": payload.crew_id,
        "hours_worked": 0,
        "delay_minutes": 0,
        "delay_reason": None,
        "weather_impact_minutes": 0,
        "notes": payload.notes,
        "status_at_end_of_day": "in_progress",
        "activity_code": activity_code,
        "activity_description": payload.description,
        "location_name": loc_name,
        "trade_code": payload.trade_code,
    }
    await db.daily_report_activities.insert_one(row.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tasks", op="INSERT",
        record_id=task_doc["id"], before=None, after=task_doc,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    if is_locked_add:
        from services.edit_permissions import stamp_add_metadata
        # Stamp BOTH the task row (so PM review surfaces "added post-
        # submit") AND the grid row (so the DR's audit chain mirrors it).
        await stamp_add_metadata(
            db=db, collection="tasks", record_id=task_doc["id"],
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            add_reason=payload.add_reason or "", after=task_doc,
            request_id=ctx["request_id"],
        )
        await stamp_add_metadata(
            db=db, collection="daily_report_activities", record_id=row["id"],
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            add_reason=payload.add_reason or "", after=row,
            request_id=ctx["request_id"],
        )
    return {"task": {k: v for k, v in task_doc.items() if k != "_id"},
            "grid_row": row}


# ============================================================
# Approve daily report (PM/admin)
# ============================================================
@router.post("/daily-reports/{daily_report_id}/approve", response_model=DailyReport)
async def approve_daily_report(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    role = (ctx["user"].get("role") or "").lower()
    if role not in ("admin", "pm"):
        raise HTTPException(403, "Only admin or pm can approve daily reports")
    db = get_db()
    before = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Daily report not found")
    if before.get("status") != "submitted":
        raise HTTPException(409, "Daily report must be submitted before approval")
    updates = {
        "status": "approved",
        "approved_by": ctx["user"]["id"],
        "approved_at": utc_now().isoformat(),
    }
    await db.daily_reports.update_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"$set": updates},
    )
    after = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_reports", op="UPDATE",
        record_id=daily_report_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return DailyReport(**after)


# ============================================================
# CONTEXT BUNDLE — single round-trip for the page rebuild + copilot
# ============================================================
async def _user_label_map(db, tenant_id: str, ids: list[str]) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    docs = await db.users.find({"id": {"$in": list(set(ids))}}, {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "email": 1}).to_list(len(ids))
    out = {}
    for d in docs:
        nm = " ".join([p for p in [d.get("first_name"), d.get("last_name")] if p]).strip() or d.get("email") or d["id"][:8]
        out[d["id"]] = nm
    return out


@router.get("/daily-reports/{daily_report_id}/context")
async def daily_report_context(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    """Single-shot bundle for DailyReports.jsx + copilot:
       - report
       - project (incl. project_type)
       - grid (activities)
       - presence rows
       - photos / voice annotations / delays / safety / qc / rfis / deliveries
         filtered to this DR (or to this date for deliveries)
       - audit log entries
       - user labels (for prepared/submitted/approved by)
    """
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    project_id = dr["project_id"]
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0}) or {}

    grid = await db.daily_report_activities.find({"daily_report_id": daily_report_id}, {"_id": 0}).sort("position", 1).to_list(500)
    grid_task_ids = [r["schedule_activity_id"] for r in grid if r.get("schedule_activity_id")]
    # Features F2/F3/F4 (2026-05-18) -- stamp the DR Activity Grid view
    # fields (work_category, open_blocker_count, auto_status) onto each
    # row alongside the pre-existing task_updated_at optimistic-lock
    # token. Single $in query. METADATA / VIEW ONLY.
    await _enrich_grid_rows(db, ctx["tenant_id"], grid)

    # Bucket C3a Phase 3 — pre-populate constraint_type +
    # constraint_reference for any row that has no constraint set yet,
    # using the most recent open gating_entity that gates the row's
    # task. Persisted (idempotent) so a manual override sticks across
    # reads. Skipped on submitted/approved DRs — those are locked.
    if grid_task_ids and dr.get("status") == "draft":
        # Map type → expected constraint_type so the auto-fill picks the
        # right vocabulary value.
        TYPE_TO_CONSTRAINT = {
            "rfi": "awaiting_rfi_response",
            "submittal": "awaiting_submittal_return",
            "field_issue": "awaiting_predecessor_completion",
            "bulletin": "awaiting_bulletin_approval",
            "coordination_request": "awaiting_predecessor_completion",
        }
        OPEN_STATES = ["draft", "submitted", "open", "under_review", "awaiting_clarification"]
        # Single $in across the union of task IDs.
        gates = await db.gating_entities.find(
            tenant_filter(ctx["tenant_id"], {
                "gates_task_ids": {"$in": grid_task_ids},
                "status": {"$in": OPEN_STATES},
            }),
            {"_id": 0, "id": 1, "entity_type": 1, "gates_task_ids": 1, "submitted_at": 1},
        ).sort("submitted_at", -1).to_list(500)
        # Build {task_id → first (most-recent) open gate}.
        first_gate: dict[str, dict] = {}
        for g in gates:
            for tid in (g.get("gates_task_ids") or []):
                if tid in grid_task_ids and tid not in first_gate:
                    first_gate[tid] = g
        now_iso = utc_now().isoformat()
        for r in grid:
            tid = r.get("schedule_activity_id")
            if not tid or r.get("constraint_type"):
                continue  # already has a constraint (auto-filled or user-set)
            g = first_gate.get(tid)
            if not g:
                continue
            ctype = TYPE_TO_CONSTRAINT.get(g["entity_type"])
            if not ctype:
                continue
            await db.daily_report_activities.update_one(
                {"id": r["id"]},
                {"$set": {
                    "constraint_type": ctype,
                    "constraint_reference_entity_type": g["entity_type"],
                    "constraint_reference_entity_id": g["id"],
                    "constraint_auto_filled": True,
                    "last_sync_at": now_iso,
                }},
            )
            # Reflect in the response so the frontend sees it on this read.
            r["constraint_type"] = ctype
            r["constraint_reference_entity_type"] = g["entity_type"]
            r["constraint_reference_entity_id"] = g["id"]
            r["constraint_auto_filled"] = True
    presence = await db.daily_presence.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("trade", 1).to_list(200)
    photos = await db.photos.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("captured_at", -1).to_list(200)

    voice = await db.voice_annotations.find(
        tenant_filter(ctx["tenant_id"], {"linked_entity_type": "daily_report", "linked_entity_id": daily_report_id}), {"_id": 0},
    ).sort("created_at", -1).to_list(200)

    # Bug 2026-05: panel queries must exclude soft-deleted leftovers
    # (legacy /crud/{entity}/{id} DELETE set status='deleted' before the
    # hard-delete fix). RFIs are still soft-delete by design (FK refs),
    # so the filter stays load-bearing for them.
    not_deleted = {"status": {"$ne": "deleted"}}
    delays = await db.delays.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0}).sort("reported_at", -1).to_list(200)
    safety = await db.safety_events.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0}).sort("reported_at", -1).to_list(200)
    qc = await db.qc_issues.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0}).sort("reported_at", -1).to_list(200)
    rfis = await db.rfis.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0}).sort("date_initiated", -1).to_list(100)
    deliveries = await db.deliveries.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "delivery_date": dr["report_date"]}), {"_id": 0},
    ).sort("delivery_date", -1).to_list(100)

    audit_log = await db.audit_log.find(
        tenant_filter(ctx["tenant_id"], {"$or": [
            {"table_name": "daily_reports", "record_id": daily_report_id},
            {"table_name": "daily_presence", "record_id": {"$in": [p["id"] for p in presence]}} if presence else {"_unused": True},
        ]}), {"_id": 0},
    ).sort("created_at", -1).limit(100).to_list(100)

    # Bucket D Task 2 -- enrich every supplement row with its parent
    # DR's report_date so the panel SupplementBadge can render
    # "SUPP→YYYY-MM-DD" without a per-row fetch. Single $in query
    # across all event collections.
    parent_dr_ids: set[str] = set()
    for coll_rows in (delays, safety, qc, rfis, deliveries, photos, presence, grid):
        for r in coll_rows:
            if r.get("is_supplement") and r.get("parent_daily_report_id"):
                parent_dr_ids.add(r["parent_daily_report_id"])
    parent_dr_dates: dict[str, str] = {}
    if parent_dr_ids:
        parent_drs = await db.daily_reports.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": list(parent_dr_ids)}}),
            {"_id": 0, "id": 1, "report_date": 1},
        ).to_list(length=None)
        parent_dr_dates = {d["id"]: d["report_date"] for d in parent_drs}
    for coll_rows in (delays, safety, qc, rfis, deliveries, photos, presence, grid):
        for r in coll_rows:
            pid = r.get("parent_daily_report_id")
            if pid and pid in parent_dr_dates:
                r["parent_daily_report_date"] = parent_dr_dates[pid]

    user_ids = [dr.get("prepared_by"), dr.get("submitted_by"), dr.get("approved_by")]
    user_labels = await _user_label_map(db, ctx["tenant_id"], user_ids)

    return {
        "report": dr,
        # Feature DR Header Auto-Population (2026-05-20) -- the project
        # sub-dict must also surface the four fields read by
        # DrAutoHeader (client_name, station_id, contract_number,
        # contractor_name). Without these the header rendered as
        # em-dashes regardless of what was set on the project record.
        "project": {"id": proj.get("id"), "name": proj.get("name"),
                    "project_type": proj.get("project_type"),
                    "address_line1": proj.get("address_line1"),
                    "city": proj.get("city"), "state": proj.get("state"),
                    "em_385_required": proj.get("em_385_required"),
                    "client_name": proj.get("client_name"),
                    "station_id": proj.get("station_id"),
                    "contract_number": proj.get("contract_number"),
                    "contractor_name": proj.get("contractor_name")},
        "grid": grid, "presence": presence, "photos": photos,
        "voice_annotations": voice, "delays": delays,
        "safety_events": safety, "qc_issues": qc, "rfis": rfis,
        "deliveries": deliveries, "audit_log": audit_log,
        "user_labels": user_labels,
    }

