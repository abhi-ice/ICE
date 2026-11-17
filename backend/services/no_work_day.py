"""Heading text for a daily report filed as a no-work day.

A report submitted as a no-work day stores is_no_work_day=True, a reason
code (no_work_reason, e.g. "weather") and an optional note (no_work_note,
required when the reason is "other"). Every place that shows or exports a
report uses no_work_heading() so the wording is the same everywhere:

    "No work day — Weather"
    "No work day — Holiday: Thanksgiving"
    "No work day — Building closed for inspection"   (reason "other")
    "No work day"                                     (no reason stored)

The frontend mirror lives in frontend/src/lib/noWorkDay.js; keep the two
label lists in step.
"""
from typing import Iterable, Optional

from database import tenant_filter

NO_WORK_DAY_TEXT = "No work day"

# Plain labels for the stored reason codes (models.NoWorkReason).
NO_WORK_REASON_LABELS = {
    "weather": "Weather",
    "holiday": "Holiday",
    "scheduled_non_work": "Scheduled non-work day",
    "crew_unavailable": "Crew unavailable",
    "tenant_restriction": "Tenant or owner access restricted",
    "other": "Other",
}


def no_work_reason_text(dr: dict) -> str:
    """The reason in words, from the stored reason and note. Empty when
    neither is stored."""
    reason = (dr.get("no_work_reason") or "").strip()
    note = " ".join(str(dr.get("no_work_note") or "").split())
    if reason == "other" and note:
        # "Other" is only a placeholder; the typed note is the reason.
        return note
    label = NO_WORK_REASON_LABELS.get(reason) or reason.replace("_", " ").capitalize()
    if label and note:
        return f"{label}: {note}"
    return label or note


def no_work_heading(dr: Optional[dict]) -> Optional[str]:
    """'No work day — <reason>' for a no-work-day report, else None."""
    if not dr or not dr.get("is_no_work_day"):
        return None
    reason = no_work_reason_text(dr)
    return f"{NO_WORK_DAY_TEXT} — {reason}" if reason else NO_WORK_DAY_TEXT


# What counts as recorded work on a report (the same rule the submit route
# uses to refuse a no-work day that has work on it): any activity row, or any
# delay, RFI, quality issue, safety event, delivery, photo or coordination
# event filed on it.
WORK_EVENT_COLLECTIONS = (
    "delays", "rfis", "qc_issues", "safety_events",
    "deliveries", "photos",
)


async def report_is_empty(db, tenant_id: str, daily_report_id: str) -> bool:
    """True if the report has no activity rows and nothing filed on it."""
    if await db.daily_report_activities.count_documents(
            {"daily_report_id": daily_report_id}, limit=1):
        return False
    for coll in WORK_EVENT_COLLECTIONS:
        if await db[coll].count_documents(
                tenant_filter(tenant_id, {"daily_report_id": daily_report_id}), limit=1):
            return False
    if await db.coordination_events.count_documents(
            tenant_filter(tenant_id, {"linked_daily_report_id": daily_report_id}), limit=1):
        return False
    return True


async def clear_no_work_day_if_work_added(db, *, tenant_id: str, daily_report_id: Optional[str],
                                          actor_id: Optional[str],
                                          request_id: Optional[str] = None) -> bool:
    """A report filed as a no-work day stops being one once work is recorded
    on it after submit (an admin or project manager, or the author within the
    24-hour window, adds an activity row, a delay, a photo ...). Otherwise the
    client PDF, exports and screens would print "No work day" in large type
    over a report that lists activities and hours.

    Called after every successful add to a submitted or approved report
    (services/edit_permissions.stamp_add_metadata). Clears is_no_work_day and
    the stored reason and note, and writes an audit row with what was
    cleared. Returns True when it cleared something. Never raises."""
    if not daily_report_id:
        return False
    try:
        dr = await db.daily_reports.find_one(
            tenant_filter(tenant_id, {"id": daily_report_id}),
            {"_id": 0, "id": 1, "is_no_work_day": 1, "no_work_reason": 1, "no_work_note": 1},
        )
        if not dr or not dr.get("is_no_work_day"):
            return False
        if await report_is_empty(db, tenant_id, daily_report_id):
            return False
        res = await db.daily_reports.update_one(
            tenant_filter(tenant_id, {"id": daily_report_id, "is_no_work_day": True}),
            {"$set": {"is_no_work_day": False},
             "$unset": {"no_work_reason": "", "no_work_note": ""}},
        )
        if not getattr(res, "modified_count", 1):
            return False
        from audit import write_audit
        await write_audit(
            tenant_id=tenant_id, table_name="daily_reports", op="NO_WORK_DAY_CLEARED",
            record_id=daily_report_id,
            before={k: dr.get(k) for k in ("is_no_work_day", "no_work_reason", "no_work_note")},
            after={"is_no_work_day": False,
                   "reason": "work was recorded on the report after it was submitted"},
            actor_id=actor_id, request_id=request_id,
        )
        return True
    except Exception:
        import logging
        logging.getLogger("no_work_day").warning(
            "could not clear the no-work-day flag on %s", daily_report_id, exc_info=True)
        return False


def no_work_days_summary(drs: Iterable[dict]) -> list[dict]:
    """One entry per no-work-day report, oldest first, for weekly summaries."""
    out = []
    for dr in drs or []:
        heading = no_work_heading(dr)
        if heading:
            out.append({
                "daily_report_id": dr.get("id"),
                "report_date": str(dr.get("report_date") or "")[:10],
                "heading": heading,
            })
    out.sort(key=lambda r: r["report_date"])
    return out
