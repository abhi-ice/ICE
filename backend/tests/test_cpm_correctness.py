"""CPM Correctness Regression Tests — Part H (2026-05-15).

The 11 spec tests covering Parts A-G. Each test exercises the REAL
code paths (compute_cpm, compute_projected_finish_dates,
_resolve_task_impacts, auto_recompute_after_event,
create_synthetic_delay_for_qc) against an in-memory Mongo stub.
The stub mirrors the subset of Motor semantics the CPM helpers use
($ne, $in, $nin, array-contains-scalar, $set updates, find_one,
insert_one, delete_one, count_documents). Audit + auth modules are
no-op stubbed so the math under test executes without HTTP / RBAC /
audit-chain fanfare.

Whole purpose of this suite: prevent any future refactor from
silently breaking the math we just got right. If a future commit
flips the +1 day FS asymmetry, forgets to wire auto_recompute on a
new event type, or accidentally double-counts QC issues as
blockers (Bug #53 regression), one of these tests fails.

Concrete dates throughout -- 2026-05-04 (a Monday) is the anchor so
weekday/calendar mental math is consistent with the Part C/D round-
trips. Asserts are against real datetime.date values, not abstract
day labels.

Spec tests (11 / 11):

    1.  test_parallel_tasks_isolation
        Two tasks with no dependency between them. Logging a delay
        on one does NOT shift the other.

    2.  test_max_at_convergence
        Two predecessors converge into one successor. MAX wins on
        the forward pass; predecessor on the shorter path picks up
        positive float.

    3.  test_fs_lag_concrete_cure
        FS+7d lag (the canonical "let the concrete cure" pattern).
        Successor starts predecessor.EF + 1 + 7 days.

    4.  test_ss_relationship
        SS+2d. Successor starts 2 days after predecessor's start
        (NO +1 offset; parallel-start semantics).

    5.  test_ff_relationship
        FF+0. Both tasks finish the same day. Successor's start
        derived from successor's finish minus its duration.

    6.  test_sf_relationship
        SF+0 (rare). Successor's finish constrained to
        predecessor's start. Predecessor picks up positive float
        because SF decouples its finish from the successor's
        timeline.

    7.  test_lead_overlap
        FS-3d (lead/overlap). Successor starts before predecessor
        finishes; the +1 / -3 arithmetic produces an earlier ES.

    8.  test_auto_recompute_on_delay_create
        Part E hook fires on /delays POST: compute_cpm +
        compute_projected_finish_dates run automatically, and the
        audit captures the FRESH float in per_task_impacts.

    9.  test_stale_float_fixed     <-- the critical one
        Pre-Part-E, the per_task_impacts snapshot on a delay read
        STALE float (the value from before compute_cpm ran).
        Post-Part-E, the snapshot reads FRESH float because
        auto_recompute_after_event fires before the re-resolve.

    10. test_qc_link_only_no_cascade
        Bug #53: a QC issue linked to a task does NOT trigger CPM
        cascade. linked_task_ids stays on the QC row (for gallery
        filtering), but compute_projected_finish_dates does NOT
        sum QC into the open-delay impact pool.

    11. test_qc_rework_routes_through_synthetic_delay
        A QC issue with severity=rework_required spawns a
        synthetic delay via create_synthetic_delay_for_qc, and the
        synthetic delay's create flow runs the SAME
        auto_recompute_after_event as the user-facing /delays POST.

Run from repo root:
    python backend/tests/test_cpm_correctness.py     # standalone
    pytest backend/tests/test_cpm_correctness.py     # via pytest
"""
import asyncio
import os
import sys
import types
import uuid
from datetime import date, datetime, timedelta

# Path setup so `services.cpm` / `routers.field_events_router` resolve.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# =============================================================================
# In-memory DB stub. Mirrors the subset of Motor semantics the CPM helpers use.
# =============================================================================
class _FindCursor:
    """Mimics motor's AsyncIOMotorCursor: chainable sort/limit, awaitable
    to_list. We never use sort ordering in the CPM code paths, so .sort
    is a no-op."""

    def __init__(self, items):
        self.items = list(items)

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, n):
        self.items = self.items[: int(n)]
        return self

    async def to_list(self, n):
        # Motor's to_list(N) returns up to N docs.
        return self.items[: int(n)] if n is not None else self.items

    def __aiter__(self):
        async def gen():
            for d in self.items:
                yield d
        return gen()


def _matches(row, query):
    """Return True if `row` satisfies the Mongo-style filter `query`.

    Supports the subset used by the CPM code paths:
        scalar equality
        $ne, $in, $nin, $gte, $lte, $gt, $lt
        array-contains-scalar (`{field: value}` matches docs where
        `field` is a list containing value)
        nested AND via top-level keys
    """
    if not query:
        return True
    for k, v in query.items():
        if k == "_id":
            continue
        rv = row.get(k)
        if isinstance(v, dict):
            # Operator subset.
            if "$ne" in v and rv == v["$ne"]:
                return False
            if "$in" in v and rv not in v["$in"]:
                return False
            if "$nin" in v and rv in v["$nin"]:
                return False
            if "$gte" in v and (rv is None or rv < v["$gte"]):
                return False
            if "$lte" in v and (rv is None or rv > v["$lte"]):
                return False
            if "$gt" in v and (rv is None or rv <= v["$gt"]):
                return False
            if "$lt" in v and (rv is None or rv >= v["$lt"]):
                return False
        else:
            # Array-contains-scalar: stored list, scalar query.
            if isinstance(rv, list):
                if v not in rv:
                    return False
            elif rv != v:
                return False
    return True


class _StubCol:
    """A single collection. Rows are dicts."""

    def __init__(self):
        self.rows: list[dict] = []

    def find(self, query=None, projection=None):
        return _FindCursor([dict(r) for r in self.rows if _matches(r, query or {})])

    async def find_one(self, query=None, projection=None):
        for r in self.rows:
            if _matches(r, query or {}):
                return dict(r)
        return None

    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        return types.SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, query, ops):
        for r in self.rows:
            if _matches(r, query or {}):
                if "$set" in ops:
                    r.update(ops["$set"])
                if "$unset" in ops:
                    for k in ops["$unset"]:
                        r.pop(k, None)
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        return types.SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, ops):
        n = 0
        for r in self.rows:
            if _matches(r, query or {}):
                if "$set" in ops:
                    r.update(ops["$set"])
                n += 1
        return types.SimpleNamespace(matched_count=n, modified_count=n)

    async def delete_one(self, query):
        for i, r in enumerate(self.rows):
            if _matches(r, query or {}):
                del self.rows[i]
                return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)

    async def count_documents(self, query):
        return sum(1 for r in self.rows if _matches(r, query or {}))


