"""Per-project access in the iter5/6/7/8, system, photo tray, field
suggestion, resource leveling, resources, vendors and WebSocket routers and
the AI assistant tools (2026-11-16).

Rules checked here:
  * switch ON: an assigned user is allowed; an unassigned user gets 404 on a
    read and 403 "You are not assigned to this project" on a write (a phone
    keeps a 403 save queued; a 404 would drop it);
  * switch OFF: nothing is refused, and the would-be refusal is recorded in
    project_access_watch;
  * an all-projects admin sees everything; an admin limited to specific
    projects keeps admin powers only on those projects and loses the
    company-wide controls;
  * lists are limited to the caller's projects while the switch is on;
  * notifications and WebSocket messages about a project only reach people
    who may see it; ones without a project stay visible;
  * the AI assistant's tools enforce the same rules whatever arguments the
    model writes.

Offline: handlers are called directly with the in-memory fake database and
the seeded world from test_project_access_foundation (two companies, every
role, an all-projects admin "admin1" and an admin limited to P2 "admin2";
every non-admin of company A is assigned to P1 only).
"""
import asyncio
import importlib
import json
import re
import sys

import pytest
from fastapi import BackgroundTasks, HTTPException

ROUTER_MODULES = [
    "routers.iter5_router", "routers.iter6_router", "routers.iter7_router",
    "routers.iter8_router", "routers.system_router", "routers.photo_tray_router",
    "routers.field_suggestions_router", "routers.resource_leveling_router",
    "routers.resources_router", "routers.ws_router", "routers.vendors_router",
]
PATCHED = ["auth", "services.project_access", "services.project_scope",
           "services.copilot_tools"] + ROUTER_MODULES
_ALSO = ["services.photo_ai", "services.ai_agents", "services.spec_search",
         "services.field_suggestions", "services.resource_leveling"]

# Some older test files put bare stand-ins for `database` / `audit` into
# sys.modules at collection time (they have no __file__). Import the modules
# under test against the real ones, then put those stand-ins back exactly as
# they were, so this file neither depends on nor disturbs them.
_stand_ins = {n: sys.modules[n] for n in ("database", "audit")
              if n in sys.modules and not getattr(sys.modules[n], "__file__", None)}
for _n in _stand_ins:
    del sys.modules[_n]
try:
    for _m in PATCHED + _ALSO:
        importlib.import_module(_m)
finally:
    sys.modules.update(_stand_ins)

import auth  # noqa: E402
from services import project_access as pa  # noqa: E402
from services import runtime_settings  # noqa: E402

import test_project_access_foundation as F  # noqa: E402
from test_project_access_foundation import A, B, P1, P2, P3, PB, run, _token  # noqa: E402


@pytest.fixture
def db(monkeypatch):
    d = F.DB()
    F._seed(d)
    for name in PATCHED:
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

    # The fake cursor learns `async for` for the resource leveling walk.
    def _aiter(self):
        docs = list(self._docs)

        async def gen():
            for x in docs:
                yield x
        return gen()
    monkeypatch.setattr(F._Cursor, "__aiter__", _aiter, raising=False)
    # ...and regular-expression filters (global search).
    plain = F._match_value

    def _match_value(actual, cond):
        if isinstance(cond, re.Pattern):
            return isinstance(actual, str) and bool(cond.search(actual))
        return plain(actual, cond)
    monkeypatch.setattr(F, "_match_value", _match_value)
    monkeypatch.delenv("FF_PROJECT_ACCESS", raising=False)
    runtime_settings.reset_cache_for_tests()
    # Records on P1 and P2 of company A, and on company B's project.
    d.daily_reports.docs += [
        {"id": "dr1", "tenant_id": A, "project_id": P1, "report_date": "2026-11-02", "status": "draft"},
        {"id": "dr2", "tenant_id": A, "project_id": P2, "report_date": "2026-11-02", "status": "draft"},
    ]
    d.rfis.docs += [
        {"id": "r1", "tenant_id": A, "project_id": P1, "rfi_number": "RFI-1", "subject": "Pour one", "status": "sent"},
        {"id": "r2", "tenant_id": A, "project_id": P2, "rfi_number": "RFI-2", "subject": "Pour two", "status": "sent"},
        {"id": "rb", "tenant_id": B, "project_id": PB, "rfi_number": "RFI-B", "subject": "Pour bee", "status": "sent"},
    ]
    d.delays.docs += [
        {"id": "d1", "tenant_id": A, "project_id": P1, "description": "rain", "status": "open"},
    ]
    yield d
    runtime_settings.reset_cache_for_tests()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv("FF_PROJECT_ACCESS", "1")
    runtime_settings.reset_cache_for_tests()


