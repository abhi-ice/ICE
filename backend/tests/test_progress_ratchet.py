"""Progress lock: a submitted report's progress can't be undone by another
report (2026-11-16).

The owner's rule: once a progress is shown on a submitted (or approved)
daily report it is locked; later reports can go further but not back, and
going back is only possible by editing the report that recorded it. If it
does go back, the schedule follows what the reports say.

Checked here, through the real daily report handlers against the local test
MongoDB (DB_NAME must be a throwaway test database; each test uses its own
company id and removes only its own documents):
  * floor: a later report can't go below an earlier submitted one;
  * ceiling: a back-dated report can't go above a later submitted one
    (also when "complete" is picked as the end-of-day status);
  * drafts never lock anything;
  * going back = editing the report that recorded the higher value, itself
    checked against its own neighbours;
  * walking a task back to 0 across several days -> not started, actual
    start / finish cleared, schedule follows;
  * submit is refused when a draft row fell out of range;
  * new rows (add and grid pre-fill) start at the floor; the grid read
    exposes the limits;
  * the AI assistant's mark_activity_status tool is refused the same way;
  * the 422 body carries code "progress_out_of_range" and the limits.

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_ratchet bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_progress_ratchet.py -q
"""
import asyncio
import importlib.util
import json
import os
import sys
import uuid

import pytest

if not os.environ.get("MONGO_URL") or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")):
    pytest.skip("needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)", allow_module_level=True)

from models import DailyReportActivityUpdate  # noqa: E402
from routers import daily_reports_router as drr  # noqa: E402
from services.progress_ratchet import (  # noqa: E402
    ProgressOutOfRangeError, progress_out_of_range_handler, limits_from_history,
)

COLLS = ["tasks", "projects", "daily_reports", "daily_report_activities",
         "task_sync_events", "audit_log", "productivity_observations",
         "project_access_watch", "delays", "daily_presence", "schedule_proposals",
         "edit_requests"]
REASON = "Correcting the progress entry"

# Several sibling test modules swap sys.modules["database"] for an in-memory
# stub when they are collected, and modules first imported while that stub
# was installed keep its get_db. Load the real database module straight from
# its file (only when the installed one is not it) and, for each test, point
# sys.modules and every loaded service/router module at it.
def _load_real_database():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database.py")
    mod = sys.modules.get("database")
    if mod is not None and os.path.abspath(getattr(mod, "__file__", "") or "") == path:
        return mod
    spec = importlib.util.spec_from_file_location("database", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


database = _load_real_database()


@pytest.fixture(autouse=True)
def _use_real_database(monkeypatch):
    from services import copilot_tools  # noqa: F401  (loaded before patching)
    from routers import dr_voice_router  # noqa: F401
    monkeypatch.setitem(sys.modules, "database", database)
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name.startswith(("services.", "routers.")) or name == "server"):
            continue
        fn = getattr(mod, "get_db", None)
        if callable(fn) and fn is not database.get_db:
            monkeypatch.setattr(mod, "get_db", database.get_db)
    yield


def _run(coro_fn):
    """Fresh motor client per test (each test has its own event loop)."""
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-ratchet-" + uuid.uuid4().hex[:8]
        db = database.get_db()
        try:
            return await coro_fn(db, tenant)
        finally:
            rows = await db.daily_reports.find({"tenant_id": tenant}, {"id": 1}).to_list(None)
            await db.daily_report_activities.delete_many(
                {"daily_report_id": {"$in": [r["id"] for r in rows]}})
            for c in COLLS:
                if c != "daily_report_activities":
                    await db[c].delete_many({"tenant_id": tenant})
            database._client.close()
            database._client, database._db = None, None
    return asyncio.run(wrapper())


def _ctx(tenant):
    return {"tenant_id": tenant, "user": {"id": "u-admin", "role": "admin"},
            "request_id": "req-" + uuid.uuid4().hex[:6]}


