"""Schedule-engine strategy seam (Mode 1 / Mode 2). CPM Mode 2, 2026-06-29.

Mode 1 (InternalCpmEngine) is byte-identical to the pre-Mode-2 behavior of
cpm.auto_recompute_after_event: it runs the verbatim internal chain
(compute_cpm -> compute_projected_finish_dates -> compute_remaining_float_for_tasks)
and returns the compute_cpm result dict.

Mode 2 (MsProjectEngine) will enqueue a PUSH job for the Windows MS Project
worker and return immediately with {status: "queued", ...}; the MS-Project-
computed dates are ingested asynchronously by the worker /results postback,
NOT by the recompute call (MS Project recalculates out-of-band). Until the
worker + job queue land (build-plan Phase 1+), MsProjectEngine.recompute
raises a clear NotImplementedError rather than silently no-op'ing -- this
path is unreachable while FF_SCHEDULE_MODE_2 is OFF and no project is set to
schedule_mode="mode2".

This module is only imported when FF_SCHEDULE_MODE_2 is ON (the hook in
cpm.py short-circuits to the internal chain when the flag is OFF), so it adds
zero surface area to the default Mode-1 system.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class ScheduleEngine(ABC):
    """Strategy: 'recompute the schedule for this project after an event.'"""

    @abstractmethod
    async def recompute(
        self, tenant_id: str, project_id: str, *,
        actor_id: Optional[str] = None, request_id: Optional[str] = None,
    ) -> Optional[dict]:
        ...


class InternalCpmEngine(ScheduleEngine):
    """Mode 1 -- our own CPM engine. Delegates to the verbatim internal chain
    so behavior is identical to the pre-Mode-2 hook."""

    async def recompute(self, tenant_id, project_id, *, actor_id=None, request_id=None):
        from services.cpm import _run_internal_cpm_chain
        return await _run_internal_cpm_chain(
            tenant_id, project_id, actor_id=actor_id, request_id=request_id,
        )


class MsProjectEngine(ScheduleEngine):
    """Mode 2 -- Microsoft Project's engine drives the schedule via the Windows
    worker. recompute() enqueues a PUSH job and returns {status: "queued"};
    the computed dates are ingested asynchronously by the worker /results
    handler. Not yet wired (build-plan Phase 1+ adds services.ms_project_sync
    and the worker); raises until then so a misconfiguration fails loudly."""

    async def recompute(self, tenant_id, project_id, *, actor_id=None, request_id=None):
        try:
            from services.ms_project_sync import enqueue_push_job, last_known_summary
        except ImportError as exc:
            raise NotImplementedError(
                "Schedule Mode 2 (MS Project engine) is not yet wired: the "
                "ms_project_sync worker/queue layer (build-plan Phase 1+) is "
                "not present. A project must not be set to schedule_mode='mode2' "
                "until the worker is deployed and its live-Project validation "
                "gate is green."
            ) from exc
        job = await enqueue_push_job(
            tenant_id, project_id, actor_id=actor_id, request_id=request_id,
        )
        stale = await last_known_summary(tenant_id, project_id)
        return {
            "status": "queued",
            "job_id": job["job_id"],
            "seq": job.get("seq"),
            "stale": stale,
        }


class EnterpriseCpmEngine(ScheduleEngine):
    """Enterprise CPM / "Diagram 2" (2026-06-29) -- ICE's own P6-class engine:
    forward+backward iterative pass, data date + remaining duration, 9
    constraint types, calendars, retained-logic vs progress-override, the
    controlling/driving-chain critical path, finish + free float, and an
    integrity report. Computes IN-PROCESS (no external worker) and writes its
    OWN output columns plus a read-only display mirror, so every downstream
    surface renders unchanged. Reached only when FF_ENTERPRISE_CPM is on and a
    project is schedule_engine='enterprise'."""

    async def recompute(self, tenant_id, project_id, *, actor_id=None, request_id=None):
        from services.enterprise_cpm import run_enterprise_cpm
        return await run_enterprise_cpm(
            tenant_id, project_id, actor_id=actor_id, request_id=request_id,
        )


def get_engine_for(project: dict) -> ScheduleEngine:
    """Pick the engine for a project. Default the impact-days InternalCpmEngine;
    the EnterpriseCpmEngine wins first when the project is schedule_engine=
    'enterprise' (and FF_ENTERPRISE_CPM is on); else Mode 2 (MsProjectEngine)
    when schedule_mode='mode2'. Each selector is gated by its own flag inside
    the feature_flags helpers, so an unset flag falls through to Mode 1."""
    from services.feature_flags import (
        enterprise_cpm_enabled, schedule_engine_for_project,
        schedule_mode_2_enabled, schedule_mode_for_project,
    )
    # Each branch requires its OWN flag, so the two schedule features are fully
    # isolated: with only FF_ENTERPRISE_CPM on, a mode2 project still falls
    # through to the internal engine (and vice-versa), and the enterprise
    # module is never reached when its flag is off.
    if enterprise_cpm_enabled() and schedule_engine_for_project(project) == "enterprise":
        return EnterpriseCpmEngine()
    if schedule_mode_2_enabled() and schedule_mode_for_project(project) == "mode2":
        return MsProjectEngine()
    return InternalCpmEngine()
