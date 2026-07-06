"""Bucket C3a Phase 1 — gating entities lifecycle service.

Single source of truth for converting underlying entity records (RFI,
Submittal, WorkItem, etc.) into scheduleable CPM nodes. Every create
endpoint for those entity types calls create_for_entity() and writes
the returned id back to entity.gating_entity_id. Every status-changing
PATCH calls sync_status() so the gating_entities row stays current.

Working-day math uses the project_calendar_days collection. C3a only
populates is_working_day on those rows; C3b will extend with weather /
holiday / closure tracking.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional

from database import get_db, tenant_filter
from services.site_time import project_today
from models import gen_id, utc_now


# Statuses considered "still gating". Mirror of OPEN_STATUSES in
# routers.gating_entities_router; defined here so other services
# (services.cascade_engine, etc.) can import without dragging in
# the FastAPI router module.
OPEN_STATUSES_PROXY: tuple[str, ...] = (
    "draft", "submitted", "open", "under_review", "awaiting_clarification",
)


# SLA defaults (working days). Per-project override lives on the entity
# row when we add a project_settings collection — for C3a, defaults only.
SLA_DEFAULTS: dict[str, int] = {
    "rfi": 10,
    "submittal": 14,
    "bulletin": 30,
    "field_issue": 5,
    "coordination_request": 7,
    "mop": 14,
    "hot_work_permit": 5,
}

# Default attribution / cause per entity type. AI or user can override
# later via PATCH (Phase 2+ surfaces).
DEFAULT_ATTRIBUTION: dict[str, str] = {
    "rfi": "a_e",
    "submittal": "a_e",
    "bulletin": "a_e",
    "field_issue": "gc",
    "coordination_request": "gc",
    "mop": "gc",
    "hot_work_permit": "gc",
}
DEFAULT_CAUSE: dict[str, str] = {
    "rfi": "design_clarification",
    "submittal": "procurement",
    "bulletin": "design_change",
    "field_issue": "other",
    "coordination_request": "building_coordination",
    "mop": "regulatory",
    "hot_work_permit": "regulatory",
}


# ---------- Status mapping ----------

def map_underlying_status_to_gating(entity_type: str, underlying_status: Optional[str]) -> str:
    """Translate the underlying entity's status to the canonical
    GatingEntityStatus. Conservative defaults — anything we don't
    recognize falls back to 'open' so the entity still gates."""
    if not underlying_status:
        return "open"
    s = underlying_status.lower()
    if entity_type == "rfi":
        if s == "draft": return "draft"
        if s == "sent": return "submitted"
        if s == "in_review": return "under_review"
        if s == "answered": return "responded"
        if s == "closed": return "closed"
    elif entity_type == "submittal":
        if s in ("not_yet_submitted", "draft"): return "draft"
        if s in ("submitted",): return "submitted"
        if s in ("in_review", "under_review"): return "under_review"
        if s in ("approved", "responded", "approved_as_noted"): return "responded"
        if s in ("revise_resubmit",): return "awaiting_clarification"
        if s in ("closed", "complete"): return "closed"
    elif entity_type == "field_issue":
        if s == "open": return "open"
        if s in ("in_progress",): return "under_review"
        if s in ("resolved", "closed"): return "closed"
    return "open"


# ---------- Working-day math via project_calendar_days ----------

async def _working_day_set(tenant_id: str, project_id: str,
                            start: date, end: date) -> set[date]:
    """Return the set of dates in [start, end] flagged as working days
    in the project calendar. Falls back to Mon-Fri if the calendar has
    no row for a date — keeps math sensible before initialize-calendar
    has been run."""
    db = get_db()
    docs = await db.project_calendar_days.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "calendar_date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "calendar_date": 1, "is_working_day": 1},
    ).to_list(2000)
    explicit_working: set[date] = set()
    explicit_seen: set[date] = set()
    for d in docs:
        try:
            cd = date.fromisoformat(str(d["calendar_date"])[:10])
            explicit_seen.add(cd)
            if d.get("is_working_day"):
                explicit_working.add(cd)
        except Exception:
            continue
    out: set[date] = set(explicit_working)
    cur = start
    while cur <= end:
        if cur not in explicit_seen and cur.weekday() < 5:
            out.add(cur)
        cur += timedelta(days=1)
    return out


async def add_working_days(tenant_id: str, project_id: str, start: date, days: int) -> date:
    """Return the date that is `days` working days after `start`. Used
    to compute target_response_at = submitted_at + sla_working_days."""
    if days <= 0:
        return start
    # Walk forward, accumulating working days. Cap iterations to avoid
    # an infinite loop if the calendar somehow has zero working days.
    horizon = start + timedelta(days=days * 3 + 14)
    working = await _working_day_set(tenant_id, project_id, start + timedelta(days=1), horizon)
    cur = start
    counted = 0
    while counted < days:
        cur += timedelta(days=1)
        if cur > horizon:
            break  # safety
        if cur in working:
            counted += 1
    return cur


async def working_day_count_between(tenant_id: str, project_id: str,
                                     start: date, end: date) -> int:
    """Count working days in (start, end] — does not include start day."""
    if end <= start:
        return 0
    working = await _working_day_set(tenant_id, project_id, start + timedelta(days=1), end)
    return len(working)


# ---------- Age + SLA computation ----------

async def compute_age_fields(ge: dict, *, today: Optional[date] = None) -> dict:
    """Returns {current_age_calendar_days, current_age_working_days,
    is_over_sla} for the given gating_entities dict. Pure-ish — does
    one DB query for the working-day calendar."""
    today = today or project_today()
    submitted = ge.get("submitted_at")
    sla = int(ge.get("sla_working_days") or 0)
    if not submitted:
        return {"current_age_calendar_days": None,
                "current_age_working_days": None, "is_over_sla": False}
    try:
        sd = (date.fromisoformat(str(submitted)[:10])
              if isinstance(submitted, str)
              else submitted.date() if hasattr(submitted, "date")
              else submitted)
    except Exception:
        return {"current_age_calendar_days": None,
                "current_age_working_days": None, "is_over_sla": False}
    cal_age = max(0, (today - sd).days)
    work_age = await working_day_count_between(
        ge["tenant_id"], ge["project_id"], sd, today,
    )
    over_sla = sla > 0 and work_age > sla and ge.get("status") not in ("responded", "closed", "withdrawn")
    return {
        "current_age_calendar_days": cal_age,
        "current_age_working_days": work_age,
        "is_over_sla": over_sla,
    }


# ---------- Display label denormalization ----------

def build_display_label(entity_type: str, entity: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (entity_number, display_label) for an underlying entity.
    Mirrored at create + status-sync time so the dropdown UI doesn't
    need a per-row join."""
    if entity_type == "rfi":
        num = entity.get("rfi_number")
        sub = (entity.get("subject") or "")[:80]
        return num, f"{num}: {sub}" if num else sub
    if entity_type == "submittal":
        num = entity.get("submittal_number")
        title = (entity.get("title") or "")[:80]
        return num, f"{num}: {title}" if num else title
    if entity_type == "field_issue":
        title = (entity.get("title") or "")[:80]
        return None, title
    return None, (entity.get("title") or entity.get("subject") or "")[:80]


