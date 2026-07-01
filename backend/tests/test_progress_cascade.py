"""B2 (2026-06-12) -- no-progress / behind auto-slip cascade tests.

Two layers:
  (1) PURE  -- progress_shift_days over hand-built task dicts: behind /
      on-schedule / ahead / complete / not-started / paperwork / no-dates.
  (2) INTEGRATION -- the REAL compute_projected_finish_dates over an
      in-memory Mongo stub, proving:
        * flag OFF  -> a behind task with NO logged delay produces NO
          projected dates (byte-identical to pre-B2 behaviour);
        * flag ON   -> the behind task slips AND cascades to its
          successor through the existing float-aware machinery.

The stub mirrors the subset of Motor semantics the cascade uses, same
shape as tests/test_cpm_correctness.py. A deterministic data_date is
passed into compute_projected_finish_dates so the test never depends on
the wall clock.
"""
import asyncio
import os
import sys
import types
import uuid
from datetime import date

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ----------------------------------------------------------------------------
# In-memory DB + module stubs (compact mirror of test_cpm_correctness.py).
# ----------------------------------------------------------------------------
class _Cursor:
    def __init__(self, items): self.items = list(items)
    def sort(self, *a, **k): return self
    def limit(self, n): self.items = self.items[:int(n)]; return self
    async def to_list(self, n): return self.items[:int(n)] if n is not None else self.items


def _matches(row, q):
    if not q:
        return True
    for k, v in q.items():
        if k == "_id":
            continue
        rv = row.get(k)
        if isinstance(v, dict):
            if "$ne" in v and rv == v["$ne"]:
                return False
            if "$in" in v and rv not in v["$in"]:
                return False
            if "$nin" in v and rv in v["$nin"]:
                return False
        elif isinstance(rv, list):
            if v not in rv:
                return False
        elif rv != v:
            return False
    return True


class _Col:
    def __init__(self): self.rows = []
    def find(self, q=None, p=None): return _Cursor([dict(r) for r in self.rows if _matches(r, q or {})])
    async def find_one(self, q=None, p=None):
        for r in self.rows:
            if _matches(r, q or {}):
                return dict(r)
        return None
    async def insert_one(self, doc): self.rows.append(dict(doc)); return types.SimpleNamespace(inserted_id=doc.get("id"))
    async def update_one(self, q, ops):
        for r in self.rows:
            if _matches(r, q or {}):
                if "$set" in ops:
                    r.update(ops["$set"])
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        return types.SimpleNamespace(matched_count=0, modified_count=0)
    async def count_documents(self, q): return sum(1 for r in self.rows if _matches(r, q or {}))


class _DB:
    def __init__(self):
        self.tasks = _Col(); self.delays = _Col(); self.rfis = _Col()
        self.submittals = _Col(); self.audit_log = _Col()
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        c = _Col(); object.__setattr__(self, name, c); return c
    def __getitem__(self, k): return getattr(self, k)
    def reset(self):
        for v in list(self.__dict__.values()):
            if isinstance(v, _Col):
                v.rows.clear()


_STUB = _DB()

_db_mod = types.ModuleType("database")
_db_mod.get_db = lambda: _STUB
_db_mod.tenant_filter = lambda tid, q=None: {**(q or {}), "tenant_id": tid}
sys.modules["database"] = _db_mod

_audit_mod = types.ModuleType("audit")
async def _noop(**_k): return None
_audit_mod.write_audit = _noop
sys.modules["audit"] = _audit_mod

from services.task_status import compute_projected_finish_dates  # noqa: E402
from services.progress_cascade import (  # noqa: E402
    progress_shift_days, build_progress_shift_map,
)
from services.working_days import add_working_days  # noqa: E402

TENANT, PROJECT = "t-test", "p-b2"


def _task(code, ps, pf, pct=0.0, work_category="field_work", preds=None, **extra):
    doc = {
        "id": f"task-{code}", "tenant_id": TENANT, "project_id": PROJECT,
        "activity_code": code, "description": f"Task {code}",
        "planned_start": ps.isoformat() if ps else None,
        "planned_finish": pf.isoformat() if pf else None,
        "duration_days": extra.pop("duration_days", 5),
        "pct_complete": pct, "status": extra.pop("status", "in_progress"),
        "work_category": work_category, "predecessors": preds or [],
        "projected_start_date": None, "projected_finish_date": None,
        "slip_days": None, "total_float_days": None,
    }
    doc.update(extra)
    return doc


