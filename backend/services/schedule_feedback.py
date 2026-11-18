"""One-way schedule feedback from Daily Reports.

* recompute_task_actuals -- actual start/finish derived from the DR rows that
  still exist (first worked day / first day at 100%).
* open_delays_for_task   -- open/in-progress delays that block adding a task
  to a DR until resolved.
* logic proposals        -- out-of-order work and unplanned work create
  PENDING relationship-change proposals (AI-drafted) for admin approval.
  Nothing here ever edits predecessors on its own.
* activity name suggestions -- heading-based names plus AI for duplicates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date
from typing import Optional

from database import tenant_filter
from services.llm_keys import llm_api_key
from models import gen_id, utc_now, pred_code, pred_relationship

log = logging.getLogger(__name__)

OPEN_DELAY_STATUSES = ["open", "in_progress"]
PAPERWORK = {"preconstruction_paperwork", "construction_paperwork", "closeout_paperwork"}
_bg_tasks: set = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _d(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


# ---------------------------------------------------------------- indexes
HISTORY_INDEXES = [
    ("daily_report_activities", [("schedule_activity_id", 1)]),
    ("task_sync_events", [("tenant_id", 1), ("task_id", 1), ("triggered_at", 1)]),
]


async def ensure_history_indexes(db, create=None) -> None:
    """Indexes the per-task history lookups below rely on (2026-11-16):
    report rows by task, and a task's progress events in time order.
    Without them every lookup scans the whole collection. `create` is the
    server's logging index helper; by default create_index is called
    directly. Idempotent."""
    for coll, keys in HISTORY_INDEXES:
        if create is not None:
            await create(db[coll], keys)
        else:
            await db[coll].create_index(keys)


# ---------------------------------------------------------------- report history
def _row_has_report(r: dict) -> bool:
    """True when a report row actually records something about the task: it
    was saved by a person (last_sync_at), shows progress (end-of-day differs
    from start-of-day), logs hours, or sits at 100%. A row that was only
    added to a report and never touched carries no information, so it never
    decides the task's current progress."""
    eod = float(r.get("pct_complete_end_of_day") or 0)
    sod = float(r.get("pct_complete_start_of_day") or 0)
    return bool(r.get("last_sync_at")) or eod != sod or eod >= 100 \
        or float(r.get("hours_worked") or 0) > 0


def _row_sets_progress(r: dict) -> bool:
    """True when the row's end-of-day percent was entered by a person
    (pct_set_at, stamped when a save carries the end-of-day percent), or,
    for rows saved before that stamp existed, when its end-of-day differs
    from its start-of-day. A pre-filled row that was only opened for notes,
    crew or hours still counts for actual dates, but never decides the
    task's current progress (2026-11-16)."""
    if r.get("pct_set_at"):
        return True
    return float(r.get("pct_complete_end_of_day") or 0) != float(r.get("pct_complete_start_of_day") or 0)


def progress_rows(history: list) -> list:
    """The rows of a task_report_history list that decide progress."""
    return [r for r in history if _row_sets_progress(r)]


async def task_report_histories(db, tenant_id: str, task_ids: list,
                                exclude_dr_id: Optional[str] = None,
                                exclude_row_id: Optional[str] = None,
                                include_untouched: bool = False) -> dict:
    """task_report_history for many tasks with two queries in total
    ({task_id: history}; a task with no rows maps to []). include_untouched
    keeps rows that were added but never touched (see _row_has_report)."""
    out: dict = {tid: [] for tid in task_ids}
    if not task_ids:
        return out
    rows = await db.daily_report_activities.find(
        {"schedule_activity_id": {"$in": list(task_ids)}}, {"_id": 0},
    ).to_list(None)
    rows = [r for r in rows
            if r.get("daily_report_id") != exclude_dr_id and r.get("id") != exclude_row_id
            and (include_untouched or _row_has_report(r))]
    if not rows:
        return out
    drs = await db.daily_reports.find(
        tenant_filter(tenant_id, {"id": {"$in": list({r["daily_report_id"] for r in rows})}}),
        {"_id": 0, "id": 1, "report_date": 1, "status": 1},
    ).to_list(None)
    by_id = {d["id"]: d for d in drs}
    for r in rows:
        d = by_id.get(r["daily_report_id"])
        rd = _d((d or {}).get("report_date"))
        if not rd:
            continue
        out.setdefault(r["schedule_activity_id"], []).append(
            {**r, "_report_date": rd, "_report_status": d.get("status"), "_report_id": d["id"]})
    # Same-date rows (rare) fall back to the most recently saved one last.
    for hist in out.values():
        hist.sort(key=lambda r: (r["_report_date"], str(r.get("last_sync_at") or "")))
    return out


