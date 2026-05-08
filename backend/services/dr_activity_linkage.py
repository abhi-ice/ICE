"""Bucket D Phase 1 + Phase 4 -- DR-activity linkage validation helper
+ supplement stamping helper.

Every event create endpoint that requires per-DR WBS linkage
(delays / rfis / qc-issues / deliveries -- safety + coordination
are optional) imports `validate_linked_dr_activity_ids` and runs it
before persisting. Centralized here so the rule lives in one place
and all routers stay consistent.

Two checks:
  1. Min-1 if `required=True` -- raises 422 with a friendly message.
  2. Each id resolves to a real daily_report_activities row that
     belongs to the supplied daily_report_id (cross-DR linkage is
     not allowed -- a delay logged on Tuesday's DR cannot link to
     Monday's WBS row except via the supplement flow in Phase 4,
     where the parent_daily_report_id explicitly opts in).

Phase 4 adds `stamp_supplement_metadata`: validates supplement payload
shape and stamps supplement_filed_at/by, ready for insert. Returns
the audit-payload dict for the caller to write a SUPPLEMENT_FILED
audit entry alongside the entity's normal INSERT audit.
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
    requires DB access). Async wrapper validate_supplement_against_db
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


async def validate_linked_dr_activity_ids(
    *,
    tenant_id: str,
    daily_report_id: Optional[str],
    activity_ids: Iterable[str],
    required: bool = True,
    parent_daily_report_id: Optional[str] = None,
) -> list[str]:
    """Validate the linked_dr_activity_ids list against the DB.

    Returns the validated list (deduped, in input order). Raises
    HTTPException(422) on missing or cross-DR ids.

    `parent_daily_report_id` (Phase 4 supplement flow) overrides the
    DR-scope check: when set, ids must belong to that parent DR's
    activities rather than the container DR.
    """
    ids = [str(i) for i in (activity_ids or []) if i]
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

    scope_dr = parent_daily_report_id or daily_report_id
    if not scope_dr:
        # Required + no scope = caller programming error; surface
        # rather than silently accept ids of unknown provenance.
        raise HTTPException(
            422,
            "daily_report_id is required when linked_dr_activity_ids is set",
        )

    db = get_db()
    rows = await db.daily_report_activities.find(
        {"id": {"$in": deduped}, "daily_report_id": scope_dr},
        {"_id": 0, "id": 1},
    ).to_list(length=None)
    found_ids = {r["id"] for r in rows}
    missing = [i for i in deduped if i not in found_ids]
    if missing:
        # Tenant-guard cross-check via the parent DR's tenant.
        dr = await db.daily_reports.find_one(
            tenant_filter(tenant_id, {"id": scope_dr}),
            {"_id": 0, "id": 1},
        )
        if not dr:
            raise HTTPException(
                404, f"daily_report_id {scope_dr} not found in this tenant",
            )
        # DR exists in tenant but the ids don't match its activities.
        raise HTTPException(
            422,
            f"linked_dr_activity_ids contains {len(missing)} id(s) "
            f"that don't belong to daily report {scope_dr}: "
            f"{', '.join(missing[:5])}"
            + (" ..." if len(missing) > 5 else ""),
        )
    return deduped
