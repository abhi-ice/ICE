"""Bucket D Phase 5 -- QC -> rework -> CPM cascade.

When a QC issue is logged with severity='rework_required' and the
super enters rework_duration_days, the platform:
  1. Creates a synthetic Delay record (cause_class=quality_rework)
     with delay_days = the entered rework duration directly.
  2. Cross-links: qc_issue.linked_synthetic_delay_id ->
     delay.id; delay.linked_qc_issue_id -> qc.id.
  3. Triggers the existing C1 CPM cascade
     (services.task_status.compute_projected_finish_dates) so
     downstream activities pick up the projected slip.
  4. Audits QC_REWORK_DELAY_CREATED with both ids + super estimate.

QC rework days (2026-05-17) -- the rework duration is now entered in
WORKING DAYS (0.5-day increments), consistent with delay / RFI impact
units. The old "hours" input + h/8 conversion (_hours_to_days) were
removed: the entered value is already days and flows straight into
the synthetic delay's delay_days / per_task_impacts, so the synthetic
quality_rework delay is unit-identical to a manually-logged delay and
rides the SAME float-aware, holiday-aware cascade with no
special-casing.

KNOWN LIMITATION (accepted -- the cascade engine is NOT changed
here): the cascade (task_status.py compute_projected_finish_dates)
applies WHOLE-day impacts -- it does int(round(...)) on the per-task
impact, so a 0.5-day rework rounds to 0 working days of schedule
shift (1.5 -> 2, 2.5 -> 2). The half-day value is still stored +
displayed; the QC form shows a non-blocking warning so the super is
not misled.

Outcome capture (separate endpoint, mirrors C2 mitigation pattern):
  * Updates qc.rework_actual_days + outcome notes.
  * Recomputes synthetic delay's delay_days from actual.
  * Re-runs cascade.
  * Audits QC_REWORK_OUTCOME_RECORDED.
  * Calibration RAG ingests (estimate, actual, basis) -- the Phase
    C2 mitigation_outcomes table can carry these tuples too with
    source='qc_rework' for cross-domain training data.
"""
from __future__ import annotations
from typing import Optional

from database import get_db, tenant_filter
from models import gen_id, utc_now