class _StubDB:
    """Database with auto-instantiated collections. Any attribute access
    that doesn't already exist creates an empty _StubCol -- so code that
    queries db.work_items / db.work_item_links / etc. doesn't crash even
    when the test didn't seed those collections."""

    def __init__(self):
        self.tasks = _StubCol()
        self.delays = _StubCol()
        self.qc_issues = _StubCol()
        self.rfis = _StubCol()
        self.submittals = _StubCol()
        self.safety_events = _StubCol()
        self.work_items = _StubCol()
        self.work_item_links = _StubCol()
        self.projects = _StubCol()
        self.mitigations = _StubCol()
        self.audit_log = _StubCol()

    def __getattr__(self, name):
        # Auto-create unknown collections so tests don't crash on
        # unseeded surfaces (e.g., db.daily_reports inside a helper that
        # the test doesn't care about).
        if name.startswith("_"):
            raise AttributeError(name)
        col = _StubCol()
        object.__setattr__(self, name, col)
        return col

    def __getitem__(self, key):
        return getattr(self, key)

    def reset(self):
        for attr in list(self.__dict__):
            v = self.__dict__[attr]
            if isinstance(v, _StubCol):
                v.rows.clear()


_STUB = _StubDB()


# =============================================================================
# Stub modules BEFORE importing the real services.
# =============================================================================
_db_mod = types.ModuleType("database")
_db_mod.get_db = lambda: _STUB
_db_mod.tenant_filter = lambda tenant_id, q=None: {**(q or {}), "tenant_id": tenant_id}
sys.modules["database"] = _db_mod

_audit_mod = types.ModuleType("audit")


async def _noop_write_audit(**_kw):
    return None


_audit_mod.write_audit = _noop_write_audit
sys.modules["audit"] = _audit_mod

# auth: only needed because routers/field_events_router.py imports it.
_auth_mod = types.ModuleType("auth")
_auth_mod.require_tenant = lambda: {}
sys.modules["auth"] = _auth_mod

# Now import the real services. This must come AFTER the stubs above.
from services.cpm import (  # noqa: E402
    compute_cpm,
    auto_recompute_after_event,
)
from services.task_status import (  # noqa: E402
    compute_projected_finish_dates,
    compute_and_persist_status,
)
from services.qc_rework_cascade import (  # noqa: E402
    create_synthetic_delay_for_qc,
)
from routers.field_events_router import (  # noqa: E402
    _resolve_task_impacts,
)


# =============================================================================
# Test helpers
# =============================================================================
TENANT = "t-test"
PROJECT = "p-msu"


def D(m: int, d: int) -> date:
    """Date in May/June 2026 anchored at the Monday 2026-05-04."""
    if m == 5 and d > 31:
        return date(2026, 6, d - 31)
    if m == 6 and d > 30:
        return date(2026, 7, d - 30)
    return date(2026, m, d)


def _reset():
    _STUB.reset()
    _STUB.projects.rows.append({
        "id": PROJECT, "tenant_id": TENANT, "name": "MSU Renovation",
    })


def _add_task(
    code: str,
    duration: int,
    planned_start: date | None = None,
    planned_finish: date | None = None,
    predecessors: list | None = None,
    **extras,
) -> dict:
    """Insert a task row into the stub. Returns the dict."""
    if planned_finish is None and planned_start is not None and duration > 0:
        planned_finish = planned_start + timedelta(days=duration - 1)
    pf_iso = planned_finish.isoformat() if planned_finish else None
    ps_iso = planned_start.isoformat() if planned_start else None
    doc = {
        "id": f"task-{code}",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "activity_code": code,
        "description": f"Task {code}",
        "duration_days": duration,
        "planned_start": ps_iso,
        "planned_finish": pf_iso,
        "predecessors": predecessors or [],
        "status": extras.pop("status", "not_started"),
        "is_critical_path": extras.pop("is_critical_path", False),
        "total_float_days": extras.pop("total_float_days", None),
        "projected_finish_date": extras.pop("projected_finish_date", None),
        "projected_start_date": extras.pop("projected_start_date", None),
        "pct_complete": extras.pop("pct_complete", 0),
        "computed_status": extras.pop("computed_status", "on_track"),
        "has_open_blocker": extras.pop("has_open_blocker", False),
        "open_blocker_count": extras.pop("open_blocker_count", 0),
        "open_blocker_summary": extras.pop("open_blocker_summary", []),
    }
    doc.update(extras)
    _STUB.tasks.rows.append(doc)
    return doc


def _pred(code: str, rel_type: str = "FS", lag_days: int = 0) -> dict:
    return {"code": code, "type": rel_type, "lag_days": lag_days}


def _add_open_delay(
    task_id: str, impact_days: float, logged_date: date | None = None,
    severity: str = "moderate",
) -> dict:
    doc = {
        "id": f"delay-{uuid.uuid4()}",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "linked_task_ids": [task_id],
        "status": "open",
        "severity": severity,
        "description": "test delay",
        "logged_date": (logged_date or D(5, 14)).isoformat(),
        "date": (logged_date or D(5, 14)).isoformat(),
        "delay_days": impact_days,
        "per_task_impacts": [{
            "task_id": task_id,
            "projected_impact_days": float(impact_days),
        }],
    }
    _STUB.delays.rows.append(doc)
    return doc


