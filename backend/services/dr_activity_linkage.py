"""Bucket D Corrective (2026-05-08) — master-schedule task linkage
validation helper + supplement stamping helper.

Replaces the earlier dr-activity scope (`linked_dr_activity_ids`
referencing daily_report_activities.id), which was a wrong build that
created two parallel pickers on the delay form. The canonical scope is
master schedule: `linked_task_ids` references tasks.id, same as
delay's existing field. Migration 007 backfills + drops the old field.

Two checks (validate_linked_task_ids):
  1. Min-1 if `required=True` -- raises 422 with a friendly message.
  2. Each id resolves to a real tasks row scoped by tenant_id +
     project_id (cross-project linkage is not allowed).

Phase 4 supplement helper (stamp_supplement_metadata) is unchanged --
it operates on supplement metadata fields, not on linkage scope.
"""
from typing import Iterable, Optional
from datetime import datetime, timezone
from fastapi import HTTPException

from database import get_db, tenant_filter
from models import LINKAGE_MIN_ERR


def stamp_supplement_metadata(doc: dict, *, actor_id: str, fact_type: str) -> Optional[dict]:
    """If doc carries is_supplement=True, validate the supplement
    payload (parent_daily_report_id + supplement_reason both required),
    stamp supplement_filed_at/by, and return the audit payload for
    the caller to fire SUPPLEMENT_FILED. Returns None on non-supplement
    docs so the caller can skip the audit.

    Used by every event create handler in Phase 4 so the supplement
    contract lives in one place.

    Pure function -- does NOT validate parent DR lock state (that
    requires DB access). Async wrapper validate_supplement_parent_locked
    below handles that check."""
    if not doc.get("is_supplement"):
        return None
    if not doc.get("parent_daily_report_id"):
        raise HTTPException(422, "parent_daily_report_id required when is_supplement=true")
    if not (doc.get("supplement_reason") or "").strip():
        raise HTTPException(422, "supplement_reason required when is_supplement=true")
    doc["supplement_filed_at"] = datetime.now(timezone.utc).isoformat()
    doc["supplement_filed_by"] = actor_id
    return {
        "parent_daily_report_id": doc.get("parent_daily_report_id"),
        "container_daily_report_id": doc.get("daily_report_id"),
        "fact_type": fact_type,
        "reason": doc.get("supplement_reason"),
    }


async def validate_supplement_parent_locked(
    *, tenant_id: str, parent_daily_report_id: str,
) -> None:
    """Task 3 fix C: a supplement is only allowed against a SUBMITTED
    or APPROVED parent DR. Filing a supplement against a draft DR is
    nonsense -- the user should edit the draft directly. Raises 422
    when the parent is unlocked, 404 when missing."""
    db = get_db()
    parent = await db.daily_reports.find_one(
        tenant_filter(tenant_id, {"id": parent_daily_report_id}),
        {"_id": 0, "id": 1, "status": 1},
    )
    if not parent:
        raise HTTPException(
            404, f"parent_daily_report_id {parent_daily_report_id} not found",
        )
    if parent.get("status") not in ("submitted", "approved"):
        raise HTTPException(
            422,
            f"Cannot file a supplement against a daily report in status="
            f"{parent.get('status')!r}. Edit the draft DR directly.",
        )


async def validate_linked_task_ids(
    *,
    tenant_id: str,
    project_id: str,
    task_ids: Iterable[str],
    required: bool = True,
) -> list[str]:
    """Validate the linked_task_ids list against the master schedule.

    Returns the validated list (deduped, in input order). Raises
    HTTPException(422) on missing or cross-project task ids.

    Master-schedule scope: ids must resolve to `tasks` rows scoped to
    the same tenant + project. Cross-project task references are
    rejected. Past / today / future tasks are all valid -- the picker
    UI shows the full list and the cascade handles forward propagation.
    """
    ids = [str(i) for i in (task_ids or []) if i]
    # Dedup preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for i in ids:
        if i not in seen:
            deduped.append(i)
            seen.add(i)

    if required and not deduped:
        raise HTTPException(422, LINKAGE_MIN_ERR)
    if not deduped:
        return []

    db = get_db()
    rows = await db.tasks.find(
        tenant_filter(tenant_id, {
            "id": {"$in": deduped},
            "project_id": project_id,
        }),
        {"_id": 0, "id": 1},
    ).to_list(length=None)
    found_ids = {r["id"] for r in rows}
    missing = [i for i in deduped if i not in found_ids]
    if missing:
        raise HTTPException(
            422,
            f"linked_task_ids contains {len(missing)} id(s) that don't "
            f"resolve to a task on project {project_id}: "
            f"{', '.join(missing[:5])}"
            + (" ..." if len(missing) > 5 else ""),
        )
    return deduped