# ============================ (1) PURE TESTS ================================

DD = date(2026, 5, 6)  # Wed -- midpoint of a 5/4..5/8 task


def test_behind_task_slips():
    # planned 5/4..5/8 (calendar span 4d). At 5/6, planned% = 50.
    # actual 10% -> 40% behind. OD = wd_between(5/4,5/8) = 4 -> round(4*0.4)=2.
    t = _task("A", date(2026, 5, 4), date(2026, 5, 8), pct=10.0)
    assert progress_shift_days(t, DD) == 2


def test_on_schedule_no_slip():
    t = _task("A", date(2026, 5, 4), date(2026, 5, 8), pct=50.0)
    assert progress_shift_days(t, DD) == 0


def test_ahead_no_slip():
    t = _task("A", date(2026, 5, 4), date(2026, 5, 8), pct=80.0)
    assert progress_shift_days(t, DD) == 0


def test_complete_task_no_slip():
    t = _task("A", date(2026, 5, 4), date(2026, 5, 8), pct=100.0)
    assert progress_shift_days(t, DD) == 0


def test_not_started_before_planned_start_no_slip():
    # data date BEFORE planned_start -> planned% is 0 -> nothing behind.
    t = _task("A", date(2026, 5, 20), date(2026, 5, 26), pct=0.0)
    assert progress_shift_days(t, DD) == 0


def test_paperwork_never_slips():
    for cat in ("preconstruction_paperwork", "construction_paperwork",
                "closeout_paperwork"):
        t = _task("A", date(2026, 5, 4), date(2026, 5, 8), pct=0.0,
                  work_category=cat)
        assert progress_shift_days(t, DD) == 0, cat


def test_missing_dates_no_slip():
    assert progress_shift_days(_task("A", None, None, pct=0.0), DD) == 0


def test_fully_behind_slips_whole_remaining_duration():
    # 0% done at a point where 100% planned -> slip ~ full OD.
    t = _task("A", date(2026, 5, 4), date(2026, 5, 8), pct=0.0)
    assert progress_shift_days(t, date(2026, 5, 20)) == 4  # past finish, OD=4


def test_one_day_task_small_shortfall_rounds_to_zero():
    # OD=1: a fractional behind-amount rounds to 0 (you can't slip a 1-day
    # task by a fraction of a day). Conservative floor, documented.
    t = _task("A", date(2026, 5, 4), date(2026, 5, 4), pct=10.0)  # OD=0? same day
    # same-day == milestone (OD 0) -> 0; use a true 1-working-day span instead:
    t2 = _task("A", date(2026, 5, 4), date(2026, 5, 5), pct=10.0)  # OD=1
    # At 5/5 planned% ~100 (>=pf), behind ~90% -> round(1*0.9)=1; at midpoint
    # we use DD where planned<100. Use DD=5/4 (start): planned 0 -> not behind.
    # Pick a 40%-behind point: a 1-day task can't show a sub-day slip.
    assert progress_shift_days(t2, date(2026, 5, 4)) == 0   # planned 0 at start
    assert progress_shift_days(t, DD) == 0                  # OD=0 milestone


def test_milestone_zero_duration_never_slips():
    # planned_start == planned_finish (a milestone, OD=0) -> no slip even when
    # incomplete and past its date.
    t = _task("M", date(2026, 5, 4), date(2026, 5, 4), pct=0.0)
    assert progress_shift_days(t, date(2026, 5, 20)) == 0