async def _simulate_create_delay(
    task_id: str, impact_days: float,
    logged_date: date | None = None,
) -> dict:
    """Replays the field_events_router.create_delay flow at the data-
    layer level, in the SAME order the production endpoint enforces:

       1. _resolve_task_impacts -- build initial per_task_impacts
          (this is the snapshot that PRE-Part-E used and would have
          captured stale float)
       2. db.delays.insert_one
       3. auto_recompute_after_event -- compute_cpm + compute_projected_finish_dates
       4. _resolve_task_impacts -- RE-RESOLVE with fresh state
       5. db.delays.update_one to overwrite per_task_impacts

    The audit op in the real endpoint fires AFTER step 5 (so it captures
    fresh impacts). We skip it here because audit is stubbed.
    """
    payload = {
        "tenant_id": TENANT, "project_id": PROJECT,
        "linked_task_ids": [task_id],
        "per_task_impacts": [
            {"task_id": task_id, "projected_impact_days": float(impact_days)},
        ],
        "status": "open",
        "severity": "moderate",
        "description": "test delay",
        "logged_date": (logged_date or D(5, 14)).isoformat(),
        "date": (logged_date or D(5, 14)).isoformat(),
        "delay_days": float(impact_days),
    }
    # Step 1: pre-insert resolve (stale).
    linked_ids, impacts = await _resolve_task_impacts(_STUB, TENANT, payload)
    doc = {
        **payload,
        "id": f"delay-{uuid.uuid4()}",
        "linked_task_ids": linked_ids,
        "per_task_impacts": impacts,
    }
    # Step 2: insert.
    await _STUB.delays.insert_one(dict(doc))
    # Step 3: auto-recompute.
    await auto_recompute_after_event(
        TENANT, PROJECT, actor_id="test", request_id="req-test",
    )
    # Step 4: re-resolve with fresh state.
    _, fresh = await _resolve_task_impacts(_STUB, TENANT, doc)
    # Step 5: update if changed (idempotent).
    if fresh != doc["per_task_impacts"]:
        doc["per_task_impacts"] = fresh
        await _STUB.delays.update_one(
            {"id": doc["id"], "tenant_id": TENANT},
            {"$set": {"per_task_impacts": fresh}},
        )
    return doc


async def _simulate_create_multi_task_delay(
    impacts: list[tuple[str, float]],
    logged_date: date | None = None,
) -> dict:
    """CPM Part I (2026-05-15) -- multi-task variant of
    _simulate_create_delay for tests that exercise the
    `per_task_impacts contains multiple tasks` edge case.

    Each entry in `impacts` is (task_id, projected_impact_days).
    Replays the SAME 5-step create_delay flow but with a multi-task
    linked_task_ids + per_task_impacts payload.
    """
    task_ids = [tid for tid, _ in impacts]
    payload = {
        "tenant_id": TENANT, "project_id": PROJECT,
        "linked_task_ids": task_ids,
        "per_task_impacts": [
            {"task_id": tid, "projected_impact_days": float(imp)}
            for tid, imp in impacts
        ],
        "status": "open",
        "severity": "moderate",
        "description": "test multi-task delay",
        "logged_date": (logged_date or D(5, 14)).isoformat(),
        "date": (logged_date or D(5, 14)).isoformat(),
        "delay_days": float(max(imp for _, imp in impacts)),
    }
    linked_ids, impacts_out = await _resolve_task_impacts(
        _STUB, TENANT, payload,
    )
    doc = {
        **payload,
        "id": f"delay-{uuid.uuid4()}",
        "linked_task_ids": linked_ids,
        "per_task_impacts": impacts_out,
    }
    await _STUB.delays.insert_one(dict(doc))
    await auto_recompute_after_event(
        TENANT, PROJECT, actor_id="test", request_id="req-test",
    )
    _, fresh = await _resolve_task_impacts(_STUB, TENANT, doc)
    if fresh != doc["per_task_impacts"]:
        doc["per_task_impacts"] = fresh
        await _STUB.delays.update_one(
            {"id": doc["id"], "tenant_id": TENANT},
            {"$set": {"per_task_impacts": fresh}},
        )
    return doc


async def _get_task(task_id: str) -> dict:
    t = await _STUB.tasks.find_one({"id": task_id, "tenant_id": TENANT})
    assert t is not None, f"task {task_id} not in stub"
    return t


async def _get_delay(delay_id: str) -> dict:
    d = await _STUB.delays.find_one({"id": delay_id, "tenant_id": TENANT})
    assert d is not None, f"delay {delay_id} not in stub"
    return d


# =============================================================================
# THE 11 TESTS
# =============================================================================

# -----------------------------------------------------------------------------
# 1. test_parallel_tasks_isolation
# -----------------------------------------------------------------------------
async def _test_parallel_tasks_isolation():
    """Two unconnected tasks. A delay on one does NOT shift the other.

    Setup:
        T_A   root, planned 5/4..5/6 (3d)
        T_B   root, planned 5/4..5/8 (5d)
                -- parallel, no edge between A and B

    Action:
        Log a 5-day delay on T_A; run auto-recompute.

    Expect:
        T_A.projected_finish_date == 5/11 (planned 5/6 + 5d)
        T_B.projected_finish_date is None (no delay touching it)
        T_A._slip == 5; T_B._slip == 0
    """
    _reset()
    _add_task("A", duration=3, planned_start=D(5, 4), planned_finish=D(5, 6))
    _add_task("B", duration=5, planned_start=D(5, 4), planned_finish=D(5, 8))
    _add_open_delay("task-A", impact_days=5)

    await auto_recompute_after_event(TENANT, PROJECT)

    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")

    assert t_a["projected_finish_date"] == "2026-05-11", (
        f"T_A.projected_finish_date expected 2026-05-11, got "
        f"{t_a['projected_finish_date']!r}"
    )
    assert t_b["projected_finish_date"] is None, (
        f"T_B.projected_finish_date expected None (isolated), got "
        f"{t_b['projected_finish_date']!r}"
    )
    # T_A picks up a 5-day shift; T_B's planned_finish stays untouched.
    assert t_a["planned_finish"] == "2026-05-06"
    assert t_b["planned_finish"] == "2026-05-08"


# -----------------------------------------------------------------------------
# 2. test_max_at_convergence
# -----------------------------------------------------------------------------
async def _test_max_at_convergence():
    """Two predecessors converge at one successor; MAX wins.

    Setup:
        T_A  root, planned 5/4..5/6 (3d)      -- shorter path
        T_B  root, planned 5/4..5/8 (5d)      -- longer path
        T_C  preds [A FS+0, B FS+0], dur 4

    Forward pass:
        A.EF = 5/6 -> C.ES candidate = 5/7
        B.EF = 5/8 -> C.ES candidate = 5/9
        MAX(5/7, 5/9) = 5/9, C.EF = 5/12

    Backward pass:
        C.LF = 5/12 (tail), C.LS = 5/9
        From C: A.LF = 5/8, B.LF = 5/8
        A.LS = 5/6 (LF - dur+1), Float A = LS - ES = 5/6 - 5/4 = 2d
        B.LS = 5/4, Float B = 0

    Expect:
        project_finish == 5/12
        T_B + T_C critical (float 0)
        T_A NOT critical, float = 2
    """
    _reset()
    _add_task("A", duration=3, planned_start=D(5, 4))
    _add_task("B", duration=5, planned_start=D(5, 4))
    _add_task("C", duration=4, planned_start=D(5, 4),
              predecessors=[_pred("A"), _pred("B")])

    result = await compute_cpm(TENANT, PROJECT)

    assert result.get("project_finish") == "2026-05-12", (
        f"project_finish expected 2026-05-12, got {result.get('project_finish')!r}"
    )
    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    t_c = await _get_task("task-C")
    assert t_a["total_float_days"] == 2, (
        f"T_A float expected 2 (shorter-path slack), got "
        f"{t_a['total_float_days']!r}"
    )
    assert t_a["is_critical_path"] is False
    assert t_b["total_float_days"] == 0
    assert t_b["is_critical_path"] is True
    assert t_c["total_float_days"] == 0
    assert t_c["is_critical_path"] is True


