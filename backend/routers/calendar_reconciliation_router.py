"""Bucket C3c Phase 3 — Working-day vs calendar-day reconciliation.

Reporting layer over project_calendar_days (created in C3a) cross-joined
with daily_report aggregates (weather impact, production hours,
attendance) so a TIA can cite both clocks side-by-side.

  GET /api/projects/{pid}/calendar-reconciliation?date_range_start=&date_range_end=
  GET /api/projects/{pid}/calendar-reconciliation/csv?date_range_start=&date_range_end=

Tenant-scoped. Read-only -- this is a derived view, never mutates state.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from datetime import date, timedelta
from typing import Optional
import csv
import io

from database import get_db, tenant_filter
from auth import require_tenant


router = APIRouter(tags=["calendar-reconciliation"])


def _parse_date(s: str, label: str) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        raise HTTPException(400, f"Invalid {label} (expected ISO yyyy-mm-dd)")


async def _build_rows(tenant_id: str, project_id: str,
                       start: date, end: date) -> list[dict]:
    """Per-date aggregation: pull project_calendar_days for the range,
    sum daily_report_activities aggregates per report_date, then merge.
    Dates in range that have neither calendar row nor DR fall back to a
    Mon-Fri working-day default (mirrors the cascade engine's working
    day fallback in services.gating_entities)."""
    db = get_db()

    # Calendar rows
    cal_docs = await db.project_calendar_days.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "calendar_date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "calendar_date": 1, "is_working_day": 1, "is_holiday": 1,
         "is_building_closed": 1, "is_restricted_access": 1, "notes": 1},
    ).to_list(2000)
    cal_by_date: dict[str, dict] = {}
    for d in cal_docs:
        key = str(d.get("calendar_date") or "")[:10]
        if key:
            cal_by_date[key] = d

    # Daily reports in range
    # Addition 3 -- include no-work fields so each row in TIA Section 11
    # can render the explicit no-work-day acknowledgment instead of a
    # bare blank.
    drs = await db.daily_reports.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "report_date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "id": 1, "report_date": 1, "status": 1,
         "weather_conditions": 1, "is_no_work_day": 1,
         "no_work_reason": 1, "no_work_note": 1},
    ).to_list(5000)
    dr_by_date: dict[str, dict] = {}
    dr_ids: list[str] = []
    for r in drs:
        key = str(r.get("report_date") or "")[:10]
        if key:
            dr_by_date[key] = r
            dr_ids.append(r["id"])

    # Sum activity-level metrics per date.
    agg_by_dr: dict[str, dict] = {}
    if dr_ids:
        rows = await db.daily_report_activities.find(
            {"daily_report_id": {"$in": dr_ids}},
            {"_id": 0, "daily_report_id": 1, "hours_worked": 1,
             "weather_impact_minutes": 1, "delay_minutes": 1, "crew_count": 1},
        ).to_list(50000)
        for a in rows:
            d = agg_by_dr.setdefault(a["daily_report_id"], {
                "production_hours": 0.0, "weather_impact_hours": 0.0,
                "delay_hours": 0.0, "headcount_total": 0,
            })
            d["production_hours"] += float(a.get("hours_worked") or 0)
            d["weather_impact_hours"] += float(a.get("weather_impact_minutes") or 0) / 60.0
            d["delay_hours"] += float(a.get("delay_minutes") or 0) / 60.0
            d["headcount_total"] += int(a.get("crew_count") or 0)

    # Walk the date range and emit one row per date.
    out: list[dict] = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        cal = cal_by_date.get(key)
        is_working = (cal["is_working_day"] if cal else (cur.weekday() < 5))
        dr = dr_by_date.get(key)
        agg = agg_by_dr.get(dr["id"]) if dr else None
        row = {
            "date": key,
            "is_working_day": bool(is_working),
            "is_holiday": bool(cal.get("is_holiday")) if cal else False,
            "is_building_closed": bool(cal.get("is_building_closed")) if cal else False,
            "is_restricted_access": bool(cal.get("is_restricted_access")) if cal else False,
            "notes": (cal.get("notes") if cal else None),
            "weather_conditions": (dr.get("weather_conditions") if dr else None),
            "weather_impact_hours": round(agg["weather_impact_hours"], 2) if agg else 0.0,
            "production_hours": round(agg["production_hours"], 2) if agg else 0.0,
            "delay_hours": round(agg["delay_hours"], 2) if agg else 0.0,
            "headcount_total": int(agg["headcount_total"]) if agg else 0,
            "daily_report_id": dr["id"] if dr else None,
            "daily_report_status": dr.get("status") if dr else None,
            # Addition 3 -- explicit no-work-day acknowledgment surfaces
            # in TIA Section 11. Rendered ONLY for submitted DRs since
            # the field is set at submit time and unset on draft DRs.
            "is_no_work_day": bool(dr.get("is_no_work_day")) if dr else False,
            "no_work_reason": (dr.get("no_work_reason") if dr else None),
            "no_work_note": (dr.get("no_work_note") if dr else None),
        }
        out.append(row)
        cur += timedelta(days=1)
    return out


@router.get("/projects/{project_id}/calendar-reconciliation")
async def calendar_reconciliation(
    project_id: str,
    date_range_start: str = Query(...),
    date_range_end: str = Query(...),
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1, "name": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    start = _parse_date(date_range_start, "date_range_start")
    end = _parse_date(date_range_end, "date_range_end")
    if end < start:
        raise HTTPException(400, "date_range_end must be >= date_range_start")
    if (end - start).days > 730:
        raise HTTPException(400, "Range too large -- cap is 2 years")

    rows = await _build_rows(ctx["tenant_id"], project_id, start, end)

    # Summary aggregates
    working_days = sum(1 for r in rows if r["is_working_day"])
    calendar_days = len(rows)
    total_prod_hours = round(sum(r["production_hours"] for r in rows), 2)
    total_weather_hours = round(sum(r["weather_impact_hours"] for r in rows), 2)
    total_delay_hours = round(sum(r["delay_hours"] for r in rows), 2)

    return {
        "project_id": project_id,
        "project_name": proj.get("name"),
        "date_range_start": start.isoformat(),
        "date_range_end": end.isoformat(),
        "calendar_days": calendar_days,
        "working_days": working_days,
        "total_production_hours": total_prod_hours,
        "total_weather_impact_hours": total_weather_hours,
        "total_delay_hours": total_delay_hours,
        "rows": rows,
    }


@router.get("/projects/{project_id}/calendar-reconciliation/csv")
async def calendar_reconciliation_csv(
    project_id: str,
    date_range_start: str = Query(...),
    date_range_end: str = Query(...),
    ctx: dict = Depends(require_tenant),
):
    """Same data as /calendar-reconciliation but as a CSV download.
    Suitable as a TIA exhibit or for hand-off to a scheduling engineer."""
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "id": 1, "name": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    start = _parse_date(date_range_start, "date_range_start")
    end = _parse_date(date_range_end, "date_range_end")
    if end < start:
        raise HTTPException(400, "date_range_end must be >= date_range_start")
    if (end - start).days > 730:
        raise HTTPException(400, "Range too large -- cap is 2 years")

    rows = await _build_rows(ctx["tenant_id"], project_id, start, end)

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "date", "is_working_day", "is_holiday", "is_building_closed",
        "is_restricted_access", "weather_conditions", "weather_impact_hours",
        "delay_hours", "production_hours", "headcount_total", "notes",
        "daily_report_status",
    ])
    for r in rows:
        w.writerow([
            r["date"],
            "Y" if r["is_working_day"] else "N",
            "Y" if r["is_holiday"] else "",
            "Y" if r["is_building_closed"] else "",
            "Y" if r["is_restricted_access"] else "",
            r.get("weather_conditions") or "",
            r["weather_impact_hours"],
            r["delay_hours"],
            r["production_hours"],
            r["headcount_total"],
            r.get("notes") or "",
            r.get("daily_report_status") or "",
        ])
    fname = f"calendar_reconciliation_{project_id[:8]}_{start.isoformat()}_{end.isoformat()}.csv"
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
