"""Day-1 round 2 (2026-10-25) -- backend-lock group.

1. False "Schedule changed" 409. The daily-report activity PATCH used
   tasks.updated_at as its optimistic-lock token, but schedule recomputes
   restamp updated_at on EVERY task, so after a super saved activity A the
   next save on a different activity B answered 409 and the edit was lost.
   The lock is now the human-edit stamp tasks.progress_lock_at:
     * multi-activity: save A, a recompute bumps updated_at on all tasks,
       save B with B's ORIGINAL grid token -> 200;
     * a genuine conflict (another person saved the same activity after this
       client loaded it) still 409s -- both directions, through the grid and
       through the Tasks page / a schedule re-import;
     * legacy clients holding an old updated_at-based token never get a
       false 409, and are still refused after a real human edit.
2. Weather field clearing. PATCH /daily-reports/{id} honors an explicit null
   for the five weather fields (only when the client sent the key); every
   other field keeps "null = do not change"; locked reports stay locked.

Handlers are called directly against a small in-memory fake of the motor
collections (no live MongoDB / server needed).

Run:  cd backend && python -m pytest tests/test_day1_round2_backend_lock.py -q
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from routers import daily_reports_router as drr
from routers import tasks_router as tr
from routers import schedule_proposals_router as spr
from services.dr_activity_grid import enrich_grid_row
from models import (
    DailyReportUpdate, DailyReportActivityUpdate, TaskUpdate,
    ScheduleProposalDecision,
)


# ------------------------------------------------------------------
# Minimal in-memory stand-in for motor collections
# ------------------------------------------------------------------
def _get_path(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _matches(doc, flt):
    for k, cond in (flt or {}).items():
        val = _get_path(doc, k)
        if isinstance(cond, dict) and any(op.startswith("$") for op in cond):
            for op, arg in cond.items():
                if op == "$nin" and val in arg:
                    return False
                if op == "$in" and val not in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
        elif val != cond:
            return False
    return True


def _project(doc, projection):
    out = copy.deepcopy(doc)
    if projection:
        keep = [k for k, v in projection.items() if v and k != "_id"]
        if keep:
            out = {k: out[k] for k in keep if k in out}
    return out


class _Res:
    def __init__(self, matched=0):
        self.matched_count = matched
        self.modified_count = matched
        self.deleted_count = matched


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    async def to_list(self, *a, **kw):
        return self._docs


class FakeColl:
    def __init__(self, docs=None):
        self.docs = [copy.deepcopy(d) for d in (docs or [])]

    async def find_one(self, flt=None, projection=None):
        for d in self.docs:
            if _matches(d, flt):
                return _project(d, projection)
        return None

    def find(self, flt=None, projection=None):
        return _Cursor([_project(d, projection) for d in self.docs if _matches(d, flt)])

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        return _Res(1)

    def _apply(self, d, update):
        for k, v in (update.get("$set") or {}).items():
            parts = k.split(".")
            cur = d
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = copy.deepcopy(v)
        for k in (update.get("$unset") or {}):
            d.pop(k, None)
        for k, v in (update.get("$push") or {}).items():
            d.setdefault(k, []).append(copy.deepcopy(v))

    async def update_one(self, flt, update):
        for d in self.docs:
            if _matches(d, flt):
                self._apply(d, update)
                return _Res(1)
        return _Res(0)

    async def update_many(self, flt, update):
        n = 0
        for d in self.docs:
            if _matches(d, flt):
                self._apply(d, update)
                n += 1
        return _Res(n)


class FakeDB:
    def __init__(self, **colls):
        self._colls = {k: FakeColl(v) for k, v in colls.items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeColl())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


class _BG:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **kw):
        self.tasks.append(fn)


TID = "t1"
SUPER_A = {"tenant_id": TID, "user": {"id": "u-super-a", "role": "superintendent"}, "request_id": "req-a"}
SUPER_B = {"tenant_id": TID, "user": {"id": "u-super-b", "role": "superintendent"}, "request_id": "req-b"}
PM = {"tenant_id": TID, "user": {"id": "u-pm", "role": "pm"}, "request_id": "req-pm"}

T0 = "2020-01-01T12:00:00.000000+00:00"          # old updated_at
L0 = "2020-01-02T09:00:00.000000+00:00"          # an earlier human edit


def _run(coro):
    return asyncio.run(coro)


def _task(tid, code, **extra):
    d = {"id": tid, "tenant_id": TID, "project_id": "p1", "activity_code": code,
         "description": f"Activity {code}", "pct_complete": 10.0, "status": "in_progress",
         "created_at": "2020-01-01T00:00:00+00:00", "updated_at": T0}
    d.update(extra)
    return d


def _row(rid, task_id, pos):
    return {"id": rid, "daily_report_id": "dr1", "schedule_activity_id": task_id,
            "position": pos, "pct_complete_start_of_day": 10.0,
            "pct_complete_end_of_day": 10.0, "crew_count": 0, "hours_worked": 0,
            "status_at_end_of_day": "in_progress"}


def _dr(status="draft"):
    return {"id": "dr1", "tenant_id": TID, "project_id": "p1",
            "report_date": "2026-10-25", "status": status, "prepared_by": "u-super-a",
            "created_at": "2026-10-25T11:00:00+00:00",
            "weather_conditions": "Sunny", "weather_temp_high_f": 71.0,
            "weather_temp_low_f": 55.0, "weather_precipitation_in": 0.1,
            "weather_wind_mph": 8.0, "notes": "keep me"}


def _bump_all_updated_at(db):
    """What enterprise CPM / MS Project sync / status recompute do: restamp
    updated_at on EVERY task (naive utcnow, like the real services), and
    never touch progress_lock_at."""
    later = (datetime.utcnow() + timedelta(seconds=2)).isoformat()
    for t in db.tasks.docs:
        t["updated_at"] = later
        t["computed_status"] = "on_track"
    return later


@pytest.fixture
def env(monkeypatch, request):
    tasks_seed = getattr(request, "param", "with_lock")
    extra = {"progress_lock_at": L0} if tasks_seed == "with_lock" else {}
    db = FakeDB(
        daily_reports=[_dr("draft")],
        daily_report_activities=[_row("rowA", "taskA", 0), _row("rowB", "taskB", 1)],
        tasks=[_task("taskA", "A100", **extra), _task("taskB", "B200", **extra)],
        projects=[{"id": "p1", "tenant_id": TID, "name": "P"}],
    )
    monkeypatch.setattr(drr, "get_db", lambda: db)
    monkeypatch.setattr(tr, "get_db", lambda: db)
    monkeypatch.setattr(spr, "get_db", lambda: db)

    async def _noop_audit(**kw):
        return None
    monkeypatch.setattr(drr, "write_audit", _noop_audit)
    monkeypatch.setattr(tr, "write_audit", _noop_audit)
    monkeypatch.setattr(spr, "write_audit", _noop_audit)

    async def _policy(**kw):
        return ({"id": "dr1", "status": "draft"}, False, None)
    monkeypatch.setattr("services.edit_permissions.enforce_locked_dr_edit_policy", _policy)
    monkeypatch.setattr("services.feature_flags.slider_gating_enabled", lambda: False)
    monkeypatch.setattr("services.feature_flags.schedule_mode_2_enabled", lambda: False)
    monkeypatch.setattr("services.feature_flags.enterprise_cpm_enabled", lambda: False)

    # Every grid save triggers the status recompute AND (engine projects)
    # the schedule cascade -- both restamp updated_at on all tasks.
    async def _status_recompute(db_, tenant_id, task_id, **kw):
        _bump_all_updated_at(db)
    monkeypatch.setattr("services.task_status.compute_and_persist_status", _status_recompute)
    monkeypatch.setattr("services.feature_flags.progress_recompute_enabled_for", lambda p: True)

    async def _cascade(tenant_id, project_id, **kw):
        _bump_all_updated_at(db)
    monkeypatch.setattr("services.cpm.auto_recompute_after_event", _cascade)
    monkeypatch.setattr(tr, "auto_recompute_after_event", _cascade)

    async def _cpm(*a, **kw):
        _bump_all_updated_at(db)
    monkeypatch.setattr(spr, "compute_cpm", _cpm)
    return db


def _grid_tokens(db):
    """What /context and /grid hand the client (JSON round-trip)."""
    rows = copy.deepcopy(db.daily_report_activities.docs)
    _run(drr._enrich_grid_rows(db, TID, rows))
    out = {}
    for r in rows:
        tok = r.get("task_updated_at")
        out[r["id"]] = drr.DailyReportActivity(**r).model_dump(mode="json")["task_updated_at"] if tok else None
    return out


def _save(row_id, pct, token, ctx=SUPER_A):
    return _run(drr.update_grid_row(
        row_id,
        DailyReportActivityUpdate(pct_complete_end_of_day=pct, task_last_modified_at=token),
        ctx,
    ))


def _task_doc(db, tid):
    return next(t for t in db.tasks.docs if t["id"] == tid)


# ------------------------------------------------------------------
# 1a. Multi-activity: no false 409 after a recompute
# ------------------------------------------------------------------
@pytest.mark.parametrize("env", ["with_lock", "never_edited"], indirect=True)
def test_save_a_then_b_with_original_token_after_recompute(env):
    db = env
    tokens = _grid_tokens(db)
    assert tokens["rowA"] and tokens["rowB"]
    _save("rowA", 40, tokens["rowA"])
    # The save's status recompute + cascade restamped updated_at on B too.
    assert drr._stamp_is_newer(_task_doc(db, "taskB")["updated_at"], tokens["rowB"])
    # One more independent recompute for good measure (MS Project sync etc).
    _bump_all_updated_at(db)
    out = _save("rowB", 55, tokens["rowB"])          # must NOT 409
    assert out.pct_complete_end_of_day == 55
    assert _task_doc(db, "taskB")["pct_complete"] == 55
    assert _task_doc(db, "taskA")["pct_complete"] == 40


def test_grid_token_is_progress_lock_at_and_recompute_does_not_move_it(env):
    db = env
    before = _grid_tokens(db)
    _bump_all_updated_at(db)
    after = _grid_tokens(db)
    assert before == after, "a recompute alone must not change any grid token"
    assert _task_doc(db, "taskA")["progress_lock_at"] == L0


def test_patch_stamps_progress_lock_at_with_the_same_value_as_updated_at(env, monkeypatch):
    db = env
    # Turn the bumps off so the handler's own write is observable.
    async def _quiet(*a, **kw):
        return None
    monkeypatch.setattr("services.task_status.compute_and_persist_status", _quiet)
    monkeypatch.setattr("services.cpm.auto_recompute_after_event", _quiet)
    out = _save("rowA", 40, _grid_tokens(db)["rowA"])
    t = _task_doc(db, "taskA")
    assert t["progress_lock_at"] == t["updated_at"]
    assert t["progress_lock_at"] > L0
    assert t["progress_lock_at"] == out.last_sync_at.isoformat()
    # Task B untouched by a person -> its lock did not move.
    assert _task_doc(db, "taskB")["progress_lock_at"] == L0


def test_chained_saves_on_same_row_use_returned_stamp(env):
    db = env
    first = _save("rowA", 40, _grid_tokens(db)["rowA"])
    tok = first.model_dump(mode="json")["task_updated_at"]
    assert tok is not None
    _bump_all_updated_at(db)
    second = _save("rowA", 50, tok)
    third = _save("rowA", 60, second.model_dump(mode="json")["task_updated_at"])
    assert third.pct_complete_end_of_day == 60
    assert _task_doc(db, "taskA")["pct_complete"] == 60


def test_returned_stamp_is_never_older_than_progress_lock_at(env):
    db = env
    out = _save("rowA", 40, _grid_tokens(db)["rowA"])
    lock = _task_doc(db, "taskA")["progress_lock_at"]
    assert not drr._stamp_is_newer(lock, out.model_dump(mode="json")["task_updated_at"])


# ------------------------------------------------------------------
# 1b. Genuine conflicts still 409 -- both directions
# ------------------------------------------------------------------
@pytest.mark.parametrize("env", ["with_lock", "never_edited"], indirect=True)
def test_real_conflict_a_then_b(env):
    db = env
    tok_a = _grid_tokens(db)["rowA"]      # super A loads
    tok_b = _grid_tokens(db)["rowA"]      # super B loads the same activity
    _save("rowA", 40, tok_a, SUPER_A)
    with pytest.raises(HTTPException) as ei:
        _save("rowA", 70, tok_b, SUPER_B)
    assert ei.value.status_code == 409
    detail = ei.value.detail
    assert detail["error"] == "stale_write"
    assert set(detail) == {"error", "message", "task_id", "current_task"}
    assert detail["task_id"] == "taskA"
    assert detail["current_task"]["pct_complete"] == 40
    assert _task_doc(db, "taskA")["pct_complete"] == 40     # B's edit refused, A's kept


@pytest.mark.parametrize("env", ["with_lock", "never_edited"], indirect=True)
def test_real_conflict_b_then_a(env):
    db = env
    tok_a = _grid_tokens(db)["rowA"]
    tok_b = _grid_tokens(db)["rowA"]
    _save("rowA", 25, tok_b, SUPER_B)
    with pytest.raises(HTTPException) as ei:
        _save("rowA", 90, tok_a, SUPER_A)
    assert ei.value.status_code == 409
    assert _task_doc(db, "taskA")["pct_complete"] == 25


def test_after_409_a_fresh_grid_token_saves(env):
    db = env
    stale = _grid_tokens(db)["rowA"]
    _save("rowA", 40, stale, SUPER_B)
    with pytest.raises(HTTPException):
        _save("rowA", 70, stale, SUPER_A)
    out = _save("rowA", 70, _grid_tokens(db)["rowA"], SUPER_A)   # reload, retry
    assert out.pct_complete_end_of_day == 70


def test_tasks_page_edit_is_a_real_conflict_for_the_grid(env):
    db = env
    tok = _grid_tokens(db)["rowA"]
    _run(tr.update_task("taskA", TaskUpdate(pct_complete=65), _BG(), PM))
    t = _task_doc(db, "taskA")
    assert t["progress_lock_at"] == t["updated_at"]
    with pytest.raises(HTTPException) as ei:
        _save("rowA", 30, tok)
    assert ei.value.status_code == 409
    assert _task_doc(db, "taskA")["pct_complete"] == 65


def test_tasks_page_non_progress_edit_does_not_move_the_lock(env):
    db = env
    tok = _grid_tokens(db)["rowA"]
    _run(tr.update_task("taskA", TaskUpdate(description="Renamed"), _BG(), PM))
    assert _task_doc(db, "taskA")["progress_lock_at"] == L0
    out = _save("rowA", 30, tok)           # nothing a person changed in progress terms
    assert out.pct_complete_end_of_day == 30


def test_tasks_page_unchanged_value_does_not_move_the_lock(env):
    db = env
    _run(tr.update_task("taskA", TaskUpdate(pct_complete=10.0), _BG(), PM))
    assert _task_doc(db, "taskA")["progress_lock_at"] == L0


@pytest.mark.parametrize("field,value", [
    ("status", "complete"), ("actual_start", "2026-10-01"),
    ("planned_finish", "2026-12-01"), ("duration_days", 12.0),
])
def test_tasks_page_schedule_fields_move_the_lock(env, field, value):
    db = env
    _run(tr.update_task("taskA", TaskUpdate(**{field: value}), _BG(), PM))
    t = _task_doc(db, "taskA")
    assert t["progress_lock_at"] > L0
    assert t["progress_lock_at"] == t["updated_at"]


def test_paperwork_blocker_moves_the_lock(env, monkeypatch):
    db = env

    async def _today(*a, **kw):
        return "2026-10-25"
    monkeypatch.setattr("routers.docs_router._project_today_iso", _today)
    tok = _grid_tokens(db)["rowA"]
    _run(tr.set_paperwork_blocker("taskA", tr.PaperworkBlockerPayload(impact_days=2), PM))
    assert _task_doc(db, "taskA")["progress_lock_at"] > L0
    with pytest.raises(HTTPException) as ei:
        _save("rowA", 30, tok)
    assert ei.value.status_code == 409
    tok2 = _grid_tokens(db)["rowA"]
    _run(tr.clear_paperwork_blocker("taskA", tr.ClearPaperworkBlockerPayload(), PM))
    with pytest.raises(HTTPException):
        _save("rowA", 30, tok2)


def test_schedule_reimport_moves_the_lock_on_updated_tasks(env):
    db = env
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "s.xlsx", "source": "file_upload", "diff": {},
        "parsed_rows": [{"activity_code": "A100", "description": "Activity A100",
                         "planned_start": "2026-11-01", "planned_finish": "2026-11-05",
                         "duration_days": 5}],
    })
    tok_a = _grid_tokens(db)["rowA"]
    tok_b = _grid_tokens(db)["rowB"]
    out = _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    assert out["applied"]["updated"] == 1
    ta = _task_doc(db, "taskA")
    assert ta["progress_lock_at"] > L0
    assert ta["pct_complete"] == 10.0              # pt.76 progress kept
    # Re-imported A: a grid save made against the pre-import task 409s.
    with pytest.raises(HTTPException) as ei:
        _save("rowA", 30, tok_a)
    assert ei.value.status_code == 409
    # B was not in the import -> only recomputed, so its save goes through.
    assert _task_doc(db, "taskB")["progress_lock_at"] == L0
    assert _save("rowB", 30, tok_b).pct_complete_end_of_day == 30


def test_identical_reimport_does_not_move_the_lock(env):
    """Review fix: a full re-import rewrites every row. Rows whose
    progress/schedule values did not change must not 409 a super who has
    them open in the daily report; rows that did change still 409."""
    db = env

    def _prop(pid, finish):
        return {
            "id": pid, "tenant_id": TID, "project_id": "p1", "status": "pending_review",
            "filename": "s.xlsx", "source": "ms_project_pull", "diff": {},
            "parsed_rows": [
                {"activity_code": "A100", "description": "Activity A100",
                 "planned_start": "2026-11-01", "planned_finish": "2026-11-05",
                 "duration_days": 5},
                {"activity_code": "B200", "description": "Activity B200",
                 "planned_start": "2026-11-06", "planned_finish": finish,
                 "duration_days": 5},
            ],
        }
    db.schedule_proposals.docs.append(_prop("prop1", "2026-11-10"))
    _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    tok_a = _grid_tokens(db)["rowA"]
    tok_b = _grid_tokens(db)["rowB"]
    lock_a = _task_doc(db, "taskA")["progress_lock_at"]
    # Second pull: A identical, B's finish moved.
    db.schedule_proposals.docs.append(_prop("prop2", "2026-11-12"))
    out = _run(spr.approve_proposal("prop2", ScheduleProposalDecision(), _BG(), PM))
    assert out["applied"]["updated"] == 2
    assert _task_doc(db, "taskA")["progress_lock_at"] == lock_a
    assert _task_doc(db, "taskB")["planned_finish"] == "2026-11-12"
    assert _save("rowA", 30, tok_a).pct_complete_end_of_day == 30
    with pytest.raises(HTTPException) as ei:
        _save("rowB", 30, tok_b)
    assert ei.value.status_code == 409


def test_ms_project_pull_progress_lands_on_new_tasks_never_on_existing(env):
    """A pull carries the file's % complete. A task ICE did not have yet
    takes it; a task that already exists keeps ICE's own progress."""
    db = env
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "MS Project (live)", "source": "ms_project_pull", "diff": {},
        "parsed_rows": [
            {"activity_code": "A100", "description": "Activity A100",
             "planned_start": "2026-11-01", "planned_finish": "2026-11-05",
             "duration_days": 5, "pct_complete": 100.0,
             "actual_start": "2026-11-01", "actual_finish": "2026-11-05"},
            {"activity_code": "N900", "description": "Install ICRA/ILSM",
             "planned_start": "2026-09-29", "planned_finish": "2026-10-12",
             "duration_days": 10, "pct_complete": 75.0,
             "actual_start": "2026-09-29", "actual_finish": None},
        ],
    })
    out = _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    assert out["applied"]["created"] == 1 and out["applied"]["migrated_progress"] == 1
    a = _task_doc(db, "taskA")
    assert a["pct_complete"] == 10.0 and a["status"] != "complete"
    n = next(t for t in db.tasks.docs if t.get("activity_code") == "N900")
    assert n["pct_complete"] == 75.0 and n["status"] == "in_progress"
    assert n["actual_start"] == "2026-09-29" and n["actual_finish"] is None


