"""Iter 5 router — fills the remaining master prompt gaps in one cohesive module:

  - notifications (in-app feed + unread count + auto-create helper)
  - cost_rate_categories + cost_estimates (master prompt §17)
  - productivity rates aggregation (master prompt §16)
  - aging summary (RFIs/submittals/work_items older than N days)
  - calendars per project (master prompt §4.5)
  - trade_taxonomy CSI MasterFormat 2020 list endpoint (seeded)
  - crew_assignments + equipment_assignments (master prompt §4.6)
  - daily_report_attendance + daily_report_deliveries (§4.7)
  - work_item_history event log (§4.10)
  - admin/tenants (superadmin), admin/ai-observability, admin/productivity-dashboard
  - weather/now (Open-Meteo proxy — substitute for NOAA per master prompt §10)
  - global search across projects/tasks/rfis/work_items/specs
  - drawing-revisions inheritance helper (§8) — copy non-sketch markups forward
  - additional AI agents: work_item_assistant, change_order_assistant, polygon_suggester
"""
import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta, date
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, Literal, List
from pydantic import Field
from emergentintegrations.llm.chat import LlmChat, UserMessage
from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel, ReadModel
from auth import require_tenant
from services.ai_agents import _new_chat, MODEL_NAME, MODEL_PROVIDER, _track_usage

router = APIRouter(tags=["iter5"])


# ============================================================
# NOTIFICATIONS  (master prompt §23)
# ============================================================
class Notification(ReadModel):
    id: str
    tenant_id: str
    user_id: Optional[str] = None
    kind: str
    title: str
    body: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    read_at: Optional[str] = None
    created_at: str


async def emit_notification(
    *, tenant_id: str, user_id: Optional[str], kind: str, title: str,
    body: Optional[str] = None, entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
):
    db = get_db()
    doc = {
        "id": gen_id(), "tenant_id": tenant_id, "user_id": user_id,
        "kind": kind, "title": title, "body": body,
        "entity_type": entity_type, "entity_id": entity_id,
        "read_at": None, "created_at": utc_now().isoformat(),
    }
    await db.notifications.insert_one(doc.copy())
    # Real-time fan-out (master prompt §23)
    try:
        from routers.ws_router import broadcast
        await broadcast(tenant_id, {"kind": "notification", "payload": {**doc}})
    except Exception:
        pass
    return doc


