"""MS Project file pipeline (2026-07-02) -- endpoint + engine-lock tests.

Covers the hosted-.mpp contract:
  * upload endpoint: mode2 gate, extension/empty/size validation, rev
    increments, auto-first-sync (push with tasks / pull without)
  * download + generate-file endpoints
  * worker file endpoints: claimed-job gate, sha verification, versioning
  * engine lock + mode2/impact-days pair rule on project create/update
  * feature_flags.progress_recompute_enabled_for truth table
  * extended sync-state payload (file / active_job / worker_online)

Same in-memory Mongo stub pattern as test_ms_project_sync (module-level
sys.modules['database'] stub; asyncio.run per call). services.storage is
monkeypatched wholesale -- these tests never hit the network. Endpoint
functions are called directly with a fake ctx (the auth dependency is unit-
tested elsewhere).
"""
import asyncio
import hashlib
import io
import os
import sys
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Satisfy `from database import ...` without motor installed. Another test
# module (test_ms_project_sync) may have installed its own stub already --
# keep it; the fixture below rebinds get_db on every module under test anyway.
if "database" not in sys.modules:
    _dbmod = types.ModuleType("database")
    _dbmod.get_db = lambda: None
    _dbmod.tenant_filter = lambda tid, q=None: {**(q or {}), "tenant_id": tid}
    sys.modules["database"] = _dbmod
if "audit" not in sys.modules:
    _auditmod = types.ModuleType("audit")
    async def _noop_audit(**k):
        return None
    _auditmod.write_audit = _noop_audit
    sys.modules["audit"] = _auditmod
# test_cpm_correctness installs a minimal auth stub (require_tenant only);
# routers.projects_router also imports can_manage_project_members. Fill the
# gap -- these tests call endpoints directly with a fake ctx, so the auth
# dependency itself is never exercised.
_auth = sys.modules.get("auth")
if _auth is not None and not hasattr(_auth, "can_manage_project_members"):
    _auth.can_manage_project_members = lambda role: role in ("admin", "pm")


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
    async def update_many(self, q, ops):
        n = 0
        for r in self.rows:
            if _match(r, q or {}):
                r.update(ops.get("$set", {}))
                for k, inc in ops.get("$inc", {}).items():
                    r[k] = r.get(k, 0) + inc
                n += 1
        return types.SimpleNamespace(matched_count=n, modified_count=n)
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
        self.tasks = _Col(); self.projects = _Col(); self.files = _Col()
        self.schedule_jobs = _Col(); self.schedule_job_seq = _Col()
        self.schedule_job_watermark = _Col(); self.audit_log = _Col()
    def __getattr__(self, n):
        if n.startswith("_"):
            raise AttributeError(n)
        c = _Col(); object.__setattr__(self, n, c); return c
    def __getitem__(self, n):
        return getattr(self, n)


_STUB = _DB()

from fastapi import HTTPException, UploadFile  # noqa: E402
from models import ProjectCreate, ProjectUpdate  # noqa: E402
from services import feature_flags as ff  # noqa: E402
from services import ms_project_sync as mps  # noqa: E402
from services import runtime_settings as rs  # noqa: E402
from services import storage  # noqa: E402
import routers.ms_project_internal_router as mir  # noqa: E402
import routers.projects_router as pr  # noqa: E402

T, P = "t1", "p1"
CTX = {"tenant_id": T, "user": {"id": "u1", "role": "admin", "email": "a@b.c"},
       "request_id": "req-1"}

# ----- fake object storage (spec: never hit the network) -----
_OBJECTS: dict = {}
_AUDITS: list = []
_FLAG_CALLS: list = []


def _fake_make_path(tenant_id, kind, ext):
    return f"test/{kind}/{tenant_id}/obj{len(_OBJECTS)}.{ext}"


def _fake_put_object(path, data, content_type):
    _OBJECTS[path] = (bytes(data), content_type)
    return {"ok": True}


def _fake_get_object(path):
    data, ct = _OBJECTS[path]
    return data, ct


