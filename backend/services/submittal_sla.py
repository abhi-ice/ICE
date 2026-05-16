"""Bucket E Phase 2 (2026-05-11) -- Submittal A/E Review SLA service.

Owns the clock arithmetic, breach detection, chain-of-custody event
recording, aging roll-up, and pause/resume semantics for the
14-day-default A/E review SLA established in Phase 1.

Design notes
------------
- Default working-day SLA per FAR 52.236-15 / GSAM. Calendar-day mode
  available via project.submittal_sla_uses_calendar_days (admin
  override) + per-submittal override on sla_uses_calendar_days_
  override.
- Working-day count skips:
    * weekends (Sat/Sun)
    * 2026 federal holidays (hardcoded; future bucket pulls from
      python-holidays or a project_holidays table)
    * project no-work days (from daily_reports.is_no_work_day=true)
- Pause/resume: while paused (sla_status="paused"), aging-day clock
  stops. Resume re-bases the due date forward by the accumulated
  paused duration.
- Revisions: revise_resubmit -> submitted transitions increment
  review_cycle_count, sum the prior cycle's calendar+working day
  totals into total_review_*, and re-compute a fresh due_date from
  the new submitted_to_ae_at.
- detect_breaches scans all open submittals where now > due_date AND
  not yet stamped breached. Idempotent: rows already at sla_status=
  "breached" skip re-recording.
- Cache: aging_summary cached 5min per (tenant, project, days) -- the
  per-project register list refresh hits this every 60s; without the
  cache we'd re-scan submittals on every page render. Invalidated
  on every mutation path that touches submittal state.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Any

from database import get_db, tenant_filter
from models import gen_id, utc_now, SUBMITTAL_DEFAULT_SLA_DAYS
from audit import write_audit

log = logging.getLogger("submittal_sla")


# =====================================================================
# Federal holiday set (2026). Hardcoded per spec; future bucket reads
# a holidays library or per-project table. List sourced from OPM
# 2026 calendar.
# =====================================================================
FEDERAL_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; July 4 is Sat)
    date(2026, 9, 7),    # Labor Day
    date(2026, 10, 12),  # Columbus Day
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas Day
}

# Aging buckets per Phase 5 spec. Inclusive-lower, exclusive-upper.
AGING_BUCKETS = [
    (0, 8,   "0-7d"),
    (8, 15,  "8-14d"),
    (15, 22, "15-21d"),
    (22, 31, "22-30d"),
    (31, None, "30+d"),
]

# Aging-summary cache: 5-minute TTL per (tenant, project, days).
AGING_CACHE_TTL_SECONDS = 5 * 60
_AGING_CACHE_MAX = 64
_aging_cache: dict[str, tuple[float, dict]] = {}


def _aging_cache_key(*, tenant_id: str, project_id: str, days: int) -> str:
    return f"{tenant_id}|{project_id}|{days}"


def _evict_aging_cache_if_needed() -> None:
    if len(_aging_cache) <= _AGING_CACHE_MAX:
        return
    items = sorted(_aging_cache.items(), key=lambda kv: kv[1][0])
    for k, _ in items[: max(1, len(items) // 2)]:
        _aging_cache.pop(k, None)


def invalidate_aging_cache(*, tenant_id: str, project_id: Optional[str] = None) -> None:
    """Drop cached aging summaries. Called on every mutation path that
    touches submittal state. If project_id is None, flushes every page
    for the tenant."""
    if not tenant_id:
        return
    prefix = f"{tenant_id}|"
    if project_id:
        prefix = f"{tenant_id}|{project_id}|"
    for k in list(_aging_cache.keys()):
        if k.startswith(prefix):
            _aging_cache.pop(k, None)


# =====================================================================
# Per-project no-work day lookup (Bucket D integration).
# =====================================================================

async def _project_no_work_days(
    *, tenant_id: str, project_id: str,
    start: date, end: date,
) -> set[date]:
    """Pull no-work dates declared on the project's daily_reports.
    Returns a set of date() values that the working-day arithmetic
    should skip. Range-bounded so we don't load the whole project
    history on every SLA calc."""
    if not project_id:
        return set()
    db = get_db()
    rows = await db.daily_reports.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "is_no_work_day": True,
            "report_date": {
                "$gte": start.isoformat(),
                "$lte": end.isoformat(),
            },
        }),
        {"_id": 0, "report_date": 1},
    ).to_list(length=None)
    out: set[date] = set()
    for r in rows:
        try:
            out.add(date.fromisoformat(str(r["report_date"])[:10]))
        except Exception:
            pass
    return out


# =====================================================================
# Clock arithmetic.
# =====================================================================

def _is_working_day(d: date, no_work: set[date]) -> bool:
    """True if `d` counts as a working day for SLA purposes."""
    if d.weekday() >= 5:        # Sat / Sun
        return False
    if d in FEDERAL_HOLIDAYS_2026:
        return False
    if d in no_work:
        return False
    return True


def _working_days_between(
    start: date, end: date, no_work: set[date],
) -> int:
    """Count working days from `start` (inclusive) to `end` (exclusive).
    Returns 0 if end <= start."""
    if end <= start:
        return 0
    days = 0
    cur = start
    while cur < end:
        if _is_working_day(cur, no_work):
            days += 1
        cur += timedelta(days=1)
    return days


def _add_working_days(start: date, n: int, no_work: set[date]) -> date:
    """Return the date that is `n` working days after `start`."""
    if n <= 0:
        return start
    cur = start
    remaining = n
    # Walk forward day-by-day; for 14d windows this is trivial.
    while remaining > 0:
        cur += timedelta(days=1)
        if _is_working_day(cur, no_work):
            remaining -= 1
    return cur


async def compute_due_date(
    *, tenant_id: str, project_id: str, submitted_at: datetime,
    sla_days: int, uses_calendar_days: bool,
) -> datetime:
    """Compute the A/E review due date from a submission timestamp.
    Returns a tz-aware UTC datetime at midnight on the due day -- the
    SLA "boundary" is end-of-day on the due date.

    Bug #40 diagnostic note (2026-05-13):
    -------------------------------------
    If a submittal's `a_e_review_due_at` looks too short relative to
    the project's `submittal_review_sla_days` setting (e.g. SLA = 14
    working days but due is only ~4 working days from submission),
    the math here is NOT the cause. Verified: this function walks
    forward `sla_days` working days from `submitted_at.date()`,
    skipping weekends + FEDERAL_HOLIDAYS_2026 + per-project
    no_work_days. For a 2026-05-12 submission with 14 WD, due
    should be ~2026-06-02 (Memorial Day 5/25 is in the holiday set).

    Three likely data conditions for a too-short stored due date:

      1. Per-submittal `sla_days_override` is set. Resolved at
         line 376 above:
           sla_days = (sub["sla_days_override"]
                        or project["submittal_review_sla_days"]
                        or SUBMITTAL_DEFAULT_SLA_DAYS)
         If the override is non-None, the project's SLA is ignored.
         Forensic check: db.submittals.findOne({submittal_number:
         "SUB-0001"}, {sla_days_override: 1, sla_uses_calendar_days_
         override: 1}).

      2. The stored `a_e_review_due_at` was computed when the
         project's SLA setting was different (e.g. 4 WD historically,
         then changed to 14). The PATCH /projects/{id} handler does
         NOT currently trigger a per-submittal recompute on SLA
         setting changes. Existing submittals keep their stale
         due dates until something else fires their recompute.
         Forensic fix: call recompute_for_submittal(tenant_id, sub_
         id) from a Python shell, OR record a review-event on the
         submittal (which calls recompute internally).

      3. `submitted_to_ae_at` itself is wrong. The clock anchors on
         this field; if it's a different date than expected, the
         due date will be too. Check db.audit_log for the
         SUBMITTAL_SLA_REVIEW_EVENT_RECORDED op on this submittal
         and verify the submitted_to_ae_at timestamp.

    None of these are code defects in this function. If a
    follow-up sprint wants to auto-recompute existing submittals
    on project SLA changes, the right place to add that is the
    PATCH /projects/{id} handler in projects_router.py:158 --
    fire a BG task calling recompute_for_project. NOT changed here
    to keep this fix scoped to documentation only.
    """
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    base = submitted_at.date()
    if uses_calendar_days:
        due = base + timedelta(days=sla_days)
    else:
        # Pull no-work days over the window [base, base + 4*sla_days]
        # so the working-day march has every skip declared. 4x cushion
        # because weekends + holidays + no-work can stack.
        no_work = await _project_no_work_days(
            tenant_id=tenant_id, project_id=project_id,
            start=base, end=base + timedelta(days=max(60, sla_days * 4)),
        )
        due = _add_working_days(base, sla_days, no_work)
    return datetime(due.year, due.month, due.day, 23, 59, 59,
                    tzinfo=timezone.utc)


async def compute_aging(
    *, tenant_id: str, project_id: str,
    start_iso: str, end_iso: Optional[str] = None,
) -> tuple[int, int]:
    """Returns (calendar_days, working_days) between two ISO strings.
    end_iso=None -> compare to now. Returns (0, 0) on bad input."""
    if not start_iso:
        return (0, 0)
    try:
        s = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
    except Exception:
        return (0, 0)
    if s.tzinfo is None:
        s = s.replace(tzinfo=timezone.utc)
    if end_iso:
        try:
            e = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        except Exception:
            e = datetime.now(timezone.utc)
    else:
        e = datetime.now(timezone.utc)
    if e.tzinfo is None:
        e = e.replace(tzinfo=timezone.utc)
    cal = max(0, (e.date() - s.date()).days)
    no_work = await _project_no_work_days(
        tenant_id=tenant_id, project_id=project_id,
        start=s.date(), end=e.date() + timedelta(days=1),
    )
    work = _working_days_between(s.date(), e.date(), no_work)
    return (cal, work)


async def compute_must_approve_by(
    *, tenant_id: str, project_id: str,
    schedule_activity_id: Optional[str],
    procurement_lead_time_days: Optional[int],
    uses_calendar_days: bool = False,
) -> Optional[str]:
    """Backward calc: linked task's planned_start - lead_time. Returns
    None when either input is missing OR the task isn't found."""
    if not schedule_activity_id or procurement_lead_time_days is None:
        return None
    if procurement_lead_time_days < 0:
        return None
    db = get_db()
    task = await db.tasks.find_one(
        tenant_filter(tenant_id, {"id": schedule_activity_id}),
        {"_id": 0, "planned_start": 1},
    )
    if not task or not task.get("planned_start"):
        return None
    try:
        ps = task["planned_start"]
        if isinstance(ps, str):
            ps_date = date.fromisoformat(ps[:10])
        elif isinstance(ps, datetime):
            ps_date = ps.date()
        else:
            ps_date = date.fromisoformat(str(ps)[:10])
    except Exception:
        return None
    if uses_calendar_days:
        target = ps_date - timedelta(days=procurement_lead_time_days)
    else:
        # Walk BACK working days. Re-use the forward helper by mirroring:
        # find the date such that working_days_between(target, ps_date)
        # == lead_time. Cap at 4x cushion as elsewhere.
        no_work = await _project_no_work_days(
            tenant_id=tenant_id, project_id=project_id,
            start=ps_date - timedelta(days=max(60, procurement_lead_time_days * 4)),
            end=ps_date,
        )
        target = ps_date
        remaining = procurement_lead_time_days
        while remaining > 0:
            target -= timedelta(days=1)
            if _is_working_day(target, no_work):
                remaining -= 1
    return target.isoformat()


