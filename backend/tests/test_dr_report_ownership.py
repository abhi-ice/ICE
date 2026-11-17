"""Daily-report ownership rules (2026-11-16).

(a) Deleting a submitted or approved report (even with a reason) is for an
    admin or project manager only; everyone else gets 403 "Only an admin or
    project manager can delete a submitted report.". A draft may also be
    deleted by the person who started it (prepared_by / created_by).
(b) A draft is changed (report fields, grid rows, crew, attendance, submit,
    weather, refresh) only by its author, an admin or a project manager.
    Another superintendent on the same project gets 403 "Only the person who
    started this report, an admin or a project manager can change it.".
(c) Removing one grid row from a submitted or approved report follows the
    rules for editing a row: a reason is required, and on someone else's
    report only an admin or project manager may do it.

Offline: reuses the in-memory fake database and real-router harness from
test_project_access_dr_routes (loaded from its file, not registered in
sys.modules).
"""
import asyncio
import importlib
import importlib.util
import os
from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import auth
from services import edit_permissions as ep
from services import project_access as pa

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_dr_routes_harness_owner", os.path.join(_HERE, "test_project_access_dr_routes.py"))
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)  # not added to sys.modules

A, P1, P2 = D.A, D.P1, D.P2
DRAFT_MSG = ("Only the person who started this report, an admin or a project manager "
             "can change it.")
DRAFT_DELETE_MSG = ("Only the person who started this report, an admin or a project "
                    "manager can delete it.")
LOCKED_DELETE_MSG = "Only an admin or project manager can delete a submitted report."
REASON = "wrong activity was logged"

EXTRA_ROUTERS = ["routers.iter5_router", "routers.field_events_router",
                 "routers.photo_tray_router"]

enforced = D.enforced


def _now():
    return datetime.now(timezone.utc).isoformat()


db = D.db  # the harness's fake database fixture (switch OFF by default)


@pytest.fixture
def world(db, monkeypatch):
    d = db
    # A second superintendent on project 1.
    d.users.docs.append({"id": "super2", "email": "super2@example.com", "full_name": "Super2",
                         "tenant_id": A, "role": "super", "created_at": "1",
                         "password_hash": "x"})
    d.tenant_users.docs.append({"id": "tu-super2", "user_id": "super2", "tenant_id": A,
                                "role": "super", "status": "active"})
    d.project_members.docs.append({"id": "pm-super2", "tenant_id": A, "project_id": P1,
                                   "user_id": "super2", "role_on_project": "super",
                                   "added_at": "1"})
    base = {"tenant_id": A, "project_id": P1, "created_at": "2026-11-05T00:00:00+00:00"}
    d.daily_reports.docs += [
        {**base, "id": "drA", "report_date": "2026-11-05", "status": "draft",
         "prepared_by": "super1"},
        {**base, "id": "drB", "report_date": "2026-11-06", "status": "draft",
         "prepared_by": "super1"},
        {**base, "id": "drL", "report_date": "2026-11-04", "status": "submitted",
         "prepared_by": "super1", "submitted_by": "super1", "submitted_at": _now()},
        {**base, "id": "drAp", "report_date": "2026-11-03", "status": "approved",
         "prepared_by": "super1", "submitted_by": "super1", "submitted_at": _now()},
        {**base, "id": "drO", "report_date": "2026-11-02", "status": "submitted",
         "prepared_by": "super2", "submitted_by": "super2", "submitted_at": _now()},
    ]
    d.daily_report_activities.docs += [
        {"id": "rowA", "daily_report_id": "drA", "activity_code": "A1", "position": 0},
        {"id": "rowL", "daily_report_id": "drL", "activity_code": "A1", "position": 0},
        {"id": "rowL2", "daily_report_id": "drL", "activity_code": "A1", "position": 1},
        {"id": "rowO", "daily_report_id": "drO", "activity_code": "A1", "position": 0},
    ]
    d.daily_presence.docs += [
        {"id": "prA", "tenant_id": A, "daily_report_id": "drA", "project_id": P1,
         "trade": "Electric", "headcount": 2, "hours_per_person": 8},
    ]
    d.daily_report_attendance.docs += [
        {"id": "atA", "tenant_id": A, "daily_report_id": "drA", "name": "Pat"},
    ]
    for name in EXTRA_ROUTERS:
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", lambda: d)
    return d


