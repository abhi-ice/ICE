"""Per-project access on the schedule routes (2026-11-16).

Files covered: tasks_router, schedule_proposals_router, schedule_review_router,
schedule_diffs_router, calendar_reconciliation_router, the user routes of
ms_project_internal_router, and services/schedule_digest.

  * every route either names its project (or a record of one) in the path,
    so the route-level check covers it, or checks a body/form id itself, or
    is a worker/cron route (marked "# project-scope: tenant-level");
  * switch ON: assigned user allowed; unassigned 404 on a read and 403
    "You are not assigned to this project" on a save (a phone keeps a 403
    save queued, a 404 would drop it);
  * switch OFF: allowed, and the would-be refusal is recorded in
    project_access_watch;
  * all-projects admin vs admin limited to specific projects;
  * body ids (POST /tasks, schedule upload form, activity-name approvals);
  * the live task_progress message carries project_id;
  * the weekly digest goes only to people who may see the project.

Offline: real routers on a tiny FastAPI app with the in-memory fake database
from test_project_access_foundation. Nothing is left in sys.modules.
"""
import asyncio
import importlib
import inspect
import re

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import auth
from services import project_access as pa
from services import runtime_settings

from test_project_access_foundation import A, B, DB, P1, P2, P3, _seed, _token  # noqa: F401

ROUTER_MODULES = [
    "routers.tasks_router", "routers.schedule_proposals_router",
    "routers.schedule_review_router", "routers.schedule_diffs_router",
    "routers.calendar_reconciliation_router", "routers.ms_project_internal_router",
]
PATCH_DB = ["auth", "services.project_access", "services.project_scope",
            "services.schedule_digest"] + ROUTER_MODULES

NOT_ASSIGNED = "You are not assigned to this project"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch):
    d = DB()
    _seed(d)
    for name in PATCH_DB:
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", lambda: d)
    monkeypatch.setattr(auth, "_attempt_indexes_ready", False)

    async def _audit(**kw):
        d.audit_log.docs.append({"op": kw.get("op")})
    for name in ROUTER_MODULES:
        mod = importlib.import_module(name)
        if hasattr(mod, "write_audit"):
            monkeypatch.setattr(mod, "write_audit", _audit)
    monkeypatch.delenv("FF_PROJECT_ACCESS", raising=False)
    runtime_settings.reset_cache_for_tests()
    # one record of each kind on P1 (assigned to everyone) and P2 (not)
    for pid in (P1, P2):
        n = pid[-1]
        d.tasks.docs.append({"id": f"t{n}", "tenant_id": A, "project_id": pid,
                             "activity_code": f"A{n}", "description": "Pour",
                             "status": "in_progress", "pct_complete": 10,
                             "created_at": "2026-11-01T00:00:00+00:00"})
        d.schedule_update_diffs.docs.append({"id": f"diff{n}", "tenant_id": A,
                                             "project_id": pid, "computed_at": n})
        d.schedule_proposals.docs.append({"id": f"sp{n}", "tenant_id": A, "project_id": pid,
                                          "status": "rejected", "uploaded_at": n})
        d.activity_name_suggestions.docs.append({
            "id": f"ans{n}", "tenant_id": A, "task_id": f"t{n}", "activity_code": f"A{n}",
            "suggested_name": f"Pour slab {n}", "status": "pending"})
        d.logic_proposals.docs.append({"id": f"lp{n}", "tenant_id": A, "task_id": f"t{n}",
                                       "status": "pending"})
    yield d
    runtime_settings.reset_cache_for_tests()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()


@pytest.fixture
def client(db):
    app = FastAPI()
    for name in ROUTER_MODULES:
        app.include_router(importlib.import_module(name).router, prefix="/api",
                           dependencies=[Depends(pa.enforce_project_scope)])
    return TestClient(app)


def _h(uid):
    return {"Authorization": f"Bearer {_token(uid)}"}


def _watch(db, uid):
    return [w for w in db.project_access_watch.docs if w.get("user_id") == uid]


# ------------------------------------------------------- coverage guard
# Routes that carry their project in the body/form and check it themselves.
BODY_CHECKED = {
    ("POST", "/tasks"),
    ("POST", "/imports/schedule"),
    ("POST", "/activity-name-suggestions/approve"),
}