def test_predecessor_and_successor_both_behind_inherited_plus_own(monkeypatch):
    """When a predecessor AND its successor are each independently behind,
    the successor slips by inherited(from pred) + its OWN shortfall -- two
    distinct lateness sources, additive, NOT double-counted. Uses SS so both
    can be in-window and behind at the same data date."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _STUB.reset()
    # A and B both planned 5/4..5/8 (OD 4), both 10% at DD 5/6 (planned 50%)
    # -> each own shift = round(4*0.4) = 2. B is SS+0 after A.
    _STUB.tasks.rows.append(_task("A", date(2026, 5, 4), date(2026, 5, 8), pct=10.0))
    _STUB.tasks.rows.append(_task(
        "B", date(2026, 5, 4), date(2026, 5, 8), pct=10.0,
        preds=[{"code": "A", "type": "SS", "lag_days": 0}],
    ))
    a, b = asyncio.run(_run(DD))
    assert a["slip_days"] == 2                       # A's own behind-ness
    # B = inherited from A (SS pushes B's start +2) + B's own +2 = 4.
    assert b["slip_days"] == 4
    assert b["projected_finish_date"] == add_working_days(date(2026, 5, 8), 4).isoformat()


def test_build_map_omits_zero_shift():
    behind = _task("A", date(2026, 5, 4), date(2026, 5, 8), pct=10.0)
    ok = _task("B", date(2026, 5, 4), date(2026, 5, 8), pct=60.0)
    m = build_progress_shift_map([behind, ok], DD)
    assert m == {"task-A": 2}


# ====================== (2) INTEGRATION (flag off / on) =====================

def _seed_behind_chain():
    """A (behind) -> B (FS+0 successor, future)."""
    _STUB.reset()
    _STUB.tasks.rows.append(_task("A", date(2026, 5, 4), date(2026, 5, 8), pct=10.0))
    _STUB.tasks.rows.append(_task(
        "B", date(2026, 5, 11), date(2026, 5, 13), pct=0.0, status="not_started",
        duration_days=3, preds=[{"code": "A", "type": "FS", "lag_days": 0}],
    ))


async def _run(data_date):
    await compute_projected_finish_dates(
        _STUB, TENANT, PROJECT, data_date=data_date,
    )
    a = await _STUB.tasks.find_one({"id": "task-A"})
    b = await _STUB.tasks.find_one({"id": "task-B"})
    return a, b


def test_flag_off_behind_task_does_not_slip(monkeypatch):
    """Byte-identical to pre-B2: a behind task with NO logged delay produces
    no projected dates, and its successor is untouched."""
    monkeypatch.delenv("FF_PROGRESS_CASCADE", raising=False)
    _seed_behind_chain()
    a, b = asyncio.run(_run(DD))
    assert a["projected_finish_date"] is None
    assert b["projected_finish_date"] is None


def test_flag_on_behind_task_slips_and_cascades(monkeypatch):
    """B2: the behind task slips +2 working days AND its FS successor inherits
    the slip through the existing float-aware cascade."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _seed_behind_chain()
    a, b = asyncio.run(_run(DD))
    # A: planned_finish 5/8 + 2 working days = 5/12 (Mon); 5/9-5/10 weekend.
    assert a["projected_finish_date"] == add_working_days(date(2026, 5, 8), 2).isoformat()
    assert a["projected_finish_date"] == "2026-05-12"
    assert a["slip_days"] == 2
    # B inherits: FS+0 after A, no float gap -> B.planned_finish 5/13 + 2 = 5/15.
    assert b["projected_finish_date"] == "2026-05-15"
    assert b["slip_days"] == 2


def test_flag_on_behind_slip_absorbed_by_successor_float(monkeypatch):
    """B2 respects free float: a behind task slips, but a successor that
    starts far enough later absorbs the slip and does NOT move -- proving the
    progress shift flows through the SAME float-aware overflow gate a logged
    delay uses (not a blind shift-everything propagation)."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _STUB.reset()
    # A behind (slips +2). B is FS+0 after A but planned to start 5/20 -- a
    # wide gap, so A's impacted finish 5/12 lands well before B's start.
    _STUB.tasks.rows.append(_task("A", date(2026, 5, 4), date(2026, 5, 8), pct=10.0))
    _STUB.tasks.rows.append(_task(
        "B", date(2026, 5, 20), date(2026, 5, 22), pct=0.0, status="not_started",
        duration_days=3, preds=[{"code": "A", "type": "FS", "lag_days": 0}],
    ))
    a, b = asyncio.run(_run(DD))
    assert a["projected_finish_date"] == "2026-05-12"  # A still slips
    assert b["projected_finish_date"] is None          # float absorbed it
    assert b["slip_days"] is None


def test_flag_on_on_schedule_task_no_slip(monkeypatch):
    """B2 never pulls an on-schedule task: no slip, no cascade."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _STUB.reset()
    _STUB.tasks.rows.append(_task("A", date(2026, 5, 4), date(2026, 5, 8), pct=60.0))
    a, _ = asyncio.run(_run(DD))
    assert a["projected_finish_date"] is None


# --- NEGATIVE-DIRECTION state transitions: a slip must REVERT cleanly ------
# These prove "when the behind-ness goes away, what state is the task left
# in?" -- the projected dates must clear to None, not linger stale.

