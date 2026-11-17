"""Daily-report role permissions (2026-11-16).

A staging test showed viewer, submittal technician and scheduling engineer
accounts could create, edit, submit and delete daily reports and everything
filed on them. Every daily-report write path now runs one check
(auth.ensure_daily_report_writer / require_daily_report_writer, built on
role_can_write_field_events). Read-only roles get a 403 whose body is
{"detail": "Your role can view daily reports but not change them.",
 "code": "role_read_only"} so a phone's offline queue drops the save
instead of keeping it for a retry forever.

Offline: reuses the in-memory fake database and real-router harness from
test_project_access_dr_routes (loaded from its file, not registered in
sys.modules). The per-project access switch stays OFF here, so only the
role rule can refuse.
"""
import importlib
import importlib.util
import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth
from services import project_access as pa

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_dr_routes_harness", os.path.join(_HERE, "test_project_access_dr_routes.py"))
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)  # not added to sys.modules

P1 = D.P1
MESSAGE = "Your role can view daily reports but not change them."
READ_ONLY = ["viewer1", "submittal_tech1", "scheduling_engineer1"]
WRITERS = ["admin1", "pm1", "px1", "super1", "pe1", "foreman1"]

EXTRA_ROUTERS = ["routers.field_events_router", "routers.photo_tray_router",
                 "routers.iter3_router", "routers.iter5_router", "routers.docs_router",
                 "routers.iter8_router"]


db = D.db  # the harness's fake database fixture


def _app():
    from fastapi import APIRouter
    api = APIRouter(prefix="/api")
    for name in D.ROUTERS + EXTRA_ROUTERS:
        api.include_router(importlib.import_module(name).router)
    app = FastAPI()
    app.add_exception_handler(auth.RoleReadOnlyError, auth.role_read_only_handler)
    app.include_router(importlib.import_module(D.VOICE_DR).router,
                       dependencies=[Depends(pa.enforce_project_scope)])
    app.include_router(api, dependencies=[Depends(pa.enforce_project_scope)])
    return app


def _seed_report_records(db):
    """A delay, a safety event and a photo filed on draft dr1 (the generic
    /crud routes are refused for read-only roles on these), plus an unlinked
    delay (outside the daily report rule)."""
    base = {"tenant_id": D.A, "project_id": P1, "created_at": "2026-11-01"}
    db.delays.docs += [
        {**base, "id": "dl1", "daily_report_id": "dr1", "description": "rain"},
        {**base, "id": "dl-free", "description": "no report"},
    ]
    db.safety_events.docs.append({**base, "id": "se1", "daily_report_id": "dr1",
                                  "description": "trip"})
    db.photos.docs.append({**base, "id": "ph-dr", "daily_report_id": "dr1",
                           "caption": "slab"})


@pytest.fixture
def client(db, monkeypatch):
    for name in EXTRA_ROUTERS:
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", lambda: db)
    _seed_report_records(db)
    return TestClient(_app())


def _h(uid):
    return D._h(uid)


def _assert_read_only(r, label):
    assert r.status_code == 403, (label, r.status_code, r.text[:200])
    assert r.json() == {"detail": MESSAGE, "code": "role_read_only"}, label


# ------------------------------------------------------------ the rule
@pytest.mark.parametrize("role,ok", [
    ("admin", True), ("pm", True), ("px", True), ("super", True), ("pe", True),
    ("foreman", True), ("Super ", True), ("viewer", False), ("submittal_tech", False),
    ("scheduling_engineer", False), ("", False), (None, False), ("unknown", False),
])
def test_role_rule(role, ok):
    assert auth.role_can_write_field_events(role) is ok
    ctx = {"tenant_id": "t", "user": {"id": "u", "role": role}}
    if ok:
        auth.ensure_daily_report_writer(ctx)
    else:
        with pytest.raises(auth.RoleReadOnlyError) as e:
            auth.ensure_daily_report_writer(ctx)
        assert e.value.status_code == 403 and e.value.detail == MESSAGE


def test_if_linked_only_refuses_report_linked_records():
    viewer = {"tenant_id": "t", "user": {"id": "u", "role": "viewer"}}
    auth.ensure_daily_report_writer_if_linked(viewer, None, {"project_id": "p"})
    for key in ("daily_report_id", "parent_daily_report_id",
                "linked_daily_report_id", "created_from_daily_report_id"):
        with pytest.raises(auth.RoleReadOnlyError):
            auth.ensure_daily_report_writer_if_linked(viewer, {key: "dr1"})