def _ctx(uid, tid=A):
    """A request context exactly as the API builds it for this sign-in."""
    return run(auth.build_request_context(_token(uid, tid), None))


def _status(coro):
    with pytest.raises(HTTPException) as e:
        run(coro)
    return e.value.status_code, e.value.detail


def _watch(db, uid=None, pid=None):
    return [w for w in db.project_access_watch.docs
            if (uid is None or w["user_id"] == uid) and (pid is None or w["project_id"] == pid)]


# ------------------------------------------------------------ body ids
def test_body_daily_report_switch_off_records_and_allows(db):
    ir5 = importlib.import_module("routers.iter5_router")
    ctx = _ctx("super1")
    out = run(ir5.add_attendance(ir5.AttendanceEntry(daily_report_id="dr2", name="Pat"), ctx))
    assert out["daily_report_id"] == "dr2"
    rows = _watch(db, "super1", P2)
    assert len(rows) == 1 and rows[0]["write"] is True and rows[0]["count"] == 1
    # Own project: nothing recorded.
    run(ir5.add_attendance(ir5.AttendanceEntry(daily_report_id="dr1", name="Pat"), _ctx("super1")))
    assert not _watch(db, "super1", P1)


def test_body_daily_report_switch_on_is_403_never_404(db, enforced):
    ir5 = importlib.import_module("routers.iter5_router")
    code, detail = _status(ir5.add_attendance(
        ir5.AttendanceEntry(daily_report_id="dr2", name="Pat"), _ctx("super1")))
    assert (code, detail) == (403, pa.NOT_ASSIGNED)
    assert _watch(db, "super1", P2)[0]["blocked_count"] == 1
    assert not [r for r in db.daily_report_attendance.docs if r["daily_report_id"] == "dr2"]
    # Assigned project still works for every role.
    for role in F.ROLES:
        run(ir5.add_attendance(ir5.AttendanceEntry(daily_report_id="dr1", name=role),
                               _ctx(f"{role}1")))
    # Unknown daily report: the router's own 404 (not an access answer).
    code, _ = _status(ir5.add_attendance(
        ir5.AttendanceEntry(daily_report_id="nope", name="Pat"), _ctx("super1")))
    assert code == 404


def test_admin_all_vs_project_limited_admin(db, enforced):
    ir5 = importlib.import_module("routers.iter5_router")
    run(ir5.add_attendance(ir5.AttendanceEntry(daily_report_id="dr1", name="x"), _ctx("admin1")))
    run(ir5.add_attendance(ir5.AttendanceEntry(daily_report_id="dr2", name="x"), _ctx("admin1")))
    # admin2 is limited to P2.
    run(ir5.add_attendance(ir5.AttendanceEntry(daily_report_id="dr2", name="x"), _ctx("admin2")))
    code, detail = _status(ir5.add_attendance(
        ir5.AttendanceEntry(daily_report_id="dr1", name="x"), _ctx("admin2")))
    assert (code, detail) == (403, pa.NOT_ASSIGNED)
    # An admin of company A is only a "super" in company B: no admin pass.
    ctx_b = run(auth.build_request_context(_token("admin1", A), B))
    assert ctx_b["role"] == "super" and not pa.is_full_admin(ctx_b)


