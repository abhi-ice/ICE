"""Per-project access foundation (2026-11-16).

Covers services/project_access.py and the auth / team / project changes
built on it:
  * the role and access scope come from the ACTIVE company;
  * all-projects admins vs admins limited to specific projects vs every
    other role (assignment = any project_members row);
  * FF_PROJECT_ACCESS off = watch only (recorded, never refused);
    on = 404 for reads, 403 "You are not assigned to this project" for
    writes (a phone keeps a 403 save queued; a 404 would drop it);
  * the route-level dependency, including child records two hops away,
    and that it shares the sign-in lookup with the endpoint;
  * invites with projects, the member access editor and the
    last-all-projects-admin guard (409);
  * the creator of a project is assigned to it; the project list is
    filtered only when the switch is on;
  * the preflight report;
  * sign-in lockout per (account, address) + per account, and the voice PIN
    exchange (one-time use, per-address lock).

Offline: functions are called directly (or through a tiny FastAPI app) and
the database is an in-memory fake. Nothing is left in sys.modules.
"""
import asyncio
import copy
import importlib
import os
import re
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import Request

import auth
from models import (InviteAccept, InviteCreate, MemberAccessUpdate, ProjectCreate,
                    UserLogin, utc_now)
from services import project_access as pa
from services import project_scope, runtime_settings


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ fakes
def _get(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _match_value(actual, cond):
    if isinstance(cond, dict) and any(str(k).startswith("$") for k in cond):
        for op, v in cond.items():
            if op == "$in" and actual not in v:
                return False
            if op == "$nin" and actual in v:
                return False
            if op == "$ne" and actual == v:
                return False
            if op == "$gte" and not (actual is not None and actual >= v):
                return False
            if op == "$gt" and not (actual is not None and actual > v):
                return False
            if op == "$lte" and not (actual is not None and actual <= v):
                return False
            if op == "$exists" and (actual is not None) != bool(v):
                return False
        return True
    return actual == cond


def _match(doc, flt):
    for k, v in (flt or {}).items():
        if k == "$and":
            if not all(_match(doc, f) for f in v):
                return False
        elif k == "$or":
            if not any(_match(doc, f) for f in v):
                return False
        elif not _match_value(_get(doc, k), v):
            return False
    return True


def _project(doc, proj):
    d = copy.deepcopy(doc)
    if not proj:
        return d
    inc = {k for k, v in proj.items() if v and k != "_id"}
    exc = {k for k, v in proj.items() if not v}
    if inc:
        return {k: v for k, v in d.items() if k in inc}
    return {k: v for k, v in d.items() if k not in exc}


class _Res:
    def __init__(self, n=1, upserted_id=None):
        self.matched_count = n
        self.modified_count = n
        self.deleted_count = n
        self.upserted_id = upserted_id


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        if isinstance(key, list):
            key, direction = key[0]
        self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key) or ""),
                        reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, *a, **k):
        return [copy.deepcopy(d) for d in self._docs]


class DupKey(Exception):
    pass


def _apply(doc, upd, inserting=False):
    for k, v in (upd.get("$set") or {}).items():
        doc[k] = v
    for k, v in (upd.get("$inc") or {}).items():
        doc[k] = (doc.get(k) or 0) + v
    for k in (upd.get("$unset") or {}):
        doc.pop(k, None)
    if inserting:
        for k, v in (upd.get("$setOnInsert") or {}).items():
            doc[k] = v


class Coll:
    def __init__(self, name, docs=None, unique=None):
        self.name = name
        self.docs = [dict(d) for d in (docs or [])]
        self.unique = unique  # tuple of fields that must be unique
        self.calls = {}

    def _count(self, op):
        self.calls[op] = self.calls.get(op, 0) + 1

    async def create_index(self, keys, **kw):
        return "ix"

    async def find_one(self, flt=None, proj=None, sort=None, **k):
        self._count("find_one")
        rows = [d for d in self.docs if _match(d, flt)]
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return _project(rows[0], proj) if rows else None

    def find(self, flt=None, proj=None, **k):
        self._count("find")
        return _Cursor([_project(d, proj) for d in self.docs if _match(d, flt)])

    async def count_documents(self, flt):
        return sum(1 for d in self.docs if _match(d, flt))

    async def insert_one(self, doc):
        if self.unique:
            key = tuple(doc.get(f) for f in self.unique)
            if any(tuple(d.get(f) for f in self.unique) == key for d in self.docs):
                raise DupKey("duplicate")
        self.docs.append(dict(doc))
        return _Res()

    async def update_one(self, flt, upd, upsert=False, **k):
        for d in self.docs:
            if _match(d, flt):
                _apply(d, upd)
                return _Res(1)
        if upsert:
            doc = {k2: v for k2, v in flt.items() if not k2.startswith("$")
                   and not isinstance(v, dict)}
            _apply(doc, upd, inserting=True)
            self.docs.append(doc)
            return _Res(0, upserted_id="new")
        return _Res(0)

    async def update_many(self, flt, upd, **k):
        n = 0
        for d in self.docs:
            if _match(d, flt):
                _apply(d, upd)
                n += 1
        return _Res(n)

    async def find_one_and_update(self, flt, upd, projection=None, **k):
        await asyncio.sleep(0)
        for d in self.docs:
            if _match(d, flt):
                before = _project(d, projection)
                _apply(d, upd)
                return before
        return None

    async def delete_one(self, flt):
        for d in list(self.docs):
            if _match(d, flt):
                self.docs.remove(d)
                return _Res(1)
        return _Res(0)

    async def delete_many(self, flt):
        keep = [d for d in self.docs if not _match(d, flt)]
        n = len(self.docs) - len(keep)
        self.docs = keep
        return _Res(n)


class DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._c:
            unique = ("tenant_id", "project_id", "user_id") if name == "project_members" else None
            self._c[name] = Coll(name, unique=unique)
        return self._c[name]

    def __getitem__(self, name):
        return getattr(self, name)


# ------------------------------------------------------------------ world
A, B = "tenant-a", "tenant-b"
P1, P2, P3 = "proj-1", "proj-2", "proj-3"     # company A
PB = "proj-b"                                 # company B
NOW = "2026-11-01T00:00:00+00:00"

ROLES = ["pm", "px", "super", "pe", "foreman", "viewer",
         "submittal_tech", "scheduling_engineer"]


def _seed(db):
    db.tenants.docs += [
        {"id": A, "legal_name": "A Co", "display_name": "A Co", "slug": "aco",
         "type": "general_contractor", "status": "active", "created_at": NOW},
        {"id": B, "legal_name": "B Co", "display_name": "B Co", "slug": "bco",
         "type": "general_contractor", "status": "active", "created_at": NOW},
    ]
    db.projects.docs += [
        {"id": P1, "tenant_id": A, "name": "One", "status": "active", "created_at": "1"},
        {"id": P2, "tenant_id": A, "name": "Two", "status": "active", "created_at": "2"},
        {"id": P3, "tenant_id": A, "name": "Three", "status": "active", "created_at": "3"},
        {"id": PB, "tenant_id": B, "name": "Bee", "status": "active", "created_at": "4"},
    ]
    users = [
        ("admin1", A, "admin", None), ("admin2", A, "admin", "assigned"),
        ("adminB", B, "admin", None),
    ] + [(f"{r}1", A, r, None) for r in ROLES]
    for uid, tid, role, scope in users:
        u = {"id": uid, "email": f"{uid}@example.com", "full_name": uid.title(),
             "tenant_id": tid, "role": role, "created_at": NOW,
             "password_hash": "x"}
        if scope:
            u["access_scope"] = scope
        db.users.docs.append(u)
        db.tenant_users.docs.append({"id": f"tu-{uid}", "user_id": uid, "tenant_id": tid,
                                     "role": role, "status": "active"})
    # admin of A is a "super" in company B
    db.tenant_users.docs.append({"id": "tu-x", "user_id": "admin1", "tenant_id": B,
                                 "role": "super", "status": "active"})
    # every non-admin of A is on P1 (any role_on_project counts)
    for r in ROLES:
        db.project_members.docs.append({
            "id": f"pm-{r}", "tenant_id": A, "project_id": P1, "user_id": f"{r}1",
            "role_on_project": "viewer", "added_at": NOW})
    db.project_members.docs.append({
        "id": "pm-admin2", "tenant_id": A, "project_id": P2, "user_id": "admin2",
        "role_on_project": "pm", "added_at": NOW})


MODULES = [
    "auth", "services.project_access", "services.project_scope",
    "routers.auth_router", "routers.projects_router", "routers.project_members_router",
    "routers.iter7_router", "routers.system_router",
]


@pytest.fixture
def db(monkeypatch):
    d = DB()
    _seed(d)
    for name in MODULES:
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", lambda: d)
    monkeypatch.setattr(auth, "_attempt_indexes_ready", False)

    async def _audit(**kw):
        d.audit_log.docs.append({"op": kw.get("op"), "after": kw.get("after")})
    for name in ("routers.auth_router", "routers.projects_router",
                 "routers.project_members_router"):
        monkeypatch.setattr(importlib.import_module(name), "write_audit", _audit)
    monkeypatch.delenv("FF_PROJECT_ACCESS", raising=False)
    runtime_settings.reset_cache_for_tests()
    yield d
    runtime_settings.reset_cache_for_tests()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()


def _token(uid, tid=A):
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    return pyjwt.encode({"sub": uid, "tid": tid, "tenant_id": tid, "email": "x", "exp": exp},
                        auth.JWT_SECRET, algorithm=auth.JWT_ALG)


def _ctx(uid, tenant=A, header=None, method=None):
    ctx = run(auth.build_request_context(_token(uid, tenant), header))
    if method:
        ctx["_pa_method"] = method
        ctx["_pa_route"] = "/api/test"
    return ctx


def _request(ip="10.0.0.1"):
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "client": (ip, 5555)})


# ---------------------------------------------------- 1. role + scope
def test_active_company_role_is_used_not_home_role(db):
    home = _ctx("admin1")
    assert home["role"] == "admin" and home["user"]["role"] == "admin"
    assert home["access_scope"] == "all" and pa.is_full_admin(home)
    other = _ctx("admin1", header=B)
    assert other["tenant_id"] == B
    assert other["role"] == "super" and other["user"]["role"] == "super"
    assert other["access_scope"] == "assigned" and not pa.is_full_admin(other)
    with pytest.raises(HTTPException) as e:
        auth.role_required(other, ["pm"])
    assert e.value.status_code == 403


def test_unknown_x_tenant_falls_back_to_home(db):
    ctx = _ctx("super1", header=B)
    assert ctx["tenant_id"] == A and ctx["role"] == "super"


