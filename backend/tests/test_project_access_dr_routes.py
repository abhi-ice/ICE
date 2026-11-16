"""Per-project access on the daily report family of routes (2026-11-16).

Routers covered: daily_reports_router, dr_voice_router, edit_requests_router,
voice_router, gating_entities_router, coordination_router.

What is checked, through the real routers mounted with the route-level
dependency (services/project_access.enforce_project_scope), the way
server.py mounts them:
  * every route with an id in its URL is understood by the dependency
    (or is marked "# project-scope: tenant-level");
  * switch ON: assigned user allowed; unassigned user 404 on reads and
    403 "You are not assigned to this project" on writes (never 404 on a
    write, or a phone would drop its queued save);
  * switch OFF: allowed, and the would-be refusal is recorded in
    project_access_watch;
  * project ids and report ids that arrive in the request BODY (create a
    report, top-level presence, edit requests, coordination events, voice
    notes linked to a record);
  * lists (edit requests, pending count, voice annotations, activity
    chips) are limited to the caller's projects while the switch is ON;
  * all-projects admin vs an admin limited to specific projects.

Offline: the in-memory fake database from the foundation tests is loaded
from its file without being registered in sys.modules, and every module
attribute patched here is undone by monkeypatch.
"""
import importlib
import importlib.util
import inspect
import os
import re

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from services import project_access as pa
from services import runtime_settings

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_pa_foundation_fakes", os.path.join(_HERE, "test_project_access_foundation.py"))
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)  # not added to sys.modules

A, B, P1, P2, PB = F.A, F.B, F.P1, F.P2, F.PB
NOT_ASSIGNED = pa.NOT_ASSIGNED

ROUTERS = [
    "routers.daily_reports_router", "routers.edit_requests_router",
    "routers.voice_router", "routers.gating_entities_router",
    "routers.coordination_router",
]
VOICE_DR = "routers.dr_voice_router"

PATCH_DB = ["auth", "services.project_access", "services.project_scope",
            "services.edit_permissions"] + ROUTERS + [VOICE_DR]


# ------------------------------------------------------------------ world
def _seed_records(db):
    created = "2026-11-01T00:00:00+00:00"
    for rid, pid in (("dr1", P1), ("dr2", P2)):
        db.daily_reports.docs.append({
            "id": rid, "tenant_id": A, "project_id": pid, "report_date": "2026-11-01",
            "status": "draft", "created_at": created})
        db.daily_reports.docs.append({
            "id": rid + "s", "tenant_id": A, "project_id": pid, "report_date": "2026-10-30",
            "status": "submitted", "created_at": created, "submitted_by": "super1"})
    db.daily_report_activities.docs += [
        {"id": "act1", "daily_report_id": "dr1", "activity_code": "A1", "position": 0},
        {"id": "act2", "daily_report_id": "dr2", "activity_code": "A2", "position": 0},
        {"id": "act2s", "daily_report_id": "dr2s", "activity_code": "A2S", "position": 0},
    ]
    db.tasks.docs += [
        {"id": "t1", "tenant_id": A, "project_id": P1, "activity_code": "A1",
         "description": "One", "status": "not_started"},
        {"id": "t2", "tenant_id": A, "project_id": P2, "activity_code": "A2",
         "description": "Two", "status": "not_started"},
    ]
    db.rfis.docs += [{"id": "rfi2", "tenant_id": A, "project_id": P2}]
    db.edit_requests.docs += [
        {"id": "er1", "tenant_id": A, "project_id": P1, "daily_report_id": "dr1s",
         "event_type": "activity", "event_id": "x", "requested_by": "super1",
         "requested_at": created, "reason": "fix the count please", "status": "pending"},
        {"id": "er2", "tenant_id": A, "project_id": P2, "daily_report_id": "dr2s",
         "event_type": "activity", "event_id": "act2s", "requested_by": "foreman1",
         "requested_at": created, "reason": "fix the count please", "status": "pending"},
    ]
    for eid, pid in (("ce1", P1), ("ce2", P2)):
        db.coordination_events.docs.append({
            "id": eid, "tenant_id": A, "project_id": pid, "occurred_at": created,
            "channel": "email", "direction": "inbound", "other_party_name": "X",
            "topic": "other", "summary": "s", "created_at": created,
            "created_by_user_id": "super1", "deleted_at": None})
    db.gating_entities.docs += [
        {"id": "ge2", "tenant_id": A, "project_id": P2, "entity_type": "rfi",
         "entity_id": "rfi2", "status": "open"},
    ]
    db.voice_annotations.docs += [
        {"id": "va1", "tenant_id": A, "created_at": "3", "audio_url": "a/1",
         "linked_entity_type": "daily_report", "linked_entity_id": "dr1"},
        {"id": "va2", "tenant_id": A, "created_at": "2", "audio_url": "a/2",
         "linked_entity_type": "rfi", "linked_entity_id": "rfi2"},
        {"id": "va3", "tenant_id": A, "created_at": "1", "audio_url": "a/3"},
    ]


