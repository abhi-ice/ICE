"""Daily-report router. Master prompt §10: pre-populate from 2WLA at 7am."""
from fastapi import APIRouter, Depends, HTTPException
from datetime import date, datetime, timedelta
from typing import Optional
from database import get_db, tenant_filter
from models import (
    DailyReport, DailyReportCreate, DailyReportActivity, DailyReportActivityUpdate,
    gen_id, utc_now, StrictModel,
)
from auth import require_tenant
from audit import write_audit

router = APIRouter(tags=["daily-reports"])


async def _prepopulate_grid(tenant_id: str, project_id: str, daily_report_id: str, report_date: date):
    """Find activities planned for the day → create grid rows. Pre-fill
    pct_complete_start_of_day from yesterday's end_of_day or task.pct_complete.
    """
    db = get_db()
    start_window = (report_date - timedelta(days=1)).isoformat()
    end_window = (report_date + timedelta(days=1)).isoformat()

    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$nin": ["deleted", "complete"]},
            "$or": [
                {"planned_start": {"$lte": end_window}, "planned_finish": {"$gte": start_window}},
            ],
        }),
        {"_id": 0},
    ).limit(40).to_list(40)

    yesterday = (report_date - timedelta(days=1)).isoformat()
    for pos, t in enumerate(tasks):
        # yesterday's row?
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

        # location name
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
            "position": pos,
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


@router.get("/projects/{project_id}/daily-reports", response_model=list[DailyReport])
async def list_daily_reports(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.daily_reports.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("report_date", -1).to_list(200)
    return [DailyReport(**d) for d in docs]


@router.get("/daily-reports/{daily_report_id}", response_model=DailyReport)
async def get_daily_report(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Daily report not found")
    return DailyReport(**doc)


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
    # tasks planned on this date (planned_start <= date <= planned_finish)
    candidates = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "status": {"$nin": ["deleted", "complete"]},
            "planned_start": {"$lte": report_date},
            "planned_finish": {"$gte": report_date},
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


@router.post("/daily-reports/{daily_report_id}/grid", response_model=DailyReportActivity)
async def add_grid_row(daily_report_id: str, payload: AddActivityRow, ctx: dict = Depends(require_tenant)):
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
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
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    before = await db.daily_report_activities.find_one({"id": row_id}, {"_id": 0})
    if not before:
        raise HTTPException(404, "Row not found")
    # tenant guard via parent daily report
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": before["daily_report_id"]}), {"_id": 0, "id": 1})
    if not dr:
        raise HTTPException(404, "Row not found")
    await db.daily_report_activities.update_one({"id": row_id}, {"$set": updates})
    after = await db.daily_report_activities.find_one({"id": row_id}, {"_id": 0})

    # Cascade pct_complete back to the underlying task
    if "pct_complete_end_of_day" in updates:
        new_pct = float(updates["pct_complete_end_of_day"])
        await db.tasks.update_one(
            tenant_filter(ctx["tenant_id"], {"id": before["schedule_activity_id"]}),
            {"$set": {
                "prev_pct_complete": before.get("pct_complete_end_of_day", 0),
                "pct_complete": new_pct,
                "status": "complete" if new_pct >= 100 else "in_progress",
            }},
        )
        # productivity observation (master prompt §16)
        if updates.get("hours_worked") or after.get("hours_worked"):
            t = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": before["schedule_activity_id"]}), {"_id": 0})
            await db.productivity_observations.insert_one({
                "id": gen_id(),
                "tenant_id": ctx["tenant_id"],
                "project_id": dr.get("project_id") or t.get("project_id"),
                "schedule_activity_id": before["schedule_activity_id"],
                "trade": t.get("trade_code") if t else None,
                "location_id": t.get("location_id") if t else None,
                "crew_id": after.get("crew_id"),
                "date": utc_now().date().isoformat(),
                "hours_worked": float(after.get("hours_worked") or 0),
                "pct_delta": new_pct - before.get("pct_complete_end_of_day", 0),
                "created_at": utc_now().isoformat(),
            })
    return DailyReportActivity(**after)


class SubmitPayload(StrictModel):
    notes: Optional[str] = None


@router.post("/daily-reports/{daily_report_id}/submit", response_model=DailyReport)
async def submit_daily_report(daily_report_id: str, payload: SubmitPayload, ctx: dict = Depends(require_tenant)):
    db = get_db()
    before = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Daily report not found")
    updates = {
        "status": "submitted",
        "submitted_by": ctx["user"]["id"],
        "submitted_at": utc_now().isoformat(),
    }
    if payload.notes:
        updates["notes"] = payload.notes
    await db.daily_reports.update_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"$set": updates}
    )
    after = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="daily_reports", op="UPDATE",
        record_id=daily_report_id, before=before, after=after,
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
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    rows = await db.daily_report_activities.find({"daily_report_id": daily_report_id}, {"_id": 0}).sort("position", 1).to_list(200)
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": dr["project_id"]}), {"_id": 0, "name": 1})

    lines = [
        f"DAILY REPORT — {proj['name'] if proj else dr['project_id']}",
        f"Date: {dr['report_date']}",
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