@pytest.mark.parametrize("role", ROLES)
def test_every_non_admin_role_is_assigned_scope(db, role):
    db.users.find_one  # noqa
    for u in db.users.docs:
        if u["id"] == f"{role}1":
            u["access_scope"] = "all"  # a stale "all" never counts
    ctx = _ctx(f"{role}1")
    assert ctx["access_scope"] == "assigned" and not pa.is_full_admin(ctx)
    assert run(pa.allowed_project_ids(ctx)) == frozenset({P1})


def test_admin_scope_defaults_and_stored_value(db):
    assert _ctx("admin1")["access_scope"] == "all"
    limited = _ctx("admin2")
    assert limited["access_scope"] == "assigned" and limited["role"] == "admin"
    assert run(pa.allowed_project_ids(limited)) == frozenset({P2})
    # the membership row wins over the user record
    for tu in db.tenant_users.docs:
        if tu["user_id"] == "admin2":
            tu["access_scope"] = "all"
    assert pa.is_full_admin(_ctx("admin2"))


def test_ctx_without_scope_keeps_admin_full():
    assert pa.is_full_admin({"user": {"id": "x", "role": "admin"}, "tenant_id": A})
    assert not pa.is_full_admin({"user": {"id": "x", "role": "pm"}, "tenant_id": A})


def test_old_tokens_still_work_and_me_shows_scope(db):
    ar = importlib.import_module("routers.auth_router")
    me = run(ar.me(_ctx("super1")))
    assert me.role == "super" and me.access_scope == "assigned"


def test_legacy_access_helpers_respect_limited_admin(db):
    assert run(auth.get_accessible_project_ids(
        tenant_id=A, user_id="admin1", user_role="admin")) is None
    assert run(auth.get_accessible_project_ids(
        tenant_id=A, user_id="admin2", user_role="admin")) == [P2]
    assert not run(auth.has_project_access(
        tenant_id=A, user_id="admin2", user_role="admin", project_id=P1))


# ---------------------------------------------------- 2. check_project_access
def test_watch_only_records_and_never_refuses(db):
    ctx = _ctx("super1", method="PATCH")
    run(pa.check_project_access(ctx, P2, write=True, route="/api/projects/{project_id}"))
    run(pa.check_project_access(_ctx("super1", method="PATCH"), P2, write=True,
                                route="/api/projects/{project_id}"))
    rows = db.project_access_watch.docs
    assert len(rows) == 1
    row = rows[0]
    assert (row["tenant_id"], row["user_id"], row["project_id"], row["route"],
            row["method"]) == (A, "super1", P2, "/api/projects/{project_id}", "PATCH")
    assert row["count"] == 2 and row["role"] == "super" and row["first_at"] and row["last_at"]
    assert "blocked_count" not in row


def test_enforced_read_404_write_403(db, enforced):
    with pytest.raises(HTTPException) as e:
        run(pa.check_project_access(_ctx("super1", method="GET"), P2, write=False))
    assert e.value.status_code == 404 and e.value.detail == "Project not found"
    with pytest.raises(HTTPException) as e:
        run(pa.check_project_access(_ctx("foreman1", method="POST"), P2, write=True))
    assert e.value.status_code == 403
    assert e.value.detail == "You are not assigned to this project"
    blocked = [r for r in db.project_access_watch.docs if r.get("blocked_count")]
    assert len(blocked) == 2


@pytest.mark.parametrize("uid", ["admin1"] + [f"{r}1" for r in ROLES])
def test_enforced_assigned_and_full_admin_pass(db, enforced, uid):
    run(pa.check_project_access(_ctx(uid), P1, write=True))


def test_enforced_limited_admin_only_own_projects(db, enforced):
    run(pa.check_project_access(_ctx("admin2"), P2, write=True))
    with pytest.raises(HTTPException) as e:
        run(pa.check_project_access(_ctx("admin2"), P1, write=True))
    assert e.value.status_code == 403


def test_unknown_or_other_company_project_left_to_caller(db, enforced):
    run(pa.check_project_access(_ctx("super1"), "nope", write=True))
    run(pa.check_project_access(_ctx("super1"), PB, write=True))
    run(pa.check_project_access(_ctx("super1"), None, write=True))
    assert db.project_access_watch.docs == []


def test_watch_failure_never_fails_request(db, monkeypatch):
    class Broken(Coll):
        async def update_one(self, *a, **k):
            raise RuntimeError("db down")
    db._c["project_access_watch"] = Broken("project_access_watch")
    run(pa.check_project_access(_ctx("super1"), P2, write=True))


def test_require_project_applies_check(db, enforced):
    with pytest.raises(HTTPException) as e:
        run(project_scope.require_project(_ctx("super1", method="POST"), P2))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        run(project_scope.require_project(_ctx("super1", method="GET"), P2))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        run(project_scope.require_project(_ctx("super1"), "missing"))
    assert e.value.status_code == 404
    assert run(project_scope.require_project(_ctx("super1"), P1))["id"] == P1
    assert run(project_scope.require_project_if_given(_ctx("super1"), "")) is None
    # the worker variant has no user and no membership rule
    assert run(project_scope.require_project_for_tenant(A, P2))["id"] == P2