def test_crud_patch_moving_record_to_unassigned_project(db):
    ir8 = importlib.import_module("routers.iter8_router")
    # OFF: allowed and recorded.
    out = run(ir8.crud_patch("delays", "d1", {"project_id": P2, "description": "x"},
                             BackgroundTasks(), _ctx("super1")))
    assert out["project_id"] == P2
    assert _watch(db, "super1", P2)


def test_crud_patch_moving_record_switch_on(db, enforced):
    ir8 = importlib.import_module("routers.iter8_router")
    code, detail = _status(ir8.crud_patch("delays", "d1", {"project_id": P2, "description": "x"},
                                          BackgroundTasks(), _ctx("super1")))
    assert (code, detail) == (403, pa.NOT_ASSIGNED)
    assert db.delays.docs[0]["project_id"] == P1
    # A plain edit on the user's own project is fine.
    out = run(ir8.crud_patch("delays", "d1", {"description": "dry"},
                             BackgroundTasks(), _ctx("super1")))
    assert out["description"] == "dry"


# ------------------------------------------------------------ lists
def test_global_search_limited_when_on(db, enforced):
    ir5 = importlib.import_module("routers.iter5_router")
    out = run(ir5.global_search(q="pour", ctx=_ctx("super1")))
    assert {r["id"] for r in out["results"] if r["type"] == "rfi"} == {"r1"}
    # Project "Two" is P2: hidden from super1, found by admins who may see it.
    out = run(ir5.global_search(q="tw", ctx=_ctx("super1")))
    assert [r for r in out["results"] if r["type"] == "project"] == []
    out = run(ir5.global_search(q="tw", ctx=_ctx("admin2")))
    assert {r["id"] for r in out["results"] if r["type"] == "project"} == {P2}
    out = run(ir5.global_search(q="on", ctx=_ctx("super1")))
    assert {r["id"] for r in out["results"] if r["type"] == "project"} == {P1}
    # All-projects admin: every project of the company, never company B.
    out = run(ir5.global_search(q="pour", ctx=_ctx("admin1")))
    assert {r["id"] for r in out["results"] if r["type"] == "rfi"} == {"r1", "r2"}


def test_global_search_unchanged_when_off(db):
    ir5 = importlib.import_module("routers.iter5_router")
    out = run(ir5.global_search(q="pour", ctx=_ctx("super1")))
    assert {r["id"] for r in out["results"] if r["type"] == "rfi"} == {"r1", "r2"}
    assert not db.project_access_watch.docs  # lists are never recorded


def test_dashboard_and_approvals_counts(db, enforced):
    sr = importlib.import_module("routers.system_router")
    base = {"tenant_id": A, "status": "pending", "proposed_at": F.NOW, "created_at": F.NOW,
            "kind": "schedule_change", "subject_entity_type": "task",
            "subject_entity_id": "t", "proposed_payload": {}, "proposed_by": "x",
            "decisions": [], "quorum_required": 1}
    db.approvals.docs += [{**base, "id": "a1", "project_id": P1},
                          {**base, "id": "a2", "project_id": P2}]
    out = run(sr.dashboard_summary(project_id=None, ctx=_ctx("super1")))
    assert out["open_rfis"] == 1 and out["active_projects"] == 1
    out = run(sr.dashboard_summary(project_id=None, ctx=_ctx("admin1")))
    assert out["open_rfis"] == 2 and out["active_projects"] == 3
    assert [a.id for a in run(sr.list_approvals("pending", _ctx("super1")))] == ["a1"]
    assert run(sr.pending_count(_ctx("super1"))) == {"count": 1}
    assert run(sr.pending_count(_ctx("admin2"))) == {"count": 1}
    assert run(sr.pending_count(_ctx("admin1"))) == {"count": 2}