def test_file_upload_progress_still_updates_existing_tasks(env):
    """The ICE-owns-progress rule is for MS Project pulls only: an imported
    spreadsheet that carries % complete still updates an existing task."""
    db = env
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "s.xlsx", "source": "file_upload", "diff": {},
        "parsed_rows": [{"activity_code": "A100", "description": "Activity A100",
                         "planned_start": "2026-11-01", "planned_finish": "2026-11-05",
                         "duration_days": 5, "pct_complete": 60.0,
                         "actual_start": "2026-11-01"}],
    })
    out = _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    assert out["applied"]["migrated_progress"] == 1
    a = _task_doc(db, "taskA")
    assert a["pct_complete"] == 60.0 and a["status"] == "in_progress"


def test_later_pull_keeps_labels_and_trade_already_on_tasks(env):
    db = env
    ta = next(t for t in db.tasks.docs if t["id"] == "taskA")
    ta.update(work_category="construction_paperwork", trade_code="MECH")
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "MS Project (live)", "source": "ms_project_pull", "diff": {},
        "parsed_rows": [{"activity_code": "A100", "description": "Activity A100",
                         "planned_start": "2026-11-02", "planned_finish": "2026-11-06",
                         "duration_days": 5}],
    })
    _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    a = _task_doc(db, "taskA")
    assert a["planned_start"] == "2026-11-02"
    assert a["work_category"] == "construction_paperwork" and a["trade_code"] == "MECH"