def compute_urgency_flag(
    *, due_date_iso: Optional[str],
    must_approve_by_iso: Optional[str],
    aging_days: Optional[int] = None,
) -> bool:
    """True when the current SLA pace would miss must_approve_by:
    due_date > must_approve_by. Returns False when either side is
    missing (we can't predict)."""
    if not due_date_iso or not must_approve_by_iso:
        return False
    try:
        due = date.fromisoformat(str(due_date_iso)[:10])
        target = date.fromisoformat(str(must_approve_by_iso)[:10])
    except Exception:
        return False
    return due > target


# =====================================================================
# SLA status derivation.
# =====================================================================

def _sla_status_for(
    *, status: str, due_date_iso: Optional[str], paused: bool,
    superseded: bool, now_iso: str,
) -> str:
    """Map (current submittal status + due-date + paused flag) to one
    of: n_a / on_track / approaching / breached / paused. Approaching
    = within 3 calendar days of due."""
    if superseded:
        return "n_a"
    if paused:
        return "paused"
    if status in ("approved", "approved_as_noted", "rejected", "closed"):
        return "n_a"
    if status == "not_yet_submitted":
        return "n_a"
    if not due_date_iso:
        return "n_a"
    try:
        now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        due_dt = datetime.fromisoformat(str(due_date_iso).replace("Z", "+00:00"))
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        if due_dt.tzinfo is None:
            due_dt = due_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return "n_a"
    if now_dt > due_dt:
        return "breached"
    delta = (due_dt - now_dt).total_seconds() / 86400.0
    if delta <= 3.0:
        return "approaching"
    return "on_track"


# =====================================================================
# Recompute -- single submittal.
# =====================================================================