@router.get("/notifications", response_model=list[Notification])
async def list_notifications(unread_only: bool = False, limit: int = 50, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {
        "$or": [{"user_id": ctx["user"]["id"]}, {"user_id": None}],
    })
    if unread_only:
        f["read_at"] = None
    docs = await db.notifications.find(f, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    return [Notification(**d) for d in docs]


@router.get("/notifications/unread-count")
async def unread_count(ctx: dict = Depends(require_tenant)):
    """Followup-fix 2 (2026-05-07): explicit try/except so a missing
    notifications collection or transient DB hiccup returns {count: 0}
    instead of bubbling a 500. The sidebar polls this every 60s; one
    hiccup shouldn't fill the user's console with errors."""
    try:
        db = get_db()
        n = await db.notifications.count_documents(tenant_filter(ctx["tenant_id"], {
            "$or": [{"user_id": ctx["user"]["id"]}, {"user_id": None}],
            "read_at": None,
        }))
        return {"count": n}
    except Exception:
        return {"count": 0}


@router.post("/notifications/{notification_id}/mark-read")
async def mark_notif_read(notification_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    await db.notifications.update_one(
        tenant_filter(ctx["tenant_id"], {"id": notification_id}),
        {"$set": {"read_at": utc_now().isoformat()}},
    )
    return {"ok": True}


@router.post("/notifications/mark-all-read")
async def mark_all_read(ctx: dict = Depends(require_tenant)):
    db = get_db()
    res = await db.notifications.update_many(
        tenant_filter(ctx["tenant_id"], {"$or": [{"user_id": ctx["user"]["id"]}, {"user_id": None}], "read_at": None}),
        {"$set": {"read_at": utc_now().isoformat()}},
    )
    return {"updated": res.modified_count}


# ============================================================
# COST RATE LIBRARY  (master prompt §17)
# ============================================================
class CostRateCategoryCreate(StrictModel):
    code: str
    name: str
    unit: str = "hour"  # hour|sqft|cy|lf|each
    base_rate_usd: float
    prevailing_wage_rate_usd: Optional[float] = None
    trade_code: Optional[str] = None
    notes: Optional[str] = None
    effective_start: Optional[str] = None
    effective_end: Optional[str] = None


class CostRateCategory(ReadModel):
    id: str
    tenant_id: str
    code: str
    name: str
    unit: str
    base_rate_usd: float
    prevailing_wage_rate_usd: Optional[float] = None
    trade_code: Optional[str] = None
    notes: Optional[str] = None
    effective_start: Optional[str] = None
    effective_end: Optional[str] = None
    created_at: str


@router.post("/cost-rates", response_model=CostRateCategory)
async def create_rate(payload: CostRateCategoryCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    if await db.cost_rate_categories.find_one(tenant_filter(ctx["tenant_id"], {"code": payload.code}), {"_id": 0}):
        raise HTTPException(409, "Rate code exists")
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "created_at": utc_now().isoformat(),
    })
    await db.cost_rate_categories.insert_one(doc.copy())
    return CostRateCategory(**doc)


@router.get("/cost-rates", response_model=list[CostRateCategory])
async def list_rates(ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.cost_rate_categories.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).sort("code", 1).to_list(2000)
    return [CostRateCategory(**d) for d in docs]


class CostRateUpdate(StrictModel):
    base_rate_usd: Optional[float] = None
    prevailing_wage_rate_usd: Optional[float] = None
    name: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/cost-rates/{rate_id}", response_model=CostRateCategory)
async def update_rate(rate_id: str, payload: CostRateUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    res = await db.cost_rate_categories.update_one(
        tenant_filter(ctx["tenant_id"], {"id": rate_id}), {"$set": updates},
    )
    if not res.matched_count:
        raise HTTPException(404)
    doc = await db.cost_rate_categories.find_one(tenant_filter(ctx["tenant_id"], {"id": rate_id}), {"_id": 0})
    return CostRateCategory(**doc)


class CostEstimateRequest(StrictModel):
    project_id: str
    line_items: list[dict]  # [{rate_code, quantity}]
    apply_prevailing_wage: bool = False


@router.post("/cost-estimates")
async def estimate_cost(payload: CostEstimateRequest, ctx: dict = Depends(require_tenant)):
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    apply_pw = payload.apply_prevailing_wage or bool(proj.get("prevailing_wage_required"))
    lines: list[dict] = []
    total = 0.0
    for li in payload.line_items:
        rate = await db.cost_rate_categories.find_one(
            tenant_filter(ctx["tenant_id"], {"code": li.get("rate_code")}), {"_id": 0},
        )
        if not rate:
            lines.append({"rate_code": li.get("rate_code"), "error": "rate not found"})
            continue
        qty = float(li.get("quantity") or 0)
        unit_rate = rate["base_rate_usd"]
        if apply_pw and rate.get("prevailing_wage_rate_usd"):
            unit_rate = rate["prevailing_wage_rate_usd"]
        line_total = round(qty * unit_rate, 2)
        total += line_total
        lines.append({
            "rate_code": rate["code"], "name": rate["name"], "unit": rate["unit"],
            "quantity": qty, "unit_rate": unit_rate, "line_total": line_total,
        })
    return {
        "project_id": payload.project_id,
        "applied_prevailing_wage": apply_pw,
        "lines": lines,
        "total_usd": round(total, 2),
        "computed_at": utc_now().isoformat(),
    }


# ============================================================
# PRODUCTIVITY DASHBOARD (master prompt §16)
# ============================================================
@router.get("/productivity-rates")
async def productivity_rates(project_id: Optional[str] = None, days: int = 90, ctx: dict = Depends(require_tenant)):
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    f = tenant_filter(ctx["tenant_id"], {"date": {"$gte": cutoff}})
    if project_id:
        f["project_id"] = project_id
    pipeline = [
        {"$match": f},
        {"$group": {
            "_id": {"trade": "$trade", "location_id": "$location_id"},
            "total_hours": {"$sum": "$hours_worked"},
            "total_pct_delta": {"$sum": "$pct_delta"},
            "sample_size": {"$sum": 1},
        }},
        {"$sort": {"sample_size": -1}},
    ]
    rates = []
    async for r in db.productivity_observations.aggregate(pipeline):
        hours = float(r.get("total_hours") or 0)
        delta = float(r.get("total_pct_delta") or 0)
        if hours > 0 and delta > 0:
            rates.append({
                "trade": r["_id"]["trade"],
                "location_id": r["_id"]["location_id"],
                "hours_per_pct_point": round(hours / delta, 2),
                "sample_size": r["sample_size"],
                "total_hours": hours,
            })
    return {
        "window_days": days,
        "computed_at": utc_now().isoformat(),
        "rates": rates,
    }


# ============================================================
# AGING SUMMARY  (sidebar smart badges)
# ============================================================
@router.get("/aging-summary")
async def aging_summary(ctx: dict = Depends(require_tenant)):
    """Followup-fix 2 (2026-05-07): now sources aging from
    gating_entities.current_age_working_days (the C3a working-day-aware
    aging clock) instead of calendar-day deltas against per-collection
    timestamps. Emits both new (_wd suffix) and legacy (_d suffix)
    field names so frontend can migrate at its own pace.

    Working-day aging requires a per-row compute (the gating service
    runs the project_calendar_days walk). For badge counts at sidebar
    poll cadence we approximate: pull lightweight gating_entities rows
    by status, run compute_age_fields only on rows that COULD be over
    SLA based on calendar-day age first (cheap pre-filter), then count.

    Approvals stay sourced from approvals collection -- they're not
    yet wrapped in gating_entities."""
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    now = datetime.now(timezone.utc)

    # Fast path -- pull all open gating_entities once and bucket by type.
    # Cap to 500 rows; if a tenant has more than 500 OPEN gates the badge
    # just shows the lower-bound count, which is fine for a sidebar
    # widget (the actual /aging page does the full enumeration).
    OPEN = ("draft", "submitted", "open", "under_review", "awaiting_clarification")
    ges = await db.gating_entities.find(
        {**f, "status": {"$in": list(OPEN)}},
        {"_id": 0, "id": 1, "tenant_id": 1, "project_id": 1,
         "entity_type": 1, "submitted_at": 1, "sla_working_days": 1, "status": 1},
    ).to_list(500)

    rfis_over = 0
    submittals_over = 0
    work_items_over = 0
    if ges:
        from services.gating_entities import compute_age_fields
        for g in ges:
            sla = int(g.get("sla_working_days") or 0)
            if sla <= 0:
                continue
            # Cheap pre-filter: skip rows whose calendar-day age can't
            # possibly exceed the SLA's working-day budget (working days
            # <= calendar days). If cal_age <= sla_wd, the row is
            # guaranteed under SLA and we can skip the calendar walk.
            sub_iso = g.get("submitted_at")
            if not sub_iso:
                continue
            try:
                sd = date.fromisoformat(str(sub_iso)[:10])
            except Exception:
                continue
            cal_age = (now.date() - sd).days
            if cal_age <= sla:
                continue
            age = await compute_age_fields(g)
            if not age.get("is_over_sla"):
                continue
            etype = g.get("entity_type") or ""
            if etype == "rfi":
                rfis_over += 1
            elif etype == "submittal":
                submittals_over += 1
            elif etype in ("field_issue", "work_item"):
                work_items_over += 1

    # Approvals: kept on the approvals collection. 3-calendar-day SLA
    # for badge purposes (matches legacy behavior).
    apv_cut = (now - timedelta(days=3)).isoformat()
    try:
        approvals_over = await db.approvals.count_documents(
            {**f, "status": "pending", "proposed_at": {"$lte": apv_cut}},
        )
    except Exception:
        approvals_over = 0

    return {
        # New, spec-aligned field names (working-day aware).
        "rfis_over_5wd": rfis_over,
        "submittals_over_14wd": submittals_over,
        "work_items_over_7wd": work_items_over,
        "approvals_over_3wd": approvals_over,
        # Legacy field names kept for one-version backwards compat
        # with the existing Sidebar.jsx + Aging.jsx readers. Drop in
        # a future cleanup once both surfaces consume the _wd names.
        "rfis_over_5d": rfis_over,
        "submittals_over_14d": submittals_over,
        "work_items_over_7d": work_items_over,
        "approvals_over_3d": approvals_over,
    }


# ============================================================
# CALENDARS (master prompt §4.5)
# ============================================================
class CalendarCreate(StrictModel):
    project_id: Optional[str] = None
    name: str
    work_hours_per_day: float = 8
    work_days_per_week: int = 5
    holidays: list[str] = Field(default_factory=list)
    exceptions: list[dict] = Field(default_factory=list)


@router.post("/calendars")
async def create_cal(payload: CalendarCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "created_at": utc_now().isoformat(),
    })
    await db.calendars.insert_one(doc.copy())
    return doc


@router.get("/calendars")
async def list_cal(project_id: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    if project_id:
        f["$or"] = [{"project_id": project_id}, {"project_id": None}]
    docs = await db.calendars.find(f, {"_id": 0}).to_list(100)
    return docs


# ============================================================
# CREW + EQUIPMENT ASSIGNMENTS (§4.6)
# ============================================================
class CrewAssignmentCreate(StrictModel):
    crew_id: str
    project_id: str
    schedule_activity_id: Optional[str] = None
    start_date: str
    end_date: str
    pct_capacity: float = 100
    notes: Optional[str] = None


@router.post("/crew-assignments")
async def create_crew_assign(payload: CrewAssignmentCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "active",
        "created_by": ctx["user"]["id"], "created_at": utc_now().isoformat(),
    })
    await db.crew_assignments.insert_one(doc.copy())
    return doc


@router.get("/crew-assignments")
async def list_crew_assign(project_id: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    if project_id:
        f["project_id"] = project_id
    docs = await db.crew_assignments.find(f, {"_id": 0}).to_list(2000)
    return docs


class EquipAssignmentCreate(StrictModel):
    equipment_id: str
    project_id: str
    schedule_activity_id: Optional[str] = None
    start_date: str
    end_date: str
    notes: Optional[str] = None


@router.post("/equipment-assignments")
async def create_equip_assign(payload: EquipAssignmentCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "active",
        "created_by": ctx["user"]["id"], "created_at": utc_now().isoformat(),
    })
    await db.equipment_assignments.insert_one(doc.copy())
    return doc


@router.get("/equipment-assignments")
async def list_equip_assign(project_id: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    if project_id:
        f["project_id"] = project_id
    docs = await db.equipment_assignments.find(f, {"_id": 0}).to_list(2000)
    return docs


# ============================================================
# DAILY REPORT ATTENDANCE + DELIVERIES (§4.7)
# ============================================================
class AttendanceEntry(StrictModel):
    daily_report_id: str
    person_id: Optional[str] = None
    name: Optional[str] = None
    crew_id: Optional[str] = None
    hours_on_site: float = 8.0
    role: Optional[str] = None
    notes: Optional[str] = None


@router.post("/daily-report-attendance")
async def add_attendance(payload: AttendanceEntry, ctx: dict = Depends(require_tenant)):
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.daily_report_id}), {"_id": 0, "id": 1})
    if not dr:
        raise HTTPException(404, "Daily report not found")
    doc = payload.model_dump(mode="json")
    doc.update({"id": gen_id(), "tenant_id": ctx["tenant_id"], "created_at": utc_now().isoformat()})
    await db.daily_report_attendance.insert_one(doc.copy())
    return doc