def _app():
    from fastapi import APIRouter
    api = APIRouter(prefix="/api")
    for name in D.ROUTERS + EXTRA_ROUTERS:
        api.include_router(importlib.import_module(name).router)
    app = FastAPI()
    app.add_exception_handler(auth.RoleReadOnlyError, auth.role_read_only_handler)
    # Same wiring as server.py (codes for the phone's offline queue).
    app.add_exception_handler(ep.NotReportAuthorError, ep.not_report_author_handler)
    app.add_exception_handler(ep.ReportNotDraftError, ep.not_report_author_handler)
    from services.ai_unavailable import AiUnavailableError, ai_unavailable_handler
    app.add_exception_handler(AiUnavailableError, ai_unavailable_handler)
    app.include_router(importlib.import_module(D.VOICE_DR).router,
                       dependencies=[Depends(pa.enforce_project_scope)])
    app.include_router(api, dependencies=[Depends(pa.enforce_project_scope)])
    return app


@pytest.fixture
def client(world):
    return TestClient(_app())


def _h(uid):
    return D._h(uid)


def _ids(coll):
    return {d["id"] for d in coll.docs}


def _refused(r, msg, label=""):
    assert r.status_code == 403, (label, r.status_code, r.text[:300])
    assert r.json()["detail"] == msg, (label, r.text[:300])


# ------------------------------------------------------------ the rules
def _ctx(uid, role):
    return {"tenant_id": A, "user": {"id": uid, "role": role}}


def test_rules_unit():
    draft = {"status": "draft", "prepared_by": "u1"}
    legacy = {"status": "draft", "created_by": "u1"}
    no_author = {"status": "draft"}
    locked = {"status": "submitted", "prepared_by": "u1", "submitted_by": "u1"}
    approved = {"status": "approved", "prepared_by": "u1"}
    # draft changes
    assert ep.can_change_draft(_ctx("u1", "super"), draft)
    assert ep.can_change_draft(_ctx("u1", "foreman"), legacy)
    assert not ep.can_change_draft(_ctx("u2", "super"), draft)
    assert not ep.can_change_draft(_ctx("u2", "super"), legacy)
    assert ep.can_change_draft(_ctx("u2", "pm"), draft)
    assert ep.can_change_draft(_ctx("u2", "Admin"), draft)
    assert ep.can_change_draft(_ctx("u2", "super"), no_author)  # older row, no author
    assert ep.can_change_draft(_ctx("u2", "super"), locked)  # locked rules decide
    with pytest.raises(HTTPException) as e:
        ep.ensure_can_change_draft(_ctx("u2", "foreman"), draft)
    assert e.value.status_code == 403 and e.value.detail == DRAFT_MSG
    # delete
    ep.ensure_can_delete_report(_ctx("u1", "super"), draft)
    ep.ensure_can_delete_report(_ctx("u2", "pm"), draft)
    for rep in (locked, approved):
        ep.ensure_can_delete_report(_ctx("u2", "pm"), rep)
        ep.ensure_can_delete_report(_ctx("u2", "admin"), rep)
        for role in ("super", "foreman", "pe", "px"):
            with pytest.raises(HTTPException) as e:
                ep.ensure_can_delete_report(_ctx("u1", role), rep)
            assert e.value.status_code == 403 and e.value.detail == LOCKED_DELETE_MSG
    with pytest.raises(HTTPException) as e:
        ep.ensure_can_delete_report(_ctx("u2", "super"), draft)
    assert e.value.detail == DRAFT_DELETE_MSG


# ------------------------------------------------------------ (a) delete report
@pytest.mark.parametrize("uid", ["super1", "foreman1", "super2", "pe1"])
def test_submitted_or_approved_delete_refused_for_field_roles(client, db, uid):
    for rid in ("drL", "drAp"):
        r = client.delete(f"/api/daily-reports/{rid}", params={"reason": REASON},
                          headers=_h(uid))
        _refused(r, LOCKED_DELETE_MSG, (uid, rid))
    assert {"drL", "drAp"} <= _ids(db.daily_reports)


@pytest.mark.parametrize("uid,rid", [("pm1", "drL"), ("admin1", "drAp")])
def test_submitted_or_approved_delete_by_admin_or_pm(client, db, uid, rid):
    r = client.delete(f"/api/daily-reports/{rid}", headers=_h(uid))
    assert r.status_code == 422, r.text[:200]  # still needs a reason
    r = client.delete(f"/api/daily-reports/{rid}", params={"reason": REASON}, headers=_h(uid))
    assert r.status_code == 200, r.text[:300]
    assert rid not in _ids(db.daily_reports)


