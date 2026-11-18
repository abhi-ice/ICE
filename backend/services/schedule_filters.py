"""Shared slip-aware schedule-window filter (pt.72).

Every "what's scheduled when" surface — the DR "Today's Planned Activity" grid,
Lookahead, Resource Leveling demand, and the task look-ahead export — must read a
task's EFFECTIVE window: projected_start_date / projected_finish_date when a blocker
has SLIPPED the task, else the baseline planned_start / planned_finish. (projected_*
are written together and cleared to None when there's no slip, so the $ifNull falls
back to planned cleanly.) Centralised here so the rule cannot drift across call sites.
All dates are 'YYYY-MM-DD' strings, so the lexicographic $lte/$gte compare is exact.
"""
from typing import Optional


def effective_schedule_overlap(start_iso: str, end_iso: str) -> dict:
    """Mongo ``$expr`` matching tasks whose EFFECTIVE window overlaps the
    inclusive [start_iso, end_iso] range. Use as
    ``{"$expr": effective_schedule_overlap(a, b)}``. A single-day
    contains-check is just ``overlap(day, day)``. Both effective dates must be
    non-null — mirrors the prior planned_* field-match, which silently skipped
    tasks with missing dates."""
    es = {"$ifNull": ["$projected_start_date", "$planned_start"]}
    ef = {"$ifNull": ["$projected_finish_date", "$planned_finish"]}
    return {"$and": [
        {"$ne": [es, None]},
        {"$ne": [ef, None]},
        {"$lte": [es, end_iso]},
        {"$gte": [ef, start_iso]},
    ]}


def effective_start(task: dict) -> Optional[str]:
    """A task's effective start date string (projected if slipped, else planned)."""
    return task.get("projected_start_date") or task.get("planned_start")


def effective_finish(task: dict) -> Optional[str]:
    """A task's effective finish date string (projected if slipped, else planned)."""
    return task.get("projected_finish_date") or task.get("planned_finish")


# Overdue unfinished work (2026-11-16). A task whose finish is already BEFORE
# the window start, but which is still not done, used to drop out of the
# look-ahead, its export and the daily report activity picker entirely -- so
# the late work nobody finished vanished from view. These helpers keep such
# tasks in those lists and let callers flag them overdue. Deleted and
# completed tasks stay excluded.
#
# "Overdue" is judged on the PLANNED finish plus actual progress, never on
# the projected dates: the impact-days forecast moves every unfinished task
# to the data date or later, so a projected finish is never in the past
# right after a recompute, and an overdue flag read from it would come and
# go depending on whether anything triggered a recompute that day. A task
# whose planned finish has passed and is not complete is overdue, even when
# it has been forecast (or slipped) into the window.
_DONE_STATUSES = ["complete", "deleted"]

# Only field work is pulled into a list because it is overdue. Paperwork
# (submittals, procurement, closeout documents) is tracked on its own pages;
# on a real schedule hundreds of old paperwork items are open past their
# planned finish, and pulling them in buried the field work in the
# look-ahead. Tasks with no work category yet count as field work, so
# nothing untagged disappears. Paperwork whose own dates fall inside the
# window is still listed as before.
PAPERWORK_CATEGORIES = ["preconstruction_paperwork", "construction_paperwork", "closeout_paperwork",
                        "contract_administration"]


def overdue_unfinished(start_iso: str, end_iso: str) -> dict:
    """Mongo ``$expr`` matching field-work tasks that are overdue relative to
    the window [start_iso, end_iso]: planned finish before start_iso, status
    not complete/deleted, percent complete under 100 (a missing percent
    counts as 0), and not a paperwork category (see PAPERWORK_CATEGORIES).
    end_iso is kept for the callers' symmetry."""
    return {"$and": [
        {"$not": [{"$in": [{"$ifNull": ["$work_category", ""]}, PAPERWORK_CATEGORIES]}]},
        {"$ne": [{"$ifNull": ["$planned_finish", None]}, None]},
        {"$lt": ["$planned_finish", start_iso]},
        {"$not": [{"$in": [{"$ifNull": ["$status", ""]}, _DONE_STATUSES]}]},
        {"$lt": [{"$ifNull": ["$pct_complete", 0]}, 100]},
    ]}


def window_or_overdue(start_iso: str, end_iso: str) -> dict:
    """Mongo ``$expr`` for the look-ahead style lists: tasks whose effective
    window overlaps [start_iso, end_iso], plus overdue unfinished tasks (see
    overdue_unfinished). Use as ``{"$expr": window_or_overdue(a, b)}``."""
    return {"$or": [
        effective_schedule_overlap(start_iso, end_iso),
        overdue_unfinished(start_iso, end_iso),
    ]}


