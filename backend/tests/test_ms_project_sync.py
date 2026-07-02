"""CPM Mode 2 — backend job queue + result ingest tests (2026-06-29).

In-memory Mongo stub (supports find_one_and_update with $inc/$set/sort/upsert/
return_document, plus find/sort/update_one) exercising the real
services.ms_project_sync against seeded tasks. No network, no Project.
"""
import asyncio
import os
import sys
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _match(row, q):
    for k, v in (q or {}).items():
        if k == "_id":
            continue
        rv = row.get(k)
        if isinstance(v, dict):
            if "$ne" in v and rv == v["$ne"]:
                return False
            if "$lt" in v and not (rv is not None and rv < v["$lt"]):
                return False
            if "$in" in v and rv not in v["$in"]:
                return False
        elif rv != v:
            return False
    return True


class _Cur:
    def __init__(self, items): self.items = items
    def sort(self, key, direction=1):
        if isinstance(key, list):
            key, direction = key[0]
        self.items.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                        reverse=(direction < 0))
        return self
    async def to_list(self, n): return self.items[:n] if n else self.items


class _Col:
    def __init__(self): self.rows = []
    def find(self, q=None, p=None):
        return _Cur([dict(r) for r in self.rows if _match(r, q or {})])
    async def find_one(self, q=None, p=None, sort=None):
        rows = [r for r in self.rows if _match(r, q or {})]
        if sort:
            k, d = sort[0]
            rows.sort(key=lambda r: (r.get(k) is None, r.get(k)), reverse=(d < 0))
        return dict(rows[0]) if rows else None
    async def insert_one(self, doc): self.rows.append(dict(doc)); return types.SimpleNamespace(inserted_id=doc.get("id"))
    async def update_one(self, q, ops, upsert=False):
        for r in self.rows:
            if _match(r, q or {}):
                r.update(ops.get("$set", {}))
                for k, inc in ops.get("$inc", {}).items():
                    r[k] = r.get(k, 0) + inc
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            doc = dict(q); doc.update(ops.get("$set", {}))
            for k, inc in ops.get("$inc", {}).items():
                doc[k] = inc
            self.rows.append(doc)
            return types.SimpleNamespace(matched_count=0, modified_count=0, upserted_id=1)
        return types.SimpleNamespace(matched_count=0, modified_count=0)
    async def find_one_and_update(self, q, ops, upsert=False, return_document=False, sort=None):
        rows = [r for r in self.rows if _match(r, q or {})]
        if sort:
            k, d = sort[0]
            rows.sort(key=lambda r: (r.get(k) is None, r.get(k)), reverse=(d < 0))
        if rows:
            r = rows[0]
            for k, inc in ops.get("$inc", {}).items():
                r[k] = r.get(k, 0) + inc
            r.update(ops.get("$set", {}))
            return dict(r) if return_document else None
        if upsert:
            doc = dict(q)
            for k, inc in ops.get("$inc", {}).items():
                doc[k] = inc
            doc.update(ops.get("$set", {}))
            self.rows.append(doc)
            return dict(doc) if return_document else None
        return None


class _DB:
    def __init__(self):
        self.tasks = _Col(); self.projects = _Col()
        self.schedule_jobs = _Col(); self.schedule_job_seq = _Col()
        self.schedule_job_watermark = _Col(); self.audit_log = _Col()
    def __getattr__(self, n):
        if n.startswith("_"):
            raise AttributeError(n)
        c = _Col(); object.__setattr__(self, n, c); return c


_STUB = _DB()
_dbmod = types.ModuleType("database")
_dbmod.get_db = lambda: _STUB
_dbmod.tenant_filter = lambda tid, q=None: {**(q or {}), "tenant_id": tid}
sys.modules["database"] = _dbmod
_audit = types.ModuleType("audit")
async def _noop(**k): return None
_audit.write_audit = _noop
sys.modules["audit"] = _audit

from services import ms_project_sync as mps  # noqa: E402
from services import runtime_settings as rs  # noqa: E402

