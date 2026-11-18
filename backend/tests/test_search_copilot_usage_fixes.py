"""Assistant fixes found by the usage review (2026-11-17).

Checked here, all offline with the in-memory fake database:
  * mark_activity_status refuses a submitted or approved (locked) daily
    report, exactly like record_presence and create_unplanned_activity;
  * POST /ai/copilot checks the daily report in focus as a read, so someone
    who may only read it can still ask about it; the write tools still refuse
    a read-only role on that report;
  * delete_entity by number (activity code, RFI number, submittal number)
    looks only in the selected project, and with no project selected asks
    which one when the number is used in more than one project;
  * the photo instructions no longer mention a "Send-to-form" button;
  * create_task never hands out an activity code already used in the project;
  * the role limits on write tools (viewers write nothing, a submittal
    technician writes submittals, a scheduling engineer writes schedule
    activities) apply whatever the search switch is set to;
  * after "which project?", delete_entity takes project_id;
  * create_unplanned_activity checks the project before the report lock.
"""
import importlib
import inspect
import json

import pytest
from fastapi import HTTPException

from test_project_access_routers import db, enforced, _ctx, _watch  # noqa: F401
from test_project_access_foundation import A, P1, P2, P3, run
from test_search_copilot import turn, search_off, search_on  # noqa: F401
import auth

ct = importlib.import_module("services.copilot_tools")
sr = importlib.import_module("routers.system_router")


def _grid_row(dbh, dr_id="dr1", code="A100"):
    dbh.daily_report_activities.docs.append({
        "id": f"row-{dr_id}", "tenant_id": A, "daily_report_id": dr_id,
        "activity_code": code, "pct_complete_end_of_day": 10, "notes": "before"})


def _set_status(dbh, dr_id, status):
    for d in dbh.daily_reports.docs:
        if d["id"] == dr_id:
            d["status"] = status


# ------------------------------------------------------------ P3 locked report
@pytest.mark.parametrize("status", ["submitted", "approved"])
def test_mark_activity_status_refuses_locked_report(db, status):  # noqa: F811
    _grid_row(db)
    _set_status(db, "dr1", status)
    ctx = {**_ctx("super1"), "project_id": P1, "daily_report_id": "dr1"}
    out = run(ct._tool_mark_activity_status(ctx, {"activity_code": "A100",
                                                  "pct_complete_end_of_day": 80}))
    assert out == {"error": "daily report locked"}
    row = db.daily_report_activities.docs[0]
    assert row["pct_complete_end_of_day"] == 10 and row["notes"] == "before"


def test_mark_activity_status_still_changes_a_draft(db):  # noqa: F811
    _grid_row(db)
    ctx = {**_ctx("super1"), "project_id": P1, "daily_report_id": "dr1"}
    out = run(ct._tool_mark_activity_status(ctx, {"activity_code": "A100", "notes": "after"}))
    assert "updated" in out, out
    assert db.daily_report_activities.docs[0]["notes"] == "after"


def test_record_presence_also_refuses_an_approved_report(db):  # noqa: F811
    _set_status(db, "dr1", "approved")
    ctx = {**_ctx("super1"), "project_id": P1, "daily_report_id": "dr1"}
    out = run(ct._tool_record_presence(ctx, {"trade": "Carpentry", "headcount": 3}))
    assert out == {"error": "daily report locked"}
    assert not db.daily_presence.docs


# ------------------------------------------------------------ P4 report in focus
def _fake_turn(monkeypatch):
    called = []

    async def _turn(**kw):
        called.append(kw)
        return {"reply": "ok"}
    monkeypatch.setattr(ct, "execute_copilot_turn", _turn)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return called


def test_reader_with_report_in_focus_can_ask(db, enforced, monkeypatch):  # noqa: F811
    called = _fake_turn(monkeypatch)
    msg = sr.CopilotMessage(message="what happened on this report?",
                            project_id=P1, daily_report_id="dr1")
    run(sr.ai_copilot(msg, _ctx("viewer1")))
    assert len(called) == 1 and called[0]["daily_report_id"] == "dr1"
    # A report on a project the caller is not on is still refused, as a
    # read (404), and the assistant is not run.
    msg = sr.CopilotMessage(message="what happened?", daily_report_id="dr2")
    with pytest.raises(HTTPException) as e:
        run(sr.ai_copilot(msg, _ctx("viewer1")))
    assert e.value.status_code == 404
    assert len(called) == 1