async def _setup(db, tenant, pct=0.0):
    pid = "p-" + uuid.uuid4().hex[:8]
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Ratchet test",
                                  "schedule_engine": "impact_days", "schedule_mode": "mode1"})
    tid = "task-" + uuid.uuid4().hex[:8]
    await db.tasks.insert_one({
        "id": tid, "tenant_id": tenant, "project_id": pid, "activity_code": "A100",
        "description": "Framing", "planned_start": "2026-11-02", "planned_finish": "2026-11-30",
        "pct_complete": pct, "status": "in_progress" if pct else "not_started",
    })
    return pid, tid


async def _report(db, tenant, pid, day, status="draft"):
    rid = "dr-" + uuid.uuid4().hex[:8]
    await db.daily_reports.insert_one({"id": rid, "tenant_id": tenant, "project_id": pid,
                                       "report_date": day, "status": status,
                                       "prepared_by": "u-admin",
                                       "created_at": "2026-11-16T00:00:00+00:00"})
    return rid


async def _lock(db, rid, status="submitted"):
    await db.daily_reports.update_one({"id": rid}, {"$set": {
        "status": status, "submitted_by": "u-admin",
        "submitted_at": "2026-11-16T12:00:00+00:00"}})


async def _add(ctx, dr_id, tid):
    return await drr.add_grid_row(dr_id, drr.AddActivityRow(schedule_activity_id=tid), ctx)


async def _save(ctx, row_id, **fields):
    return await drr.update_grid_row(row_id, DailyReportActivityUpdate(**fields), ctx)


async def _refused(coro):
    with pytest.raises(ProgressOutOfRangeError) as ei:
        await coro
    return ei.value


async def _task(db, tenant, tid):
    return await db.tasks.find_one({"tenant_id": tenant, "id": tid}, {"_id": 0})


async def _day(db, tenant, pid, tid, ctx, day, pct, status="submitted"):
    """A report for `day` with the task at `pct`, then locked."""
    rid = await _report(db, tenant, pid, day)
    row = await _add(ctx, rid, tid)
    await _save(ctx, row.id, pct_complete_end_of_day=pct)
    if status != "draft":
        await _lock(db, rid, status)
    return rid, row.id


# ------------------------------------------------------------------ floor
def test_floor_later_report_cannot_go_below_submitted():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        err = await _refused(_save(ctx, r17.id, pct_complete_end_of_day=30))
        assert err.status_code == 422
        assert err.detail == ("Progress can't go below 45% — the 11/16 report already "
                              "recorded 45%. To lower it, edit the 11/16 report.")
        assert err.extra["floor"] == 45 and err.extra["floor_from"] == "2026-11-16"
        assert err.extra["ceiling"] == 100 and err.extra["ceiling_from"] is None
        # Nothing was written.
        row = await db.daily_report_activities.find_one({"id": r17.id})
        assert row["pct_complete_end_of_day"] == 45 and not row.get("pct_set_at")
        assert (await _task(db, tenant, tid))["pct_complete"] == 45
        # Same or further is fine.
        await _save(ctx, r17.id, pct_complete_end_of_day=45)
        await _save(ctx, r17.id, pct_complete_end_of_day=60)
        assert (await _task(db, tenant, tid))["pct_complete"] == 60
    _run(body)


def test_approved_report_locks_too():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        await _day(db, tenant, pid, tid, ctx, "2026-11-16", 50, status="approved")
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        await _refused(_save(ctx, r17.id, pct_complete_end_of_day=10))
    _run(body)


# ------------------------------------------------------------------ ceiling
def test_ceiling_backdated_report_cannot_claim_more_than_later_submitted():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        await _day(db, tenant, pid, tid, ctx, "2026-11-18", 30)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        err = await _refused(_save(ctx, r16.id, pct_complete_end_of_day=40))
        assert err.detail == ("Progress can't go above 30% — the 11/18 report recorded "
                              "30%. To raise it here, edit the 11/18 report first.")
        assert err.extra["ceiling"] == 30 and err.extra["ceiling_from"] == "2026-11-18"
        # "Complete" as the end-of-day status means 100%: refused too.
        await _refused(_save(ctx, r16.id, status_at_end_of_day="complete"))
        res = await _save(ctx, r16.id, pct_complete_end_of_day=25)
        assert res.task_progress_kept_from == "2026-11-18"
        assert (await _task(db, tenant, tid))["pct_complete"] == 30
    _run(body)