async def task_report_history(db, tenant_id: str, task_id: str,
                              exclude_dr_id: Optional[str] = None,
                              exclude_row_id: Optional[str] = None,
                              include_untouched: bool = False) -> list:
    """Every report row that records something about the task, oldest report
    date first. Each row carries `_report_date` (a date), `_report_status`
    and `_report_id`. Rows whose report no longer exists are left out.
    exclude_dr_id / exclude_row_id leave one report or one row out (used
    while that report or row is being deleted, or is the one being saved).
    Use progress_rows() on the result to get only the rows that decide the
    task's current progress."""
    return (await task_report_histories(db, tenant_id, [task_id], exclude_dr_id=exclude_dr_id,
                                        exclude_row_id=exclude_row_id,
                                        include_untouched=include_untouched))[task_id]


def _status_for_pct(pct: float) -> str:
    return "complete" if pct >= 100 else "in_progress" if pct > 0 else "not_started"


async def pre_report_progress(db, tenant_id: str, task_id: str,
                              history: Optional[list] = None) -> dict:
    """The task's progress as it stood before any daily report changed it.
    Source, best first: the "before" value of the earliest report write-back
    event (task_sync_events); else the start-of-day of the earliest
    remaining report row; else the task's current value."""
    out: dict = {}
    # One small query per field (earliest event that changed it), served by
    # the (tenant_id, task_id, triggered_at) index, instead of loading every
    # event the task ever had.
    for k in ("pct_complete", "status", "current_crew_id"):
        evs = await db.task_sync_events.find(
            tenant_filter(tenant_id, {"task_id": task_id, f"fields_updated.{k}": {"$exists": True}}),
            {"_id": 0, f"fields_updated.{k}": 1},
        ).sort("triggered_at", 1).limit(1).to_list(1)
        if evs:
            ev = evs[0]
            out[k] = ((ev.get("fields_updated") or {}).get(k) or {}).get("before")
    if "pct_complete" not in out or out["pct_complete"] is None:
        if history:
            out["pct_complete"] = history[0].get("pct_complete_start_of_day")
            out.pop("status", None)
        else:
            task = await db.tasks.find_one(
                tenant_filter(tenant_id, {"id": task_id}),
                {"_id": 0, "pct_complete": 1, "status": 1},
            ) or {}
            out["pct_complete"] = task.get("pct_complete")
            out.setdefault("status", task.get("status"))
    pct = float(out.get("pct_complete") or 0)
    out["pct_complete"] = pct
    if not out.get("status") or out["status"] == "deleted":
        out["status"] = _status_for_pct(pct)
    return out


async def current_progress_from_reports(db, tenant_id: str, task_id: str,
                                        exclude_dr_id: Optional[str] = None,
                                        exclude_row_id: Optional[str] = None) -> dict:
    """The task fields (pct_complete, status, last_synced_from_dr and, when
    known, current_crew_id) that the task should carry right now: the
    end-of-day of the LATEST-DATED report row, or the pre-report value when
    no report row is left. Back-dated reports never win over a later one.
    Used after a report or a single row is deleted."""
    history = progress_rows(await task_report_history(db, tenant_id, task_id,
                                                      exclude_dr_id=exclude_dr_id,
                                                      exclude_row_id=exclude_row_id))
    if not history:
        out = await pre_report_progress(db, tenant_id, task_id)
        out["last_synced_from_dr"] = None
        return out
    r = history[-1]
    pct = float(r.get("pct_complete_end_of_day") or 0)
    out = {
        "pct_complete": pct,
        "status": _status_for_pct(pct),
        "last_synced_from_dr": {
            "daily_report_id": r["_report_id"],
            "daily_report_date": r["_report_date"].isoformat(),
            "synced_at": utc_now().isoformat(),
            "sync_state": "provisional" if r.get("_report_status") == "draft" else "confirmed",
        },
    }
    if r.get("linked_crew_id"):
        out["current_crew_id"] = r["linked_crew_id"]
    return out


async def start_of_day_pct(db, tenant_id: str, task: dict, report_date,
                           history: Optional[list] = None) -> float:
    """Start-of-day percent for a new report row of `task` on `report_date`.
    A report at or after the task's latest report starts from the task's
    current progress (the normal next-day case; it also keeps any change
    made on the schedule since). A BACK-DATED report -- one dated before a
    report that already set the task's progress -- starts from the
    end-of-day of the most recent earlier-dated report row, else from the
    task's progress before any report touched it. Only rows whose
    end-of-day a person entered count (see _row_sets_progress). `history`
    (from task_report_histories) saves the lookup when already loaded."""
    rd = _d(report_date)
    if history is None:
        history = await task_report_history(db, tenant_id, task["id"])
    history = progress_rows(history)
    if not history or rd is None or rd >= history[-1]["_report_date"]:
        return float(task.get("pct_complete") or 0)
    earlier = [r for r in history if r["_report_date"] < rd]
    if earlier:
        return float(earlier[-1].get("pct_complete_end_of_day") or 0)
    return float((await pre_report_progress(db, tenant_id, task["id"], history))["pct_complete"])