def test_report_in_focus_is_logged_as_a_read_when_off(db, monkeypatch):  # noqa: F811
    called = _fake_turn(monkeypatch)
    msg = sr.CopilotMessage(message="what happened?", daily_report_id="dr2")
    run(sr.ai_copilot(msg, _ctx("super1")))
    assert len(called) == 1
    rows = _watch(db, "super1", P2)
    assert rows and rows[0]["write"] is False


@pytest.mark.parametrize("tool", ["mark_activity_status", "record_presence",
                                  "create_unplanned_activity", "create_qc_issue",
                                  "create_delay", "create_safety_event"])
def test_write_tools_still_refuse_a_reader_on_the_report(db, tool, monkeypatch):  # noqa: F811
    ep = importlib.import_module("services.edit_permissions")
    monkeypatch.setattr(ep, "get_db", lambda: db)
    ctx = {**_ctx("viewer1"), "project_id": P1, "daily_report_id": "dr1"}
    out = run(ct._daily_report_guard(ctx, tool, {}))
    # The read-only-role refusal itself, not some other failure.
    assert out == {"error": auth.DAILY_REPORT_READ_ONLY_MESSAGE}
    # Control: a superintendent on P1 may use the same tool on that report.
    ctx = {**_ctx("super1"), "project_id": P1, "daily_report_id": "dr1"}
    assert run(ct._daily_report_guard(ctx, tool, {})) is None


def test_report_linked_write_checks_the_report_project(db, enforced, monkeypatch):  # noqa: F811
    # The report in focus is on a project the caller is not on: the write
    # is refused by the tool, whatever the route let through.
    ep = importlib.import_module("services.edit_permissions")
    monkeypatch.setattr(ep, "get_db", lambda: db)
    ctx = {**_ctx("super1"), "project_id": P1, "daily_report_id": "dr2"}
    out = run(ct._daily_report_guard(ctx, "create_qc_issue", {"description": "x"}))
    assert out and "error" in out
    _grid_row(db, "dr2")
    out = run(ct._tool_mark_activity_status(ctx, {"activity_code": "A100", "notes": "x"}))
    assert "error" in out
    assert db.daily_report_activities.docs[0]["notes"] == "before"


# ------------------------------------------------------------ P5 lookups by number
def _same_number_on_two_projects(dbh):
    dbh.rfis.docs.append({"id": "r3", "tenant_id": A, "project_id": P2,
                          "rfi_number": "RFI-1", "subject": "Other pour", "status": "sent"})


def _rfi(dbh, rid):
    return next(r for r in dbh.rfis.docs if r["id"] == rid)


def test_delete_by_number_stays_in_selected_project(db):  # noqa: F811
    _same_number_on_two_projects(db)
    ctx = {**_ctx("admin1"), "project_id": P2}
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "rfi", "rfi_number": "RFI-1"}))
    assert out == {"deleted": {"resource": "rfi", "id": "r3"}}
    assert _rfi(db, "r1")["status"] == "sent"


def test_delete_by_number_not_in_selected_project(db):  # noqa: F811
    ctx = {**_ctx("admin1"), "project_id": P3}
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "rfi", "rfi_number": "RFI-1"}))
    assert "error" in out and "not found" in out["error"]
    assert _rfi(db, "r1")["status"] == "sent"


def test_delete_by_number_with_no_project_asks_which(db):  # noqa: F811
    _same_number_on_two_projects(db)
    ctx = _ctx("admin1")
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "rfi", "rfi_number": "RFI-1"}))
    assert "error" in out and "which project" in out["error"]
    assert _rfi(db, "r1")["status"] == "sent" and _rfi(db, "r3")["status"] == "sent"
    # A number used once is still found company-wide.
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "rfi", "rfi_number": "RFI-2"}))
    assert out == {"deleted": {"resource": "rfi", "id": "r2"}}


def test_delete_task_by_code_stays_in_selected_project(db):  # noqa: F811
    db.tasks.docs += [
        {"id": "t1", "tenant_id": A, "project_id": P1, "activity_code": "A0100", "status": "not_started"},
        {"id": "t2", "tenant_id": A, "project_id": P2, "activity_code": "A0100", "status": "not_started"},
    ]
    ctx = {**_ctx("admin1"), "project_id": P1}
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "task", "activity_code": "A0100"}))
    assert out == {"deleted": {"resource": "task", "id": "t1"}}
    assert db.tasks.docs[1]["status"] == "not_started"


