"""Railway move (2026-11-16): project scope on create endpoints + MS Project jobs.

Covers:
  * services.project_scope.require_project / require_project_if_given:
    own project passes, another company's or a made-up id is 404, a
    non-string id (e.g. a {"$ne": ...} object) is 404, None stays allowed
    where project_id is optional;
  * create endpoints (crew / equipment assignments, deliveries, calendars,
    marketplace postings, delays, safety, QC, photos, crews' home project,
    field suggestions, crew reallocation) refuse another company's project;
  * MS Project: trigger_pull and every enqueue_* refuse another company's
    project, a made-up id and a path-like id, and queue nothing;
  * marketplace list hides tenant_id / project_id / posted_by on other
    companies' postings, keeps them on the caller's own;
  * worker: resolve_project_file / cache_path / execute_job refuse ids that
    are not plain tokens and keep the exact same path for valid ids.

No network, no real Mongo: a small in-memory fake database stands in.
"""
import asyncio
import importlib.util
import os
import types

import pytest
from fastapi import HTTPException

from services import project_scope
from services import ms_project_sync as mps
from routers import (
    iter5_router, field_events_router, resources_router,
    resource_leveling_router, field_suggestions_router,
    ms_project_internal_router,
)

TID = "tenant-a"
OTHER = "tenant-b"
OWN_PID = "11111111-2222-3333-4444-555555555555"
FOREIGN_PID = "99999999-8888-7777-6666-555555555555"
CTX = {"tenant_id": TID, "user": {"id": "u1", "role": "admin"}, "request_id": "r1"}


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- fakes
def _match(doc, flt):
    for k, v in flt.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    async def to_list(self, n):
        return [dict(d) for d in self.docs]


class _Coll:
    def __init__(self):
        self.docs = []

    async def find_one(self, flt, proj=None, **kw):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    def find(self, flt=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt or {})])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def find_one_and_update(self, flt, upd, **kw):
        for d in self.docs:
            if _match(d, flt):
                break
        else:
            d = dict(flt)
            self.docs.append(d)
        for k, n in upd.get("$inc", {}).items():
            d[k] = d.get(k, 0) + n
        d.update(upd.get("$set", {}))
        return dict(d)

    async def update_one(self, flt, upd, upsert=False):
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set", {}))
                return
        if upsert:
            d = dict(flt)
            d.update(upd.get("$set", {}))
            self.docs.append(d)


class _DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.fixture
def db(monkeypatch):
    d = _DB()
    d.projects.docs.append({"id": OWN_PID, "tenant_id": TID, "name": "Own"})
    d.projects.docs.append({"id": FOREIGN_PID, "tenant_id": OTHER, "name": "Theirs"})
    for mod in (project_scope, mps, iter5_router, field_events_router,
                resources_router, resource_leveling_router,
                field_suggestions_router, ms_project_internal_router):
        monkeypatch.setattr(mod, "get_db", lambda: d)
    return d


def _assert_404(coro):
    with pytest.raises(HTTPException) as e:
        _run(coro)
    assert e.value.status_code == 404


# ------------------------------------------------------ the helper itself
def test_require_project_own_foreign_missing_and_odd_ids(db):
    assert _run(project_scope.require_project(CTX, OWN_PID))["id"] == OWN_PID
    _assert_404(project_scope.require_project(CTX, FOREIGN_PID))
    _assert_404(project_scope.require_project(CTX, "does-not-exist"))
    _assert_404(project_scope.require_project(CTX, {"$ne": None}))
    _assert_404(project_scope.require_project(CTX, ""))
    _assert_404(project_scope.require_project(CTX, None))


def test_require_project_if_given_keeps_none_allowed(db):
    assert _run(project_scope.require_project_if_given(CTX, None)) is None
    assert _run(project_scope.require_project_if_given(CTX, "")) is None
    assert _run(project_scope.require_project_if_given(CTX, OWN_PID))["id"] == OWN_PID
    _assert_404(project_scope.require_project_if_given(CTX, FOREIGN_PID))