# ---------------------------------------------------------------- start of day
async def refresh_start_of_day(db, tenant_id: str, task_id: str) -> int:
    """Keep each report row's start-of-day ("was N%") equal to the progress
    the task actually had going into that day (2026-11-16): the end-of-day
    of the most recent EARLIER-dated report row whose percent a person
    entered. Run whenever a report row's percent changes, a row or report is
    deleted, or a report is submitted, so correcting an older report (for
    example lowering 11/16 from 45% to 30%) also corrects the "was" shown on
    the following days. A row with no earlier progress row keeps its own
    start-of-day (it holds the value from before any report).

    Never changes which rows count as entered progress: an older row with no
    pct_set_at stamp that counts only because its end-of-day differs from its
    start-of-day is stamped first, and an untouched pre-filled row (end-of-day
    equal to its start-of-day) moves its end-of-day along with it so it stays
    untouched. Left alone: a start-of-day a person typed (sod_set_at) and
    untouched rows on submitted or approved reports. Each write only applies
    if the row was not saved since it was read. Rows on the same date all
    start from the previous date's value. Returns the number of rows changed."""
    history = await task_report_history(db, tenant_id, task_id, include_untouched=True)
    changed = 0
    # End-of-day carried into the current date: from any earlier entered row
    # (used for drafts), and from earlier SUBMITTED or APPROVED rows only
    # (used for locked reports, so an unsubmitted draft never rewrites what
    # a locked report says it started from).
    prev_any, prev_locked = None, None
    cur_date, cur_any, cur_locked = None, None, None
    pre_value = None
    for r in history:
        rd = r["_report_date"]
        if rd != cur_date:
            if cur_any is not None:
                prev_any = cur_any
            if cur_locked is not None:
                prev_locked = cur_locked
            cur_date, cur_any, cur_locked = rd, None, None
        sets = _row_sets_progress(r)
        locked = r.get("_report_status") in ("submitted", "approved")
        # Leave alone: a start-of-day a person typed, and untouched rows on
        # submitted or approved reports (their printed record stays as is).
        skip = bool(r.get("sod_set_at")) or (locked and not sets)
        target = prev_locked if locked else prev_any
        if locked and target is None and prev_any is not None:
            # A locked report whose only earlier rows are drafts starts from
            # the task's progress before any report touched it.
            if pre_value is None:
                pre_value = float((await pre_report_progress(db, tenant_id, task_id, history))
                                  .get("pct_complete") or 0)
            target = pre_value
        if target is not None and not skip:
            old_sod = float(r.get("pct_complete_start_of_day") or 0)
            if old_sod != float(target):
                upd = {"pct_complete_start_of_day": target}
                if sets and not r.get("pct_set_at"):
                    upd["pct_set_at"] = r.get("updated_at") or r.get("created_at") or utc_now().isoformat()
                if not sets:
                    upd["pct_complete_end_of_day"] = target
                # Only if nobody saved the row since it was read.
                flt = {"id": r["id"],
                       "pct_complete_start_of_day": r.get("pct_complete_start_of_day"),
                       "pct_complete_end_of_day": r.get("pct_complete_end_of_day"),
                       "pct_set_at": r.get("pct_set_at")}
                res = await db.daily_report_activities.update_one(flt, {"$set": upd})
                changed += res.modified_count
        if sets:
            eod = float(r.get("pct_complete_end_of_day") or 0)
            cur_any = eod if cur_any is None else max(cur_any, eod)
            if locked:
                cur_locked = eod if cur_locked is None else max(cur_locked, eod)
    return changed


# ---------------------------------------------------------------- actuals
async def recompute_task_actuals(db, tenant_id: str, task_id: str,
                                 refresh_sod: bool = True) -> dict:
    """actual_start = earliest report date the activity was worked (progress
    made, hours logged, or at 100%); actual_finish = the first report date it
    reached 100% with no later report back below 100%. Rows are read in
    report-date order and each row's start-of-day is taken as the previous
    report's end-of-day (the first row starts from the pre-report value), so
    a report entered out of date order is judged correctly. Imported
    (MS Project) actuals and actual dates set by hand (actuals_source
    "manual") are never overwritten."""
    try:
        if refresh_sod:
            await refresh_start_of_day(db, tenant_id, task_id)
    except Exception:  # never block the actual-date recompute on this
        logging.getLogger(__name__).warning("start-of-day refresh failed for task %s", task_id, exc_info=True)
    task = await db.tasks.find_one(
        tenant_filter(tenant_id, {"id": task_id}),
        # "id" is projected so a task with no actual-date fields yet still
        # comes back as a non-empty (truthy) document.
        {"_id": 0, "id": 1, "actual_start": 1, "actual_finish": 1, "actuals_source": 1},
    )
    if not task:
        return {}
    if task.get("actual_start") and task.get("actuals_source") != "daily_report":
        return {}
    # Actual dates a person set by hand on the schedule are kept too.
    if task.get("actuals_source") == "manual" and task.get("actual_finish"):
        return {}
    history = await task_report_history(db, tenant_id, task_id)
    worked, finish = [], None
    if history:
        first_sod = float(history[0].get("pct_complete_start_of_day") or 0)
        has_event = await db.task_sync_events.find_one(
            tenant_filter(tenant_id, {"task_id": task_id,
                                      "fields_updated.pct_complete": {"$exists": True}}),
            {"_id": 1},
        )
        if has_event:
            first_sod = (await pre_report_progress(db, tenant_id, task_id, history))["pct_complete"]
        prev_eod = first_sod
        for r in history:
            rd = r["_report_date"]
            # A row whose end-of-day nobody entered (opened only for notes,
            # crew or hours, or submitted untouched) carries the previous
            # value forward: its pre-filled percent may be stale, so only
            # its hours count as work.
            eod = (float(r.get("pct_complete_end_of_day") or 0) if _row_sets_progress(r)
                   else prev_eod)
            if eod > prev_eod or float(r.get("hours_worked") or 0) > 0 or eod >= 100:
                worked.append(rd)
            if eod >= 100:
                finish = finish or rd
            else:
                finish = None  # dropped back below 100: not finished yet
            prev_eod = eod
    a_start = min(worked).isoformat() if worked else None
    a_finish = finish.isoformat() if finish else None
    upd = {"actual_start": a_start, "actual_finish": a_finish,
           "actuals_source": "daily_report" if a_start else None}
    if (task.get("actual_start"), task.get("actual_finish")) != (a_start, a_finish):
        await db.tasks.update_one(tenant_filter(tenant_id, {"id": task_id}), {"$set": upd})
    return upd


