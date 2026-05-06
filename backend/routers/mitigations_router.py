"""Bucket C2 — AI mitigation scenarios for delays.

Phase 1: generate (3 scenarios per call) + read (active + history).
Phase 2: /choose and /unchoose with proportional schedule reflow.
Phase 3 will add /record-outcome and the on-resolve cascade.

All endpoints are tenant-scoped. The Delay detail page is the only
surface that consumes them — daily report and Tasks page stay clean.
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional

from database import get_db, tenant_filter
from models import Mitigation, MitigationChooseRequest, MitigationOutcomeRequest, gen_id, utc_now
from auth import require_tenant
from audit import write_audit


router = APIRouter(tags=["mitigations"])


@router.post("/delays/{delay_id}/generate-scenarios")
async def generate_scenarios(delay_id: str, ctx: dict = Depends(require_tenant)):
    """Generate (or regenerate) the 3 AI mitigation scenarios for a delay.

    Idempotent in spirit — every call replaces active_scenarios with a
    fresh A/B/C set; the prior set is moved to scenario_history with
    decision_status='superseded' so the calibration RAG can still see
    the old generations on later runs.
    """
    from services.ai_agents import generate_mitigation_scenarios
    db = get_db()
    delay = await db.delays.find_one(
        tenant_filter(ctx["tenant_id"], {"id": delay_id}), {"_id": 0},
    )
    if not delay:
        raise HTTPException(404, "Delay not found")

    try:
        scenarios = await generate_mitigation_scenarios(
            tenant_id=ctx["tenant_id"],
            delay_id=delay_id,
            proposed_by_user_id=ctx["user"]["id"],
        )
    except ValueError as e:
        # Pydantic / validation / AI-shape errors come through as ValueError
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(502, f"AI mitigation engine unavailable: {e}")

    # Single audit entry for the batch (per-mitigation INSERT audit gets
    # noisy; this captures the user-visible action).
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="mitigations", op="MITIGATION_PROPOSE",
        record_id=delay_id, before=None,
        after={"scenario_ids": [s["id"] for s in scenarios]},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    return {
        "delay_id": delay_id,
        "scenarios": [Mitigation(**s).model_dump(mode="json") for s in scenarios],
    }


@router.get("/delays/{delay_id}/scenarios")
async def list_scenarios(delay_id: str, ctx: dict = Depends(require_tenant)):
    """Active 3 (A/B/C) for the delay, plus a brief history summary
    (id + label + status) so the Delay detail page can render a
    "previous generations" disclosure without a second round-trip."""
    db = get_db()
    delay = await db.delays.find_one(
        tenant_filter(ctx["tenant_id"], {"id": delay_id}),
        {"_id": 0, "id": 1, "active_scenarios": 1, "scenario_history": 1,
         "chosen_mitigation_id": 1, "scenarios_last_generated_at": 1},
    )
    if not delay:
        raise HTTPException(404, "Delay not found")

    active_ids = delay.get("active_scenarios") or []
    history = delay.get("scenario_history") or []

    # Pull the active mitigations in label order (A < B < C).
    active_docs: list[dict] = []
    if active_ids:
        rows = await db.mitigations.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": active_ids}}),
            {"_id": 0},
        ).to_list(len(active_ids))
        by_id = {r["id"]: r for r in rows}
        for mid in active_ids:
            r = by_id.get(mid)
            if r:
                active_docs.append(r)

    # History summary: trim to id + label + decision_status + key numbers.
    history_summary: list[dict] = []
    history_ids = [sid for entry in history for sid in (entry.get("scenario_ids") or [])]
    if history_ids:
        hist_rows = await db.mitigations.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": history_ids}}),
            {"_id": 0, "id": 1, "scenario_label": 1, "strategy_name": 1,
             "decision_status": 1, "estimated_schedule_impact_days": 1,
             "estimated_cost_impact_usd": 1, "estimated_confidence": 1,
             "actual_time_recovered_days": 1, "actual_cost_usd": 1},
        ).to_list(len(history_ids))
        hist_by_id = {r["id"]: r for r in hist_rows}
        for entry in history:
            for sid in (entry.get("scenario_ids") or []):
                r = hist_by_id.get(sid)
                if r:
                    history_summary.append({
                        **r,
                        "generation_at": entry.get("generated_at"),
                    })

    return {
        "delay_id": delay_id,
        "scenarios_last_generated_at": delay.get("scenarios_last_generated_at"),
        "chosen_mitigation_id": delay.get("chosen_mitigation_id"),
        "active": [Mitigation(**a).model_dump(mode="json") for a in active_docs],
        "history": history_summary,
    }


# ============================================================
# Phase 2 — Decision capture + schedule reflow
# ============================================================

def _redistribute_impacts(prior_impacts: list[dict], new_total_days: float) -> list[dict]:
    """Split a chosen scenario's total schedule_impact_days across the
    delay's linked tasks proportionally to their existing share. Falls
    back to equal split when the prior total is zero (e.g., the delay
    was logged with 0-day impacts). Numbers are rounded to 2 decimals
    so they read cleanly in the UI but still support 0.5-day grains."""
    n = len(prior_impacts)
    if n == 0:
        return []
    prior_total = sum(float(i.get("projected_impact_days") or 0) for i in prior_impacts)
    out: list[dict] = []
    for imp in prior_impacts:
        new_imp = dict(imp)
        if prior_total > 0:
            share = float(imp.get("projected_impact_days") or 0) / prior_total
            new_imp["projected_impact_days"] = round(new_total_days * share, 2)
        else:
            new_imp["projected_impact_days"] = round(new_total_days / n, 2)
        # Recompute consumes_float / pushes_critical_path against the
        # snapshotted float_at_logging — keeps the indicator semantics
        # consistent with what the user saw at logging time.
        proj = float(new_imp["projected_impact_days"])
        f_val = imp.get("float_at_logging_days")
        if f_val is not None:
            new_imp["consumes_float_only"] = proj <= float(f_val)
            # CP push only flips if it's now exceeding float; we don't
            # know is_critical_path at redistribute time so we keep the
            # original. The CPM walk-forward (compute_projected_finish_dates)
            # computes the authoritative projected_finish_date downstream.
            if not new_imp["consumes_float_only"]:
                new_imp["pushes_critical_path"] = imp.get("pushes_critical_path", False)
            else:
                new_imp["pushes_critical_path"] = False
        out.append(new_imp)
    return out


@router.post("/mitigations/{mitigation_id}/choose")
async def choose_mitigation(
    mitigation_id: str, payload: MitigationChooseRequest,
    ctx: dict = Depends(require_tenant),
):
    """Mark this scenario as chosen, mark its peers in the same active
    set as not_chosen, snapshot the delay's per_task_impacts, then
    redistribute the chosen scenario's schedule_impact_days across the
    linked tasks proportionally. The C1 CPM walk-forward picks up the
    new per_task_impacts on the next event (we trigger one explicitly)."""
    db = get_db()
    m = await db.mitigations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": mitigation_id}), {"_id": 0},
    )
    if not m:
        raise HTTPException(404, "Mitigation not found")
    delay = await db.delays.find_one(
        tenant_filter(ctx["tenant_id"], {"id": m["linked_delay_id"]}), {"_id": 0},
    )
    if not delay:
        raise HTTPException(404, "Linked delay not found")
    if delay.get("chosen_mitigation_id"):
        raise HTTPException(
            409,
            "A scenario is already chosen for this delay. Click 'Change choice' to revert first.",
        )
    if mitigation_id not in (delay.get("active_scenarios") or []):
        # Defensive — chooser should be one of the current 3, not a superseded one.
        raise HTTPException(
            409,
            "Cannot choose a scenario that is no longer in the active set. Regenerate first.",
        )
    if delay.get("status") == "resolved":
        raise HTTPException(403, "Cannot choose a scenario on a resolved delay.")

    now_iso = utc_now().isoformat()

    # 1. Snapshot the delay's per_task_impacts BEFORE we redistribute,
    #    so /unchoose can restore them.
    pre_choose_snapshot = list(delay.get("per_task_impacts") or [])

    # 2. Redistribute proportionally based on the chosen scenario's total
    #    estimated_schedule_impact_days. Acceleration scenarios with a
    #    negative number reduce the projected impact (can go to 0 or below).
    new_total = float(m.get("estimated_schedule_impact_days") or 0)
    new_impacts = _redistribute_impacts(pre_choose_snapshot, new_total)

    # 3. Update the chosen mitigation.
    await db.mitigations.update_one(
        {"tenant_id": ctx["tenant_id"], "id": mitigation_id},
        {"$set": {
            "decision_status": "chosen",
            "decision_made_at": now_iso,
            "decision_made_by_user_id": ctx["user"]["id"],
            "decision_rationale": payload.decision_rationale,
            "updated_at": now_iso,
        }},
    )
    # 4. Mark the other active scenarios as not_chosen.
    peer_ids = [pid for pid in (delay.get("active_scenarios") or []) if pid != mitigation_id]
    if peer_ids:
        await db.mitigations.update_many(
            {"tenant_id": ctx["tenant_id"], "id": {"$in": peer_ids}},
            {"$set": {"decision_status": "not_chosen", "updated_at": now_iso}},
        )
    # 5. Update the delay record: chosen pointer + new impacts + snapshot.
    await db.delays.update_one(
        {"tenant_id": ctx["tenant_id"], "id": delay["id"]},
        {"$set": {
            "chosen_mitigation_id": mitigation_id,
            "per_task_impacts": new_impacts,
            "per_task_impacts_pre_choose": pre_choose_snapshot,
            # Keep linked_task_ids identical — choosing a scenario doesn't
            # change which tasks are linked, only the impact magnitude.
        }},
    )

    # 6. Audit + CPM recompute (same C1 path the delay PATCH uses).
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="mitigations", op="MITIGATION_CHOOSE",
        record_id=mitigation_id,
        before={"decision_status": m.get("decision_status")},
        after={"decision_status": "chosen", "delay_id": delay["id"],
                "schedule_impact_days": new_total, "per_task_impacts": new_impacts},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    from services.task_status import compute_and_persist_status, compute_projected_finish_dates
    for tid in (delay.get("linked_task_ids") or []):
        await compute_and_persist_status(db, ctx["tenant_id"], tid,
                                          actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if delay.get("linked_task_ids"):
        await compute_projected_finish_dates(db, ctx["tenant_id"], delay["project_id"],
                                              actor_id=ctx["user"]["id"], request_id=ctx["request_id"])

    after_m = await db.mitigations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": mitigation_id}), {"_id": 0},
    )
    after_d = await db.delays.find_one(
        tenant_filter(ctx["tenant_id"], {"id": delay["id"]}), {"_id": 0},
    )
    return {
        "mitigation": Mitigation(**after_m).model_dump(mode="json"),
        "delay_id": delay["id"],
        "delay_per_task_impacts": after_d.get("per_task_impacts"),
    }


@router.post("/mitigations/{mitigation_id}/unchoose")
async def unchoose_mitigation(mitigation_id: str, ctx: dict = Depends(require_tenant)):
    """Undo a /choose: revert this mitigation to proposed, peers back to
    proposed too (ready for re-evaluation), restore the delay's
    per_task_impacts from the snapshot, recompute status + CPM. Refused
    once the delay is resolved — at that point the choice is part of the
    permanent record and any change goes through Phase 3 outcome capture."""
    db = get_db()
    m = await db.mitigations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": mitigation_id}), {"_id": 0},
    )
    if not m:
        raise HTTPException(404, "Mitigation not found")
    delay = await db.delays.find_one(
        tenant_filter(ctx["tenant_id"], {"id": m["linked_delay_id"]}), {"_id": 0},
    )
    if not delay:
        raise HTTPException(404, "Linked delay not found")
    if delay.get("chosen_mitigation_id") != mitigation_id:
        raise HTTPException(409, "This mitigation isn't currently the chosen scenario.")
    if delay.get("status") == "resolved":
        raise HTTPException(
            403,
            "Cannot unchoose a scenario after the delay is resolved; record outcome instead.",
        )

    now_iso = utc_now().isoformat()
    snapshot = delay.get("per_task_impacts_pre_choose")

    # 1. Revert this mitigation + every active peer to 'proposed'.
    active_ids = delay.get("active_scenarios") or []
    if active_ids:
        await db.mitigations.update_many(
            {"tenant_id": ctx["tenant_id"], "id": {"$in": active_ids}},
            {"$set": {
                "decision_status": "proposed",
                "decision_made_at": None,
                "decision_made_by_user_id": None,
                "decision_rationale": None,
                "updated_at": now_iso,
            }},
        )

    # 2. Restore the delay's per_task_impacts from snapshot (or leave
    #    current if no snapshot — defensive for older data).
    delay_set: dict = {
        "chosen_mitigation_id": None,
        "per_task_impacts_pre_choose": None,
    }
    if snapshot is not None:
        delay_set["per_task_impacts"] = snapshot
    await db.delays.update_one(
        {"tenant_id": ctx["tenant_id"], "id": delay["id"]},
        {"$set": delay_set},
    )

    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="mitigations", op="MITIGATION_UNCHOOSE",
        record_id=mitigation_id,
        before={"decision_status": "chosen", "delay_id": delay["id"]},
        after={"decision_status": "proposed", "restored_per_task_impacts": snapshot},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    # 3. CPM recompute so projected_finish_date unwinds to the original
    #    delay impact (or stays the same if snapshot equalled new impacts).
    from services.task_status import compute_and_persist_status, compute_projected_finish_dates
    for tid in (delay.get("linked_task_ids") or []):
        await compute_and_persist_status(db, ctx["tenant_id"], tid,
                                          actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if delay.get("linked_task_ids"):
        await compute_projected_finish_dates(db, ctx["tenant_id"], delay["project_id"],
                                              actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return {"ok": True, "delay_id": delay["id"]}


# ============================================================
# Phase 3 — Execution tracking + outcome capture
# ============================================================

@router.patch("/mitigations/{mitigation_id}/record-outcome")
async def record_outcome(
    mitigation_id: str, payload: MitigationOutcomeRequest,
    ctx: dict = Depends(require_tenant),
):
    """Record actual cost / time / notes after the chosen mitigation has
    executed. Allowed for mitigations whose decision_status is in the
    executed-or-already-recorded states (so users can edit a previous
    outcome with corrections). Setting any one of the three fields counts
    as a recording — the others can be backfilled later by re-calling
    this endpoint."""
    db = get_db()
    m = await db.mitigations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": mitigation_id}), {"_id": 0},
    )
    if not m:
        raise HTTPException(404, "Mitigation not found")
    # Sanity gate — outcome only makes sense for a chosen scenario whose
    # delay has been resolved (executed) or where outcome was previously
    # recorded (we allow corrections).
    if m.get("decision_status") not in ("chosen", "executed", "outcome_recorded"):
        raise HTTPException(
            409,
            "Cannot record outcome on a mitigation that wasn't chosen and executed.",
        )

    raw = payload.model_dump(mode="json", exclude_unset=True)
    # All-empty payload? Reject so we don't write a zero-information row.
    if not any(v is not None for v in raw.values()):
        raise HTTPException(400, "Provide at least one of actual_cost_usd, actual_time_recovered_days, outcome_notes.")

    now_iso = utc_now().isoformat()
    updates: dict = {k: v for k, v in raw.items() if v is not None}
    updates["decision_status"] = "outcome_recorded"
    updates["outcome_recorded_at"] = now_iso
    updates["updated_at"] = now_iso

    await db.mitigations.update_one(
        {"tenant_id": ctx["tenant_id"], "id": mitigation_id},
        {"$set": updates},
    )

    after = await db.mitigations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": mitigation_id}), {"_id": 0},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="mitigations", op="OUTCOME_RECORD",
        record_id=mitigation_id,
        before={k: m.get(k) for k in ("decision_status", "actual_cost_usd",
                                        "actual_time_recovered_days", "outcome_notes")},
        after=updates,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return Mitigation(**after).model_dump(mode="json")