# ------------------------------------------------------------------ drafts
def test_drafts_never_lock():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45, status="draft")
        await _day(db, tenant, pid, tid, ctx, "2026-11-20", 20, status="draft")
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        await _save(ctx, r17.id, pct_complete_end_of_day=10)
        await _save(ctx, r17.id, pct_complete_end_of_day=90)
    _run(body)


# ------------------------------------------------------------------ going back
def test_edit_locked_report_to_go_back():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        d17, r17 = await _day(db, tenant, pid, tid, ctx, "2026-11-17", 60)
        assert (await _task(db, tenant, tid))["pct_complete"] == 60
        # The 11/17 report can't go below the 11/16 one ...
        err = await _refused(_save(ctx, r17, pct_complete_end_of_day=40, edit_reason=REASON))
        assert err.extra["floor_from"] == "2026-11-16"
        # ... and the 11/16 report can't go above the 11/17 one.
        err = await _refused(_save(ctx, r16, pct_complete_end_of_day=70, edit_reason=REASON))
        assert err.extra["ceiling_from"] == "2026-11-17"
        # Lower the 11/16 report first (it keeps the task at 60: a later
        # report sets current progress), then the 11/17 one.
        res = await _save(ctx, r16, pct_complete_end_of_day=20, edit_reason=REASON)
        assert res.task_progress_kept_from == "2026-11-17"
        assert (await _task(db, tenant, tid))["pct_complete"] == 60
        await _save(ctx, r17, pct_complete_end_of_day=30, edit_reason=REASON)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 30
        assert t["status"] == "in_progress"
        # A new report now starts from 30, not 45 or 60.
        d18 = await _report(db, tenant, pid, "2026-11-18")
        r18 = await _add(ctx, d18, tid)
        assert r18.pct_complete_start_of_day == 30
        assert r18.progress_floor == 30 and r18.progress_floor_from == "2026-11-17"
    _run(body)


def test_walk_back_to_zero_makes_task_not_started():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 20)
        d17, r17 = await _day(db, tenant, pid, tid, ctx, "2026-11-17", 100)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 100 and t["status"] == "complete"
        assert t["actual_start"] == "2026-11-16" and t["actual_finish"] == "2026-11-17"

        # Straight to 0 on the latest report: refused (11/16 says 20%).
        err = await _refused(_save(ctx, r17, pct_complete_end_of_day=0, edit_reason=REASON))
        assert "11/16" in err.detail
        # Edit the 11/16 report down first, then the 11/17 one.
        await _save(ctx, r16, pct_complete_end_of_day=0, edit_reason=REASON)
        await _save(ctx, r17, pct_complete_end_of_day=0, edit_reason=REASON)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 0
        assert t["status"] == "not_started"
        assert t.get("actual_start") is None and t.get("actual_finish") is None
        assert t["last_synced_from_dr"]["daily_report_id"] == d17
    _run(body)


def test_walk_back_keeps_in_progress_when_hours_were_logged():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 20)
        await db.daily_reports.update_one({"id": d16}, {"$set": {"status": "draft"}})
        await _save(ctx, r16, hours_worked=6)
        await _lock(db, d16)
        await _save(ctx, r16, pct_complete_end_of_day=0, edit_reason=REASON)
        t = await _task(db, tenant, tid)
        # Work was shown (hours), so it has started even at 0%.
        assert t["pct_complete"] == 0
        assert t["actual_start"] == "2026-11-16"
        assert t["status"] == "in_progress"
    _run(body)


def test_deleting_submitted_report_lets_schedule_follow():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, _r = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        await drr.delete_daily_report(d16, REASON, ctx)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 0
        assert t["status"] == "not_started"
        assert t.get("actual_start") is None
        # Nothing limits a new report any more.
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        assert r17.progress_floor == 0 and r17.progress_floor_from is None
        await _save(ctx, r17.id, pct_complete_end_of_day=5)
    _run(body)