def test_notifications_get_a_project_and_are_filtered(db, enforced):
    ir5 = importlib.import_module("routers.iter5_router")
    db.deliveries.docs.append({"id": "del2", "tenant_id": A, "project_id": P2})
    doc = run(ir5.emit_notification(tenant_id=A, user_id=None, kind="k", title="t",
                                    entity_type="delivery", entity_id="del2"))
    assert doc["project_id"] == P2     # worked out from the record
    run(ir5.emit_notification(tenant_id=A, user_id=None, kind="k", title="p1",
                              project_id=P1))
    run(ir5.emit_notification(tenant_id=A, user_id=None, kind="k", title="company"))
    titles = {n.title for n in run(ir5.list_notifications(ctx=_ctx("super1")))}
    assert titles == {"p1", "company"}
    assert run(ir5.unread_count(_ctx("super1"))) == {"count": 2}
    assert len(run(ir5.list_notifications(ctx=_ctx("admin1")))) == 3


def test_notifications_all_visible_when_off(db):
    ir5 = importlib.import_module("routers.iter5_router")
    run(ir5.emit_notification(tenant_id=A, user_id=None, kind="k", title="p2", project_id=P2))
    assert len(run(ir5.list_notifications(ctx=_ctx("super1")))) == 1


# ------------------------------------------------------------ company-wide controls
def test_company_wide_controls_need_all_projects_admin(db):
    sr = importlib.import_module("routers.system_router")
    ir7 = importlib.import_module("routers.iter7_router")
    ir5 = importlib.import_module("routers.iter5_router")
    pt = importlib.import_module("routers.photo_tray_router")
    limited = _ctx("admin2")
    for coro in (sr.list_audit(ctx=limited), ir7.verify_audit_chain(ctx=limited),
                 ir5.admin_ai_obs(ctx=limited), pt.get_current_tenant_budget(ctx=limited)):
        code, detail = _status(coro)
        assert code == 403 and detail == pa.FULL_ADMIN_ONLY
    # The all-projects admin still reads the audit log.
    assert run(sr.list_audit(ctx=_ctx("admin1"))) == []
    # Not an admin at all: the older "admin only" refusal.
    code, _ = _status(sr.list_audit(ctx=_ctx("pm1")))
    assert code == 403


# ------------------------------------------------------------ photo tray
def test_tray_access_limited_admin(db):
    pt = importlib.import_module("routers.photo_tray_router")
    tray_p1 = {"id": "t1", "tenant_id": A, "project_id": P1}
    tray_p2 = {"id": "t2", "tenant_id": A, "project_id": P2}
    run(pt._enforce_tray_access(_ctx("admin1"), tray_p1, pt._ROLE_TRIGGER_SINGLE))
    run(pt._enforce_tray_access(_ctx("admin2"), tray_p2, pt._ROLE_TRIGGER_SINGLE))
    code, _ = _status(pt._enforce_tray_access(_ctx("admin2"), tray_p1, pt._ROLE_TRIGGER_SINGLE))
    assert code == 404   # always-on check, unchanged leak-resistant answer


def test_batch_analyze_without_project_walks_only_own_projects(db, enforced, monkeypatch):
    pt = importlib.import_module("routers.photo_tray_router")
    photo_ai = importlib.import_module("services.photo_ai")
    calls = []

    async def fake_batch(*, tenant_id, project_id=None, limit=10):
        calls.append(project_id)
        return {"attempted": 1, "complete": 1}
    monkeypatch.setattr(photo_ai, "batch_analyze_pending", fake_batch)
    # AI counts as available here (no key and no dev mode now answer 422
    # "ai_unavailable" before the batch runs; services/ai_unavailable.py).
    monkeypatch.setattr(photo_ai, "photo_ai_can_run", lambda: True)
    out = run(pt.analyze_batch(pt._AiBatchPayload(limit=5), _ctx("admin2")))
    assert calls == [P2] and out["attempted"] == 1
    calls.clear()
    run(pt.analyze_batch(pt._AiBatchPayload(limit=5), _ctx("admin1")))
    assert calls == [None]   # all-projects admin: company-wide as before