def test_approve_waits_for_labelling_but_not_forever(env):
    from datetime import datetime, timedelta, timezone
    db = env
    now = datetime.now(timezone.utc)
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "MS Project (live)", "source": "ms_project_pull", "diff": {},
        "work_category_tagging": {"status": "running", "started_at": now.isoformat()},
        "parsed_rows": [{"activity_code": "N902", "description": "New",
                         "planned_start": "2026-11-02", "planned_finish": "2026-11-06",
                         "duration_days": 5}],
    })
    with pytest.raises(HTTPException) as ei:
        _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    assert ei.value.status_code == 409 and "field work" in ei.value.detail
    db.schedule_proposals.docs[-1]["work_category_tagging"]["started_at"] = \
        (now - timedelta(minutes=6)).isoformat()
    out = _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    assert out["applied"]["created"] == 1


def test_edits_wait_for_labelling_too(env):
    from datetime import datetime, timezone
    db = env
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "MS Project (live)", "source": "ms_project_pull", "diff": {},
        "work_category_tagging": {"status": "running",
                                  "started_at": datetime.now(timezone.utc).isoformat()},
        "parsed_rows": [{"activity_code": "N903", "description": "New",
                         "planned_start": "2026-11-02", "planned_finish": "2026-11-06",
                         "duration_days": 5}],
    })
    with pytest.raises(HTTPException) as ei:
        _run(spr.set_work_category(
            "prop1", spr.WorkCategoryOverride(activity_code="N903", work_category="field_work"), PM))
    assert ei.value.status_code == 409


