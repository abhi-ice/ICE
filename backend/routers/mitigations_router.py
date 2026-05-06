"""Bucket C2 — AI mitigation scenarios for delays.

Phase 1: generate (3 scenarios per call) + read (active + history).
Phase 2 will add /choose and /unchoose. Phase 3 will add /record-outcome
and the on-resolve cascade.

All endpoints are tenant-scoped. The Delay detail page is the only
surface that consumes them — daily report and Tasks page stay clean.
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional

from database import get_db, tenant_filter
from models import Mitigation, gen_id, utc_now
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