# ---------------------------------------------------- 3. resolve + filters
def test_resolve_project_id_one_and_two_hops(db):
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": A, "project_id": P2})
    db.daily_report_activities.docs.append({"id": "act1", "daily_report_id": "dr1"})
    db.drawings.docs.append({"id": "dw1", "tenant_id": A, "project_id": P3})
    db.drawing_revisions.docs.append({"id": "rev1", "tenant_id": A, "drawing_id": "dw1"})
    db.drawing_markups.docs.append({"id": "mk1", "tenant_id": A, "drawing_revision_id": "rev1"})
    db.voice_annotations.docs.append({"id": "va1", "tenant_id": A,
                                      "linked_entity_type": "daily_report",
                                      "linked_entity_id": "dr1"})
    db.crews.docs.append({"id": "c1", "tenant_id": A, "home_project_id": P3})
    ctx = _ctx("super1")
    assert run(pa.resolve_project_id(ctx, "daily_report", "dr1")) == P2
    assert run(pa.resolve_project_id(ctx, "daily_report_activity", "act1")) == P2
    assert run(pa.resolve_project_id(ctx, "drawing_markup", "mk1")) == P3
    assert run(pa.resolve_project_id(ctx, "voice_annotation", "va1")) == P2
    assert run(pa.resolve_project_id(ctx, "crew", "c1")) == P3
    assert run(pa.resolve_project_id(ctx, "daily-reports", "dr1")) == P2
    assert run(pa.resolve_project_id(ctx, "schedule_activity", "missing")) is None
    assert run(pa.resolve_project_id(ctx, "no_such_kind", "dr1")) is None


def test_project_scope_filter(db, monkeypatch):
    ctx = _ctx("super1")
    q = {"tenant_id": A}
    assert run(pa.project_scope_filter(ctx, q)) == q  # off: unchanged
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    assert run(pa.project_scope_filter(ctx, q)) == {"tenant_id": A, "project_id": {"$in": [P1]}}
    assert run(pa.project_scope_filter(ctx, q, include_company_level=True)) == {
        "tenant_id": A, "project_id": {"$in": [P1, None]}}
    both = run(pa.project_scope_filter(ctx, {"tenant_id": A, "project_id": P2}))
    assert "$and" in both
    assert run(pa.project_scope_filter(_ctx("admin1"), q)) == q
    rows = [{"project_id": P1}, {"project_id": P2}, {"project_id": None}]
    assert run(pa.filter_rows(ctx, rows)) == [{"project_id": P1}]


def test_targets_for_route():
    t = pa.targets_for_route
    assert t("/api/projects/{project_id}/tasks", {"project_id": P1}) == [("project", P1)]
    assert t("/api/daily-report-activities/{row_id}", {"row_id": "a"}) == [
        ("daily_report_activity", "a")]
    assert t("/api/entities/{entity_type}/{entity_id}",
             {"entity_type": "rfi", "entity_id": "r"}) == [("rfi", "r")]
    assert t("/api/crud/{entity}/{id}", {"entity": "safety-events", "id": "s"}) == [
        ("safety_event", "s")]
    assert t("/api/ai/copilot/sessions/{session_id}", {"session_id": "x"}) == []
    assert t("/api/daily-reports", {}, {"project_id": P1}) == [("project", P1)]
    assert t("/api/x", {}, {"project_id": "a,b"}) == []


# ---------------------------------------------------- 4. route dependency
def _app(db):
    from auth import require_tenant
    r = APIRouter(prefix="/api")

    @r.get("/projects/{project_id}")
    async def get_p(project_id: str, ctx: dict = Depends(require_tenant)):
        return {"ok": True, "role": ctx["role"]}

    @r.patch("/projects/{project_id}")
    async def patch_p(project_id: str, ctx: dict = Depends(require_tenant)):
        return {"ok": True}

    @r.patch("/daily-report-activities/{row_id}")
    async def patch_act(row_id: str, ctx: dict = Depends(require_tenant)):
        return {"ok": True}

    @r.post("/daily-reports")
    async def list_dr(project_id: str = "", ctx: dict = Depends(require_tenant)):
        return {"ok": True}

    @r.post("/internal/ms-project/file/{project_id}")
    async def worker(project_id: str):
        return {"ok": True}

    @r.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    app = FastAPI()
    app.include_router(r, dependencies=[Depends(pa.enforce_project_scope)])
    db.daily_reports.docs.append({"id": "dr9", "tenant_id": A, "project_id": P2})
    db.daily_report_activities.docs.append({"id": "act9", "daily_report_id": "dr9"})
    return TestClient(app)


def _h(uid, tid=None):
    h = {"Authorization": f"Bearer {_token(uid)}"}
    if tid:
        h["X-Tenant-ID"] = tid
    return h


def test_dependency_enforced(db, enforced):
    c = _app(db)
    r = c.get(f"/api/projects/{P2}", headers=_h("super1"))
    assert r.status_code == 404 and r.json()["detail"] == "Project not found"
    r = c.patch(f"/api/projects/{P2}", headers=_h("super1"))
    assert r.status_code == 403 and r.json()["detail"] == "You are not assigned to this project"
    # child record two hops away (activity -> report -> project)
    r = c.patch("/api/daily-report-activities/act9", headers=_h("foreman1"))
    assert r.status_code == 403
    # project_id in the query string
    r = c.post(f"/api/daily-reports?project_id={P2}", headers=_h("viewer1"))
    assert r.status_code == 403
    assert c.get(f"/api/projects/{P1}", headers=_h("super1")).status_code == 200
    assert c.patch(f"/api/projects/{P2}", headers=_h("admin1")).status_code == 200
    assert c.patch(f"/api/projects/{P2}", headers=_h("admin2")).status_code == 200
    assert c.patch(f"/api/projects/{P1}", headers=_h("admin2")).status_code == 403
    # unknown project: the endpoint decides (here it answers 200)
    assert c.patch("/api/projects/nope", headers=_h("super1")).status_code == 200
    # no sign-in: the endpoint's own 401
    assert c.get(f"/api/projects/{P2}").status_code == 401
    # exempt worker path and WebSocket untouched
    assert c.post(f"/api/internal/ms-project/file/{P2}").status_code == 200
    with c.websocket_connect("/api/ws") as ws:
        assert ws.receive_text() == "hi"


