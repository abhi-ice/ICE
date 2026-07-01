"""B1 (2026-06-12) -- relationship-aware EOD-slider gating tests.

Pure tests for services.slider_gating.compute_slider_gate -- one assertion
per row of Sid's pinned table, plus most-restrictive-predecessor,
unresolved-predecessor, and the feature-flag default.

No DB / no network -- compute_slider_gate is a pure function over a task
dict + a {pred_code: pred_task} map. Path setup mirrors the other backend
unit suites.
"""
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.slider_gating import (  # noqa: E402
    compute_slider_gate, evaluate_eod_gate, OPEN_GATE,
    FF_INCOMPLETE_CAP, SF_INCOMPLETE_CAP, OVERRIDE_MIN_REASON,
)
from services import feature_flags  # noqa: E402


def _task(*preds):
    """A successor task with the given predecessor entries."""
    return {"id": "succ", "activity_code": "S", "predecessors": list(preds)}


def _pred(code, rel="FS", lag=0):
    return {"code": code, "type": rel, "lag_days": lag}


def _pstate(code, pct, status=None):
    return {code: {"id": f"task-{code}", "activity_code": code,
                   "pct_complete": pct, "status": status}}


# --- the pinned table, row by row -----------------------------------------

def test_fs_pred_incomplete_locks_at_zero():
    g = compute_slider_gate(_task(_pred("A", "FS")), _pstate("A", 40))
    assert g["locked"] is True and g["max_pct"] == 0.0
    assert g["relationship"] == "FS" and g["binding_pred_code"] == "A"


def test_fs_pred_complete_is_free():
    g = compute_slider_gate(_task(_pred("A", "FS")), _pstate("A", 100))
    assert g == OPEN_GATE


def test_fs_pred_complete_via_status_is_free():
    # pct lagged but status==complete forces 100 -> free.
    g = compute_slider_gate(_task(_pred("A", "FS")),
                            _pstate("A", 90, status="complete"))
    assert g == OPEN_GATE


def test_ss_pred_not_started_locks():
    g = compute_slider_gate(_task(_pred("A", "SS")), _pstate("A", 0))
    assert g["locked"] is True and g["max_pct"] == 0.0
    assert g["relationship"] == "SS"


def test_ss_pred_started_is_free():
    g = compute_slider_gate(_task(_pred("A", "SS")), _pstate("A", 5))
    assert g == OPEN_GATE


def test_ff_pred_incomplete_caps_at_95():
    g = compute_slider_gate(_task(_pred("A", "FF")), _pstate("A", 60))
    assert g["locked"] is False and g["max_pct"] == FF_INCOMPLETE_CAP
    assert g["relationship"] == "FF"


def test_ff_pred_complete_is_free():
    g = compute_slider_gate(_task(_pred("A", "FF")), _pstate("A", 100))
    assert g == OPEN_GATE


def test_sf_pred_not_started_caps_at_99():
    g = compute_slider_gate(_task(_pred("A", "SF")), _pstate("A", 0))
    assert g["locked"] is False and g["max_pct"] == SF_INCOMPLETE_CAP
    assert g["relationship"] == "SF"


def test_sf_pred_started_is_free():
    g = compute_slider_gate(_task(_pred("A", "SF")), _pstate("A", 10))
    assert g == OPEN_GATE


# --- multi-predecessor: most restrictive wins ------------------------------

def test_most_restrictive_predecessor_wins_fs_over_ff():
    # A: FF incomplete -> cap 95 ; B: FS incomplete -> lock 0. Lowest wins.
    task = _task(_pred("A", "FF"), _pred("B", "FS"))
    states = {**_pstate("A", 60), **_pstate("B", 50)}
    g = compute_slider_gate(task, states)
    assert g["max_pct"] == 0.0 and g["locked"] is True
    assert g["binding_pred_code"] == "B"


def test_multi_pred_all_free_is_open():
    task = _task(_pred("A", "FS"), _pred("B", "SS"))
    states = {**_pstate("A", 100), **_pstate("B", 30)}
    assert compute_slider_gate(task, states) == OPEN_GATE


def test_tie_prefers_locked_gate():
    # Two FS preds both incomplete: both lock at 0; result locked at 0.
    task = _task(_pred("A", "FS"), _pred("B", "FS"))
    states = {**_pstate("A", 10), **_pstate("B", 20)}
    g = compute_slider_gate(task, states)
    assert g["locked"] is True and g["max_pct"] == 0.0


# --- edge cases ------------------------------------------------------------

def test_no_predecessors_is_open():
    assert compute_slider_gate(_task(), {}) == OPEN_GATE


def test_unresolved_predecessor_is_skipped():
    # Predecessor task not present in the state map -> no constraint.
    g = compute_slider_gate(_task(_pred("GHOST", "FS")), {})
    assert g == OPEN_GATE


def test_returned_gate_is_a_copy_not_the_shared_constant():
    g = compute_slider_gate(_task(), {})
    g["max_pct"] = 1.0
    assert OPEN_GATE["max_pct"] == 100.0  # shared constant untouched