# A sibling test module may have imported ms_project_sync first, binding its
# get_db to a DIFFERENT stub -- rebind so this module's _STUB always wins here.
mps.get_db = _dbmod.get_db

T, P = "t1", "p1"


@pytest.fixture(autouse=True)
def _mode2_connector_on(monkeypatch):
    """record_result's engine fence requires the GLOBAL FF_SCHEDULE_MODE_2
    switch on top of the project's schedule_mode='mode2' -- turn it on for
    every test in THIS module. monkeypatch restores the env after each test,
    so nothing leaks into other test files. The runtime-settings cache is
    emptied too (and restored) so a UI override can never shadow the env
    fallback these tests rely on."""
    monkeypatch.setattr(rs, "_cache", {})
    monkeypatch.setenv("FF_SCHEDULE_MODE_2", "1")


def _seed_project():
    # schedule_mode="mode2" matches reality (jobs only exist for mode2
    # projects) and keeps record_result's engine fence open.
    _STUB.projects.rows.append({"id": P, "tenant_id": T, "name": "Proj P1",
                                "ms_project_file": None, "schedule_mode": "mode2"})


def _seed_task(code, ps, pf):
    _STUB.tasks.rows.append({
        "id": f"task-{code}", "tenant_id": T, "project_id": P, "activity_code": code,
        "status": "in_progress", "pct_complete": 0,
        "planned_start": ps, "planned_finish": pf,
        "external_task_id": None,
    })


def _reset():
    for v in _STUB.__dict__.values():
        if isinstance(v, _Col):
            v.rows.clear()
    _seed_project()


def test_enqueue_assigns_monotonic_seq():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    j1 = asyncio.run(mps.enqueue_push_job(T, P, actor_id="u"))
    j2 = asyncio.run(mps.enqueue_push_job(T, P, actor_id="u"))
    assert j1["seq"] == 1 and j2["seq"] == 2
    jobs = _STUB.schedule_jobs.rows
    assert len(jobs) == 2 and all(j["status"] == "pending" for j in jobs)
    # change_set carries the task with its activity_code stamped to Text1
    cs = jobs[0]["change_set"]
    assert cs["task_updates"][0]["set_text_field"] == {"Text1": "A"}


def test_claim_is_atomic_and_ordered():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_push_job(T, P))
    asyncio.run(mps.enqueue_push_job(T, P))
    j = asyncio.run(mps.claim_next_job("w1", ["push"]))
    assert j["seq"] == 1 and j["status"] == "claimed" and j["worker_id"] == "w1"
    j2 = asyncio.run(mps.claim_next_job("w1", ["push"]))
    assert j2["seq"] == 2
    assert asyncio.run(mps.claim_next_job("w1", ["push"])) is None  # nothing left


def test_ingest_writes_projected_only_when_moved():
    _reset()
    _seed_task("A", "2026-01-05", "2026-01-09")   # will MOVE
    _seed_task("B", "2026-01-05", "2026-01-09")   # stays on plan
    job = asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    result = {
        "job_id": claimed["id"], "seq": claimed["seq"], "worker_id": "w1", "status": "ok",
        "project_rowversion": "rv1", "project_finish": "2026-01-14", "critical_count": 1,
        "tasks": [
            {"text1": "A", "unique_id": 11, "start": "2026-01-07", "finish": "2026-01-14",
             "critical": True, "total_slack_days": 0.0},
            {"text1": "B", "unique_id": 12, "start": "2026-01-05", "finish": "2026-01-09",
             "critical": False, "total_slack_days": 3.0},
        ],
    }
    out = asyncio.run(mps.record_result(result))
    assert out["ok"] and out["ingested"] == 2
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    b = next(t for t in _STUB.tasks.rows if t["activity_code"] == "B")
    # A moved -> projected_* set, slip computed, critical from Project, ext id captured
    assert a["projected_finish_date"] == "2026-01-14"
    assert a["slip_days"] is not None and a["slip_days"] > 0
    assert a["is_critical_path"] is True
    assert a["external_task_id"] == 11 and a["external_system"] == "ms_project"
    # B unmoved -> projected_* cleared to None (null-means-unmoved invariant)
    assert b["projected_finish_date"] is None and b["slip_days"] is None
    assert b["is_critical_path"] is False
    # job finalized + watermark advanced
    job_doc = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job_doc["status"] == "done"
    wm = _STUB.schedule_job_watermark.rows[0]
    assert wm["last_ingested_seq"] == claimed["seq"]


