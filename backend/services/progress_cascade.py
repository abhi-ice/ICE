"""B2 (2026-06-12) -- no-progress / behind auto-slip.

The gap this closes (documented as the engine's honest limit, Diagram-1
limb J): today a task that is simply NOT MOVING -- behind its planned
percent with no logged delay / RFI / submittal breach -- does NOT push
its successors. The cascade is impact-days-only. B2 makes field progress
(reported via the EOD slider, written to task.pct_complete) feed the
schedule: a task behind its planned percent at the data date grows its
remaining duration, slides its finish later, and cascades downstream
through the SAME float-aware machinery a logged delay uses.

MECHANIC (FDOT-grounded, conservative slide model):
    planned_pct = linear plan percent at the data date
    actual_pct  = task.pct_complete
    if actual_pct >= planned_pct -> 0 (on or ahead of schedule; B2 NEVER
        pulls a task earlier, mirroring the cascade's clamp-at-0 rule)
    else:
        OD    = working_days_between(planned_start, planned_finish)
                (the imported span -- the duration the schedule budgeted)
        shift = round(OD * (planned_pct - actual_pct) / 100)
    The behind-fraction of the original duration is the working-day slip.

This module is the ONLY place B2 lives besides the two flag-gated wiring
points (compute_projected_finish_dates folds the shift into a task's own
shift; the activity-grid PATCH fires the recompute). It is NOT imported
by cpm.py / working_days.py. It MAY read work_category here (this file is
not one of the three guarded CPM-engine files) to skip paperwork tasks,
which have no per-day field progress and would otherwise read "behind"
the instant their planned_start passes (the same false alarm the pt.79
status shortcut guards against).

Gated behind FF_PROGRESS_CASCADE -- with the flag off the cascade never
calls this module and the projected dates are byte-identical to today.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

# Reuse the EXISTING pure plan-percent helper + paperwork category set so
# B2 stays consistent with the status machine (no parallel definitions).
from services.task_status import planned_pct_at, PAPERWORK_CATEGORIES
from services.working_days import working_days_between


def _parse(v) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def progress_shift_days(task: dict, data_date: date) -> int:
    """Working-day slip implied by `task` being behind its planned percent
    at `data_date`. 0 when the task is on/ahead of schedule, complete, not
    yet started, paperwork, or missing planned dates.

    Pure + deterministic -- no DB, no I/O.
    """
    ps = _parse(task.get("planned_start"))
    pf = _parse(task.get("planned_finish"))
    if ps is None or pf is None:
        return 0
    # Paperwork has no per-day field progress -- never auto-slip it.
    if task.get("work_category") in PAPERWORK_CATEGORIES:
        return 0
    try:
        actual = float(task.get("pct_complete") or 0)
    except (TypeError, ValueError):
        actual = 0.0
    # Already complete -> nothing to slip.
    if actual >= 100.0:
        return 0
    # Not started yet (data date before the planned start) -> planned_pct
    # is 0, so there is nothing to be "behind" on.
    if data_date < ps:
        return 0
    planned = planned_pct_at(task, data_date)
    if actual >= planned:
        return 0  # on or ahead of schedule -- B2 never pulls earlier
    od = working_days_between(ps, pf)
    if od <= 0:
        return 0
    shift = int(round(od * (planned - actual) / 100.0))
    return max(0, shift)


def build_progress_shift_map(tasks: list, data_date: date) -> dict:
    """task_id -> progress_shift_days, for every task with a positive
    behind-shift. Tasks with zero shift are omitted (the cascade treats a
    missing key as 0). Pure -- operates on a list of task dicts."""
    out: dict = {}
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        shift = progress_shift_days(t, data_date)
        if shift > 0:
            out[tid] = shift
    return out


async def build_progress_shift_map_from_db(
    db, tenant_id: str, project_id: str, data_date: date,
) -> dict:
    """DB-backed wrapper the cascade calls (B2). Fetches the fields
    progress_shift_days needs -- INCLUDING work_category, the paperwork
    skip signal -- and returns the {task_id -> shift} map.

    This wrapper is why the CPM engine files (cpm.py / task_status.py /
    working_days.py) never reference work_category for B2: the progress
    query + the paperwork filter live HERE, in the non-engine module, so
    the engine only consumes the resulting shift map. Keeps the
    architectural guard (the CPM engine must not read DR view metadata)
    intact for the cascade path.
    """
    from database import tenant_filter
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id, "status": {"$ne": "deleted"},
        }),
        {"_id": 0, "id": 1, "planned_start": 1, "planned_finish": 1,
         "pct_complete": 1, "work_category": 1},
    ).to_list(5000)
    return build_progress_shift_map(tasks, data_date)