async def recompute_aging_for_submittal(
    *, tenant_id: str, submittal_id: str,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Recomputes every derived SLA + aging field on one submittal.
    Idempotent. Returns the updated subset. Invalidates the aging
    summary cache for the project."""
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(tenant_id, {"id": submittal_id}),
        {"_id": 0},
    )
    if not sub:
        return {"ok": False, "reason": "not_found"}

    project_id = sub.get("project_id") or ""
    project = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "submittal_review_sla_days": 1,
         "submittal_sla_uses_calendar_days": 1},
    ) or {}
    sla_days = (sub.get("sla_days_override")
                or project.get("submittal_review_sla_days")
                or SUBMITTAL_DEFAULT_SLA_DAYS)
    if sub.get("sla_uses_calendar_days_override") is not None:
        uses_calendar = bool(sub["sla_uses_calendar_days_override"])
    else:
        uses_calendar = bool(project.get("submittal_sla_uses_calendar_days", False))

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    updates: dict[str, Any] = {}

    submitted_to_ae_at = sub.get("submitted_to_ae_at")
    superseded = bool(sub.get("superseded_by"))
    paused = sub.get("sla_status") == "paused"

    # Re-derive due_date.
    new_due = None
    if submitted_to_ae_at and not superseded:
        try:
            ts = datetime.fromisoformat(
                str(submitted_to_ae_at).replace("Z", "+00:00"),
            )
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            due_dt = await compute_due_date(
                tenant_id=tenant_id, project_id=project_id,
                submitted_at=ts, sla_days=int(sla_days),
                uses_calendar_days=uses_calendar,
            )
            new_due = due_dt.isoformat()
            updates["a_e_review_due_at"] = new_due
        except Exception as exc:
            log.warning("due-date recompute failed for %s: %s",
                        submittal_id, exc)

    # Aging metrics: clock anchored on submitted_to_ae_at; end is
    # approved_at if disposition reached, else now.
    end_iso = None
    if sub.get("status") in ("approved", "approved_as_noted", "rejected",
                              "closed") and sub.get("approved_at"):
        end_iso = sub["approved_at"]
    if submitted_to_ae_at:
        cal, work = await compute_aging(
            tenant_id=tenant_id, project_id=project_id,
            start_iso=submitted_to_ae_at, end_iso=end_iso,
        )
        updates["aging_days"] = cal
        updates["aging_working_days"] = work
    else:
        updates["aging_days"] = None
        updates["aging_working_days"] = None

    # SLA status + overdue.
    sla_status = _sla_status_for(
        status=sub.get("status") or "not_yet_submitted",
        due_date_iso=new_due or sub.get("a_e_review_due_at"),
        paused=paused, superseded=superseded, now_iso=now_iso,
    )
    updates["sla_status"] = sla_status
    if sla_status == "breached":
        updates["is_overdue"] = True
        try:
            due_chk = new_due or sub.get("a_e_review_due_at")
            due_dt = datetime.fromisoformat(
                str(due_chk).replace("Z", "+00:00"),
            )
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)
            updates["overdue_days"] = max(0, (now - due_dt).days)
        except Exception:
            updates["overdue_days"] = None
    else:
        updates["is_overdue"] = False
        updates["overdue_days"] = None

    # must_approve_by + urgency flag.
    mab = await compute_must_approve_by(
        tenant_id=tenant_id, project_id=project_id,
        schedule_activity_id=sub.get("schedule_activity_id"),
        procurement_lead_time_days=sub.get("procurement_lead_time_days"),
        uses_calendar_days=uses_calendar,
    )
    updates["must_approve_by"] = mab
    updates["urgency_flag"] = compute_urgency_flag(
        due_date_iso=new_due or sub.get("a_e_review_due_at"),
        must_approve_by_iso=mab,
    )

    await db.submittals.update_one(
        {"id": submittal_id, "tenant_id": tenant_id},
        {"$set": updates},
    )
    invalidate_aging_cache(tenant_id=tenant_id, project_id=project_id)

    # CPM Part E (2026-05-15) -- if sla_status crossed the breached
    # boundary (in or out), the open-blocker aggregation for this
    # submittal's linked task changed -- trigger an auto-recompute
    # so downstream projected_finish_date reflects the new gating
    # state. Only fires on the breached transition; aging-only
    # updates (approaching <-> on_track etc.) don't affect
    # count_open_blockers and skip the hook. Fail-safe.
    prev_status = sub.get("sla_status")
    new_status = updates.get("sla_status")
    breached_boundary_crossed = (
        (prev_status == "breached") != (new_status == "breached")
    )
    # CPM Part K-2 (2026-05-15) -- HYBRID auto-suggest fires ONLY on
    # the in-to-breached transition (not on out-to-breached or every
    # recompute). detect_breaches handles the bulk scan; this path
    # handles single-row recomputes that cross the boundary (e.g.,
    # PATCH /submittals that bumped aging past the SLA window).
    entered_breached = (
        prev_status != "breached" and new_status == "breached"
    )
    k2_suggested_impacts = False
    if entered_breached:
        try:
            k2_res = await auto_suggest_per_task_impacts_for_submittal(
                tenant_id=tenant_id, submittal_id=submittal_id,
                only_when_breached=False,  # we know it's breached
                actor_id=actor_id, request_id=request_id,
            )
            if k2_res.get("suggested"):
                k2_suggested_impacts = True
        except Exception as exc:
            log.warning(
                "K-2 auto-suggest after recompute flip failed "
                "(sub=%s, project=%s): %s",
                submittal_id, project_id, exc,
            )
    if breached_boundary_crossed:
        try:
            from services.cpm import auto_recompute_after_event
            await auto_recompute_after_event(
                tenant_id, project_id,
                actor_id=actor_id, request_id=request_id,
            )
        except Exception as exc:
            log.warning(
                "Part E auto-recompute after submittal SLA flip "
                "failed (sub=%s, project=%s): %s",
                submittal_id, project_id, exc,
            )

    # CPM Part K-3 (2026-05-15) -- after K-2 wrote fresh entries AND
    # auto_recompute_after_event refreshed total_float_days, stamp the
    # 5 verdict keys via the resolver. Mirrors the order in
    # detect_breaches (K-2 -> recompute -> K-3). The source field
    # rides through unchanged. Fail-safe.
    if k2_suggested_impacts:
        try:
            await _resolve_and_persist_submittal_per_task_impacts(
                tenant_id=tenant_id, submittal_id=submittal_id,
                project_id=project_id,
                exclude_submittal_id=submittal_id,
            )
        except Exception as exc:
            log.warning(
                "K-3 verdict stamping after recompute flip failed "
                "(sub=%s, project=%s): %s",
                submittal_id, project_id, exc,
            )

    return {"ok": True, "updates": updates, "submittal_id": submittal_id}


async def recompute_aging_bulk(
    *, tenant_id: str, project_id: Optional[str] = None,
    limit: int = 5000,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Walks open submittals (status != closed/rejected) for the tenant
    -- optionally scoped to a project -- and re-runs the per-row
    recompute. Returns counts."""
    db = get_db()
    q: dict = {"tenant_id": tenant_id,
                "status": {"$nin": ["closed", "rejected"]}}
    if project_id:
        q["project_id"] = project_id
    rows = await db.submittals.find(
        q, {"_id": 0, "id": 1},
    ).limit(max(1, min(int(limit), 10000))).to_list(length=None)
    out = {"examined": len(rows), "updated": 0, "failed": 0}
    seen_projects: set = set()
    for r in rows:
        try:
            res = await recompute_aging_for_submittal(
                tenant_id=tenant_id, submittal_id=r["id"],
                actor_id=actor_id, request_id=request_id,
            )
            if res.get("ok"):
                out["updated"] += 1
            else:
                out["failed"] += 1
        except Exception as exc:
            out["failed"] += 1
            log.warning("bulk recompute failed for %s: %s", r["id"], exc)
    # Audit one row per recompute pass so the chain captures the
    # cadence of the cleanup runs.
    if out["updated"]:
        try:
            await write_audit(
                tenant_id=tenant_id, table_name="submittals",
                op="SUBMITTAL_SLA_AGING_RECOMPUTED",
                record_id=project_id or "tenant_wide",
                before=None, after=out,
                actor_id=actor_id,
                request_id=request_id or "submittal_sla_bulk_recompute",
            )
        except Exception:
            pass
    return out


# =====================================================================
# Pause / resume.
# =====================================================================

async def pause_sla(
    *, tenant_id: str, submittal_id: str, reason: str,
    actor_id: str, request_id: Optional[str] = None,
) -> dict:
    if not reason or len(reason.strip()) < 5:
        return {"ok": False, "reason": "reason_min_5_chars",
                "http_status": 422}
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(tenant_id, {"id": submittal_id}), {"_id": 0},
    )
    if not sub:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    if sub.get("sla_status") == "paused":
        return {"ok": False, "reason": "already_paused",
                "http_status": 409}
    now_iso = utc_now().isoformat()
    await db.submittals.update_one(
        {"id": submittal_id, "tenant_id": tenant_id},
        {"$set": {
            "sla_status": "paused",
            "sla_breach_reason": (sub.get("sla_breach_reason") or None),
            "sla_paused_at": now_iso,
            "sla_pause_reason": reason.strip(),
        }},
    )
    invalidate_aging_cache(
        tenant_id=tenant_id, project_id=sub.get("project_id"),
    )
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="submittals",
            op="SUBMITTAL_SLA_PAUSED",
            record_id=submittal_id, before={"sla_status": sub.get("sla_status")},
            after={"sla_status": "paused", "reason": reason.strip()},
            actor_id=actor_id, request_id=request_id,
        )
    except Exception:
        pass
    # Also record a chain event.
    await _persist_review_event(
        tenant_id=tenant_id, project_id=sub.get("project_id") or "",
        submittal_id=submittal_id, event_type="supplement_filed",
        performed_by=actor_id, notes=f"SLA paused: {reason.strip()}",
        status_before=sub.get("status"),
        status_after=sub.get("status"),
    )
    return {"ok": True}