def test_stale_result_is_skipped():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    # watermark already at seq 5
    _STUB.schedule_job_watermark.rows.append({"tenant_id": T, "project_id": P, "last_ingested_seq": 5})
    asyncio.run(mps.enqueue_push_job(T, P))  # seq 1
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-07",
                   "finish": "2026-01-20", "critical": True, "total_slack_days": 0}],
    }))
    assert out["reason"] == "stale_skipped"
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a.get("projected_finish_date") is None  # not ingested


def test_worker_error_recorded_no_ingest():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "error",
        "error": {"code": "ProjectError", "message": "modal dialog"},
    }))
    assert out["reason"] == "worker_error_recorded"
    job_doc = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job_doc["status"] == "error"


def test_reaper_requeues_then_parks():
    _reset()
    # a claimed job with an expired lease, attempt just under max
    _STUB.schedule_jobs.rows.append({
        "id": "stuck1", "tenant_id": T, "project_id": P, "seq": 1, "op": "push",
        "status": "claimed", "lease_expires_at": "2000-01-01T00:00:00",
        "attempt": 1, "worker_id": "w1"})
    n = asyncio.run(mps.reap_expired_leases())
    assert n == 1
    j = next(x for x in _STUB.schedule_jobs.rows if x["id"] == "stuck1")
    assert j["status"] == "pending" and j["worker_id"] is None
    # now at max attempts -> park
    j["status"] = "claimed"; j["lease_expires_at"] = "2000-01-01T00:00:00"; j["attempt"] = mps.MAX_ATTEMPTS
    asyncio.run(mps.reap_expired_leases())
    j = next(x for x in _STUB.schedule_jobs.rows if x["id"] == "stuck1")
    assert j["status"] == "parked"


def test_ms_project_engine_recompute_enqueues_queued_ack():
    """The Mode-2 engine (selected by get_engine_for for schedule_mode='mode2')
    enqueues a PUSH job and returns a {status:'queued'} ack with the job_id/seq,
    instead of running our internal CPM chain. (Uses the stub DB installed for
    this module.)"""
    from services.schedule_engine import get_engine_for, MsProjectEngine
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    # get_engine_for now gates the mode2 branch on FF_SCHEDULE_MODE_2 (so the
    # MS-Project and enterprise schedule features are isolated). Turn it on for
    # this selection and restore afterward so the module stays env-clean.
    _prev = os.environ.get("FF_SCHEDULE_MODE_2")
    os.environ["FF_SCHEDULE_MODE_2"] = "1"
    try:
        eng = get_engine_for({"schedule_mode": "mode2"})
        assert isinstance(eng, MsProjectEngine)
    finally:
        if _prev is None:
            os.environ.pop("FF_SCHEDULE_MODE_2", None)
        else:
            os.environ["FF_SCHEDULE_MODE_2"] = _prev
    out = asyncio.run(eng.recompute(T, P, actor_id="u", request_id="r"))
    assert out["status"] == "queued"
    assert out["job_id"] and out["seq"] == 1
    # a real pending job now exists for the worker to claim
    assert any(j["status"] == "pending" and j["op"] == "push"
               for j in _STUB.schedule_jobs.rows)


def test_pull_adapter_maps_text1_and_dependencies():
    from services.ms_project_ingest import worker_tasks_to_parsed_rows
    rows = worker_tasks_to_parsed_rows(
        [{"text1": "A", "name": "Pour", "start": "2026-01-05", "finish": "2026-01-09",
          "duration_days": 5, "milestone": False},
         {"text1": "B", "name": "Cure", "start": "2026-01-12", "finish": "2026-01-14",
          "duration_days": 3, "milestone": False}],
        [{"successor_code": "B", "predecessor_code": "A", "type": "SS", "lag_days": 2}])
    by = {r["activity_code"]: r for r in rows}
    assert by["A"]["planned_start"] == "2026-01-05" and by["A"]["predecessors"] == []
    assert by["B"]["predecessors"] == [{"code": "A", "type": "SS", "lag_days": 2}]