def test_dependency_other_company_admin_is_not_admin_there(db, enforced):
    c = _app(db)
    db.projects.docs.append({"id": "pb2", "tenant_id": B, "name": "B2"})
    r = c.get("/api/projects/pb2", headers=_h("admin1", B))
    assert r.status_code == 404
    db.project_members.docs.append({"id": "x", "tenant_id": B, "project_id": "pb2",
                                    "user_id": "admin1", "role_on_project": "super"})
    r = c.get("/api/projects/pb2", headers=_h("admin1", B))
    assert r.status_code == 200 and r.json()["role"] == "super"


def test_dependency_watch_only_when_off(db):
    c = _app(db)
    assert c.patch(f"/api/projects/{P2}", headers=_h("super1")).status_code == 200
    assert c.patch("/api/daily-report-activities/act9", headers=_h("super1")).status_code == 200
    rows = {(r["route"], r["method"]) for r in db.project_access_watch.docs}
    assert ("/api/projects/{project_id}", "PATCH") in rows
    assert ("/api/daily-report-activities/{row_id}", "PATCH") in rows


def test_dependency_shares_sign_in_lookup(db, enforced):
    c = _app(db)
    before = db.users.calls.get("find_one", 0)
    assert c.get(f"/api/projects/{P1}", headers=_h("super1")).status_code == 200
    assert db.users.calls.get("find_one", 0) - before == 1


# ---------------------------------------------------- 5. invites
def _ar():
    return importlib.import_module("routers.auth_router")


@pytest.fixture
def no_email(monkeypatch):
    import services.invite_email as ie
    monkeypatch.setattr(ie, "send_invite_email", lambda **kw: {"sent": True})
    monkeypatch.setenv("INVITE_BASE_URL", "https://prod.example.com")


def _accept(code, email, db):
    return run(_ar().accept_invite(InviteAccept(code=code, email=email, full_name="N",
                                                password="Password-123"), _request()))


def test_invite_admin_limited_to_projects(db, no_email):
    out = run(_ar().create_invite(InviteCreate(email="lim@example.com", role="admin",
                                               access_scope="assigned",
                                               project_ids=[P1, P3, P1]),
                                  _request(), _ctx("admin1")))
    inv = out["invite"]
    assert inv["access_scope"] == "assigned" and inv["project_ids"] == [P1, P3]
    tok = _accept(inv["code"], "lim@example.com", db)
    uid = tok.user.id
    tu = [t for t in db.tenant_users.docs if t["user_id"] == uid][0]
    assert tu["access_scope"] == "assigned"
    rows = [m for m in db.project_members.docs if m["user_id"] == uid]
    assert {m["project_id"] for m in rows} == {P1, P3}
    assert {m["role_on_project"] for m in rows} == {"pm"}
    ctx = run(auth.build_request_context(tok.access_token, None))
    assert ctx["role"] == "admin" and not pa.is_full_admin(ctx)


def test_invite_admin_defaults_to_all_and_ignores_projects(db, no_email):
    out = run(_ar().create_invite(InviteCreate(email="full@example.com", role="admin",
                                               project_ids=[P1]),
                                  _request(), _ctx("admin1")))
    assert out["invite"]["access_scope"] == "all" and out["invite"]["project_ids"] == []
    tok = _accept(out["invite"]["code"], "full@example.com", db)
    assert pa.is_full_admin(run(auth.build_request_context(tok.access_token, None)))


@pytest.mark.parametrize("role,expected", [("super", "super"), ("pe", "super"),
                                           ("pm", "pm"), ("foreman", "foreman"),
                                           ("viewer", "viewer"),
                                           ("submittal_tech", "submittal_tech"),
                                           ("scheduling_engineer", "scheduling_engineer")])
def test_invite_non_admin_forced_assigned_with_projects(db, no_email, role, expected):
    out = run(_ar().create_invite(InviteCreate(email=f"n-{role}@example.com", role=role,
                                               access_scope="all", project_ids=[P2]),
                                  _request(), _ctx("admin1")))
    assert out["invite"]["access_scope"] == "assigned"
    tok = _accept(out["invite"]["code"], f"n-{role}@example.com", db)
    rows = [m for m in db.project_members.docs if m["user_id"] == tok.user.id]
    assert [(m["project_id"], m["role_on_project"]) for m in rows] == [(P2, expected)]


def test_invite_rejects_projects_of_another_company(db, no_email):
    with pytest.raises(HTTPException) as e:
        run(_ar().create_invite(InviteCreate(email="z@example.com", role="super",
                                             project_ids=[PB]),
                                _request(), _ctx("admin1")))
    assert e.value.status_code == 422


@pytest.mark.parametrize("uid", ["admin2", "pm1", "super1"])
def test_only_full_admin_invites(db, no_email, uid):
    with pytest.raises(HTTPException) as e:
        run(_ar().create_invite(InviteCreate(email="z@example.com", role="super"),
                                _request(), _ctx(uid)))
    assert e.value.status_code == 403