async def _fake_record_file(*, tenant_id, storage_path, original_filename,
                            content_type, size, kind, actor_id=None):
    doc = {"id": f"file-{len(_STUB.files.rows) + 1}", "tenant_id": tenant_id,
           "storage_path": storage_path, "original_filename": original_filename,
           "content_type": content_type, "size": size, "kind": kind,
           "is_deleted": False, "uploaded_by": actor_id,
           "created_at": "2026-07-02T00:00:00+00:00"}
    await _STUB.files.insert_one(doc)
    return doc


async def _record_audit(**kw):
    _AUDITS.append(kw)


async def _record_flag_on(name, updated_by=""):
    _FLAG_CALLS.append(name)


class _BG:
    def __init__(self): self.calls = []
    def add_task(self, fn, *a, **k): self.calls.append((fn, a, k))


class _Req:
    """Minimal stand-in for fastapi.Request in the raw-bytes worker POST."""
    def __init__(self, body): self._body = body
    async def body(self): return self._body


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=name)


def _seed_project(**over):
    doc = {"id": P, "tenant_id": T, "name": "Alpha", "schedule_mode": "mode2",
           "schedule_engine": "impact_days",
           "created_at": "2026-07-01T00:00:00+00:00"}
    doc.update(over)
    _STUB.projects.rows.append(doc)
    return doc


def _seed_task(code="A"):
    _STUB.tasks.rows.append({
        "id": f"task-{code}", "tenant_id": T, "project_id": P,
        "activity_code": code, "description": code, "status": "in_progress",
        "planned_start": "2026-01-05", "planned_finish": "2026-01-09",
        "predecessors": []})


def _project_row():
    return next(r for r in _STUB.projects.rows if r["id"] == P)


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    for col in list(_STUB.__dict__.values()):
        if isinstance(col, _Col):
            col.rows.clear()
    _OBJECTS.clear(); _AUDITS.clear(); _FLAG_CALLS.clear()
    # Rebind get_db on every module under test: a sibling test module may have
    # imported them first against ITS own stub.
    for mod in (mps, mir, pr):
        monkeypatch.setattr(mod, "get_db", lambda: _STUB)
    monkeypatch.setattr(mir, "write_audit", _record_audit)
    monkeypatch.setattr(pr, "write_audit", _record_audit)
    monkeypatch.setattr(storage, "make_path", _fake_make_path)
    monkeypatch.setattr(storage, "put_object", _fake_put_object)
    monkeypatch.setattr(storage, "get_object", _fake_get_object)
    monkeypatch.setattr(storage, "record_file", _fake_record_file)
    monkeypatch.setattr(rs, "ensure_flag_on", _record_flag_on)
    rs.reset_cache_for_tests()
    yield
    rs.reset_cache_for_tests()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Upload endpoint.
# --------------------------------------------------------------------------
def test_upload_409_when_project_not_mode2():
    _seed_project(schedule_mode="mode1")
    with pytest.raises(HTTPException) as e:
        run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"x"), CTX))
    assert e.value.status_code == 409


def test_upload_400_on_wrong_extension_and_empty_file():
    _seed_project()
    with pytest.raises(HTTPException) as e:
        run(mir.upload_ms_project_file(P, _upload("plan.xer", b"x"), CTX))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        run(mir.upload_ms_project_file(P, _upload("plan.mpp", b""), CTX))
    assert e.value.status_code == 400


def test_upload_413_over_size_cap(monkeypatch):
    _seed_project()
    monkeypatch.setattr(mir, "MSP_MAX_BYTES", 10)
    with pytest.raises(HTTPException) as e:
        run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"x" * 11), CTX))
    assert e.value.status_code == 413


def test_upload_413_on_content_length_precheck():
    # A declared Content-Length over the cap is refused before the body is
    # read (the tiny actual body here would otherwise pass the post-read check).
    _seed_project()
    with pytest.raises(HTTPException) as e:
        run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"x"), CTX,
                                       content_length=mir.MSP_MAX_BYTES + 1))
    assert e.value.status_code == 413
    assert _STUB.files.rows == []  # nothing stored