async def resume_sla(
    *, tenant_id: str, submittal_id: str, actor_id: str,
    request_id: Optional[str] = None,
) -> dict:
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(tenant_id, {"id": submittal_id}), {"_id": 0},
    )
    if not sub:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    if sub.get("sla_status") != "paused":
        return {"ok": False, "reason": "not_paused", "http_status": 409}
    now = datetime.now(timezone.utc)
    paused_at = sub.get("sla_paused_at")
    paused_duration_days = 0
    if paused_at:
        try:
            pa = datetime.fromisoformat(str(paused_at).replace("Z", "+00:00"))
            if pa.tzinfo is None:
                pa = pa.replace(tzinfo=timezone.utc)
            paused_duration_days = max(0, (now - pa).days)
        except Exception:
            pass
    # Roll the due-date forward by the paused duration.
    new_due = None
    cur_due = sub.get("a_e_review_due_at")
    if cur_due and paused_duration_days > 0:
        try:
            cd = datetime.fromisoformat(str(cur_due).replace("Z", "+00:00"))
            if cd.tzinfo is None:
                cd = cd.replace(tzinfo=timezone.utc)
            new_due = (cd + timedelta(days=paused_duration_days)).isoformat()
        except Exception:
            new_due = cur_due
    updates = {
        "sla_status": "on_track",
        "sla_paused_at": None,
        "sla_pause_reason": None,
    }
    if new_due:
        updates["a_e_review_due_at"] = new_due
    await db.submittals.update_one(
        {"id": submittal_id, "tenant_id": tenant_id},
        {"$set": updates},
    )
    # Re-run derivation for sla_status + aging post-resume.
    await recompute_aging_for_submittal(
        tenant_id=tenant_id, submittal_id=submittal_id,
        actor_id=actor_id, request_id=request_id,
    )
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="submittals",
            op="SUBMITTAL_SLA_RESUMED",
            record_id=submittal_id,
            before={"sla_status": "paused", "due": cur_due},
            after={"sla_status": "on_track", "due": new_due or cur_due,
                    "paused_duration_days": paused_duration_days},
            actor_id=actor_id, request_id=request_id,
        )
    except Exception:
        pass
    return {"ok": True, "paused_duration_days": paused_duration_days,
            "new_due_at": new_due}


# =====================================================================
# Chain event recorder.
# =====================================================================

# Maps event_type -> status_after the parent submittal should adopt.
# None means "don't change status" (e.g., sla_breached is a marker).
_STATUS_TRANSITIONS: dict[str, Optional[str]] = {
    "submitted_to_ae":      "submitted",
    "ae_review_started":    "in_review",
    "ae_review_completed":  None,   # Disposition events carry the new status.
    "forwarded_to_sub":     None,
    "returned_from_sub":    "submitted",
    "sla_breached":         None,
    "supplement_filed":     None,
    "approved":             "approved",
    "approved_as_noted":    "approved_as_noted",
    "rejected":             "rejected",
    "revise_resubmit":      "revise_resubmit",
    "closed":               "closed",
}


async def _persist_review_event(
    *, tenant_id: str, project_id: str, submittal_id: str,
    event_type: str, performed_by: Optional[str],
    notes: Optional[str] = None,
    status_before: Optional[str] = None,
    status_after: Optional[str] = None,
    performed_at: Optional[datetime] = None,
) -> dict:
    db = get_db()
    now = performed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Look up the prior event so we can stamp elapsed-day deltas.
    prior = await db.submittal_review_events.find_one(
        tenant_filter(tenant_id, {"submittal_id": submittal_id}),
        {"_id": 0, "performed_at": 1},
        sort=[("performed_at", -1)],
    )
    cal_delta = None
    work_delta = None
    if prior and prior.get("performed_at"):
        cal_delta, work_delta = await compute_aging(
            tenant_id=tenant_id, project_id=project_id,
            start_iso=prior["performed_at"], end_iso=now.isoformat(),
        )
    doc = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "submittal_id": submittal_id,
        "event_type": event_type,
        "performed_by": performed_by,
        "performed_at": now.isoformat(),
        "status_before": status_before,
        "status_after": status_after,
        "notes": notes,
        "calendar_days_elapsed": cal_delta,
        "working_days_elapsed": work_delta,
        "created_at": now.isoformat(),
    }
    await db.submittal_review_events.insert_one(doc.copy())
    return doc