async def recompute_actuals_for_report(db, tenant_id: str, daily_report_id: str) -> list:
    """Re-run recompute_task_actuals for every task that has a row on this
    daily report, and return those task ids (in row order, no repeats).
    Used when a report is submitted or approved, so a task touched only by
    an older report still gets its actual start / finish. Never changes a
    task's percent complete."""
    rows = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id}, {"_id": 0, "schedule_activity_id": 1},
    ).to_list(5000)
    task_ids = list(dict.fromkeys(r["schedule_activity_id"] for r in rows
                                  if r.get("schedule_activity_id")))
    for tid in task_ids:
        await recompute_task_actuals(db, tenant_id, tid)
    return task_ids


async def settle_unstarted_status(db, tenant_id: str, task_id: str) -> bool:
    """Keep 'not started' and 'in progress' in line with the reports
    (2026-11-16). Run after a report save, once actual dates are recomputed:
      * 0% and no actual start, marked 'in_progress' -> 'not_started';
      * an actual start (worked on a report, for example crew hours logged)
        but still marked 'not_started' and under 100% -> 'in_progress'.
    True when the status changed."""
    res = await db.tasks.update_one(
        tenant_filter(tenant_id, {"id": task_id, "status": "in_progress",
                                  "pct_complete": {"$in": [0, None]},
                                  "actual_start": {"$in": [None, ""]}}),
        {"$set": {"status": "not_started"}},
    )
    if res.modified_count:
        return True
    res = await db.tasks.update_one(
        tenant_filter(tenant_id, {"id": task_id, "status": "not_started",
                                  "pct_complete": {"$not": {"$gte": 100}},
                                  "actual_start": {"$nin": [None, ""]}}),
        {"$set": {"status": "in_progress"}},
    )
    return bool(res.modified_count)


# ---------------------------------------------------------------- crew hours
async def task_hours_from_reports(db, tenant_id: str, task_id: str) -> float:
    """Total hours logged for a task: the sum of hours_worked over every
    report row for it whose daily report still exists."""
    rows = await db.daily_report_activities.find(
        {"schedule_activity_id": task_id}, {"_id": 0, "daily_report_id": 1, "hours_worked": 1},
    ).to_list(None)
    rows = [r for r in rows if float(r.get("hours_worked") or 0)]
    if not rows:
        return 0.0
    live = {d["id"] for d in await db.daily_reports.find(
        tenant_filter(tenant_id, {"id": {"$in": list({r["daily_report_id"] for r in rows})}}),
        {"_id": 0, "id": 1},
    ).to_list(None)}
    return round(sum(float(r["hours_worked"]) for r in rows if r["daily_report_id"] in live), 2)


async def refresh_task_hours(db, tenant_id: str, task_id: str) -> float:
    """Set the task's actual_hours_to_date to the hours summed over all its
    report rows (only written when the value changes)."""
    total = await task_hours_from_reports(db, tenant_id, task_id)
    await db.tasks.update_one(
        tenant_filter(tenant_id, {"id": task_id, "actual_hours_to_date": {"$ne": total}}),
        {"$set": {"actual_hours_to_date": total}},
    )
    return total