# ------------------------------------------------------------------ submit
def test_submit_refused_when_row_fell_out_of_range():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        # The 11/17 draft is saved at 30 first ...
        d17, r17 = await _day(db, tenant, pid, tid, ctx, "2026-11-17", 30, status="draft")
        # ... then the 11/16 report is filled in at 45 and submitted.
        d16, _r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45, status="draft")
        await drr.submit_daily_report(d16, drr.SubmitPayload(), ctx)
        assert (await db.daily_reports.find_one({"id": d16}))["status"] == "submitted"

        err = await _refused(drr.submit_daily_report(d17, drr.SubmitPayload(), ctx))
        assert err.extra["floor"] == 45 and err.extra["floor_from"] == "2026-11-16"
        assert len(err.extra["rows"]) == 1
        assert err.extra["rows"][0]["row_id"] == r17
        assert "A100" in err.detail and "can't go below 45%" in err.detail
        assert (await db.daily_reports.find_one({"id": d17}))["status"] == "draft"

        # Fix the row, then submit works.
        await _save(ctx, r17, pct_complete_end_of_day=50)
        await drr.submit_daily_report(d17, drr.SubmitPayload(), ctx)
        assert (await db.daily_reports.find_one({"id": d17}))["status"] == "submitted"
    _run(body)


# ------------------------------------------------------------------ pre-fill + grid
def test_new_rows_start_at_floor_and_grid_shows_limits():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        await _day(db, tenant, pid, tid, ctx, "2026-11-20", 70)
        # Someone lowered the task on the schedule page meanwhile.
        await db.tasks.update_one({"id": tid}, {"$set": {"pct_complete": 10}})

        d21 = await _report(db, tenant, pid, "2026-11-21")
        r21 = await _add(ctx, d21, tid)
        assert r21.pct_complete_start_of_day == 70
        assert r21.pct_complete_end_of_day == 70

        # Grid pre-fill on a report between the two submitted ones.
        d18 = await _report(db, tenant, pid, "2026-11-18")
        assert (await drr.refresh_grid(d18, ctx))["inserted"] == 1
        row = await db.daily_report_activities.find_one({"daily_report_id": d18}, {"_id": 0})
        assert row["pct_complete_start_of_day"] == 45
        assert row["pct_complete_end_of_day"] == 45

        grid = await drr.get_grid(d18, ctx)
        g = grid[0]
        assert (g.progress_floor, g.progress_floor_from) == (45, "2026-11-16")
        assert (g.progress_ceiling, g.progress_ceiling_from) == (70, "2026-11-20")
        ctx_out = await drr.daily_report_context(d18, ctx)
        assert ctx_out["grid"][0]["progress_floor"] == 45
        assert ctx_out["grid"][0]["progress_ceiling_from"] == "2026-11-20"
    _run(body)


# ------------------------------------------------------------------ AI assistant
def test_assistant_tool_refused_out_of_range():
    async def body(db, tenant):
        from services.copilot_tools import _tool_mark_activity_status
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        tctx = {**ctx, "daily_report_id": d17}
        out = await _tool_mark_activity_status(tctx, {"activity_code": "A100",
                                                      "pct_complete_end_of_day": 20})
        assert out["code"] == "progress_out_of_range"
        assert out["floor"] == 45 and "11/16" in out["error"]
        row = await db.daily_report_activities.find_one({"id": r17.id})
        assert row["pct_complete_end_of_day"] == 45
        out = await _tool_mark_activity_status(tctx, {"activity_code": "A100",
                                                      "pct_complete_end_of_day": 55})
        assert "updated" in out, out
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 55
        assert t["last_synced_from_dr"]["daily_report_id"] == d17
    _run(body)