# ------------------------------------------------------------ field suggestions
def test_bulk_confirm_skips_unassigned_projects(db, enforced, monkeypatch):
    fsr = importlib.import_module("routers.field_suggestions_router")
    db.field_suggestions.docs += [
        {"id": "s1", "tenant_id": A, "project_id": P1, "entity_type": "rfi", "entity_id": "r1"},
        {"id": "s2", "tenant_id": A, "project_id": P2, "entity_type": "rfi", "entity_id": "r2"},
    ]
    seen = []

    async def fake_bulk(*, tenant_id, suggestion_ids, user_id, user_role, request_id):
        seen.append(list(suggestion_ids))
        return {"ok": True, "confirmed": list(suggestion_ids), "skipped": []}
    monkeypatch.setattr(fsr.fs, "bulk_confirm", fake_bulk)
    payload = fsr.FieldSuggestionBulkConfirmPayload(suggestion_ids=["s1", "s2"])
    out = run(fsr.bulk_confirm_endpoint(payload, _ctx("pm1")))
    assert seen == [["s1"]]
    assert out["failure_details"] == [{"id": "s2", "reason": "not_assigned"}]
    seen.clear()
    run(fsr.bulk_confirm_endpoint(payload, _ctx("admin1")))
    assert seen == [["s1", "s2"]]


# ------------------------------------------------------------ resource leveling
def test_recompute_without_project_limited_to_own_projects(db, enforced, monkeypatch):
    rl = importlib.import_module("routers.resource_leveling_router")
    demand = []

    async def fake_demand(*, tenant_id, project_id):
        demand.append(project_id)
        return {"cleared": 0, "added": 0}

    async def fake_cap(*, tenant_id, crew_id):
        return {"written": 0, "zeroed": 0}
    monkeypatch.setattr(rl.svc, "recompute_demand_for_project", fake_demand)
    monkeypatch.setattr(rl.svc, "recompute_capacity_for_crew", fake_cap)
    run(rl.trigger_recompute(rl.RecomputePayload(), BackgroundTasks(), _ctx("admin2")))
    assert demand == [P2]
    demand.clear()
    run(rl.trigger_recompute(rl.RecomputePayload(), BackgroundTasks(), _ctx("admin1")))
    assert sorted(demand) == [P1, P2, P3]
    # A project in the body that the caller is not on: 403.
    code, detail = _status(rl.trigger_recompute(
        rl.RecomputePayload(project_id=P1), BackgroundTasks(), _ctx("admin2")))
    assert (code, detail) == (403, pa.NOT_ASSIGNED)


# ------------------------------------------------------------ AI assistant tools
def test_copilot_query_is_limited_and_cannot_be_widened(db, enforced):
    ct = importlib.import_module("services.copilot_tools")
    ctx = _ctx("super1")
    out = run(ct._tool_query(ctx, {"collection": "rfis"}))
    assert [r["id"] for r in out["rows"]] == ["r1"]
    # The model names another project, or tries to filter its way there.
    out = run(ct._tool_query(ctx, {"collection": "rfis", "project_id": P2}))
    assert out["error"] == pa.NOT_FOUND
    out = run(ct._tool_query(ctx, {"collection": "rfis", "filter": {"project_id": P2}}))
    assert [r["id"] for r in out["rows"]] == ["r1"]
    out = run(ct._tool_query(ctx, {"collection": "rfis",
                                   "filter": {"$or": [{"project_id": P2}, {"id": "r2"}]}}))
    assert out["rows"] == []
    out = run(ct._tool_query(ctx, {"collection": "projects"}))
    assert [r["id"] for r in out["rows"]] == [P1]
    # A limited admin cannot read the company-wide audit log through it.
    out = run(ct._tool_query(_ctx("admin2"), {"collection": "audit_log"}))
    assert "error" in out and "audit_log" not in out["allowed_collections"]


def test_copilot_delete_and_update_check_the_record_project(db, enforced):
    ct = importlib.import_module("services.copilot_tools")
    ctx = _ctx("super1")
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "rfi", "id": "r2"}))
    assert out["error"] == pa.NOT_ASSIGNED
    assert db.rfis.docs[1].get("status") == "sent"
    out = run(ct._tool_update_entity(ctx, {"entity_type": "rfi", "id": "r1",
                                           "fields": {"project_id": P2}}))
    assert out["error"] == pa.NOT_ASSIGNED
    assert db.rfis.docs[0]["project_id"] == P1
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "rfi", "id": "r1"}))
    assert out == {"deleted": {"resource": "rfi", "id": "r1"}}