@pytest.fixture
def db(monkeypatch):
    d = F.DB()
    F._seed(d)
    _seed_records(d)
    for name in PATCH_DB:
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", lambda: d)
    import auth
    monkeypatch.setattr(auth, "_attempt_indexes_ready", False)

    async def _audit(**kw):
        d.audit_log.docs.append({"op": kw.get("op")})
    for name in ROUTERS:
        mod = importlib.import_module(name)
        if hasattr(mod, "write_audit"):
            monkeypatch.setattr(mod, "write_audit", _audit)

    async def _no_tasks(**kw):
        return []
    from services import dr_activity_linkage
    monkeypatch.setattr(dr_activity_linkage, "validate_linked_task_ids", _no_tasks)

    vr = importlib.import_module("routers.voice_router")

    async def _put(*a, **k):
        return None

    async def _get(*a, **k):
        return b"abc", "audio/webm"
    monkeypatch.setattr(vr, "aput_object", _put)
    monkeypatch.setattr(vr, "record_file", _put)
    monkeypatch.setattr(vr, "aget_object", _get)

    ge = importlib.import_module("routers.gating_entities_router")

    async def _recompute(tenant_id, project_id):
        return {"recomputed": 0}
    monkeypatch.setattr(ge, "recompute_all_cascades", _recompute)

    monkeypatch.delenv("FF_PROJECT_ACCESS", raising=False)
    runtime_settings.reset_cache_for_tests()
    yield d
    runtime_settings.reset_cache_for_tests()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()


def _app():
    from fastapi import APIRouter
    api = APIRouter(prefix="/api")
    for name in ROUTERS:
        api.include_router(importlib.import_module(name).router)
    app = FastAPI()
    # Same wiring as server.py.
    app.include_router(importlib.import_module(VOICE_DR).router,
                       dependencies=[Depends(pa.enforce_project_scope)])
    app.include_router(api, dependencies=[Depends(pa.enforce_project_scope)])
    return app


@pytest.fixture
def client(db):
    return TestClient(_app())


def _h(uid):
    return {"Authorization": f"Bearer {F._token(uid)}"}


def _watch(db, user):
    return [r for r in db.project_access_watch.docs if r["user_id"] == user]


# ---------------------------------------------------- route map guard
def test_every_url_id_is_understood_or_marked_tenant_level():
    """Each route here with an id in its URL must name a project id or a
    record kind the dependency can resolve; anything else must carry the
    "# project-scope: tenant-level" marker above its decorator."""
    app = _app()
    marked = set()
    for name in ROUTERS + [VOICE_DR]:
        src = inspect.getsource(importlib.import_module(name))
        lines = src.splitlines()
        for i, line in enumerate(lines):
            m = re.match(r'@router\.\w+\("([^"]+)"', line)
            if m and i and "project-scope: tenant-level" in lines[i - 1]:
                marked.add(m.group(1))
    missing = []
    for route in app.routes:
        path = getattr(route, "path", "")
        params = re.findall(r"\{([^}:]+)", path)
        if not params:
            continue
        sample = {p: "x" for p in params}
        if pa.targets_for_route(path, sample):
            continue
        if any(path.endswith(m) for m in marked):
            continue
        missing.append(path)
    assert missing == []


# ---------------------------------------------------- reads (URL ids)
def test_reads_enforced(client, enforced):
    # assigned (super1 is on P1)
    assert client.get("/api/daily-reports/dr1", headers=_h("super1")).status_code == 200
    # not assigned: 404 for reads, for every URL shape here
    for url in ("/api/daily-reports/dr2", "/api/daily-reports/dr2/grid",
                f"/api/projects/{P2}/daily-reports", "/api/coordination-events/ce2",
                f"/api/projects/{P2}/coordination-events", "/api/gating-entities/ge2",
                f"/api/projects/{P2}/gating-entities", "/api/files/audio/va2"):
        r = client.get(url, headers=_h("super1"))
        assert r.status_code == 404, url
    assert client.get("/api/files/audio/va1", headers=_h("super1")).status_code == 200


def test_reads_watch_only_when_off(client, db):
    assert client.get("/api/daily-reports/dr2", headers=_h("super1")).status_code == 200
    rows = _watch(db, "super1")
    assert len(rows) == 1
    assert rows[0]["project_id"] == P2 and rows[0]["method"] == "GET"
    assert rows[0]["route"] == "/api/daily-reports/{daily_report_id}"
    assert "blocked_count" not in rows[0]