def _iso(value) -> Optional[str]:
    """Date or 'YYYY-MM-DD...' string -> 'YYYY-MM-DD' (None stays None)."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    return str(value)[:10] or None


def is_overdue(task: dict, start_iso: str) -> bool:
    """Python mirror of overdue_unfinished for marking rows already fetched:
    True when the task is field work (or untagged), its planned finish is
    before start_iso, and it is not complete, deleted, or at 100%."""
    if task.get("work_category") in PAPERWORK_CATEGORIES:
        return False
    ef = _iso(task.get("planned_finish"))
    if not ef or ef >= start_iso:
        return False
    if (task.get("status") or "") in _DONE_STATUSES:
        return False
    try:
        pct = float(task.get("pct_complete") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    return pct < 100


# Ongoing overhead (2026-11-18). An activity that runs the whole project --
# general conditions, supervision, project management, site safety -- is
# "level of effort": it belongs in the schedule (cost, billing, forecasts)
# but not in a two-week look-ahead, which shows the field work coming up.
# Recognised by its dates alone, never by its name: it spans at least
# OVERHEAD_SHARE of the project's whole schedule (earliest planned start to
# latest planned finish of the schedule's activities) and at least
# OVERHEAD_MIN_DAYS calendar days.
OVERHEAD_SHARE = 0.85
OVERHEAD_MIN_DAYS = 60


def _as_date(v):
    from datetime import date, datetime
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def overhead_ids_from(tasks) -> set:
    """Ids of the ongoing-overhead activities among `tasks` (each with id,
    planned_start, planned_finish). Pure: no database."""
    spans = []
    for t in tasks:
        s, f = _as_date(t.get("planned_start")), _as_date(t.get("planned_finish"))
        if s and f and f >= s:
            spans.append((t.get("id"), s, f))
    if not spans:
        return set()
    p0 = min(s for _, s, _ in spans)
    p1 = max(f for _, _, f in spans)
    project_days = (p1 - p0).days
    if project_days <= 0:
        return set()
    return {i for i, s, f in spans
            if (f - s).days >= OVERHEAD_MIN_DAYS and (f - s).days >= OVERHEAD_SHARE * project_days}


async def ongoing_overhead_ids(db, base_filter: dict) -> set:
    """overhead_ids_from() over the schedule activities matching base_filter
    (one project). Unplanned field work is not part of the schedule."""
    rows = await db.tasks.find(
        {**base_filter, "created_in_field": {"$ne": True}},
        {"_id": 0, "id": 1, "planned_start": 1, "planned_finish": 1},
    ).to_list(20000)
    return overhead_ids_from(rows)


async def find_window_and_overdue(db, base_filter: dict, start_iso: str, end_iso: str,
                                  window_limit: int = 500, overdue_limit: int = 200):
    """Look-ahead rows for [start_iso, end_iso] in two capped queries, so a
    flood of overdue rows can never push in-window work past the cap:
      1. tasks whose effective window overlaps the dates (up to
         window_limit, planned-start order);
      2. overdue unfinished tasks NOT already in (1) (up to overdue_limit,
         oldest planned finish first).
    Returns (rows, window_ids): every row carries `overdue`; overdue rows
    come first (planned-start order), then the rest in planned-start order.
    window_ids are the ids found by the window query (1)."""
    # Unplanned field work is not part of the schedule (2026-11-18).
    base_filter = {**base_filter, "created_in_field": {"$ne": True}}
    # Contract administration (bonds, insurance, notice to proceed...) and
    # ongoing overhead (general conditions and the like) are not look-ahead work.
    base_filter = {**base_filter, "work_category": {"$ne": "contract_administration"}}
    overhead = await ongoing_overhead_ids(db, base_filter)
    if overhead:
        base_filter = {**base_filter, "id": {"$nin": list(overhead)}}
    window = await db.tasks.find(
        {**base_filter, "$expr": effective_schedule_overlap(start_iso, end_iso)},
        {"_id": 0},
    ).sort("planned_start", 1).to_list(window_limit)
    window_ids = {d["id"] for d in window}
    extra = await db.tasks.find(
        {**base_filter, "id": {"$nin": list(window_ids | overhead)},
         "$expr": overdue_unfinished(start_iso, end_iso)},
        {"_id": 0},
    ).sort("planned_finish", 1).to_list(overdue_limit)
    rows = window + extra
    for d in rows:
        d["overdue"] = is_overdue(d, start_iso)
    late = sorted((d for d in rows if d["overdue"]), key=lambda d: str(d.get("planned_start") or ""))
    rest = [d for d in rows if not d["overdue"]]
    return late + rest, window_ids