def test_guard_checks_the_record_the_delete_will_hit(db, monkeypatch):  # noqa: F811
    # r1 (P1, RFI-1) is filed on a draft report someone else started; r3
    # (P2, RFI-1) is on no report. A superintendent who is not the author
    # may delete r3 but not r1, so the guard's answer depends on which RFI-1
    # it looks at. (The old company-wide lookup found r1 first.)
    ep = importlib.import_module("services.edit_permissions")
    monkeypatch.setattr(ep, "get_db", lambda: db)
    _same_number_on_two_projects(db)
    _rfi(db, "r1")["daily_report_id"] = "dr1"
    for d in db.daily_reports.docs:
        if d["id"] == "dr1":
            d["prepared_by"] = "pm1"
    db.project_members.docs.append({"id": "pm-super1-p2", "tenant_id": A, "project_id": P2,
                                    "user_id": "super1", "role_on_project": "super",
                                    "added_at": "2026-11-01T00:00:00+00:00"})
    args = {"entity_type": "rfi", "rfi_number": "RFI-1"}
    ctx = {**_ctx("super1"), "project_id": P2}
    row, err = run(ct._find_by_code(ctx, "rfi", args))
    assert err is None and row["id"] == "r3"
    assert run(ct._daily_report_guard(ctx, "delete_entity", args)) is None
    out = run(ct._tool_delete_entity(ctx, args))
    assert out == {"deleted": {"resource": "rfi", "id": "r3"}}
    assert _rfi(db, "r1")["status"] == "sent"
    # With P1 selected the guard looks at r1 and refuses (not the author).
    ctx = {**_ctx("super1"), "project_id": P1}
    out = run(ct._daily_report_guard(ctx, "delete_entity", args))
    assert out and "error" in out


def test_delete_by_number_takes_project_from_the_call(db):  # noqa: F811
    # No project selected and RFI-1 used twice: the "which project" error
    # names the projects, and the answer is passed back as project_id.
    _same_number_on_two_projects(db)
    ctx = _ctx("admin1")
    args = {"entity_type": "rfi", "rfi_number": "RFI-1"}
    out = run(ct._tool_delete_entity(ctx, args))
    assert "project_id" in out["error"] and out["project_ids"] == sorted([P1, P2])
    out = run(ct._tool_delete_entity(ctx, {**args, "project_id": P2}))
    assert out == {"deleted": {"resource": "rfi", "id": "r3"}}
    assert _rfi(db, "r1")["status"] == "sent"


def test_delete_project_id_offered_only_in_the_search_on_list():
    on = ct._tool_catalogue(True)
    line = next(ln for ln in on.splitlines() if "- delete_entity" in ln)
    assert "project_id?: str" in line
    off = next(ln for ln in ct._tool_catalogue(False).splitlines() if "- delete_entity" in ln)
    assert "project_id" not in off


# ------------------------------------------------------------ P6 photo instructions
def test_photo_instructions_have_no_send_to_form_button():
    src = inspect.getsource(ct)
    assert "Send-to-form" not in src
    assert "to create the record unless" in src


# ------------------------------------------------------------ P8 task codes
@pytest.mark.parametrize("codes,expected", [
    (["A0002", "B0001"], "A0003"),         # old rule gave A0002 again
    (["A9", "A0010"], "A0011"),            # old rule gave A0010 again
    (["XY9-1234", "AF2311", "A0005"], "A0006"),
    (["XY9-1234", "AF2311"], "A0001"),
    ([], "A0001"),
])
def test_create_task_code_is_unique_in_project(db, codes, expected):  # noqa: F811
    for i, c in enumerate(codes):
        db.tasks.docs.append({"id": f"t{i}", "tenant_id": A, "project_id": P1,
                              "activity_code": c})
    # Another project's codes do not matter.
    db.tasks.docs.append({"id": "tx", "tenant_id": A, "project_id": P2, "activity_code": "A0500"})
    ctx = {**_ctx("admin1"), "project_id": P1}
    out = run(ct._tool_create_task(ctx, {"description": "Firestopping corridor",
                                         "planned_start": "2026-11-23", "duration_days": 3}))
    code = out["created"]["activity_code"]
    assert code == expected
    in_p1 = [t["activity_code"] for t in db.tasks.docs if t["project_id"] == P1]
    assert in_p1.count(code) == 1


# ------------------------------------------------------------ P3 check order
def test_unplanned_activity_checks_project_before_lock(db, enforced):  # noqa: F811
    # super1 is not on P2: told so, not that P2's report is locked.
    _set_status(db, "dr2", "submitted")
    ctx = {**_ctx("super1"), "daily_report_id": "dr2"}
    out = run(ct._tool_create_unplanned_activity(ctx, {"description": "Patch slab"}))
    assert "error" in out and out != {"error": "daily report locked"}
    assert not db.tasks.docs
    # On their own project's locked report they get the lock message.
    _set_status(db, "dr1", "approved")
    ctx = {**_ctx("super1"), "daily_report_id": "dr1"}
    out = run(ct._tool_create_unplanned_activity(ctx, {"description": "Patch slab"}))
    assert out == {"error": "daily report locked"}