def test_server_registers_the_handler():
    src = open(os.path.join(_HERE, "..", "server.py"), encoding="utf-8").read()
    assert "app.add_exception_handler(RoleReadOnlyError, role_read_only_handler)" in src


# ------------------------------------------------------------ refused
WRITE_CALLS = [
    ("post", "/api/daily-reports", {"json": {"project_id": P1, "report_date": "2026-11-03"}}),
    ("patch", "/api/daily-reports/dr1", {"json": {"notes": "x"}}),
    ("patch", "/api/daily-reports/dr1", {"json": {"weather_conditions": "rain"}}),
    ("delete", "/api/daily-reports/dr1", {}),
    ("post", "/api/daily-reports/dr1/grid", {"json": {"schedule_activity_id": "t1"}}),
    ("patch", "/api/daily-report-activities/act1", {"json": {"notes": "x"}}),
    ("delete", "/api/daily-report-activities/act1", {}),
    ("post", "/api/daily-reports/dr1/presence", {"json": {"daily_report_id": "dr1", "trade": "x"}}),
    ("post", "/api/daily-presence", {"json": {"daily_report_id": "dr1", "trade": "x"}}),
    ("patch", "/api/daily-presence/pr1", {"json": {"headcount": 3}}),
    ("delete", "/api/daily-presence/pr1", {}),
    ("post", "/api/daily-reports/dr1/unplanned-activity", {"json": {}}),
    ("post", "/api/daily-reports/dr1/weather-autofill", {}),
    ("post", "/api/daily-reports/dr1/submit", {"json": {"is_no_work_day": True}}),
    ("post", "/api/daily-reports/dr1/voice/extract", {"json": {"transcript": "hello"}}),
    ("post", "/api/edit-requests", {"json": {}}),
    ("post", "/api/photos/upload", {"files": {"file": ("p.jpg", b"1", "image/jpeg")}}),
    ("patch", "/api/photos/ph1", {"json": {"caption": "x"}}),
    ("post", "/api/photos/ph1/pin", {"json": {}}),
    ("post", "/api/photo-tray", {"files": {"file": ("p.jpg", b"1", "image/jpeg")},
                                 "data": {"project_id": P1}}),
    ("post", "/api/photo-tags", {"json": {}}),
    ("delete", "/api/photo-tags/ph1", {}),
    ("post", "/api/photo-tray/attach-to-dr", {"json": {}}),
    ("post", "/api/daily-report-attendance", {"json": {}}),
    ("post", "/api/delays", {"json": {"project_id": P1, "daily_report_id": "dr1",
                                      "date": "2026-11-01", "description": "rain"}}),
    ("post", "/api/coordination-events", {"json": {
        "project_id": P1, "occurred_at": "2026-11-01T10:00:00Z", "channel": "email",
        "direction": "inbound", "other_party_name": "Owner", "topic": "other",
        "summary": "call", "linked_daily_report_id": "dr1"}}),
    ("post", "/api/voice/annotate", {"files": {"file": ("v.webm", b"123", "audio/webm")},
                                     "data": {"kind": "voice_note",
                                              "linked_entity_type": "daily_report",
                                              "linked_entity_id": "dr1"}}),
    # The generic edit/delete routes are not a side door (2026-11-16).
    ("patch", "/api/crud/delays/dl1", {"json": {"description": "x"}}),
    ("delete", "/api/crud/delays/dl1", {}),
    ("patch", "/api/crud/safety-events/se1", {"json": {"description": "x"}}),
    ("delete", "/api/crud/safety-events/se1", {}),
    ("patch", "/api/crud/photos/ph-dr", {"json": {"caption": "x"}}),
    ("delete", "/api/crud/photos/ph-dr", {}),
]


@pytest.mark.parametrize("uid", READ_ONLY)
def test_read_only_roles_refused_everywhere(client, db, uid):
    before = {name: len(getattr(db, name).docs) for name in (
        "daily_reports", "daily_report_activities", "daily_presence",
        "coordination_events", "voice_annotations", "delays", "safety_events",
        "photos")}
    snap = {n: [dict(x) for x in getattr(db, n).docs]
            for n in ("delays", "safety_events", "photos")}
    for method, url, kw in WRITE_CALLS:
        r = getattr(client, method)(url, headers=_h(uid), **kw)
        _assert_read_only(r, (uid, method, url))
    after = {name: len(getattr(db, name).docs) for name in before}
    assert after == before
    for n, docs in snap.items():
        assert getattr(db, n).docs == docs, n
    assert next(d for d in db.daily_reports.docs if d["id"] == "dr1")["status"] == "draft"