def test_worker_post_413_on_content_length_precheck():
    _seed_project()
    _seed_claimed_job("j1")
    with pytest.raises(HTTPException) as e:
        run(mir.worker_upload_file("j1", _Req(b"x"), x_file_sha=_sha12(b"x"),
                                   _w={"worker": True},
                                   content_length=mir.MSP_MAX_BYTES + 1))
    assert e.value.status_code == 413
    assert _STUB.files.rows == []


def test_store_rev_allocation_is_atomic_under_concurrent_snapshots():
    # Two "concurrent" stores that both read the SAME stale project doc must
    # still allocate monotonic revs (1 then 2): the rev comes from an atomic
    # $inc on the project row, never from the caller's snapshot.
    _seed_project()
    stale_snapshot = dict(_project_row())  # neither store sees the other's write
    p1 = run(mir._store_msp_version(_STUB, T, stale_snapshot, b"v1",
                                    filename="plan.mpp", source="upload"))
    p2 = run(mir._store_msp_version(_STUB, T, stale_snapshot, b"v2",
                                    filename="plan.mpp", source="upload"))
    assert p1["ms_project_file_rev"] == 1 and p2["ms_project_file_rev"] == 2
    proj = _project_row()
    # the pointer never moves backwards: it ends on the LAST store
    assert proj["ms_project_file_rev"] == 2
    assert proj["ms_project_file_id"] == p2["ms_project_file_id"]
    assert proj["ms_project_file_sha"] == _sha12(b"v2")
    assert sorted(r["rev"] for r in _STUB.files.rows) == [1, 2]  # no duplicate revs


def test_upload_supersedes_pending_create_jobs():
    # A queued (not-yet-claimed) generation must never displace a user upload
    # later: the upload flips pending create jobs to error NOW.
    _seed_project(); _seed_task("A")
    gen = run(mir.generate_ms_project_file(P, CTX))
    out = run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v1"), CTX))
    assert out["file"]["rev"] == 1
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == gen["job_id"])
    assert job["status"] == "error"
    assert job["result_status"] == "superseded_by_upload"
    assert job["error"]["code"] == "superseded_by_upload"
    assert job["finished_at"]
    # claimed jobs are NOT touched (they're fenced at the worker file POST)
    _seed_claimed_job("j-claimed", op="create", status="claimed")
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v2"), CTX))
    claimed = next(j for j in _STUB.schedule_jobs.rows if j["id"] == "j-claimed")
    assert claimed["status"] == "claimed"


def test_upload_stores_v1_and_auto_enqueues_pull_without_tasks():
    _seed_project()
    body = b"MPP-BYTES-1"
    out = run(mir.upload_ms_project_file(P, _upload("Plan.mpp", body), CTX))
    assert out["file"]["rev"] == 1 and out["file"]["name"] == "Plan.mpp"
    assert out["file"]["source"] == "upload"
    # no tasks yet -> the file's network becomes the schedule via a PULL
    assert out["auto_job"]["op"] == "pull"
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == out["auto_job"]["job_id"])
    assert job["op"] == "pull" and job["status"] == "pending"
    # project pointer + sha stored
    proj = _project_row()
    assert proj["ms_project_file_rev"] == 1
    assert proj["ms_project_file_sha"] == _sha12(body)
    assert proj["ms_project_file_source"] == "upload"
    # files row got the follow-up version metadata
    row = next(r for r in _STUB.files.rows if r["id"] == proj["ms_project_file_id"])
    assert row["kind"] == "msp" and row["sha12"] == _sha12(body)
    assert row["project_id"] == P and row["rev"] == 1 and row["source"] == "upload"
    # bytes actually landed in (fake) object storage
    assert _OBJECTS[row["storage_path"]][0] == body