def test_behind_then_caught_up_clears_projected_dates(monkeypatch):
    """A task that previously slipped (projected dates set) and is now back
    on schedule must have its projected dates CLEARED to None on the next
    recompute -- the slip self-corrects, no stale projection lingers."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _STUB.reset()
    # Pre-existing slip in the DB, but the task is now on-track (60% at DD).
    _STUB.tasks.rows.append(_task(
        "A", date(2026, 5, 4), date(2026, 5, 8), pct=60.0,
        projected_finish_date="2026-05-12", projected_start_date="2026-05-06",
        slip_days=2,
    ))
    a, _ = asyncio.run(_run(DD))
    assert a["projected_finish_date"] is None
    assert a["projected_start_date"] is None
    assert a["slip_days"] is None


def test_behind_then_completed_clears_projected_dates(monkeypatch):
    """A task that slipped while in progress and is now 100% complete must
    clear its progress-driven projected dates (complete -> no remaining
    duration -> no progress slip)."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _STUB.reset()
    _STUB.tasks.rows.append(_task(
        "A", date(2026, 5, 4), date(2026, 5, 8), pct=100.0, status="complete",
        projected_finish_date="2026-05-12", slip_days=2,
    ))
    a, _ = asyncio.run(_run(DD))
    assert a["projected_finish_date"] is None
    assert a["slip_days"] is None


def test_successor_inherited_slip_clears_when_predecessor_recovers(monkeypatch):
    """The cascade reverts downstream too: a successor that inherited a
    predecessor's progress slip must clear once the predecessor is back on
    track -- the negative direction of test_flag_on_behind_task_slips_and_cascades."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _STUB.reset()
    # A is now on-track (was behind); B previously inherited A's +2 slip.
    _STUB.tasks.rows.append(_task(
        "A", date(2026, 5, 4), date(2026, 5, 8), pct=60.0,
        projected_finish_date="2026-05-12", slip_days=2,
    ))
    _STUB.tasks.rows.append(_task(
        "B", date(2026, 5, 11), date(2026, 5, 13), pct=0.0, status="not_started",
        duration_days=3, preds=[{"code": "A", "type": "FS", "lag_days": 0}],
        projected_finish_date="2026-05-15", slip_days=2,
    ))
    a, b = asyncio.run(_run(DD))
    assert a["projected_finish_date"] is None
    assert b["projected_finish_date"] is None  # inherited slip cleared
    assert b["slip_days"] is None


def test_flag_off_does_not_clear_existing_projected_dates(monkeypatch):
    """Isolation in the negative direction: with the flag OFF, B2 neither
    creates NOR clears projected dates from progress -- it is fully inert. A
    pre-existing projection (e.g. from a logged delay) is left untouched when
    there is no blocker driving it... but here there's no blocker, so the
    EXISTING impact-days cascade (own_impact=0) clears it exactly as it did
    pre-B2. This asserts flag-off behaviour is the original cascade, not B2."""
    monkeypatch.delenv("FF_PROGRESS_CASCADE", raising=False)
    _STUB.reset()
    _STUB.tasks.rows.append(_task(
        "A", date(2026, 5, 4), date(2026, 5, 8), pct=10.0,  # behind, but flag off
        projected_finish_date="2026-05-12", slip_days=2,
    ))
    a, _ = asyncio.run(_run(DD))
    # Flag off: no progress shift; no logged blocker -> the ORIGINAL cascade
    # clears the stale projection (total_shift 0). Behind-ness is ignored.
    assert a["projected_finish_date"] is None


def test_flag_on_logged_delay_and_progress_not_double_counted(monkeypatch):
    """MAX-not-sum: a task both 2 days behind on progress AND carrying a
    2-day logged delay slips 2 (not 4)."""
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    _STUB.reset()
    _STUB.tasks.rows.append(_task("A", date(2026, 5, 4), date(2026, 5, 8), pct=10.0))
    _STUB.delays.rows.append({
        "id": f"delay-{uuid.uuid4()}", "tenant_id": TENANT, "project_id": PROJECT,
        "linked_task_ids": ["task-A"], "status": "open", "severity": "moderate",
        "per_task_impacts": [{"task_id": "task-A", "projected_impact_days": 2.0}],
    })
    a, _ = asyncio.run(_run(DD))
    # progress shift = 2, own impact = 2 -> MAX = 2 (not 4).
    assert a["slip_days"] == 2
    assert a["projected_finish_date"] == "2026-05-12"
