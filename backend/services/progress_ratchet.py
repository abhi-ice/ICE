"""Progress can't go backwards once a daily report is submitted (2026-11-16).

The rule, as the owner put it: once a person has shown a progress on an
activity and submitted (or had approved) that day's report, the activity is
locked at that progress. A later report may go further, never back. Going
back is only possible by editing the report that recorded the higher value.

For an activity row for task T on report R dated D the allowed end-of-day
percent range is:

  * floor   = the highest end-of-day percent a person entered for T on any
              OTHER submitted or approved report dated BEFORE D (0 if none);
  * ceiling = the lowest end-of-day percent a person entered for T on any
              OTHER submitted or approved report dated AFTER D (100 if none).
              A back-dated report can't claim more than a later submitted
              report already said, or progress would seem to go backwards.

Drafts never lock anything. Only rows whose end-of-day a person entered
count (services.schedule_feedback.progress_rows), the same notion the
back-dated report logic uses. A value outside the range is refused with a
422 whose body is {"detail": <plain message>, "code": "progress_out_of_range",
"floor", "floor_from", "ceiling", "ceiling_from"} (see the handler below,
registered in server.py). The phone's offline queue reads the code to drop
such a save instead of retrying it forever.

Older data saved before this rule may already disagree (an earlier
submitted report above a later one). Then the floor wins and the ceiling is
not applied, so the person is never left with no allowed value at all.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import HTTPException

PROGRESS_OUT_OF_RANGE_CODE = "progress_out_of_range"
LOCKED_REPORT_STATUSES = ("submitted", "approved")


def _d(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def fmt_pct(v: float) -> str:
    """45.0 -> "45%", 45.5 -> "45.5%"."""
    v = float(v or 0)
    return f"{int(v)}%" if v == int(v) else f"{round(v, 1)}%"


def fmt_day(iso: Optional[str]) -> str:
    """"2026-11-16" -> "11/16" (the way the site team names a report)."""
    d = _d(iso)
    return f"{d.month}/{d.day}" if d else str(iso or "")


def no_limits() -> dict:
    return {"floor": 0.0, "floor_from": None, "ceiling": 100.0, "ceiling_from": None}


def limits_from_history(history: list, report_date, exclude_dr_id: Optional[str] = None) -> dict:
    """Allowed end-of-day range for a row on a report dated `report_date`,
    from a task_report_history list (rows carry _report_date, _report_status
    and _report_id). Rows on `exclude_dr_id` (the row's own report) never
    limit it."""
    from services.schedule_feedback import progress_rows
    rd = _d(report_date)
    out = no_limits()
    if rd is None:
        return out
    for r in progress_rows(history or []):
        if r.get("_report_status") not in LOCKED_REPORT_STATUSES:
            continue
        if exclude_dr_id and r.get("_report_id") == exclude_dr_id:
            continue
        eod = float(r.get("pct_complete_end_of_day") or 0)
        when = r["_report_date"]
        if when < rd:
            # Highest earlier value; on a tie name the earliest report, since
            # lowering that one first is what frees the others.
            if out["floor_from"] is None or eod > out["floor"]:
                out["floor"], out["floor_from"] = eod, when.isoformat()
        elif when > rd:
            # Lowest later value; on a tie name the latest report, since
            # raising that one first is what frees the others.
            if (out["ceiling_from"] is None or eod < out["ceiling"]
                    or (eod == out["ceiling"] and when.isoformat() > out["ceiling_from"])):
                out["ceiling"], out["ceiling_from"] = eod, when.isoformat()
    if out["floor_from"] is None:
        out["floor"] = 0.0
    if out["ceiling"] < out["floor"]:
        # Older data that already went backwards: keep the floor only.
        out["ceiling"], out["ceiling_from"] = 100.0, None
    return out


async def progress_limits(db, tenant_id: str, task_id: str, report_date,
                          exclude_dr_id: Optional[str] = None) -> dict:
    """limits_from_history for one task (one history lookup)."""
    from services.schedule_feedback import task_report_history
    hist = await task_report_history(db, tenant_id, task_id, exclude_dr_id=exclude_dr_id)
    return limits_from_history(hist, report_date, exclude_dr_id=exclude_dr_id)


async def progress_limits_for_tasks(db, tenant_id: str, task_ids: list, report_date,
                                    exclude_dr_id: Optional[str] = None) -> dict:
    """{task_id: limits} for many tasks with two queries in total."""
    from services.schedule_feedback import task_report_histories
    ids = list(dict.fromkeys(t for t in task_ids if t))
    hists = await task_report_histories(db, tenant_id, ids, exclude_dr_id=exclude_dr_id)
    return {tid: limits_from_history(hists.get(tid) or [], report_date, exclude_dr_id=exclude_dr_id)
            for tid in ids}


def clamp_to_limits(value: float, limits: dict) -> float:
    """A pre-filled percent moved into the allowed range."""
    v = max(float(value or 0), float(limits.get("floor") or 0))
    return min(v, float(limits.get("ceiling") if limits.get("ceiling") is not None else 100))


def out_of_range_message(value: float, limits: dict) -> Optional[str]:
    """Plain message when `value` is outside the range, else None."""
    v = float(value or 0)
    floor, ceiling = float(limits.get("floor") or 0), float(limits.get("ceiling", 100))
    if limits.get("floor_from") and v < floor:
        day = fmt_day(limits["floor_from"])
        return (f"Progress can't go below {fmt_pct(floor)} — the {day} report already "
                f"recorded {fmt_pct(floor)}. To lower it, edit the {day} report.")
    if limits.get("ceiling_from") and v > ceiling:
        day = fmt_day(limits["ceiling_from"])
        return (f"Progress can't go above {fmt_pct(ceiling)} — the {day} report recorded "
                f"{fmt_pct(ceiling)}. To raise it here, edit the {day} report first.")
    return None


class ProgressOutOfRangeError(HTTPException):
    """422 for an end-of-day percent outside the range the submitted reports
    allow. The body is {"detail": text, "code": "progress_out_of_range"} plus
    the limits (and, on a refused submit, the rows)."""

    code = PROGRESS_OUT_OF_RANGE_CODE

    def __init__(self, message: str, limits: Optional[dict] = None, rows: Optional[list] = None):
        super().__init__(422, message)
        lim = limits or no_limits()
        self.extra = {k: lim.get(k) for k in ("floor", "floor_from", "ceiling", "ceiling_from")}
        if rows is not None:
            self.extra["rows"] = rows


def ensure_in_range(value: float, limits: dict) -> None:
    """Raise ProgressOutOfRangeError when `value` is outside `limits`."""
    msg = out_of_range_message(value, limits)
    if msg:
        raise ProgressOutOfRangeError(msg, limits)


def error_dict(message: str, limits: dict) -> dict:
    """The same refusal as a plain dict (for the AI assistant's tools)."""
    return {"error": message, "code": PROGRESS_OUT_OF_RANGE_CODE,
            **{k: limits.get(k) for k in ("floor", "floor_from", "ceiling", "ceiling_from")}}


async def progress_out_of_range_handler(request, exc: ProgressOutOfRangeError):
    """Exception handler (registered in server.py): the usual {"detail": text}
    every page shows, plus "code" and the limits."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail, "code": exc.code, **exc.extra})