def test_limited_admin_deletes_only_on_assigned_project(client, db, enforced):
    # admin2 is limited to project 2.
    r = client.delete("/api/daily-reports/drL", params={"reason": REASON}, headers=_h("admin2"))
    assert r.status_code == 403 and r.json()["detail"] == pa.NOT_ASSIGNED
    r = client.delete("/api/daily-reports/dr2s", params={"reason": REASON}, headers=_h("admin2"))
    assert r.status_code == 200, r.text[:300]
    assert "drL" in _ids(db.daily_reports) and "dr2s" not in _ids(db.daily_reports)


def test_draft_delete_by_other_super_refused(client, db):
    r = client.delete("/api/daily-reports/drA", headers=_h("super2"))
    _refused(r, DRAFT_DELETE_MSG)
    r = client.delete("/api/daily-reports/drA", headers=_h("foreman1"))
    _refused(r, DRAFT_DELETE_MSG)
    assert "drA" in _ids(db.daily_reports)


@pytest.mark.parametrize("uid", ["super1", "pm1", "admin1"])
def test_draft_delete_by_author_or_admin_or_pm(client, db, uid):
    r = client.delete("/api/daily-reports/drA", headers=_h(uid))
    assert r.status_code == 200, r.text[:300]
    assert "drA" not in _ids(db.daily_reports)


# ------------------------------------------------------------ (b) edit a draft
OTHER_EDITS = [
    ("patch", "/api/daily-reports/drA", {"json": {"notes": "x"}}),
    ("patch", "/api/daily-reports/drA", {"json": {"weather_conditions": "rain"}}),
    ("post", "/api/daily-reports/drA/refresh-grid", {}),
    ("post", "/api/daily-reports/drA/grid", {"json": {"schedule_activity_id": "t1"}}),
    ("patch", "/api/daily-report-activities/rowA", {"json": {"notes": "x"}}),
    ("delete", "/api/daily-report-activities/rowA", {}),
    ("post", "/api/daily-reports/drA/presence", {"json": {"daily_report_id": "drA", "trade": "x"}}),
    ("post", "/api/daily-presence", {"json": {"daily_report_id": "drA", "trade": "x"}}),
    ("patch", "/api/daily-presence/prA", {"json": {"headcount": 3}}),
    ("delete", "/api/daily-presence/prA", {}),
    ("post", "/api/daily-reports/drA/unplanned-activity",
     {"json": {"description": "Extra work", "duration_days": 1}}),
    ("post", "/api/daily-reports/drA/weather-autofill", {}),
    ("post", "/api/daily-reports/drA/submit", {"json": {"is_no_work_day": True,
                                                         "no_work_reason": "weather"}}),
    ("post", "/api/daily-report-attendance", {"json": {"daily_report_id": "drA", "name": "Lee"}}),
    ("delete", "/api/daily-report-attendance/atA", {}),
]


@pytest.mark.parametrize("uid", ["super2", "foreman1", "pe1"])
def test_other_field_users_cannot_change_a_draft(client, db, uid):
    snap = {n: [dict(x) for x in getattr(db, n).docs] for n in (
        "daily_reports", "daily_report_activities", "daily_presence",
        "daily_report_attendance", "tasks")}
    for method, url, kw in OTHER_EDITS:
        r = getattr(client, method)(url, headers=_h(uid), **kw)
        _refused(r, DRAFT_MSG, (uid, method, url))
    for n, docs in snap.items():
        assert getattr(db, n).docs == docs, n


def test_author_keeps_working_on_own_draft(client, db):
    h = _h("super1")
    r = client.patch("/api/daily-reports/drA", json={"notes": "mine"}, headers=h)
    assert r.status_code == 200 and r.json()["notes"] == "mine"
    r = client.post("/api/daily-reports/drA/presence",
                    json={"daily_report_id": "drA", "trade": "Electric"}, headers=h)
    assert r.status_code == 200, r.text[:300]
    new_pr = r.json()["id"]
    r = client.post("/api/daily-presence", json={"daily_report_id": "drA", "trade": "Plumbing"},
                    headers=h)
    assert r.status_code == 200, r.text[:300]
    r = client.patch(f"/api/daily-presence/{new_pr}", json={"headcount": 4}, headers=h)
    assert r.status_code == 200, r.text[:300]
    r = client.delete("/api/daily-presence/prA", headers=h)
    assert r.status_code == 200, r.text[:300]
    r = client.post("/api/daily-reports/drB/grid", json={"schedule_activity_id": "t1"}, headers=h)
    assert r.status_code == 200, r.text[:300]
    r = client.delete("/api/daily-report-activities/rowA", headers=h)
    assert r.status_code == 200, r.text[:300]
    r = client.post("/api/daily-report-attendance", json={"daily_report_id": "drA", "name": "Lee"},
                    headers=h)
    assert r.status_code == 200, r.text[:300]
    r = client.delete("/api/daily-report-attendance/atA", headers=h)
    assert r.status_code == 200, r.text[:300]