async def create_synthetic_delay_for_qc(
    *, tenant_id: str, qc_issue: dict, actor_id: str, request_id: Optional[str] = None,
    owner_attribution: str = "gc",
) -> Optional[dict]:
    """Phase 5 entrypoint. Returns the synthetic delay doc on success;
    None if QC isn't rework_required or rework_duration_days not set.

    Idempotent: if the QC issue already carries linked_synthetic_delay_id,
    this is a no-op (returns the existing delay doc). Prevents duplicate
    synthetic delays if the cascade is triggered twice (e.g. retry after
    network blip, or future PATCH-severity-to-rework_required flows)."""
    if qc_issue.get("severity") != "rework_required":
        return None
    days = qc_issue.get("rework_duration_days")
    if not days or float(days) <= 0:
        return None

    db = get_db()
    # Bucket D verification fix -- skip when the cascade has already
    # been run for this QC issue. Returns the existing delay doc so
    # callers can rely on the back-pointer being set.
    existing_delay_id = qc_issue.get("linked_synthetic_delay_id")
    if existing_delay_id:
        existing = await db.delays.find_one(
            tenant_filter(tenant_id, {"id": existing_delay_id}), {"_id": 0},
        )
        if existing:
            return existing
        # Back-pointer set but the delay row is gone -- fall through
        # and recreate (fix the orphan back-pointer).
    # QC rework days (2026-05-17) -- the entered value is already in
    # working days; it flows straight into delay_days with NO
    # conversion, so the synthetic delay is unit-identical to a
    # manually-logged delay.
    delay_days = float(days)
    now_iso = utc_now().isoformat()
    delay_doc = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": qc_issue["project_id"],
        "daily_report_id": qc_issue.get("daily_report_id"),
        "date": qc_issue.get("date"),
        "description": (
            f"Rework caused by QC issue: {(qc_issue.get('description') or '').strip()[:300]}"
        ),
        "severity": "moderate",
        "status": "open",
        "delay_days": delay_days,
        "logged_date": qc_issue.get("date"),
        "reason_category": "other",
        "owner_attribution": owner_attribution,
        "cause_class": "quality_rework",
        # Cross-link to the source QC issue.
        "linked_qc_issue_id": qc_issue["id"],
        # Bucket D Corrective -- linked_task_ids comes directly from
        # qc_issue.linked_task_ids (master-schedule scope, no DR-activity
        # translation step needed). The cascade walks these task ids
        # forward through CPM successors.
        "linked_task_ids": list(qc_issue.get("linked_task_ids") or []),
        "per_task_impacts": [],
        "is_supplement": False,
        "reported_by": actor_id,
        "reported_at": now_iso,
        "created_at": now_iso,
    }

    # Build per-task impact rows from the QC's task linkage. Each task
    # gets the same projected_impact_days (the rework hours converted to
    # days); per-task float snapshots happen via _resolve_task_impacts
    # in the delay PATCH path on subsequent edits.
    if delay_doc["linked_task_ids"]:
        for sid in delay_doc["linked_task_ids"]:
            delay_doc["per_task_impacts"].append({
                "task_id": sid, "projected_impact_days": delay_days,
            })

    await db.delays.insert_one(delay_doc.copy())

    # Reverse-link on the QC row.
    await db.qc_issues.update_one(
        tenant_filter(tenant_id, {"id": qc_issue["id"]}),
        {"$set": {"linked_synthetic_delay_id": delay_doc["id"]}},
    )

    # Audit the cascade event.
    from audit import write_audit
    await write_audit(
        tenant_id=tenant_id, table_name="delays",
        op="QC_REWORK_DELAY_CREATED",
        record_id=delay_doc["id"], before=None, after={
            "qc_issue_id": qc_issue["id"],
            "delay_days": delay_days,
            "rework_duration_days": days,
            "linked_task_ids": delay_doc["linked_task_ids"],
            "owner_attribution": owner_attribution,
        },
        actor_id=actor_id, request_id=request_id,
    )

    # Run the existing C1 CPM cascade for the affected tasks.
    # CPM Part E (2026-05-15) -- the synthetic delay route was a
    # parallel path that did compute_and_persist_status +
    # compute_projected_finish_dates directly. Now folded through
    # auto_recompute_after_event so the synthetic delay benefits from
    # the same compute_cpm-first-then-project-finish ordering as the
    # user-facing /delays POST. Idempotent + fail-safe.
    if delay_doc["linked_task_ids"]:
        try:
            from services.task_status import compute_and_persist_status
            from services.cpm import auto_recompute_after_event
            for tid in delay_doc["linked_task_ids"]:
                await compute_and_persist_status(
                    db, tenant_id, tid, actor_id=actor_id, request_id=request_id,
                )
            await auto_recompute_after_event(
                tenant_id, qc_issue["project_id"],
                actor_id=actor_id, request_id=request_id,
            )
        except Exception:
            # Cascade hiccup shouldn't undo the delay write -- caller
            # can re-run cascade via the existing endpoints. The
            # auto_recompute_after_event helper is itself fail-safe
            # internally; this outer try/except guards against import
            # failures or signature drift.
            pass

    return delay_doc


