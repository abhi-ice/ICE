"""Feature flags for the progress-driven scheduling work (B1 + B2).

CPM B1/B2 (2026-06-12) -- single source of truth for the two env-gated
features added by the progress-engine work:

  B1  FF_SLIDER_GATING    -- relationship-aware EOD-slider gating in the
                             Daily Report activity grid (FS/SS/FF/SF locks
                             + caps, most-restrictive predecessor, PM/Admin
                             override). Server validation + read-time gate
                             stamping + frontend clamp.
  B2  FF_PROGRESS_CASCADE  -- no-progress / behind auto-slip: a task whose
                             actual %% lags its planned %% at the data date
                             grows its remaining duration, slides its finish,
                             and cascades downstream through the SAME engine
                             a logged delay uses.

BOTH DEFAULT OFF. With both off the system is byte-identical to the
pre-B1/B2 codebase -- compute_projected_finish_dates, the activity-grid
PATCH, and the grid enrichment all take their original code paths. This
is the isolation contract: nothing changes for existing tenants until an
operator explicitly sets the env var.

Truthy values (case-insensitive): "1", "true", "yes", "on". Anything else
(including unset / empty) is OFF. Read live from os.environ on every call
so a test can monkeypatch os.environ without re-importing, and so a deploy
can flip the flag without a code change.
"""
from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def _enabled(var_name: str) -> bool:
    return (os.environ.get(var_name) or "").strip().lower() in _TRUTHY


def slider_gating_enabled() -> bool:
    """B1 -- relationship-aware EOD-slider gating. Default OFF."""
    return _enabled("FF_SLIDER_GATING")


def progress_cascade_enabled() -> bool:
    """B2 -- no-progress / behind auto-slip cascade. Default OFF."""
    return _enabled("FF_PROGRESS_CASCADE")


def schedule_mode_2_enabled() -> bool:
    """Mode 2 (2026-06-29) -- connect to MS Project; its engine drives the
    schedule via the Windows worker. Default OFF. When OFF, the schedule-engine
    seam in cpm.auto_recompute_after_event takes a byte-identical Mode-1 fast
    path (it never even reads the project doc)."""
    return _enabled("FF_SCHEDULE_MODE_2")


def schedule_mode_for_project(project: dict) -> str:
    """The schedule mode for a project: 'mode2' (MS Project engine) or 'mode1'
    (our internal CPM engine, the default). Read from the project doc's
    schedule_mode field; anything other than the literal 'mode2' is treated as
    'mode1' so a missing/legacy field is safe. Only consulted when
    schedule_mode_2_enabled() is True."""
    return "mode2" if (project or {}).get("schedule_mode") == "mode2" else "mode1"


def enterprise_cpm_enabled() -> bool:
    """Enterprise CPM / "Diagram 2" (2026-06-29) -- ICE's own P6-class engine,
    built BESIDE the impact-days engine. Default OFF. When OFF,
    cpm.auto_recompute_after_event takes the byte-identical Mode-1 fast path
    (it never even reads the project doc), so the default system is unchanged.
    Distinct from schedule_mode_2_enabled(): Mode 2 connects to MS Project;
    this picks which of OUR engines runs."""
    return _enabled("FF_ENTERPRISE_CPM")


def schedule_engine_for_project(project: dict) -> str:
    """Which of OUR engines a project uses: 'enterprise' (the Diagram-2 engine)
    or 'impact_days' (the default). Read from the project doc's schedule_engine
    field; anything other than the literal 'enterprise' is treated as
    'impact_days' so a missing/legacy field is safe. Only consulted when
    enterprise_cpm_enabled() is True."""
    return "enterprise" if (project or {}).get("schedule_engine") == "enterprise" else "impact_days"