def test_old_invite_without_access_fields(db):
    exp = (utc_now() + timedelta(days=1)).isoformat()
    db.team_invites.docs += [
        {"id": "i1", "tenant_id": A, "email": "olda@example.com", "role": "admin",
         "code": "OLDADMIN1234", "status": "pending", "expires_at": exp},
        {"id": "i2", "tenant_id": A, "email": "olds@example.com", "role": "super",
         "code": "OLDSUPER1234", "status": "pending", "expires_at": exp},
    ]
    a = _accept("OLDADMIN1234", "olda@example.com", db)
    s = _accept("OLDSUPER1234", "olds@example.com", db)
    assert pa.is_full_admin(run(auth.build_request_context(a.access_token, None)))
    assert not [m for m in db.project_members.docs if m["user_id"] == s.user.id]


def test_invite_codes_hidden_from_limited_admin(db):
    db.team_invites.docs.append({"id": "i9", "tenant_id": A, "email": "q@example.com",
                                 "role": "super", "code": "SECRETCODE12", "status": "pending",
                                 "invited_at": NOW})
    rows = run(_ar().list_invites(_ctx("admin2")))
    assert rows and "code" not in rows[0]
    assert run(_ar().list_invites(_ctx("admin1")))[0]["code"] == "SECRETCODE12"


# ---------------------------------------------------- 6. member access + guard
def test_member_access_edit_scope_and_projects(db):
    ar = _ar()
    out = run(ar.update_member_access("super1", MemberAccessUpdate(project_ids=[P2, P3]),
                                      _ctx("admin1")))
    assert set(out["projects_added"]) == {P2, P3} and out["projects_removed"] == [P1]
    assert {m["project_id"] for m in db.project_members.docs if m["user_id"] == "super1"} == {P2, P3}
    out = run(ar.update_member_access("admin2", MemberAccessUpdate(access_scope="all"),
                                      _ctx("admin1")))
    assert out["access_scope"] == "all" and pa.is_full_admin(_ctx("admin2"))
    out = run(ar.update_member_access("pm1", MemberAccessUpdate(role="admin"), _ctx("admin1")))
    assert out["role"] == "admin" and out["access_scope"] == "all"
    assert _ctx("pm1")["role"] == "admin"
    assert any(r["op"] == "MEMBER_ACCESS_CHANGED" for r in db.audit_log.docs)


def test_member_access_edit_refuses_others(db):
    ar = _ar()
    for uid in ("admin2", "pm1"):
        with pytest.raises(HTTPException) as e:
            run(ar.update_member_access("super1", MemberAccessUpdate(project_ids=[P2]),
                                        _ctx(uid)))
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        run(ar.update_member_access("adminB", MemberAccessUpdate(access_scope="assigned"),
                                    _ctx("admin1")))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        run(ar.update_member_access("super1", MemberAccessUpdate(project_ids=[PB]),
                                    _ctx("admin1")))
    assert e.value.status_code == 422


def test_last_full_admin_guard(db):
    ar = _ar()
    msg = "The company must keep at least one admin with access to all projects"
    with pytest.raises(HTTPException) as e:
        run(ar.update_member_access("admin1", MemberAccessUpdate(access_scope="assigned"),
                                    _ctx("admin1")))
    assert e.value.status_code == 409 and e.value.detail == msg
    with pytest.raises(HTTPException) as e:
        run(ar.update_member_access("admin1", MemberAccessUpdate(role="pm"), _ctx("admin1")))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        run(pa.ensure_keeps_full_admin(db, A, "admin1", removing=True))
    assert e.value.status_code == 409
    # a second all-projects admin makes the change fine
    run(ar.update_member_access("admin2", MemberAccessUpdate(access_scope="all"), _ctx("admin1")))
    # A limited admin needs at least one project (422 with none).
    with pytest.raises(HTTPException) as e:
        run(ar.update_member_access("admin1", MemberAccessUpdate(access_scope="assigned"),
                                    _ctx("admin1")))
    assert e.value.status_code == 422
    run(ar.update_member_access("admin1", MemberAccessUpdate(access_scope="assigned",
                                                             project_ids=[P1]),
                                _ctx("admin1")))
    assert not pa.is_full_admin(_ctx("admin1"))
    # now admin2 is the last one: removal is refused by the guard
    with pytest.raises(HTTPException) as e:
        run(pa.ensure_keeps_full_admin(db, A, "admin2", removing=True))
    assert e.value.status_code == 409


def test_remove_member_needs_full_admin_and_keeps_one(db):
    ar = _ar()
    with pytest.raises(HTTPException) as e:
        run(ar.remove_member("super1", _ctx("admin2")))
    assert e.value.status_code == 403
    assert run(ar.remove_member("admin2", _ctx("admin1")))["ok"]