async def record_rework_outcome(
    *, tenant_id: str, qc_issue: dict, actual_days: float,
    notes: Optional[str], actor_id: str, request_id: Optional[str] = None,
) -> dict:
    """Phase 5 outcome capture. Updates qc + synthetic delay,
    re-runs cascade, audits, returns the updated qc + delay docs.

    QC rework days (2026-05-17) -- actual_days is the rework actual in
    WORKING DAYS (was actual_hours; the h/8 conversion is gone)."""
    db = get_db()
    delay_id = qc_issue.get("linked_synthetic_delay_id")
    now_iso = utc_now().isoformat()

    actual_days = float(actual_days)

    qc_updates = {
        "rework_actual_days": actual_days,
        "rework_outcome_notes": notes,
        "rework_completed_at": now_iso,
        "rework_outcome_recorded_by": actor_id,
        "status": "closed",
        "resolved_at": now_iso,
    }
    await db.qc_issues.update_one(
        tenant_filter(tenant_id, {"id": qc_issue["id"]}), {"$set": qc_updates},
    )

    delay_updates: dict = {}
    if delay_id:
        delay_updates = {
            # Match the C1 resolved-delay shape -- delay_days stays as
            # the projected impact; actual_days_open / cpm_impact_summary
            # are computed by the cascade resolver. For Phase 5 we just
            # update delay_days to reflect actuals AND status=resolved
            # so the cascade can release float per the C1 Phase 5 logic.
            "delay_days": actual_days,
            "status": "resolved",
            "resolved_date": now_iso[:10],
            "resolved_note": (
                f"Rework completed; actual {actual_days}d. "
                + (notes or "")
            ).strip()[:500],
            "resolved_by_user_id": actor_id,
        }
        await db.delays.update_one(
            tenant_filter(tenant_id, {"id": delay_id}), {"$set": delay_updates},
        )

    from audit import write_audit
    await write_audit(
        tenant_id=tenant_id, table_name="qc_issues",
        op="QC_REWORK_OUTCOME_RECORDED",
        record_id=qc_issue["id"], before=None, after={
            "rework_actual_days": actual_days,
            "linked_synthetic_delay_id": delay_id,
            "notes": notes,
        },
        actor_id=actor_id, request_id=request_id,
    )

    # Calibration RAG ingest (Phase C2 pattern). One row per outcome
    # tuple, keyed by source='qc_rework' so Phase C2 mitigation
    # generator can pull cross-domain training data later.
    try:
        await db.mitigation_outcomes.insert_one({
            "id": gen_id(),
            "tenant_id": tenant_id,
            "project_id": qc_issue.get("project_id"),
            "source": "qc_rework",
            "source_id": qc_issue["id"],
            # QC rework days (2026-05-17) -- predicted + actual are
            # both WORKING DAYS now (the h/8 conversion is gone).
            "predicted_days": qc_issue.get("rework_duration_days"),
            "actual_days": actual_days,
            "estimate_basis": qc_issue.get("rework_duration_basis"),
            "outcome_notes": notes,
            "recorded_at": now_iso,
            "recorded_by": actor_id,
        })
    except Exception:
        pass

    # Re-run cascade (delay status changed -> may release float).
    # Bucket D Corrective -- linked_task_ids on the QC are master task
    # ids directly; no DR-activity translation step needed.
    # CPM Part E (2026-05-15) -- routed through auto_recompute_after_event
    # so the synthetic delay outcome capture path triggers the same
    # compute_cpm + projected_finish refresh as the user-facing PATCH
    # /delays/{id} flow. Fail-safe.
    try:
        from services.task_status import compute_and_persist_status
        from services.cpm import auto_recompute_after_event
        for tid in (qc_issue.get("linked_task_ids") or []):
            await compute_and_persist_status(
                db, tenant_id, tid,
                actor_id=actor_id, request_id=request_id,
            )
        await auto_recompute_after_event(
            tenant_id, qc_issue["project_id"],
            actor_id=actor_id, request_id=request_id,
        )
    except Exception:
        pass

    after_qc = await db.qc_issues.find_one(
        tenant_filter(tenant_id, {"id": qc_issue["id"]}), {"_id": 0},
    )
    after_delay = (await db.delays.find_one(
        tenant_filter(tenant_id, {"id": delay_id}), {"_id": 0},
    )) if delay_id else None
    return {"qc_issue": after_qc, "synthetic_delay": after_delay}


async def suggest_ai_benchmark(tenant_id: str, project_id: str) -> dict:
    """Phase 5 -- calibration RAG suggestion for rework DAYS. Pulls
    prior qc_rework outcomes and returns a {benchmark_days, basis}
    dict. Currently uses simple mean; future iteration can plug a
    similarity model (TF-IDF on QC description) per the C2 calibration
    pattern. Returns {benchmark_days: None, basis: ...} when no
    history exists.

    QC rework days (2026-05-17) -- queries actual_days (was
    actual_hours). Pre-change mitigation_outcomes rows that only
    carry actual_hours are simply skipped -- this is a pure-advisory
    feature and the history self-heals as new days-based outcomes
    are recorded."""
    db = get_db()
    rows = await db.mitigation_outcomes.find(
        tenant_filter(tenant_id, {
            "project_id": project_id, "source": "qc_rework",
            "actual_days": {"$type": "number", "$gt": 0},
        }),
        {"_id": 0, "actual_days": 1},
    ).to_list(length=200)
    if not rows:
        return {"benchmark_days": None,
                "basis": "No prior QC rework outcomes recorded; super judgment only."}
    actuals = [float(r["actual_days"]) for r in rows]
    avg = sum(actuals) / len(actuals)
    return {
        "benchmark_days": round(avg, 1),
        "basis": (
            f"Calibration RAG: similar QC issues averaged "
            f"~{round(avg, 1)}d based on {len(actuals)} prior outcomes "
            f"on this project."
        ),
    }