async def sync_row_from_presence(db, tenant_id: str, daily_report_id: str,
                                 task_id: Optional[str]) -> bool:
    """Crew logged on a report activity counts toward that activity
    (2026-11-16). The report row for `task_id` on this report gets
    hours_worked = the sum of the linked crew rows' total hours (headcount x
    hours each) and crew_count = their summed headcount, and the task's
    actual_hours_to_date is re-summed across all its report rows.

    When the last linked crew row is removed, the row's hours and crew go
    back to 0 only if they had come from crew rows (hours_from_presence).
    Hours a person typed on the row (hours_from_presence False, above 0)
    are never replaced by crew hours; only the crew count follows the crew
    rows. Returns True when the report row changed."""
    if not (daily_report_id and task_id):
        return False
    rows = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id, "schedule_activity_id": task_id}, {"_id": 0},
    ).sort("position", 1).to_list(None)
    if not rows:
        return False
    row = rows[0]  # one row per activity per report; the first if ever more
    crews = await db.daily_presence.find(
        tenant_filter(tenant_id, {"daily_report_id": daily_report_id,
                                  "schedule_activity_id": task_id}),
        {"_id": 0, "headcount": 1, "hours_per_person": 1, "total_hours": 1},
    ).to_list(None)
    if crews:
        crew_count = int(sum(float(c.get("headcount") or 0) for c in crews))
        typed = (not row.get("hours_from_presence")
                 and float(row.get("hours_worked") or 0) > 0)
        if typed:
            # Hours a person typed on the row win: the crew rows only set
            # the crew count. Clearing the typed hours (to 0) lets the crew
            # hours fill the row again on the next crew change.
            new = {"crew_count": crew_count}
        else:
            hours = round(sum(
                float(c["total_hours"]) if c.get("total_hours") is not None
                else float(c.get("headcount") or 0) * float(c.get("hours_per_person") or 0)
                for c in crews), 2)
            new = {"hours_worked": hours, "crew_count": crew_count,
                   "hours_from_presence": True}
    elif row.get("hours_from_presence"):
        new = {"hours_worked": 0, "crew_count": 0, "hours_from_presence": False}
    else:
        new = {}
    changed = any(row.get(k) != v for k, v in new.items())
    if changed:
        await db.daily_report_activities.update_one({"id": row["id"]}, {"$set": new})
    await refresh_task_hours(db, tenant_id, task_id)
    return changed


async def after_presence_change(db, tenant_id: str, daily_report_id: str, task_ids,
                                project_id, *, actor_id=None, request_id=None) -> None:
    """Crew rows linked to a report activity count toward it (2026-11-16):
    re-sync the activity's report row (hours worked, crew count) and the
    task's hours to date, then its actual dates and status. One schedule
    recompute runs when an actual date moved. Best effort: the crew row is
    already saved, so a failure here is logged, never raised. Used by the
    crew roster endpoints, the grid crew dropdown and voice intake."""
    from services.task_status import compute_and_persist_status
    actuals_moved = False
    for tid in dict.fromkeys(t for t in (task_ids or []) if t):
        try:
            if not await sync_row_from_presence(db, tenant_id, daily_report_id, tid):
                continue
            t0 = await db.tasks.find_one(
                tenant_filter(tenant_id, {"id": tid}),
                {"_id": 0, "id": 1, "actual_start": 1, "actual_finish": 1})
            if not t0:
                continue
            act = await recompute_task_actuals(db, tenant_id, tid)
            if act and (act.get("actual_start"), act.get("actual_finish")) !=                     (t0.get("actual_start"), t0.get("actual_finish")):
                actuals_moved = True
            await settle_unstarted_status(db, tenant_id, tid)
            await compute_and_persist_status(db, tenant_id, tid,
                                             actor_id=actor_id, request_id=request_id)
        except Exception as exc:
            log.warning("crew hours sync failed for %s: %s", tid, exc)
    if actuals_moved and project_id:
        try:
            from services.cpm import auto_recompute_after_event
            await auto_recompute_after_event(tenant_id, project_id,
                                             actor_id=actor_id, request_id=request_id)
        except Exception as exc:
            log.warning("recompute after crew change failed: %s", exc)