def test_pull_never_blanks_fields_ms_project_does_not_send(env):
    db = env
    ta = next(t for t in db.tasks.docs if t["id"] == "taskA")
    ta.update(constraint_type="snet", constraint_date="2026-11-01", calendar_id="cal-6day",
              required_trade="Electrician")
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "MS Project (live)", "source": "ms_project_pull", "diff": {},
        "parsed_rows": [{"activity_code": "A100", "description": "Activity A100",
                         "planned_start": "2026-11-03", "planned_finish": "2026-11-07",
                         "duration_days": 5}],
    })
    _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    a = _task_doc(db, "taskA")
    assert a["planned_start"] == "2026-11-03"
    assert (a["constraint_type"], a["calendar_id"], a["required_trade"]) == ("snet", "cal-6day", "Electrician")


def test_actual_start_at_zero_percent_imports_as_in_progress(env):
    db = env
    db.schedule_proposals.docs.append({
        "id": "prop1", "tenant_id": TID, "project_id": "p1", "status": "pending_review",
        "filename": "MS Project (live)", "source": "ms_project_pull", "diff": {},
        "parsed_rows": [{"activity_code": "N901", "description": "Started yesterday",
                         "planned_start": "2026-10-24", "planned_finish": "2026-10-30",
                         "duration_days": 5, "pct_complete": 0.0,
                         "actual_start": "2026-10-24", "actual_finish": None}],
    })
    _run(spr.approve_proposal("prop1", ScheduleProposalDecision(), _BG(), PM))
    n = next(t for t in db.tasks.docs if t.get("activity_code") == "N901")
    assert n["status"] == "in_progress" and n["actual_start"] == "2026-10-24"