# ---------- Create / update ----------

async def create_for_entity(
    *, tenant_id: str, project_id: str, entity_type: str, entity: dict,
    submitted_at: Optional[datetime] = None,
    gates_task_ids: Optional[list[str]] = None,
    sla_working_days: Optional[int] = None,
) -> dict:
    """Insert a gating_entities row for the given underlying entity and
    return the new doc. Caller is responsible for writing entity.gating_
    entity_id back. Status maps from entity.status; SLA defaults per type
    unless caller overrides."""
    db = get_db()
    sla = int(sla_working_days if sla_working_days is not None else SLA_DEFAULTS.get(entity_type, 10))
    submitted_dt = submitted_at or utc_now()
    submitted_iso = submitted_dt.isoformat() if hasattr(submitted_dt, "isoformat") else str(submitted_dt)
    submitted_d = (submitted_dt.date() if hasattr(submitted_dt, "date") else submitted_dt)
    target_d = await add_working_days(tenant_id, project_id, submitted_d, sla)
    target_iso = target_d.isoformat() + "T00:00:00+00:00"

    entity_number, display_label = build_display_label(entity_type, entity)
    underlying_status = entity.get("status")
    gating_status = map_underlying_status_to_gating(entity_type, underlying_status)

    now_iso = utc_now().isoformat()
    doc = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "entity_type": entity_type,
        "entity_id": entity["id"],
        "entity_number": entity_number,
        "display_label": display_label,
        "submitted_at": submitted_iso,
        "target_response_at": target_iso,
        "actual_responded_at": None,
        "closed_at": None,
        "sla_working_days": sla,
        "status": gating_status,
        "owner_attribution": DEFAULT_ATTRIBUTION.get(entity_type, "none"),
        "cause_class": DEFAULT_CAUSE.get(entity_type, "other"),
        "gates_task_ids": list(gates_task_ids or []),
        "current_age_calendar_days": 0,
        "current_age_working_days": 0,
        "is_over_sla": False,
        "privileged": False,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await db.gating_entities.insert_one(doc.copy())

    # Audit (lazy import to avoid circular).
    from audit import write_audit
    await write_audit(
        tenant_id=tenant_id, table_name="gating_entities", op="GATING_ENTITY_CREATE",
        record_id=doc["id"], before=None, after=doc,
        actor_id=None, request_id=None,
    )
    return doc