async def record_review_event(
    *, tenant_id: str, submittal_id: str, event_type: str,
    performed_by: Optional[str], notes: Optional[str] = None,
    performed_at: Optional[datetime] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Records one chain-of-custody event. Also fires the parent status
    transition (when defined by _STATUS_TRANSITIONS) + triggers a full
    recompute of derived fields."""
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(tenant_id, {"id": submittal_id}), {"_id": 0},
    )
    if not sub:
        return {"ok": False, "reason": "not_found", "http_status": 404}

    status_before = sub.get("status")
    new_status = _STATUS_TRANSITIONS.get(event_type)
    is_revision = (
        event_type == "returned_from_sub"
        and status_before == "revise_resubmit"
    )

    now = performed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_iso = now.isoformat()

    sub_updates: dict[str, Any] = {}

    # Sticky timestamps depending on event_type.
    if event_type == "submitted_to_ae":
        sub_updates["submitted_to_ae_at"] = now_iso
        if not sub.get("submitted_at"):
            sub_updates["submitted_at"] = now_iso
    elif event_type == "ae_review_started":
        if not sub.get("a_e_review_started_at"):
            sub_updates["a_e_review_started_at"] = now_iso
    elif event_type == "ae_review_completed":
        sub_updates["a_e_review_completed_at"] = now_iso
    elif event_type == "forwarded_to_sub":
        sub_updates["forwarded_to_sub_at"] = now_iso
    elif event_type == "returned_from_sub":
        sub_updates["received_back_at"] = now_iso

    # Revision bookkeeping. revise_resubmit -> submitted via the
    # returned_from_sub path increments review_cycle_count, rolls
    # the prior cycle's days into total_review_*, resets the per-
    # cycle clock by stamping a new submitted_to_ae_at.
    if is_revision:
        prior_cycle_cal, prior_cycle_work = await compute_aging(
            tenant_id=tenant_id, project_id=sub.get("project_id") or "",
            start_iso=sub.get("submitted_to_ae_at"),
            end_iso=sub.get("received_back_at") or now_iso,
        )
        sub_updates["review_cycle_count"] = int(sub.get("review_cycle_count") or 0) + 1
        sub_updates["total_review_calendar_days"] = (
            int(sub.get("total_review_calendar_days") or 0) + prior_cycle_cal
        )
        sub_updates["total_review_working_days"] = (
            int(sub.get("total_review_working_days") or 0) + prior_cycle_work
        )
        # Fresh clock: this returned_from_sub IS the new submission.
        sub_updates["submitted_to_ae_at"] = now_iso
        sub_updates["a_e_review_started_at"] = None
        sub_updates["a_e_review_completed_at"] = None
        sub_updates["forwarded_to_sub_at"] = None
        sub_updates["received_back_at"] = now_iso

    # Status transition (if event_type defines one).
    if new_status and new_status != status_before:
        sub_updates["status"] = new_status
        if new_status in ("approved", "approved_as_noted"):
            sub_updates["approved_at"] = now_iso

    if sub_updates:
        await db.submittals.update_one(
            {"id": submittal_id, "tenant_id": tenant_id},
            {"$set": sub_updates},
        )

    # Record the chain event.
    event_doc = await _persist_review_event(
        tenant_id=tenant_id,
        project_id=sub.get("project_id") or "",
        submittal_id=submittal_id, event_type=event_type,
        performed_by=performed_by, notes=notes,
        status_before=status_before,
        status_after=sub_updates.get("status", status_before),
        performed_at=now,
    )

    # Recompute derived fields.
    await recompute_aging_for_submittal(
        tenant_id=tenant_id, submittal_id=submittal_id,
        actor_id=performed_by, request_id=request_id,
    )

    try:
        await write_audit(
            tenant_id=tenant_id, table_name="submittal_review_events",
            op="SUBMITTAL_SLA_REVIEW_EVENT_RECORDED",
            record_id=event_doc["id"], before={"status": status_before},
            after={"submittal_id": submittal_id,
                    "event_type": event_type,
                    "status_after": sub_updates.get("status", status_before),
                    "is_revision": is_revision,
                    "calendar_days_elapsed": event_doc.get("calendar_days_elapsed"),
                    "working_days_elapsed": event_doc.get("working_days_elapsed")},
            actor_id=performed_by,
            request_id=request_id or f"sub_event_{event_doc['id']}",
        )
    except Exception:
        pass

    invalidate_aging_cache(
        tenant_id=tenant_id, project_id=sub.get("project_id"),
    )
    return {"ok": True, "event": event_doc,
            "is_revision": is_revision,
            "submittal_updates": sub_updates}


async def assign_reviewer(
    *, tenant_id: str, submittal_id: str, reviewer_user_id: str,
    actor_id: str, request_id: Optional[str] = None,
) -> dict:
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(tenant_id, {"id": submittal_id}), {"_id": 0},
    )
    if not sub:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    user = await db.users.find_one(
        {"id": reviewer_user_id}, {"_id": 0, "id": 1},
    )
    if not user:
        return {"ok": False, "reason": "reviewer_not_found",
                "http_status": 422}
    prev = sub.get("assigned_reviewer_id")
    await db.submittals.update_one(
        {"id": submittal_id, "tenant_id": tenant_id},
        {"$set": {"assigned_reviewer_id": reviewer_user_id,
                   "assigned_reviewer_at": utc_now().isoformat()}},
    )
    invalidate_aging_cache(
        tenant_id=tenant_id, project_id=sub.get("project_id"),
    )
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="submittals",
            op="SUBMITTAL_SLA_REVIEWER_ASSIGNED",
            record_id=submittal_id,
            before={"assigned_reviewer_id": prev},
            after={"assigned_reviewer_id": reviewer_user_id},
            actor_id=actor_id, request_id=request_id,
        )
    except Exception:
        pass
    return {"ok": True, "reviewer_id": reviewer_user_id}


# =====================================================================
# Breach detection.
# =====================================================================

async def detect_breaches(
    *, tenant_id: str, project_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Scans open submittals for ones now past their due_date and not
    yet stamped 'breached'. For each, flips sla_status -> breached,
    records a sla_breached chain event, fires SUBMITTAL_SLA_BREACHED
    audit, and triggers cascade signal on linked tasks (Phase 4d).
    Idempotent: rows already breached skip silently."""
    db = get_db()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    q: dict = {
        "tenant_id": tenant_id,
        "status": {"$nin": ["approved", "approved_as_noted", "rejected", "closed"]},
        "sla_status": {"$nin": ["breached", "n_a", "paused"]},
        "a_e_review_due_at": {"$lt": now_iso, "$ne": None},
    }
    if project_id:
        q["project_id"] = project_id
    rows = await db.submittals.find(
        q, {"_id": 0, "id": 1, "project_id": 1,
             "a_e_review_due_at": 1, "linked_task_ids": 1,
             "schedule_activity_id": 1, "status": 1,
             "gating_entity_id": 1},
    ).to_list(length=None)

    out = {"scanned": len(rows), "newly_breached": 0,
           "cascade_flagged_tasks": 0, "failures": 0}
    # CPM Part E (2026-05-15) -- track unique project_ids that had a
    # newly-breached submittal so we can fire auto_recompute once per
    # project at the end of the scan rather than once per submittal
    # (could be hundreds in a tenant-wide breach scan).
    breached_projects: set = set()
    # CPM Part K-3 (2026-05-15) -- track submittals that K-2 just
    # auto-suggested impacts on. After the batch recompute fires per
    # project, we walk this list to stamp the 5 verdict keys (which
    # require fresh total_float_days from the recompute).
    k2_stamped_submittals: list[tuple[str, str]] = []  # (sid, project_id)
    for r in rows:
        try:
            sid = r["id"]
            due = r.get("a_e_review_due_at")
            try:
                due_dt = datetime.fromisoformat(
                    str(due).replace("Z", "+00:00"),
                )
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=timezone.utc)
                overdue_days = max(0, (now - due_dt).days)
            except Exception:
                overdue_days = None
            await db.submittals.update_one(
                {"id": sid, "tenant_id": tenant_id},
                {"$set": {
                    "sla_status": "breached",
                    "is_overdue": True,
                    "overdue_days": overdue_days,
                    "sla_breach_reason": (
                        f"a_e_review_due_at passed; overdue by "
                        f"{overdue_days} day(s)"
                    ),
                }},
            )
            await _persist_review_event(
                tenant_id=tenant_id,
                project_id=r.get("project_id") or "",
                submittal_id=sid, event_type="sla_breached",
                performed_by=None,
                notes=f"Auto-detected breach at {now_iso}; "
                      f"overdue by {overdue_days} day(s)",
                status_before=r.get("status"),
                status_after=r.get("status"),
                performed_at=now,
            )
            out["newly_breached"] += 1
            if r.get("project_id"):
                breached_projects.add(r["project_id"])
            # Phase 4d -- cascade flag linked tasks via gating_entities.
            cascade_count = await _cascade_flag_on_breach(
                tenant_id=tenant_id, project_id=r.get("project_id") or "",
                submittal_id=sid,
                schedule_activity_id=r.get("schedule_activity_id"),
                gating_entity_id=r.get("gating_entity_id"),
                actor_id=actor_id, request_id=request_id,
            )
            out["cascade_flagged_tasks"] += cascade_count
            # CPM Part K-2 (2026-05-15) -- HYBRID auto-suggest. The row
            # was JUST flipped to sla_status=breached (above), so we
            # call the helper with only_when_breached=False to skip
            # the re-read sla_status check (we know we're at the
            # breach moment). The helper itself re-reads the row from
            # db so it picks up the freshly-written breached state +
            # current aging fields.
            try:
                k2_res = await auto_suggest_per_task_impacts_for_submittal(
                    tenant_id=tenant_id, submittal_id=sid,
                    only_when_breached=False,
                    actor_id=actor_id, request_id=request_id,
                )
                if k2_res.get("changed"):
                    out.setdefault("auto_suggested_count", 0)
                    out["auto_suggested_count"] += len(k2_res.get("suggested") or [])
                    out.setdefault("cleaned_orphan_count", 0)
                    out["cleaned_orphan_count"] += len(k2_res.get("cleaned") or [])
                    # CPM Part K-3 -- queue this submittal for the
                    # post-recompute verdict-stamping pass. Only when
                    # K-2 actually suggested entries (cleaned-only
                    # cases don't need new stamps because cleaned
                    # entries are removed, not replaced).
                    if k2_res.get("suggested") and r.get("project_id"):
                        k2_stamped_submittals.append((sid, r["project_id"]))
            except Exception as exc:
                log.warning(
                    "K-2 auto-suggest after breach failed for sub=%s: %s",
                    sid, exc,
                )
            try:
                await write_audit(
                    tenant_id=tenant_id, table_name="submittals",
                    op="SUBMITTAL_SLA_BREACHED",
                    record_id=sid,
                    before={"sla_status": "on_track or approaching"},
                    after={"sla_status": "breached",
                            "overdue_days": overdue_days,
                            "due_at": due,
                            "cascade_flagged_tasks": cascade_count},
                    actor_id=actor_id, request_id=request_id,
                )
            except Exception:
                pass
            invalidate_aging_cache(
                tenant_id=tenant_id, project_id=r.get("project_id"),
            )
        except Exception as exc:
            out["failures"] += 1
            log.warning("breach detect failed for %s: %s",
                         r.get("id"), exc)

    # CPM Part E (2026-05-15) -- fire auto-recompute once per project
    # that had a newly-breached submittal. The recompute refreshes
    # total_float_days + projected_finish_date so the Tasks UI shows
    # the cascade impact without waiting for a manual button click.
    # Fail-safe: a CPM failure on one project does NOT abort the
    # rest of the breach scan.
    if breached_projects:
        try:
            from services.cpm import auto_recompute_after_event
            for pid in breached_projects:
                try:
                    await auto_recompute_after_event(
                        tenant_id, pid,
                        actor_id=actor_id, request_id=request_id,
                    )
                except Exception as exc:
                    log.warning(
                        "Part E auto-recompute after breach detection "
                        "failed (project=%s): %s", pid, exc,
                    )
        except Exception:
            pass

    # CPM Part K-3 (2026-05-15) -- verdict-stamping pass. Runs AFTER
    # the batch recompute so each submittal's resolver reads fresh
    # total_float_days (refreshed by auto_recompute_after_event
    # above). For each submittal K-2 just suggested impacts on, we
    # stamp the 5 server-only verdict keys (float_at_logging_days,
    # prior_open_impacts_at_logging_days, remaining_float_at_logging_
    # days, consumes_float_only, pushes_critical_path). The K-1
    # `source` field rides through unchanged. Fail-safe per row.
    k3_stamped_count = 0
    for sid, pid in k2_stamped_submittals:
        try:
            res = await _resolve_and_persist_submittal_per_task_impacts(
                tenant_id=tenant_id, submittal_id=sid,
                project_id=pid, exclude_submittal_id=sid,
            )
            if res.get("changed"):
                k3_stamped_count += 1
        except Exception as exc:
            log.warning(
                "K-3 verdict stamping after breach failed for sub=%s: %s",
                sid, exc,
            )
    if k3_stamped_count:
        out["k3_verdict_stamped_count"] = k3_stamped_count
    return out


