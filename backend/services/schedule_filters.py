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