# ------------------------------------------------------------------ response body
def test_error_body_shape():
    err = ProgressOutOfRangeError("Progress can't go below 45%.",
                                  {"floor": 45.0, "floor_from": "2026-11-16",
                                   "ceiling": 100.0, "ceiling_from": None})
    resp = asyncio.run(progress_out_of_range_handler(None, err))
    assert resp.status_code == 422
    assert json.loads(resp.body) == {
        "detail": "Progress can't go below 45%.", "code": "progress_out_of_range",
        "floor": 45.0, "floor_from": "2026-11-16", "ceiling": 100.0, "ceiling_from": None}


def test_old_data_that_went_backwards_keeps_floor_only():
    from datetime import date
    hist = [
        {"_report_date": date(2026, 11, 10), "_report_status": "submitted", "_report_id": "a",
         "pct_complete_end_of_day": 60, "pct_set_at": "x"},
        {"_report_date": date(2026, 11, 14), "_report_status": "submitted", "_report_id": "b",
         "pct_complete_end_of_day": 30, "pct_set_at": "x"},
    ]
    lim = limits_from_history(hist, "2026-11-12")
    assert (lim["floor"], lim["floor_from"]) == (60, "2026-11-10")
    assert (lim["ceiling"], lim["ceiling_from"]) == (100, None)


# ------------------------------------------------------------------ AI assistant follows the grid
def _record_recomputes(monkeypatch):
    """Count forecast / following-task recomputes and computed-status refreshes."""
    calls = {"recompute": 0, "status": 0}
    import services.cpm as cpm
    import services.task_status as ts
    real_status = ts.compute_and_persist_status

    async def fake_recompute(*a, **k):
        calls["recompute"] += 1

    async def counted_status(*a, **k):
        calls["status"] += 1
        return await real_status(*a, **k)
    monkeypatch.setattr(cpm, "auto_recompute_after_event", fake_recompute)
    monkeypatch.setattr(ts, "compute_and_persist_status", counted_status)
    return calls


def test_assistant_tool_reruns_status_and_forecast(monkeypatch):
    calls = _record_recomputes(monkeypatch)

    async def body(db, tenant):
        from services.copilot_tools import _tool_mark_activity_status
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        await _add(ctx, d16, tid)
        tctx = {**ctx, "daily_report_id": d16}
        before = dict(calls)
        out = await _tool_mark_activity_status(tctx, {"activity_code": "A100",
                                                      "pct_complete_end_of_day": 45})
        assert "updated" in out, out
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 45 and t["status"] == "in_progress"
        assert t["actual_start"] == "2026-11-16"
        assert calls["recompute"] > before["recompute"], "forecast / following tasks not re-run"
        assert calls["status"] > before["status"], "computed status not refreshed"

        # Back to 0 through the assistant: not started, actual start cleared,
        # and the forecast re-runs again.
        before = dict(calls)
        out = await _tool_mark_activity_status(tctx, {"activity_code": "A100",
                                                      "pct_complete_end_of_day": 0})
        assert "updated" in out, out
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 0 and t["status"] == "not_started"
        assert t.get("actual_start") is None
        assert calls["recompute"] > before["recompute"]
    _run(body)


def test_assistant_backdated_edit_leaves_task_alone():
    async def body(db, tenant):
        from services.copilot_tools import _tool_mark_activity_status
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 20, status="draft")
        await _day(db, tenant, pid, tid, ctx, "2026-11-17", 40)
        # The schedule was changed after the 11/17 report (import, sync or
        # the Tasks page).
        await db.tasks.update_one({"id": tid}, {"$set": {"pct_complete": 60}})
        out = await _tool_mark_activity_status({**ctx, "daily_report_id": d16},
                                               {"activity_code": "A100",
                                                "pct_complete_end_of_day": 30})
        assert "updated" in out, out
        row = await db.daily_report_activities.find_one({"id": r16})
        assert row["pct_complete_end_of_day"] == 30
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 60, "a back-dated assistant edit overwrote the schedule"
        assert t["last_synced_from_dr"]["daily_report_date"] == "2026-11-17"
    _run(body)