# ------------------------------------------------------------------
# 1c. Legacy tokens (clients loaded before this deploy)
# ------------------------------------------------------------------
def test_legacy_updated_at_token_never_false_409s(env):
    db = env
    # Old server handed out updated_at; recomputes made it far newer than
    # the last human edit (L0).
    later = _bump_all_updated_at(db)
    legacy = later                       # naive form, as the old grid served it
    out = _save("rowB", 33, legacy)
    assert out.pct_complete_end_of_day == 33
    legacy_z = datetime.fromisoformat(T0).isoformat().replace("+00:00", "Z")
    # A legacy token equal to the old updated_at T0 (< L0) on a task whose
    # lock is L0: L0 was written together with an updated_at >= L0, so a
    # token of T0 can only have been issued BEFORE that human edit -> 409.
    with pytest.raises(HTTPException):
        _save("rowA", 44, legacy_z)


def test_legacy_token_is_still_refused_after_a_real_human_edit(env):
    db = env
    # A recompute (after L0, before now) restamped updated_at; an old client
    # loaded the grid then and holds that updated_at as its token.
    legacy = "2020-01-05T00:00:00"
    for t in db.tasks.docs:
        t["updated_at"] = legacy
    _save("rowA", 40, _grid_tokens(db)["rowA"], SUPER_B)   # someone edits after
    with pytest.raises(HTTPException) as ei:
        _save("rowA", 80, legacy, SUPER_A)
    assert ei.value.status_code == 409