def test_pull_ingests_dates_and_builds_proposal():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_pull_job(T, P, actor_id="u"))
    claimed = asyncio.run(mps.claim_next_job("w1", ["pull"]))
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "project_rowversion": "rv2", "project_finish": "2026-02-05", "critical_count": 1,
        "tasks": [
            {"text1": "A", "unique_id": 1, "start": "2026-01-07", "finish": "2026-01-14",
             "critical": True, "total_slack_days": 0},
            {"text1": "Z", "unique_id": 9, "name": "New Z", "start": "2026-02-01",
             "finish": "2026-02-05", "critical": False, "total_slack_days": 5},
        ],
        "dependencies": [{"successor_code": "Z", "predecessor_code": "A",
                          "type": "FS", "lag_days": 0}],
    }))
    assert out["ok"]
    # existing task A got Project's computed dates ingested (read-only)
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a["projected_finish_date"] == "2026-01-14" and a["external_task_id"] == 1
    # a reviewable proposal was created from the structural diff (Z is added)
    props = [p for p in _STUB.schedule_proposals.rows if p["source"] == "ms_project_pull"]
    assert len(props) == 1 and props[0]["status"] == "pending_review"
    added_codes = [r.get("activity_code") for r in props[0]["diff"].get("added", [])]
    assert "Z" in added_codes
    # sync-state rowversion recorded, no conflict
    st = asyncio.run(mps.get_sync_state(T, P))
    assert st["last_rowversion"] == "rv2" and st["conflict"] is False


def test_conflict_sets_sync_state_and_skips_ingest():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "conflict",
        "project_rowversion": "rvX"}))
    assert out["reason"] == "conflict_recorded"
    st = asyncio.run(mps.get_sync_state(T, P))
    assert st["conflict"] is True and st["conflict_at"]
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a.get("projected_finish_date") is None  # not ingested
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job["status"] == "conflict"


def test_duplicate_conflict_postback_is_idempotent():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    asyncio.run(mps.record_result({"job_id": claimed["id"], "seq": claimed["seq"],
                                    "status": "conflict", "project_rowversion": "rvX"}))
    first_at = asyncio.run(mps.get_sync_state(T, P))["conflict_at"]
    # replay the same conflict postback -> no-op (conflict_at must not jitter)
    out = asyncio.run(mps.record_result({"job_id": claimed["id"], "seq": claimed["seq"],
                                         "status": "conflict", "project_rowversion": "rvX"}))
    assert out.get("idempotent") is True
    assert asyncio.run(mps.get_sync_state(T, P))["conflict_at"] == first_at


def test_expected_rowversion_carried_then_cleared_on_ok():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    # seed a standing rowversion + conflict
    _STUB.ms_project_sync_state.rows.append(
        {"tenant_id": T, "project_id": P, "last_rowversion": "rvOLD", "conflict": True})
    asyncio.run(mps.enqueue_push_job(T, P))
    job = _STUB.schedule_jobs.rows[-1]
    # the push job carries the last rowversion as the conflict basis
    assert job["project_file"]["expected_rowversion"] == "rvOLD"
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "project_rowversion": "rvNEW",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-05",
                   "finish": "2026-01-09", "critical": False, "total_slack_days": 2}]}))
    st = asyncio.run(mps.get_sync_state(T, P))
    assert st["last_rowversion"] == "rvNEW" and st["conflict"] is False  # cleared on success


def test_last_known_summary():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "project_finish": "2026-02-01", "critical_count": 4,
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-05",
                   "finish": "2026-01-09", "critical": False, "total_slack_days": 2}],
    }))
    s = asyncio.run(mps.last_known_summary(T, P))
    assert s and s["project_finish"] == "2026-02-01" and s["critical_count"] == 4