# --- server-side enforcement decision (evaluate_eod_gate) ------------------
# This is the LOAD-BEARING gate: the PATCH translates these decisions into
# a 422 / an override audit / a normal write. Tested directly so a future
# edit that disables enforcement is caught.

def _fs_task():
    """Successor with one FS predecessor 'A'."""
    return _task(_pred("A", "FS"))


def test_decision_allow_on_decrease():
    # Lowering progress is never gated, even with a locked predecessor.
    d = evaluate_eod_gate(_fs_task(), _pstate("A", 0),
                          old_eod=50, new_eod=20, role="super",
                          override_reason=None)
    assert d["action"] == "allow"


def test_decision_allow_within_cap():
    # FF cap 95; an increase to 90 stays within the cap -> allow.
    d = evaluate_eod_gate(_task(_pred("A", "FF")), _pstate("A", 50),
                          old_eod=80, new_eod=90, role="super",
                          override_reason=None)
    assert d["action"] == "allow"


def test_decision_block_past_cap_no_override():
    # FS pred incomplete locks at 0; an increase to 10 with no override -> block.
    d = evaluate_eod_gate(_fs_task(), _pstate("A", 40),
                          old_eod=0, new_eod=10, role="super",
                          override_reason=None)
    assert d["action"] == "block"
    assert d["gate"]["max_pct"] == 0.0 and d["gate"]["binding_pred_code"] == "A"


def test_decision_block_non_privileged_role_with_valid_reason():
    # A non-PM/Admin cannot override even with a long reason -> block.
    d = evaluate_eod_gate(_fs_task(), _pstate("A", 40),
                          old_eod=0, new_eod=10, role="super",
                          override_reason="legitimate sounding reason")
    assert d["action"] == "block"


def test_decision_block_pm_with_short_reason():
    # PM but reason below the min length -> block.
    short = "x" * (OVERRIDE_MIN_REASON - 1)
    d = evaluate_eod_gate(_fs_task(), _pstate("A", 40),
                          old_eod=0, new_eod=10, role="pm",
                          override_reason=short)
    assert d["action"] == "block"


def test_decision_override_pm_with_valid_reason():
    d = evaluate_eod_gate(_fs_task(), _pstate("A", 40),
                          old_eod=0, new_eod=100, role="pm",
                          override_reason="pred verbally confirmed complete")
    assert d["action"] == "override"
    assert d["gate"]["relationship"] == "FS"


def test_decision_override_admin_case_insensitive_role():
    d = evaluate_eod_gate(_fs_task(), _pstate("A", 40),
                          old_eod=0, new_eod=100, role="ADMIN",
                          override_reason="authorised by project executive")
    assert d["action"] == "override"


def test_decision_allow_when_no_gate():
    # No predecessors -> open gate -> any increase allowed.
    d = evaluate_eod_gate(_task(), {},
                          old_eod=0, new_eod=100, role="super",
                          override_reason=None)
    assert d["action"] == "allow"


def test_decision_above_cap_keep_or_decrease_allowed_increase_blocked():
    # A successor already at 50% whose predecessor is now FS-locked (cap 0):
    # keeping 50 or lowering it is ALLOWED (only increases past the cap are
    # gated); pushing to 60 is BLOCKED. This is the contract the frontend
    # effMax = max(cap, current) mirrors.
    task = _fs_task()
    states = _pstate("A", 0)  # predecessor not complete -> FS lock, cap 0
    keep = evaluate_eod_gate(task, states, old_eod=50, new_eod=50,
                             role="super", override_reason=None)
    assert keep["action"] == "allow"
    lower = evaluate_eod_gate(task, states, old_eod=50, new_eod=40,
                              role="super", override_reason=None)
    assert lower["action"] == "allow"
    raise_it = evaluate_eod_gate(task, states, old_eod=50, new_eod=60,
                                 role="super", override_reason=None)
    assert raise_it["action"] == "block"


def test_decision_boundary_exactly_at_cap_allowed_one_over_blocked():
    # FF cap is 95. Landing EXACTLY on the cap is allowed; one point over is
    # blocked. Nails the inclusive-cap boundary.
    task = _task(_pred("A", "FF"))
    states = _pstate("A", 50)
    at_cap = evaluate_eod_gate(task, states, old_eod=90, new_eod=95,
                               role="super", override_reason=None)
    assert at_cap["action"] == "allow"
    over_cap = evaluate_eod_gate(task, states, old_eod=95, new_eod=96,
                                 role="super", override_reason=None)
    assert over_cap["action"] == "block"


# --- feature flag default --------------------------------------------------

def test_slider_gating_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_SLIDER_GATING", raising=False)
    assert feature_flags.slider_gating_enabled() is False
    monkeypatch.setenv("FF_SLIDER_GATING", "1")
    assert feature_flags.slider_gating_enabled() is True
    monkeypatch.setenv("FF_SLIDER_GATING", "off")
    assert feature_flags.slider_gating_enabled() is False