# -----------------------------------------------------------------------------
# 3. test_fs_lag_concrete_cure
# -----------------------------------------------------------------------------
async def _test_fs_lag_concrete_cure():
    """FS+7d ("concrete cure") -- successor starts pred.EF + 1 + 7 days.

    Setup:
        T_A "Pour slab"  root, 5/4..5/8 (5d)
        T_B "Cure 7d"    FS+7 from A, dur 3

    Forward:
        A.EF = 5/8 -> B.ES = 5/8 + 1 + 7 = 5/16, B.EF = 5/18

    Expect: project_finish == 5/18; both critical (single path).
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))
    _add_task("B", duration=3, planned_start=D(5, 16),
              predecessors=[_pred("A", "FS", 7)])

    result = await compute_cpm(TENANT, PROJECT)

    assert result.get("project_finish") == "2026-05-18", (
        f"FS+7 cure: project_finish expected 2026-05-18, got "
        f"{result.get('project_finish')!r}"
    )
    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    assert t_a["total_float_days"] == 0
    assert t_a["is_critical_path"] is True
    assert t_b["total_float_days"] == 0
    assert t_b["is_critical_path"] is True


# -----------------------------------------------------------------------------
# 4. test_ss_relationship
# -----------------------------------------------------------------------------
async def _test_ss_relationship():
    """SS+2d -- successor starts 2 days after pred's START (NO +1 offset).

    Setup:
        T_A  root, 5/4..5/8 (5d)
        T_B  SS+2 from A, dur 4

    Forward:
        A.ES = 5/4 -> B.ES = 5/4 + 2 = 5/6, B.EF = 5/9

    project_finish = MAX(5/8, 5/9) = 5/9 (B is tail by EF).
    Backward: B (tail) LF=5/9, LS=5/6. A via SS+2: A.LS = 5/4. A.LF = 5/8.
    Both critical (float 0).
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))
    _add_task("B", duration=4, planned_start=D(5, 6),
              predecessors=[_pred("A", "SS", 2)])

    result = await compute_cpm(TENANT, PROJECT)

    assert result.get("project_finish") == "2026-05-09", (
        f"SS+2: project_finish expected 2026-05-09, got "
        f"{result.get('project_finish')!r}"
    )
    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    assert t_a["total_float_days"] == 0
    assert t_a["is_critical_path"] is True
    assert t_b["total_float_days"] == 0
    assert t_b["is_critical_path"] is True


# -----------------------------------------------------------------------------
# 5. test_ff_relationship
# -----------------------------------------------------------------------------
async def _test_ff_relationship():
    """FF+0 -- both tasks finish the same day. NO +1 offset.

    Setup:
        T_A  root, 5/4..5/8 (5d)
        T_B  FF+0 from A, dur 3

    Forward:
        A.EF = 5/8 -> B.EF = 5/8, B.ES = 5/8 - 2 = 5/6
    project_finish = 5/8 (A and B both end here).
    Backward: B (tail) LF=5/8, LS=5/6. A via FF+0: A.LF = 5/8. A.LS = 5/4.
    Both critical.
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))
    _add_task("B", duration=3, planned_start=D(5, 6),
              predecessors=[_pred("A", "FF", 0)])

    result = await compute_cpm(TENANT, PROJECT)

    assert result.get("project_finish") == "2026-05-08", (
        f"FF+0: project_finish expected 2026-05-08, got "
        f"{result.get('project_finish')!r}"
    )
    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    assert t_a["total_float_days"] == 0
    assert t_b["total_float_days"] == 0


# -----------------------------------------------------------------------------
# 6. test_sf_relationship
# -----------------------------------------------------------------------------
async def _test_sf_relationship():
    """SF+0 (rare) -- successor.finish = predecessor.start; NO +1 offset.

    Setup:
        T_A  root, planned 5/8..5/12 (5d)
        T_B  SF+0 from A, dur 3

    Forward:
        A.ES = 5/8 -> B.EF = 5/8, B.ES = 5/8 - 2 = 5/6
    project_finish = MAX(5/12, 5/8) = 5/12 (A's planned finish).

    Backward edge case: SF decouples A's finish from B's timeline, so
    A picks up positive float. B's LF = project_finish (no successors).
    A.LS via SF = B.LF = 5/12. A.LF = 5/12 + 4 = 5/16. Float A = 4.
    B.LF = 5/12, B.LS = 5/10. Float B = 5/10 - 5/6 = 4.
    Neither critical -- documented Part D edge case.
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 8))
    _add_task("B", duration=3, planned_start=D(5, 6),
              predecessors=[_pred("A", "SF", 0)])

    result = await compute_cpm(TENANT, PROJECT)

    assert result.get("project_finish") == "2026-05-12", (
        f"SF+0: project_finish expected 2026-05-12, got "
        f"{result.get('project_finish')!r}"
    )
    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    assert t_a["total_float_days"] == 4, (
        f"SF+0 A float expected 4 (decoupled finish), got "
        f"{t_a['total_float_days']!r}"
    )
    assert t_a["is_critical_path"] is False
    assert t_b["total_float_days"] == 4
    assert t_b["is_critical_path"] is False