# --------------------------------------------------------------------------
# File pipeline (2026-07-02) -- create-job snapshot + ingest guards.
# --------------------------------------------------------------------------
def _project_row():
    return next(r for r in _STUB.projects.rows if r["id"] == P)


def test_enqueue_create_job_snapshot_shape():
    _reset()
    # Rich rows: dict + legacy bare-string predecessors, a milestone, and a
    # no-code task that must be skipped. Deliberately seeded out of date order.
    _STUB.tasks.rows.append({
        "id": "task-B", "tenant_id": T, "project_id": P, "activity_code": "B",
        "status": "in_progress", "description": "Cure", "duration_days": 3.0,
        "planned_start": "2026-01-12", "planned_finish": "2026-01-14",
        "is_milestone": False,
        "predecessors": [{"code": "A", "type": "SS", "lag_days": 2}]})
    _STUB.tasks.rows.append({
        "id": "task-A", "tenant_id": T, "project_id": P, "activity_code": "A",
        "status": "in_progress", "description": "Pour", "duration_days": None,
        "planned_start": "2026-01-05", "planned_finish": "2026-01-09",
        "is_milestone": False, "predecessors": ["0"]})  # legacy bare string
    _STUB.tasks.rows.append({
        "id": "task-M", "tenant_id": T, "project_id": P, "activity_code": "M",
        "status": "not_started", "description": None, "duration_days": 0,
        "planned_start": "2026-01-20", "planned_finish": "2026-01-20",
        "is_milestone": True, "predecessors": []})
    _STUB.tasks.rows.append({
        "id": "task-nocode", "tenant_id": T, "project_id": P, "activity_code": None,
        "status": "in_progress", "description": "orphan",
        "planned_start": "2026-01-02", "planned_finish": "2026-01-03"})
    out = asyncio.run(mps.enqueue_create_job(T, P, actor_id="u"))
    assert out["seq"] == 1
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == out["job_id"])
    assert job["op"] == "create" and job["status"] == "pending"
    cs = job["change_set"]
    # project header: name + start_date (no project start_date seeded -> min
    # planned_start of the included tasks)
    assert cs["project"] == {"name": "Proj P1", "start_date": "2026-01-05"}
    # no-code task skipped; rest ordered by planned_start
    assert [r["activity_code"] for r in cs["tasks"]] == ["A", "B", "M"]
    a, b, m = cs["tasks"]
    assert a["name"] == "Pour" and a["duration_days"] == 1  # None -> default 1
    assert a["predecessors"] == [{"code": "0", "type": "FS", "lag_days": 0}]
    assert b["duration_days"] == 3
    assert b["predecessors"] == [{"code": "A", "type": "SS", "lag_days": 2}]
    assert m["name"] == "M"  # description fallback
    assert m["is_milestone"] is True and m["duration_days"] == 0  # 0 stays 0


def test_create_result_ingests_like_push_readback():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_create_job(T, P, actor_id="u"))
    claimed = asyncio.run(mps.claim_next_job("w1", ["create"]))
    assert claimed["op"] == "create"
    # The worker uploaded the generated file BEFORE posting results, so the
    # CURRENT files row carries this job's id -- exactly what the create rev
    # guard requires for the ingest to be allowed.
    _seed_hosted_file("f-gen", 1, job_id=claimed["id"])
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "project_rowversion": "rev-1#abc",
        "tasks": [{"text1": "A", "unique_id": 7, "start": "2026-01-05",
                   "finish": "2026-01-09", "critical": False, "total_slack_days": 2}]}))
    assert out["ok"] and out["ingested"] == 1
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a["external_task_id"] == 7 and a["external_system"] == "ms_project"


def test_engine_fence_skips_result_when_project_left_mode2():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    _project_row()["schedule_mode"] = "mode1"  # engine changed mid-flight
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "project_rowversion": "rvX",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-07",
                   "finish": "2026-01-20", "critical": True, "total_slack_days": 0}]}))
    assert out == {"ok": True, "skipped": "engine_changed"}
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job["status"] == "done" and job["result_status"] == "skipped_engine_changed"
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a.get("projected_finish_date") is None  # not ingested
    assert _STUB.ms_project_sync_state.rows == []  # sync state untouched


