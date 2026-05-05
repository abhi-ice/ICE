"""Schedule diff + AI review.

Compares an incoming parsed schedule (from MPP/XER/CSV/XLSX) against the
project's current `tasks` collection and produces a structured diff that
PMs/Admins can review before the rows commit.

Federal-contract context: every baseline change must be justifiable. The
proposal flow gives an immutable audit trail: who uploaded, what changed,
who approved, when.
"""
from __future__ import annotations
from datetime import date, datetime
from typing import Iterable, Optional

DATE_SHIFT_THRESHOLD_DAYS = 3  # User spec: "date shifts > 3 days"


def _to_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _diff_days(a: Optional[date], b: Optional[date]) -> Optional[int]:
    if a is None or b is None:
        return None
    return (b - a).days


def compute_schedule_diff(current_tasks: Iterable[dict], parsed_rows: Iterable[dict]) -> dict:
    """Pure function — no DB calls. `current_tasks` is a list of task docs
    with at minimum {activity_code, description, planned_start, planned_finish,
    duration_days, predecessors}. `parsed_rows` is the list returned by
    `parse_schedule_file`.
    """
    cur = {(t.get("activity_code") or ""): t for t in current_tasks if t.get("activity_code")}
    nxt = {(r.get("activity_code") or ""): r for r in parsed_rows if r.get("activity_code")}

    added: list[dict] = []
    dropped: list[dict] = []
    date_shifts: list[dict] = []
    duration_changes: list[dict] = []
    predecessor_changes: list[dict] = []

    # NEW activity codes
    for code, row in nxt.items():
        if code not in cur:
            added.append({
                "activity_code": code,
                "description": row.get("description") or "",
                "planned_start": row.get("planned_start"),
                "planned_finish": row.get("planned_finish"),
                "duration_days": row.get("duration_days"),
                "trade_code": row.get("trade_code"),
            })

    # DROPPED activity codes
    for code, t in cur.items():
        if code not in nxt:
            dropped.append({
                "activity_code": code,
                "description": t.get("description") or "",
                "planned_start": t.get("planned_start"),
                "planned_finish": t.get("planned_finish"),
                "duration_days": t.get("duration_days"),
                "pct_complete": t.get("pct_complete", 0),
            })

    # CHANGES in common activities
    max_shift = 0
    total_shift = 0
    shifts_count = 0
    for code in nxt.keys() & cur.keys():
        new = nxt[code]
        old = cur[code]
        old_start, new_start = _to_date(old.get("planned_start")), _to_date(new.get("planned_start"))
        old_finish, new_finish = _to_date(old.get("planned_finish")), _to_date(new.get("planned_finish"))
        start_delta = _diff_days(old_start, new_start)
        finish_delta = _diff_days(old_finish, new_finish)

        biggest = 0
        kind = None
        if start_delta is not None and abs(start_delta) > DATE_SHIFT_THRESHOLD_DAYS:
            biggest = max(abs(biggest), abs(start_delta))
            kind = "start"
        if finish_delta is not None and abs(finish_delta) > DATE_SHIFT_THRESHOLD_DAYS:
            biggest = max(abs(biggest), abs(finish_delta))
            kind = "both" if kind == "start" else "finish"

        if biggest:
            date_shifts.append({
                "activity_code": code,
                "description": old.get("description") or new.get("description"),
                "kind": kind,
                "old_start": old_start.isoformat() if old_start else None,
                "new_start": new_start.isoformat() if new_start else None,
                "old_finish": old_finish.isoformat() if old_finish else None,
                "new_finish": new_finish.isoformat() if new_finish else None,
                "start_delta_days": start_delta,
                "finish_delta_days": finish_delta,
                "biggest_shift_days": biggest,
            })
            max_shift = max(max_shift, biggest)
            total_shift += biggest
            shifts_count += 1

        # Duration changes — anything > 1 day swing is worth flagging
        old_dur = old.get("duration_days")
        new_dur = new.get("duration_days")
        if old_dur is not None and new_dur is not None:
            try:
                d_old, d_new = float(old_dur), float(new_dur)
                if abs(d_new - d_old) >= 1.0:
                    duration_changes.append({
                        "activity_code": code,
                        "description": old.get("description") or new.get("description"),
                        "old_duration_days": d_old,
                        "new_duration_days": d_new,
                        "delta_days": d_new - d_old,
                    })
            except (TypeError, ValueError):
                pass

        # Predecessor change
        old_p = sorted([str(x) for x in (old.get("predecessors") or [])])
        new_p = sorted([str(x) for x in (new.get("predecessors") or [])])
        if old_p != new_p:
            predecessor_changes.append({
                "activity_code": code,
                "description": old.get("description") or new.get("description"),
                "old_predecessors": old_p,
                "new_predecessors": new_p,
            })

    summary = {
        "current_count": len(cur),
        "incoming_count": len(nxt),
        "added_count": len(added),
        "dropped_count": len(dropped),
        "shifted_count": shifts_count,
        "duration_changed_count": len(duration_changes),
        "predecessor_changed_count": len(predecessor_changes),
        "max_shift_days": max_shift,
        "avg_shift_days": round(total_shift / shifts_count, 1) if shifts_count else 0,
    }
    return {
        "added": added,
        "dropped": dropped,
        "date_shifts": date_shifts,
        "duration_changes": duration_changes,
        "predecessor_changes": predecessor_changes,
        "summary": summary,
    }