async def after_progress_change(db, tenant_id: str, daily_report: dict, changes: list,
                                *, actor_id=None, request_id=None) -> None:
    """The schedule follows a report row's end-of-day percent that was set
    outside the report grid (the AI assistant, a voice memo) -- the same
    steps a slider save on the grid runs (2026-11-16):
      * the task's percent, status and last-synced marker change ONLY when
        this row is the latest-dated row with a percent a person entered; a
        back-dated report never overwrites a later one, nor a change made on
        the schedule since then;
      * actual start / finish are recomputed from the reports and 'not
        started' / 'in progress' settled (a task brought back to 0% with no
        work on any report becomes not started);
      * the computed status is refreshed, and the forecast and following
        tasks re-run when the percent or an actual date moved.
    `changes` is a list of (task_id, row_id, previous end-of-day percent).
    Best effort: the row is already saved, so a failure is logged, never
    raised."""
    from services.task_status import compute_and_persist_status
    report_date = _d(daily_report.get("report_date"))
    project_id = daily_report.get("project_id")
    run_recompute = False
    now_iso = utc_now().isoformat()
    for task_id, row_id, prev_eod in changes:
        if not task_id:
            continue
        try:
            t0 = await db.tasks.find_one(
                tenant_filter(tenant_id, {"id": task_id}),
                {"_id": 0, "id": 1, "pct_complete": 1, "status": 1, "project_id": 1,
                 "actual_start": 1, "actual_finish": 1})
            if not t0:
                continue
            project_id = project_id or t0.get("project_id")
            later = [h for h in progress_rows(await task_report_history(
                         db, tenant_id, task_id, exclude_row_id=row_id))
                     if report_date and h["_report_date"] > report_date]
            pct_changed = False
            if not later:
                row = await db.daily_report_activities.find_one(
                    {"id": row_id}, {"_id": 0, "pct_complete_end_of_day": 1}) or {}
                pct = float(row.get("pct_complete_end_of_day") or 0)
                task_set = {
                    "pct_complete": pct,
                    "status": _status_for_pct(pct),
                    "prev_pct_complete": prev_eod,
                    "last_synced_from_dr": {
                        "daily_report_id": daily_report.get("id"),
                        "daily_report_date": daily_report.get("report_date"),
                        "synced_at": now_iso,
                        "sync_state": ("provisional" if daily_report.get("status") in (None, "draft")
                                       else "confirmed"),
                    },
                }
                await db.tasks.update_one(tenant_filter(tenant_id, {"id": task_id}),
                                          {"$set": task_set})
                pct_changed = pct != float(t0.get("pct_complete") or 0)
                if pct_changed:
                    # Same write-back record a grid save leaves (it also
                    # tells pre_report_progress what the task was before).
                    await db.task_sync_events.insert_one({
                        "id": gen_id(),
                        "tenant_id": tenant_id,
                        "project_id": project_id,
                        "task_id": task_id,
                        "daily_report_id": daily_report.get("id"),
                        "daily_report_activity_id": row_id,
                        "triggered_by_user_id": actor_id,
                        "triggered_at": now_iso,
                        "sync_state": task_set["last_synced_from_dr"]["sync_state"],
                        "fields_updated": {
                            "pct_complete": {"before": t0.get("pct_complete"), "after": pct},
                            "status": {"before": t0.get("status"), "after": task_set["status"]},
                        },
                        "cpm_recomputed": False,
                        "cpm_changes": [],
                    })
            act = await recompute_task_actuals(db, tenant_id, task_id)
            actuals_moved = bool(act) and (act.get("actual_start"), act.get("actual_finish")) != \
                (t0.get("actual_start"), t0.get("actual_finish"))
            await settle_unstarted_status(db, tenant_id, task_id)
            await compute_and_persist_status(db, tenant_id, task_id,
                                             actor_id=actor_id, request_id=request_id)
            if pct_changed or actuals_moved:
                run_recompute = True
            elif not later:
                # Same rule as a grid slider save: a progress-aware engine
                # re-flows on any percent save.
                from services.feature_flags import progress_recompute_enabled_for
                proj = await db.projects.find_one(
                    tenant_filter(tenant_id, {"id": project_id}),
                    {"_id": 0, "schedule_mode": 1, "schedule_engine": 1}) or {}
                run_recompute = run_recompute or progress_recompute_enabled_for(proj)
        except Exception as exc:
            log.warning("schedule refresh after progress change failed for %s: %s", task_id, exc)
    if run_recompute and project_id:
        try:
            from services.cpm import auto_recompute_after_event
            await auto_recompute_after_event(tenant_id, project_id,
                                             actor_id=actor_id, request_id=request_id)
        except Exception as exc:
            log.warning("recompute after progress change failed: %s", exc)


# ---------------------------------------------------------------- delays
async def open_delays_for_task(db, tenant_id: str, task_id: str) -> list:
    docs = await db.delays.find(
        tenant_filter(tenant_id, {"linked_task_ids": task_id,
                                  "status": {"$in": OPEN_DELAY_STATUSES}}),
        {"_id": 0, "id": 1, "description": 1, "reason_category": 1, "status": 1,
         "date": 1, "logged_date": 1, "impact_start_date": 1, "per_task_impacts": 1,
         "daily_report_id": 1},
    ).to_list(50)
    out = []
    for d in docs:
        days = sum(float(i.get("projected_impact_days") or 0)
                   for i in (d.get("per_task_impacts") or []) if i.get("task_id") == task_id)
        out.append({"id": d["id"], "description": d.get("description"),
                    "reason_category": d.get("reason_category"), "status": d.get("status"),
                    "logged_date": d.get("logged_date") or d.get("date"),
                    "impact_start_date": d.get("impact_start_date"),
                    "daily_report_id": d.get("daily_report_id"),
                    "impact_days": days})
    return out


# ---------------------------------------------------------------- AI helper
async def _ask_ai(system: str, text: str) -> Optional[dict]:
    key = llm_api_key()
    if not key:
        return None
    try:
        from services.anthropic_compat import LlmChat, UserMessage
        chat = LlmChat(api_key=key, session_id=f"sched-fb-{gen_id()}",
                       system_message=system).with_model("anthropic", "claude-sonnet-4-5-20250929")
        raw = str(await chat.send_message(UserMessage(text=text))).strip()
        m = re.search(r"[\[{][\s\S]*[\]}]", raw)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:
        log.warning("schedule_feedback AI call failed: %s", exc)
        return None


# ---------------------------------------------------------------- logic proposals
LOGIC_SYSTEM = (
    "You are a federal construction scheduler. You receive one schedule relationship problem "
    "observed from daily reports and propose the minimal, logical relationship change. Output ONE "
    "JSON object only: {\"predecessors\": [{\"code\": str, \"type\": \"FS\"|\"SS\"|\"FF\"|\"SF\", "
    "\"lag_days\": int}], \"rationale\": \"1-3 plain sentences\"}. The predecessors list is the "
    "COMPLETE new predecessor list for the target activity. Only use activity codes given to you."
)