# =====================================================================
# CPM Part K-2 (2026-05-15) -- HYBRID auto-suggest per_task_impacts on
# SLA breach.
#
# Fires ONLY at the just-now-crossed-into-breached transition (caller
# is responsible for the boundary detection). Builds per_task_impacts
# entries for the submittal's currently-linked task using days-past-SLA
# in the project's aging mode (working vs calendar) as the projected
# impact days. Source is stamped "system_suggested" so downstream UI
# (Part K-6) can pin a "suggested" pill that the super can override
# (flipping source to "super_overridden").
#
# Skip semantics on re-breach:
#   * super_overridden entries are NEVER touched (preserve super
#     judgment across breach <-> unbreach <-> rebreach cycles)
#   * system_suggested entries are NEVER refreshed (the original
#     suggestion stays; re-breach is not a license to overwrite the
#     prior estimate; super still has final word)
#   * Only tasks with NO existing entry get a fresh auto-suggestion
#
# Cleanup semantics:
#   * Entries whose task_id is no longer the linked task (e.g., super
#     changed schedule_activity_id while submittal was over SLA) are
#     dropped from the array -- captured in the audit payload as
#     "cleaned_orphans" so the federal auditor sees why an entry
#     disappeared.
#
# Audit: SUBMITTAL_IMPACT_AUTO_SUGGESTED carries the full before/after
# array + suggested / skipped_super_overridden / skipped_already_
# suggested / cleaned_orphans / sla_days / aging_metric_used / linked_
# task_id so a TIA defensibility audit can reconstruct the heuristic
# exactly.
# =====================================================================