def test_upload_rev_increments_and_auto_enqueues_push_with_tasks():
    _seed_project(); _seed_task("A")
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v1"), CTX))
    out = run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v2"), CTX))
    assert out["file"]["rev"] == 2
    assert out["auto_job"]["op"] == "push"  # has tasks -> push ICE actuals
    proj = _project_row()
    assert proj["ms_project_file_rev"] == 2 and proj["ms_project_file_sha"] == _sha12(b"v2")
    # every version is a NEW files row -- never overwritten
    revs = sorted(r["rev"] for r in _STUB.files.rows if r["kind"] == "msp")
    assert revs == [1, 2]
    # audit rows carry before/after of the pointer fields
    assert len(_AUDITS) == 2
    assert _AUDITS[1]["table_name"] == "projects" and _AUDITS[1]["op"] == "UPDATE"
    assert _AUDITS[1]["before"]["ms_project_file_rev"] == 1
    assert _AUDITS[1]["after"]["ms_project_file_rev"] == 2


def test_download_streams_current_version():
    _seed_project()
    with pytest.raises(HTTPException) as e:  # nothing stored yet
        run(mir.download_ms_project_file(P, CTX))
    assert e.value.status_code == 404
    run(mir.upload_ms_project_file(P, _upload("Alpha Plan.mpp", b"MPP-DATA"), CTX))
    resp = run(mir.download_ms_project_file(P, CTX))
    assert resp.body == b"MPP-DATA"
    assert resp.headers["content-type"] == "application/vnd.ms-project"
    assert 'filename="Alpha Plan.mpp"' in resp.headers["content-disposition"]


def test_download_filename_sanitized_for_header_safety():
    # Response headers are latin-1: a non-ASCII stored name must fall back to
    # a safe default instead of 500ing; quotes/control chars are stripped so
    # they can't break out of (or inject into) the header value.
    _seed_project()
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"D"), CTX))
    _project_row()["ms_project_file_name"] = "計画書.mpp"  # non-latin-1
    resp = run(mir.download_ms_project_file(P, CTX))
    assert resp.body == b"D"
    assert 'filename="schedule.mpp"' in resp.headers["content-disposition"]
    _project_row()["ms_project_file_name"] = 'we"ird\r\nname.mpp'
    resp = run(mir.download_ms_project_file(P, CTX))
    assert 'filename="weirdname.mpp"' in resp.headers["content-disposition"]
    # the sanitizer itself: empty/control-only names get the fallback
    assert mir._download_filename("") == "schedule.mpp"
    assert mir._download_filename("\r\n\x00") == "schedule.mpp"
    assert mir._download_filename("  ok name.mpp  ") == "ok name.mpp"


# --------------------------------------------------------------------------
# Generate-file endpoint.
# --------------------------------------------------------------------------
def test_generate_file_guards_and_enqueue():
    _seed_project(schedule_mode="mode1")
    with pytest.raises(HTTPException) as e:  # not mode2
        run(mir.generate_ms_project_file(P, CTX))
    assert e.value.status_code == 409
    _project_row()["schedule_mode"] = "mode2"
    with pytest.raises(HTTPException) as e:  # no task with an activity code
        run(mir.generate_ms_project_file(P, CTX))
    assert e.value.status_code == 409
    _seed_task("A")
    out = run(mir.generate_ms_project_file(P, CTX))
    assert out["job_id"] and out["seq"] == 1
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == out["job_id"])
    assert job["op"] == "create" and job["status"] == "pending"
    # a stored file already exists -> generate is blocked
    _project_row()["ms_project_file_id"] = "file-x"
    with pytest.raises(HTTPException) as e:
        run(mir.generate_ms_project_file(P, CTX))
    assert e.value.status_code == 409


