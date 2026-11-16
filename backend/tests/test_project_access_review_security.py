"""Security review follow-ups for per-project access (2026-11-16).

An admin limited to specific projects ("admin2", assigned to P2 only, in the
seeded world of test_project_access_foundation) must not be able to:
  * permanently delete a project they are not assigned to, even while the
    FF_PROJECT_ACCESS switch is off (deleting cannot be undone);
  * export the project audit extract, which also carries company-wide audit
    rows that name no project;
  * run the company-wide submittal jobs without naming one of their projects;
  * get past the older always-on project checks (exhibit exports, recovery
    reports, submittal timing) on a project they are not assigned to.
An all-projects admin keeps all of these.

Offline: handlers are called directly with the in-memory fake database.
"""
import importlib
import sys

import pytest
from fastapi import BackgroundTasks, HTTPException

MODULES = ["routers.projects_router", "routers.exhibit_exports_router",
           "routers.recovery_reports_router", "routers.submittal_sla_router"]
PATCHED = ["auth", "services.project_access", "services.project_scope"] + MODULES

# Import against the real database / audit modules, then put back any bare
# stand-ins other test files left in sys.modules, exactly as they were.
_stand_ins = {n: sys.modules[n] for n in ("database", "audit")
              if n in sys.modules and not getattr(sys.modules[n], "__file__", None)}
for _n in _stand_ins:
    del sys.modules[_n]
try:
    for _m in PATCHED:
        importlib.import_module(_m)
finally:
    sys.modules.update(_stand_ins)

import auth  # noqa: E402
from services import runtime_settings  # noqa: E402

import test_project_access_foundation as F  # noqa: E402
from test_project_access_foundation import A, P1, P2, run, _token  # noqa: E402


@pytest.fixture
def db(monkeypatch):
    d = F.DB()
    F._seed(d)
    for name in PATCHED:
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", lambda: d)

    async def _audit(**kw):
        d.audit_log.docs.append({"op": kw.get("op")})
    for name in MODULES:
        mod = importlib.import_module(name)
        if hasattr(mod, "write_audit"):
            monkeypatch.setattr(mod, "write_audit", _audit)
    monkeypatch.delenv("FF_PROJECT_ACCESS", raising=False)
    runtime_settings.reset_cache_for_tests()
    yield d
    runtime_settings.reset_cache_for_tests()


def _ctx(uid, tid=A):
    return run(auth.build_request_context(_token(uid, tid), None))


def _status(coro):
    with pytest.raises(HTTPException) as e:
        run(coro)
    return e.value.status_code, e.value.detail


def test_limited_admin_cannot_delete_unassigned_project_even_when_off(db):
    pr = importlib.import_module("routers.projects_router")
    before = len(db.projects.docs)
    code, detail = _status(pr.delete_project(P1, ctx=_ctx("admin2")))
    assert code == 403 and detail == "You are not assigned to this project"
    assert len(db.projects.docs) == before
    assert not [a for a in db.audit_log.docs if a.get("op") == "DELETE_PROJECT"]


def test_limited_admin_cannot_export_audit_extract(db):
    ex = importlib.import_module("routers.exhibit_exports_router")
    code, _ = _status(ex.export_audit_extract(project_id=P2, format="csv", ctx=_ctx("admin2")))
    assert code == 403


def test_always_on_checks_know_limited_admins(db):
    ex = importlib.import_module("routers.exhibit_exports_router")
    rr = importlib.import_module("routers.recovery_reports_router")
    sl = importlib.import_module("routers.submittal_sla_router")
    for mod in (ex, rr, sl):
        assert _status(mod._check_project_access(_ctx("admin2"), P1))[0] == 404
        run(mod._check_project_access(_ctx("admin2"), P2))
        run(mod._check_project_access(_ctx("admin1"), P1))


def test_company_wide_submittal_jobs_need_a_project_for_limited_admin(db):
    sl = importlib.import_module("routers.submittal_sla_router")
    for fn in (sl.recompute_aging_endpoint, sl.detect_breaches_endpoint):
        code, _ = _status(fn(BackgroundTasks(), None, ctx=_ctx("admin2")))
        assert code == 403
        res = run(fn(BackgroundTasks(), sl._RecomputePayload(project_id=P2), ctx=_ctx("admin2")))
        assert res["project_id"] == P2
        assert run(fn(BackgroundTasks(), None, ctx=_ctx("admin1")))["queued"] is True