@pytest.mark.parametrize("uid", ["pm1", "admin1"])
def test_admin_and_pm_can_change_anyones_draft(client, db, uid):
    h = _h(uid)
    r = client.patch("/api/daily-reports/drA", json={"notes": f"by {uid}"}, headers=h)
    assert r.status_code == 200 and r.json()["notes"] == f"by {uid}"
    r = client.post("/api/daily-reports/drA/presence",
                    json={"daily_report_id": "drA", "trade": "Electric"}, headers=h)
    assert r.status_code == 200, r.text[:300]
    r = client.delete("/api/daily-report-activities/rowA", headers=h)
    assert r.status_code == 200, r.text[:300]


def test_create_or_open_still_returns_someone_elses_draft(client, db):
    """Opening the day's report is a read: a second superintendent still gets
    the existing draft back (and can then read, but not change, it)."""
    r = client.post("/api/daily-reports", json={"project_id": P1, "report_date": "2026-11-05"},
                    headers=_h("super2"))
    assert r.status_code == 200 and r.json()["id"] == "drA"
    assert client.get("/api/daily-reports/drA", headers=_h("super2")).status_code == 200


# ------------------------------------------------------------ (c) row delete after submit
def test_locked_row_delete_needs_reason(client, db):
    r = client.delete("/api/daily-report-activities/rowL", headers=_h("super1"))
    assert r.status_code == 422, r.text[:200]
    r = client.delete("/api/daily-report-activities/rowL", headers=_h("pm1"))
    assert r.status_code == 422, r.text[:200]
    assert {"rowL", "rowL2"} <= _ids(db.daily_report_activities)


def test_locked_row_delete_by_owner_within_window(client, db):
    r = client.delete("/api/daily-report-activities/rowL", params={"reason": REASON},
                      headers=_h("super1"))
    assert r.status_code == 200, r.text[:300]
    r = client.delete("/api/daily-report-activities/rowL2", params={"edit_reason": REASON},
                      headers=_h("super1"))
    assert r.status_code == 200, r.text[:300]
    assert not ({"rowL", "rowL2"} & _ids(db.daily_report_activities))
    ops = [a["op"] for a in db.audit_log.docs]
    assert ops.count("ACTIVITY_GRID_DELETED_POST_SUBMIT") == 2


@pytest.mark.parametrize("uid", ["super1", "foreman1", "pe1"])
def test_locked_row_delete_on_someone_elses_report_refused(client, db, uid):
    r = client.delete("/api/daily-report-activities/rowO", params={"reason": REASON},
                      headers=_h(uid))
    assert r.status_code == 403, r.text[:300]
    assert "rowO" in _ids(db.daily_report_activities)


@pytest.mark.parametrize("uid", ["pm1", "admin1"])
def test_locked_row_delete_by_admin_or_pm(client, db, uid):
    r = client.delete("/api/daily-report-activities/rowO", params={"reason": REASON},
                      headers=_h(uid))
    assert r.status_code == 200, r.text[:300]
    assert "rowO" not in _ids(db.daily_report_activities)


def test_locked_row_delete_after_window_needs_edit_request(client, db):
    rep = next(d for d in db.daily_reports.docs if d["id"] == "drL")
    rep["submitted_at"] = "2026-01-01T00:00:00+00:00"
    r = client.delete("/api/daily-report-activities/rowL", params={"reason": REASON},
                      headers=_h("super1"))
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "edit_request_required"
    assert "rowL" in _ids(db.daily_report_activities)


# ------------------------------------------------------------ batch 2 (2026-11-16)
def _refused_not_author(r, label=""):
    _refused(r, DRAFT_MSG, label)
    assert r.json().get("code") == "not_report_author", (label, r.text[:300])


def _snap(db, names):
    return {n: [dict(x) for x in getattr(db, n).docs] for n in names}


VOICE_TOUCHED = ("daily_reports", "daily_report_activities", "daily_presence",
                 "delays", "safety_events", "qc_issues", "deliveries", "rfis",
                 "coordination_events")