def test_generate_409_while_any_job_pending_or_claimed():
    # No duplicate create jobs: a second Generate while ANY job is in flight
    # (pending or claimed, any op) is refused.
    _seed_project(); _seed_task("A")
    first = run(mir.generate_ms_project_file(P, CTX))
    with pytest.raises(HTTPException) as e:
        run(mir.generate_ms_project_file(P, CTX))
    assert e.value.status_code == 409
    assert "already in progress" in e.value.detail
    # still blocked once the worker claims it
    job = next(j for j in _STUB.schedule_jobs.rows if j["id"] == first["job_id"])
    job["status"] = "claimed"
    with pytest.raises(HTTPException) as e:
        run(mir.generate_ms_project_file(P, CTX))
    assert e.value.status_code == 409
    # a finished job no longer blocks
    job["status"] = "error"
    out = run(mir.generate_ms_project_file(P, CTX))
    assert out["job_id"] != first["job_id"]
    assert sum(1 for j in _STUB.schedule_jobs.rows if j["op"] == "create") == 2


# --------------------------------------------------------------------------
# Worker file endpoints.
# --------------------------------------------------------------------------
def _seed_claimed_job(job_id="j1", op="push", status="claimed", project_file=None):
    _STUB.schedule_jobs.rows.append({
        "id": job_id, "tenant_id": T, "project_id": P, "seq": 1, "op": op,
        "status": status, "actor_id": "u1", "project_file": project_file})


def test_worker_get_requires_claimed_job_and_stored_file():
    _seed_project()
    _seed_claimed_job("j1", status="pending")
    with pytest.raises(HTTPException) as e:
        run(mir.worker_download_file("j1", _w={"worker": True}))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        run(mir.worker_download_file("nope", _w={"worker": True}))
    assert e.value.status_code == 404
    _seed_claimed_job("j2")
    with pytest.raises(HTTPException) as e:  # claimed but no stored file
        run(mir.worker_download_file("j2", _w={"worker": True}))
    assert e.value.status_code == 404


def test_worker_get_streams_bytes_with_sha_and_rev_headers():
    _seed_project()
    body = b"MPP-HOSTED"
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", body), CTX))
    _seed_claimed_job("j1")
    resp = run(mir.worker_download_file("j1", _w={"worker": True}))
    assert resp.body == body
    assert resp.headers["x-file-sha"] == _sha12(body)
    assert resp.headers["x-file-rev"] == "1"
    # 2026-07-03: the worker learns how this version got here (fallback for
    # jobs whose descriptor predates file_source) -- an 'upload' base makes
    # a push reset its in-file impact ledger first.
    assert resp.headers["x-file-source"] == "upload"


def test_worker_post_409_unclaimed_and_422_on_sha_mismatch():
    _seed_project()
    _seed_claimed_job("j1", status="pending")
    with pytest.raises(HTTPException) as e:
        run(mir.worker_upload_file("j1", _Req(b"data"),
                                   x_file_sha=_sha12(b"data"), _w={"worker": True}))
    assert e.value.status_code == 409
    _seed_claimed_job("j2")
    with pytest.raises(HTTPException) as e:
        run(mir.worker_upload_file("j2", _Req(b"data"),
                                   x_file_sha="deadbeef0000", _w={"worker": True}))
    assert e.value.status_code == 422
    assert _STUB.files.rows == []  # nothing stored


def test_worker_post_stores_new_version_with_job_id():
    _seed_project()
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v1"), CTX))
    _seed_claimed_job("j1", op="push")
    out = run(mir.worker_upload_file("j1", _Req(b"v2-from-worker"),
                                     x_file_sha=_sha12(b"v2-from-worker"),
                                     _w={"worker": True}))
    assert out == {"rev": 2}
    proj = _project_row()
    assert proj["ms_project_file_rev"] == 2
    assert proj["ms_project_file_sha"] == _sha12(b"v2-from-worker")
    assert proj["ms_project_file_source"] == "worker_push"
    assert proj["ms_project_file_name"] == "plan.mpp"  # original name kept
    row = next(r for r in _STUB.files.rows if r["id"] == proj["ms_project_file_id"])
    # job_id on the row = record_result's self-upload rev allowance
    assert row["job_id"] == "j1" and row["source"] == "worker_push" and row["rev"] == 2


