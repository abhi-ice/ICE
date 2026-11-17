"""Guard: every API route has a known per-project access story (2026-11-16).

The whole app is imported and every route is walked. Each route must be one
of these, or the test fails and lists it:

  * exempt      - its path starts with one of project_access.EXEMPT_PREFIXES
                  (sign-in, health, MS Project worker, cron, Stripe webhook,
                  company settings, the team page, WebSocket);
  * url         - the route-level check (enforce_project_scope) finds a
                  project or a project record in the URL: a project id path
                  parameter, a record id after a known URL word, the generic
                  /entities/{type}/{id} and /crud/{entity}/{id} forms, or a
                  required project_id query parameter;
  * handler     - the handler itself calls one of the shared access helpers
                  (see HANDLER_HELPERS) for ids in the body, lists, or
                  company-wide admin rules;
  * tenant      - the decorator carries "# project-scope: tenant-level" just
                  above it (company-wide data: vendors, crews, cost rates,
                  the caller's own assistant history, audit log...);
  * allow-listed - in ALLOW below, with the reason.

When this test fails for a new route, decide which of these it is. Do not add
it to ALLOW just to make the test pass: if the route reads or changes one
project's data, check it.

Offline. The app is imported against the real modules (some older test files
leave bare `database` / `audit` stand-ins in sys.modules; they are put back
exactly as they were, and nothing new is left behind).
"""
import importlib
import inspect
import re
import sys

import pytest
from fastapi.routing import APIRoute

_stand_ins = {n: sys.modules[n] for n in ("database", "audit")
              if n in sys.modules and not getattr(sys.modules[n], "__file__", None)}
for _n in _stand_ins:
    del sys.modules[_n]
try:
    server = importlib.import_module("server")
    pa = importlib.import_module("services.project_access")
finally:
    sys.modules.update(_stand_ins)


# Shared access helpers. A handler that calls any of these checks access
# itself (project or record ids from the body, list filters, or the
# all-projects-admin rule for company-wide controls).
HANDLER_HELPERS = (
    # services.project_access / services.project_scope
    "require_project", "require_project_if_given", "check_project_access",
    "check_project_for_request", "check_record_access", "project_scope_filter",
    "filter_rows", "allowed_project_ids", "require_full_admin", "is_full_admin",
    # auth (older always-on checks, limited-admin aware)
    "has_project_access", "get_accessible_project_ids", "can_manage_project_members",
    # router-local wrappers around the helpers above
    "_check_project_access",      # exhibit exports, recovery reports, submittal SLA
    "_check_entity_access",       # field suggestions
    "_split_by_project_access",   # field suggestion bulk confirm / reject
    "_check_linked_records",      # field events, docs
    "_project_or_404",            # schedule review
    "_spec_section_scope",        # iter5 search
)
_HELPER_RE = re.compile(r"\b(" + "|".join(map(re.escape, HANDLER_HELPERS)) + r")\s*\(")

# (method, path) -> reason. Keep this short; prefer a real check or a
# tenant-level marker in the router.
ALLOW: dict[tuple[str, str], str] = {
}

TENANT_MARK = "project-scope: tenant-level"
_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")


def _routes():
    for r in server.app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api"):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield m, r


def _is_generic(path: str) -> bool:
    """/entities/{entity_type}/{entity_id} and /crud/{entity}/{id}: a record
    type parameter followed by an id parameter."""
    segs = [s for s in path.split("/") if s]
    return any(_PARAM.fullmatch(a) and _PARAM.fullmatch(b) for a, b in zip(segs, segs[1:]))


def _url_covered(route: APIRoute) -> bool:
    params = {name: "x" for name in _PARAM.findall(route.path)}
    if pa.targets_for_route(route.path, params):
        return True
    if _is_generic(route.path):
        return True   # record types are checked in test_generic_registries_resolve
    for q in route.dependant.query_params:
        if q.name == "project_id" and q.required:
            return True
    return False


def _tenant_marked(fn) -> bool:
    """The marker comment sits above the decorator stack (other decorators
    and comment lines may come between)."""
    try:
        lines, start = inspect.getsourcelines(fn)
        mod_lines = inspect.getsource(inspect.getmodule(fn)).splitlines()
    except (OSError, TypeError):
        return False
    i = start - 2   # line above the first decorator (0-based)
    while 0 <= i < len(mod_lines):
        ln = mod_lines[i].strip()
        if TENANT_MARK in ln:
            return True
        if ln.startswith("@") or ln.startswith("#"):
            i -= 1
            continue
        return False
    return False


def _handler_checked(fn) -> bool:
    try:
        return bool(_HELPER_RE.search(inspect.getsource(fn)))
    except (OSError, TypeError):
        return False


def classify(method: str, route: APIRoute) -> str | None:
    if pa.is_exempt(route.path):
        return "exempt"
    if _url_covered(route):
        return "url"
    if _handler_checked(route.endpoint):
        return "handler"
    if _tenant_marked(route.endpoint):
        return "tenant"
    if (method, route.path) in ALLOW:
        return "allow"
    return None


def test_every_route_is_classified():
    unclassified = []
    counts: dict[str, int] = {}
    for method, r in _routes():
        c = classify(method, r)
        counts[c or "UNCLASSIFIED"] = counts.get(c or "UNCLASSIFIED", 0) + 1
        if c is None:
            fn = r.endpoint
            unclassified.append(f"{method} {r.path}  ({fn.__module__}.{fn.__name__})")
    assert not unclassified, (
        "These routes have no per-project access story. Check the project "
        "(services.project_scope.require_project / services.project_access."
        "check_record_access / project_scope_filter), or mark a company-wide "
        "route with '# project-scope: tenant-level (reason)' above its decorator:\n  "
        + "\n  ".join(unclassified))
    # Sanity: the walk really saw the app.
    assert counts.get("url", 0) > 150 and counts.get("handler", 0) > 50, counts


