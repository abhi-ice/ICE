"""Schedule diff + AI review.

Compares an incoming parsed schedule (from MPP/XER/CSV/XLSX) against the
project's current `tasks` collection and produces a structured diff that
PMs/Admins can review before the rows commit.

Federal-contract context: every baseline change must be justifiable. The
proposal flow gives an immutable audit trail: who uploaded, what changed,
who approved, when.

CPM Part A (2026-05-15) -- predecessors changed from List[str] to
List[dict] {code, type, lag_days}. The diff renderer now extracts the
activity_code via pred_code() so the "predecessors_added /
predecessors_removed / predecessors_modified" stats stay code-based.
A future enhancement could surface type/lag changes separately
(e.g. "FS+0 changed to FS+7" without the code itself changing); not
in scope for Part A.
"""
from __future__ import annotations
from datetime import date, datetime
from typing import Iterable, Optional

from models import pred_code

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
        # CPM Part A -- extract activity_code from each predecessor
        # entry. str(dict) would produce "{'code':...}" garbage on
        # post-migration data; pred_code() returns the code itself.
        old_p = sorted(c for c in (pred_code(x) for x in (old.get("predecessors") or [])) if c)
        new_p = sorted(c for c in (pred_code(x) for x in (new.get("predecessors") or [])) if c)
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


# ============================================================
# Bucket C3c Phase 1 — Version-to-version diff
#
# The compute_schedule_diff() above compares an INCOMING parsed schedule
# against the current `tasks` collection (used by the proposal flow).
# What follows is a different concept: a structured diff between two
# already-approved schedule_proposals, persisted in schedule_update_diffs
# so the TIA generator and the Schedule Updates page can query history.
# ============================================================


def _row_predecessors(row: dict) -> list[str]:
    # CPM Part A -- return sorted activity_codes regardless of
    # whether row.predecessors is the legacy bare-string form or
    # the post-migration dict form. pred_code() handles both.
    return sorted(c for c in (pred_code(p) for p in (row.get("predecessors") or [])) if c)


def _to_iso(v) -> Optional[str]:
    d = _to_date(v)
    return d.isoformat() if d else None


def compute_version_diff(
    earlier_rows: Iterable[dict],
    later_rows: Iterable[dict],
    earlier_critical_codes: Optional[set[str]] = None,
    later_critical_codes: Optional[set[str]] = None,
) -> dict:
    """Pure function. Compares two snapshots of parsed schedule rows.

    Each row is expected to have at least: activity_code, description,
    duration_days, planned_start, planned_finish, predecessors,
    pct_complete (latter optional).

    Returns a dict shaped to match the ScheduleUpdateDiff schema fields:
    activities_added, activities_removed, activities_modified,
    predecessor_changes, newly_critical_activities,
    removed_critical_activities, total_float_distribution_shift, plus
    aggregate counts.
    """
    earlier = {(r.get("activity_code") or ""): r for r in earlier_rows if r.get("activity_code")}
    later = {(r.get("activity_code") or ""): r for r in later_rows if r.get("activity_code")}

    # ---------- added ----------
    activities_added: list[dict] = []
    for code, r in later.items():
        if code not in earlier:
            activities_added.append({
                "activity_code": code,
                "id": r.get("id"),
                "description": r.get("description") or "",
                "duration_days": r.get("duration_days"),
                "start_date": _to_iso(r.get("planned_start")),
                "end_date": _to_iso(r.get("planned_finish")),
                "predecessors": _row_predecessors(r),
            })

    # ---------- removed ----------
    activities_removed: list[dict] = []
    for code, r in earlier.items():
        if code not in later:
            activities_removed.append({
                "activity_code": code,
                "id": r.get("id"),
                "description": r.get("description") or "",
                "duration_days": r.get("duration_days"),
                "start_date": _to_iso(r.get("planned_start")),
                "end_date": _to_iso(r.get("planned_finish")),
            })

    # ---------- modified + predecessor changes ----------
    activities_modified: list[dict] = []
    predecessor_changes: list[dict] = []
    preds_added_count = 0
    preds_removed_count = 0
    preds_modified_count = 0

    for code in earlier.keys() & later.keys():
        old, new = earlier[code], later[code]
        per_field: dict = {"activity_code": code, "id": new.get("id") or old.get("id")}

        def _maybe(field_key, old_v, new_v, key_in_payload):
            if (old_v or None) != (new_v or None):
                per_field[key_in_payload] = {"prior": old_v, "new": new_v}

        _maybe("description", old.get("description"), new.get("description"), "description_change")
        _maybe("duration", old.get("duration_days"), new.get("duration_days"), "duration_change")
        _maybe("start", _to_iso(old.get("planned_start")), _to_iso(new.get("planned_start")), "start_change")
        _maybe("end", _to_iso(old.get("planned_finish")), _to_iso(new.get("planned_finish")), "end_change")
        _maybe("status", old.get("status"), new.get("status"), "status_change")
        _maybe("pct", old.get("pct_complete"), new.get("pct_complete"), "pct_change")

        if len(per_field) > 2:
            activities_modified.append(per_field)

        # Predecessor diff is its own list (a single activity may have
        # multiple pred changes, each as a row).
        old_p = set(_row_predecessors(old))
        new_p = set(_row_predecessors(new))
        for p in new_p - old_p:
            predecessor_changes.append({
                "successor_id": code,
                "predecessor_id": p,
                "change_type": "added",
                "prior_relationship": None,
                "new_relationship": "FS",  # parsed schedules don't carry relationship type
                "prior_lag_days": None,
                "new_lag_days": 0,
            })
            preds_added_count += 1
        for p in old_p - new_p:
            predecessor_changes.append({
                "successor_id": code,
                "predecessor_id": p,
                "change_type": "removed",
                "prior_relationship": "FS",
                "new_relationship": None,
                "prior_lag_days": 0,
                "new_lag_days": None,
            })
            preds_removed_count += 1
        # 'modified' would be a relationship-type or lag swap; we don't
        # have that fidelity from parsed_rows yet, so leave it at zero.

    # ---------- critical-path movement ----------
    earlier_cp = set(earlier_critical_codes or [])
    later_cp = set(later_critical_codes or [])
    newly_critical = sorted(later_cp - earlier_cp)
    removed_critical = sorted(earlier_cp - later_cp)

    total_count = max(1, len(later))
    change_pct = round(len(activities_modified) / total_count * 100.0, 2)

    return {
        "activities_added": activities_added,
        "activities_removed": activities_removed,
        "activities_modified": activities_modified,
        "predecessor_changes": predecessor_changes,
        "newly_critical_activities": newly_critical,
        "removed_critical_activities": removed_critical,
        "total_float_distribution_shift": {
            "earlier_critical_count": len(earlier_cp),
            "later_critical_count": len(later_cp),
            "delta": len(later_cp) - len(earlier_cp),
        },
        # Aggregate counts
        "activities_added_count": len(activities_added),
        "activities_removed_count": len(activities_removed),
        "activities_modified_count": len(activities_modified),
        "predecessors_added_count": preds_added_count,
        "predecessors_removed_count": preds_removed_count,
        "predecessors_modified_count": preds_modified_count,
        "cp_changes_count": len(newly_critical) + len(removed_critical),
        "total_change_percentage": change_pct,
    }


