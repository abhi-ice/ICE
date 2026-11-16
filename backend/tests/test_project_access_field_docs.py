"""Per-project access in the field-event, document, upload, markup,
submittal, mitigation and TIA routers (2026-11-16).

The real routers are mounted in a small FastAPI app with the same
route-level dependency as server.py, on the in-memory fake database from
test_project_access_foundation. Covers:
  * every route in these routers is either checked from its URL by the
    dependency or checks its body itself (guard test);
  * switch ON: assigned user allowed; unassigned refused with 404 on a read
    and 403 "You are not assigned to this project" on a write (a phone keeps
    a 403 save queued; a 404 would drop it);
  * body and form fields: project ids and the records they point at
    (daily report, drawing, drawing revision, RFI, linked records);
  * switch OFF: allowed, and the would-be refusal is recorded;
  * all-projects admin vs an admin limited to specific projects.
Nothing is left in sys.modules: only real modules are imported.
"""
import importlib

import pytest
from fastapi import APIRouter, FastAPI, Depends
from fastapi.testclient import TestClient

import auth
from services import project_access as pa
from services import runtime_settings

from test_project_access_foundation import DB, _seed, _token, A, P1, P2

ROUTER_MODULES = [
    "routers.field_events_router", "routers.docs_router", "routers.upload_router",
    "routers.iter3_router", "routers.iter4_router", "routers.mitigations_router",
    "routers.tia_router",
]
PATCH_DB = ["auth", "services.project_access", "services.project_scope"] + ROUTER_MODULES

NOT_ASSIGNED = "You are not assigned to this project"

# Routes whose project or records come from the request body or a form
# field; each checks them in the handler (see the router docstrings).
BODY_CHECKED = {
    ("POST", "/api/delays"), ("POST", "/api/safety-events"), ("POST", "/api/qc-issues"),
    ("POST", "/api/photos"), ("POST", "/api/rfis"), ("POST", "/api/change-orders"),
    ("POST", "/api/work-items"), ("POST", "/api/work-item-links"), ("POST", "/api/drawings"),
    ("POST", "/api/specs"), ("POST", "/api/drawing-revisions/upload"),
    ("POST", "/api/drawing-markups"), ("POST", "/api/photos/upload"),
    ("POST", "/api/specs/search"), ("POST", "/api/submittals"),
    ("POST", "/api/drawings-spatial-metadata"),
}


def _world(db):
    """Records in P1 (where every non-admin of company A is assigned) and
    in P2 (where only the limited admin admin2 is assigned)."""
    for pid, sfx in ((P1, "1"), (P2, "2")):
        db.daily_reports.docs.append({"id": f"dr{sfx}", "tenant_id": A, "project_id": pid})
        db.delays.docs.append({"id": f"d{sfx}", "tenant_id": A, "project_id": pid,
                               "status": "open"})
        db.mitigations.docs.append({"id": f"m{sfx}", "tenant_id": A,
                                    "linked_delay_id": f"d{sfx}"})
        db.rfis.docs.append({"id": f"rfi{sfx}", "tenant_id": A, "project_id": pid})
        db.work_items.docs.append({"id": f"wi{sfx}", "tenant_id": A, "project_id": pid})
        db.drawings.docs.append({"id": f"dw{sfx}", "tenant_id": A, "project_id": pid})
        db.drawing_revisions.docs.append({"id": f"rev{sfx}", "tenant_id": A,
                                          "drawing_id": f"dw{sfx}"})
        db.drawing_markups.docs.append({"id": f"mk{sfx}", "tenant_id": A,
                                        "drawing_revision_id": f"rev{sfx}",
                                        "page_number": 1, "markup_type": "pen",
                                        "geometry": {}, "style": {},
                                        "created_at": "2026-11-01T00:00:00+00:00"})
        db.project_specs.docs.append({"id": f"sp{sfx}", "tenant_id": A, "project_id": pid})
        db.voice_annotations.docs.append({"id": f"va{sfx}", "tenant_id": A, "project_id": pid})
        db.tia_generations.docs.append({"id": f"tia{sfx}", "tenant_id": A, "project_id": pid,
                                        "status": "pending"})


@pytest.fixture
def db(monkeypatch):
    d = DB()
    _seed(d)
    _world(d)
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
    yield d
    runtime_settings.reset_cache_for_tests()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()


def _app():
    api = APIRouter(prefix="/api")
    for name in ROUTER_MODULES:
        api.include_router(importlib.import_module(name).router)
    app = FastAPI()
    app.include_router(api, dependencies=[Depends(pa.enforce_project_scope)])
    return TestClient(app, raise_server_exceptions=False)


def _h(uid):
    return {"Authorization": f"Bearer {_token(uid)}"}


def _co(pid, **extra):
    body = {"project_id": pid, "title": "CO", "description": "x"}
    body.update(extra)
    return body