def _seed_hosted_file(file_id, rev, *, job_id=None):
    proj = _project_row()
    proj["ms_project_file_id"] = file_id
    proj["ms_project_file_rev"] = rev
    proj["ms_project_file_sha"] = "a" * 12
    _STUB.files.rows.append({"id": file_id, "tenant_id": T, "project_id": P,
                             "rev": rev, "job_id": job_id})


def test_stale_file_rev_skips_ingest_and_reenqueues_push():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    _seed_hosted_file("f1", 1)
    asyncio.run(mps.enqueue_push_job(T, P))  # descriptor carries file_rev 1
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    assert claimed["project_file"]["file_id"] == "f1"
    assert claimed["project_file"]["file_rev"] == 1
    # A user re-uploads mid-job: rev bumps to 2 with NO job_id on the row.
    _seed_hosted_file("f2", 2)
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-07",
                   "finish": "2026-01-20", "critical": True, "total_slack_days": 0}]}))
    assert out["reason"] == "stale_file_rev" and out["requeued_job_id"]
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job["status"] == "done" and job["result_status"] == "stale_file_rev"
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a.get("projected_finish_date") is None  # not ingested
    # self-heal: a fresh push job is pending against the NEW file version
    fresh = next(j for j in _STUB.schedule_jobs.rows if j["id"] == out["requeued_job_id"])
    assert fresh["status"] == "pending" and fresh["op"] == "push"
    assert fresh["project_file"]["file_rev"] == 2


def test_self_uploaded_rev_bump_still_ingests():
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    _seed_hosted_file("f1", 1)
    asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    # The WORKER's own file upload during this job bumped the rev: the new
    # files row carries this job's id, so the rev guard must let it through.
    _seed_hosted_file("f2", 2, job_id=claimed["id"])
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "project_rowversion": "rev-2#bbbbbbbbbbbb",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-07",
                   "finish": "2026-01-14", "critical": True, "total_slack_days": 0}]}))
    assert out["ok"] and out["ingested"] == 1
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a["projected_finish_date"] == "2026-01-14"


def test_flag_off_skips_ingest_even_for_mode2_project(monkeypatch):
    # An admin turning the connector OFF in Settings (global kill-switch) must
    # stop ingest for ALL projects, even ones still marked schedule_mode=mode2.
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_push_job(T, P))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    monkeypatch.delenv("FF_SCHEDULE_MODE_2", raising=False)  # connector OFF
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "project_rowversion": "rvX",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-07",
                   "finish": "2026-01-20", "critical": True, "total_slack_days": 0}]}))
    assert out == {"ok": True, "skipped": "engine_changed"}
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job["status"] == "done" and job["result_status"] == "skipped_engine_changed"
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a.get("projected_finish_date") is None  # not ingested
    assert _STUB.ms_project_sync_state.rows == []  # sync state untouched


def test_create_result_stale_when_upload_won_and_not_reenqueued():
    # CREATE REV GUARD: a user upload became the current file mid-generation
    # (files row has NO job_id). The generated numbers describe a file the
    # project doesn't use -- skip the ingest, finalize done, and do NOT
    # re-enqueue (a real file exists; the upload queued its own first sync).
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_create_job(T, P, actor_id="u"))
    claimed = asyncio.run(mps.claim_next_job("w1", ["create"]))
    _seed_hosted_file("f-user", 1)  # user upload, no job_id on the row
    n_jobs = len(_STUB.schedule_jobs.rows)
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-07",
                   "finish": "2026-01-20", "critical": True, "total_slack_days": 0}]}))
    assert out == {"ok": True, "reason": "stale_file_rev"}
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job["status"] == "done" and job["result_status"] == "stale_file_rev"
    a = next(t for t in _STUB.tasks.rows if t["activity_code"] == "A")
    assert a.get("projected_finish_date") is None  # not ingested
    assert len(_STUB.schedule_jobs.rows) == n_jobs  # NO re-enqueue