def test_worker_post_create_job_records_source_generated():
    _seed_project()
    _seed_claimed_job("j1", op="create")
    out = run(mir.worker_upload_file("j1", _Req(b"generated"),
                                     x_file_sha=_sha12(b"generated"),
                                     _w={"worker": True}))
    assert out == {"rev": 1}
    proj = _project_row()
    assert proj["ms_project_file_source"] == "generated"
    assert proj["ms_project_file_name"] == "Alpha.mpp"  # derived from project name


def test_generated_rev1_store_never_stamps_adoption():
    # ADOPTION GATE (2026-07-03): a GENERATED file is built from ICE's
    # planned dates -- it contains zero impact history, so even as rev 1 it
    # must NOT stamp ms_project_file_adopted_at. Pre-generation delays keep
    # contributing in full on the next push (gate stays off).
    _seed_project()
    _seed_claimed_job("j1", op="create")
    run(mir.worker_upload_file("j1", _Req(b"generated"),
                               x_file_sha=_sha12(b"generated"),
                               _w={"worker": True}))
    assert _project_row().get("ms_project_file_adopted_at") is None


def test_upload_stamps_adoption_at_any_rev_worker_push_never():
    # ADOPTION GATE (2026-07-03): EVERY user upload re-baselines what the
    # file's plan already reflects, so the stamp moves on each upload (not
    # just rev 1); a worker_push version (ICE's own ledgered change) never
    # touches it.
    _seed_project()
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v1"), CTX))
    first = _project_row().get("ms_project_file_adopted_at")
    assert first  # rev 1 upload stamped
    # worker_push (rev 2) does not move the gate
    _seed_claimed_job("j1", op="push")
    run(mir.worker_upload_file("j1", _Req(b"v2"), x_file_sha=_sha12(b"v2"),
                               _w={"worker": True}))
    assert _project_row()["ms_project_file_adopted_at"] == first
    # a SECOND user upload (rev 3) re-baselines: the stamp moves forward
    out = run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v3"), CTX))
    assert out["file"]["rev"] == 3
    moved = _project_row()["ms_project_file_adopted_at"]
    assert moved != first and moved > first


def test_worker_post_409_create_fence_when_a_file_now_exists():
    # The create job was enqueued when NO file existed; a user uploaded one
    # while the worker generated -- the uploaded file wins, the worker's 409s.
    _seed_project()
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"user-upload"), CTX))
    _seed_claimed_job("j1", op="create")
    with pytest.raises(HTTPException) as e:
        run(mir.worker_upload_file("j1", _Req(b"generated"),
                                   x_file_sha=_sha12(b"generated"),
                                   _w={"worker": True}))
    assert e.value.status_code == 409
    assert "keeping the uploaded file" in e.value.detail
    proj = _project_row()  # pointer untouched: still the user's upload
    assert proj["ms_project_file_rev"] == 1
    assert proj["ms_project_file_sha"] == _sha12(b"user-upload")
    assert len(_STUB.files.rows) == 1  # no new version stored


def test_worker_post_409_push_fence_when_newer_version_uploaded():
    # The push job carried file base f1; a user uploaded v2 mid-job. The
    # worker's updated v1-based file must not clobber the newer upload.
    _seed_project()
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v1"), CTX))
    f1 = _project_row()["ms_project_file_id"]
    _seed_claimed_job("j1", op="push", project_file={"file_id": f1, "file_rev": 1})
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v2"), CTX))  # mid-job upload
    with pytest.raises(HTTPException) as e:
        run(mir.worker_upload_file("j1", _Req(b"worker-bytes"),
                                   x_file_sha=_sha12(b"worker-bytes"),
                                   _w={"worker": True}))
    assert e.value.status_code == 409
    assert "newer file version" in e.value.detail
    proj = _project_row()
    assert proj["ms_project_file_rev"] == 2
    assert proj["ms_project_file_sha"] == _sha12(b"v2")
    # same job, base still current -> the fence lets the worker through
    _seed_claimed_job("j2", op="push",
                      project_file={"file_id": proj["ms_project_file_id"], "file_rev": 2})
    out = run(mir.worker_upload_file("j2", _Req(b"worker-bytes"),
                                     x_file_sha=_sha12(b"worker-bytes"),
                                     _w={"worker": True}))
    assert out == {"rev": 3}