async def sync_status(
    *, tenant_id: str, entity_type: str, entity_id: str,
    underlying_status: Optional[str] = None,
    actual_responded_at: Optional[datetime] = None,
    closed_at: Optional[datetime] = None,
    new_display_label: Optional[str] = None,
    actor_id: Optional[str] = None, request_id: Optional[str] = None,
) -> Optional[dict]:
    """Mirror the underlying entity's status (and optional response /
    close timestamps) onto the gating_entities row. No-op if the row
    doesn't exist yet (older record, backfill needed)."""
    db = get_db()
    row = await db.gating_entities.find_one(
        tenant_filter(tenant_id, {"entity_type": entity_type, "entity_id": entity_id}),
        {"_id": 0},
    )
    if not row:
        return None
    new_gating = map_underlying_status_to_gating(entity_type, underlying_status) if underlying_status else row.get("status")
    updates: dict = {"updated_at": utc_now().isoformat()}
    if new_gating != row.get("status"):
        updates["status"] = new_gating
    if actual_responded_at and not row.get("actual_responded_at"):
        updates["actual_responded_at"] = (actual_responded_at.isoformat()
                                           if hasattr(actual_responded_at, "isoformat")
                                           else str(actual_responded_at))
    if closed_at and not row.get("closed_at"):
        updates["closed_at"] = (closed_at.isoformat()
                                  if hasattr(closed_at, "isoformat")
                                  else str(closed_at))
    if new_display_label and new_display_label != row.get("display_label"):
        updates["display_label"] = new_display_label

    if len(updates) <= 1:
        # Only updated_at — nothing to change.
        return row
    await db.gating_entities.update_one(
        tenant_filter(tenant_id, {"id": row["id"]}),
        {"$set": updates},
    )
    after = await db.gating_entities.find_one(
        tenant_filter(tenant_id, {"id": row["id"]}), {"_id": 0},
    )
    if "status" in updates:
        from audit import write_audit
        await write_audit(
            tenant_id=tenant_id, table_name="gating_entities",
            op="GATING_ENTITY_STATUS_CHANGE",
            record_id=row["id"],
            before={"status": row.get("status")},
            after={"status": updates["status"], "actual_responded_at": updates.get("actual_responded_at"),
                    "closed_at": updates.get("closed_at")},
            actor_id=actor_id, request_id=request_id,
        )
    return after
