"""AI unavailable, second round (2026-11-16).

Every AI feature gives the same plain 422 "ai_unavailable" answer with no AI
key, and nothing made up or empty is saved:

  * delay "Generate AI report": an earlier report is kept (it used to be
    overwritten by the raw key error, which then printed in the client PDF),
    and a failed AI call raises instead of returning error text;
  * RFI drafter: no error text as a draft and no suggested RFI body;
  * recovery suggestions: no empty "pending" result row;
  * mitigation scenarios and the schedule-change summary: 422, not 500/503;
  * old dev-stub photo results never feed field suggestions or the tray list;
  * the AI assistant's write tools follow the daily report role and author
    rules (a viewer's "mark A1240 70% complete" changes nothing).

Offline: reuses the small in-memory fake database from test_ai_unavailable
(loaded from its file, not registered in sys.modules).
"""
import asyncio
import importlib.util
import json
import os

import pytest

from services.ai_unavailable import (
    AiUnavailableError,
    DELAY_REPORT_UNAVAILABLE_MESSAGE,
    RFI_DRAFT_UNAVAILABLE_MESSAGE,
    RECOVERY_UNAVAILABLE_MESSAGE,
    MITIGATION_UNAVAILABLE_MESSAGE,
    SCHEDULE_NARRATIVE_UNAVAILABLE_MESSAGE,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_ai_unavail_fakes", os.path.join(_HERE, "test_ai_unavailable.py"))
U = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(U)  # not added to sys.modules

class FakeDB(U.FakeDB):
    def __getitem__(self, name):  # db["delays"] as well as db.delays
        return getattr(self, name)


TENANT, CTX = U.TENANT, U.CTX
no_ai = U.no_ai


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("PHOTO_AI_DEV_MODE", raising=False)


async def _ok(*_a, **_k):
    return None


class _FailingChat:
    async def send_message(self, *_a, **_k):
        raise RuntimeError("401 invalid x-api-key sk-ant-REDACTED")


# ------------------------------------------------------------ delay AI report
def test_delay_report_without_key_keeps_the_earlier_report(no_ai, monkeypatch):
    import routers.field_events_router as fer
    db = FakeDB()
    db.delays.docs.append({"id": "d1", "tenant_id": TENANT, "project_id": "p1",
                           "ai_report": "Good narrative written last week."})
    monkeypatch.setattr(fer, "get_db", lambda: db)
    with pytest.raises(AiUnavailableError) as ei:
        run(fer.generate_delay_ai_report("d1", CTX))
    assert ei.value.detail == DELAY_REPORT_UNAVAILABLE_MESSAGE
    assert db.delays.docs[0]["ai_report"] == "Good narrative written last week."
    assert db.delays.writes == 0


def test_failed_delay_report_call_raises_instead_of_returning_error_text(with_key, monkeypatch):
    import services.ai_agents as aa
    db = FakeDB()
    monkeypatch.setattr(aa, "get_db", lambda: db)
    monkeypatch.setattr(aa, "_new_chat", lambda *a, **k: _FailingChat())
    monkeypatch.setattr(aa, "_track_usage", _ok)
    with pytest.raises(AiUnavailableError) as ei:
        run(aa.generate_delay_report(tenant_id=TENANT, delay={"id": "d1", "description": "rain"}))
    assert ei.value.detail == DELAY_REPORT_UNAVAILABLE_MESSAGE
    assert "x-api-key" not in ei.value.detail
    assert db.field_suggestions.writes == 0


# ------------------------------------------------------------ RFI drafter
def _rfi_env(monkeypatch):
    import routers.system_router as sr
    from services import field_suggestions as fs
    db = FakeDB()
    db.rfis.docs.append({"id": "rfi1", "tenant_id": TENANT, "project_id": "p1", "body": ""})
    made = []

    async def _rec(**kw):
        made.append(kw)
    monkeypatch.setattr(sr, "get_db", lambda: db)
    monkeypatch.setattr(sr, "check_project_access", _ok)
    monkeypatch.setattr(fs, "create_suggestion", _rec)
    from models import RfiDraftRequest
    payload = RfiDraftRequest(project_id="p1", subject="Ceiling height", context="Room 305",
                              rfi_id="rfi1")
    return sr, payload, made


def test_rfi_draft_without_key_is_422_and_suggests_nothing(no_ai, monkeypatch):
    sr, payload, made = _rfi_env(monkeypatch)
    with pytest.raises(AiUnavailableError) as ei:
        run(sr.ai_rfi_draft(payload, CTX))
    assert ei.value.detail == RFI_DRAFT_UNAVAILABLE_MESSAGE
    assert made == []


def test_failed_rfi_draft_call_saves_no_error_text(with_key, monkeypatch):
    import services.ai_agents as aa
    sr, payload, made = _rfi_env(monkeypatch)
    monkeypatch.setattr(aa, "get_db", lambda: FakeDB())
    monkeypatch.setattr(aa, "_new_chat", lambda *a, **k: _FailingChat())
    monkeypatch.setattr(aa, "_track_usage", _ok)
    out = run(aa.rfi_draft(tenant_id=TENANT, project_id="p1", subject="s", context="c",
                           schedule_activity_id=None))
    assert out["ok"] is False and out["draft"] == ""
    with pytest.raises(AiUnavailableError):
        run(sr.ai_rfi_draft(payload, CTX))
    assert made == []


# ------------------------------------------------------------ recovery
def test_recovery_without_key_is_422_and_saves_nothing(no_ai, monkeypatch):
    import routers.system_router as sr
    from models import RecoveryRequest
    db = FakeDB()
    monkeypatch.setattr(sr, "check_project_access", _ok)
    monkeypatch.setattr(sr, "check_record_access", _ok)
    with pytest.raises(AiUnavailableError) as ei:
        run(sr.ai_recovery(RecoveryRequest(project_id="p1", target_activity_id="t1"), CTX))
    assert ei.value.detail == RECOVERY_UNAVAILABLE_MESSAGE
    assert db.ai_recovery_suggestions.docs == []


def test_failed_recovery_call_saves_no_empty_pending_row(with_key, monkeypatch):
    import services.ai_agents as aa
    db = FakeDB()
    db.tasks.docs.append({"id": "t1", "tenant_id": TENANT, "project_id": "p1",
                          "activity_code": "A1", "description": "Slab"})
    monkeypatch.setattr(aa, "get_db", lambda: db)
    monkeypatch.setattr(aa, "_new_chat", lambda *a, **k: _FailingChat())
    monkeypatch.setattr(aa, "_track_usage", _ok)
    with pytest.raises(AiUnavailableError):
        run(aa.recovery_suggestions(tenant_id=TENANT, project_id="p1",
                                    target_activity_id="t1", trigger="manual"))
    assert db.ai_recovery_suggestions.docs == []


# ------------------------------------------------------------ mitigation + narrative
def test_mitigation_scenarios_without_key_is_422(no_ai, monkeypatch):
    import routers.mitigations_router as mr
    db = FakeDB()
    db.delays.docs.append({"id": "d1", "tenant_id": TENANT, "project_id": "p1"})
    monkeypatch.setattr(mr, "get_db", lambda: db)
    with pytest.raises(AiUnavailableError) as ei:
        run(mr.generate_scenarios("d1", CTX))
    assert ei.value.detail == MITIGATION_UNAVAILABLE_MESSAGE
    assert db.total_writes() == 0


def test_schedule_change_summary_without_key_is_422_not_503(no_ai, monkeypatch):
    import routers.schedule_diffs_router as sdr
    db = FakeDB()
    db.schedule_update_diffs.docs.append({"id": "x1", "tenant_id": TENANT})
    monkeypatch.setattr(sdr, "get_db", lambda: db)
    with pytest.raises(AiUnavailableError) as ei:
        run(sdr.generate_ai_narrative("x1", CTX))
    assert ei.value.status_code == 422
    assert ei.value.detail == SCHEDULE_NARRATIVE_UNAVAILABLE_MESSAGE


# ------------------------------------------------------------ old dev-stub rows
STUB_TRAY = {
    "id": "trS", "tenant_id": TENANT, "project_id": "p1", "status": "untagged",
    "ai_analysis_status": "complete", "ai_model_version": "dev-stub-v1",
    "ai_confidence_score": 0.87, "ai_suggested_class": "safety",
    "ai_suggested_event_type": "safety_event", "ai_suggested_trade": "general",
    "ai_suggested_caption": "Worker near open edge", "uploaded_at": "2026-11-01",
}


def test_stub_row_creates_no_field_suggestions(no_ai, monkeypatch):
    import routers.photo_tray_router as ptr
    from services import field_suggestions as fs
    made = []

    async def _rec(**kw):
        made.append(kw)
    monkeypatch.setattr(fs, "create_suggestion", _rec)
    run(ptr._create_photo_ai_field_suggestions(
        tenant_id=TENANT, project_id="p1", tray=dict(STUB_TRAY),
        ref_field="safety_event_id", ref_id="se1", event_doc={},
        actor_id="u1", request_id="r1"))
    assert made == []


def test_tray_list_and_detail_hide_stub_results(no_ai, monkeypatch):
    import routers.photo_tray_router as ptr
    db = FakeDB()
    real = {**STUB_TRAY, "id": "trR", "ai_model_version": "claude-real",
            "ai_suggested_class": "progress"}
    db.photo_tray.docs += [dict(STUB_TRAY), real]
    monkeypatch.setattr(ptr, "get_db", lambda: db)
    rows = run(ptr.list_tray("p1", status=None, capture_source=None, date_from=None,
                             date_to=None, untagged_only=False, ctx=CTX))
    by_id = {r["id"]: r for r in rows}
    stub = by_id["trS"]
    assert stub["ai_analysis_status"] == "failed"
    for k in ("ai_suggested_class", "ai_suggested_event_type", "ai_suggested_trade",
              "ai_suggested_caption", "ai_confidence_score"):
        assert stub[k] is None, k
    assert by_id["trR"]["ai_suggested_class"] == "progress"
    detail = run(ptr.get_tray_entry("trS", CTX))
    assert detail["tray"]["ai_suggested_class"] is None
    # The saved row is left as it is.
    assert db.photo_tray.docs[0]["ai_suggested_class"] == "safety"


def test_stub_rows_still_show_in_dev_mode(monkeypatch):
    from services.photo_ai import without_dev_stub_ai
    monkeypatch.setenv("PHOTO_AI_DEV_MODE", "1")
    assert without_dev_stub_ai(dict(STUB_TRAY))["ai_suggested_class"] == "safety"


# ------------------------------------------------------------ AI assistant write tools
class _PlanChat:
    """Returns one plan with the given actions, then a final reply."""

    def __init__(self, actions):
        self._plans = [{"actions": actions, "reply": "Done."},
                       {"actions": [], "reply": "Done."}]

    def with_model(self, *_a, **_k):
        return self

    async def send_message(self, *_a, **_k):
        return json.dumps(self._plans.pop(0) if self._plans else {"actions": [], "reply": "ok"})


def _copilot_env(monkeypatch, actions):
    import services.copilot_tools as ct
    import services.edit_permissions as ep
    db = FakeDB()
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": TENANT, "project_id": None,
                                  "report_date": "2026-11-16", "status": "draft",
                                  "prepared_by": "super1"})
    db.daily_report_activities.docs.append({
        "id": "row1", "daily_report_id": "dr1", "activity_code": "A1240",
        "schedule_activity_id": "t1", "pct_complete_end_of_day": 20, "crew_count": 0})
    db.tasks.docs.append({"id": "t1", "tenant_id": TENANT, "activity_code": "A1240",
                          "pct_complete": 20})
    db.delays.docs.append({"id": "dl1", "tenant_id": TENANT, "daily_report_id": "dr1",
                           "description": "rain", "status": "open"})
    monkeypatch.setattr(ct, "get_db", lambda: db)
    monkeypatch.setattr(ep, "get_db", lambda: db)
    monkeypatch.setattr(ct, "LlmChat", lambda **_k: _PlanChat(actions))
    return ct, db


