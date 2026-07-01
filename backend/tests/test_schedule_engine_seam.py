"""CPM Mode 2 — Phase 0 ScheduleEngine seam tests (2026-06-29).

These cover the pure seam: the feature-flag helpers, per-project mode
resolution, engine selection, and that the not-yet-wired Mode-2 engine fails
loudly rather than silently no-op'ing. The BYTE-IDENTICAL proof for the Mode-1
path is the full existing CPM suite (test_cpm_correctness.py) still passing with
the flag OFF — auto_recompute_after_event short-circuits to the verbatim
_run_internal_cpm_chain when FF_SCHEDULE_MODE_2 is unset.

Pure: no DB stub needed. get_engine_for imports only feature_flags; the engine
classes import cpm/ms_project_sync lazily inside recompute().
"""
import asyncio
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services import feature_flags  # noqa: E402
from services.schedule_engine import (  # noqa: E402
    get_engine_for, InternalCpmEngine, MsProjectEngine,
)


# --- feature flag default + parsing ---------------------------------------

def test_mode2_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_SCHEDULE_MODE_2", raising=False)
    assert feature_flags.schedule_mode_2_enabled() is False
    monkeypatch.setenv("FF_SCHEDULE_MODE_2", "1")
    assert feature_flags.schedule_mode_2_enabled() is True
    monkeypatch.setenv("FF_SCHEDULE_MODE_2", "off")
    assert feature_flags.schedule_mode_2_enabled() is False


# --- per-project mode resolution ------------------------------------------

def test_schedule_mode_for_project():
    assert feature_flags.schedule_mode_for_project({"schedule_mode": "mode2"}) == "mode2"
    assert feature_flags.schedule_mode_for_project({"schedule_mode": "mode1"}) == "mode1"
    # missing / None / unknown / empty -> mode1 (safe default)
    assert feature_flags.schedule_mode_for_project({}) == "mode1"
    assert feature_flags.schedule_mode_for_project({"schedule_mode": None}) == "mode1"
    assert feature_flags.schedule_mode_for_project({"schedule_mode": "weird"}) == "mode1"
    assert feature_flags.schedule_mode_for_project(None) == "mode1"


# --- engine selection ------------------------------------------------------

def test_get_engine_for_defaults_to_internal():
    assert isinstance(get_engine_for({}), InternalCpmEngine)
    assert isinstance(get_engine_for({"schedule_mode": "mode1"}), InternalCpmEngine)
    assert isinstance(get_engine_for({"schedule_mode": None}), InternalCpmEngine)


def test_get_engine_for_mode2_selects_ms_project(monkeypatch):
    # get_engine_for now gates the mode2 branch on FF_SCHEDULE_MODE_2 (so the
    # MS-Project and enterprise features are fully isolated); the selection is
    # only valid when the flag is on -- the only context production reaches it.
    monkeypatch.setenv("FF_SCHEDULE_MODE_2", "1")
    assert isinstance(get_engine_for({"schedule_mode": "mode2"}), MsProjectEngine)
    # flag off -> mode2 project falls through to the internal engine.
    monkeypatch.setenv("FF_SCHEDULE_MODE_2", "off")
    assert isinstance(get_engine_for({"schedule_mode": "mode2"}), InternalCpmEngine)


# NOTE: MsProjectEngine.recompute's enqueue behavior (returning a {status:
# "queued"} ack) is covered in tests/test_ms_project_sync.py, which installs the
# in-memory DB stub the queue needs. Here we only assert the pure seam wiring
# above (flag, mode resolution, engine selection) which needs no DB.