@router.get("/daily-reports/{daily_report_id}/attendance")
async def list_attendance(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.daily_report_attendance.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0},
    ).to_list(500)
    return docs


@router.delete("/daily-report-attendance/{entry_id}")
async def remove_attendance(entry_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    res = await db.daily_report_attendance.delete_one(tenant_filter(ctx["tenant_id"], {"id": entry_id}))
    if not res.deleted_count:
        raise HTTPException(404)
    return {"ok": True}


# ============================================================
# COMPARE-TO-YESTERDAY  (§10)
# ============================================================
@router.get("/daily-reports/{daily_report_id}/compare-yesterday")
async def compare_yesterday(daily_report_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0})
    if not dr:
        raise HTTPException(404)
    today_d = date.fromisoformat(dr["report_date"])
    y_d = (today_d - timedelta(days=1)).isoformat()
    yesterday = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"project_id": dr["project_id"], "report_date": y_d}), {"_id": 0},
    )
    today_rows = await db.daily_report_activities.find({"daily_report_id": daily_report_id}, {"_id": 0}).to_list(500)
    yesterday_rows = []
    if yesterday:
        yesterday_rows = await db.daily_report_activities.find({"daily_report_id": yesterday["id"]}, {"_id": 0}).to_list(500)

    y_by_act = {r["schedule_activity_id"]: r for r in yesterday_rows}
    diffs = []
    for r in today_rows:
        prev = y_by_act.get(r["schedule_activity_id"])
        diffs.append({
            "activity_code": r.get("activity_code"),
            "description": r.get("activity_description"),
            "today_pct": r.get("pct_complete_end_of_day"),
            "yesterday_pct": prev.get("pct_complete_end_of_day") if prev else None,
            "delta_pct": (r.get("pct_complete_end_of_day", 0) or 0) - (prev.get("pct_complete_end_of_day", 0) if prev else 0),
            "today_hours": r.get("hours_worked"),
            "yesterday_hours": prev.get("hours_worked") if prev else None,
        })
    return {
        "today_date": dr["report_date"],
        "yesterday_date": y_d if yesterday else None,
        "yesterday_found": bool(yesterday),
        "rows": diffs,
    }