def test_copilot_delete_switch_off_records(db):
    ct = importlib.import_module("services.copilot_tools")
    out = run(ct._tool_delete_entity(_ctx("super1"), {"entity_type": "rfi", "id": "r2"}))
    assert out == {"deleted": {"resource": "rfi", "id": "r2"}}
    rows = _watch(db, "super1", P2)
    assert rows and rows[0]["route"] == "copilot:delete_entity"


def test_copilot_turn_write_tools_follow_the_caller(db, enforced, monkeypatch):
    """The orchestrator checks the session project before any write tool,
    using the caller's sign-in, so a plan the model writes cannot act on a
    project the caller is not assigned to."""
    ct = importlib.import_module("services.copilot_tools")
    ai_agents = importlib.import_module("services.ai_agents")
    spec_search = importlib.import_module("services.spec_search")
    plans = []

    class FakeChat:
        def __init__(self, **kw):
            pass

        def with_model(self, *a):
            return self

        async def send_message(self, msg):
            return plans.pop(0) if plans else json.dumps({"actions": [], "reply": "done"})

    async def _blank(*a, **k):
        return ""

    async def _no_specs(**k):
        return []
    monkeypatch.setattr(ct, "LlmChat", FakeChat)
    monkeypatch.setattr(ai_agents, "_master_doc_block", _blank)
    monkeypatch.setattr(ai_agents, "_kb_block", _blank)
    monkeypatch.setattr(spec_search, "search_all_specs_in_project", _no_specs)
    plan = json.dumps({"actions": [{"tool": "create_floor",
                                    "args": {"floor_number": 1, "floor_name": "L1"}}],
                       "reply": "x"})
    plans.append(plan)
    res = run(ct.execute_copilot_turn(
        tenant_id=A, project_id=P2, user_id="super1", user_role="super",
        session_id="s", message="add a floor", access_ctx=_ctx("super1")))
    assert res["tool_results"][0]["result"]["error"] == pa.NOT_ASSIGNED
    assert not db.floors.docs
    plans.append(plan)
    res = run(ct.execute_copilot_turn(
        tenant_id=A, project_id=P1, user_id="super1", user_role="super",
        session_id="s", message="add a floor", access_ctx=_ctx("super1")))
    assert "created" in res["tool_results"][0]["result"]
    assert [f["project_id"] for f in db.floors.docs] == [P1]
    assert db.copilot_messages.docs[-1]["project_id"] == P1


# ------------------------------------------------------------ WebSocket
class _Sock:
    def __init__(self):
        self.sent = []

    async def send_text(self, m):
        self.sent.append(json.loads(m))


def _sockets(monkeypatch, wr):
    s_p1, s_all, s_unknown = _Sock(), _Sock(), _Sock()
    monkeypatch.setattr(wr, "_subs", {A: {s_p1, s_all, s_unknown}})
    monkeypatch.setattr(wr, "_owners", {s_p1: "super1", s_all: "admin1", s_unknown: "pm1"})
    monkeypatch.setattr(wr, "_allowed", {s_p1: frozenset({P1}), s_all: None})
    monkeypatch.setattr(wr, "_limited", set())
    monkeypatch.setattr(wr, "_lock", asyncio.Lock())
    return s_p1, s_all, s_unknown


def test_ws_project_messages_only_reach_allowed_sockets(db, enforced, monkeypatch):
    wr = importlib.import_module("routers.ws_router")
    s_p1, s_all, s_unknown = _sockets(monkeypatch, wr)
    run(wr.broadcast(A, {"kind": "task_progress", "project_id": P2}))
    # A socket whose project list is not known yet gets no project
    # messages (fail closed) until the minute-ly re-check fills it in.
    assert not s_p1.sent and len(s_all.sent) == 1 and not s_unknown.sent
    run(wr.broadcast(A, {"kind": "notification", "payload": {"project_id": P1}}))
    assert len(s_p1.sent) == 1
    run(wr.broadcast(A, {"kind": "hello-everyone"}))
    assert len(s_p1.sent) == 2 and len(s_all.sent) == 3 and len(s_unknown.sent) == 1