def test_last_full_admin_guard_rechecks_after_saving(db, monkeypatch):
    """Two owners limiting / removing each other at the same moment both pass
    the check made before saving. The re-check after saving must undo the
    change and refuse, leaving the member exactly as before."""
    ar = _ar()
    run(ar.update_member_access("admin2", MemberAccessUpdate(access_scope="all"), _ctx("admin1")))
    before = dict(next(u for u in db.users.docs if u["id"] == "admin1"))
    # The other owner's change landed first: admin2 is now limited, but this
    # request's earlier check saw two all-projects admins.
    run(ar.update_member_access("admin2", MemberAccessUpdate(access_scope="assigned"),
                                _ctx("admin1")))
    # Stand-in for the earlier check: it passed, and the member was an
    # all-projects admin at that moment.
    monkeypatch.setattr(ar, "ensure_keeps_full_admin", lambda *a, **k: _passed())
    with pytest.raises(HTTPException) as e:
        run(ar.update_member_access("admin1", MemberAccessUpdate(role="pm"), _ctx("admin1")))
    assert e.value.status_code == 409
    after = next(u for u in db.users.docs if u["id"] == "admin1")
    assert after.get("role") == before.get("role") == "admin"
    assert after.get("access_scope") == before.get("access_scope")
    assert pa.is_full_admin(_ctx("admin1"))
    # The same for removal: admin1 is the only all-projects admin left.
    run(ar.update_member_access("admin2", MemberAccessUpdate(access_scope="all"), _ctx("admin1")))
    run(ar.update_member_access("admin1", MemberAccessUpdate(access_scope="assigned",
                                                             project_ids=[P1]),
                                _ctx("admin2")))
    with pytest.raises(HTTPException) as e:
        run(ar.remove_member("admin2", _ctx("admin1") | {"access_scope": "all"}))
    assert e.value.status_code == 409
    kept = next(u for u in db.users.docs if u["id"] == "admin2")
    assert not kept.get("removal_pending") and pa.is_full_admin(_ctx("admin2"))


async def _passed():
    return True


def test_company_wide_controls_need_full_admin(db):
    ar = _ar()
    sr = importlib.import_module("routers.system_router")
    with pytest.raises(HTTPException) as e:
        run(ar.close_account(_ctx("admin2")))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        run(ar.admin_reset_password("super1", ar.PasswordResetPayload(new_password="x" * 10),
                                    _ctx("admin2")))
    assert e.value.status_code == 403
    for uid in ("admin2", "pm1", "super1"):
        with pytest.raises(HTTPException) as e:
            run(sr.cron_cascade_recompute(_ctx(uid)))
        assert e.value.status_code == 403
    asr = importlib.import_module("routers.admin_settings_router")
    with pytest.raises(HTTPException) as e:
        run(asr.get_settings(_ctx("admin2")))
    assert e.value.status_code == 403


def test_list_members_shows_scope_and_counts(db):
    rows = {r["id"]: r for r in run(_ar().list_members(_ctx("admin1")))}
    assert rows["admin1"]["access_scope"] == "all"
    assert rows["admin2"]["access_scope"] == "assigned" and rows["admin2"]["project_count"] == 1
    assert rows["super1"]["project_ids"] == [P1]
    plain = {r["id"]: r for r in run(_ar().list_members(_ctx("super1")))}
    assert "project_ids" not in plain["super1"] and plain["super1"]["project_count"] == 1
    assert "password_hash" not in plain["super1"]


# ---------------------------------------------------- 7. projects
def test_creator_is_assigned(db):
    pr = importlib.import_module("routers.projects_router")
    p = run(pr.create_project(ProjectCreate(name="New PM"), _ctx("pm1")))
    assert [m["role_on_project"] for m in db.project_members.docs
            if m["project_id"] == p.id and m["user_id"] == "pm1"] == ["pm"]
    p2 = run(pr.create_project(ProjectCreate(name="New lim"), _ctx("admin2")))
    assert any(m["project_id"] == p2.id and m["user_id"] == "admin2"
               for m in db.project_members.docs)
    p3 = run(pr.create_project(ProjectCreate(name="New full"), _ctx("admin1")))
    assert not [m for m in db.project_members.docs if m["project_id"] == p3.id]


def test_project_list_filtered_only_when_enforced(db, monkeypatch):
    pr = importlib.import_module("routers.projects_router")
    assert {p.id for p in run(pr.list_projects(None, _ctx("super1")))} == {P1, P2, P3}
    assert db.project_access_watch.docs == []
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    assert [p.id for p in run(pr.list_projects(None, _ctx("super1")))] == [P1]
    assert [p.id for p in run(pr.list_projects(None, _ctx("admin2")))] == [P2]
    assert {p.id for p in run(pr.list_projects(None, _ctx("admin1")))} == {P1, P2, P3}


def test_limited_admin_manages_members_only_on_own_projects(db):
    pmr = importlib.import_module("routers.project_members_router")
    run(pmr._enforce_manage(_ctx("admin2"), P2))
    with pytest.raises(HTTPException) as e:
        run(pmr._enforce_manage(_ctx("admin2"), P1))
    assert e.value.status_code == 403
    run(pmr._enforce_manage(_ctx("admin1"), P1))
    with pytest.raises(HTTPException):
        run(pmr.list_project_members(P1, _ctx("admin2")))