DR_WRITES = [
    {"tool": "mark_activity_status", "args": {"activity_code": "A1240",
                                              "pct_complete_end_of_day": 70}},
    {"tool": "record_presence", "args": {"trade": "Carpenters", "headcount": 2}},
    {"tool": "create_delay", "args": {"description": "wind"}},
    {"tool": "create_daily_report", "args": {"report_date": "2026-11-17"}},
    {"tool": "delete_entity", "args": {"entity_type": "delay", "id": "dl1"}},
    {"tool": "update_entity", "args": {"entity_type": "delay", "id": "dl1",
                                       "fields": {"description": "x"}}},
]

_SNAP = ("daily_reports", "daily_report_activities", "daily_presence", "tasks", "delays")


@pytest.mark.parametrize("uid,role", [("viewer1", "viewer"), ("super2", "super")])
def test_assistant_daily_report_writes_follow_role_and_author_rules(uid, role, monkeypatch):
    ct, db = _copilot_env(monkeypatch, DR_WRITES)
    before = {n: [dict(d) for d in getattr(db, n).docs] for n in _SNAP}
    out = run(ct.execute_copilot_turn(
        tenant_id=TENANT, project_id=None, user_id=uid, user_role=role,
        session_id="s1", message="mark A1240 70% complete and log 2 crew",
        daily_report_id="dr1"))
    assert {n: getattr(db, n).docs for n in _SNAP} == before
    assert len(out["tool_results"]) == len(DR_WRITES)
    # A superintendent may still open (create) a day's report; here it
    # already exists and is returned unchanged. Every other write is refused.
    refused = [r for r in out["tool_results"]
               if not (role == "super" and r["tool"] == "create_daily_report")]
    assert all("error" in (r.get("result") or {}) for r in refused), out["tool_results"]


def test_assistant_author_can_still_mark_progress(monkeypatch):
    ct, db = _copilot_env(monkeypatch, DR_WRITES[:1])
    out = run(ct.execute_copilot_turn(
        tenant_id=TENANT, project_id=None, user_id="super1", user_role="super",
        session_id="s1", message="mark A1240 70% complete", daily_report_id="dr1"))
    assert "updated" in (out["tool_results"][0].get("result") or {}), out["tool_results"]
    assert db.daily_report_activities.docs[0]["pct_complete_end_of_day"] == 70.0