# ---------------------------------------------------- writes
def test_create_report_body_project(client, db, enforced):
    body = {"project_id": P2, "report_date": "2026-11-02"}
    r = client.post("/api/daily-reports", json=body, headers=_h("super1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert not any(d["report_date"] == "2026-11-02" for d in db.daily_reports.docs)
    assert _watch(db, "super1")[0]["blocked_count"] == 1
    # assigned project works
    r = client.post("/api/daily-reports", json={**body, "project_id": P1}, headers=_h("super1"))
    assert r.status_code == 200 and r.json()["project_id"] == P1
    # unknown project is still a 404 (company check first)
    r = client.post("/api/daily-reports", json={**body, "project_id": "nope"}, headers=_h("super1"))
    assert r.status_code == 404
    # another company's project: 404, not a leak
    r = client.post("/api/daily-reports", json={**body, "project_id": PB}, headers=_h("super1"))
    assert r.status_code == 404


def test_create_report_watch_only_when_off(client, db):
    r = client.post("/api/daily-reports", json={"project_id": P2, "report_date": "2026-11-02"},
                    headers=_h("super1"))
    assert r.status_code == 200
    rows = _watch(db, "super1")
    assert rows and rows[0]["project_id"] == P2 and rows[0]["method"] == "POST"
    assert rows[0]["write"] is True


def test_url_writes_get_403_never_404(client, enforced):
    for method, url, body in (
        ("post", "/api/daily-reports/dr2/refresh-grid", None),
        ("post", "/api/daily-reports/dr2/grid", {"schedule_activity_id": "t2"}),
        ("patch", "/api/daily-report-activities/act2", {"notes": "x"}),
        ("delete", "/api/daily-report-activities/act2", None),
        ("post", "/api/daily-reports/dr2/presence", {"daily_report_id": "dr2", "trade": "x"}),
        ("post", "/api/daily-reports/dr2/submit", None),
        ("post", "/api/daily-reports/dr2s/approve", None),
        ("patch", "/api/coordination-events/ce2", {"summary": "y"}),
        ("delete", "/api/coordination-events/ce2", None),
        ("post", f"/api/projects/{P2}/cascade-dashboard/refresh", None),
        ("post", "/api/edit-requests/er2/approve", {"review_note": "ok"}),
        ("post", "/api/daily-reports/dr2/voice/extract", {"transcript": "hello"}),
    ):
        kw = {"headers": _h("pm1")}
        if body is not None:
            kw["json"] = body
        r = getattr(client, method)(url, **kw)
        assert r.status_code == 403, (method, url, r.status_code)
        assert r.json()["detail"] == NOT_ASSIGNED, url
    # voice upload to a report on another project
    r = client.post("/api/daily-reports/dr2/voice/transcribe",
                    files={"audio": ("a.webm", b"123", "audio/webm")}, headers=_h("super1"))
    assert r.status_code == 403


def test_top_level_presence_body_report(client, db, enforced):
    r = client.post("/api/daily-presence", json={"daily_report_id": "dr2", "trade": "Electric"},
                    headers=_h("super1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert db.daily_presence.docs == []
    r = client.post("/api/daily-presence", json={"daily_report_id": "dr1", "trade": "Electric"},
                    headers=_h("super1"))
    assert r.status_code == 200


def test_grid_add_task_from_other_project(client, db, enforced):
    # report on P1 (allowed) but the activity belongs to P2
    r = client.post("/api/daily-reports/dr1/grid", json={"schedule_activity_id": "t2"},
                    headers=_h("super1"))
    assert r.status_code == 403
    r = client.post("/api/daily-reports/dr1/grid", json={"schedule_activity_id": "t1"},
                    headers=_h("super1"))
    assert r.status_code == 200


def test_edit_request_body_report(client, db, enforced):
    body = {"daily_report_id": "dr2s", "event_type": "activity", "event_id": "act2s",
            "reason": "the counts were wrong on this one"}
    r = client.post("/api/edit-requests", json=body, headers=_h("super1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert len(db.edit_requests.docs) == 2
    r = client.post("/api/edit-requests", json=body, headers=_h("admin2"))  # limited to P2
    assert r.status_code == 200


def test_edit_request_lists_filtered_only_when_on(client, monkeypatch):
    assert {e["id"] for e in client.get("/api/edit-requests", headers=_h("pm1")).json()} == {"er1", "er2"}
    assert client.get("/api/edit-requests/pending-count", headers=_h("pm1")).json() == {"pending": 2}
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()
    assert [e["id"] for e in client.get("/api/edit-requests", headers=_h("pm1")).json()] == ["er1"]
    assert client.get("/api/edit-requests/pending-count", headers=_h("pm1")).json() == {"pending": 1}
    assert [e["id"] for e in client.get("/api/edit-requests", headers=_h("admin2")).json()] == ["er2"]
    assert len(client.get("/api/edit-requests", headers=_h("admin1")).json()) == 2


def test_coordination_create_body_ids(client, db, enforced):
    body = {"project_id": P2, "occurred_at": "2026-11-01T10:00:00Z", "channel": "email",
            "direction": "inbound", "other_party_name": "Owner", "topic": "other",
            "summary": "call"}
    r = client.post("/api/coordination-events", json=body, headers=_h("super1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    # allowed project but a linked report from another project
    r = client.post("/api/coordination-events",
                    json={**body, "project_id": P1, "linked_daily_report_id": "dr2"},
                    headers=_h("super1"))
    assert r.status_code == 403
    assert len(db.coordination_events.docs) == 2
    r = client.post("/api/coordination-events", json={**body, "project_id": P1},
                    headers=_h("super1"))
    assert r.status_code == 200
    r = client.post("/api/coordination-events", json={**body, "project_id": "nope"},
                    headers=_h("super1"))
    assert r.status_code == 404


def test_voice_note_linked_record(client, db, enforced):
    files = {"file": ("v.webm", b"123", "audio/webm")}
    r = client.post("/api/voice/annotate", files=files,
                    data={"kind": "voice_note", "linked_entity_type": "rfi",
                          "linked_entity_id": "rfi2"}, headers=_h("super1"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    r = client.post("/api/voice/annotate", files=files,
                    data={"kind": "voice_note", "linked_entity_type": "daily_report",
                          "linked_entity_id": "dr1"}, headers=_h("super1"))
    assert r.status_code == 200 and r.json()["project_id"] == P1
    # an unlinked note is not tied to a project
    r = client.post("/api/voice/annotate", files=files, data={"kind": "voice_note"},
                    headers=_h("super1"))
    assert r.status_code == 200 and r.json()["project_id"] is None


def test_voice_list_filtered_only_when_on(client, monkeypatch):
    ids = lambda uid: [r["id"] for r in client.get("/api/voice/annotations", headers=_h(uid)).json()]
    assert ids("super1") == ["va1", "va2", "va3"]
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()
    # va2 is linked to an RFI on P2; va3 has no project and stays visible
    assert ids("super1") == ["va1", "va3"]
    assert ids("admin2") == ["va2", "va3"]
    assert ids("admin1") == ["va1", "va2", "va3"]


def test_activity_chips_hide_other_projects(client, enforced):
    r = client.get(f"/api/projects/{P1}/dr-activity-chips?ids=act1,act2", headers=_h("super1"))
    assert r.status_code == 200 and [c["id"] for c in r.json()] == ["act1"]
    r = client.get(f"/api/projects/{P1}/dr-activity-chips?ids=act1,act2", headers=_h("admin1"))
    assert [c["id"] for c in r.json()] == ["act1", "act2"]


# ---------------------------------------------------- admins
def test_all_projects_admin_vs_limited_admin(client, enforced):
    # all-projects admin: everything
    assert client.get("/api/daily-reports/dr1", headers=_h("admin1")).status_code == 200
    assert client.get("/api/daily-reports/dr2", headers=_h("admin1")).status_code == 200
    # limited admin (P2 only): keeps admin powers on P2, nothing on P1
    assert client.get("/api/daily-reports/dr2", headers=_h("admin2")).status_code == 200
    assert client.get("/api/daily-reports/dr1", headers=_h("admin2")).status_code == 404
    r = client.post("/api/daily-reports/dr2s/approve", headers=_h("admin2"))
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = client.post("/api/daily-reports/dr1s/approve", headers=_h("admin2"))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    r = client.post("/api/edit-requests/er2/approve", json={}, headers=_h("admin2"))
    assert r.status_code == 200
    r = client.post("/api/edit-requests/er1/approve", json={}, headers=_h("admin2"))
    assert r.status_code == 403
    # admin edit rule on coordination: limited admin edits on P2 only
    assert client.patch("/api/coordination-events/ce2", json={"summary": "z"},
                        headers=_h("admin2")).status_code == 200
    assert client.patch("/api/coordination-events/ce1", json={"summary": "z"},
                        headers=_h("admin2")).status_code == 403


def test_other_company_admin_gets_nothing(client, enforced):
    assert client.get("/api/daily-reports/dr1", headers=_h("adminB")).status_code == 404
    r = client.post("/api/daily-reports", json={"project_id": P1, "report_date": "2026-11-02"},
                    headers=_h("adminB"))
    assert r.status_code == 404