# ---------------------------------------------------- 8. report
def test_preflight_report(db):
    pmr = importlib.import_module("routers.project_members_router")
    db.users.docs.append({"id": "lonely", "email": "l@example.com", "full_name": "L",
                          "tenant_id": A, "role": "super", "created_at": NOW})
    db.users.docs.append({"id": "admin3", "email": "a3@example.com", "full_name": "A3",
                          "tenant_id": A, "role": "admin", "access_scope": "assigned",
                          "created_at": NOW})
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    db.daily_reports.docs += [
        {"id": "d1", "tenant_id": A, "project_id": P2, "report_date": recent,
         "prepared_by": "super1"},
        {"id": "d2", "tenant_id": A, "project_id": P2, "report_date": old,
         "prepared_by": "foreman1"},
        {"id": "d3", "tenant_id": A, "project_id": P2, "report_date": recent,
         "prepared_by": "admin1"},
        {"id": "d4", "tenant_id": A, "project_id": P1, "report_date": recent,
         "prepared_by": "pm1"},
    ]
    run(pa.check_project_access(_ctx("viewer1", method="GET"), P3, write=False))
    rep = run(pmr.project_access_report(_ctx("admin1")))
    assert [u["user_id"] for u in rep["users_without_projects"]] == ["lonely"]
    assert rep["users_without_projects"][0]["suggested_role_on_project"] == "super"
    assert [u["user_id"] for u in rep["limited_admins_without_projects"]] == ["admin3"]
    assert [p["project_id"] for p in rep["projects_without_members"]] == [P3]
    assert [(r["user_id"], r["project_id"]) for r in rep["daily_reports_by_unassigned"]] == [
        ("super1", P2)]
    assert rep["watch"][0]["project_id"] == P3 and rep["watch"][0]["email"] == "viewer1@example.com"
    assert rep["enforced"] is False
    with pytest.raises(HTTPException) as e:
        run(pmr.project_access_report(_ctx("admin2")))
    assert e.value.status_code == 403


def test_flag_registered():
    from routers import admin_settings_router as asr
    from services import feature_flags
    assert "FF_PROJECT_ACCESS" in runtime_settings.ALLOWED_FLAGS
    meta = [m for m in asr.FLAG_META if m["key"] == "FF_PROJECT_ACCESS"]
    assert meta and meta[0]["label"] == "Per-project access (off = watch only)"
    assert feature_flags.project_access_enabled() is False


# ---------------------------------------------------- 9. sign-in lockout
@pytest.fixture
def login_db(db):
    db.users.docs.append({"id": "lu", "email": "lu@example.com", "full_name": "L",
                          "tenant_id": A, "role": "super", "created_at": NOW,
                          "password_hash": auth.hash_password("Correct-horse-1")})
    return db


def _login(pw, ip):
    return run(_ar().login(UserLogin(email="lu@example.com", password=pw), _request(ip)))


def test_stranger_cannot_lock_out_real_user(login_db):
    for _ in range(10):
        with pytest.raises(HTTPException) as e:
            _login("wrong-pass", "6.6.6.6")
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        _login("Correct-horse-1", "6.6.6.6")
    assert e.value.status_code == 429
    assert _login("Correct-horse-1", "10.1.1.1").access_token


def test_account_total_limit_across_addresses(login_db):
    for i in range(100):
        with pytest.raises(HTTPException) as e:
            _login("wrong-pass", f"7.{i // 200}.{i % 200}.1")
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        _login("Correct-horse-1", "10.9.9.9")
    assert e.value.status_code == 429


# ---------------------------------------------------- 10. voice PIN
def _pin(db, pin="123456", hours=1):
    db.voice_pins.docs.append({"id": f"v{pin}{hours}", "tenant_id": A, "project_id": P1,
                               "name": "Foreman", "pin": pin, "redeemed_at": None,
                               "expires_at": (utc_now() + timedelta(hours=hours)).isoformat()})


def test_voice_pin_single_use_even_when_simultaneous(db):
    ir = importlib.import_module("routers.iter7_router")
    _pin(db)

    async def both():
        return await asyncio.gather(*[
            ir.exchange_voice_pin(ir.ExchangePinRequest(pin="123456", tenant_slug="aco"),
                                  _request(f"8.8.8.{i}"))
            for i in range(2)], return_exceptions=True)
    res = run(both())
    ok = [r for r in res if isinstance(r, dict)]
    bad = [r for r in res if isinstance(r, HTTPException)]
    assert len(ok) == 1 and len(bad) == 1 and bad[0].status_code == 401
    assert db.voice_pins.docs[0]["redeemed_at"]


def test_voice_pin_expired_is_refused(db):
    ir = importlib.import_module("routers.iter7_router")
    _pin(db, "222222", hours=-1)
    with pytest.raises(HTTPException) as e:
        run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="222222", tenant_slug="aco"),
                                  _request()))
    assert e.value.status_code == 401
    assert db.voice_pins.docs[0]["redeemed_at"] is None


def test_voice_pin_lock_is_per_address(db):
    ir = importlib.import_module("routers.iter7_router")
    _pin(db)
    for _ in range(20):
        with pytest.raises(HTTPException) as e:
            run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="999999", tenant_slug="aco"),
                                      _request("9.9.9.9")))
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="123456", tenant_slug="aco"),
                                  _request("9.9.9.9")))
    assert e.value.status_code == 429
    out = run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="123456", tenant_slug="aco"),
                                    _request("10.2.2.2")))
    assert out["access_token"]


def test_voice_pin_company_total_limit(db):
    ir = importlib.import_module("routers.iter7_router")
    for i in range(200):
        with pytest.raises(HTTPException):
            run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="999999", tenant_slug="aco"),
                                      _request(f"5.{i // 200}.{i % 200}.3")))
    with pytest.raises(HTTPException) as e:
        run(ir.exchange_voice_pin(ir.ExchangePinRequest(pin="999999", tenant_slug="aco"),
                                  _request("10.3.3.3")))
    assert e.value.status_code == 429