def test_create_result_stale_when_no_file_stored_at_all():
    # Worker posted an ok create result but never uploaded the file (its file
    # POST failed permanently): nothing to ingest against -- stale, no requeue.
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_create_job(T, P, actor_id="u"))
    claimed = asyncio.run(mps.claim_next_job("w1", ["create"]))
    n_jobs = len(_STUB.schedule_jobs.rows)
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "ok",
        "tasks": [{"text1": "A", "unique_id": 1, "start": "2026-01-07",
                   "finish": "2026-01-20", "critical": True, "total_slack_days": 0}]}))
    assert out == {"ok": True, "reason": "stale_file_rev"}
    assert len(_STUB.schedule_jobs.rows) == n_jobs


def test_file_upload_rejected_push_error_reenqueues_fresh_push():
    # SELF-HEAL: the worker's file POST was fenced off (a user uploaded a
    # newer version mid-push) and it posted error code 'file_upload_rejected'.
    # After recording the error, a FRESH push job (descriptor = the CURRENT
    # file) is auto-enqueued so ICE's actuals land in the new file.
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    _seed_hosted_file("f1", 1)
    asyncio.run(mps.enqueue_push_job(T, P, actor_id="u"))
    claimed = asyncio.run(mps.claim_next_job("w1", ["push"]))
    _seed_hosted_file("f2", 2)  # the mid-job user upload
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "error",
        "error": {"code": "file_upload_rejected",
                  "message": "A newer file version was uploaded while this sync ran."}}))
    assert out["reason"] == "worker_error_recorded" and out["requeued_job_id"]
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == claimed["id"])
    assert job["status"] == "error"
    assert job["error"]["code"] == "file_upload_rejected"
    fresh = next(j for j in _STUB.schedule_jobs.rows if j["id"] == out["requeued_job_id"])
    assert fresh["status"] == "pending" and fresh["op"] == "push"
    assert fresh["project_file"]["file_id"] == "f2"  # fresh descriptor
    assert fresh["project_file"]["file_rev"] == 2
    assert fresh["actor_id"] == "u"


def test_file_upload_rejected_create_and_pull_not_reenqueued():
    # Create/pull with 'file_upload_rejected': the uploaded file made the job
    # moot -- record the error, nothing re-enqueued.
    _reset(); _seed_task("A", "2026-01-05", "2026-01-09")
    asyncio.run(mps.enqueue_create_job(T, P, actor_id="u"))
    claimed = asyncio.run(mps.claim_next_job("w1", ["create"]))
    n_jobs = len(_STUB.schedule_jobs.rows)
    out = asyncio.run(mps.record_result({
        "job_id": claimed["id"], "seq": claimed["seq"], "status": "error",
        "error": {"code": "file_upload_rejected", "message": "upload won"}}))
    assert out == {"ok": True, "reason": "worker_error_recorded"}
    assert len(_STUB.schedule_jobs.rows) == n_jobs
    asyncio.run(mps.enqueue_pull_job(T, P, actor_id="u"))
    claimed2 = asyncio.run(mps.claim_next_job("w1", ["pull"]))
    n_jobs = len(_STUB.schedule_jobs.rows)
    out2 = asyncio.run(mps.record_result({
        "job_id": claimed2["id"], "seq": claimed2["seq"], "status": "error",
        "error": {"code": "file_upload_rejected", "message": "upload won"}}))
    assert "requeued_job_id" not in out2
    assert len(_STUB.schedule_jobs.rows) == n_jobs
    # other worker error codes on a push don't self-heal either
    asyncio.run(mps.enqueue_push_job(T, P, actor_id="u"))
    claimed3 = asyncio.run(mps.claim_next_job("w1", ["push"]))
    n_jobs = len(_STUB.schedule_jobs.rows)
    out3 = asyncio.run(mps.record_result({
        "job_id": claimed3["id"], "seq": claimed3["seq"], "status": "error",
        "error": {"code": "ProjectError", "message": "modal dialog"}}))
    assert "requeued_job_id" not in out3
    assert len(_STUB.schedule_jobs.rows) == n_jobs