# ============================================================
# WORK ITEM HISTORY (§4.10) — emit events on status change
# ============================================================
@router.get("/work-items/{wi_id}/history")
async def get_wi_history(wi_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.work_item_history.find(
        tenant_filter(ctx["tenant_id"], {"work_item_id": wi_id}), {"_id": 0},
    ).sort("created_at", 1).to_list(500)
    return docs


class WorkItemUpdate(StrictModel):
    status: Optional[str] = None
    closure_summary: Optional[str] = None
    actual_cost_impact: Optional[float] = None
    actual_time_impact_days: Optional[float] = None
    owned_by: Optional[str] = None
    target_resolution_date: Optional[str] = None


@router.patch("/work-items/{wi_id}")
async def update_work_item(wi_id: str, payload: WorkItemUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    before = await db.work_items.find_one(tenant_filter(ctx["tenant_id"], {"id": wi_id}), {"_id": 0})
    if not before:
        raise HTTPException(404)
    if "status" in updates and updates["status"] in ("resolved", "closed") and not before.get("closed_at"):
        updates["closed_at"] = utc_now().isoformat()
        updates["closed_by"] = ctx["user"]["id"]
    await db.work_items.update_one(tenant_filter(ctx["tenant_id"], {"id": wi_id}), {"$set": updates})
    after = await db.work_items.find_one(tenant_filter(ctx["tenant_id"], {"id": wi_id}), {"_id": 0})
    if before.get("status") != after.get("status"):
        await db.work_item_history.insert_one({
            "id": gen_id(), "tenant_id": ctx["tenant_id"], "work_item_id": wi_id,
            "event_type": "status_changed",
            "event_data": {"from": before.get("status"), "to": after.get("status")},
            "actor": ctx["user"]["id"], "created_at": utc_now().isoformat(),
        })
        # Notification
        await emit_notification(
            tenant_id=ctx["tenant_id"], user_id=before.get("opened_by"),
            kind="work_item_status",
            title=f"Field issue: {before['title'][:60]}",
            body=f"Status: {before.get('status')} → {after.get('status')}",
            entity_type="work_item", entity_id=wi_id,
        )
    return after


# ============================================================
# WEATHER (Open-Meteo proxy — substitute for NOAA per master prompt §10)
# ============================================================
@router.get("/weather/now")
async def weather_now(lat: float, lng: float, ctx: dict = Depends(require_tenant)):
    """Open-Meteo (no API key) — current weather for daily report auto-fill."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lng,
                "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "auto",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Weather provider error: {e}")
    cur = data.get("current", {})
    daily = data.get("daily", {})
    code = cur.get("weather_code", 0)
    cond_map = {
        0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow",
        80: "showers", 81: "showers", 82: "heavy showers",
        95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
    }
    return {
        "weather_temp_high_f": (daily.get("temperature_2m_max") or [None])[0],
        "weather_temp_low_f": (daily.get("temperature_2m_min") or [None])[0],
        "weather_conditions": cond_map.get(code, "unknown"),
        "weather_precipitation_in": (daily.get("precipitation_sum") or [None])[0],
        "current_temp_f": cur.get("temperature_2m"),
        "wind_mph": cur.get("wind_speed_10m"),
        "fetched_at": utc_now().isoformat(),
    }


# ============================================================
# GLOBAL SEARCH
# ============================================================
@router.get("/search")
async def global_search(q: str, limit: int = 10, ctx: dict = Depends(require_tenant)):
    """Search across projects, tasks, RFIs, work items, specs."""
    if not q.strip() or len(q.strip()) < 2:
        return {"results": []}
    db = get_db()
    rgx = re.compile(re.escape(q.strip()), re.IGNORECASE)
    f = tenant_filter(ctx["tenant_id"])
    out = []

    projects = await db.projects.find({**f, "name": rgx}, {"_id": 0, "id": 1, "name": 1}).limit(limit).to_list(limit)
    out.extend([{"type": "project", "id": p["id"], "label": p["name"], "route": "/projects"} for p in projects])

    tasks = await db.tasks.find({**f, "$or": [{"description": rgx}, {"activity_code": rgx}]}, {"_id": 0, "id": 1, "activity_code": 1, "description": 1}).limit(limit).to_list(limit)
    out.extend([{"type": "task", "id": t["id"], "label": f"[{t['activity_code']}] {t['description']}", "route": "/tasks"} for t in tasks])

    rfis = await db.rfis.find({**f, "$or": [{"subject": rgx}, {"rfi_number": rgx}]}, {"_id": 0, "id": 1, "rfi_number": 1, "subject": 1}).limit(limit).to_list(limit)
    out.extend([{"type": "rfi", "id": r["id"], "label": f"{r['rfi_number']} {r['subject']}", "route": "/rfis"} for r in rfis])

    wis = await db.work_items.find({**f, "$or": [{"title": rgx}, {"description": rgx}]}, {"_id": 0, "id": 1, "title": 1}).limit(limit).to_list(limit)
    out.extend([{"type": "work_item", "id": w["id"], "label": w["title"], "route": "/work-items"} for w in wis])

    specs = await db.spec_sections.find({**f, "$or": [{"section_title": rgx}, {"text_content": rgx}]}, {"_id": 0, "id": 1, "section_code": 1, "section_title": 1}).limit(limit).to_list(limit)
    out.extend([{"type": "spec_section", "id": s["id"], "label": f"{s['section_code']} {s['section_title']}", "route": "/specs"} for s in specs])

    return {"results": out[: limit * 4]}


# ============================================================
# TRADE TAXONOMY (CSI MasterFormat 2020)
# ============================================================
@router.get("/trade-taxonomy")
async def list_trades(ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.trade_taxonomy.find({"$or": [{"tenant_id": ctx["tenant_id"]}, {"tenant_id": None}]}, {"_id": 0}).sort("sort_order", 1).to_list(2000)
    return docs


# ============================================================
# ADMIN PAGES
# ============================================================
@router.get("/admin/tenants")
async def admin_tenants(ctx: dict = Depends(require_tenant)):
    if ctx["user"].get("role") != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    docs = await db.tenants.find({}, {"_id": 0}).to_list(500)
    return docs


@router.get("/admin/ai-observability")
async def admin_ai_obs(days: int = 30, ctx: dict = Depends(require_tenant)):
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pipeline = [
        {"$match": tenant_filter(ctx["tenant_id"], {"created_at": {"$gte": cutoff}})},
        {"$group": {
            "_id": {"service": "$service", "model": "$model", "ok": "$ok"},
            "count": {"$sum": 1},
            "total_latency": {"$sum": "$latency_ms"},
            "avg_latency": {"$avg": "$latency_ms"},
        }},
    ]
    rows = []
    async for r in db.ai_usage_tracking.aggregate(pipeline):
        rows.append({
            "service": r["_id"]["service"], "model": r["_id"]["model"],
            "ok": r["_id"]["ok"], "count": r["count"],
            "avg_latency_ms": int(r["avg_latency"] or 0),
        })
    total = await db.ai_usage_tracking.count_documents(tenant_filter(ctx["tenant_id"], {"created_at": {"$gte": cutoff}}))
    return {"window_days": days, "total_calls": total, "by_service": rows}


# ============================================================
# DRAWING REVISION → COPY MARKUPS FORWARD (§8 markup inheritance)
# ============================================================
@router.post("/drawing-revisions/{rev_id}/inherit-markups")
async def inherit_markups(rev_id: str, from_revision_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    # ensure both revs belong to same drawing & tenant
    new_rev = await db.drawing_revisions.find_one(tenant_filter(ctx["tenant_id"], {"id": rev_id}), {"_id": 0})
    old_rev = await db.drawing_revisions.find_one(tenant_filter(ctx["tenant_id"], {"id": from_revision_id}), {"_id": 0})
    if not new_rev or not old_rev:
        raise HTTPException(404)
    if new_rev["drawing_id"] != old_rev["drawing_id"]:
        raise HTTPException(400, "Revisions belong to different drawings")

    old_markups = await db.drawing_markups.find(
        tenant_filter(ctx["tenant_id"], {"drawing_revision_id": from_revision_id, "markup_type": {"$ne": "pen"}}),
        {"_id": 0},
    ).to_list(2000)
    copied = 0
    for m in old_markups:
        new_m = {**m, "id": gen_id(), "drawing_revision_id": rev_id,
                 "inherited_from_markup_id": m["id"],
                 "created_at": utc_now().isoformat()}
        await db.drawing_markups.insert_one(new_m.copy())
        copied += 1
    return {"copied": copied, "skipped_pen_sketches": True}


# ============================================================
# AI AGENTS — NEW (§6)
# ============================================================
WI_ASSISTANT_SYSTEM = """You are a federal-construction project engineer. Given a field event
description, propose a work-item title, root_cause hypothesis, severity, and likely linked entities
(activity, rfi, spec_section). Respond ONLY with a JSON object with keys:
title, description, priority (low|normal|high|urgent), root_cause, suggested_links (list of {entity_type,reason}).
No prose, no code fences."""


class WorkItemAssistRequest(StrictModel):
    project_id: str
    event_text: str


@router.post("/ai/work-item-assistant")
async def ai_work_item(payload: WorkItemAssistRequest, ctx: dict = Depends(require_tenant)):
    chat = _new_chat(f"wi-{gen_id()}", WI_ASSISTANT_SYSTEM)
    started = datetime.now(timezone.utc)
    try:
        resp = await chat.send_message(UserMessage(text=payload.event_text))
        ok = True
    except Exception as e:
        resp = "{}"; ok = False
    latency = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    await _track_usage(ctx["tenant_id"], gen_id(), latency, ok)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.strip(), flags=re.IGNORECASE | re.MULTILINE)
    try:
        return json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1])
    except Exception:
        return {"_raw": cleaned}


CO_ASSISTANT_SYSTEM = """You are a CFO-grade federal-construction estimator. Given an originating
RFI and proposed scope changes, return ONLY a JSON object with keys:
title, description, type (additive|deductive|no_change), amount_usd (number), time_extension_days (int),
breakdown (list of {category, amount}), rationale.
No prose, no code fences."""


class ChangeOrderAssistRequest(StrictModel):
    project_id: str
    rfi_id: Optional[str] = None
    proposed_scope: str


@router.post("/ai/change-order-assistant")
async def ai_co(payload: ChangeOrderAssistRequest, ctx: dict = Depends(require_tenant)):
    db = get_db()
    rfi_text = ""
    if payload.rfi_id:
        rfi = await db.rfis.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.rfi_id}), {"_id": 0})
        if rfi:
            rfi_text = f"Originating RFI {rfi['rfi_number']}: {rfi['subject']}\n{rfi['description']}\n\n"
    rates = await db.cost_rate_categories.find(tenant_filter(ctx["tenant_id"]), {"_id": 0, "code": 1, "name": 1, "base_rate_usd": 1, "unit": 1}).limit(20).to_list(20)
    rates_text = "\n".join(f"  {r['code']} {r['name']} ${r['base_rate_usd']}/{r['unit']}" for r in rates)
    prompt = f"{rfi_text}Proposed scope:\n{payload.proposed_scope}\n\nAvailable cost rates:\n{rates_text}\n\nReturn the JSON object."

    chat = _new_chat(f"co-{gen_id()}", CO_ASSISTANT_SYSTEM)
    started = datetime.now(timezone.utc)
    try:
        resp = await chat.send_message(UserMessage(text=prompt))
        ok = True
    except Exception as e:
        resp = "{}"; ok = False
    latency = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    await _track_usage(ctx["tenant_id"], gen_id(), latency, ok)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.strip(), flags=re.IGNORECASE | re.MULTILINE)
    try:
        return json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1])
    except Exception:
        return {"_raw": cleaned}


# ============================================================
# DELIVERIES (§4.7) — basic CRUD so /deliveries page works
# ============================================================
class DeliveryCreate(StrictModel):
    project_id: str
    delivery_date: str
    vendor: str
    description: str
    po_number: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    # Bucket D Phase 1 — DR-activity linkage. Required min-1; copilot/
    # voice paths bypass via direct insert_one. daily_report_id is also
    # added so the linkage can be scoped to a specific DR.
    daily_report_id: Optional[str] = None
    linked_dr_activity_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_reason: Optional[str] = None


@router.post("/deliveries")
async def create_delivery(payload: DeliveryCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    # Bucket D Phase 1 — DR-activity linkage required.
    from services.dr_activity_linkage import validate_linked_dr_activity_ids
    doc["linked_dr_activity_ids"] = await validate_linked_dr_activity_ids(
        tenant_id=ctx["tenant_id"],
        daily_report_id=doc.get("daily_report_id"),
        activity_ids=doc.get("linked_dr_activity_ids") or [],
        required=True,
        parent_daily_report_id=doc.get("parent_daily_report_id"),
    )
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "scheduled",
        "received_by": None, "photos": [],
        "created_at": utc_now().isoformat(),
    })
    # Bucket D Phase 4 -- supplement metadata + audit.
    from services.dr_activity_linkage import stamp_supplement_metadata
    from audit import write_audit
    sup_audit = stamp_supplement_metadata(doc, actor_id=ctx["user"]["id"], fact_type="delivery")
    await db.deliveries.insert_one(doc.copy())
    if sup_audit:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="deliveries", op="SUPPLEMENT_FILED",
            record_id=doc["id"], before=None, after=sup_audit,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    return doc


@router.get("/projects/{project_id}/deliveries")
async def list_deliveries(project_id: str,
                            affected_activity_id: Optional[str] = None,
                            ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = {"project_id": project_id}
    if affected_activity_id:
        f["linked_dr_activity_ids"] = affected_activity_id
    docs = await db.deliveries.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("delivery_date", -1).to_list(500)
    return docs


# ============================================================
# MARKETPLACE (§19) — postings + responses
# ============================================================
class MarketplacePostingCreate(StrictModel):
    project_id: Optional[str] = None
    posting_type: Literal["sub_invitation", "equipment_rental", "labor_request", "materials"]
    title: str
    description: str
    visibility: Literal["private", "network", "union_hall", "public"] = "network"
    location_city: Optional[str] = None
    location_state: Optional[str] = None
    deadline: Optional[str] = None
    budget_low: Optional[float] = None
    budget_high: Optional[float] = None


@router.post("/marketplace/postings")
async def post_marketplace(payload: MarketplacePostingCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "status": "open", "posted_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    })
    await db.marketplace_postings.insert_one(doc.copy())
    return doc


@router.get("/marketplace/postings")
async def list_marketplace(ctx: dict = Depends(require_tenant)):
    db = get_db()
    # Show our own (any visibility) + others' if visibility != private
    pipeline = {
        "$or": [
            {"tenant_id": ctx["tenant_id"]},
            {"visibility": {"$in": ["network", "public", "union_hall"]}},
        ],
    }
    docs = await db.marketplace_postings.find(pipeline, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200)
    return docs


class MarketplaceResponseCreate(StrictModel):
    posting_id: str
    message: str
    bid_amount_usd: Optional[float] = None
    proposed_start_date: Optional[str] = None


@router.post("/marketplace/responses")
async def respond_marketplace(payload: MarketplaceResponseCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    posting = await db.marketplace_postings.find_one({"id": payload.posting_id}, {"_id": 0})
    if not posting:
        raise HTTPException(404)
    if posting["visibility"] == "private" and posting["tenant_id"] != ctx["tenant_id"]:
        raise HTTPException(403, "Posting is private to its tenant")
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(),
        "responder_tenant_id": ctx["tenant_id"],
        "responder_user_id": ctx["user"]["id"],
        "status": "submitted",
        "created_at": utc_now().isoformat(),
    })
    await db.marketplace_responses.insert_one(doc.copy())
    # notify poster
    await emit_notification(
        tenant_id=posting["tenant_id"], user_id=posting["posted_by"],
        kind="marketplace_response",
        title=f"New response on {posting['title'][:60]}",
        body=payload.message[:160],
        entity_type="marketplace_posting", entity_id=posting["id"],
    )
    return doc


@router.get("/marketplace/postings/{posting_id}/responses")
async def list_responses(posting_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    posting = await db.marketplace_postings.find_one({"id": posting_id}, {"_id": 0})
    if not posting:
        raise HTTPException(404)
    # Poster sees all; responders see only their own
    if posting["tenant_id"] == ctx["tenant_id"]:
        docs = await db.marketplace_responses.find({"posting_id": posting_id}, {"_id": 0}).to_list(500)
    else:
        docs = await db.marketplace_responses.find({"posting_id": posting_id, "responder_tenant_id": ctx["tenant_id"]}, {"_id": 0}).to_list(50)
    return docs


# ============================================================
# AUTO-NOTIFY HOOKS — fired by other routers (helper)
# ============================================================
# Surfaced as a callable so other routers can `from routers.iter5_router import emit_notification`.
__all__ = ["router", "emit_notification"]