async def auto_suggest_per_task_impacts_for_submittal(
    *, tenant_id: str, submittal_id: str,
    only_when_breached: bool = True,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Idempotent re-sync of submittal.per_task_impacts.

    Args:
      only_when_breached: When True (default), the helper SKIPS rows
        whose sla_status is not 'breached'. Set False from callers
        that have already confirmed the breach (e.g., detect_breaches
        inside the per-submittal loop where the row was JUST flipped
        to breached and the in-DB read reflects that).

    Returns:
      {ok, changed, suggested: [...], cleaned: [...],
       skipped_super_overridden: [task_id...],
       skipped_already_suggested: [task_id...],
       linked_task_id, aging_metric_used,
       projected_impact_days_default, sla_days, reason?}

    Behavior:
      * No-op when submittal not found, not breached (and gate is
        enabled), or when the current state already matches expected
        (no diff vs. existing per_task_impacts).
      * Fires SUBMITTAL_IMPACT_AUTO_SUGGESTED audit only when changed.
      * Caller is responsible for the auto_recompute_after_event hook
        (detect_breaches batches this once per project; the recompute
        path calls it for the single project).
    """
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(tenant_id, {"id": submittal_id}), {"_id": 0},
    )
    if not sub:
        return {"ok": False, "reason": "not_found"}
    if only_when_breached and sub.get("sla_status") != "breached":
        return {"ok": True, "changed": False, "reason": "not_breached"}

    project_id = sub.get("project_id") or ""
    project = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "submittal_review_sla_days": 1,
         "submittal_sla_uses_calendar_days": 1},
    ) or {}

    # Resolve SLA mode (project default + per-submittal override) and
    # SLA day count (per-submittal override takes precedence).
    uses_calendar = bool(project.get("submittal_sla_uses_calendar_days", False))
    if sub.get("sla_uses_calendar_days_override") is not None:
        uses_calendar = bool(sub["sla_uses_calendar_days_override"])
    sla_days = int(
        sub.get("sla_days_override")
        or project.get("submittal_review_sla_days")
        or SUBMITTAL_DEFAULT_SLA_DAYS
    )

    # Aging-past-SLA in the project's preferred unit. Mirrors the
    # overdue_days calculation already in this module (line 491) but
    # uses aging_working_days when the project runs working-day SLAs
    # (the ICE Schedule Ops Architecture default). At the breach
    # moment this is typically 1 (just crossed) and grows daily.
    if uses_calendar:
        aging_value = int(sub.get("aging_days") or 0)
        aging_metric = "aging_days"
    else:
        aging_value = int(sub.get("aging_working_days") or 0)
        aging_metric = "aging_working_days"
    projected_impact_default = max(0, aging_value - sla_days)

    # Currently linked task set. Submittals have a singular
    # schedule_activity_id (not a list), so the set is 0 or 1 entries.
    # Future-proofing: this code uses set logic so multi-task submittals
    # would Just Work if the schema ever extends.
    linked_task = sub.get("schedule_activity_id")
    linked_set: set = {linked_task} if linked_task else set()

    current_impacts = list(sub.get("per_task_impacts") or [])
    new_impacts: list[dict] = []
    suggested: list[dict] = []
    skipped_overridden: list[str] = []
    skipped_already_suggested: list[str] = []
    cleaned: list[dict] = []
    seen_task_ids: set = set()

    for entry in current_impacts:
        if not isinstance(entry, dict):
            cleaned.append({"reason": "non_dict_entry", "entry": entry})
            continue
        tid = entry.get("task_id")
        if not tid:
            cleaned.append({"reason": "missing_task_id", "entry": entry})
            continue
        if tid not in linked_set:
            # Task was unlinked from this submittal while over SLA --
            # drop the orphaned impact entry.
            cleaned.append({
                "task_id": tid, "reason": "task_no_longer_linked",
                "dropped_entry": entry,
            })
            continue
        source = entry.get("source")
        if source == "super_overridden":
            skipped_overridden.append(tid)
        elif source == "system_suggested":
            skipped_already_suggested.append(tid)
        # Keep the entry as-is (super_overridden or already-suggested
        # both ride through unchanged on re-breach).
        new_impacts.append(entry)
        seen_task_ids.add(tid)

    # Auto-suggest a fresh entry for each linked task with no entry yet.
    # CPM Part K-6 (2026-05-15) -- also stamp `original_suggested_days`
    # so the frontend hybrid-mode UI can:
    #   (a) show "was X auto-suggested" tooltip when super edits the
    #       value (source flips to super_overridden, but the original
    #       value rides through via this field)
    #   (b) offer a "Reset to suggestion" button on super_overridden
    #       entries that restores projected_impact_days = original_
    #       suggested_days + flips source back to system_suggested
    # The K-1 validator preserves unknown fields via dict(item), and
    # K-3 verdict-stamping preserves them too -- this field round-trips
    # unchanged. Federal audit trail benefits: the auto-suggested
    # baseline is preserved server-side without needing audit_log lookups.
    for tid in linked_set:
        if tid in seen_task_ids:
            continue
        new_entry = {
            "task_id": tid,
            "projected_impact_days": projected_impact_default,
            "source": "system_suggested",
            "original_suggested_days": projected_impact_default,
        }
        suggested.append(new_entry)
        new_impacts.append(new_entry)
        seen_task_ids.add(tid)

    changed = bool(suggested or cleaned)
    if changed:
        await db.submittals.update_one(
            {"id": submittal_id, "tenant_id": tenant_id},
            {"$set": {"per_task_impacts": new_impacts}},
        )
        invalidate_aging_cache(tenant_id=tenant_id, project_id=project_id)
        try:
            await write_audit(
                tenant_id=tenant_id, table_name="submittals",
                op="SUBMITTAL_IMPACT_AUTO_SUGGESTED",
                record_id=submittal_id,
                before={"per_task_impacts": current_impacts},
                after={
                    "per_task_impacts": new_impacts,
                    "suggested": suggested,
                    "skipped_super_overridden": skipped_overridden,
                    "skipped_already_suggested": skipped_already_suggested,
                    "cleaned_orphans": cleaned,
                    "linked_task_id": linked_task,
                    "aging_metric_used": aging_metric,
                    "aging_value": aging_value,
                    "sla_days": sla_days,
                    "projected_impact_days_default": projected_impact_default,
                    "sla_uses_calendar_days": uses_calendar,
                },
                actor_id=actor_id, request_id=request_id,
            )
        except Exception as exc:
            log.warning(
                "K-2 audit write failed for sub=%s: %s",
                submittal_id, exc,
            )

    return {
        "ok": True, "changed": changed,
        "suggested": suggested, "cleaned": cleaned,
        "skipped_super_overridden": skipped_overridden,
        "skipped_already_suggested": skipped_already_suggested,
        "linked_task_id": linked_task,
        "aging_metric_used": aging_metric,
        "projected_impact_days_default": projected_impact_default,
        "sla_days": sla_days,
    }


# =====================================================================
# CPM Part K-3 (2026-05-15) -- Submittal per_task_impacts verdict
# resolver. Mirror of routers.docs_router._resolve_rfi_task_impacts
# (Part J-2) but for submittals.
#
# Stamps the SAME 5 server-only verdict keys on each per_task_impacts
# entry that the RFI + delay flows do:
#
#     float_at_logging_days
#     prior_open_impacts_at_logging_days
#     remaining_float_at_logging_days
#     consumes_float_only
#     pushes_critical_path
#
# K-1 SOURCE FIELD PRESERVATION (HARD CONTRACT):
#   The K-1 schema added a REQUIRED user-supplied `source` field to
#   each entry (system_suggested | super_overridden). The verdict-
#   stamping pass MUST NOT touch `source` -- it preserves what came
#   in. After stamping, each entry carries 8 keys:
#
#       task_id                              (preserved)
#       projected_impact_days                (preserved, coerced to int)
#       source                               (PRESERVED -- K-1 / K-2 owned)
#       float_at_logging_days                (stamped)
#       prior_open_impacts_at_logging_days   (stamped)
#       remaining_float_at_logging_days      (stamped)
#       consumes_float_only                  (stamped)
#       pushes_critical_path                 (stamped)
#
# PRIOR-OPEN SCOPE (K-5 LIVE):
#   Calls routers.field_events_router._sum_prior_open_blocker_impacts_
#   on_task which walks db.delays + db.rfis + db.submittals (K-5
#   landed the submittal cross-walk). Submittals on the same task now
#   contribute to each other's prior-sum, modulo the exclude_submittal_
#   id self-exclusion threaded below.
#
# exclude_submittal_id is plumbed through the signature (matches J-2's
# exclude_rfi_id pattern) AND threaded into the prior-sum helper call.
# Defaults to sub_doc.id so a post-insert / post-PATCH re-resolve on
# the same submittal doesn't count its own projected_impact_days
# toward the prior-sum.
# =====================================================================
async def _resolve_submittal_task_impacts(
    db, tenant_id: str, sub_doc: dict, *,
    project_id: Optional[str] = None,
    exclude_submittal_id: Optional[str] = None,
) -> tuple[bool, list[dict]]:
    """Compute + stamp the 5 server-only verdict keys on each
    per_task_impacts entry in `sub_doc`. Returns (changed, enriched).

    `changed` is True iff any item in the returned list differs from
    the input in any of the 5 stamped keys -- callers can short-
    circuit the DB write when nothing moved (mirrors J-2 idempotency).

    Args:
        db: motor database handle.
        tenant_id: required.
        sub_doc: dict with per_task_impacts. Reads project_id +
            id for prior-sum scope + self-exclusion when the kwargs
            are not provided.
        project_id: kwarg override (defaults to sub_doc.project_id).
            Required because the prior-sum query is project-scoped.
        exclude_submittal_id: kwarg override (defaults to sub_doc.id).
            Plumbed for K-5 (submittal walk in prior-sum). Unused in
            K-3 itself -- the prior-sum helper does not accept an
            exclude_submittal_id kwarg until K-5.

    Returns:
        (changed: bool, enriched: list[dict])
    """
    impacts_in = list(sub_doc.get("per_task_impacts") or [])
    if not impacts_in:
        # No impacts on this submittal -- nothing to stamp. Mirrors
        # J-2 short-circuit so callers can skip the DB write.
        return (False, [])

    proj_id = project_id or sub_doc.get("project_id")
    # K-3: exclude_submittal_id defaults to sub_doc.id so a re-resolve
    # on the same submittal doesn't count its own impact_days toward
    # the prior-sum when K-5 wires submittal-on-submittal coverage.
    excl_sub = (
        exclude_submittal_id
        if exclude_submittal_id is not None
        else sub_doc.get("id")
    )

    task_ids = [i["task_id"] for i in impacts_in if i.get("task_id")]
    if not task_ids:
        return (False, [])

    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"id": {"$in": task_ids}}),
        {"_id": 0, "id": 1, "total_float_days": 1, "is_critical_path": 1},
    ).to_list(len(task_ids))
    by_id = {t["id"]: t for t in tasks}

    # Local import to avoid a circular-import at module load
    # (routers.field_events_router imports from services.* itself).
    # Same pattern as J-2 / Part I in docs_router + field_events_router.
    from routers.field_events_router import _sum_prior_open_blocker_impacts_on_task

    out: list[dict] = []
    changed = False
    for item in impacts_in:
        tid = item.get("task_id")
        if not tid:
            # K-1 validator drops items without task_id, but defensive
            # guard here too -- legacy rows shouldn't reach this path
            # but skip silently rather than 422 if they do.
            continue
        t = by_id.get(tid) or {}
        impact = float(item.get("projected_impact_days") or 0)
        # CPM Bug 3.B (2026-05-16) -- total_float_days is a WORKING-DAY
        # count (compute_cpm: working_days_between(ES, LS)). impact is
        # working days. Verdict below is units-consistent: working-day
        # impact vs working-day remaining float.
        f = t.get("total_float_days")
        f_val = float(f) if f is not None else None
        is_cp = bool(t.get("is_critical_path"))

        if proj_id is not None and f_val is not None:
            # CPM Part K-5 (2026-05-15) -- prior-sum now walks delays
            # + RFIs + submittals (db.submittals with sla_status=
            # breached). Pass exclude_submittal_id so THIS submittal's
            # own impacts don't count as prior to itself when we re-
            # resolve post-insert / post-PATCH. The other two excludes
            # stay None -- a submittal is not a delay or RFI, so
            # nothing to exclude on those collections.
            prior = await _sum_prior_open_blocker_impacts_on_task(
                db, tenant_id, proj_id, tid,
                exclude_delay_id=None,
                exclude_rfi_id=None,
                exclude_submittal_id=excl_sub,
            )
            # Clamp at 0 -- can't have negative float in the verdict.
            remaining = max(0.0, f_val - prior)
            consumes = impact <= remaining
            pushes_cp = impact > remaining
        else:
            # Fallback: structural float unknown (compute_cpm hasn't
            # run on this project) OR project_id resolution failed.
            # Mirrors I/J-2 fallback so the snapshot is well-formed
            # in transient states.
            prior = 0.0
            remaining = None
            consumes = False
            pushes_cp = is_cp

        # ============================================================
        # K-1 SOURCE FIELD: must ride through unchanged. dict(item)
        # copies every key (including `source` and any future ones);
        # we then OVERWRITE task_id / projected_impact_days / the 5
        # verdict keys. `source` is NEVER on the overwrite list and
        # is preserved exactly as the K-1 validator accepted it.
        # ============================================================
        merged = dict(item)
        merged["task_id"] = tid
        # Integer convention (K-1 validator enforced this at the API
        # layer; int() here is defense-in-depth for legacy rows that
        # may have stored 3.0 instead of 3).
        merged["projected_impact_days"] = int(impact)
        merged["float_at_logging_days"] = f_val
        merged["prior_open_impacts_at_logging_days"] = prior
        merged["remaining_float_at_logging_days"] = remaining
        merged["consumes_float_only"] = consumes
        merged["pushes_critical_path"] = pushes_cp
        # `source` rides through via dict(item) -- explicit comment so
        # a future refactor doesn't accidentally strip it. If `source`
        # is somehow absent on a legacy row (pre-K), it stays absent
        # here (the read model + K-1 validator will tolerate it on
        # the next deserialization pass).
        out.append(merged)

        # Change detector -- True iff any of the 5 stamped keys
        # differs from what the input carried. Empty/missing keys
        # on input (typical fresh user-supplied items) always count
        # as changed since None != f_val for any populated float.
        if (
            item.get("float_at_logging_days") != f_val
            or item.get("prior_open_impacts_at_logging_days") != prior
            or item.get("remaining_float_at_logging_days") != remaining
            or item.get("consumes_float_only") != consumes
            or item.get("pushes_critical_path") != pushes_cp
        ):
            changed = True

    return (changed, out)


async def _resolve_and_persist_submittal_per_task_impacts(
    *, tenant_id: str, submittal_id: str,
    project_id: Optional[str] = None,
    exclude_submittal_id: Optional[str] = None,
) -> dict:
    """Thin orchestration wrapper: read submittal -> resolve via K-3 ->
    persist if changed. Used by detect_breaches (post-batch-recompute
    second pass), recompute_aging_for_submittal (post-CPM-recompute),
    and update_submittal (when per_task_impacts touched).

    Returns:
        {ok, changed, reason?}. Fail-safe: any exception is caught
        and reported as ok=False with the exception class in reason.
    """
    db = get_db()
    try:
        sub = await db.submittals.find_one(
            tenant_filter(tenant_id, {"id": submittal_id}),
            {"_id": 0},
        )
        if not sub:
            return {"ok": False, "reason": "not_found"}
        if not sub.get("per_task_impacts"):
            return {"ok": True, "changed": False, "reason": "no_impacts"}
        changed, stamped = await _resolve_submittal_task_impacts(
            db, tenant_id, sub,
            project_id=project_id or sub.get("project_id"),
            exclude_submittal_id=exclude_submittal_id or submittal_id,
        )
        if changed:
            await db.submittals.update_one(
                {"id": submittal_id, "tenant_id": tenant_id},
                {"$set": {"per_task_impacts": stamped}},
            )
            invalidate_aging_cache(
                tenant_id=tenant_id,
                project_id=sub.get("project_id"),
            )
        return {"ok": True, "changed": changed}
    except Exception as exc:
        log.warning(
            "K-3 resolve-and-persist failed for sub=%s: %s",
            submittal_id, exc,
        )
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


async def _cascade_flag_on_breach(
    *, tenant_id: str, project_id: str, submittal_id: str,
    schedule_activity_id: Optional[str],
    gating_entity_id: Optional[str],
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> int:
    """Phase 4d -- when a submittal breaches AND has a linked task,
    the cascade engine needs to know so downstream CPM analysis can
    reflect the slip. The submittal already has a gating_entities
    back-pointer (created by iter4_router); we surface the breach on
    that node via its sync_status helper if available, otherwise
    write an audit-only annotation on the gating entity."""
    if not gating_entity_id and not schedule_activity_id:
        return 0
    db = get_db()
    try:
        if gating_entity_id:
            await db.gating_entities.update_one(
                tenant_filter(tenant_id, {"id": gating_entity_id}),
                {"$set": {
                    "underlying_status": "breached",
                    "sla_breach_at": datetime.now(timezone.utc).isoformat(),
                    "submittal_id": submittal_id,
                }},
            )
            return 1
    except Exception as exc:
        log.warning("cascade flag on breach failed for sub=%s: %s",
                    submittal_id, exc)
    return 0


# =====================================================================
# Aging summary (Phase 5 dashboard).
# =====================================================================

async def get_aging_summary(
    *, tenant_id: str, project_id: str, days: int = 90,
    bypass_cache: bool = False,
) -> dict:
    """Per-project aging dashboard data. Returns 5 buckets, per-
    discipline rollup, top 10 longest-open, must_approve_by miss
    count, breach trend over 30/60/90 days.

    Cached 5min per (tenant, project, days). Invalidated on every
    submittal mutation."""
    if days < 1:
        days = 1
    if days > 365:
        days = 365
    key = _aging_cache_key(
        tenant_id=tenant_id, project_id=project_id, days=days,
    )
    now_mono = _time.monotonic()
    if not bypass_cache:
        cached = _aging_cache.get(key)
        if cached and cached[0] > now_mono:
            return {**cached[1], "_cache_hit": True}

    db = get_db()
    # Open submittals (any non-terminal status).
    open_rows = await db.submittals.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$nin": ["approved", "approved_as_noted",
                                  "rejected", "closed"]},
        }),
        {"_id": 0, "id": 1, "submittal_number": 1, "title": 1,
         "discipline": 1, "aging_days": 1, "aging_working_days": 1,
         "sla_status": 1, "a_e_review_due_at": 1, "must_approve_by": 1,
         "urgency_flag": 1, "review_cycle_count": 1,
         "submitted_to_ae_at": 1, "is_overdue": 1, "overdue_days": 1,
         "type": 1, "assigned_reviewer_id": 1},
    ).to_list(length=None)

    buckets = {label: 0 for _, _, label in AGING_BUCKETS}
    by_discipline: dict[str, dict] = {}
    must_approve_by_misses = 0
    breach_count = 0
    for r in open_rows:
        a = r.get("aging_days")
        if a is None:
            continue
        for lo, hi, label in AGING_BUCKETS:
            if hi is None and a >= lo:
                buckets[label] += 1; break
            if hi is not None and lo <= a < hi:
                buckets[label] += 1; break
        disc = r.get("discipline") or "(none)"
        bd = by_discipline.setdefault(disc, {
            "discipline": disc, "open_count": 0,
            "breached_count": 0, "avg_aging_days": 0.0,
            "_aging_sum": 0,
        })
        bd["open_count"] += 1
        bd["_aging_sum"] += int(a or 0)
        if r.get("sla_status") == "breached":
            bd["breached_count"] += 1
            breach_count += 1
        if r.get("urgency_flag"):
            must_approve_by_misses += 1

    by_discipline_list = []
    for v in by_discipline.values():
        n = v["open_count"]
        v["avg_aging_days"] = round(
            (v["_aging_sum"] / n) if n else 0.0, 1,
        )
        v.pop("_aging_sum", None)
        by_discipline_list.append(v)
    by_discipline_list.sort(key=lambda x: -x["open_count"])

    top_10 = sorted(
        open_rows, key=lambda r: -(r.get("aging_days") or 0),
    )[:10]

    # Breach trend over the requested window.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    breach_events = await db.submittal_review_events.find(
        {"tenant_id": tenant_id, "project_id": project_id,
         "event_type": "sla_breached",
         "performed_at": {"$gte": cutoff}},
        {"_id": 0, "performed_at": 1},
    ).to_list(length=None)
    # Bucket by 7-day rolling windows so the chart has data points.
    trend_buckets: dict[str, int] = {}
    for ev in breach_events:
        d = (ev.get("performed_at") or "")[:10]
        if d:
            trend_buckets[d] = trend_buckets.get(d, 0) + 1
    trend = sorted(
        ({"date": d, "count": c} for d, c in trend_buckets.items()),
        key=lambda x: x["date"],
    )

    out = {
        "project_id": project_id,
        "window_days": days,
        "open_count": len(open_rows),
        "breach_count": breach_count,
        "must_approve_by_misses": must_approve_by_misses,
        "buckets": [
            {"label": label, "count": buckets[label]}
            for _, _, label in AGING_BUCKETS
        ],
        "by_discipline": by_discipline_list,
        "top_10_longest_open": top_10,
        "breach_trend": trend,
        "_cache_hit": False,
    }
    _aging_cache[key] = (now_mono + AGING_CACHE_TTL_SECONDS, out)
    _evict_aging_cache_if_needed()
    return out