def test_locked_statuses_shared_with_the_screens():
    ep = importlib.import_module("services.edit_permissions")
    assert ct._LOCKED_REPORT_STATUSES is ep._LOCKED_STATUSES


# ------------------------------------------------------------ P7 role limits, switch off
def _plan(tool, args):
    return [json.dumps({"actions": [{"tool": tool, "args": args}], "reply": ""}),
            json.dumps({"actions": [], "reply": "ok"})]


@pytest.mark.parametrize("user,role,tool,args", [
    ("viewer1", "viewer", "create_task", {"description": "Hang doors"}),
    ("viewer1", "viewer", "delete_entity", {"entity_type": "rfi", "id": "r1"}),
    ("scheduling_engineer1", "scheduling_engineer", "update_entity",
     {"entity_type": "rfi", "id": "r1", "fields": {"subject": "changed"}}),
    ("submittal_tech1", "submittal_tech", "create_task", {"description": "Hang doors"}),
    # A soft delete through update_entity is a delete: refused for a
    # scheduling engineer even on a task.
    ("scheduling_engineer1", "scheduling_engineer", "update_entity",
     {"entity_type": "task", "id": "t1", "fields": {"status": "deleted"}}),
])
def test_role_limits_apply_with_search_off(search_off, turn, db, user, role, tool, args):  # noqa: F811
    run_turn = turn[0]
    db.tasks.docs.append({"id": "t1", "tenant_id": A, "project_id": P1,
                          "activity_code": "A0001", "description": "Frame walls",
                          "status": "not_started"})
    before_tasks = [dict(t) for t in db.tasks.docs]
    res = run_turn("please do it", replies=_plan(tool, args), user=user, role=role)
    rec = res["tool_results"][0]
    assert rec["tool"] == tool and "refused" in rec["result"], rec
    assert "your role" in rec["result"]["refused"]
    assert db.tasks.docs == before_tasks
    assert _rfi(db, "r1")["status"] == "sent" and _rfi(db, "r1")["subject"] == "Pour one"


def test_super_can_still_create_a_task_with_search_off(search_off, turn, db):  # noqa: F811
    run_turn = turn[0]
    res = run_turn("add a task to hang doors",
                   replies=_plan("create_task", {"description": "Hang doors"}))
    rec = res["tool_results"][0]
    assert "created" in rec["result"], rec
    assert [t["description"] for t in db.tasks.docs] == ["Hang doors"]


def test_role_refusal_matches_write_refusal_roles():
    assert ct._role_refusal("create_task", {}, "viewer")
    assert ct._role_refusal("create_submittal", {}, "submittal_tech") is None
    assert ct._role_refusal("update_entity", {"entity_type": "submittal"}, "submittal_tech") is None
    assert ct._role_refusal("update_entity", {"entity_type": "rfi"}, "submittal_tech")
    assert ct._role_refusal("create_task", {}, "super") is None
    assert ct._role_refusal("delete_entity", {}, "pm") is None


# ------------------------------------------------------------ evaluation fixes (2026-11-17)
def test_sign_off_fields_are_never_written_by_the_assistant(db):  # noqa: F811
    db.tasks.docs.append({"id": "t9", "tenant_id": A, "project_id": P1,
                          "activity_code": "U-1", "status": "not_started",
                          "requires_pm_review": True})
    args = {"entity_type": "task", "id": "t9", "fields": {"requires_pm_review": False}}
    for role in ("admin", "pm", "super"):
        assert "signed off by a person" in ct._role_refusal("update_entity", args, role)
    ctx = {**_ctx("admin1"), "project_id": P1}
    out = run(ct._tool_update_entity(ctx, args))
    assert "error" in out and db.tasks.docs[-1]["requires_pm_review"] is True
    # Ordinary fields still change.
    ok = {"entity_type": "task", "id": "t9", "fields": {"description": "x"}}
    assert ct._role_refusal("update_entity", ok, "pm") is None


def test_short_id_resolves_to_the_one_matching_record(db):  # noqa: F811
    db.delays.docs.append({"id": "5b19a56e-fe5f-42c1-a606-7978ea9abe88", "tenant_id": A,
                           "project_id": P1, "status": "open"})
    ctx = {**_ctx("admin1"), "project_id": P1}
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "delay", "id": "5b19a56e"}))
    assert out == {"deleted": {"resource": "delay", "id": "5b19a56e-fe5f-42c1-a606-7978ea9abe88"}}
    # Two records starting the same way: nothing is guessed.
    db.delays.docs += [{"id": "abcdef01-1", "tenant_id": A, "project_id": P1, "status": "open"},
                       {"id": "abcdef01-2", "tenant_id": A, "project_id": P1, "status": "open"}]
    out = run(ct._tool_delete_entity(ctx, {"entity_type": "delay", "id": "abcdef01"}))
    assert "error" in out
    assert all(d["status"] == "open" for d in db.delays.docs if d["id"].startswith("abcdef01"))


