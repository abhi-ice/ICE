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
        $or (CPM Part J-7): top-level disjunction of subqueries; used
        by services.task_status.count_open_blockers to match RFIs on
        EITHER linked_task_ids OR schedule_activity_id (Bug #50).
        $and (defensive): top-level conjunction; not currently used
        by any helper but matches Mongo semantics.
        nested AND via top-level keys
    """
    if not query:
        return True
    for k, v in query.items():
        if k == "_id":
            continue
        # CPM Part J-7 (2026-05-15) -- handle top-level $or / $and
        # logical operators before falling into per-field matching.
        if k == "$or":
            if not isinstance(v, list) or not v:
                return False
            if not any(_matches(row, sub) for sub in v):
                return False
            continue
        if k == "$and":
            if not isinstance(v, list):
                return False
            if not all(_matches(row, sub) for sub in v):
                return False
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
# CPM Part J-7 (2026-05-15) -- RFI verdict resolver for the J-7 tests.
from routers.docs_router import (  # noqa: E402
    _resolve_rfi_task_impacts,
)
# CPM Part K-7 (2026-05-15) -- Submittal cross-collection helpers.
# The K-2 auto-suggest helper + K-3 verdict resolver + K-3 persist
# wrapper from services.submittal_sla. Tests call these directly to
# verify the K-2/K-3/K-4/K-5 chain end-to-end without needing the
# full SLA date-math from detect_breaches.
from services.submittal_sla import (  # noqa: E402
    auto_suggest_per_task_impacts_for_submittal,
    _resolve_and_persist_submittal_per_task_impacts,
)
# CPM Slip-cascade fix (2026-05-16) -- working-day delta helper used
# by the SC-* cascade tests to assert exact overflow magnitudes.
from services.working_days import working_days_between  # noqa: E402


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


def _add_open_rfi(
    task_id: str | None = None,
    impact_days: int | None = None,
    *,
    rfi_id: str | None = None,
    status: str = "draft",
    linked_task_ids: list[str] | None = None,
    per_task_impacts: list[dict] | None = None,
) -> dict:
    """CPM Part J-7 (2026-05-15) -- insert an RFI row into the stub.

    Two ways to call:
        (a) task_id + impact_days  -- single-task RFI shorthand.
            Builds linked_task_ids=[task_id] and
            per_task_impacts=[{task_id, projected_impact_days}].
        (b) linked_task_ids + per_task_impacts  -- explicit shape
            (used for multi-task RFIs and zero-impact RFIs that
            test Path 1 binary-blocker semantics).

    If `per_task_impacts` is None and `impact_days` is given, the
    one-row array is synthesized. Pass `per_task_impacts=[]`
    explicitly to test the "RFI logged with no impact captured yet"
    case (test 6).

    Status defaults to "draft" so the RFI counts as a blocker in
    count_open_blockers AND contributes to the Path 1 / Path 2
    blocker walks. Pass status="answered" / "closed" for the
    resolved-RFI exclusion tests (test 3).
    """
    if linked_task_ids is None:
        if task_id is None:
            raise ValueError("must pass task_id or linked_task_ids")
        linked_task_ids = [task_id]
    if per_task_impacts is None:
        if impact_days is None or task_id is None:
            per_task_impacts = []
        else:
            per_task_impacts = [{
                "task_id": task_id,
                "projected_impact_days": int(impact_days),
            }]
    doc = {
        "id": rfi_id or f"rfi-{uuid.uuid4()}",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "rfi_number": "RFI-TEST",
        "subject": "test rfi",
        "description": "test rfi description",
        "status": status,
        "priority": "normal",
        "linked_task_ids": list(linked_task_ids),
        "per_task_impacts": list(per_task_impacts),
        "date_initiated": D(5, 14).isoformat(),
    }
    _STUB.rfis.rows.append(doc)
    return doc


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


def _add_open_submittal(
    task_id: str | None = None,
    impact_days: int | None = None,
    *,
    submittal_id: str | None = None,
    status: str = "in_review",
    sla_status: str = "breached",
    source: str = "system_suggested",
    aging_working_days: int | None = None,
    aging_days: int | None = None,
    per_task_impacts: list[dict] | None = None,
    schedule_activity_id: str | None = None,
    original_suggested_days: int | None = None,
    project_id: str | None = None,
) -> dict:
    """CPM Part K-7 (2026-05-15) -- insert a submittal row into the stub.

    Two ways to call:
        (a) task_id + impact_days  -- single-link submittal shorthand.
            Builds schedule_activity_id=task_id and per_task_impacts=
            [{task_id, projected_impact_days, source [, original_
            suggested_days]}]. source defaults to system_suggested;
            override via kwarg.
        (b) per_task_impacts=[]  -- explicitly empty (test 6: super
            cleared the auto-suggestion to claim no impact).

    Status defaults to "in_review" + sla_status="breached" so the row
    matches the K-4 Path 1 + K-5 Path 2 + count_open_blockers filter:
        schedule_activity_id == task_id
        status IN ["submitted", "in_review"]
        sla_status == "breached"

    Set sla_status="n_a" + status="approved" for the approved-exclusion
    case (test 4).
    """
    if schedule_activity_id is None:
        schedule_activity_id = task_id
    if per_task_impacts is None:
        if impact_days is None or task_id is None:
            per_task_impacts = []
        else:
            entry = {
                "task_id": task_id,
                "projected_impact_days": int(impact_days),
                "source": source,
            }
            if original_suggested_days is not None:
                entry["original_suggested_days"] = int(original_suggested_days)
            elif source == "system_suggested":
                # K-6: K-2 stamps original_suggested_days on every
                # system_suggested entry it creates. Default mirror.
                entry["original_suggested_days"] = int(impact_days)
            per_task_impacts = [entry]
    doc = {
        "id": submittal_id or f"sub-{uuid.uuid4()}",
        "tenant_id": TENANT,
        "project_id": project_id or PROJECT,
        "submittal_number": "SUB-TEST",
        "title": "test submittal",
        "type": "product_data",
        "status": status,
        "sla_status": sla_status,
        "schedule_activity_id": schedule_activity_id,
        "per_task_impacts": list(per_task_impacts),
        # Aging fields drive the K-2 auto-suggest computation:
        # projected_impact_default = max(0, aging - sla_days).
        "aging_days": aging_days,
        "aging_working_days": aging_working_days,
        "sla_days_override": None,
        "sla_uses_calendar_days_override": None,
        # Standard register-depth fields (defensive nulls; not used
        # by the K-4/K-5 walks but kept for downstream readers).
        "submitted_to_ae_at": D(5, 1).isoformat(),
        "a_e_review_due_at": D(5, 14).isoformat(),
        "is_overdue": sla_status == "breached",
        "overdue_days": 3 if sla_status == "breached" else None,
        "urgency_flag": False,
    }
    _STUB.submittals.rows.append(doc)
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


async def _simulate_resolve_delay(
    delay_id: str, resolved_date: date,
) -> dict:
    """CPM Part L-3 (2026-05-16) -- replicate the resolve-delay flow
    at data-layer level. Mirrors update_delay() in field_events_
    router.py when status transitions to 'resolved':

      1. Status -> "resolved", resolved_date set, resolved_by stamped.
      2. actual_impact_days computed as working_days_between(
         logged_date, resolved_date), clamped at 0.
      3. auto_recompute_after_event fires (which now also runs
         compute_remaining_float_for_tasks, refreshing the live
         remaining_float_days on every task).

    Returns the updated delay row.
    """
    from services.working_days import working_days_between
    delay = await _get_delay(delay_id)
    logged_d = (
        date.fromisoformat(str(delay.get("logged_date"))[:10])
        if delay.get("logged_date")
        else date.fromisoformat(str(delay.get("date"))[:10])
    )
    actual = max(0, working_days_between(logged_d, resolved_date))
    updates = {
        "status": "resolved",
        "resolved_date": resolved_date.isoformat(),
        "resolved_by_user_id": "test-user",
        "actual_impact_days": actual,
    }
    await _STUB.delays.update_one(
        {"id": delay_id, "tenant_id": TENANT},
        {"$set": updates},
    )
    # Trigger recompute (compute_cpm + compute_projected_finish +
    # NEW Part L compute_remaining_float).
    await auto_recompute_after_event(
        TENANT, PROJECT, actor_id="test", request_id="req-resolve",
    )
    return await _get_delay(delay_id)


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

    # CPM Part L (2026-05-16) -- working-day shift, was calendar.
    # T_A.planned_finish = 5/6 Wed; +5 working days = 5/13 Wed
    # (Thu, Fri, Mon, Tue, Wed -- skipping 5/9-5/10 weekend).
    # Pre-Part-L expected 5/11 (calendar 5 days from 5/6).
    assert t_a["projected_finish_date"] == "2026-05-13", (
        f"T_A.projected_finish_date expected 2026-05-13 "
        f"(Part L working-day shift), got "
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

    # CPM Part L (2026-05-16) -- T_C's ES = next_business_day(T_B.EF=5/8 Fri)
    # = 5/11 Mon (skipping 5/9-5/10 weekend). T_C dur=4, EF = 5/14 Thu.
    # Pre-Part-L expected 5/12 (calendar B.EF+1 = 5/9 Sat).
    assert result.get("project_finish") == "2026-05-14", (
        f"project_finish expected 2026-05-14 "
        f"(Part L working-day forward), got {result.get('project_finish')!r}"
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

    # CPM Part L (2026-05-16) -- FS+7 is now 7 WORKING days (was
    # calendar). A finishes Fri 5/8; +1 day -> Mon 5/11; +7 working
    # days -> 5/20 Wed; B dur=3 -> EF = 5/22 Fri.
    # Pre-Part-L expected 5/18 (calendar: 5/8 +1 +7 = 5/16 Sat,
    # B.EF = 5/16 + 2 = 5/18). Part L correctly skips both the
    # immediate weekend AND the second weekend inside the 7-day lag.
    assert result.get("project_finish") == "2026-05-22", (
        f"FS+7 cure (Part L working-day lag): project_finish "
        f"expected 2026-05-22, got {result.get('project_finish')!r}"
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

    # CPM Part L (2026-05-16) -- B.ES = A.ES + 2 working days = 5/6
    # Wed; B dur=4 -> EF = 5/11 Mon (skipping 5/9-5/10 weekend).
    # project_finish = max(A.EF=5/8, B.EF=5/11) = 5/11.
    # Pre-Part-L expected 5/9 (calendar: B.ES=5/6, +3 calendar = 5/9 Sat).
    assert result.get("project_finish") == "2026-05-11", (
        f"SS+2 (Part L working-day): project_finish expected "
        f"2026-05-11, got {result.get('project_finish')!r}"
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

    # CPM Part L (2026-05-16) -- A.ES=5/8 Fri, dur=5, EF = add_
    # working_days(5/8, 4) = 5/14 Thu (skipping 5/9-5/10 weekend).
    # B SF+0: B.EF = A.ES = 5/8 Fri; B.ES = add_working_days(5/8, -2)
    # = 5/6 Wed; B.dur=3, B.EF stays 5/8. project_finish = max(A.EF,
    # B.EF) = 5/14. Pre-Part-L expected 5/12.
    assert result.get("project_finish") == "2026-05-14", (
        f"SF+0 (Part L working-day): project_finish expected "
        f"2026-05-14, got {result.get('project_finish')!r}"
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
    # CPM Part L (2026-05-16) -- shift is 5 WORKING days. T_B planned
    # finish = 5/11 Mon; add_working_days(5/11, 5) = 5/18 Mon
    # (skipping 5/16-5/17 weekend). Pre-Part-L expected 5/16
    # (calendar: 5/11 + 5 = 5/16 Sat).
    assert t_b["projected_finish_date"] == "2026-05-18", (
        f"projected_finish_date expected 5/18 (Part L working-day "
        f"shift from 5/11 + 5 wd), got "
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
    # CPM Part L (2026-05-16) -- 2-day synthetic delay shift is in
    # WORKING days. add_working_days(5/8 Fri, 2) = 5/12 Tue (skipping
    # 5/9-5/10 weekend). Pre-Part-L expected 5/10 Sun (calendar).
    assert t_a["projected_finish_date"] == "2026-05-12", (
        f"Synthetic delay (Part L working-day shift) should push "
        f"task-A finish 2 wd: planned 5/8 -> projected 5/12. "
        f"Got {t_a['projected_finish_date']!r}"
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
# CPM PART J-7 (2026-05-15) -- RFI IMPACT-DAYS REGRESSION TESTS
# =============================================================================
# The 6 new tests cover the cross-collection RFI path:
#   * Path 1 (Slip / Impacted Finish via compute_projected_finish_dates)
#   * Path 2 (cumulative remaining-float verdict via _resolve_*)
#   * Bug #53 invariant preserved on no-impact RFIs (binary blocker)
#
# Same 2-task LONG/SHORT graph as Part I:
#   T_LONG  (dur 10, planned 5/4..5/13)  -- structural float 0
#   T_SHORT (dur 5,  planned 5/4..5/8)   -- structural float 5
#
# This is the test-graph contract -- structural float on SHORT is
# always 5 unless the test explicitly changes durations. Tests log
# RFIs / delays / synthetic QC delays against SHORT and assert
# verdict + Slip behavior.

# -----------------------------------------------------------------------------
# J-7-1. test_rfi_impact_pushes_slip_and_impacted_finish
# -----------------------------------------------------------------------------
async def _test_rfi_impact_pushes_slip_and_impacted_finish():
    """Path 1: an open RFI with 3-day impact on a task pushes that
    task's projected_finish_date by 3 days (and projected_start_date
    by 3 days), exactly as a delay would. Confirms compute_projected_
    finish_dates' Part J-4 extension correctly walks the rfis
    collection.

    Setup:
        T_SHORT structural float = 5, planned 5/4..5/8
        Open RFI on T_SHORT with 3-day impact, status="draft"

    Action:
        compute_projected_finish_dates(t1, p-msu)

    Assert:
        T_SHORT.projected_finish_date == 2026-05-11  (5/8 + 3d)
        T_SHORT.projected_start_date  == 2026-05-07  (5/4 + 3d)
        T_LONG (no RFI) untouched -> projected_finish_date None
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)
    t_short = await _get_task("task-SHORT")
    assert t_short["total_float_days"] == 5

    _add_open_rfi("task-SHORT", 3)

    await compute_projected_finish_dates(_STUB, TENANT, PROJECT)

    t_short = await _get_task("task-SHORT")
    t_long = await _get_task("task-LONG")
    # CPM Part L (2026-05-16) -- 3-day RFI shift in WORKING days.
    # planned_finish 5/8 Fri + 3 wd = 5/13 Wed (skip 5/9-5/10).
    # planned_start 5/4 Mon + 3 wd = 5/7 Thu (no weekend in span).
    # Pre-Part-L expected 5/11 finish (calendar 5/8+3=5/11 Mon).
    assert t_short["projected_finish_date"] == "2026-05-13", (
        f"RFI 3d impact (Part L working-day) should push 5/8 -> "
        f"5/13; got {t_short['projected_finish_date']!r}"
    )
    assert t_short["projected_start_date"] == "2026-05-07", (
        f"RFI 3d impact should push 5/4 -> 5/7 (no weekend in span); "
        f"got {t_short['projected_start_date']!r}"
    )
    # Slip in calendar days from planned_finish (5/8 Fri) to
    # projected (5/13 Wed) = 5 calendar days (Sat, Sun, Mon, Tue,
    # Wed). The Tasks UI's slipDays() helper renders calendar days
    # because users read off a calendar — that's the displayed
    # slip column. The underlying SHIFT was 3 working days; calendar
    # slip reflects the 2 weekend days inside the span. Part L
    # working-day fix doesn't change slipDays() (frontend), only
    # the projected_finish_date (backend). Slip changes from 3
    # calendar (pre-Part-L) to 5 calendar (Part L) because the
    # weekend now contributes to the calendar slip count.
    slip = (
        date.fromisoformat(t_short["projected_finish_date"])
        - date.fromisoformat(t_short["planned_finish"])
    ).days
    assert slip == 5, f"slip (calendar) expected 5, got {slip}"
    # T_LONG has no RFI / delay -> no shift.
    assert t_long.get("projected_finish_date") is None
    assert t_long.get("projected_start_date") is None