# -----------------------------------------------------------------------------
# 7. test_lead_overlap
# -----------------------------------------------------------------------------
async def _test_lead_overlap():
    """FS-3d (lead/overlap) -- successor starts 3 days BEFORE pred finishes.

    Setup:
        T_A  root, 5/4..5/8 (5d)
        T_B  FS-3 from A, dur 3

    Forward:
        A.EF = 5/8 -> B.ES = 5/8 + 1 - 3 = 5/6, B.EF = 5/8
    project_finish = MAX(5/8, 5/8) = 5/8.
    Backward: B (tail) LF=5/8, LS=5/6. A via FS-3: A.LF = 5/6 - 1 + 3 = 5/8.
    Both critical.
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))
    _add_task("B", duration=3, planned_start=D(5, 6),
              predecessors=[_pred("A", "FS", -3)])

    result = await compute_cpm(TENANT, PROJECT)

    assert result.get("project_finish") == "2026-05-08", (
        f"FS-3 lead: project_finish expected 2026-05-08, got "
        f"{result.get('project_finish')!r}"
    )
    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    assert t_a["total_float_days"] == 0
    assert t_a["is_critical_path"] is True
    assert t_b["total_float_days"] == 0
    assert t_b["is_critical_path"] is True


# -----------------------------------------------------------------------------
# 8. test_auto_recompute_on_delay_create
# -----------------------------------------------------------------------------
async def _test_auto_recompute_on_delay_create():
    """Part E hook -- the create-delay flow auto-fires compute_cpm +
    compute_projected_finish_dates BEFORE the audit. The post-create
    state of the affected task should reflect BOTH:
       a) total_float_days populated by compute_cpm (was None before)
       b) projected_finish_date populated by
          compute_projected_finish_dates (delay impact propagated)

    Setup:
        T_A  root, 5/4..5/8 (5d)
        T_B  FS+0 from A, dur 3   (5/9..5/11) -- tail; critical path

    Action:
        Run _simulate_create_delay(task-B, impact=5).

    Expect:
        task-B.total_float_days == 0          (auto-recompute populated it)
        task-B.is_critical_path == True
        task-B.projected_finish_date == 2026-05-16   (5/11 + 5 days)
        task-A.projected_finish_date is None         (no own impact)
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))
    _add_task("B", duration=3, planned_start=D(5, 9),
              predecessors=[_pred("A")])

    # Before: floats are at the default None.
    t_b_pre = await _get_task("task-B")
    assert t_b_pre["total_float_days"] is None
    assert t_b_pre["projected_finish_date"] is None

    await _simulate_create_delay("task-B", impact_days=5)

    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    assert t_b["total_float_days"] == 0, (
        f"auto-recompute should have populated task-B float, got "
        f"{t_b['total_float_days']!r}"
    )
    assert t_b["is_critical_path"] is True
    assert t_b["projected_finish_date"] == "2026-05-16", (
        f"projected_finish_date expected 5/16 (planned 5/11 + 5d delay), got "
        f"{t_b['projected_finish_date']!r}"
    )
    # Upstream A has no own delay impact -> projection stays None.
    assert t_a["projected_finish_date"] is None


# -----------------------------------------------------------------------------
# 9. test_stale_float_fixed     <-- THE CRITICAL ONE
# -----------------------------------------------------------------------------
async def _test_stale_float_fixed():
    """Pre-Part-E, _resolve_task_impacts on a delay read STALE float
    (the value from before compute_cpm ran). Post-Part-E,
    auto_recompute_after_event refreshes float BEFORE the re-resolve,
    so the persisted per_task_impacts snapshot has the CORRECT verdict.

    Demonstrates the bug AND the fix in the same test:

        STALE path (pre-Part-E simulated by calling resolve directly):
            task-B.total_float_days is None at DB level -> snapshot
            float_at_logging_days = None -> consumes_float_only = False
            (None != True), pushes_critical_path = False (is_critical_path
            is False in the default state).

        FRESH path (post-Part-E via _simulate_create_delay):
            auto_recompute_after_event ran -> task-B.total_float_days = 0,
            is_critical_path = True. Snapshot reads fresh:
            float_at_logging_days = 0, consumes_float_only = False
            (3 > 0), pushes_critical_path = True.

    The pre/post divergence is the proof that the fix takes effect AND
    that the bug was real. If the two snapshots were identical, the fix
    would be a no-op.
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))
    _add_task("B", duration=3, planned_start=D(5, 9),
              predecessors=[_pred("A")])

    # ---- STALE path (mimic pre-Part-E ordering: resolve BEFORE recompute).
    stale_payload = {
        "tenant_id": TENANT, "project_id": PROJECT,
        "linked_task_ids": ["task-B"],
        "per_task_impacts": [{
            "task_id": "task-B", "projected_impact_days": 3.0,
        }],
    }
    _, stale_impacts = await _resolve_task_impacts(_STUB, TENANT, stale_payload)
    stale = stale_impacts[0]

    # Pre-Part-E reads None for float -- bug surface.
    assert stale["float_at_logging_days"] is None, (
        f"STALE snapshot expected None float, got {stale['float_at_logging_days']!r}"
    )
    assert stale["consumes_float_only"] is False
    assert stale["pushes_critical_path"] is False

    # ---- FRESH path (Part E ordering -- via the create-delay simulator).
    delay = await _simulate_create_delay("task-B", impact_days=3.0)
    # The persisted delay in DB has the FRESH snapshot.
    persisted = await _get_delay(delay["id"])
    fresh = persisted["per_task_impacts"][0]

    assert fresh["float_at_logging_days"] == 0, (
        f"FRESH snapshot expected float 0, got {fresh['float_at_logging_days']!r}"
    )
    assert fresh["consumes_float_only"] is False
    assert fresh["pushes_critical_path"] is True, (
        "FRESH snapshot should flag pushes_critical_path -- T_B has 0 float "
        "AND is on the CP, so any impact > 0 pushes it"
    )

    # The bug existed AND the fix takes effect.
    assert stale != fresh, "stale and fresh snapshots should differ"


# -----------------------------------------------------------------------------
# 10. test_qc_link_only_no_cascade
# -----------------------------------------------------------------------------
async def _test_qc_link_only_no_cascade():
    """Bug #53 verification -- QC issue (non-rework) linked to a task
    does NOT trigger CPM cascade. compute_projected_finish_dates only
    sums delays in open-delay impact, not QC issues.

    Setup:
        T_A  root, 5/4..5/8 (5d)
        Insert a QC issue with severity='major' (NOT rework_required)
        with linked_task_ids = [task-A].

    Action:
        Run compute_projected_finish_dates.

    Expect:
        task-A.projected_finish_date is None
        task-A.projected_start_date is None
        (no QC -> no shift)
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))

    # Insert a QC issue WITHOUT triggering the rework cascade.
    _STUB.qc_issues.rows.append({
        "id": "qc-major-1",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "linked_task_ids": ["task-A"],
        "severity": "major",     # NOT "rework_required"
        "status": "open",
        "description": "Wall has cracks but cosmetic only",
    })

    await compute_projected_finish_dates(_STUB, TENANT, PROJECT)

    t_a = await _get_task("task-A")
    assert t_a["projected_finish_date"] is None, (
        f"Bug #53: QC linkage should NOT propagate through CPM; got "
        f"projected_finish_date={t_a['projected_finish_date']!r}"
    )
    assert t_a["projected_start_date"] is None
    # QC linkage stays on the QC row (gallery filter relies on it).
    qc = await _STUB.qc_issues.find_one({"id": "qc-major-1"})
    assert qc["linked_task_ids"] == ["task-A"]