@pytest.mark.parametrize("env", ["never_edited"], indirect=True)
def test_task_without_progress_lock_skips_the_check(env):
    db = env
    _bump_all_updated_at(db)
    out = _save("rowA", 20, T0)          # very old legacy token, nothing to conflict with
    assert out.pct_complete_end_of_day == 20


def test_no_token_still_saves(env):
    out = _save("rowA", 20, None)
    assert out.pct_complete_end_of_day == 20


@pytest.mark.parametrize("server,client,newer", [
    ("2026-10-25T12:00:00.500000+00:00", "2026-10-25T12:00:00Z", True),
    ("2026-10-25T12:00:00+00:00", "2026-10-25T12:00:00Z", False),
    ("2026-10-25T12:00:00+00:00", "2026-10-25T12:00:00", False),
    ("2026-10-25T12:00:01", "2026-10-25T12:00:00+00:00", True),
    ("2026-10-25T08:00:00-04:00", "2026-10-25T12:00:00Z", False),
    (datetime(2026, 10, 25, 12, 0, 1, tzinfo=timezone.utc), "2026-10-25T12:00:00Z", True),
])
def test_stamp_compare_is_by_instant(server, client, newer):
    assert drr._stamp_is_newer(server, client) is newer


def test_enrich_grid_row_prefers_progress_lock_at():
    row = enrich_grid_row({"id": "r"}, {"updated_at": "2026-10-25T12:00:00",
                                        "progress_lock_at": "2026-10-20T09:00:00+00:00"})
    assert row["task_updated_at"] == "2026-10-20T09:00:00+00:00"
    row = enrich_grid_row({"id": "r"}, {"updated_at": "2026-10-25T12:00:00"})
    assert row["task_updated_at"] == "2026-10-25T12:00:00"
    assert enrich_grid_row({"id": "r"}, None)["task_updated_at"] is None