def test_critical_path_reads_is_critical_path(db):  # noqa: F811
    db.tasks.docs += [
        {"id": "c1", "tenant_id": A, "project_id": P1, "activity_code": "C1",
         "status": "in_progress", "is_critical_path": True},
        {"id": "c2", "tenant_id": A, "project_id": P1, "activity_code": "C2",
         "status": "in_progress", "is_critical_path": False},
    ]
    ctx = {**_ctx("admin1"), "project_id": P1}
    out = run(ct._tool_get_schedule_analysis(ctx, {"view": "critical_path"}))
    codes = [r["activity_code"] for r in out.get("rows", [])]
    assert "C1" in codes and "C2" not in codes


def test_router_is_told_when_a_report_is_open():
    from services import model_router as mr
    assert "open for editing" in mr._prompt("same manpower as the 13th", True)
    assert "open for editing" not in mr._prompt("same manpower as the 13th")


def test_user_block_states_the_role_limit(db):  # noqa: F811
    ctx = {"tenant_id": A, "project_id": P1, "_access": _ctx("admin1")}
    text = run(ct._user_block(ctx, "viewer"))
    assert "can only read" in text and "Never offer" in text


# ------------------------------------------------------------ round 2 (2026-11-17)
def test_delay_on_open_report_takes_report_date_and_no_weather_default(db):  # noqa: F811
    db.daily_reports.docs.append({"id": "drX", "tenant_id": A, "project_id": P1,
                                  "report_date": "2026-11-14", "status": "draft"})
    ctx = {**_ctx("super1"), "project_id": P1, "daily_report_id": "drX"}
    out = run(ct._tool_create_delay(ctx, {"description": "drywall delivery did not show"}))
    assert "created" in out
    d = db.delays.docs[-1]
    assert d["date"] == "2026-11-14" and d["reason_category"] == "other"
    assert d["daily_report_id"] == "drX"


def test_create_crew_saves_company_and_sub_crew_note(db):  # noqa: F811
    ctx = {**_ctx("admin1"), "project_id": P1}
    out = run(ct._tool_create_crew(ctx, {"name": "Abatement Crew", "trade": "abatement",
                                         "target_size": 6, "is_self_perform": False,
                                         "external_company": "GKM"}))
    c = db.crews.docs[-1]
    assert c["company_name"] == "GKM" and "not self-perform" in c["notes"]
    assert out["created"]["company_name"] == "GKM" and out["created"]["target_size"] == 6


def test_spec_search_says_when_no_specs_are_uploaded(search_on, db, monkeypatch):  # noqa: F811
    from services import spec_search

    async def _none(**k):
        return []
    monkeypatch.setattr(spec_search, "search_all_specs_in_project", _none)
    ctx = {**_ctx("admin1"), "project_id": P1}
    out = run(ct._tool_search_specs(ctx, {"query": "firestopping"}))
    assert out.get("specs_loaded") is False and "no specifications" in out["note"]


def test_hidden_contact_details_leave_a_marker():
    from services.search.chunkers import docs_company as dc
    t = dc.scrub("Frank mobile: (610) 333-7002, mail frank@example.com")
    assert "333-7002" not in t and "example.com" not in t
    assert "[phone on file" in t and "[email on file" in t


def test_search_everything_is_always_on(monkeypatch):
    from services import feature_flags, runtime_settings as rs
    from routers import admin_settings_router as asr
    monkeypatch.delenv("FF_SEARCH_EVERYTHING", raising=False)
    assert feature_flags.search_everything_enabled() is True
    # An old saved Settings value no longer turns it off.
    monkeypatch.setattr(rs, "_cache", {"flags": {"FF_SEARCH_EVERYTHING": False}})
    assert feature_flags.search_everything_enabled() is True
    # No Settings switch any more.
    assert "FF_SEARCH_EVERYTHING" not in rs.ALLOWED_FLAGS
    assert all(m["key"] != "FF_SEARCH_EVERYTHING" for m in asr.FLAG_META)
    # Emergency off by server variable only.
    monkeypatch.setenv("FF_SEARCH_EVERYTHING", "0")
    assert feature_flags.search_everything_enabled() is False