# -----------------------------------------------------------------------------
# 11. test_qc_rework_routes_through_synthetic_delay
# -----------------------------------------------------------------------------
async def _test_qc_rework_routes_through_synthetic_delay():
    """A QC issue with severity=rework_required spawns a synthetic delay
    via qc_rework_cascade.create_synthetic_delay_for_qc. The synthetic
    delay's create flow runs auto_recompute_after_event (the same hook
    used by the user-facing /delays POST).

    Setup:
        T_A  root, 5/4..5/8 (5d)
        QC issue with severity=rework_required, rework_duration_hours=16
        (= 2 calendar days), linked_task_ids = [task-A].

    Action:
        Call create_synthetic_delay_for_qc directly.

    Expect:
        A synthetic delay row inserted in db.delays
        qc_issue.linked_synthetic_delay_id set to the synthetic delay's id
        Synthetic delay.cause_class == 'quality_rework'
        Synthetic delay.delay_days == 2.0   (16h / 8h/d = 2d)
        Synthetic delay.linked_task_ids == ['task-A']
        Synthetic delay's auto-recompute fires:
            task-A.total_float_days set (was None before; compute_cpm ran)
            task-A.projected_finish_date == 5/10 (planned 5/8 + 2d)
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4))

    qc = {
        "id": "qc-rework-1",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "linked_task_ids": ["task-A"],
        "severity": "rework_required",
        "rework_duration_hours": 16.0,    # 2d at 8h/day
        "rework_duration_basis": "field super estimate",
        "description": "Slab cracked; cut + repour",
        "status": "open",
        "date": D(5, 14).isoformat(),
    }
    _STUB.qc_issues.rows.append(qc)

    synthetic = await create_synthetic_delay_for_qc(
        tenant_id=TENANT, qc_issue=qc,
        actor_id="test-user", request_id="req-qc-rework",
    )
    assert synthetic is not None, "synthetic delay should have been created"
    assert synthetic["cause_class"] == "quality_rework"
    assert synthetic["delay_days"] == 2.0, (
        f"16h / 8h-per-day = 2d; got {synthetic['delay_days']!r}"
    )
    assert synthetic["linked_task_ids"] == ["task-A"]
    assert synthetic["status"] == "open"

    # The QC row was reverse-linked.
    qc_updated = await _STUB.qc_issues.find_one({"id": "qc-rework-1"})
    assert qc_updated["linked_synthetic_delay_id"] == synthetic["id"]

    # auto_recompute_after_event ran INSIDE create_synthetic_delay_for_qc --
    # compute_cpm populated task-A float; compute_projected_finish_dates
    # walked the 2-day shift forward.
    t_a = await _get_task("task-A")
    assert t_a["total_float_days"] is not None, (
        "auto-recompute should have populated task-A float"
    )
    assert t_a["total_float_days"] == 0   # single-task project, A is the tail
    assert t_a["is_critical_path"] is True
    assert t_a["projected_finish_date"] == "2026-05-10", (
        f"Synthetic delay should push task-A finish by 2 days: planned 5/8 "
        f"-> projected 5/10. Got {t_a['projected_finish_date']!r}"
    )


# =============================================================================
# CPM PART I (2026-05-15) -- CUMULATIVE REMAINING-FLOAT VERDICT TESTS
# =============================================================================
# The 5 new tests cover the gap the audit identified: per-delay verdicts
# now reflect CUMULATIVE remaining float (structural float - sum of prior
# OPEN delay impacts on the same task), not structural float in isolation.
# Test graph: 2 parallel tasks (LONG / SHORT) with no edge between them.
# LONG drives project_finish; SHORT carries positive structural float.

# -----------------------------------------------------------------------------
# I-1. test_remaining_float_two_delays_same_task
# -----------------------------------------------------------------------------
async def _test_remaining_float_two_delays_same_task():
    """Two 3-day delays on a task with structural float = 5.
    Delay 1: prior=0, remaining=5, 3 <= 5 -> within remaining float.
    Delay 2: prior=3 (delay 1 still open), remaining=2, 3 > 2 -> pushes CP.
    Delay 1's snapshot stays immutable (history is preserved)."""
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))   # 5/4..5/13, float 0
    _add_task("SHORT", duration=5, planned_start=D(5, 4))   # 5/4..5/8,  float 5
    await compute_cpm(TENANT, PROJECT)
    t_short = await _get_task("task-SHORT")
    assert t_short["total_float_days"] == 5, (
        f"setup: SHORT structural float expected 5 (project_finish 5/13 "
        f"- SHORT planned_finish 5/8 = 5d), got "
        f"{t_short['total_float_days']!r}"
    )

    delay_1 = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap_1 = (await _get_delay(delay_1["id"]))["per_task_impacts"][0]
    assert snap_1["float_at_logging_days"] == 5
    assert snap_1["prior_open_impacts_at_logging_days"] == 0
    assert snap_1["remaining_float_at_logging_days"] == 5
    assert snap_1["consumes_float_only"] is True, (
        f"delay 1 (3d on 5d remaining) should be within float; "
        f"got snap={snap_1!r}"
    )
    assert snap_1["pushes_critical_path"] is False

    delay_2 = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap_2 = (await _get_delay(delay_2["id"]))["per_task_impacts"][0]
    assert snap_2["float_at_logging_days"] == 5, (
        "structural float unchanged between events"
    )
    assert snap_2["prior_open_impacts_at_logging_days"] == 3, (
        f"delay 2 should see delay 1's 3d as prior open impact; got "
        f"prior={snap_2['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap_2["remaining_float_at_logging_days"] == 2  # 5 - 3
    assert snap_2["consumes_float_only"] is False, (
        f"delay 2 (3d) > remaining (2d): NOT within float; got snap={snap_2!r}"
    )
    assert snap_2["pushes_critical_path"] is True

    # History preservation: delay 1's snapshot must not have been rewritten
    # by delay 2's recompute.
    persisted_1_again = await _get_delay(delay_1["id"])
    snap_1_after = persisted_1_again["per_task_impacts"][0]
    assert snap_1_after["remaining_float_at_logging_days"] == 5, (
        "delay 1 snapshot must remain immutable after delay 2 is logged"
    )


# -----------------------------------------------------------------------------
# I-2. test_remaining_float_resolved_delay_excluded
# -----------------------------------------------------------------------------
async def _test_remaining_float_resolved_delay_excluded():
    """A RESOLVED prior delay does NOT count toward prior_open_impacts.
    Setup: SHORT.structural = 5. Log delay 1 (3d), resolve it, log
    delay 2 (3d). Delay 2 sees prior=0 because delay 1 is closed."""
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    delay_1 = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    # Mark delay 1 resolved (mirrors the user clicking "resolve" in UI).
    # The production update_delay flow also fires auto_recompute_after_event;
    # we replicate that here so the next delay's create sees fresh state.
    await _STUB.delays.update_one(
        {"id": delay_1["id"], "tenant_id": TENANT},
        {"$set": {"status": "resolved",
                  "resolved_date": D(5, 15).isoformat()}},
    )
    await auto_recompute_after_event(TENANT, PROJECT)

    delay_2 = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap_2 = (await _get_delay(delay_2["id"]))["per_task_impacts"][0]
    assert snap_2["prior_open_impacts_at_logging_days"] == 0, (
        f"resolved delay 1 should be EXCLUDED from prior_open; got "
        f"prior={snap_2['prior_open_impacts_at_logging_days']!r}. "
        f"This means the status:open filter in "
        f"_sum_prior_open_delay_impacts_on_task is not respecting "
        f"resolved status."
    )
    assert snap_2["remaining_float_at_logging_days"] == 5
    assert snap_2["consumes_float_only"] is True
    assert snap_2["pushes_critical_path"] is False


# -----------------------------------------------------------------------------
# I-3. test_remaining_float_edited_delay_self_excluded
# -----------------------------------------------------------------------------
async def _test_remaining_float_edited_delay_self_excluded():
    """Editing a delay's impact must self-exclude in the prior-sum
    so the delay doesn't double-count its own pre-edit value when
    re-computing the snapshot. Setup: SHORT.structural = 5. Log
    delay 1 (3d), then EDIT delay 1 to 4d. The edit's re-resolve
    must read prior=0 and remaining=5."""
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    delay_1 = await _simulate_create_delay("task-SHORT", impact_days=3.0)

    # Simulate the update_delay flow's _resolve_task_impacts call:
    # the merged dict carries the EDITED impact (4d), and we pass
    # exclude_delay_id=delay_1.id so the prior-sum doesn't count
    # the in-DB version of delay 1 as "prior".
    edit_merged = {
        "linked_task_ids": ["task-SHORT"],
        "per_task_impacts": [
            {"task_id": "task-SHORT", "projected_impact_days": 4.0},
        ],
    }
    _, edited_impacts = await _resolve_task_impacts(
        _STUB, TENANT, edit_merged,
        project_id=PROJECT,
        exclude_delay_id=delay_1["id"],
    )
    snap = edited_impacts[0]
    assert snap["projected_impact_days"] == 4.0
    assert snap["prior_open_impacts_at_logging_days"] == 0, (
        f"self-exclude failed: editing delay 1 from 3d -> 4d should "
        f"NOT see its own 3d as prior. Got "
        f"prior={snap['prior_open_impacts_at_logging_days']!r} (expected 0)."
    )
    assert snap["remaining_float_at_logging_days"] == 5  # 5 - 0
    assert snap["consumes_float_only"] is True, (
        f"4d <= 5d remaining -> within float; got snap={snap!r}"
    )
    assert snap["pushes_critical_path"] is False


# -----------------------------------------------------------------------------
# I-4. test_remaining_float_clamps_at_zero
# -----------------------------------------------------------------------------
async def _test_remaining_float_clamps_at_zero():
    """remaining_float = max(0, structural - prior). Cannot go negative.
    Setup: SHORT.structural = 2. Log delay 1 (3d, already exceeds),
    then delay 2 (2d). Delay 2 sees remaining = max(0, 2-3) = 0 (NOT -1)."""
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))   # 5/4..5/13, float 0
    _add_task("SHORT", duration=8, planned_start=D(5, 4))   # 5/4..5/11, float 2
    await compute_cpm(TENANT, PROJECT)
    t_short = await _get_task("task-SHORT")
    assert t_short["total_float_days"] == 2, (
        f"setup: SHORT structural expected 2 (5/13 - 5/11), got "
        f"{t_short['total_float_days']!r}"
    )

    delay_1 = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap_1 = (await _get_delay(delay_1["id"]))["per_task_impacts"][0]
    assert snap_1["remaining_float_at_logging_days"] == 2
    assert snap_1["consumes_float_only"] is False    # 3 > 2
    assert snap_1["pushes_critical_path"] is True

    delay_2 = await _simulate_create_delay("task-SHORT", impact_days=2.0)
    snap_2 = (await _get_delay(delay_2["id"]))["per_task_impacts"][0]
    assert snap_2["prior_open_impacts_at_logging_days"] == 3, (
        "delay 1 still open -> 3d prior"
    )
    # max(0, 2 - 3) = 0, NOT -1
    assert snap_2["remaining_float_at_logging_days"] == 0, (
        f"remaining MUST clamp at 0 (no negative-float verdicts); got "
        f"remaining={snap_2['remaining_float_at_logging_days']!r}"
    )
    assert snap_2["consumes_float_only"] is False    # 2 > 0
    assert snap_2["pushes_critical_path"] is True