def test_ws_unfiltered_when_off(db, monkeypatch):
    wr = importlib.import_module("routers.ws_router")
    s_p1, s_all, s_unknown = _sockets(monkeypatch, wr)
    run(wr.broadcast(A, {"kind": "task_progress", "project_id": P2}))
    assert len(s_p1.sent) == len(s_all.sent) == len(s_unknown.sent) == 1


def test_ws_limited_admin_filtered_when_off(db, monkeypatch):
    wr = importlib.import_module("routers.ws_router")
    s_p1, s_all, s_unknown = _sockets(monkeypatch, wr)
    wr._limited.add(s_p1)  # s_p1 now plays an admin limited to P1
    run(wr.broadcast(A, {"kind": "task_progress", "project_id": P2}))
    assert not s_p1.sent and len(s_all.sent) == len(s_unknown.sent) == 1
    run(wr.broadcast(A, {"kind": "task_progress", "project_id": P1}))
    assert len(s_p1.sent) == 1


def test_ws_access_for_flags_limited_admin(db):
    wr = importlib.import_module("routers.ws_router")
    assert run(wr._access_for(_token("admin2"), A)) == (frozenset({P2}), True)
    assert run(wr._access_for(_token("super1"), A))[1] is False
    assert run(wr._access_for(_token("admin1"), A)) == (None, False)


def test_ws_allowed_set_matches_rest_rules(db):
    wr = importlib.import_module("routers.ws_router")
    assert run(wr._allowed_for(_token("super1"), A)) == frozenset({P1})
    assert run(wr._allowed_for(_token("admin2"), A)) == frozenset({P2})
    assert run(wr._allowed_for(_token("admin1"), A)) is None


# ------------------------------------------------------------ route coverage
# Path ids the route-level dependency cannot map to a project, and why that
# is fine. Anything new here must be checked in its handler or be
# company-wide on purpose.
_UNMAPPED_OK = {
    ("routers.iter5_router", "rate_id"),            # company cost rates
    ("routers.iter7_router", "session_id"),         # payment: posting checked in handler
    ("routers.system_router", "session_id"),        # caller's own assistant history
    ("routers.resource_leveling_router", "crew_id"),  # always-on home-project check
    ("routers.resource_leveling_router", "gap_id"),   # always-on contributing-project check
    ("routers.vendors_router", "vid"),              # company vendor list
    # The record TYPE word in generic routes; the id after it is mapped.
    ("routers.field_suggestions_router", "entity_type"),
    ("routers.iter8_router", "entity"),
}


def test_every_path_id_is_covered_or_known():
    missing = []
    for name in ROUTER_MODULES:
        router = importlib.import_module(name).router
        for route in router.routes:
            path = getattr(route, "path", "")
            segs = [s for s in path.split("/") if s]
            for i, seg in enumerate(segs):
                m = re.fullmatch(r"\{([^}:]+)(?::[^}]*)?\}", seg)
                if not m:
                    continue
                param = m.group(1)
                prev = segs[i - 1] if i else ""
                if (param in pa.PROJECT_PARAMS or prev in pa.SEGMENT_KINDS
                        or re.fullmatch(r"\{.*\}", prev)
                        or (name, param) in _UNMAPPED_OK):
                    continue
                missing.append((name, path, param))
    assert missing == []


def test_tenant_level_routes_are_marked():
    """Company-wide routes in these files carry a '# project-scope:
    tenant-level' comment just above their decorator."""
    import inspect
    for name, expected in (("routers.vendors_router", 8), ("routers.resources_router", 9)):
        src = inspect.getsource(importlib.import_module(name))
        lines = src.splitlines()
        marked = [i for i, ln in enumerate(lines) if ln.startswith("@router.")
                  and lines[i - 1].startswith("# project-scope: tenant-level")]
        assert len(marked) == expected