def test_allow_list_has_no_stale_entries():
    present = {(m, r.path) for m, r in _routes()}
    assert not (set(ALLOW) - present), set(ALLOW) - present


def test_every_api_route_carries_the_route_dependency():
    missing = []
    for method, r in _routes():
        calls = [d.call for d in r.dependant.dependencies]
        if pa.enforce_project_scope not in calls:
            missing.append(f"{method} {r.path}")
    assert missing == []


def test_exempt_routes_naming_a_project_check_it_themselves():
    """An exempt path skips the route-level check. If it still names a
    project (or is signed in as a normal user and takes a project id), its
    handler must check it, unless it is a worker/cron/webhook route."""
    machine = ("/api/internal/ms-project/", "/api/cron/", "/api/webhook/")
    missing = []
    for method, r in _routes():
        if not pa.is_exempt(r.path) or r.path.startswith(machine):
            continue
        names = set(_PARAM.findall(r.path)) | {q.name for q in r.dependant.query_params}
        for b in r.dependant.body_params:
            fields = getattr(getattr(b.field_info, "annotation", None), "model_fields", None) or {}
            names |= set(fields)
        if "project_id" in names and not _handler_checked(r.endpoint):
            missing.append(f"{method} {r.path}")
    assert missing == []


def test_generic_registries_resolve():
    """Every record type the generic routes accept is one the route-level
    check can trace to a project, or a company-wide resource."""
    iter4 = importlib.import_module("routers.iter4_router")
    iter8 = importlib.import_module("routers.iter8_router")
    bad = [k for k in iter4.ENTITY_COLLECTIONS if not pa.normalize_kind(k)]
    for key, cfg in iter8.ENTITY_CONFIG.items():
        if cfg.get("project_scope") and not pa.normalize_kind(key):
            bad.append(key)
    assert bad == []


def test_known_route_classes():
    """Spot checks so the classifier itself cannot silently drift."""
    want = {
        ("POST", "/api/daily-reports"): "handler",
        ("PATCH", "/api/daily-reports/{daily_report_id}"): "url",
        ("GET", "/api/projects/{project_id}"): "url",
        ("GET", "/api/ai/recovery"): "url",                       # required ?project_id
        ("DELETE", "/api/crud/{entity}/{id}"): "url",             # generic
        ("GET", "/api/subcontractors"): "tenant",
        ("POST", "/api/internal/ms-project/claim"): "exempt",
        ("POST", "/api/field-suggestions/bulk-confirm"): "handler",
    }
    got = {}
    for method, r in _routes():
        if (method, r.path) in want:
            got[(method, r.path)] = classify(method, r)
    assert got == want


# ---------------------------------------------------------------------------
# Fix found by the walk: PATCH /locations/{id} could move a location onto a
# project (or floor) the caller is not assigned to. The URL check only saw
# the location's current project.
# ---------------------------------------------------------------------------
import test_project_access_foundation as F  # noqa: E402
from test_project_access_foundation import A, P1, P2, run  # noqa: E402


@pytest.fixture
def world(monkeypatch):
    from services import runtime_settings
    d = F.DB()
    F._seed(d)
    for name in ("auth", "services.project_access", "services.project_scope",
                 "routers.projects_router"):
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", lambda: d)
    monkeypatch.setattr(importlib.import_module("auth"), "_attempt_indexes_ready", False)
    d.floors.docs += [
        {"id": "f1", "tenant_id": A, "project_id": P1, "name": "L1"},
        {"id": "f2", "tenant_id": A, "project_id": P2, "name": "L1"},
    ]
    d.locations.docs.append({"id": "loc1", "tenant_id": A, "project_id": P1,
                             "floor_id": "f1", "name": "Room 1",
                             "location_type": "room", "created_at": F.NOW})
    monkeypatch.delenv("FF_PROJECT_ACCESS", raising=False)
    runtime_settings.reset_cache_for_tests()
    yield d
    runtime_settings.reset_cache_for_tests()


def _patch_location(uid, **body):
    from models import LocationCreate
    pr = importlib.import_module("routers.projects_router")
    payload = LocationCreate(**{"project_id": P1, "floor_id": "f1", "name": "Room 1", **body})
    ctx = F._ctx(uid, method="PATCH")
    return run(pr.update_location("loc1", payload, ctx))


def test_location_move_to_unassigned_project_refused(world, monkeypatch):
    from fastapi import HTTPException
    from services import runtime_settings
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()
    for body in ({"project_id": P2, "floor_id": "f2"}, {"floor_id": "f2"}):
        with pytest.raises(HTTPException) as e:
            _patch_location("super1", **body)
        assert e.value.status_code == 403
        assert e.value.detail == pa.NOT_ASSIGNED
    assert world.locations.docs[0]["project_id"] == P1
    # Editing in place on the assigned project still works.
    assert _patch_location("super1", name="Room 1A").name == "Room 1A"
    # The all-projects admin may move it.
    assert _patch_location("admin1", project_id=P2, floor_id="f2").project_id == P2


def test_location_move_watch_only_when_off(world):
    out = _patch_location("super1", project_id=P2, floor_id="f2")
    assert out.project_id == P2
    rows = [w for w in world.project_access_watch.docs if w.get("user_id") == "super1"]
    assert rows and rows[0]["project_id"] == P2
