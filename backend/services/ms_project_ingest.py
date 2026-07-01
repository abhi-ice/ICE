"""MS Project Mode 2 — PULL adapter: worker task list -> reviewable proposal.

CPM Mode 2 (2026-06-29). When the worker pulls Project's full network, this
converts it into the SAME parsed_rows shape compute_schedule_diff expects, so the
entire Mode-1 proposal/diff/review/approve flow is reused unchanged. The only new
logic is the worker-output -> parsed_rows mapping (incl. the dependency re-parse
and lag canonicalization).

Identity: a Project task carries our activity_code in Text1 (serialize_task ->
"text1"); the worker also resolves each dependency's predecessor to its Text1, so
dependencies arrive code-keyed (no UID->code mapping needed here). Rows with no
text1 (tasks created in Project that ICE hasn't seen) are kept and surfaced in the
proposal via compute_schedule_diff's "added" bucket — never silently dropped.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from database import get_db, tenant_filter
from models import gen_id, utc_now


def worker_tasks_to_parsed_rows(computed_tasks: list, dependencies: list) -> list:
    """Map the worker's serialize_task list + code-keyed dependency list into
    parsed_rows ({activity_code, description, planned_start/finish, duration_days,
    predecessors:[{code,type,lag_days}], is_milestone, pct_complete, actual_*}).

    `dependencies`: [{successor_code, predecessor_code, type, lag_days}] already
    resolved to ICE activity_codes by the worker (via Text1 on both ends).
    A row's planned_* are Project's COMPUTED start/finish -- for a Mode-2 project
    Project's schedule IS the baseline ICE reviews.
    """
    # Group dependencies by successor activity_code.
    preds_by_code: dict = {}
    for d in (dependencies or []):
        sc = d.get("successor_code")
        pc = d.get("predecessor_code")
        if not sc or not pc:
            continue
        # Predecessor.lag_days is an int; MS Project returns lag in (working)
        # days as a float — round to int. (Calendar-vs-working canonicalization
        # is a documented refinement; the integer day count is correct for the
        # common 0/whole-day lags.)
        try:
            lag = int(round(float(d.get("lag_days") or 0)))
        except (TypeError, ValueError):
            lag = 0
        preds_by_code.setdefault(sc, []).append({
            "code": pc,
            "type": (d.get("type") or "FS"),
            "lag_days": lag,
        })

    rows = []
    for t in (computed_tasks or []):
        code = (t.get("text1") or "").strip()
        if not code:
            # No ICE activity_code yet (created in Project). Keep it with a
            # stable surrogate so it shows as "added" in the diff for review;
            # the PM names/accepts it. Use the Project name as a fallback code.
            code = (t.get("name") or "").strip()
            if not code:
                continue
        row = {
            "activity_code": code,
            "description": t.get("name") or code,
            "wbs": t.get("wbs"),
            "planned_start": (t.get("start") or None),
            "planned_finish": (t.get("finish") or None),
            "duration_days": t.get("duration_days"),
            "is_milestone": bool(t.get("milestone", False)),
            "predecessors": preds_by_code.get(code, []),
            # progress columns: Project holds them but ICE OWNS actuals (authority
            # split) -- so we do NOT pull pct/actuals back as authoritative; the
            # proposal carries structure + dates only. (pct/actuals omitted.)
        }
        rows.append(row)
    return rows


async def build_pull_proposal(
    db, tenant_id: str, project_id: str, computed_tasks: list, dependencies: list,
    *, actor_id: Optional[str] = None,
) -> Optional[str]:
    """Build a reviewable ScheduleProposal(source='ms_project_pull') from a PULL.
    Reuses compute_schedule_diff against the current tasks. Returns proposal id,
    or None when there is nothing to review (no rows)."""
    from services.schedule_diff import compute_schedule_diff

    parsed_rows = worker_tasks_to_parsed_rows(computed_tasks, dependencies)
    if not parsed_rows:
        return None

    current = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0},
    ).to_list(5000)
    diff = compute_schedule_diff(current, parsed_rows)

    # Skip creating a no-op proposal when nothing structurally changed.
    summary = (diff or {}).get("summary") or {}
    nothing_changed = all(
        not (diff or {}).get(k)
        for k in ("added", "dropped", "date_shifts", "duration_changes", "predecessor_changes")
    )
    if nothing_changed:
        return None

    proposal = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "filename": "MS Project (live)",
        "kind": "schedule",
        "source": "ms_project_pull",
        "status": "pending_review",
        "parsed_rows": parsed_rows,
        "diff": diff,
        "ai_review": None,
        "uploaded_by": actor_id or "ms-project-worker",
        "uploaded_at": utc_now().isoformat(),
        "parse_warnings": [],
    }
    await db.schedule_proposals.insert_one(proposal.copy())
    return proposal["id"]