def test_crud_on_unlinked_record_not_blocked_by_daily_report_rule(client, db):
    """A record with no report link stays outside the daily report rule."""
    r = client.patch("/api/crud/delays/dl-free", json={"description": "edited"},
                     headers=_h("viewer1"))
    assert r.status_code == 200, r.text[:200]


def test_crud_photo_edit_needs_a_report_writer_even_unlinked(client, db):
    db.photos.docs.append({"id": "ph-free", "tenant_id": D.A, "project_id": P1,
                           "caption": "c"})
    r = client.patch("/api/crud/photos/ph-free", json={"caption": "x"},
                     headers=_h("viewer1"))
    _assert_read_only(r, "photo")


def test_viewer_cannot_link_own_unlinked_coordination_event_to_a_report(client, db):
    """Create unlinked (allowed), then PATCH the link on: refused, and the
    event stays unlinked."""
    body = {"project_id": P1, "occurred_at": "2026-11-01T10:00:00Z", "channel": "email",
            "direction": "inbound", "other_party_name": "Owner", "topic": "other",
            "summary": "call"}
    r = client.post("/api/coordination-events", json=body, headers=_h("viewer1"))
    assert r.status_code == 200, r.text[:200]
    eid = r.json()["id"]
    r = client.patch(f"/api/coordination-events/{eid}", json={"linked_daily_report_id": "dr1"},
                     headers=_h("viewer1"))
    _assert_read_only(r, "link")
    ev = next(d for d in db.coordination_events.docs if d["id"] == eid)
    assert not ev.get("linked_daily_report_id")


def test_read_only_roles_can_still_read(client):
    for uid in READ_ONLY:
        assert client.get("/api/daily-reports/dr1", headers=_h(uid)).status_code == 200


def test_unlinked_coordination_event_not_blocked_by_daily_report_rule(client):
    """The rule covers events filed on a daily report; an event with no
    report link is outside it (not refused with role_read_only)."""
    body = {"project_id": P1, "occurred_at": "2026-11-01T10:00:00Z", "channel": "email",
            "direction": "inbound", "other_party_name": "Owner", "topic": "other",
            "summary": "call"}
    r = client.post("/api/coordination-events", json=body, headers=_h("viewer1"))
    assert r.status_code == 200


def test_patch_of_report_linked_coordination_event_refused(client, db):
    db.coordination_events.docs.append({
        "id": "ce-dr", "tenant_id": D.A, "project_id": P1, "occurred_at": "2026-11-01",
        "channel": "email", "direction": "inbound", "other_party_name": "X",
        "topic": "other", "summary": "s", "created_at": "2026-11-01",
        "created_by_user_id": "super1", "deleted_at": None,
        "linked_daily_report_id": "dr1"})
    for method, kw in (("patch", {"json": {"summary": "y"}}), ("delete", {})):
        r = getattr(client, method)("/api/coordination-events/ce-dr",
                                    headers=_h("submittal_tech1"), **kw)
        _assert_read_only(r, method)


# ------------------------------------------------------------ allowed
@pytest.mark.parametrize("uid", WRITERS)
def test_writer_roles_allowed(client, db, uid):
    r = client.patch("/api/daily-reports/dr1", json={"notes": f"by {uid}"}, headers=_h(uid))
    assert r.status_code == 200, r.text[:200]
    assert r.json()["notes"] == f"by {uid}"
    r = client.post("/api/daily-reports/dr1/presence",
                    json={"daily_report_id": "dr1", "trade": "Electric"}, headers=_h(uid))
    assert r.status_code == 200, r.text[:200]
    files = {"file": ("v.webm", b"123", "audio/webm")}
    r = client.post("/api/voice/annotate", files=files,
                    data={"kind": "voice_note", "linked_entity_type": "daily_report",
                          "linked_entity_id": "dr1"}, headers=_h(uid))
    assert r.status_code == 200, r.text[:200]


def test_writer_can_create_report(client):
    r = client.post("/api/daily-reports", json={"project_id": P1, "report_date": "2026-11-04"},
                    headers=_h("foreman1"))
    assert r.status_code == 200 and r.json()["project_id"] == P1