def test_every_route_is_covered():
    """Each route names a project / known record in the path (route-level
    check), checks its body itself, or is exempt (worker, cron) and marked
    '# project-scope: tenant-level' in the source."""
    missing = []
    for name in ROUTER_MODULES:
        mod = importlib.import_module(name)
        src = inspect.getsource(mod)
        for route in mod.router.routes:
            if not isinstance(route, APIRoute):
                continue
            params = {p: "x" for p in re.findall(r"\{([^}:]+)", route.path)}
            full = "/api" + route.path
            for method in route.methods:
                if (method, route.path) in BODY_CHECKED:
                    continue
                if pa.is_exempt(full):
                    # the decorator must be marked for the reader
                    idx = src.find(f'"{route.path}"')
                    block = src[max(0, idx - 400):idx]
                    if "# project-scope: tenant-level" not in block:
                        missing.append(f"{method} {route.path} (exempt but unmarked)")
                    continue
                if not pa.targets_for_route(full, params):
                    missing.append(f"{method} {route.path}")
    assert not missing, missing


# ------------------------------------------------------- switch ON
def test_on_path_project_read_404_write_403(client, db, enforced):
    h = _h("super1")
    assert client.get(f"/api/projects/{P1}/schedule-diffs", headers=h).status_code == 200
    r = client.get(f"/api/projects/{P2}/schedule-diffs", headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "Project not found"
    r = client.get(f"/api/projects/{P2}/calendar-reconciliation?date_range_start=2026-01-01"
                   f"&date_range_end=2026-01-02", headers=h)
    assert r.status_code == 404
    r = client.post(f"/api/projects/{P2}/ms-project/pull", headers=_h("pm1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    r = client.post(f"/api/projects/{P2}/tasks/bulk-delete", json={"task_ids": ["t2"]},
                    headers=_h("pm1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert db.tasks.docs[1]["status"] == "in_progress"


def test_on_child_records_resolved(client, db, enforced):
    h = _h("pm1")
    assert client.get("/api/schedule-diffs/diff1", headers=h).status_code == 200
    assert client.get("/api/schedule-diffs/diff2", headers=h).status_code == 404
    assert client.get("/api/schedule-proposals/sp2", headers=h).status_code == 404
    r = client.post("/api/schedule-proposals/sp2/reject", json={}, headers=h)
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    # two hops: suggestion / logic proposal -> task -> project
    r = client.post("/api/activity-name-suggestions/ans2/reject", headers=h)
    assert r.status_code == 403
    r = client.post("/api/logic-proposals/lp2/reject", json={}, headers=h)
    assert r.status_code == 403
    assert db.logic_proposals.docs[1]["status"] == "pending"
    r = client.patch("/api/tasks/t2", json={"pct_complete": 50}, headers=h)
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert db.tasks.docs[1]["pct_complete"] == 10


def test_on_create_task_body_project(client, db, enforced):
    body = {"activity_code": "N1", "description": "New"}
    r = client.post("/api/tasks", json={**body, "project_id": P2}, headers=_h("pm1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    r = client.post("/api/tasks", json={**body, "project_id": "nope"}, headers=_h("pm1"))
    assert r.status_code == 404
    r = client.post("/api/tasks", json={**body, "project_id": P1}, headers=_h("pm1"))
    assert r.status_code == 200 and r.json()["project_id"] == P1


def test_on_schedule_upload_form_project(client, db, enforced):
    files = {"file": ("s.csv", b"activity_code,description\nX,Y\n", "text/csv")}
    r = client.post("/api/imports/schedule", data={"project_id": P2}, files=files,
                    headers=_h("pm1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    r = client.post("/api/imports/schedule", data={"project_id": "nope"}, files=files,
                    headers=_h("pm1"))
    assert r.status_code == 404
    assert not db.schedule_proposals.docs[2:]


def test_on_name_approvals_body_ids(client, db, enforced):
    h = _h("pm1")
    r = client.post("/api/activity-name-suggestions/approve",
                    json={"items": [{"id": "ans1"}, {"id": "ans2"}]}, headers=h)
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    # a refused batch changes nothing, not even the allowed item
    assert [s["status"] for s in db.activity_name_suggestions.docs] == ["pending", "pending"]
    r = client.post("/api/activity-name-suggestions/approve",
                    json={"items": [{"id": "ans1"}]}, headers=h)
    assert r.status_code == 200 and r.json()["approved"] == 1
    assert db.tasks.docs[0]["display_name"] == "Pour slab 1"


def test_on_admins_all_vs_limited(client, db, enforced, monkeypatch):
    mir = importlib.import_module("routers.ms_project_internal_router")

    async def _enqueue(tid, pid, **kw):
        return {"job_id": "j", "project_id": pid}
    monkeypatch.setattr(mir.mps, "enqueue_pull_job", _enqueue)
    # all-projects admin: every project
    for pid in (P1, P2, P3):
        assert client.get(f"/api/projects/{pid}/schedule-diffs", headers=_h("admin1")).status_code == 200
    assert client.post(f"/api/projects/{P3}/ms-project/pull", headers=_h("admin1")).status_code == 200
    # admin limited to P2: keeps PM/admin powers there, nothing elsewhere
    assert client.post(f"/api/projects/{P2}/ms-project/pull", headers=_h("admin2")).status_code == 200
    r = client.post(f"/api/projects/{P1}/ms-project/pull", headers=_h("admin2"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert client.get(f"/api/projects/{P1}/schedule-diffs", headers=_h("admin2")).status_code == 404
    assert client.patch("/api/tasks/t2", json={"pct_complete": 40},
                        headers=_h("admin2")).status_code == 200
    assert client.patch("/api/tasks/t1", json={"pct_complete": 40},
                        headers=_h("admin2")).status_code == 403


# ------------------------------------------------------- switch OFF
def test_off_allows_and_records(client, db):
    h = _h("super1")
    assert client.get(f"/api/projects/{P2}/schedule-diffs", headers=h).status_code == 200
    rows = _watch(db, "super1")
    assert len(rows) == 1 and rows[0]["project_id"] == P2
    assert rows[0]["method"] == "GET" and "schedule-diffs" in rows[0]["route"]
    assert not rows[0].get("blocked_count")
    # a save through a body id is let through and recorded too
    r = client.post("/api/tasks", json={"project_id": P2, "activity_code": "N2",
                                        "description": "New"}, headers=_h("pm1"))
    assert r.status_code == 200
    assert any(w["project_id"] == P2 and w["write"] for w in _watch(db, "pm1"))
    r = client.post("/api/activity-name-suggestions/approve",
                    json={"items": [{"id": "ans2"}]}, headers=_h("pm1"))
    assert r.status_code == 200 and r.json()["approved"] == 1
    assert any(w["route"] == "/activity-name-suggestions/approve" for w in _watch(db, "pm1"))
    # assigned project: nothing recorded
    before = len(db.project_access_watch.docs)
    assert client.get(f"/api/projects/{P1}/schedule-diffs", headers=h).status_code == 200
    assert len(db.project_access_watch.docs) == before


# ------------------------------------------------------- live updates
def test_task_progress_broadcast_carries_project(client, db, monkeypatch):
    ws = importlib.import_module("routers.ws_router")
    sent = []

    async def _broadcast(tid, payload):
        sent.append(payload)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    r = client.patch("/api/tasks/t1", json={"pct_complete": 55}, headers=_h("pm1"))
    assert r.status_code == 200
    msg = [m for m in sent if m.get("kind") == "task_progress"][0]
    assert msg["project_id"] == P1 and msg["payload"]["project_id"] == P1


# ------------------------------------------------------- weekly digest
def _emails(rows):
    return sorted(u["email"].split("@")[0] for u in rows)


def test_digest_recipients(db, monkeypatch):
    from services.schedule_digest import digest_recipients
    # admins (admin1 all, admin2 limited to P2) and pm1 (assigned to P1)
    assert _emails(run(digest_recipients(db, A, P1))) == ["admin1", "admin2", "pm1"]
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()
    assert _emails(run(digest_recipients(db, A, P1))) == ["admin1", "pm1"]
    assert _emails(run(digest_recipients(db, A, P2))) == ["admin1", "admin2"]
    assert _emails(run(digest_recipients(db, A, P3))) == ["admin1"]
    # no project given: company list as before
    assert _emails(run(digest_recipients(db, A))) == ["admin1", "admin2", "pm1"]
    assert all("access_scope" not in u for u in run(digest_recipients(db, A, P1)))


def test_digest_send_uses_project_recipients(db, enforced, monkeypatch):
    from services import schedule_digest as sd
    import services.email_provider as ep
    got = []

    class _P:
        def send(self, to_email, **kw):
            got.append(to_email)
            return {"sent": True}
    monkeypatch.setattr(ep, "get_email_provider", lambda: _P())

    async def _build(db_, tid, pid):
        return {"logic_changes": [], "names_pending": 0, "open_delays": [], "slipped": []}
    monkeypatch.setattr(sd, "build_digest", _build)
    res = run(sd.send_project_digest(db, A, {"id": P3, "name": "Three"}))
    assert got == ["admin1@example.com"] and res["sent"] == got


def test_digest_preview_lists_project_recipients(client, db, enforced):
    r = client.get(f"/api/projects/{P2}/schedule-digest/preview", headers=_h("admin2"))
    assert r.status_code == 200
    assert sorted(r.json()["recipients"]) == ["admin1@example.com", "admin2@example.com"]
    assert client.get(f"/api/projects/{P2}/schedule-digest/preview",
                      headers=_h("pm1")).status_code == 404