def test_voice_routes_refuse_someone_elses_draft(client, db, monkeypatch):
    """Voice dictation follows the same author rule as editing by hand, and
    the check runs before any transcription is paid for."""
    vr = importlib.import_module(D.VOICE_DR)

    async def _no_transcribe(*_a, **_k):
        raise AssertionError("must not transcribe someone else's draft")
    monkeypatch.setattr(vr, "_gemini_transcribe_audio", _no_transcribe)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    before = _snap(db, VOICE_TOUCHED)
    r = client.post("/api/daily-reports/drA/voice/extract",
                    json={"transcript": "poured the slab, 4 carpenters"}, headers=_h("super2"))
    _refused_not_author(r, "extract")
    r = client.post("/api/daily-reports/drA/voice/transcribe-and-extract",
                    files={"audio": ("v.webm", b"123", "audio/webm")}, headers=_h("super2"))
    _refused_not_author(r, "transcribe-and-extract")
    assert _snap(db, VOICE_TOUCHED) == before


def test_voice_report_checks_come_before_the_ai_check(client, db, monkeypatch):
    """With no AI key, a clip for a missing or submitted report is answered
    for what it is (404 / 409 "report_not_draft"), never "ai_unavailable",
    which would keep it on the phone for an AI retry that can't succeed."""
    for k in ("ANTHROPIC_API_KEY", "EMERGENT_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    up = {"audio": ("v.webm", b"123", "audio/webm")}
    r = client.post("/api/daily-reports/nope/voice/transcribe-and-extract", files=up,
                    headers=_h("admin1"))
    assert r.status_code == 404 and r.json().get("code") != "ai_unavailable", r.text[:200]
    for rid in ("drL", "drAp"):
        r = client.post(f"/api/daily-reports/{rid}/voice/transcribe-and-extract", files=up,
                        headers=_h("super1"))
        assert r.status_code == 409, r.text[:200]
        assert r.json()["code"] == "report_not_draft"
        r = client.post(f"/api/daily-reports/{rid}/voice/extract", json={"transcript": "x"},
                        headers=_h("super1"))
        assert r.status_code == 409 and r.json()["code"] == "report_not_draft"
    # The author's own draft with no key: the plain AI answer.
    r = client.post("/api/daily-reports/drA/voice/extract", json={"transcript": "x"},
                    headers=_h("super1"))
    assert r.status_code == 422 and r.json()["code"] == "ai_unavailable"


def test_ownership_refusals_carry_the_code(client, db):
    r = client.patch("/api/daily-reports/drA", json={"notes": "x"}, headers=_h("super2"))
    _refused_not_author(r)
    r = client.delete("/api/daily-reports/drL", headers=_h("super1"))
    assert r.status_code == 403 and r.json()["code"] == "not_report_author"


# Delays, safety events, quality issues, deliveries and photos filed on a
# draft follow the draft's author rule (services/edit_permissions
# ensure_can_change_linked_report).
def _seed_linked(db):
    base = {"tenant_id": A, "project_id": P1, "created_at": "2026-11-05",
            "daily_report_id": "drA"}
    db.delays.docs.append({**base, "id": "dlA", "description": "rain", "status": "open",
                           "date": "2026-11-05"})
    db.safety_events.docs.append({**base, "id": "seA", "description": "trip",
                                  "severity": "minor", "event_type": "near_miss"})
    db.qc_issues.docs.append({**base, "id": "qcA", "description": "crack",
                              "status": "open"})
    db.deliveries.docs.append({**base, "id": "dvA", "vendor": "V", "status": "expected"})
    db.photos.docs.append({**base, "id": "phA", "caption": "slab", "tray_photo_id": "trA"})
    db.photo_tray.docs.append({"id": "trA", "tenant_id": A, "project_id": P1,
                               "status": "partially_tagged"})


LINKED_EDITS = [
    ("post", "/api/delays", {"json": {"project_id": P1, "daily_report_id": "drA",
                                      "date": "2026-11-05", "description": "wind"}}),
    ("patch", "/api/delays/dlA", {"json": {"description": "x"}}),
    ("delete", "/api/delays/dlA", {}),
    ("patch", "/api/safety-events/seA", {"json": {"description": "x"}}),
    ("patch", "/api/qc-issues/qcA", {"json": {"description": "x"}}),
    ("patch", "/api/deliveries/dvA", {"json": {"vendor": "W"}}),
    ("patch", "/api/photos/phA", {"json": {"caption": "x"}}),
    ("delete", "/api/photo-tags/phA", {}),
    ("post", "/api/photo-tray/attach-to-dr", {"json": {"tray_ids": ["trA"],
                                                       "daily_report_id": "drA"}}),
]


def test_linked_field_events_follow_the_draft_author(client, db):
    _seed_linked(db)
    names = ("delays", "safety_events", "qc_issues", "deliveries", "photos", "photo_tray")
    before = _snap(db, names)
    for method, url, kw in LINKED_EDITS:
        r = getattr(client, method)(url, headers=_h("super2"), **kw)
        _refused_not_author(r, (method, url))
    assert _snap(db, names) == before


def test_linked_report_rule_unit(world):
    """Author, admin and project manager pass; another writer is refused; a
    submitted report and an unlinked record are left to the other rules."""
    for uid, role in (("super1", "super"), ("pm1", "pm"), ("admin1", "admin")):
        asyncio.run(ep.ensure_can_change_linked_report(_ctx(uid, role), {"daily_report_id": "drA"}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(ep.ensure_can_change_linked_report(_ctx("super2", "super"),
                                                       None, {"daily_report_id": "drA"}))
    assert e.value.status_code == 403 and e.value.code == "not_report_author"
    asyncio.run(ep.ensure_can_change_linked_report(_ctx("super2", "super"),
                                                   {"daily_report_id": "drL"}))
    asyncio.run(ep.ensure_can_change_linked_report(_ctx("super2", "super"), {"project_id": P1}))


# ------------------------------------------------------------ who owns the day
def test_pm_opens_the_day_then_superintendent_edits_it(client, db):
    """A project manager opening the day first is not counted as its author,
    so the superintendent can still fill in and submit the day's report."""
    r = client.post("/api/daily-reports", json={"project_id": P1, "report_date": "2026-11-09"},
                    headers=_h("pm1"))
    assert r.status_code == 200, r.text[:300]
    rid = r.json()["id"]
    doc = next(d for d in db.daily_reports.docs if d["id"] == rid)
    assert doc["prepared_by"] is None and doc["opened_by"] == "pm1"
    # The superintendent opens the same day and gets the same report back.
    r = client.post("/api/daily-reports", json={"project_id": P1, "report_date": "2026-11-09"},
                    headers=_h("super1"))
    assert r.json()["id"] == rid
    r = client.patch(f"/api/daily-reports/{rid}", json={"notes": "poured footings"},
                     headers=_h("super1"))
    assert r.status_code == 200 and r.json()["notes"] == "poured footings"
    r = client.post(f"/api/daily-reports/{rid}/presence",
                    json={"daily_report_id": rid, "trade": "Concrete"}, headers=_h("super1"))
    assert r.status_code == 200, r.text[:300]


def test_field_writer_opening_the_day_is_its_author(client, db):
    r = client.post("/api/daily-reports", json={"project_id": P1, "report_date": "2026-11-10"},
                    headers=_h("foreman1"))
    doc = next(d for d in db.daily_reports.docs if d["id"] == r.json()["id"])
    assert doc["prepared_by"] == "foreman1" and not doc.get("opened_by")


def test_admin_or_pm_hands_a_draft_to_another_writer(client, db):
    r = client.patch("/api/daily-reports/drA/author", json={"user_id": "super2"},
                     headers=_h("super1"))
    assert r.status_code == 403
    r = client.patch("/api/daily-reports/drA/author", json={"user_id": "viewer1"},
                     headers=_h("pm1"))
    assert r.status_code == 422
    r = client.patch("/api/daily-reports/drL/author", json={"user_id": "super2"},
                     headers=_h("pm1"))
    assert r.status_code == 409
    r = client.patch("/api/daily-reports/drA/author",
                     json={"user_id": "super2", "reason": "super1 is out sick"},
                     headers=_h("pm1"))
    assert r.status_code == 200 and r.json()["prepared_by"] == "super2"
    assert any(a.get("op") == "REPORT_AUTHOR_CHANGED" for a in db.audit_log.docs)
    # The new author can now change it; the old one cannot.
    r = client.patch("/api/daily-reports/drA", json={"notes": "by super2"}, headers=_h("super2"))
    assert r.status_code == 200
    r = client.patch("/api/daily-reports/drA", json={"notes": "by super1"}, headers=_h("super1"))
    _refused_not_author(r)


def test_single_report_read_names_the_author(client, db):
    r = client.get("/api/daily-reports/drA", headers=_h("super2"))
    assert r.status_code == 200
    body = r.json()
    assert body["prepared_by"] == "super1" and body["prepared_by_name"]