# ------------------------------------------------------------------
# 2. Weather field clearing
# ------------------------------------------------------------------
def _patch(db, body):
    return _run(drr.patch_daily_report("dr1", DailyReportUpdate.model_validate(body), SUPER_A))


def test_weather_fields_can_be_cleared_with_explicit_null(env):
    db = env
    out = _patch(db, {"weather_conditions": None, "weather_temp_high_f": None,
                      "weather_temp_low_f": None, "weather_precipitation_in": None,
                      "weather_wind_mph": None})
    stored = db.daily_reports.docs[0]
    for k in drr._CLEARABLE_WEATHER_FIELDS:
        assert stored[k] is None, k
        assert getattr(out, k) is None
    assert stored["notes"] == "keep me"


def test_weather_clear_one_field_leaves_omitted_fields_alone(env):
    db = env
    _patch(db, {"weather_wind_mph": None, "weather_temp_high_f": 75})
    stored = db.daily_reports.docs[0]
    assert stored["weather_wind_mph"] is None
    assert stored["weather_temp_high_f"] == 75
    assert stored["weather_conditions"] == "Sunny"
    assert stored["weather_temp_low_f"] == 55.0


def test_non_weather_null_still_means_do_not_change(env):
    db = env
    _patch(db, {"notes": None, "weather_conditions": "Rain"})
    stored = db.daily_reports.docs[0]
    assert stored["notes"] == "keep me"
    assert stored["weather_conditions"] == "Rain"


def test_empty_patch_changes_nothing(env):
    db = env
    snap = copy.deepcopy(db.daily_reports.docs[0])
    _patch(db, {})
    assert db.daily_reports.docs[0] == snap


@pytest.mark.parametrize("status", ["submitted", "approved"])
def test_weather_clear_blocked_on_locked_report(env, status):
    db = env
    db.daily_reports.docs[0]["status"] = status
    with pytest.raises(HTTPException) as ei:
        _patch(db, {"weather_conditions": None})
    assert ei.value.status_code == 409
    assert db.daily_reports.docs[0]["weather_conditions"] == "Sunny"