def _watch(db, uid, pid):
    return [w for w in db.project_access_watch.docs
            if w.get("user_id") == uid and w.get("project_id") == pid]


# ------------------------------------------------------------ guard
def test_every_route_is_checked_from_url_or_body():
    """A new route in these routers must either name its project / record
    in the URL in a way the dependency understands, or be listed in
    BODY_CHECKED (and check the body itself)."""
    from fastapi.routing import APIRoute
    missing = []
    for name in ROUTER_MODULES:
        for r in importlib.import_module(name).router.routes:
            if not isinstance(r, APIRoute):
                continue
            path = "/api" + r.path
            params = {p: "x" for p in pa._PARAM_RE.findall(path)}
            if "entity_type" in params:
                params["entity_type"] = "rfi"
            for m in r.methods:
                if pa.targets_for_route(path, params):
                    continue
                if (m, path) not in BODY_CHECKED:
                    missing.append((m, path))
    assert missing == []


# ------------------------------------------------------------ URL routes, switch ON
def test_url_routes_enforced(db, enforced):
    c = _app()
    h = _h("super1")
    # reads of the unassigned project: 404
    assert c.get(f"/api/projects/{P2}/change-orders", headers=h).status_code == 404
    assert c.get("/api/delays/d2", headers=h).status_code == 404
    assert c.get("/api/drawing-revisions/rev2/markups", headers=h).status_code == 404
    assert c.get("/api/tia/generations/tia2", headers=h).status_code == 404
    assert c.get("/api/entities/rfi/rfi2", headers=h).status_code == 404
    # writes: 403 with the detail a phone keeps queued
    for method, url in [
        ("patch", "/api/delays/d2"), ("delete", "/api/delays/d2"),
        ("post", "/api/mitigations/m2/choose"),          # mitigation -> delay -> project
        ("delete", "/api/drawing-markups/mk2"),          # markup -> revision -> drawing
        ("post", "/api/specs/sp2/reindex"),
        ("post", f"/api/projects/{P2}/tia/generate"),
        ("patch", "/api/voice-annotations/va2"),
    ]:
        r = c.request(method.upper(), url, headers=h, json={})
        assert r.status_code == 403, (method, url, r.status_code)
        assert r.json()["detail"] == NOT_ASSIGNED
    # the assigned project works
    assert c.get(f"/api/projects/{P1}/change-orders", headers=h).status_code == 200
    assert c.get("/api/drawing-revisions/rev1/markups", headers=h).status_code == 200
    assert c.get("/api/entities/rfi/rfi1", headers=h).status_code == 200
    assert c.delete("/api/drawing-markups/mk1", headers=h).status_code == 200


# ------------------------------------------------------------ bodies, switch ON
def test_body_project_enforced(db, enforced):
    c = _app()
    h = _h("super1")
    r = c.post("/api/change-orders", headers=h, json=_co(P2))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert c.post("/api/change-orders", headers=h, json=_co(P1)).status_code == 200
    # an unknown project is still 404 (unchanged)
    assert c.post("/api/change-orders", headers=h, json=_co("nope")).status_code == 404
    # the record a body points at must be on an assigned project too
    r = c.post("/api/change-orders", headers=h, json=_co(P1, originating_rfi_id="rfi2"))
    assert r.status_code == 403
    ev = {"date": "2026-11-02", "description": "x"}
    for url, body in [
        ("/api/delays", {"project_id": P2, **ev}),
        ("/api/safety-events", {"project_id": P2, **ev}),
        ("/api/qc-issues", {"project_id": P2, **ev}),
        ("/api/rfis", {"project_id": P2, "subject": "s", "description": "x"}),
        ("/api/drawings", {"project_id": P2, "sheet_number": "A1", "title": "t"}),
        ("/api/specs", {"project_id": P2, "name": "s"}),
        ("/api/work-items", {"project_id": P2, "title": "w", "description": "x"}),
        # assigned project, but the daily report is on the other project
        ("/api/delays", {"project_id": P1, "daily_report_id": "dr2", **ev}),
        ("/api/safety-events", {"project_id": P1, "daily_report_id": "dr2", **ev}),
        ("/api/qc-issues", {"project_id": P1, "parent_daily_report_id": "dr2", **ev}),
        ("/api/rfis", {"project_id": P1, "daily_report_id": "dr2",
                       "subject": "s", "description": "x"}),
    ]:
        r = c.post(url, headers=h, json=body)
        assert r.status_code == 403, (url, body, r.status_code, r.text[:200])
        assert r.json()["detail"] == NOT_ASSIGNED