# -----------------------------------------------------------------------------
# I-5. test_remaining_float_multi_task_delay
# -----------------------------------------------------------------------------
async def _test_remaining_float_multi_task_delay():
    """A multi-task delay contributes its PER-TASK impact to each task's
    prior_open sum, never crosses tasks. Setup: SHORT1 + SHORT2 both
    structural float 5. Log delay 1 linked to BOTH with 3d-each, then
    delay 2 linked only to SHORT1 with 3d. Delay 2's prior_open on
    SHORT1 must equal 3 (only SHORT1's slice of delay 1), NOT 6 (full
    delay 1 total)."""
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))    # float 0
    _add_task("SHORT1", duration=5, planned_start=D(5, 4))   # float 5
    _add_task("SHORT2", duration=5, planned_start=D(5, 4))   # float 5
    await compute_cpm(TENANT, PROJECT)
    t_s1 = await _get_task("task-SHORT1")
    t_s2 = await _get_task("task-SHORT2")
    assert t_s1["total_float_days"] == 5
    assert t_s2["total_float_days"] == 5

    # Delay 1 hits BOTH tasks with 3d-each. Total delay impact across
    # tasks = 6d, but per-task impact = 3d each.
    delay_1 = await _simulate_create_multi_task_delay([
        ("task-SHORT1", 3.0),
        ("task-SHORT2", 3.0),
    ])
    persisted_1 = await _get_delay(delay_1["id"])
    snap_1_s1 = next(
        i for i in persisted_1["per_task_impacts"] if i["task_id"] == "task-SHORT1"
    )
    snap_1_s2 = next(
        i for i in persisted_1["per_task_impacts"] if i["task_id"] == "task-SHORT2"
    )
    assert snap_1_s1["remaining_float_at_logging_days"] == 5
    assert snap_1_s2["remaining_float_at_logging_days"] == 5

    # Delay 2 hits ONLY SHORT1 with 3d.
    delay_2 = await _simulate_create_delay("task-SHORT1", impact_days=3.0)
    snap_2 = (await _get_delay(delay_2["id"]))["per_task_impacts"][0]
    assert snap_2["prior_open_impacts_at_logging_days"] == 3, (
        f"delay 2 prior_open on SHORT1 must be 3 (delay 1's SHORT1 slice "
        f"only), NOT 6 (full delay 1 total). Got "
        f"prior={snap_2['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap_2["remaining_float_at_logging_days"] == 2  # 5 - 3
    assert snap_2["consumes_float_only"] is False  # 3 > 2
    assert snap_2["pushes_critical_path"] is True


# =============================================================================
# Pytest sync wrappers
# =============================================================================
def test_parallel_tasks_isolation():
    asyncio.run(_test_parallel_tasks_isolation())


def test_max_at_convergence():
    asyncio.run(_test_max_at_convergence())


def test_fs_lag_concrete_cure():
    asyncio.run(_test_fs_lag_concrete_cure())


def test_ss_relationship():
    asyncio.run(_test_ss_relationship())


def test_ff_relationship():
    asyncio.run(_test_ff_relationship())


def test_sf_relationship():
    asyncio.run(_test_sf_relationship())


def test_lead_overlap():
    asyncio.run(_test_lead_overlap())


def test_auto_recompute_on_delay_create():
    asyncio.run(_test_auto_recompute_on_delay_create())


def test_stale_float_fixed():
    asyncio.run(_test_stale_float_fixed())


def test_qc_link_only_no_cascade():
    asyncio.run(_test_qc_link_only_no_cascade())


def test_qc_rework_routes_through_synthetic_delay():
    asyncio.run(_test_qc_rework_routes_through_synthetic_delay())


# CPM Part I (2026-05-15) -- cumulative remaining-float verdict.
def test_remaining_float_two_delays_same_task():
    asyncio.run(_test_remaining_float_two_delays_same_task())


def test_remaining_float_resolved_delay_excluded():
    asyncio.run(_test_remaining_float_resolved_delay_excluded())


def test_remaining_float_edited_delay_self_excluded():
    asyncio.run(_test_remaining_float_edited_delay_self_excluded())


def test_remaining_float_clamps_at_zero():
    asyncio.run(_test_remaining_float_clamps_at_zero())


def test_remaining_float_multi_task_delay():
    asyncio.run(_test_remaining_float_multi_task_delay())


# =============================================================================
# Standalone runner. Lets `python backend/tests/test_cpm_correctness.py` work
# without pytest infrastructure -- prints concrete dates per test on success
# and a clear failure message + exit 1 on the first regression.
# =============================================================================
def _run_all():
    tests = [
        ("test_parallel_tasks_isolation", _test_parallel_tasks_isolation),
        ("test_max_at_convergence", _test_max_at_convergence),
        ("test_fs_lag_concrete_cure", _test_fs_lag_concrete_cure),
        ("test_ss_relationship", _test_ss_relationship),
        ("test_ff_relationship", _test_ff_relationship),
        ("test_sf_relationship", _test_sf_relationship),
        ("test_lead_overlap", _test_lead_overlap),
        ("test_auto_recompute_on_delay_create",
         _test_auto_recompute_on_delay_create),
        ("test_stale_float_fixed", _test_stale_float_fixed),
        ("test_qc_link_only_no_cascade", _test_qc_link_only_no_cascade),
        ("test_qc_rework_routes_through_synthetic_delay",
         _test_qc_rework_routes_through_synthetic_delay),
        # CPM Part I (2026-05-15) -- cumulative remaining-float verdict.
        ("test_remaining_float_two_delays_same_task",
         _test_remaining_float_two_delays_same_task),
        ("test_remaining_float_resolved_delay_excluded",
         _test_remaining_float_resolved_delay_excluded),
        ("test_remaining_float_edited_delay_self_excluded",
         _test_remaining_float_edited_delay_self_excluded),
        ("test_remaining_float_clamps_at_zero",
         _test_remaining_float_clamps_at_zero),
        ("test_remaining_float_multi_task_delay",
         _test_remaining_float_multi_task_delay),
    ]
    passes = 0
    fails: list[tuple[str, str]] = []
    print("=" * 78)
    print("CPM CORRECTNESS REGRESSION SUITE -- Parts H+I (11 + 5 tests)")
    print("=" * 78)
    for name, fn in tests:
        try:
            asyncio.run(fn())
            print(f"PASS  {name}")
            passes += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fails.append((name, str(e)))
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            fails.append((name, f"{type(e).__name__}: {e}"))
    print()
    print("=" * 78)
    print(f"OVERALL: {passes} PASS / {len(fails)} FAIL  (of 16 spec tests)")
    print("=" * 78)
    if fails:
        for n, e in fails:
            print(f"  - {n}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