# ------------------------------------------------------------------ start-of-day edits
def test_start_of_day_edit_cannot_make_stale_prefill_lock():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant, pct=50)
        # 11/16: pre-filled 50/50, nobody entered it -> does not lock.
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _lock(db, d16)
        # 11/18 recorded 30.
        await _day(db, tenant, pid, tid, ctx, "2026-11-18", 30)
        # Editing only the 11/16 start-of-day would make its stale 50 count
        # as entered, above the later 30: refused.
        err = await _refused(_save(ctx, r16.id, pct_complete_start_of_day=40,
                                   edit_reason=REASON))
        assert "11/18" in err.detail
        row = await db.daily_report_activities.find_one({"id": r16.id})
        assert row["pct_complete_start_of_day"] == 50
    _run(body)


def test_start_of_day_edit_cannot_quietly_unlock():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 50)
        # A row saved before the percent stamp existed: it counts because its
        # end-of-day differs from its start-of-day.
        await db.daily_report_activities.update_one({"id": r16}, {"$unset": {"pct_set_at": ""}})
        await _save(ctx, r16, pct_complete_start_of_day=50, edit_reason=REASON)
        row = await db.daily_report_activities.find_one({"id": r16})
        assert row.get("pct_set_at"), "row stopped counting after a start-of-day edit"
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        await _refused(_save(ctx, r17.id, pct_complete_end_of_day=10))
    _run(body)


# ------------------------------------------------------------------ voice memo
def test_voice_progress_reaches_the_schedule(monkeypatch):
    calls = _record_recomputes(monkeypatch)
    from routers import dr_voice_router as dvr

    async def fake_extract(*a, **k):
        return {"activity_updates": [{"activity_code": "A100", "pct_complete_end_of_day": 35}]}
    monkeypatch.setattr(dvr, "extract_dr_fields", fake_extract)
    monkeypatch.setattr(dvr, "ensure_ai_available", lambda *a, **k: None)

    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        before = dict(calls)
        await dvr.voice_extract(d16, {"transcript": "framing is at thirty five percent"}, ctx)
        row = await db.daily_report_activities.find_one({"id": r16.id})
        assert row["pct_complete_end_of_day"] == 35 and row.get("pct_set_at")
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 35 and t["status"] == "in_progress"
        assert t["actual_start"] == "2026-11-16"
        assert t["last_synced_from_dr"]["daily_report_id"] == d16
        assert calls["recompute"] > before["recompute"]
    _run(body)


# ------------------------------------------------------- start of day ("was")
def test_correcting_an_earlier_report_updates_was_on_later_days():
    """Lowering 11/16 from 45% to 30% must also change the "was" (start of
    day) shown on 11/17 and 11/18, without changing their own end-of-day or
    turning an untouched pre-filled row into entered progress."""
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        d17, r17 = await _day(db, tenant, pid, tid, ctx, "2026-11-17", 50, status="draft")
        # 11/18: added but the percent never touched (pre-filled at 50).
        d18 = await _report(db, tenant, pid, "2026-11-18")
        r18 = await _add(ctx, d18, tid)
        rows = {r["id"]: r for r in await db.daily_report_activities.find(
            {"schedule_activity_id": tid}, {"_id": 0}).to_list(None)}
        assert rows[r17]["pct_complete_start_of_day"] == 45
        assert rows[r18.id]["pct_complete_start_of_day"] == 50
        await _save(ctx, r16, pct_complete_end_of_day=30, edit_reason=REASON)
        rows = {r["id"]: r for r in await db.daily_report_activities.find(
            {"schedule_activity_id": tid}, {"_id": 0}).to_list(None)}
        assert rows[r17]["pct_complete_start_of_day"] == 30
        assert rows[r17]["pct_complete_end_of_day"] == 50
        assert rows[r18.id]["pct_complete_start_of_day"] == 50  # still follows 11/17
        assert not rows[r18.id].get("pct_set_at")
        # Lower 11/17 to 40: 11/18's untouched row follows (was 40, still untouched).
        await _save(ctx, r17, pct_complete_end_of_day=40)
        r = await db.daily_report_activities.find_one({"id": r18.id}, {"_id": 0})
        assert r["pct_complete_start_of_day"] == 40 and r["pct_complete_end_of_day"] == 40
        assert not r.get("pct_set_at")
        assert (await _task(db, tenant, tid))["pct_complete"] == 40
    _run(body)