def test_body_child_records_enforced(db, enforced):
    c = _app()
    h = _h("super1")
    r = c.post("/api/drawing-markups", headers=h, json={
        "drawing_revision_id": "rev2", "markup_type": "pen", "geometry": {}})
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    r = c.post("/api/drawing-markups", headers=h, json={
        "drawing_revision_id": "rev1", "markup_type": "pin", "geometry": {},
        "linked_entity_type": "rfi", "linked_entity_id": "rfi2"})
    assert r.status_code == 403
    r = c.post("/api/drawing-markups", headers=h, json={
        "drawing_revision_id": "rev1", "markup_type": "pen", "geometry": {}})
    assert r.status_code == 200
    r = c.post("/api/work-item-links", headers=h, json={
        "work_item_id": "wi1", "entity_type": "rfi", "entity_id": "rfi2"})
    assert r.status_code == 403
    assert c.post("/api/work-item-links", headers=h, json={
        "work_item_id": "wi1", "entity_type": "rfi", "entity_id": "rfi1"}).status_code == 200
    r = c.patch("/api/voice-annotations/va1", headers=h, json={
        "linked_entity_type": "rfi", "linked_entity_id": "rfi2"})
    assert r.status_code == 403
    r = c.post("/api/drawings-spatial-metadata", headers=h, json={
        "drawing_revision_id": "rev2", "drawing_type": "plan"})
    assert r.status_code == 403


def test_form_fields_enforced(db, enforced):
    c = _app()
    h = _h("super1")
    files = {"file": ("p.jpg", b"\xff\xd8\xff", "image/jpeg")}
    r = c.post("/api/photos/upload", headers=h, data={"project_id": P2}, files=files)
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    r = c.post("/api/photos/upload", headers=h,
               data={"project_id": P1, "daily_report_id": "dr2"}, files=files)
    assert r.status_code == 403
    r = c.post("/api/photos/upload", headers=h, data={"project_id": "nope"}, files=files)
    assert r.status_code == 404
    r = c.post("/api/drawing-revisions/upload", headers=h, data={"drawing_id": "dw2"},
               files={"file": ("d.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 403


def test_spec_search_is_a_read(db, enforced):
    c = _app()
    h = _h("super1")
    r = c.post("/api/specs/search", headers=h, json={"project_id": P2, "query": "door"})
    assert r.status_code == 404
    r = c.post("/api/specs/search", headers=h, json={"project_spec_id": "sp2", "query": "door"})
    assert r.status_code == 404


def test_submittal_create_checks_project(db, enforced):
    c = _app()
    r = c.post("/api/submittals", headers=_h("pm1"), json={"project_id": P2, "title": "t"})
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED


# ------------------------------------------------------------ switch OFF
def test_switch_off_allows_and_records(db):
    c = _app()
    h = _h("super1")
    assert c.post("/api/change-orders", headers=h, json=_co(P2)).status_code == 200
    rows = _watch(db, "super1", P2)
    assert rows and rows[0]["method"] == "POST" and rows[0]["count"] >= 1
    assert not rows[0].get("blocked_count")
    assert c.post("/api/drawing-markups", headers=h, json={
        "drawing_revision_id": "rev2", "markup_type": "pen", "geometry": {}}).status_code == 200
    assert c.get(f"/api/projects/{P2}/change-orders", headers=h).status_code == 200
    assert c.delete("/api/drawing-markups/mk2", headers=h).status_code == 200
    routes = {w["route"] for w in _watch(db, "super1", P2)}
    assert "/api/drawing-markups" in routes
    assert "/api/drawing-markups/{markup_id}" in routes
    # an assigned project is never recorded
    assert c.post("/api/change-orders", headers=h, json=_co(P1)).status_code == 200
    assert _watch(db, "super1", P1) == []


# ------------------------------------------------------------ admins
def test_full_admin_and_limited_admin(db, enforced):
    c = _app()
    # all-projects admin: everything
    assert c.post("/api/change-orders", headers=_h("admin1"), json=_co(P2)).status_code == 200
    assert c.post("/api/change-orders", headers=_h("admin1"), json=_co(P1)).status_code == 200
    # admin limited to P2: P2 yes, P1 no
    assert c.post("/api/change-orders", headers=_h("admin2"), json=_co(P2)).status_code == 200
    r = c.post("/api/change-orders", headers=_h("admin2"), json=_co(P1))
    assert r.status_code == 403 and r.json()["detail"] == NOT_ASSIGNED
    assert c.get(f"/api/projects/{P1}/change-orders", headers=_h("admin2")).status_code == 404
    assert c.delete("/api/drawing-markups/mk1", headers=_h("admin2")).status_code == 403
    # the limited admin keeps project-level admin powers on their project
    # (TIA privilege unflag: the admin check passes and the handler runs)
    r = c.post("/api/tia/generations/tia2/unflag-privilege", headers=_h("admin2"),
               json={"reason": "share with the owner's team"})
    assert r.status_code == 200 and r.json()["counsel_privileged_work_product"] is False
    r = c.post("/api/tia/generations/tia1/unflag-privilege", headers=_h("admin2"),
               json={"reason": "share with the owner's team"})
    assert r.status_code == 403