# ------------------------------------------------------- create endpoints
def test_crew_and_equipment_assignments(db):
    ok = iter5_router.CrewAssignmentCreate(
        crew_id="c1", project_id=OWN_PID, start_date="2026-11-16", end_date="2026-11-20")
    assert _run(iter5_router.create_crew_assign(ok, CTX))["project_id"] == OWN_PID
    bad = iter5_router.CrewAssignmentCreate(
        crew_id="c1", project_id=FOREIGN_PID, start_date="2026-11-16", end_date="2026-11-20")
    _assert_404(iter5_router.create_crew_assign(bad, CTX))
    eq = iter5_router.EquipAssignmentCreate(
        equipment_id="e1", project_id=FOREIGN_PID, start_date="2026-11-16", end_date="2026-11-20")
    _assert_404(iter5_router.create_equip_assign(eq, CTX))
    assert len(db.crew_assignments.docs) == 1
    assert db.equipment_assignments.docs == []


def test_calendar_company_wide_still_allowed(db):
    company = iter5_router.CalendarCreate(name="Company")
    assert _run(iter5_router.create_cal(company, CTX))["project_id"] is None
    own = iter5_router.CalendarCreate(name="Own", project_id=OWN_PID)
    assert _run(iter5_router.create_cal(own, CTX))["project_id"] == OWN_PID
    foreign = iter5_router.CalendarCreate(name="Theirs", project_id=FOREIGN_PID)
    _assert_404(iter5_router.create_cal(foreign, CTX))
    assert len(db.calendars.docs) == 2


def test_marketplace_posting_project_optional(db):
    base = dict(posting_type="materials", title="T", description="D")
    p = _run(iter5_router.post_marketplace(iter5_router.MarketplacePostingCreate(**base), CTX))
    assert p["project_id"] is None
    _assert_404(iter5_router.post_marketplace(
        iter5_router.MarketplacePostingCreate(project_id=FOREIGN_PID, **base), CTX))


def test_field_events_and_delivery_refuse_foreign_project(db):
    # The check runs first, before any other validation, so only
    # project_id matters here.
    payload = types.SimpleNamespace(project_id=FOREIGN_PID)
    _assert_404(field_events_router.create_delay(payload, CTX))
    _assert_404(field_events_router.create_safety(payload, CTX))
    _assert_404(field_events_router.create_qc(payload, CTX))
    _assert_404(field_events_router.create_photo(payload, CTX))
    _assert_404(iter5_router.create_delivery(payload, CTX))
    assert db.delays.docs == [] and db.safety_events.docs == []
    assert db.qc_issues.docs == [] and db.photos.docs == [] and db.deliveries.docs == []


def test_crew_home_project(db):
    payload = types.SimpleNamespace(home_project_id=FOREIGN_PID, crew_code="X")
    _assert_404(resources_router.create_crew(payload, None, CTX))
    assert db.crews.docs == []


def test_field_suggestion_and_reallocation_refuse_foreign_project(db):
    fs_payload = types.SimpleNamespace(project_id=FOREIGN_PID, entity_type="task", entity_id="t1")
    _assert_404(field_suggestions_router.create_suggestion_endpoint(fs_payload, CTX))
    re_payload = types.SimpleNamespace(from_project_id=None, to_project_id=FOREIGN_PID)
    _assert_404(resource_leveling_router.commit_reallocation(re_payload, CTX))