# -----------------------------------------------------------------------------
# J-7-2. test_rfi_contributes_to_delay_remaining_float
# -----------------------------------------------------------------------------
async def _test_rfi_contributes_to_delay_remaining_float():
    """Path 2: an open RFI with 3-day impact is in the prior-sum
    when a subsequent delay is logged on the same task. The delay's
    verdict snapshot reads remaining = structural - RFI_prior, not
    structural alone. Confirms _sum_prior_open_blocker_impacts_on_task
    walks RFIs.

    Setup:
        T_SHORT structural float = 5
        Prior open RFI on T_SHORT with 3-day impact

    Action:
        Simulate a new delay create on T_SHORT with 3-day impact.

    Assert (the delay's snapshot, NOT the RFI's):
        prior_open_impacts_at_logging_days == 3  (RFI contributes)
        remaining_float_at_logging_days    == 2  (5 - 3)
        consumes_float_only                == False  (3 > 2)
        pushes_critical_path               == True
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    _add_open_rfi("task-SHORT", 3)

    delay = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap = (await _get_delay(delay["id"]))["per_task_impacts"][0]

    assert snap["prior_open_impacts_at_logging_days"] == 3.0, (
        f"prior RFI (3d) should contribute to the delay's prior-sum; "
        f"got prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 2.0  # 5 - 3
    assert snap["consumes_float_only"] is False, (
        f"delay 3d > remaining 2d -> NOT within float; "
        f"got snap={snap!r}"
    )
    assert snap["pushes_critical_path"] is True


# -----------------------------------------------------------------------------
# J-7-3. test_rfi_resolved_excluded_from_future_calcs
# -----------------------------------------------------------------------------
async def _test_rfi_resolved_excluded_from_future_calcs():
    """Status filter: an answered / closed RFI does NOT count toward
    the prior-sum. Confirms the status:[draft, sent, in_review] filter
    in _sum_prior_open_blocker_impacts_on_task (J-5) correctly
    excludes terminal-state RFIs.

    Setup:
        T_SHORT structural float = 5
        Prior RFI on T_SHORT with 3-day impact, status="answered"
        (the RFI was filed earlier with an impact estimate but has
        since been answered by the design team -- it no longer
        constrains the schedule)

    Action:
        Simulate a new delay create on T_SHORT with 3-day impact.

    Assert:
        prior_open_impacts_at_logging_days == 0   (RFI excluded)
        remaining_float_at_logging_days    == 5
        consumes_float_only                == True   (3 <= 5)
        pushes_critical_path               == False
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    _add_open_rfi("task-SHORT", 3, status="answered")

    delay = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap = (await _get_delay(delay["id"]))["per_task_impacts"][0]

    assert snap["prior_open_impacts_at_logging_days"] == 0.0, (
        f"answered RFI must NOT count toward prior-sum; "
        f"got prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 5.0
    assert snap["consumes_float_only"] is True   # 3 <= 5
    assert snap["pushes_critical_path"] is False


# -----------------------------------------------------------------------------
# J-7-4. test_rfi_edit_self_excluded_from_prior_sum
# -----------------------------------------------------------------------------
async def _test_rfi_edit_self_excluded_from_prior_sum():
    """exclude_rfi_id self-exclusion: editing an RFI's impact does
    NOT double-count its own pre-edit value in the prior-sum.
    Confirms the exclude_rfi_id kwarg threaded from
    _resolve_rfi_task_impacts (Part J-2) into the J-5 helper.

    Setup:
        T_SHORT structural float = 5
        Existing open RFI on T_SHORT with 3-day impact

    Action:
        Edit the SAME RFI to 4-day impact via direct call to
        _resolve_rfi_task_impacts (mirrors what update_rfi flow does
        post-recompute), passing exclude_rfi_id=<that rfi's id>.

    Assert (on the edited snapshot):
        prior_open_impacts_at_logging_days == 0   (self excluded)
        remaining_float_at_logging_days    == 5   (5 - 0)
        consumes_float_only                == True   (4 <= 5)
        pushes_critical_path               == False
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    existing = _add_open_rfi("task-SHORT", 3, rfi_id="rfi-self-edit")

    # Simulate the update_rfi flow's post-recompute re-resolve: the
    # caller passes the EDITED per_task_impacts (4d, not 3d) plus
    # exclude_rfi_id=<self> so the in-DB 3d entry is not double-counted.
    edited_doc = {
        "id": "rfi-self-edit",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "linked_task_ids": ["task-SHORT"],
        "per_task_impacts": [
            {"task_id": "task-SHORT", "projected_impact_days": 4},
        ],
    }
    _, stamped = await _resolve_rfi_task_impacts(
        _STUB, TENANT, edited_doc,
        project_id=PROJECT,
        exclude_rfi_id="rfi-self-edit",
    )
    snap = stamped[0]
    assert snap["projected_impact_days"] == 4
    assert snap["prior_open_impacts_at_logging_days"] == 0.0, (
        f"editing 3->4 must exclude self from prior-sum; "
        f"got prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 5.0
    assert snap["consumes_float_only"] is True   # 4 <= 5
    assert snap["pushes_critical_path"] is False


# -----------------------------------------------------------------------------
# J-7-5. test_mixed_rfi_delay_synthetic_qc_cumulative
# -----------------------------------------------------------------------------
async def _test_mixed_rfi_delay_synthetic_qc_cumulative():
    """Three prior blocker types contributing to a new delay's
    prior-sum: an open RFI (3d), an open delay (2d), and a QC-
    synthetic delay (2d via rework_required). The new delay's
    verdict sees the cumulative prior of 7d.

    Setup:
        T_SHORT structural float = 10  (use dur 3 for SHORT path
        to get 10d float against the longer 13d backbone)
        Open RFI    on T_SHORT, 3d impact
        Open delay  on T_SHORT, 2d impact
        QC issue    on T_SHORT, severity=rework_required,
                    rework_duration_hours=16 -> synthetic delay 2d

    Action:
        Simulate a new delay create on T_SHORT with 4-day impact.

    Assert:
        prior_open_impacts_at_logging_days == 7.0  (3 RFI + 2 delay + 2 QC)
        remaining_float_at_logging_days    == 3.0  (10 - 7)
        consumes_float_only                == False  (4 > 3)
        pushes_critical_path               == True   (4 > 3)
    """
    _reset()
    # Use a longer LONG (dur 13) so SHORT (dur 3) has float = 10.
    _add_task("LONG", duration=13, planned_start=D(5, 4))   # 5/4..5/16
    _add_task("SHORT", duration=3, planned_start=D(5, 4))   # 5/4..5/6
    await compute_cpm(TENANT, PROJECT)
    t_short = await _get_task("task-SHORT")
    assert t_short["total_float_days"] == 10, (
        f"setup: SHORT structural float expected 10 "
        f"(LONG.EF 5/16 - SHORT.LS 5/14 + ... = 10); got "
        f"{t_short['total_float_days']!r}"
    )

    # Prior open RFI 3d.
    _add_open_rfi("task-SHORT", 3)

    # Prior open delay 2d (direct row, not simulated -- we want it
    # in DB without firing auto_recompute_after_event for this test
    # setup; it's "prior" from the new delay's perspective).
    _add_open_delay("task-SHORT", impact_days=2.0)

    # QC synthetic delay via create_synthetic_delay_for_qc:
    # rework_duration_hours = 16 -> 2.0d via _hours_to_days.
    qc_issue = {
        "id": "qc-rework-mixed",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "linked_task_ids": ["task-SHORT"],
        "severity": "rework_required",
        "rework_duration_hours": 16.0,
        "rework_duration_basis": "field super estimate",
        "description": "Slab cracked",
        "status": "open",
        "date": D(5, 14).isoformat(),
    }
    _STUB.qc_issues.rows.append(qc_issue)
    synthetic = await create_synthetic_delay_for_qc(
        tenant_id=TENANT, qc_issue=qc_issue,
        actor_id="test-user", request_id="req-qc-mixed",
    )
    assert synthetic is not None
    assert synthetic["delay_days"] == 2.0

    # NOW simulate the user logging a fresh delay (4d) on SHORT.
    new_delay = await _simulate_create_delay(
        "task-SHORT", impact_days=4.0,
    )
    snap = (await _get_delay(new_delay["id"]))["per_task_impacts"][0]

    # Cumulative prior: RFI 3 + delay 2 + QC-synthetic 2 = 7
    assert snap["prior_open_impacts_at_logging_days"] == 7.0, (
        f"mixed prior expected 7 (RFI 3 + delay 2 + QC 2); "
        f"got prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 3.0  # 10 - 7
    assert snap["consumes_float_only"] is False  # 4 > 3
    assert snap["pushes_critical_path"] is True


# -----------------------------------------------------------------------------
# J-7-6. test_rfi_no_impact_days_zero_propagation
# -----------------------------------------------------------------------------
async def _test_rfi_no_impact_days_zero_propagation():
    """Bug #53 invariant: an RFI with empty per_task_impacts is still
    a binary blocker (counts in has_open_blocker / open_blocker_count)
    but does NOT push Slip / Impacted Finish and does NOT contribute
    to remaining-float verdicts on subsequent events. Models the
    "RFI filed before super knows the schedule impact" case --
    impact_days are deferred but the blocker badge is immediate.

    Setup:
        T_SHORT structural float = 5
        RFI on T_SHORT with EMPTY per_task_impacts, status="draft"

    Action 1:
        compute_projected_finish_dates -> walks RFIs but the empty
        per_task_impacts array contributes 0 to own_impact.

    Assert 1 (Path 1: no Slip / Impacted Finish change):
        T_SHORT.projected_finish_date is None
        T_SHORT.projected_start_date  is None

    Action 2:
        compute_and_persist_status on T_SHORT.

    Assert 2 (Bug #53 preserved: binary blocker badge):
        T_SHORT.has_open_blocker      is True
        T_SHORT.open_blocker_count    >= 1

    Action 3:
        Simulate a new delay create on T_SHORT with 3d impact.

    Assert 3 (Path 2: RFI doesn't contribute to delay's prior-sum):
        prior_open_impacts_at_logging_days == 0.0  (no impact captured)
        remaining_float_at_logging_days    == 5.0
        consumes_float_only                == True   (3 <= 5)
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    # Empty per_task_impacts -- RFI filed without impact estimate.
    rfi = _add_open_rfi(
        "task-SHORT", impact_days=None,
        per_task_impacts=[],
        status="draft",
    )
    assert rfi["per_task_impacts"] == []   # sanity

    # Action 1 + Assert 1: Path 1 unaffected.
    await compute_projected_finish_dates(_STUB, TENANT, PROJECT)
    t_short = await _get_task("task-SHORT")
    assert t_short.get("projected_finish_date") is None, (
        f"RFI with empty per_task_impacts must NOT push Slip; "
        f"got projected_finish_date="
        f"{t_short.get('projected_finish_date')!r}"
    )
    assert t_short.get("projected_start_date") is None

    # Action 2 + Assert 2: Bug #53 invariant preserved.
    await compute_and_persist_status(_STUB, TENANT, "task-SHORT")
    t_short = await _get_task("task-SHORT")
    assert t_short.get("has_open_blocker") is True, (
        f"Bug #53: open RFI in [draft, sent, in_review] must surface "
        f"as a binary blocker on the linked task EVEN with empty "
        f"per_task_impacts; got "
        f"has_open_blocker={t_short.get('has_open_blocker')!r}"
    )
    assert t_short.get("open_blocker_count", 0) >= 1, (
        f"Bug #53: open_blocker_count expected >= 1; got "
        f"{t_short.get('open_blocker_count')!r}"
    )

    # Action 3 + Assert 3: Path 2 (RFI without impact_days contributes 0).
    delay = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap = (await _get_delay(delay["id"]))["per_task_impacts"][0]
    assert snap["prior_open_impacts_at_logging_days"] == 0.0, (
        f"RFI with no impact_days captured must contribute 0 to the "
        f"delay's prior-sum; got "
        f"prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 5.0
    assert snap["consumes_float_only"] is True   # 3 <= 5
    assert snap["pushes_critical_path"] is False


# =============================================================================
# CPM Part K-7 (2026-05-15) -- Submittal cross-collection regression tests.
# Six tests codifying K-2 / K-3 / K-4 / K-5 scenarios across the four-
# collection blocker walks (delays + RFIs + QC-synthetic + submittals).
# =============================================================================

# -----------------------------------------------------------------------------
# K-7-1. test_submittal_auto_suggest_on_sla_breach
# -----------------------------------------------------------------------------
async def _test_submittal_auto_suggest_on_sla_breach():
    """K-2 + K-4 wiring: a breached submittal with empty per_task_
    impacts has K-2 auto-suggest fire (one entry with source=
    system_suggested + original_suggested_days), and Path 1
    (compute_projected_finish_dates via _open_blocker_impact_for_task
    K-4 walk) pushes the linked task's projected_finish forward.

    Setup:
        T_SHORT structural float = 5 (T_LONG 10d, T_SHORT 5d, both root)
        Project default SLA = 14 working days
        Over-SLA submittal on T_SHORT:
          status="in_review", sla_status="breached"
          aging_working_days=17 (3 days past SLA -- K-2 will suggest 3)
          per_task_impacts=[]

    Action 1: call auto_suggest_per_task_impacts_for_submittal (K-2)
    Assert 1: entry stamped with source=system_suggested,
              projected_impact_days=3, original_suggested_days=3

    Action 2: compute_projected_finish_dates (Path 1)
    Assert 2: T_SHORT.projected_finish_date moved forward by 3 days
              T_SHORT planned_finish=5/8 -> projected_finish=5/11
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4),
              planned_finish=D(5, 8))
    await compute_cpm(TENANT, PROJECT)

    sub = _add_open_submittal(
        task_id="task-SHORT",
        per_task_impacts=[],       # K-2 will fill in the entry
        aging_working_days=17,     # 17 - 14 = 3 day suggestion
        submittal_id="sub-k7-1",
    )

    # Action 1: K-2 fires (only_when_breached=True; sub is breached).
    res = await auto_suggest_per_task_impacts_for_submittal(
        tenant_id=TENANT, submittal_id=sub["id"],
        only_when_breached=True,
    )
    assert res.get("ok") and res.get("changed"), res
    persisted = await _STUB.submittals.find_one(
        {"id": sub["id"], "tenant_id": TENANT},
    )
    assert persisted["per_task_impacts"], (
        f"K-2 should have written an entry; got {persisted['per_task_impacts']!r}"
    )
    entry = persisted["per_task_impacts"][0]
    assert entry["source"] == "system_suggested", entry
    assert entry["projected_impact_days"] == 3, entry
    assert entry["original_suggested_days"] == 3, (
        f"K-6 baseline stamp expected 3; got {entry!r}"
    )
    assert entry["task_id"] == "task-SHORT"

    # Action 2: Path 1 picks up the new submittal impact via K-4 walk.
    await compute_projected_finish_dates(_STUB, TENANT, PROJECT)
    t_short = await _get_task("task-SHORT")
    # CPM Part L (2026-05-16) -- 3-day shift in WORKING days.
    # planned_finish 5/8 Fri + 3 wd = 5/13 Wed (skipping 5/9-5/10).
    # Pre-Part-L expected 5/11 (calendar 5/8+3).
    assert t_short["projected_finish_date"] == "2026-05-13", (
        f"T_SHORT planned 5/8 + 3-wd submittal impact (Part L "
        f"working-day shift) -> 5/13; got "
        f"{t_short['projected_finish_date']!r}"
    )


# -----------------------------------------------------------------------------
# K-7-2. test_submittal_super_override_preserved_across_re_breach
# -----------------------------------------------------------------------------
async def _test_submittal_super_override_preserved_across_re_breach():
    """K-2 idempotency on re-breach: super_overridden entries are
    NEVER overwritten when K-2 re-fires after a breach -> unbreach
    -> rebreach cycle. Also confirms system_suggested entries are
    skipped on re-breach (not "refreshed" with current aging). Each
    impact value represents the moment it was captured -- federal
    TIA defensibility requires immutable per-event snapshots.

    Setup:
        Task with float = 5 (T_LONG 10d, T_SHORT 5d)
        Submittal currently breached with entry:
          { projected_impact_days: 5, source: super_overridden,
            original_suggested_days: 3 }
        (super edited the original auto-suggestion of 3 up to 5)
        aging_working_days bumped to 20 (would suggest 6 if re-fired)

    Action:
        Call auto_suggest_per_task_impacts_for_submittal (simulates
        re-breach after the submittal cycled through approved ->
        rejected/revise -> back to in_review breached).

    Assert:
        sub.per_task_impacts still has 1 entry
        entry.projected_impact_days == 5 (super value unchanged)
        entry.source == "super_overridden" (not flipped back)
        entry.original_suggested_days == 3 (baseline preserved)
        res.changed == False (no diff -> no write -> no audit)
        res.skipped_super_overridden == ["task-SHORT"]
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    sub = _add_open_submittal(
        task_id="task-SHORT",
        impact_days=5,
        source="super_overridden",
        original_suggested_days=3,
        aging_working_days=20,    # would suggest 6 fresh -- must NOT overwrite
        submittal_id="sub-k7-2",
    )

    res = await auto_suggest_per_task_impacts_for_submittal(
        tenant_id=TENANT, submittal_id=sub["id"],
        only_when_breached=True,
    )
    assert res.get("ok") and not res.get("changed"), (
        f"super_overridden on re-breach must NOT trigger a write; "
        f"got res={res!r}"
    )
    assert res.get("skipped_super_overridden") == ["task-SHORT"], res

    persisted = await _STUB.submittals.find_one(
        {"id": sub["id"], "tenant_id": TENANT},
    )
    entry = persisted["per_task_impacts"][0]
    assert entry["projected_impact_days"] == 5, (
        f"super override 5 must survive re-breach; got {entry!r}"
    )
    assert entry["source"] == "super_overridden", entry
    assert entry["original_suggested_days"] == 3, (
        f"original baseline must ride through unchanged; got {entry!r}"
    )


# -----------------------------------------------------------------------------
# K-7-3. test_submittal_contributes_to_delay_remaining_float
# -----------------------------------------------------------------------------
async def _test_submittal_contributes_to_delay_remaining_float():
    """K-5 Path 2: an open over-SLA submittal with 4-day impact is in
    the prior-sum when a subsequent delay is logged on the same task.
    Verifies _sum_prior_open_blocker_impacts_on_task walks submittals.

    Setup:
        T_SHORT structural float = 10
        Open over-SLA submittal on T_SHORT, 4-day impact

    Action:
        Simulate a new delay create on T_SHORT with 4-day impact.

    Assert (delay's snapshot):
        prior_open_impacts_at_logging_days == 4   (submittal contributes)
        remaining_float_at_logging_days    == 6   (10 - 4)
        consumes_float_only                == True  (4 <= 6)
        pushes_critical_path               == False
    """
    _reset()
    _add_task("LONG", duration=15, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    _add_open_submittal("task-SHORT", impact_days=4)

    delay = await _simulate_create_delay("task-SHORT", impact_days=4.0)
    snap = (await _get_delay(delay["id"]))["per_task_impacts"][0]

    assert snap["prior_open_impacts_at_logging_days"] == 4.0, (
        f"open over-SLA submittal (4d) should contribute to the "
        f"delay's prior-sum; got "
        f"prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 6.0  # 10 - 4
    assert snap["consumes_float_only"] is True   # 4 <= 6
    assert snap["pushes_critical_path"] is False


# -----------------------------------------------------------------------------
# K-7-4. test_submittal_approved_excluded_from_future_calcs
# -----------------------------------------------------------------------------
async def _test_submittal_approved_excluded_from_future_calcs():
    """K-5 status filter: an approved submittal (status="approved",
    sla_status="n_a") does NOT contribute to the prior-sum even with
    per_task_impacts still populated on the row for audit history.
    Federal TIA defensibility: only currently-active blockers
    contribute. Auto-drop on disposition is the K-4/K-5 contract --
    when sla_status leaves "breached" OR status leaves [submitted,
    in_review], the row disappears from live calcs without
    retroactive snapshot rewrite.

    Setup:
        T_SHORT float = 10
        Approved submittal (status="approved", sla_status="n_a") with
        per_task_impacts entry of 3 days still present (audit history)

    Action:
        Simulate a new delay create on T_SHORT with 3-day impact.

    Assert:
        prior_open_impacts_at_logging_days == 0  (approved excluded)
        remaining_float_at_logging_days    == 10
        consumes_float_only                == True
        pushes_critical_path               == False
    """
    _reset()
    _add_task("LONG", duration=15, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    # Submittal that WAS breached, now approved. per_task_impacts
    # stays for audit but stops contributing.
    _add_open_submittal(
        "task-SHORT", impact_days=3,
        status="approved", sla_status="n_a",
    )

    delay = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap = (await _get_delay(delay["id"]))["per_task_impacts"][0]

    assert snap["prior_open_impacts_at_logging_days"] == 0.0, (
        f"approved submittal must NOT count toward prior-sum; "
        f"got prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 10.0
    assert snap["consumes_float_only"] is True   # 3 <= 10
    assert snap["pushes_critical_path"] is False


# -----------------------------------------------------------------------------
# K-7-5. test_mixed_all_four_event_types_cumulative
# -----------------------------------------------------------------------------
async def _test_mixed_all_four_event_types_cumulative():
    """K-5 cross-collection cumulative: RFI + delay + QC-synthetic +
    over-SLA submittal ALL on the same task. New delay's verdict
    reads the cumulative prior = 11 (3+2+2+4); 5d new delay > 4d
    remaining -> pushes_critical_path.

    Setup:
        T_SHORT structural float = 15
          (T_LONG dur 18, T_SHORT dur 3, both root, no convergence)
        Open RFI    on T_SHORT, 3d impact (status=draft)
        Open delay  on T_SHORT, 2d impact (status=open)
        QC issue    on T_SHORT, severity=rework_required,
                    rework_duration_hours=16 -> synthetic delay 2d
        Over-SLA submittal on T_SHORT, 4d impact

    Action:
        Simulate a new delay create on T_SHORT with 5d impact.

    Assert (the NEW delay's snapshot):
        prior_open_impacts_at_logging_days == 11  (3 RFI + 2 delay + 2 QC + 4 sub)
        remaining_float_at_logging_days    == 4   (15 - 11)
        consumes_float_only                == False  (5 > 4)
        pushes_critical_path               == True
    """
    _reset()
    _add_task("LONG", duration=18, planned_start=D(5, 4))
    _add_task("SHORT", duration=3, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    # Verify structural float on T_SHORT.
    t_short = await _get_task("task-SHORT")
    assert t_short["total_float_days"] == 15, (
        f"setup sanity: T_SHORT float expected 15 (18 - 3); got "
        f"{t_short['total_float_days']!r}"
    )

    # Add three prior blockers.
    _add_open_rfi("task-SHORT", 3)
    _add_open_delay("task-SHORT", impact_days=2)
    # QC-synthetic delay via the rework_required severity flow.
    qc_doc = {
        "id": f"qc-{uuid.uuid4()}",
        "tenant_id": TENANT, "project_id": PROJECT,
        "linked_task_ids": ["task-SHORT"],
        "status": "open",
        "severity": "rework_required",
        "rework_duration_hours": 16,   # 16h / 8h/day = 2 day synthetic
        "description": "test qc rework",
    }
    _STUB.qc_issues.rows.append(qc_doc)
    synthetic = await create_synthetic_delay_for_qc(
        tenant_id=TENANT, qc_issue=qc_doc,
        actor_id="test", request_id="req-test",
    )
    assert synthetic is not None and synthetic.get("delay_days") == 2.0, (
        f"setup sanity: 16h QC rework -> 2.0d synthetic delay; "
        f"got {synthetic!r}"
    )
    # Add the over-SLA submittal with 4d impact.
    _add_open_submittal("task-SHORT", impact_days=4)

    # Action: log the 5d delay -- its verdict reads cumulative prior.
    new_delay = await _simulate_create_delay("task-SHORT", impact_days=5.0)
    snap = (await _get_delay(new_delay["id"]))["per_task_impacts"][0]

    assert snap["prior_open_impacts_at_logging_days"] == 11.0, (
        f"mixed four-collection prior expected 11 (RFI 3 + delay 2 + "
        f"QC 2 + sub 4); got "
        f"prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 4.0  # 15 - 11
    assert snap["consumes_float_only"] is False, (
        f"new delay 5d > remaining 4d -> NOT within float; "
        f"got snap={snap!r}"
    )
    assert snap["pushes_critical_path"] is True


# -----------------------------------------------------------------------------
# K-7-6. test_submittal_empty_impacts_no_propagation
# -----------------------------------------------------------------------------
async def _test_submittal_empty_impacts_no_propagation():
    """Mirror of test_rfi_no_impact_days_zero_propagation: an over-
    SLA submittal with empty per_task_impacts still counts as a
    binary blocker (Bug #50 invariant) but does NOT contribute to
    Path 1 slip / Path 2 cumulative verdicts. Models the "super
    cleared the auto-suggestion to claim no schedule impact despite
    breach" case AND the "submittal breached before K-2 ran" case
    (legacy / transient state).

    Setup:
        T_SHORT structural float = 5
        Over-SLA submittal on T_SHORT, per_task_impacts=[]

    Action 1: compute_projected_finish_dates (Path 1)
    Assert 1: T_SHORT.projected_finish_date is None
              T_SHORT.projected_start_date  is None

    Action 2: compute_and_persist_status (count_open_blockers)
    Assert 2: T_SHORT.has_open_blocker is True   (Bug #50 preserved)
              T_SHORT.open_blocker_count >= 1

    Action 3: simulate new delay create with 3d impact (Path 2)
    Assert 3: prior_open_impacts_at_logging_days == 0
              remaining_float_at_logging_days    == 5
              consumes_float_only                == True  (3 <= 5)
              pushes_critical_path               == False
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    sub = _add_open_submittal(
        "task-SHORT", per_task_impacts=[],     # explicitly empty
    )
    assert sub["per_task_impacts"] == []   # sanity

    # Action 1 + Assert 1: Path 1 unaffected.
    await compute_projected_finish_dates(_STUB, TENANT, PROJECT)
    t_short = await _get_task("task-SHORT")
    assert t_short.get("projected_finish_date") is None, (
        f"over-SLA submittal with empty per_task_impacts must NOT push "
        f"Slip; got projected_finish_date="
        f"{t_short.get('projected_finish_date')!r}"
    )
    assert t_short.get("projected_start_date") is None

    # Action 2 + Assert 2: Bug #50 invariant preserved (binary blocker).
    await compute_and_persist_status(_STUB, TENANT, "task-SHORT")
    t_short = await _get_task("task-SHORT")
    assert t_short.get("has_open_blocker") is True, (
        f"Bug #50: over-SLA submittal must surface as a binary "
        f"blocker on the linked task EVEN with empty per_task_impacts; "
        f"got has_open_blocker={t_short.get('has_open_blocker')!r}"
    )
    assert t_short.get("open_blocker_count", 0) >= 1, (
        f"open_blocker_count expected >= 1; got "
        f"{t_short.get('open_blocker_count')!r}"
    )

    # Action 3 + Assert 3: Path 2 (submittal with no impacts -> 0 prior).
    delay = await _simulate_create_delay("task-SHORT", impact_days=3.0)
    snap = (await _get_delay(delay["id"]))["per_task_impacts"][0]
    assert snap["prior_open_impacts_at_logging_days"] == 0.0, (
        f"empty per_task_impacts submittal must contribute 0 to the "
        f"delay's prior-sum; got "
        f"prior={snap['prior_open_impacts_at_logging_days']!r}"
    )
    assert snap["remaining_float_at_logging_days"] == 5.0
    assert snap["consumes_float_only"] is True   # 3 <= 5
    assert snap["pushes_critical_path"] is False


# =============================================================================
# CPM Part L (2026-05-16) -- Working-day math + live remaining-float column.
# Seven regression tests:
#   L-1: working-day FS+0 lag (skips weekend)
#   L-2: working-day FS+3 lag (3 working days, not 3 calendar)
#   L-3: working-day duration accumulation (skips weekend)
#   L-4: remaining_float_days drops on delay create
#   L-5: remaining_float_days restored on delay resolve
#   L-6: remaining_float_days cumulative across multi-event scenario
#   L-7: actual_impact_days computed in working days at resolution
# =============================================================================

# -----------------------------------------------------------------------------
# L-7-1. test_working_day_fs_lag
# -----------------------------------------------------------------------------
async def _test_working_day_fs_lag():
    """FS+0 across a weekend: Task A finishes Fri 5/15, Task B FS+0
    must start Mon 5/18 -- NOT Sat 5/16. Verifies the next_business_
    day offset in _derive_implied_start."""
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 11))   # Mon 5/11
    _add_task("B", duration=3, planned_start=D(5, 18),   # placeholder
              predecessors=[_pred("A", "FS", 0)])
    result = await compute_cpm(TENANT, PROJECT)

    # A.ES = 5/11 Mon (root). A.EF = add_working_days(5/11, 4) = 5/15 Fri.
    # B.ES via FS+0 = next_business_day(5/15 Fri) = 5/18 Mon.
    # B.EF = add_working_days(5/18, 2) = 5/20 Wed.
    # project_finish = 5/20.
    t_b = await _get_task("task-B")
    assert result.get("project_finish") == "2026-05-20", (
        f"FS+0 across weekend: project_finish expected 5/20, got "
        f"{result.get('project_finish')!r}"
    )
    # B is on the CP -- both tasks consume the longest path with 0 float.
    assert t_b["total_float_days"] == 0
    assert t_b["is_critical_path"] is True
    print("L-7-1 PASS: FS+0 from Fri lands successor on Mon (not Sat)")


# -----------------------------------------------------------------------------
# L-7-2. test_working_day_fs_lag_with_lag
# -----------------------------------------------------------------------------
async def _test_working_day_fs_lag_with_lag():
    """FS+3 (3-working-day lag) from Fri 5/15: lag day 1 = Mon 5/18,
    day 2 = Tue 5/19, day 3 = Wed 5/20. Pre-Part-L treated lag as
    calendar (5/15+1+3=5/19 -- jumping over weekend by accident).
    Part L: 3 working days after next_business_day(5/15)=Mon, so
    Mon=1, Tue=2, Wed=3 -> Wed 5/20."""
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 11))   # Mon 5/11 -> Fri 5/15
    _add_task("B", duration=1, planned_start=D(5, 20),   # placeholder
              predecessors=[_pred("A", "FS", 3)])
    result = await compute_cpm(TENANT, PROJECT)

    # B.ES = add_working_days(next_business_day(5/15 Fri)=5/18 Mon, 3) =
    # 5/21 Thu (Mon+1=Tue, Tue+2=Wed, Wed+3=Thu... actually
    # add_working_days(5/18, 3) = Mon -> Tue (1) -> Wed (2) -> Thu (3) = 5/21).
    # Hmm let me re-trace: add_working_days steps n WORKING days
    # FORWARD from start; start IS day 0. So add_working_days(Mon, 3)
    # = Mon -> Tue (day 1) -> Wed (day 2) -> Thu (day 3) = Thu 5/21.
    # B dur=1, EF = ES = 5/21.
    t_b = await _get_task("task-B")
    expected_es = "2026-05-21"
    actual = result.get("project_finish")
    assert actual == expected_es, (
        f"FS+3 from Fri 5/15: B should start {expected_es} "
        f"(Fri->next Mon then +3 wd = Thu); got project_finish={actual!r}"
    )
    print("L-7-2 PASS: FS+3 lag treated as working days")


# -----------------------------------------------------------------------------
# L-7-3. test_working_day_duration_skips_weekend
# -----------------------------------------------------------------------------
async def _test_working_day_duration_skips_weekend():
    """Task starting Fri 5/15 with 3-working-day duration must finish
    Tue 5/19: Fri=day1, Mon=day2, Tue=day3 (skipping 5/16-5/17 weekend).
    Verifies EF = add_working_days(ES, dur-1) in compute_cpm."""
    _reset()
    _add_task("A", duration=3, planned_start=D(5, 15))   # Fri 5/15
    result = await compute_cpm(TENANT, PROJECT)

    t_a = await _get_task("task-A")
    # A.ES = 5/15 (already biz day; anchor unchanged).
    # A.EF = add_working_days(5/15 Fri, 2) = Fri (day0) -> Mon (1) -> Tue (2).
    # project_finish = 5/19.
    assert result.get("project_finish") == "2026-05-19", (
        f"3-wd duration from Fri 5/15: project_finish expected "
        f"5/19 Tue (Fri+Mon+Tue), got {result.get('project_finish')!r}"
    )
    assert t_a["is_critical_path"] is True
    assert t_a["total_float_days"] == 0
    print("L-7-3 PASS: duration accumulation skips weekend")


# -----------------------------------------------------------------------------
# L-7-4. test_remaining_float_drops_on_delay_create
# -----------------------------------------------------------------------------
async def _test_remaining_float_drops_on_delay_create():
    """Live remaining float decreases when a delay is logged.

    Setup:
        T_SHORT structural float = 5 (vs T_LONG 10-d backbone)
        No prior events on T_SHORT

    Action 1: compute_cpm + initial remaining_float refresh
    Assert 1: T_SHORT.remaining_float_days = 5 (full structural)

    Action 2: log a delay with 3-day projected impact
    Assert 2: T_SHORT.remaining_float_days = 2 (5 - 3)
              T_SHORT.total_float_days STILL = 5 (structural unchanged)
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))

    # Step 1: initial recompute populates remaining_float = structural.
    await auto_recompute_after_event(TENANT, PROJECT)
    t = await _get_task("task-SHORT")
    assert t["total_float_days"] == 5, t["total_float_days"]
    assert t["remaining_float_days"] == 5, (
        f"Fresh (no events) remaining_float should equal structural; "
        f"got remaining={t.get('remaining_float_days')!r}"
    )

    # Step 2: log a 3-day delay.
    await _simulate_create_delay("task-SHORT", impact_days=3)
    t = await _get_task("task-SHORT")
    # Structural float should NOT change.
    assert t["total_float_days"] == 5, (
        f"structural float must stay 5 after delay create; got "
        f"{t['total_float_days']!r}"
    )
    # Remaining float drops by 3.
    assert t["remaining_float_days"] == 2, (
        f"after 3-d delay, remaining should be 2 (5-3); got "
        f"{t.get('remaining_float_days')!r}"
    )
    print("L-7-4 PASS: remaining_float drops 5 -> 2 on 3-d delay create")


# -----------------------------------------------------------------------------
# L-7-5. test_remaining_float_restored_on_delay_resolve
# -----------------------------------------------------------------------------
async def _test_remaining_float_restored_on_delay_resolve():
    """When a delay is resolved, its impact STOPS contributing to
    the open-blocker sum, and remaining_float_days recovers on the
    next auto_recompute_after_event fire. actual_impact_days stamps
    on the delay row but does NOT affect remaining_float (resolved
    rows are filtered out entirely).

    Setup:
        T_SHORT structural float = 5
        Delay logged Mon 5/4 with 3-d projected impact -> remaining = 2

    Action:
        Resolve the delay on Tue 5/5 (1 working day later).

    Assert:
        delay.status == "resolved"
        delay.actual_impact_days == 1 (Mon->Tue = 1 working day)
        T_SHORT.remaining_float_days == 5 (fully restored;
            resolved delay no longer in open sum)
        T_SHORT.total_float_days == 5 (structural unchanged)
    """
    _reset()
    _add_task("LONG", duration=10, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    # Log delay (logged_date defaults to 5/14 in _simulate_create_delay,
    # but _add_open_delay above accepted logged_date kwarg. Use the
    # simulate helper for the full create flow + auto-recompute).
    delay = await _simulate_create_delay(
        "task-SHORT", impact_days=3, logged_date=D(5, 4),
    )
    t_pre = await _get_task("task-SHORT")
    assert t_pre["remaining_float_days"] == 2, (
        f"before resolve, remaining should be 2; got {t_pre!r}"
    )

    # Resolve on 5/5 (Tue, 1 working day after Mon 5/4).
    resolved = await _simulate_resolve_delay(delay["id"], D(5, 5))
    assert resolved["status"] == "resolved"
    assert resolved["actual_impact_days"] == 1, (
        f"working_days_between(5/4 Mon, 5/5 Tue) = 1; got "
        f"actual_impact_days={resolved.get('actual_impact_days')!r}"
    )

    t_post = await _get_task("task-SHORT")
    assert t_post["total_float_days"] == 5  # structural unchanged
    assert t_post["remaining_float_days"] == 5, (
        f"after resolve, remaining_float should RESTORE to 5 "
        f"(resolved delay no longer in open sum); got "
        f"{t_post.get('remaining_float_days')!r}"
    )
    print("L-7-5 PASS: remaining_float restored on resolve; "
          "actual_impact_days = 1 working day")


# -----------------------------------------------------------------------------
# L-7-6. test_remaining_float_multi_event_cumulative
# -----------------------------------------------------------------------------
async def _test_remaining_float_multi_event_cumulative():
    """Multi-event cumulative remaining_float walkthrough:
      Step 0: structural 10
      Step 1: log delay 3-d -> remaining = 7
      Step 2: open RFI 2-d -> remaining = 5
      Step 3: log delay 4-d -> remaining = 1
      Step 4: resolve first delay (actual = 1 wd) -> remaining = 3
              (10 - actual_of_resolved_no_longer_counted - 2 RFI - 4 delay = 3)

    NOTE: after the first delay is resolved, the open-impact sum is
    RFI 2 + second delay 4 = 6. remaining = 10 - 6 = 4. Hmm let me
    re-trace -- actually 10 - 6 = 4 not 3. Let me check the math.

    Pre-resolve total open = 3 + 2 + 4 = 9. remaining = 10 - 9 = 1.
    Resolve first delay: now open = 0 (resolved) + 2 + 4 = 6.
    remaining = 10 - 6 = 4. (NOT 3 as the spec said.)

    The spec gave "remaining = 3 (10 - 1 - 2 - 4)" which counts
    actual_impact (1) of the resolved delay. That's NOT what Part L
    computes -- per L-2 spec, remaining_float = structural - sum
    of OPEN blocker impacts. Resolved delays are EXCLUDED entirely
    (their actual_impact lives on the delay row for audit, but does
    NOT contribute to the open-sum). So the correct math is 10 - 6 = 4.

    Documenting the spec deviation here -- the SPEC's intent is
    captured by "resolved delays no longer contribute," which gives 4.
    """
    _reset()
    _add_task("LONG", duration=15, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)
    t = await _get_task("task-SHORT")
    assert t["total_float_days"] == 10, (
        f"setup sanity: SHORT float expected 10 (15-5); got "
        f"{t['total_float_days']!r}"
    )

    # Step 1: 3-d delay
    delay1 = await _simulate_create_delay(
        "task-SHORT", impact_days=3, logged_date=D(5, 4),
    )
    t = await _get_task("task-SHORT")
    assert t["remaining_float_days"] == 7, (
        f"step 1 remaining expected 7; got {t.get('remaining_float_days')!r}"
    )

    # Step 2: 2-d RFI
    _add_open_rfi("task-SHORT", 2)
    await auto_recompute_after_event(TENANT, PROJECT)
    t = await _get_task("task-SHORT")
    assert t["remaining_float_days"] == 5, (
        f"step 2 remaining expected 5; got {t.get('remaining_float_days')!r}"
    )

    # Step 3: 4-d delay
    delay2 = await _simulate_create_delay(
        "task-SHORT", impact_days=4, logged_date=D(5, 4),
    )
    t = await _get_task("task-SHORT")
    assert t["remaining_float_days"] == 1, (
        f"step 3 remaining expected 1 (10-3-2-4); got "
        f"{t.get('remaining_float_days')!r}"
    )

    # Step 4: resolve first delay 1-d later (actual=1 wd).
    await _simulate_resolve_delay(delay1["id"], D(5, 5))
    t = await _get_task("task-SHORT")
    # remaining = 10 - (2 RFI + 4 delay2) = 4. The resolved delay
    # drops from the open sum entirely; actual_impact_days lives on
    # the delay row for audit but doesn't reduce remaining_float.
    assert t["remaining_float_days"] == 4, (
        f"step 4 remaining expected 4 (10 - 0 resolved - 2 RFI "
        f"- 4 delay2); got {t.get('remaining_float_days')!r}"
    )
    print("L-7-6 PASS: cumulative remaining_float 10 -> 7 -> 5 -> 1 -> 4")


# -----------------------------------------------------------------------------
# L-7-7. test_actual_impact_days_calculation
# -----------------------------------------------------------------------------
async def _test_actual_impact_days_calculation():
    """actual_impact_days = working_days_between(logged_date,
    resolved_date). Validates the working-day delta math, NOT
    calendar.

    Setup:
        Delay logged Friday 5/15
        Resolved next Wednesday 5/20

    Assert:
        actual_impact_days == 3 (working days: Mon, Tue, Wed in span)
        NOT 5 (calendar Fri->Wed span)

    Walking working_days_between(5/15, 5/20):
        d=5/15 Fri: is_bd=True, count=1, d=5/16 Sat
        d=5/16 Sat: is_bd=False, count=1, d=5/17 Sun
        d=5/17 Sun: is_bd=False, count=1, d=5/18 Mon
        d=5/18 Mon: is_bd=True, count=2, d=5/19 Tue
        d=5/19 Tue: is_bd=True, count=3, d=5/20 Wed
        d=5/20 Wed: STOP (d >= end)
    Returns 3.
    """
    _reset()
    _add_task("LONG", duration=15, planned_start=D(5, 4))
    _add_task("SHORT", duration=5, planned_start=D(5, 4))
    await compute_cpm(TENANT, PROJECT)

    delay = await _simulate_create_delay(
        "task-SHORT", impact_days=5, logged_date=D(5, 15),
    )
    resolved = await _simulate_resolve_delay(delay["id"], D(5, 20))
    assert resolved["actual_impact_days"] == 3, (
        f"actual_impact_days = working_days_between(Fri 5/15, "
        f"Wed 5/20) = 3 (Fri + Mon + Tue, exclusive of Wed); "
        f"got {resolved.get('actual_impact_days')!r}"
    )
    # Sanity: projected_impact_days on the per_task_impacts snapshot
    # is UNCHANGED at 5 (audit immutability). Only the delay row's
    # actual_impact_days field captures the resolution-time variance.
    assert resolved["per_task_impacts"][0]["projected_impact_days"] == 5
    print("L-7-7 PASS: actual_impact_days=3 working days, "
          "projected_impact_days=5 immutable on snapshot")


# =============================================================================
# CPM Slip-cascade float-aware fix (2026-05-16) -- 5 regression tests.
# The pre-fix compute_projected_finish_dates over-propagated: a delay
# on a task with float cascaded its full impact to every downstream
# successor, even when the delayed task absorbed the delay entirely
# within its own float / gap. These tests pin the float-aware,
# date-based cascade: only genuine OVERFLOW past a successor's
# planned start propagates.
# =============================================================================

# -----------------------------------------------------------------------------
# SC-1. test_delay_within_float_does_not_cascade
# -----------------------------------------------------------------------------
async def _test_delay_within_float_does_not_cascade():
    """A delay on a task whose successor is gated by a DIFFERENT
    (later) predecessor must not cascade. The delayed task moves;
    nothing downstream does.

    Setup:
        A  dur 2, planned 5/4 Mon - 5/5 Tue   (lots of float)
        B  dur 20, planned 5/4 Mon - 5/29 Fri (binding predecessor)
        C  dur 3, planned 6/1 Mon - 6/3 Wed, preds=[A FS+0, B FS+0]
           -- C is gated by B (finishes 5/29), not A (finishes 5/5)

    Action: log a 2-day delay on A.

    Assert:
        A.projected_finish_date moved to 5/7 (5/5 + 2 working days)
        C.projected_finish_date is None   (delay absorbed by float)
        C.projected_start_date  is None
    """
    _reset()
    _add_task("A", duration=2, planned_start=D(5, 4),
              planned_finish=D(5, 5))
    _add_task("B", duration=20, planned_start=D(5, 4),
              planned_finish=D(5, 29))
    _add_task("C", duration=3, planned_start=D(6, 1),
              planned_finish=D(6, 3),
              predecessors=[_pred("A", "FS", 0), _pred("B", "FS", 0)])
    await compute_cpm(TENANT, PROJECT)

    # Sanity: A carries substantial structural float (B is binding).
    t_a_pre = await _get_task("task-A")
    assert (t_a_pre["total_float_days"] or 0) > 2, (
        f"setup sanity: A should have float > 2 (B binding); got "
        f"{t_a_pre['total_float_days']!r}"
    )

    await _simulate_create_delay("task-A", impact_days=2)

    t_a = await _get_task("task-A")
    t_c = await _get_task("task-C")
    # A itself moved +2 working days (5/5 -> 5/7 Thu).
    assert t_a["projected_finish_date"] == "2026-05-07", (
        f"directly-delayed A should move to 5/7; got "
        f"{t_a['projected_finish_date']!r}"
    )
    # C did NOT move -- A's 2-day delay is fully absorbed.
    assert t_c.get("projected_finish_date") is None, (
        f"C must NOT cascade -- A's delay absorbed by float / B "
        f"binding; got projected_finish_date="
        f"{t_c.get('projected_finish_date')!r}"
    )
    assert t_c.get("projected_start_date") is None, (
        f"C.projected_start_date must be None; got "
        f"{t_c.get('projected_start_date')!r}"
    )
    print("SC-1 PASS: delay within float does NOT cascade")


# -----------------------------------------------------------------------------
# SC-2. test_delay_exceeding_gap_cascades_only_overflow
# -----------------------------------------------------------------------------
async def _test_delay_exceeding_gap_cascades_only_overflow():
    """When a delay EXCEEDS the gap to a successor, only the overflow
    cascades -- not the full delay.

    Setup:
        A  dur 5, planned 5/4 Mon - 5/8 Fri
        C  dur 3, planned 5/18 Mon - 5/20 Wed, preds=[A FS+0]
           -- 5-working-day gap (A FS+0 implies 5/11; C planned 5/18)

    Action: log an 8-day delay on A.

    Assert:
        A.projected_finish_date = 5/20 (5/8 + 8 working days)
        C moves by EXACTLY 3 working days (8 delay - 5 gap), NOT 8.
        C.projected_finish_date = 5/25 (5/20 + 3 working days)
    """
    _reset()
    _add_task("A", duration=5, planned_start=D(5, 4),
              planned_finish=D(5, 8))
    _add_task("C", duration=3, planned_start=D(5, 18),
              planned_finish=D(5, 20),
              predecessors=[_pred("A", "FS", 0)])
    await compute_cpm(TENANT, PROJECT)

    await _simulate_create_delay("task-A", impact_days=8)

    t_a = await _get_task("task-A")
    t_c = await _get_task("task-C")
    assert t_a["projected_finish_date"] == "2026-05-20", (
        f"A + 8 working days from 5/8 -> 5/20; got "
        f"{t_a['projected_finish_date']!r}"
    )
    assert t_c["projected_finish_date"] == "2026-05-25", (
        f"C should move only the 3-day overflow (8 delay - 5 gap) "
        f"-> 5/20 + 3 wd = 5/25; got {t_c['projected_finish_date']!r}"
    )
    # Verify the overflow magnitude precisely.
    c_move = working_days_between(
        date.fromisoformat("2026-05-20"),   # C planned_finish
        date.fromisoformat(t_c["projected_finish_date"]),
    )
    assert c_move == 3, (
        f"C should move exactly 3 working days (overflow), not 8; "
        f"got {c_move}"
    )
    print("SC-2 PASS: delay exceeding gap cascades only the overflow")


# -----------------------------------------------------------------------------
# SC-3. test_delay_on_critical_path_cascades_fully
# -----------------------------------------------------------------------------
async def _test_delay_on_critical_path_cascades_fully():
    """A delay on a CRITICAL-PATH task (zero gaps downstream) MUST
    cascade fully -- that's correct behavior, NOT the bug.

    Setup (pure FS+0 zero-gap chain, all critical):
        A  dur 3, planned 5/4 Mon - 5/6 Wed
        B  dur 2, planned 5/7 Thu - 5/8 Fri, preds=[A FS+0]
        C  dur 4, planned 5/11 Mon - 5/14 Thu, preds=[B FS+0]

    Action: log a 3-day delay on A.

    Assert: A, B, C ALL move +3 working days. The project finish
    (C, the tail) moves +3.
    """
    _reset()
    _add_task("A", duration=3, planned_start=D(5, 4),
              planned_finish=D(5, 6))
    _add_task("B", duration=2, planned_start=D(5, 7),
              planned_finish=D(5, 8),
              predecessors=[_pred("A", "FS", 0)])
    _add_task("C", duration=4, planned_start=D(5, 11),
              planned_finish=D(5, 14),
              predecessors=[_pred("B", "FS", 0)])
    await compute_cpm(TENANT, PROJECT)

    await _simulate_create_delay("task-A", impact_days=3)

    t_a = await _get_task("task-A")
    t_b = await _get_task("task-B")
    t_c = await _get_task("task-C")
    # A: 5/6 + 3 wd = 5/11 Mon.
    assert t_a["projected_finish_date"] == "2026-05-11", (
        f"A + 3 wd -> 5/11; got {t_a['projected_finish_date']!r}"
    )
    # B: 5/8 + 3 wd = 5/13 Wed (zero gap -> full cascade).
    assert t_b["projected_finish_date"] == "2026-05-13", (
        f"B should cascade fully (+3) on a zero-float chain -> 5/13; "
        f"got {t_b['projected_finish_date']!r}"
    )
    # C: 5/14 + 3 wd = 5/19 Tue.
    assert t_c["projected_finish_date"] == "2026-05-19", (
        f"C (project tail) should cascade fully (+3) -> 5/19; got "
        f"{t_c['projected_finish_date']!r}"
    )
    # Project finish (the tail task C) moved exactly 3 working days.
    proj_move = working_days_between(
        date.fromisoformat("2026-05-14"),   # C planned_finish
        date.fromisoformat(t_c["projected_finish_date"]),
    )
    assert proj_move == 3, (
        f"project finish should move +3 on a CP delay; got {proj_move}"
    )
    print("SC-3 PASS: delay on critical path cascades fully (+3 all)")


# -----------------------------------------------------------------------------
# SC-4. test_delay_partially_absorbed_then_cascades
# -----------------------------------------------------------------------------
async def _test_delay_partially_absorbed_then_cascades():
    """A delay larger than the available gap: the delayed task moves
    by its full delay; the downstream task moves only by the
    overflow (delay - gap).

    Setup:
        T  dur 3, planned 5/4 Mon - 5/6 Wed
        D  dur 4, planned 5/11 Mon - 5/14 Thu, preds=[T FS+0]
           -- 2-working-day gap (T FS+0 implies 5/7; D planned 5/11)

    Action: log a 5-day delay on T.

    Assert:
        T moves +5 (5/6 -> 5/13 Wed)
        D moves +3 (5 delay - 2 gap absorbed) -> 5/14 + 3 = 5/19 Tue
    """
    _reset()
    _add_task("T", duration=3, planned_start=D(5, 4),
              planned_finish=D(5, 6))
    _add_task("D", duration=4, planned_start=D(5, 11),
              planned_finish=D(5, 14),
              predecessors=[_pred("T", "FS", 0)])
    await compute_cpm(TENANT, PROJECT)

    await _simulate_create_delay("task-T", impact_days=5)

    t_t = await _get_task("task-T")
    t_d = await _get_task("task-D")
    # T itself moves the full 5 working days: 5/6 + 5 wd = 5/13 Wed.
    assert t_t["projected_finish_date"] == "2026-05-13", (
        f"directly-delayed T should move +5 -> 5/13; got "
        f"{t_t['projected_finish_date']!r}"
    )
    # D moves only the 3-day overflow (5 - 2 gap): 5/14 + 3 = 5/19.
    assert t_d["projected_finish_date"] == "2026-05-19", (
        f"D should move only +3 (5 delay - 2 gap) -> 5/19; got "
        f"{t_d['projected_finish_date']!r}"
    )
    d_move = working_days_between(
        date.fromisoformat("2026-05-14"),   # D planned_finish
        date.fromisoformat(t_d["projected_finish_date"]),
    )
    assert d_move == 3, (
        f"D should move exactly 3 working days; got {d_move}"
    )
    print("SC-4 PASS: 5-day delay, 2-day gap absorbs -> downstream +3")


# -----------------------------------------------------------------------------
# SC-5. test_two_predecessors_only_binding_one_cascades
# -----------------------------------------------------------------------------
async def _test_two_predecessors_only_binding_one_cascades():
    """A successor with two predecessors moves only when its BINDING
    predecessor is delayed. Delaying the non-binding (earlier) one
    within the gap does nothing.

    Setup:
        A  dur 2, planned 5/4 Mon - 5/5 Tue   (non-binding, earlier)
        B  dur 6, planned 5/4 Mon - 5/11 Mon  (binding, later)
        C  dur 3, planned 5/12 Tue - 5/14 Thu,
           preds=[A FS+0, B FS+0]  -- gated by B

    Action 1: log a 3-day delay on A (non-binding, within the gap).
    Assert 1: C does NOT move.

    Action 2: log a 4-day delay on B (binding).
    Assert 2: C moves with B (+4).
    """
    _reset()
    _add_task("A", duration=2, planned_start=D(5, 4),
              planned_finish=D(5, 5))
    _add_task("B", duration=6, planned_start=D(5, 4),
              planned_finish=D(5, 11))
    _add_task("C", duration=3, planned_start=D(5, 12),
              planned_finish=D(5, 14),
              predecessors=[_pred("A", "FS", 0), _pred("B", "FS", 0)])
    await compute_cpm(TENANT, PROJECT)

    # Action 1: delay the non-binding predecessor A.
    await _simulate_create_delay("task-A", impact_days=3)
    t_a = await _get_task("task-A")
    t_c = await _get_task("task-C")
    assert t_a["projected_finish_date"] == "2026-05-08", (
        f"A + 3 wd from 5/5 -> 5/8; got {t_a['projected_finish_date']!r}"
    )
    assert t_c.get("projected_finish_date") is None, (
        f"C must NOT move -- A is non-binding, delay within gap; "
        f"got {t_c.get('projected_finish_date')!r}"
    )

    # Action 2: delay the binding predecessor B.
    await _simulate_create_delay("task-B", impact_days=4)
    t_b = await _get_task("task-B")
    t_c = await _get_task("task-C")
    # B: 5/11 + 4 wd = 5/15 Fri.
    assert t_b["projected_finish_date"] == "2026-05-15", (
        f"B + 4 wd from 5/11 -> 5/15; got {t_b['projected_finish_date']!r}"
    )
    # C now moves with B: 5/14 + 4 wd = 5/20 Wed.
    assert t_c["projected_finish_date"] == "2026-05-20", (
        f"C should cascade with binding B (+4) -> 5/20; got "
        f"{t_c.get('projected_finish_date')!r}"
    )
    c_move = working_days_between(
        date.fromisoformat("2026-05-14"),   # C planned_finish
        date.fromisoformat(t_c["projected_finish_date"]),
    )
    assert c_move == 4, (
        f"C should move +4 with the binding predecessor; got {c_move}"
    )
    print("SC-5 PASS: only the binding predecessor's delay cascades")


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


# CPM Part J-7 (2026-05-15) -- RFI impact-days regression tests.
def test_rfi_impact_pushes_slip_and_impacted_finish():
    asyncio.run(_test_rfi_impact_pushes_slip_and_impacted_finish())


def test_rfi_contributes_to_delay_remaining_float():
    asyncio.run(_test_rfi_contributes_to_delay_remaining_float())


def test_rfi_resolved_excluded_from_future_calcs():
    asyncio.run(_test_rfi_resolved_excluded_from_future_calcs())


def test_rfi_edit_self_excluded_from_prior_sum():
    asyncio.run(_test_rfi_edit_self_excluded_from_prior_sum())


def test_mixed_rfi_delay_synthetic_qc_cumulative():
    asyncio.run(_test_mixed_rfi_delay_synthetic_qc_cumulative())


def test_rfi_no_impact_days_zero_propagation():
    asyncio.run(_test_rfi_no_impact_days_zero_propagation())


# CPM Part K-7 (2026-05-15) -- Submittal impact-days regression tests.
def test_submittal_auto_suggest_on_sla_breach():
    asyncio.run(_test_submittal_auto_suggest_on_sla_breach())


def test_submittal_super_override_preserved_across_re_breach():
    asyncio.run(_test_submittal_super_override_preserved_across_re_breach())


def test_submittal_contributes_to_delay_remaining_float():
    asyncio.run(_test_submittal_contributes_to_delay_remaining_float())


def test_submittal_approved_excluded_from_future_calcs():
    asyncio.run(_test_submittal_approved_excluded_from_future_calcs())


def test_mixed_all_four_event_types_cumulative():
    asyncio.run(_test_mixed_all_four_event_types_cumulative())


def test_submittal_empty_impacts_no_propagation():
    asyncio.run(_test_submittal_empty_impacts_no_propagation())


# CPM Part L (2026-05-16) -- Working-day math + live remaining-float tests.
def test_working_day_fs_lag():
    asyncio.run(_test_working_day_fs_lag())


def test_working_day_fs_lag_with_lag():
    asyncio.run(_test_working_day_fs_lag_with_lag())


def test_working_day_duration_skips_weekend():
    asyncio.run(_test_working_day_duration_skips_weekend())


def test_remaining_float_drops_on_delay_create():
    asyncio.run(_test_remaining_float_drops_on_delay_create())


def test_remaining_float_restored_on_delay_resolve():
    asyncio.run(_test_remaining_float_restored_on_delay_resolve())


def test_remaining_float_multi_event_cumulative():
    asyncio.run(_test_remaining_float_multi_event_cumulative())


def test_actual_impact_days_calculation():
    asyncio.run(_test_actual_impact_days_calculation())


# CPM Slip-cascade float-aware fix (2026-05-16) -- 5 cascade tests.
def test_delay_within_float_does_not_cascade():
    asyncio.run(_test_delay_within_float_does_not_cascade())


def test_delay_exceeding_gap_cascades_only_overflow():
    asyncio.run(_test_delay_exceeding_gap_cascades_only_overflow())


def test_delay_on_critical_path_cascades_fully():
    asyncio.run(_test_delay_on_critical_path_cascades_fully())


def test_delay_partially_absorbed_then_cascades():
    asyncio.run(_test_delay_partially_absorbed_then_cascades())


def test_two_predecessors_only_binding_one_cascades():
    asyncio.run(_test_two_predecessors_only_binding_one_cascades())


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
        # CPM Part J-7 (2026-05-15) -- RFI impact-days regression.
        ("test_rfi_impact_pushes_slip_and_impacted_finish",
         _test_rfi_impact_pushes_slip_and_impacted_finish),
        ("test_rfi_contributes_to_delay_remaining_float",
         _test_rfi_contributes_to_delay_remaining_float),
        ("test_rfi_resolved_excluded_from_future_calcs",
         _test_rfi_resolved_excluded_from_future_calcs),
        ("test_rfi_edit_self_excluded_from_prior_sum",
         _test_rfi_edit_self_excluded_from_prior_sum),
        ("test_mixed_rfi_delay_synthetic_qc_cumulative",
         _test_mixed_rfi_delay_synthetic_qc_cumulative),
        ("test_rfi_no_impact_days_zero_propagation",
         _test_rfi_no_impact_days_zero_propagation),
        # CPM Part K-7 (2026-05-15) -- Submittal impact-days regression.
        ("test_submittal_auto_suggest_on_sla_breach",
         _test_submittal_auto_suggest_on_sla_breach),
        ("test_submittal_super_override_preserved_across_re_breach",
         _test_submittal_super_override_preserved_across_re_breach),
        ("test_submittal_contributes_to_delay_remaining_float",
         _test_submittal_contributes_to_delay_remaining_float),
        ("test_submittal_approved_excluded_from_future_calcs",
         _test_submittal_approved_excluded_from_future_calcs),
        ("test_mixed_all_four_event_types_cumulative",
         _test_mixed_all_four_event_types_cumulative),
        ("test_submittal_empty_impacts_no_propagation",
         _test_submittal_empty_impacts_no_propagation),
        # CPM Part L (2026-05-16) -- Working-day math + live remaining float.
        ("test_working_day_fs_lag", _test_working_day_fs_lag),
        ("test_working_day_fs_lag_with_lag",
         _test_working_day_fs_lag_with_lag),
        ("test_working_day_duration_skips_weekend",
         _test_working_day_duration_skips_weekend),
        ("test_remaining_float_drops_on_delay_create",
         _test_remaining_float_drops_on_delay_create),
        ("test_remaining_float_restored_on_delay_resolve",
         _test_remaining_float_restored_on_delay_resolve),
        ("test_remaining_float_multi_event_cumulative",
         _test_remaining_float_multi_event_cumulative),
        ("test_actual_impact_days_calculation",
         _test_actual_impact_days_calculation),
        # CPM Slip-cascade float-aware fix (2026-05-16).
        ("test_delay_within_float_does_not_cascade",
         _test_delay_within_float_does_not_cascade),
        ("test_delay_exceeding_gap_cascades_only_overflow",
         _test_delay_exceeding_gap_cascades_only_overflow),
        ("test_delay_on_critical_path_cascades_fully",
         _test_delay_on_critical_path_cascades_fully),
        ("test_delay_partially_absorbed_then_cascades",
         _test_delay_partially_absorbed_then_cascades),
        ("test_two_predecessors_only_binding_one_cascades",
         _test_two_predecessors_only_binding_one_cascades),
    ]
    passes = 0
    fails: list[tuple[str, str]] = []
    print("=" * 78)
    print("CPM CORRECTNESS REGRESSION SUITE -- Parts H+I+J+K+L "
          "+ Slip-cascade fix (11 + 5 + 6 + 6 + 7 + 5 tests)")
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
    print(f"OVERALL: {passes} PASS / {len(fails)} FAIL  (of 40 spec tests)")
    print("=" * 78)
    if fails:
        for n, e in fails:
            print(f"  - {n}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