async def _create_proposal(db, tenant_id: str, project_id: str, task: dict, kind: str,
                           trigger: dict, suggested: list, rationale: str) -> Optional[dict]:
    dupe = await db.logic_proposals.find_one(tenant_filter(tenant_id, {
        "task_id": task["id"], "kind": kind, "status": "pending",
        "trigger.predecessor_code": trigger.get("predecessor_code")}), {"_id": 0, "id": 1})
    if dupe:
        return None
    doc = {
        "id": gen_id(), "tenant_id": tenant_id, "project_id": project_id,
        "task_id": task["id"], "activity_code": task.get("activity_code"),
        "activity_name": task.get("display_name") or task.get("description"),
        "kind": kind, "trigger": trigger, "status": "pending",
        "current_predecessors": task.get("predecessors") or [],
        "suggested_predecessors": suggested, "rationale": rationale,
        "ai_status": "pending", "created_at": utc_now().isoformat(),
    }
    await db.logic_proposals.insert_one(doc.copy())
    _spawn(_ai_refine_proposal(db, tenant_id, doc["id"]))
    return doc


async def _ai_refine_proposal(db, tenant_id: str, proposal_id: str) -> None:
    p = await db.logic_proposals.find_one(tenant_filter(tenant_id, {"id": proposal_id}), {"_id": 0})
    if not p:
        return
    # Unplanned field work is not part of the schedule (2026-11-18).
    nearby = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": p["project_id"], "status": {"$ne": "deleted"},
                                  "created_in_field": {"$ne": True}}),
        {"_id": 0, "activity_code": 1, "description": 1, "display_name": 1, "wbs": 1,
         "planned_start": 1, "planned_finish": 1, "pct_complete": 1, "predecessors": 1},
    ).sort("planned_start", 1).to_list(400)
    payload = {"problem": p["kind"], "trigger": p["trigger"],
               "target_activity": p["activity_code"], "target_name": p["activity_name"],
               "current_predecessors": p["current_predecessors"],
               "default_suggestion": p["suggested_predecessors"],
               "schedule": nearby[:300]}
    out = await _ask_ai(LOGIC_SYSTEM, json.dumps(payload, default=str))
    codes = {t["activity_code"] for t in nearby}
    upd = {"ai_status": "failed"}
    if isinstance(out, dict) and isinstance(out.get("predecessors"), list):
        preds = [{"code": str(x.get("code")), "type": (x.get("type") or "FS").upper(),
                  "lag_days": int(x.get("lag_days") or 0)}
                 for x in out["predecessors"]
                 if isinstance(x, dict) and x.get("code") in codes and x.get("code") != p["activity_code"]
                 and (x.get("type") or "FS").upper() in {"FS", "SS", "FF", "SF"}]
        upd = {"ai_status": "done", "suggested_predecessors": preds,
               "rationale": str(out.get("rationale") or p["rationale"])[:800]}
    await db.logic_proposals.update_one(tenant_filter(tenant_id, {"id": proposal_id}), {"$set": upd})


async def detect_out_of_order(db, tenant_id: str, task_id: str, report_date) -> None:
    """Work logged on an activity whose FS predecessor isn't finished."""
    task = await db.tasks.find_one(tenant_filter(tenant_id, {"id": task_id}), {"_id": 0})
    if not task or not task.get("predecessors"):
        return
    codes = [pred_code(p) for p in task["predecessors"] if pred_code(p)]
    preds = {t["activity_code"]: t for t in await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": task["project_id"], "activity_code": {"$in": codes},
                                  "status": {"$ne": "deleted"}}),
        {"_id": 0, "activity_code": 1, "pct_complete": 1, "description": 1, "display_name": 1},
    ).to_list(len(codes) or 1)}
    for p in task["predecessors"]:
        code = pred_code(p)
        rel, lag = pred_relationship(p)
        pt = preds.get(code)
        if rel != "FS" or not pt or float(pt.get("pct_complete") or 0) >= 100:
            continue
        suggested = [dict(x) if isinstance(x, dict) else {"code": x, "type": "FS", "lag_days": 0}
                     for x in task["predecessors"]]
        for x in suggested:
            if x.get("code") == code:
                x["type"], x["lag_days"] = "SS", 0
        await _create_proposal(
            db, tenant_id, task["project_id"], task, "out_of_order",
            {"predecessor_code": code,
             "predecessor_name": pt.get("display_name") or pt.get("description"),
             "predecessor_pct": pt.get("pct_complete"), "report_date": str(report_date)},
            suggested,
            f"Work started on {task.get('activity_code')} on {report_date} while its finish-to-start "
            f"predecessor {code} is only {float(pt.get('pct_complete') or 0):.0f}% complete. "
            "Suggest overlapping them (start-to-start).",
        )


async def propose_for_unplanned(db, tenant_id: str, task: dict, report_date) -> None:
    # Unplanned work stays on daily reports and is never tied into the
    # schedule logic, so no proposal is created (2026-11-18).
    return None