# -------------------------------------------------------------- marketplace
def test_marketplace_list_hides_other_companies_ids(db):
    db.marketplace_postings.docs += [
        {"id": "m1", "tenant_id": TID, "project_id": OWN_PID, "posted_by": "u1",
         "visibility": "private", "title": "mine", "posting_type": "materials",
         "description": "d", "created_at": "2026-11-16"},
        {"id": "m2", "tenant_id": OTHER, "project_id": FOREIGN_PID, "posted_by": "u9",
         "visibility": "network", "title": "theirs", "posting_type": "materials",
         "description": "d", "created_at": "2026-11-15", "budget_low": 5},
    ]
    out = {d["id"]: d for d in _run(iter5_router.list_marketplace(CTX))}
    assert out["m1"]["tenant_id"] == TID and out["m1"]["posted_by"] == "u1"
    assert out["m1"]["project_id"] == OWN_PID
    theirs = out["m2"]
    for k in ("tenant_id", "project_id", "posted_by"):
        assert k not in theirs
    # Everything the marketplace page reads is still there.
    for k in ("id", "title", "description", "posting_type", "visibility",
              "created_at", "budget_low"):
        assert k in theirs
    # The stored record itself is untouched.
    assert db.marketplace_postings.docs[1]["tenant_id"] == OTHER


# --------------------------------------------------------------- MS Project
@pytest.mark.parametrize("pid", [FOREIGN_PID, "no-such-project", "../../etc", "..\\x"])
def test_trigger_pull_refuses_projects_outside_tenant(db, pid):
    _assert_404(ms_project_internal_router.trigger_pull(pid, CTX))
    assert db.schedule_jobs.docs == []


@pytest.mark.parametrize("fn", ["enqueue_push_job", "enqueue_pull_job", "enqueue_create_job"])
def test_every_enqueue_refuses_foreign_project(db, fn):
    _assert_404(getattr(mps, fn)(TID, FOREIGN_PID))
    assert db.schedule_jobs.docs == []


def test_trigger_pull_own_project_still_queues(db):
    out = _run(ms_project_internal_router.trigger_pull(OWN_PID, CTX))
    assert out["seq"] == 1
    assert db.schedule_jobs.docs[0]["project_id"] == OWN_PID


def test_trigger_pull_role_check_unchanged(db):
    ctx = {**CTX, "user": {"id": "u2", "role": "super"}}
    with pytest.raises(HTTPException) as e:
        _run(ms_project_internal_router.trigger_pull(OWN_PID, ctx))
    assert e.value.status_code == 403


# ------------------------------------------------------------------ worker
_WORKER_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "worker", "msp_worker.py"))


@pytest.fixture(scope="module")
def worker():
    spec = importlib.util.spec_from_file_location("_msp_worker_under_test", _WORKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_worker_resolves_valid_id_exactly_as_before(worker, tmp_path):
    cfg = {"project_dir": str(tmp_path), "cache_dir": str(tmp_path / "cache")}
    job = {"project_id": OWN_PID, "project_file": {"logical_id": OWN_PID}}
    assert worker.resolve_project_file(job, cfg) == os.path.join(str(tmp_path), f"{OWN_PID}.mpp")
    # An admin-pinned full path still wins, as before.
    pinned = {"project_file": {"expected_fullname": "C:\\Schedules\\x.mpp"}}
    assert worker.resolve_project_file(pinned, cfg) == "C:\\Schedules\\x.mpp"
    p = worker.cache_path(cfg, OWN_PID, "rev-3.mpp")
    assert p == os.path.join(str(tmp_path / "cache"), OWN_PID, "rev-3.mpp")
    assert worker.cache_path(cfg, "", "generated.mpp").endswith(os.path.join("project", "generated.mpp"))


@pytest.mark.parametrize("bad", ["../x", "..\\x", "a/b", "a\\b", "C:evil", "..", ".", "/abs", "x y"])
def test_worker_refuses_path_like_ids(worker, tmp_path, bad):
    cfg = {"project_dir": str(tmp_path), "cache_dir": str(tmp_path / "cache"),
           "worker_id": "w1"}
    job = {"id": "j1", "op": "pull", "project_id": bad, "project_file": {}}
    assert worker.resolve_project_file(job, cfg) is None
    with pytest.raises(ValueError):
        worker.cache_path(cfg, bad, "rev-1.mpp")
    out = worker.execute_job(job, cfg, api=None)
    assert out["status"] == "error" and out["error"]["code"] == "unsafe_project_id"
    assert not (tmp_path / "cache").exists()
