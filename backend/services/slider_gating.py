"""B1 (2026-06-12) -- relationship-aware EOD-slider gating.

The Daily Report activity grid lets the field super report end-of-day
progress with a 0-100 slider. B1 makes that slider obey the schedule
network: a successor cannot earn progress its predecessors don't yet
allow. The exact rule set is Sid's pinned reference table (the
"Predecessor state -> Successor EOD slider behaviour" table on the
SiteOps CPM mindmap), implemented here as a single pure function.

PURE + VIEW/VALIDATION ONLY. This module is NOT imported by cpm.py /
task_status.py / working_days.py (the CPM math engine), and it does NOT
feed float / cascade / critical-path. It only decides how far the slider
may travel for a given row. The gate is computed read-time (stamped onto
grid rows for the frontend to clamp) and re-checked server-side in the
activity-grid PATCH. Gated behind FF_SLIDER_GATING -- with the flag off
neither call site invokes this module.

THE PINNED TABLE (most-restrictive predecessor wins):

  rel  predecessor state      successor slider
  ---  ---------------------  ------------------------------------------
  FS   pred  < 100%           LOCKED at 0%   (cannot start until pred done)
  FS   pred == 100%           free 0-100%
  SS   pred == 0%  (no start) LOCKED at 0%   (SS gates the START only)
  SS   pred  > 0%  (started)  free 0-100%
  FF   pred  < 100%           may progress, CAPPED ~95% (cannot finish
                              before the predecessor finishes)
  FF   pred == 100%           free 0-100%
  SF   pred not started       cannot reach 100% (capped ~99%)
  SF   pred started           may reach 100%
  Override  PM / Admin        can override any gate with a logged reason
  Multi-pred                  the most restrictive predecessor wins

The FF / SF caps are "completion" semantics, not crisp 0/100 locks, so
they get named constants rather than magic numbers.
"""
from __future__ import annotations

from typing import Optional

from models import pred_code, pred_relationship

# FF: a successor may keep producing but must not be marked finished while
# its predecessor is still open. 95% leaves a visible "not done" band.
FF_INCOMPLETE_CAP = 95.0
# SF: the successor's finish is tied to the predecessor's start; until the
# predecessor starts, the successor cannot be reported complete.
SF_INCOMPLETE_CAP = 99.0

# A fully-open gate -- the slider behaves exactly as it did pre-B1.
OPEN_GATE = {
    "locked": False,
    "max_pct": 100.0,
    "relationship": None,
    "binding_pred_code": None,
    "reason": "",
}

# Minimum length of a PM/Admin gate-override reason. Enforced both
# client-side (EodSlider) and server-side (evaluate_eod_gate / the PATCH).
OVERRIDE_MIN_REASON = 8