def test_renew_lease_heartbeats_worker_liveness(monkeypatch):
    # During a long job the worker renews instead of claiming -- the renew
    # endpoint must refresh the liveness stamp so the UI stays 'online'.
    _seed_project()
    _seed_claimed_job("j1", op="push")
    touched = []
    async def _touch(worker_id):
        touched.append(worker_id)
    monkeypatch.setattr(rs, "touch_worker", _touch)
    out = run(mir.renew({"job_id": "j1", "worker_id": "w9"}, _w={"worker": True}))
    assert out == {"renewed": True}
    assert touched == ["w9"]
    out = run(mir.renew({"job_id": "j1"}, _w={"worker": True}))  # id defaults
    assert out == {"renewed": True}
    assert touched == ["w9", "worker"]


# --------------------------------------------------------------------------
# Engine lock + pair rule (projects_router).
# --------------------------------------------------------------------------
def test_engine_lock_409_on_change():
    _seed_project(schedule_mode="mode2", schedule_engine="impact_days")
    with pytest.raises(HTTPException) as e:
        run(pr.update_project(P, ProjectUpdate(schedule_mode="mode1",
                                               schedule_engine="impact_days"),
                              _BG(), ctx=CTX))
    assert e.value.status_code == 409
    # enterprise-locked project can't move off the enterprise engine either
    _project_row().update({"schedule_mode": "mode1", "schedule_engine": "enterprise"})
    with pytest.raises(HTTPException) as e:
        run(pr.update_project(P, ProjectUpdate(schedule_mode="mode1",
                                               schedule_engine="impact_days"),
                              _BG(), ctx=CTX))
    assert e.value.status_code == 409


def test_engine_lock_same_value_writes_pass():
    # The dialog always sends BOTH fields on every save -- a same-value write
    # on a locked project must go through (unrelated edits keep working).
    _seed_project(schedule_mode="mode2", schedule_engine="impact_days")
    out = run(pr.update_project(P, ProjectUpdate(name="Renamed",
                                                 schedule_mode="mode2",
                                                 schedule_engine="impact_days"),
                                _BG(), ctx=CTX))
    assert out.name == "Renamed" and out.schedule_mode == "mode2"
    # locked doc that predates the schedule_engine field: default compares equal
    del _project_row()["schedule_engine"]
    out = run(pr.update_project(P, ProjectUpdate(schedule_mode="mode2",
                                                 schedule_engine="impact_days"),
                                _BG(), ctx=CTX))
    assert out.schedule_mode == "mode2"


def test_mode2_update_forces_impact_days_pair():
    _seed_project(schedule_mode="mode1", schedule_engine="impact_days")
    out = run(pr.update_project(P, ProjectUpdate(schedule_mode="mode2",
                                                 schedule_engine="enterprise"),
                                _BG(), ctx=CTX))
    # mode2 + enterprise must be impossible: the pair rule wins in the same $set
    assert out.schedule_mode == "mode2" and out.schedule_engine == "impact_days"
    assert "FF_SCHEDULE_MODE_2" in _FLAG_CALLS and "FF_ENTERPRISE_CPM" not in _FLAG_CALLS


def test_mode2_create_forces_impact_days_pair():
    out = run(pr.create_project(ProjectCreate(name="New", schedule_mode="mode2",
                                              schedule_engine="enterprise"), ctx=CTX))
    assert out.schedule_mode == "mode2" and out.schedule_engine == "impact_days"
    assert "FF_SCHEDULE_MODE_2" in _FLAG_CALLS and "FF_ENTERPRISE_CPM" not in _FLAG_CALLS