def test_was_refresh_keeps_old_unstamped_progress_rows_counted():
    """A row saved before pct_set_at existed counts as progress only because
    end-of-day differs from start-of-day; moving its start-of-day must not
    make it look untouched."""
    from services.schedule_feedback import refresh_start_of_day, _row_sets_progress

    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 30)
        d17, r17 = await _day(db, tenant, pid, tid, ctx, "2026-11-17", 50, status="draft")
        # Make 11/17 look like a row saved before the stamp existed (start 0,
        # end 30, no pct_set_at): after the refresh its start becomes 30 too.
        await db.daily_report_activities.update_one(
            {"id": r17}, {"$unset": {"pct_set_at": ""},
                          "$set": {"pct_complete_start_of_day": 0, "pct_complete_end_of_day": 30}})
        await refresh_start_of_day(db, tenant, tid)
        r = await db.daily_report_activities.find_one({"id": r17}, {"_id": 0})
        assert r["pct_complete_start_of_day"] == 30 and r["pct_complete_end_of_day"] == 30
        assert _row_sets_progress(r)
    _run(body)


def test_was_refresh_leaves_hand_typed_start_and_locked_untouched_rows():
    from services.schedule_feedback import refresh_start_of_day

    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        d17, r17 = await _day(db, tenant, pid, tid, ctx, "2026-11-17", 50)
        # A person types 11/17's start-of-day by hand (with a reason).
        await _save(ctx, r17, pct_complete_start_of_day=40, edit_reason=REASON)
        # 11/18: untouched pre-filled row, then the report is approved.
        d18 = await _report(db, tenant, pid, "2026-11-18")
        r18 = await _add(ctx, d18, tid)
        await _lock(db, d18, "approved")
        before18 = await db.daily_report_activities.find_one({"id": r18.id}, {"_id": 0})
        await _save(ctx, r16, pct_complete_end_of_day=30, edit_reason=REASON)
        await refresh_start_of_day(db, tenant, tid)
        r = await db.daily_report_activities.find_one({"id": r17}, {"_id": 0})
        assert r["pct_complete_start_of_day"] == 40 and r.get("sod_set_at")
        after18 = await db.daily_report_activities.find_one({"id": r18.id}, {"_id": 0})
        assert (after18["pct_complete_start_of_day"], after18["pct_complete_end_of_day"]) == \
            (before18["pct_complete_start_of_day"], before18["pct_complete_end_of_day"])
    _run(body)


def test_one_time_job_does_not_refresh_was():
    from services.schedule_feedback import recompute_task_actuals

    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 45)
        d17, r17 = await _day(db, tenant, pid, tid, ctx, "2026-11-17", 50, status="draft")
        await db.daily_report_activities.update_one({"id": r16}, {"$set": {"pct_complete_end_of_day": 20}})
        await recompute_task_actuals(db, tenant, tid, refresh_sod=False)
        r = await db.daily_report_activities.find_one({"id": r17}, {"_id": 0})
        assert r["pct_complete_start_of_day"] == 45
    _run(body)


def test_draft_never_rewrites_a_locked_reports_was():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        # 11/12 draft at 60 (back-dated, never submitted), 11/16 submitted at 40.
        d12, r12 = await _day(db, tenant, pid, tid, ctx, "2026-11-12", 60, status="draft")
        d16, r16 = await _day(db, tenant, pid, tid, ctx, "2026-11-16", 40)
        await _save(ctx, r16, pct_complete_end_of_day=45, edit_reason=REASON)
        r = await db.daily_report_activities.find_one({"id": r16}, {"_id": 0})
        # Starts from the value before any report (0), not the draft's 60.
        assert r["pct_complete_start_of_day"] == 0
    _run(body)