async def apply_logic_proposal(db, tenant_id: str, proposal: dict, predecessors: list) -> None:
    # Never write schedule logic onto unplanned field work.
    await db.tasks.update_one(
        tenant_filter(tenant_id, {"id": proposal["task_id"], "created_in_field": {"$ne": True}}),
        {"$set": {"predecessors": predecessors, "updated_at": utc_now().isoformat()}},
    )


# ---------------------------------------------------------------- names
NAME_SYSTEM = (
    "You name construction schedule activities so a field superintendent can tell them apart. "
    "Input: a JSON list of activities in schedule order with code, description, wbs, headings "
    "(section_path) and the name of the nearest heading-like rows around them. Output ONE JSON "
    "array only: [{\"code\": str, \"name\": str}] for EVERY input activity. Format names as "
    "'Area or phase › Activity' (max 70 chars, use ' › '). Keep the original trade words; add the "
    "phase/area/floor/system that distinguishes duplicates. Never invent scope. NEVER include WBS "
    "numbers, outline numbers or activity codes in a name."
)


_WBS_RE = re.compile(r"\(?\b(?:WBS\s*)?\d+(?:\.\d+)+\b\)?", re.IGNORECASE)


def _clean_name(name: str, code: Optional[str]) -> str:
    """Strip WBS/outline numbers and the activity code from a name."""
    out = _WBS_RE.sub("", name)
    if code:
        out = out.replace(code, "")
    out = re.sub(r"\s*›\s*(›\s*)+", " › ", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ›·-–,")
    return out[:120]


def _window_label(t: dict) -> Optional[str]:
    ps = _d(t.get("planned_start"))
    pf = _d(t.get("planned_finish"))
    if not ps:
        return None
    if pf and pf != ps:
        return f"{ps.strftime('%b')} {ps.day}–{pf.strftime('%b')} {pf.day}" if ps.month != pf.month \
            else f"{ps.strftime('%b')} {ps.day}–{pf.day}"
    return f"{ps.strftime('%b')} {ps.day}"


def heading_name(t: dict) -> Optional[str]:
    path = [s for s in (t.get("section_path") or []) if s]
    if not path:
        return None
    return _clean_name(" › ".join(path[-2:] + [t.get("description") or t.get("activity_code")]),
                       t.get("activity_code")) or None


async def generate_name_suggestions(db, tenant_id: str, project_id: str, *, auto_approve: bool = False) -> dict:
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"},
                                  "work_category": {"$nin": list(PAPERWORK)},
                                  "created_in_field": {"$ne": True}}),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "wbs": 1,
         "section_path": 1, "display_name": 1, "planned_start": 1, "planned_finish": 1},
    ).to_list(5000)
    tasks.sort(key=lambda t: (str(t.get("wbs") or ""), str(t.get("activity_code") or "")))
    names = {t["id"]: (heading_name(t), "headings") for t in tasks}
    ai_input = [{"code": t["activity_code"], "description": t.get("description"),
                 "wbs": t.get("wbs"), "section_path": t.get("section_path"),
                 "planned_start": t.get("planned_start")} for t in tasks]
    by_code = {t["activity_code"]: t for t in tasks}
    for i in range(0, len(ai_input), 120):
        out = await _ask_ai(NAME_SYSTEM, json.dumps(ai_input[i:i + 120], default=str))
        for item in out if isinstance(out, list) else []:
            t = by_code.get(str(item.get("code"))) if isinstance(item, dict) else None
            if t and item.get("name"):
                clean = _clean_name(str(item["name"]), t.get("activity_code"))
                if clean:
                    names[t["id"]] = (clean, "ai")
    # No AI (or AI failed): tell duplicate names apart by their planned window.
    from collections import Counter
    dup = Counter((t.get("description") or "").strip().lower() for t in tasks)
    for t in tasks:
        if names[t["id"]][0] is None and dup[(t.get("description") or "").strip().lower()] > 1:
            win = _window_label(t)
            if win:
                names[t["id"]] = (f"{t.get('description')} · {win}"[:120], "dates")
    now = utc_now().isoformat()
    await db.activity_name_suggestions.update_many(
        tenant_filter(tenant_id, {"project_id": project_id, "status": "pending"}),
        {"$set": {"status": "superseded"}})
    made = 0
    for t in tasks:
        name, src = names[t["id"]]
        if not name or name == (t.get("display_name") or t.get("description")):
            continue
        doc = {"id": gen_id(), "tenant_id": tenant_id, "project_id": project_id,
               "task_id": t["id"], "activity_code": t["activity_code"],
               "current_name": t.get("display_name") or t.get("description"),
               "suggested_name": name, "source": src,
               "status": "approved" if auto_approve else "pending", "created_at": now}
        await db.activity_name_suggestions.insert_one(doc.copy())
        if auto_approve:
            await db.tasks.update_one(tenant_filter(tenant_id, {"id": t["id"]}),
                                      {"$set": {"display_name": name}})
        made += 1
    return {"suggested": made, "total": len(tasks)}