def _pred_progress(pred_task: dict) -> float:
    """Predecessor progress in [0, 100]. Reads pct_complete; a
    status of 'complete' forces 100 even if pct lagged behind."""
    try:
        pct = float(pred_task.get("pct_complete") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if str(pred_task.get("status") or "").lower() == "complete":
        pct = max(pct, 100.0)
    return max(0.0, min(100.0, pct))


def _gate_for_one(rel_type: str, pred_task: dict, pred_c: str) -> Optional[dict]:
    """The gate a single predecessor imposes on its successor's slider,
    or None when this predecessor imposes no constraint (free)."""
    p = _pred_progress(pred_task)
    finished = p >= 100.0
    started = p > 0.0

    if rel_type == "SS":
        if not started:
            return {"locked": True, "max_pct": 0.0, "relationship": "SS",
                    "binding_pred_code": pred_c,
                    "reason": f"Start-to-Start: predecessor {pred_c} has not started."}
        return None  # started -> free
    if rel_type == "FF":
        if not finished:
            return {"locked": False, "max_pct": FF_INCOMPLETE_CAP,
                    "relationship": "FF", "binding_pred_code": pred_c,
                    "reason": f"Finish-to-Finish: cannot finish before predecessor "
                              f"{pred_c} finishes."}
        return None  # finished -> free
    if rel_type == "SF":
        if not started:
            return {"locked": False, "max_pct": SF_INCOMPLETE_CAP,
                    "relationship": "SF", "binding_pred_code": pred_c,
                    "reason": f"Start-to-Finish: cannot reach 100% until predecessor "
                              f"{pred_c} starts."}
        return None  # started -> free
    # FS (default).
    if not finished:
        return {"locked": True, "max_pct": 0.0, "relationship": "FS",
                "binding_pred_code": pred_c,
                "reason": f"Finish-to-Start: predecessor {pred_c} is not complete."}
    return None  # finished -> free


def compute_slider_gate(task: dict, pred_states: dict) -> dict:
    """The effective EOD-slider gate for one grid row's linked task.

    `pred_states` maps a predecessor activity_code -> the predecessor task
    dict (must carry pct_complete + status). A predecessor whose task is
    absent from the map is skipped (no constraint) -- mirrors the cascade's
    "skip unresolved predecessor" behaviour and avoids gating a row on a
    predecessor that no longer exists.

    Returns the MOST RESTRICTIVE gate across all predecessors:
      {locked: bool, max_pct: float, relationship: str|None,
       binding_pred_code: str|None, reason: str}
    A row with no gating predecessor returns OPEN_GATE (max_pct 100).
    Restrictiveness order: lowest max_pct wins; among equal max_pct a
    locked gate beats an unlocked one.
    """
    best: Optional[dict] = None
    for pred_entry in (task.get("predecessors") or []):
        c = pred_code(pred_entry)
        if not c:
            continue
        pred_task = pred_states.get(c)
        if not pred_task:
            continue  # unresolved predecessor -> no constraint
        rel_type, _lag = pred_relationship(pred_entry)
        gate = _gate_for_one(rel_type, pred_task, c)
        if gate is None:
            continue
        if best is None:
            best = gate
            continue
        # Most restrictive: lower max_pct wins; tie -> prefer locked.
        if (gate["max_pct"] < best["max_pct"]
                or (gate["max_pct"] == best["max_pct"]
                    and gate["locked"] and not best["locked"])):
            best = gate
    return dict(best) if best is not None else dict(OPEN_GATE)


def evaluate_eod_gate(task, pred_states, *, old_eod, new_eod, role,
                      override_reason):
    """Decide what to do with an EOD%% change under FF_SLIDER_GATING.

    This is the LOAD-BEARING server-side enforcement, factored out of the
    activity-grid PATCH so it is pure + directly testable. The PATCH only
    fetches predecessor state, calls this, and translates the decision into
    a 422 / an override audit / a normal write.

    Returns one of:
      {"action": "allow"}                  -- not an increase, or within the
                                              predecessor gate's cap
      {"action": "override", "gate": gate} -- past the cap, but the actor is
                                              admin/pm AND supplied a reason
                                              of at least OVERRIDE_MIN_REASON
                                              characters (audit + allow)
      {"action": "block", "gate": gate}    -- past the cap, no valid override
                                              (the PATCH raises 422)

    `role` is the actor's role string; `override_reason` is the optional
    control field from the PATCH. Lowering progress or staying within the
    cap is ALWAYS allowed (an increase is the only constrained direction).
    """
    try:
        old_v = float(old_eod or 0)
        new_v = float(new_eod or 0)
    except (TypeError, ValueError):
        return {"action": "allow"}
    if new_v <= old_v:
        return {"action": "allow"}
    gate = compute_slider_gate(task, pred_states)
    if new_v <= gate["max_pct"]:
        return {"action": "allow"}
    reason_ok = (
        bool(override_reason)
        and len(str(override_reason).strip()) >= OVERRIDE_MIN_REASON
    )
    is_privileged = (str(role or "").lower() in ("admin", "pm"))
    if is_privileged and reason_ok:
        return {"action": "override", "gate": gate}
    return {"action": "block", "gate": gate}