def test_first_enterprise_adoption_fires_background_recompute():
    _seed_project(schedule_mode="mode1", schedule_engine="impact_days")
    bg = _BG()
    run(pr.update_project(P, ProjectUpdate(schedule_engine="enterprise",
                                           schedule_mode="mode1"), bg, ctx=CTX))
    assert [fn.__name__ for fn, _a, _k in bg.calls] == ["auto_recompute_after_event"]
    assert bg.calls[0][1] == (T, P)
    assert "FF_ENTERPRISE_CPM" in _FLAG_CALLS
    # already-enterprise saves don't re-fire it
    bg2 = _BG()
    run(pr.update_project(P, ProjectUpdate(schedule_engine="enterprise",
                                           schedule_mode="mode1"), bg2, ctx=CTX))
    assert bg2.calls == []


def test_mode2_adoption_does_not_fire_recompute():
    # mode2's numbers arrive via the file-upload auto-sync, not a local run.
    _seed_project(schedule_mode="mode1", schedule_engine="impact_days")
    bg = _BG()
    run(pr.update_project(P, ProjectUpdate(schedule_mode="mode2",
                                           schedule_engine="impact_days"), bg, ctx=CTX))
    assert bg.calls == []


# --------------------------------------------------------------------------
# progress_recompute_enabled_for truth table.
# --------------------------------------------------------------------------
def test_progress_recompute_enabled_for_truth_table(monkeypatch):
    for var in ("FF_PROGRESS_CASCADE", "FF_SCHEDULE_MODE_2", "FF_ENTERPRISE_CPM"):
        monkeypatch.delenv(var, raising=False)
    plain = {}
    mode2 = {"schedule_mode": "mode2"}
    ent = {"schedule_engine": "enterprise"}
    # all flags off -> pre-B2 behavior for everyone
    assert ff.progress_recompute_enabled_for(plain) is False
    assert ff.progress_recompute_enabled_for(mode2) is False
    assert ff.progress_recompute_enabled_for(ent) is False
    # B2 flag covers every project (the impact-days linear cascade)
    monkeypatch.setenv("FF_PROGRESS_CASCADE", "1")
    assert ff.progress_recompute_enabled_for(plain) is True
    monkeypatch.delenv("FF_PROGRESS_CASCADE")
    # mode2 projects recompute natively -- but only their own
    monkeypatch.setenv("FF_SCHEDULE_MODE_2", "1")
    assert ff.progress_recompute_enabled_for(mode2) is True
    assert ff.progress_recompute_enabled_for(plain) is False
    assert ff.progress_recompute_enabled_for(ent) is False
    monkeypatch.delenv("FF_SCHEDULE_MODE_2")
    # enterprise projects likewise
    monkeypatch.setenv("FF_ENTERPRISE_CPM", "1")
    assert ff.progress_recompute_enabled_for(ent) is True
    assert ff.progress_recompute_enabled_for(plain) is False
    assert ff.progress_recompute_enabled_for(mode2) is False


# --------------------------------------------------------------------------
# Extended sync-state payload.
# --------------------------------------------------------------------------
def test_sync_state_carries_file_active_job_and_worker_liveness(monkeypatch):
    _seed_project()
    st = run(mir.sync_state(P, CTX))
    assert st["file"] is None and st["active_job"] is None
    assert st["worker_online"] is False  # no heartbeat recorded
    run(mir.upload_ms_project_file(P, _upload("plan.mpp", b"v1"), CTX))
    from datetime import datetime, timezone
    monkeypatch.setattr(rs, "_cache", {
        "msp_worker_last_seen": datetime.now(timezone.utc).isoformat(),
        "msp_worker_id": "w1"})
    st = run(mir.sync_state(P, CTX))
    assert st["file"]["name"] == "plan.mpp" and st["file"]["rev"] == 1
    assert st["file"]["source"] == "upload"
    # the auto-first-sync job is the active one
    assert st["active_job"]["op"] == "pull" and st["active_job"]["status"] == "pending"
    assert st["active_job"]["id"] and st["active_job"]["created_at"]
    assert st["worker_online"] is True
    # a stale heartbeat (>120s) reads offline
    monkeypatch.setattr(rs, "_cache", {
        "msp_worker_last_seen": "2026-07-01T00:00:00+00:00"})
    st = run(mir.sync_state(P, CTX))
    assert st["worker_online"] is False
