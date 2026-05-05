"""Daily-report router. Master prompt §10: pre-populate from 2WLA at 7am."""
import re
import requests
from fastapi import APIRouter, Depends, HTTPException
from datetime import date, datetime, timedelta
from typing import Optional
from database import get_db, tenant_filter
from models import (
    DailyReport, DailyReportCreate, DailyReportUpdate,
    DailyReportActivity, DailyReportActivityUpdate,
    DailyPresence, DailyPresenceCreate, DailyPresenceUpdate,
    UnplannedActivityCreate,
    gen_id, utc_now, StrictModel,
)
from auth import require_tenant
from audit import write_audit

router = APIRouter(tags=["daily-reports"])


async def _prepopulate_grid(tenant_id: str, project_id: str, daily_report_id: str, report_date: date):
    """Find activities planned for the day → create grid rows. A task is
    'planned for this day' iff `planned_start <= report_date <= planned_finish`.
    Pre-fill pct_complete_start_of_day from yesterday's end_of_day or
    task.pct_complete.
    """
    db = get_db()
    rd_iso = report_date.isoformat()
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$nin": ["deleted", "complete"]},
            "planned_start": {"$lte": rd_iso},
            "planned_finish": {"$gte": rd_iso},
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
            r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params=params, timeout=8)
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
            timeout=10,
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
    report_date, save into the DR doc. Idempotent — re-runs overwrite."""
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
    coords = _geocode(proj.get("city"), proj.get("state"), proj.get("postal_code"))
    if not coords:
        addr = ", ".join([p for p in addr_parts if p]).strip(", ")
        raise HTTPException(422, f"Could not geocode project address ({addr or 'no city/state on project'})")
    weather = _fetch_weather(coords[0], coords[1], dr["report_date"])
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
        "created_by": ctx["user"]["id"], "created_at": utc_now().isoformat(),
    })
    await db.daily_presence.insert_one(doc.copy())
    return DailyPresence(**doc)


@router.patch("/daily-presence/{presence_id}", response_model=DailyPresence)
async def patch_presence(presence_id: str, payload: DailyPresenceUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.daily_presence.find_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Presence row not found")
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": doc["daily_report_id"]}), {"_id": 0, "status": 1})
    if dr and dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if updates:
        await db.daily_presence.update_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"$set": updates})
    after = await db.daily_presence.find_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"_id": 0})
    return DailyPresence(**after)


@router.delete("/daily-presence/{presence_id}")
async def delete_presence(presence_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.daily_presence.find_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}), {"_id": 0, "daily_report_id": 1})
    if not doc:
        raise HTTPException(404, "Presence row not found")
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": doc["daily_report_id"]}), {"_id": 0, "status": 1})
    if dr and dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")
    await db.daily_presence.delete_one(tenant_filter(ctx["tenant_id"], {"id": presence_id}))
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
    if dr.get("status") == "submitted":
        raise HTTPException(409, "Daily report locked")

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
    presence = await db.daily_presence.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("trade", 1).to_list(200)
    photos = await db.photos.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("captured_at", -1).to_list(200)

    voice = await db.voice_annotations.find(
        tenant_filter(ctx["tenant_id"], {"linked_entity_type": "daily_report", "linked_entity_id": daily_report_id}), {"_id": 0},
    ).sort("created_at", -1).to_list(200)

    delays = await db.delays.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("reported_at", -1).to_list(200)
    safety = await db.safety_events.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("reported_at", -1).to_list(200)
    qc = await db.qc_issues.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("reported_at", -1).to_list(200)
    rfis = await db.rfis.find(tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0}).sort("date_initiated", -1).to_list(100)
    deliveries = await db.deliveries.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "delivery_date": dr["report_date"]}), {"_id": 0},
    ).sort("delivery_date", -1).to_list(100)

    audit_log = await db.audit_log.find(
        tenant_filter(ctx["tenant_id"], {"$or": [
            {"table_name": "daily_reports", "record_id": daily_report_id},
            {"table_name": "daily_presence", "record_id": {"$in": [p["id"] for p in presence]}} if presence else {"_unused": True},
        ]}), {"_id": 0},
    ).sort("created_at", -1).limit(100).to_list(100)

    user_ids = [dr.get("prepared_by"), dr.get("submitted_by"), dr.get("approved_by")]
    user_labels = await _user_label_map(db, ctx["tenant_id"], user_ids)

    return {
        "report": dr,
        "project": {"id": proj.get("id"), "name": proj.get("name"),
                    "project_type": proj.get("project_type"),
                    "address_line1": proj.get("address_line1"),
                    "city": proj.get("city"), "state": proj.get("state"),
                    "em_385_required": proj.get("em_385_required")},
        "grid": grid, "presence": presence, "photos": photos,
        "voice_annotations": voice, "delays": delays,
        "safety_events": safety, "qc_issues": qc, "rfis": rfis,
        "deliveries": deliveries, "audit_log": audit_log,
        "user_labels": user_labels,
    }