async def auto_diff_on_approval(
    tenant_id: str,
    project_id: str,
    later_proposal_id: str,
) -> Optional[dict]:
    """Called from the schedule approval BackgroundTask. Looks up the
    prior approved proposal for this project; if found, computes a
    version-to-version diff between its parsed_rows and the new one.
    Persists into schedule_update_diffs. Returns the new row, or None
    if there's no prior version (first approved schedule for the
    project).

    Idempotent on (tenant_id, earlier_schedule_version_id,
    later_schedule_version_id) — a re-run skips and returns existing.
    """
    from database import get_db, tenant_filter
    from models import gen_id, utc_now

    db = get_db()

    later = await db.schedule_proposals.find_one(
        tenant_filter(tenant_id, {"id": later_proposal_id, "status": "approved"}),
        {"_id": 0},
    )
    if not later:
        return None

    # Find prior approved proposal for the same project (excluding this one)
    earlier = await db.schedule_proposals.find_one(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": "approved",
            "id": {"$ne": later_proposal_id},
        }),
        {"_id": 0},
        sort=[("reviewed_at", -1)],
    )
    if not earlier:
        return None

    # Idempotency check
    existing = await db.schedule_update_diffs.find_one(
        tenant_filter(tenant_id, {
            "earlier_schedule_version_id": earlier["id"],
            "later_schedule_version_id": later_proposal_id,
        }),
        {"_id": 0},
    )
    if existing:
        return existing

    earlier_rows = [r for r in (earlier.get("parsed_rows") or [])
                    if not r.get("excluded_as_summary")]
    later_rows = [r for r in (later.get("parsed_rows") or [])
                  if not r.get("excluded_as_summary")]

    # Critical-path comparison: pull current is_critical_path from tasks
    # for the LATER set (post-apply); use the earlier's parsed snapshot
    # is_critical_path field if present (proposal flow doesn't currently
    # capture it, so usually empty — that's fine, cp diff will read 0).
    later_codes = [r.get("activity_code") for r in later_rows if r.get("activity_code")]
    later_cp: set[str] = set()
    if later_codes:
        cp_docs = await db.tasks.find(
            tenant_filter(tenant_id, {
                "project_id": project_id,
                "activity_code": {"$in": later_codes},
                "is_critical_path": True,
            }),
            {"_id": 0, "activity_code": 1},
        ).to_list(5000)
        later_cp = {d.get("activity_code") for d in cp_docs if d.get("activity_code")}

    earlier_cp = {
        r.get("activity_code") for r in earlier_rows
        if r.get("is_critical_path") and r.get("activity_code")
    }

    payload = compute_version_diff(earlier_rows, later_rows, earlier_cp, later_cp)

    row = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "earlier_schedule_version_id": earlier["id"],
        "later_schedule_version_id": later_proposal_id,
        "computed_at": utc_now().isoformat(),
        **payload,
        "ai_narrative": None,
        "ai_narrative_generated_at": None,
        "ai_narrative_generated_by": None,
    }
    await db.schedule_update_diffs.insert_one(row.copy())
    return row
