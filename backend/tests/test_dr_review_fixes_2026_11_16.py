"""Review findings on the 2026-11-16 daily report / forecast / look-ahead work.

Each test names the finding it covers. Run through the real handlers against
the local test MongoDB (DB_NAME must be a throwaway test database; each test
uses its own company id and removes only its own documents).

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_fix_review bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_dr_review_fixes_2026_11_16.py -q
"""
import asyncio
import os
import uuid
from datetime import date, timedelta

import pytest

if not os.environ.get("MONGO_URL") or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")):
    pytest.skip("needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)", allow_module_level=True)

import database  # noqa: E402
from fastapi import BackgroundTasks  # noqa: E402
from models import DailyReportActivityUpdate, DailyPresenceCreate, TaskUpdate  # noqa: E402
from routers import daily_reports_router as drr  # noqa: E402
from services.working_days import add_working_days, working_days_between  # noqa: E402
# Imported now, at collection, so they bind the real database module even
# when a later-collected test module swaps in a stub (test_ms_project_sync).
from routers import tasks_router, schedule_review_router, dr_voice_router  # noqa: E402,F401
from services import (  # noqa: E402,F401
    cpm, forecast_refresh, one_time_migrations, schedule_engine, tasks_exports,
    dr_activity_linkage,
)

COLLS = ["tasks", "projects", "daily_reports", "daily_report_activities",
         "task_sync_events", "audit_log", "productivity_observations",
         "project_access_watch", "delays", "daily_presence", "schedule_proposals",
         "crew_capacity", "logic_proposals", "schedule_jobs"]

DATA_DATE = date(2026, 11, 16)  # a Monday


def _run(coro_fn):
    """Fresh motor client per test (each test has its own event loop)."""
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-review-" + uuid.uuid4().hex[:8]
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


@pytest.fixture
def frozen_data_date(monkeypatch):
    """Pin the project's site-clock today (the forecast data date)."""
    state = {"today": DATA_DATE}

    async def fake_today_for(db, tenant_id, project_id):
        return state["today"]
    import services.task_status as ts
    import services.site_time as st
    monkeypatch.setattr(ts, "project_today_for", fake_today_for)
    monkeypatch.setattr(st, "project_today_for", fake_today_for)
    return state


def _ctx(tenant, role="admin"):
    return {"tenant_id": tenant, "user": {"id": "u-admin", "role": role},
            "request_id": "req-" + uuid.uuid4().hex[:6]}


async def _project(db, tenant, **extra):
    pid = "p-" + uuid.uuid4().hex[:8]
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Review test",
                                  "status": "active", "schedule_engine": "impact_days",
                                  "schedule_mode": "mode1", **extra})
    return pid


async def _task(db, tenant, pid, code="A100", pct=0.0, ps="2026-11-02", pf="2026-11-30", **extra):
    tid = "task-" + uuid.uuid4().hex[:8]
    doc = {"id": tid, "tenant_id": tenant, "project_id": pid, "activity_code": code,
           "description": "Work " + code, "planned_start": ps, "planned_finish": pf,
           "pct_complete": pct, "status": "in_progress" if pct else "not_started",
           "created_at": "2026-11-01T00:00:00+00:00"}
    doc.update(extra)
    await db.tasks.insert_one(doc)
    return tid


async def _report(db, tenant, pid, day, status="draft"):
    rid = "dr-" + uuid.uuid4().hex[:8]
    await db.daily_reports.insert_one({"id": rid, "tenant_id": tenant, "project_id": pid,
                                       "report_date": day, "status": status,
                                       "created_at": "2026-11-16T00:00:00+00:00"})
    return rid


async def _add(ctx, dr_id, tid):
    return await drr.add_grid_row(dr_id, drr.AddActivityRow(schedule_activity_id=tid), ctx)


async def _save(ctx, row_id, **fields):
    return await drr.update_grid_row(row_id, DailyReportActivityUpdate(**fields), ctx)


async def _get(db, tenant, tid):
    return await db.tasks.find_one({"tenant_id": tenant, "id": tid}, {"_id": 0})


async def _row(db, row_id):
    return await db.daily_report_activities.find_one({"id": row_id}, {"_id": 0})


# ---------------------------------------------------------------------------
# Deleting an older report or row keeps progress set outside reports.
def test_deleting_older_report_keeps_progress_set_outside_reports():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        d05 = await _report(db, tenant, pid, "2026-11-05")
        r05 = await _add(ctx, d05, tid)
        await _save(ctx, r05.id, pct_complete_end_of_day=10)
        d10 = await _report(db, tenant, pid, "2026-11-10")
        r10 = await _add(ctx, d10, tid)
        await _save(ctx, r10.id, pct_complete_end_of_day=30)
        # A Microsoft Project pull then sets 60 directly on the task.
        await db.tasks.update_one({"id": tid}, {"$set": {"pct_complete": 60}})

        await drr.delete_daily_report(d05, None, ctx)
        t = await _get(db, tenant, tid)
        assert t["pct_complete"] == 60
        assert t["last_synced_from_dr"]["daily_report_id"] == d10
    _run(body)


def test_deleting_older_row_keeps_tasks_page_edit():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        d05 = await _report(db, tenant, pid, "2026-11-05")
        r05 = await _add(ctx, d05, tid)
        await _save(ctx, r05.id, pct_complete_end_of_day=10, hours_worked=4)
        d10 = await _report(db, tenant, pid, "2026-11-10")
        r10 = await _add(ctx, d10, tid)
        await _save(ctx, r10.id, pct_complete_end_of_day=30, hours_worked=6)
        # Tasks page edit to 60.
        from routers import tasks_router
        await tasks_router.update_task(tid, TaskUpdate(pct_complete=60), BackgroundTasks(), ctx)
        assert (await _get(db, tenant, tid))["pct_complete"] == 60

        await drr.remove_grid_row(r05.id, ctx)
        t = await _get(db, tenant, tid)
        assert t["pct_complete"] == 60
        # Hours and actual dates still follow the rows that remain.
        assert t["actual_hours_to_date"] == 6
        assert t["actual_start"] == "2026-11-10"
    _run(body)


# ---------------------------------------------------------------------------
# A stale pre-filled later row opened only for notes / crew does not win.
def test_notes_or_crew_only_save_on_later_report_does_not_block_earlier_change():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _save(ctx, r16.id, pct_complete_end_of_day=70)
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        assert r17.pct_complete_start_of_day == 70 and r17.pct_complete_end_of_day == 70
        await _save(ctx, r17.id, notes="Waiting on inspector")
        await _save(ctx, r17.id, crew_count=3)
        assert (await _row(db, r17.id)).get("last_sync_at")

        # The 11/16 report is corrected from 70 to 80.
        res = await _save(ctx, r16.id, pct_complete_end_of_day=80)
        assert res.task_progress_kept_from is None
        t = await _get(db, tenant, tid)
        assert t["pct_complete"] == 80
        assert t["last_synced_from_dr"]["daily_report_id"] == d16
        # A recompute of current progress also reads 80, not 11/17's 70.
        from services.schedule_feedback import current_progress_from_reports
        assert (await current_progress_from_reports(db, tenant, tid))["pct_complete"] == 80

        # Once a person enters 11/17's end-of-day, that row decides.
        await _save(ctx, r17.id, pct_complete_end_of_day=85)
        assert (await _get(db, tenant, tid))["pct_complete"] == 85
    _run(body)


# ---------------------------------------------------------------------------
# A back-dated save leaves the edit stamps alone (no false 409).
def test_backdated_save_does_not_cause_false_conflict():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        res = await _save(ctx, r16.id, pct_complete_end_of_day=50)
        stamp_a = res.task_updated_at
        lock_before = (await _get(db, tenant, tid))["progress_lock_at"]

        # Supervisor B saves a back-dated row for the same task.
        d13 = await _report(db, tenant, pid, "2026-11-13")
        r13 = await _add(ctx, d13, tid)
        resb = await _save(ctx, r13.id, pct_complete_end_of_day=20)
        assert resb.task_progress_kept_from == "2026-11-16"
        assert (await _get(db, tenant, tid))["progress_lock_at"] == lock_before

        # Supervisor A saves again on 11/16 with the stamp held before.
        out = await _save(ctx, r16.id, pct_complete_end_of_day=60, task_last_modified_at=stamp_a)
        assert out.task_progress_kept_from is None
        assert (await _get(db, tenant, tid))["pct_complete"] == 60
    _run(body)


# ---------------------------------------------------------------------------
# History lookups are indexed.
def test_history_indexes_exist():
    async def body(db, tenant):
        from services.schedule_feedback import ensure_history_indexes
        await ensure_history_indexes(db)
        rows_idx = await db.daily_report_activities.index_information()
        ev_idx = await db.task_sync_events.index_information()
        assert any(v["key"] == [("schedule_activity_id", 1)] for v in rows_idx.values())
        assert any(v["key"] == [("tenant_id", 1), ("task_id", 1), ("triggered_at", 1)]
                   for v in ev_idx.values())
    _run(body)


def test_grid_prefill_loads_history_in_one_query(monkeypatch):
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        t1 = await _task(db, tenant, pid, "A1")
        # Already started, so the forecast never moves its start past 11/13.
        t2 = await _task(db, tenant, pid, "A2", pct=5)
        d10 = await _report(db, tenant, pid, "2026-11-10")
        r1 = await _add(ctx, d10, t1)
        await _save(ctx, r1.id, pct_complete_end_of_day=30)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r1b = await _add(ctx, d16, t1)
        await _save(ctx, r1b.id, pct_complete_end_of_day=70)

        import services.schedule_feedback as sf
        calls = {"single": 0}
        real = sf.task_report_history

        async def counting(*a, **k):
            calls["single"] += 1
            return await real(*a, **k)
        monkeypatch.setattr(sf, "task_report_history", counting)
        d13 = await _report(db, tenant, pid, "2026-11-13")
        out = await drr.refresh_grid(d13, ctx)
        assert out["inserted"] == 2
        assert calls["single"] == 0
        rows = {r["schedule_activity_id"]: r for r in await db.daily_report_activities.find(
            {"daily_report_id": d13}, {"_id": 0}).to_list(None)}
        assert rows[t1]["pct_complete_start_of_day"] == 30
        assert rows[t2]["pct_complete_start_of_day"] == 5
    _run(body)


# ---------------------------------------------------------------------------
# A slider save re-runs the forecast with every flag off.
def test_slider_save_moves_forecast_with_flags_off(frozen_data_date):
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        ps = date(2026, 11, 2)
        pf = add_working_days(ps, 9)
        assert working_days_between(ps, pf + timedelta(days=1)) == 10
        a = await _task(db, tenant, pid, "A100", pct=10, ps=ps.isoformat(), pf=pf.isoformat(),
                        actual_start=ps.isoformat(), actuals_source="ms_project",
                        duration_days=10)
        b = await _task(db, tenant, pid, "B200", ps="2026-11-17", pf="2026-11-20",
                        predecessors=["A100"], duration_days=4)
        from services.cpm import auto_recompute_after_event
        await auto_recompute_after_event(tenant, pid)
        a0, b0 = await _get(db, tenant, a), await _get(db, tenant, b)
        assert a0["projected_finish_date"] > DATA_DATE.isoformat()

        d16 = await _report(db, tenant, pid, DATA_DATE.isoformat())
        row = await _add(ctx, d16, a)
        await _save(ctx, row.id, pct_complete_end_of_day=90)
        a1, b1 = await _get(db, tenant, a), await _get(db, tenant, b)
        assert a1["pct_complete"] == 90
        assert (a1.get("projected_finish_date") or a1["planned_finish"]) < a0["projected_finish_date"]
        b0_start = b0.get("projected_start_date") or b0["planned_start"]
        b1_start = b1.get("projected_start_date") or b1["planned_start"]
        assert b1_start < b0_start
    _run(body)


# ---------------------------------------------------------------------------
# Remaining-days math, complete status and paperwork in the forecast.
def _span_dates(start: date, span: int):
    pf = add_working_days(start, span - 1)
    assert working_days_between(start, pf + timedelta(days=1)) == span
    return start.isoformat(), pf.isoformat()


def test_remaining_days_have_no_floating_point_extra_day():
    async def body(db, tenant):
        from services.task_status import compute_projected_finish_dates
        pid = await _project(db, tenant)
        ps10, pf10 = _span_dates(date(2026, 9, 14), 10)
        ps20, pf20 = _span_dates(date(2026, 9, 14), 20)
        t70 = await _task(db, tenant, pid, "S10", pct=70, ps=ps10, pf=pf10, actual_start=ps10)
        t95 = await _task(db, tenant, pid, "S20", pct=95, ps=ps20, pf=pf20, actual_start=ps20)
        await compute_projected_finish_dates(db, tenant, pid, data_date=DATA_DATE)
        # 10 days at 70% -> 3 days left (Mon-Wed); 20 at 95% -> 1 day.
        assert (await _get(db, tenant, t70))["projected_finish_date"] == \
            add_working_days(DATA_DATE, 2).isoformat()
        assert (await _get(db, tenant, t95))["projected_finish_date"] == DATA_DATE.isoformat()
    _run(body)


def test_complete_status_and_paperwork_are_not_forecast():
    async def body(db, tenant):
        from services.task_status import compute_projected_finish_dates
        pid = await _project(db, tenant)
        ps, pf = _span_dates(date(2026, 10, 1), 10)
        done = await _task(db, tenant, pid, "DONE", pct=80, ps=ps, pf=pf, actual_start=ps,
                           status="complete")
        paper = await _task(db, tenant, pid, "SUB1", pct=0, ps=ps, pf=pf, status="in_progress",
                            work_category="preconstruction_paperwork")
        field = await _task(db, tenant, pid, "FLD1", pct=0, ps=ps, pf=pf, status="in_progress")
        await compute_projected_finish_dates(db, tenant, pid, data_date=DATA_DATE)
        for tid in (done, paper):
            t = await _get(db, tenant, tid)
            assert (t.get("projected_finish_date") or t["planned_finish"]) < DATA_DATE.isoformat()
        # Ordinary field work in progress at 0% is still forecast.
        assert (await _get(db, tenant, field))["projected_finish_date"] >= DATA_DATE.isoformat()
    _run(body)


# ---------------------------------------------------------------------------
# Daily forecast refresh; overdue is stable across recomputes.
def test_daily_forecast_refresh_and_stable_overdue(frozen_data_date, monkeypatch):
    async def body(db, tenant):
        from services.forecast_refresh import refresh_due_forecasts
        from services.schedule_filters import is_overdue
        from routers import tasks_router
        pid = await _project(db, tenant)
        late = await _task(db, tenant, pid, "LATE", ps="2026-11-02", pf="2026-11-02",
                           duration_days=1)
        first = await refresh_due_forecasts(db, tenant)
        assert first["refreshed"] == 1
        t = await _get(db, tenant, late)
        assert t["projected_start_date"] == "2026-11-16"
        assert is_overdue(t, "2026-11-16") is True
        # Same day again: nothing to do.
        assert (await refresh_due_forecasts(db, tenant))["already_today"] == 1

        # Friday, no events: the stored forecast is stale, but the task is
        # overdue either way (judged on the planned finish).
        frozen_data_date["today"] = date(2026, 11, 20)
        monkeypatch.setattr(tasks_router, "project_today", lambda tz=None: date(2026, 11, 20))
        t = await _get(db, tenant, late)
        assert is_overdue(t, "2026-11-20") is True
        dash = await tasks_router.dashboard_activities(pid, days=14, limit=8, ctx=_ctx(tenant))
        assert [r["activity_code"] for r in dash["items"] if r["overdue"]] == ["LATE"]

        # The daily refresh moves the forecast to the new data date; the
        # overdue flag and the dashboard do not change with it.
        assert (await refresh_due_forecasts(db, tenant))["refreshed"] == 1
        t = await _get(db, tenant, late)
        assert t["projected_start_date"] == "2026-11-20"
        assert is_overdue(t, "2026-11-20") is True
        dash = await tasks_router.dashboard_activities(pid, days=14, limit=8, ctx=_ctx(tenant))
        assert [r["activity_code"] for r in dash["items"] if r["overdue"]] == ["LATE"]
    _run(body)


def test_daily_forecast_refresh_skips_ms_project_projects(frozen_data_date, monkeypatch):
    async def body(db, tenant):
        import services.feature_flags as ff
        from services.forecast_refresh import refresh_due_forecasts
        monkeypatch.setattr(ff, "schedule_mode_2_enabled", lambda: True)
        await _project(db, tenant, schedule_mode="mode2")
        out = await refresh_due_forecasts(db, tenant)
        assert out["skipped_engine"] == 1 and out["refreshed"] == 0
        assert await db.schedule_jobs.count_documents({"tenant_id": tenant}) == 0
    _run(body)


# ---------------------------------------------------------------------------
# The live "Add activity" picker uses the shared overdue rule.
def test_activity_picker_late_uses_shared_rule():
    async def body(db, tenant):
        from routers import schedule_review_router as srr
        pid = await _project(db, tenant)
        await _task(db, tenant, pid, "FULL", pct=100, ps="2026-11-02", pf="2026-11-06",
                    status="in_progress")
        await _task(db, tenant, pid, "HALF", pct=40, ps="2026-11-02", pf="2026-11-06")
        await _task(db, tenant, pid, "SLIP", pct=10, ps="2026-11-02", pf="2026-11-06",
                    projected_start_date="2026-11-16", projected_finish_date="2026-11-20")
        dr = await _report(db, tenant, pid, "2026-11-16")
        out = await srr.activity_picker(dr, ctx=_ctx(tenant))
        late = {t["activity_code"]: t["late"] for t in out}
        assert late == {"FULL": False, "HALF": True, "SLIP": True}
    _run(body)


# ---------------------------------------------------------------------------
# Overdue rows never push in-window work past the cap.
def test_many_overdue_rows_do_not_drop_in_window_work():
    async def body(db, tenant):
        from routers import tasks_router
        from services import tasks_exports
        pid = await _project(db, tenant)
        docs = [{"id": f"task-od-{i}-{uuid.uuid4().hex[:6]}", "tenant_id": tenant,
                 "project_id": pid, "activity_code": f"OD{i:04d}", "description": "late",
                 "planned_start": "2026-09-01", "planned_finish": "2026-09-30",
                 "pct_complete": 0, "status": "not_started",
                 "created_at": "2026-11-01T00:00:00+00:00"} for i in range(520)]
        await db.tasks.insert_many(docs)
        inwin = await _task(db, tenant, pid, "INWIN", ps="2026-11-20", pf="2026-11-24")
        rows = await tasks_router.lookahead(pid, days=14, start=DATA_DATE, ctx=_ctx(tenant))
        codes = [r.activity_code for r in rows]
        assert "INWIN" in codes
        assert sum(1 for r in rows if r.overdue) == 200
        # Only the in-window row was marked in_window.
        assert (await _get(db, tenant, inwin))["lookahead_status"] == "in_window"
        assert await db.tasks.count_documents(
            {"tenant_id": tenant, "lookahead_status": "in_window"}) == 1
        payload = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=DATA_DATE)
        assert "INWIN" in [r["activity_code"] for r in payload["rows"]]
    _run(body)


# ---------------------------------------------------------------------------
# Startup migration: settings loaded first; untouched back-dated rows.
def test_migration_waits_for_settings_and_uses_ms_project_engine(monkeypatch):
    async def body(db, tenant):
        from services import one_time_migrations as otm
        from services import runtime_settings as rs
        import services.schedule_engine as se
        import services.cpm as cpm

        # 1. Settings fail to load: the actual-date job is not claimed.
        async def fail():
            raise RuntimeError("database not reachable")
        monkeypatch.setattr(rs, "refresh_cache", fail)
        seen = []

        async def fake_run_job(_db, jid, _job):
            seen.append(jid)
        real_run_job = otm._run_job
        monkeypatch.setattr(otm, "_run_job", fake_run_job)
        await otm.run_one_time_jobs()
        assert otm.ACTUALS_JOB_ID not in seen
        monkeypatch.setattr(otm, "_run_job", real_run_job)

        # 2. Settings say Mode 2 is on: a Mode 2 project goes to the
        #    Microsoft Project engine, never the internal chain.
        async def load():
            rs._cache = {"flags": {"FF_SCHEDULE_MODE_2": True}}
        monkeypatch.setattr(rs, "refresh_cache", load)
        assert await otm._settings_loaded()
        dispatched, internal = [], []

        class FakeEngine:
            async def recompute(self, tenant_id, project_id, **kw):
                dispatched.append(project_id)
        monkeypatch.setattr(se, "get_engine_for", lambda project: FakeEngine())

        async def fake_chain(tenant_id, project_id, **kw):
            internal.append(project_id)
        monkeypatch.setattr(cpm, "_run_internal_cpm_chain", fake_chain)
        try:
            pid = await _project(db, tenant, schedule_mode="mode2")
            tid = await _task(db, tenant, pid, pct=30)
            d = await _report(db, tenant, pid, "2026-11-09", status="approved")
            await db.daily_report_activities.insert_one({
                "id": "row-" + uuid.uuid4().hex[:8], "daily_report_id": d,
                "schedule_activity_id": tid, "position": 0,
                "pct_complete_start_of_day": 0, "pct_complete_end_of_day": 30,
                "pct_set_at": "2026-11-09T12:00:00+00:00", "hours_worked": 0,
            })
            await otm._recompute_actuals_from_reports(db)
            assert (await _get(db, tenant, tid))["actual_start"] == "2026-11-09"
            assert pid in dispatched and pid not in internal
        finally:
            rs.reset_cache_for_tests()
    _run(body)


def test_untouched_backdated_row_does_not_move_actual_start_earlier():
    async def body(db, tenant):
        from services import one_time_migrations as otm
        from services.schedule_feedback import recompute_task_actuals
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        d10 = await _report(db, tenant, pid, "2026-11-10")
        r10 = await _add(ctx, d10, tid)
        await _save(ctx, r10.id, pct_complete_end_of_day=50)
        assert (await _get(db, tenant, tid))["actual_start"] == "2026-11-10"
        # An older report's row, pre-filled with the then-current 50 and
        # submitted untouched (submit stamps last_sync_at).
        d01 = await _report(db, tenant, pid, "2026-11-01", status="submitted")
        await db.daily_report_activities.insert_one({
            "id": "row-" + uuid.uuid4().hex[:8], "daily_report_id": d01,
            "schedule_activity_id": tid, "position": 0,
            "pct_complete_start_of_day": 50, "pct_complete_end_of_day": 50,
            "hours_worked": 0, "last_sync_at": "2026-11-12T12:00:00+00:00",
        })
        await recompute_task_actuals(db, tenant, tid)
        assert (await _get(db, tenant, tid))["actual_start"] == "2026-11-10"
        await otm._recompute_actuals_from_reports(db)
        assert (await _get(db, tenant, tid))["actual_start"] == "2026-11-10"
    _run(body)


def test_stale_running_claim_is_retaken():
    async def body(db, tenant):
        from services import one_time_migrations as otm
        job_id = "test-stale-" + uuid.uuid4().hex[:8]
        try:
            await db.migrations.insert_one({"_id": job_id, "status": "running",
                                            "started_at": "2026-01-01T00:00:00+00:00"})
            runs = []

            async def job(_db):
                runs.append(1)
                return {}
            await otm._run_job(db, job_id, job)
            assert runs == [1]
            assert (await db.migrations.find_one({"_id": job_id}))["status"] == "done"
            # A fresh "running" claim is left alone.
            await db.migrations.update_one({"_id": job_id}, {"$set": {
                "status": "running", "started_at": otm.utc_now().isoformat()}})
            await otm._run_job(db, job_id, job)
            assert runs == [1]
        finally:
            await db.migrations.delete_one({"_id": job_id})
    _run(body)


# ---------------------------------------------------------------------------
# Actual dates set by hand survive submit / approve.
def test_manual_actual_start_survives_approve():
    async def body(db, tenant):
        from routers import tasks_router
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        d03 = await _report(db, tenant, pid, "2026-11-03")
        r03 = await _add(ctx, d03, tid)
        await _save(ctx, r03.id, pct_complete_end_of_day=20)
        assert (await _get(db, tenant, tid))["actual_start"] == "2026-11-03"

        await tasks_router.update_task(tid, TaskUpdate(actual_start="2026-11-01"), BackgroundTasks(), ctx)
        t = await _get(db, tenant, tid)
        assert t["actual_start"] == "2026-11-01" and t["actuals_source"] == "manual"

        await db.daily_reports.update_one({"id": d03}, {"$set": {"status": "submitted"}})
        await drr.approve_daily_report(d03, ctx)
        assert (await _get(db, tenant, tid))["actual_start"] == "2026-11-01"
    _run(body)


# ---------------------------------------------------------------------------
# Crew rows: grid dropdown, typed hours, voice intake.
async def _crew(ctx, dr_id, tid, hc, hrs, trade="Carpentry"):
    return await drr.create_presence(dr_id, DailyPresenceCreate(
        daily_report_id=dr_id, trade=trade, company_name="Metro " + trade,
        headcount=hc, hours_per_person=hrs, schedule_activity_id=tid), ctx)


def test_grid_crew_pick_is_not_counted_twice():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        dr = await _report(db, tenant, pid, "2026-11-16")
        row = await _add(ctx, dr, tid)
        c1 = await _crew(ctx, dr, tid, 3, 8)
        assert (await _row(db, row.id))["hours_worked"] == 24

        # Picking that same (already linked) crew in the grid adds nothing.
        await _save(ctx, row.id, linked_crew_id=c1.id)
        assert await db.daily_presence.count_documents({"daily_report_id": dr}) == 1
        r = await _row(db, row.id)
        assert (r["hours_worked"], r["crew_count"]) == (24, 3)

        # Another crew row on the same activity adds only its own hours.
        await _crew(ctx, dr, tid, 2, 8, trade="Drywall")
        r = await _row(db, row.id)
        assert (r["hours_worked"], r["crew_count"]) == (40, 5)
        assert (await _get(db, tenant, tid))["actual_hours_to_date"] == 40

        # Picking a crew that is not yet linked here: its own headcount is
        # used (not the row's crew total), and it counts right away.
        c3 = await _crew(ctx, dr, None, 4, 8, trade="Electrical")
        await _save(ctx, row.id, linked_crew_id=c3.id)
        auto = await db.daily_presence.find_one({"daily_report_id": dr, "sub_id": c3.id})
        assert auto["headcount"] == 4
        r = await _row(db, row.id)
        assert (r["hours_worked"], r["crew_count"]) == (72, 9)
    _run(body)


def test_typed_hours_are_kept_when_crew_rows_change():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid)
        dr = await _report(db, tenant, pid, "2026-11-16")
        row = await _add(ctx, dr, tid)
        await _save(ctx, row.id, hours_worked=32)
        await _crew(ctx, dr, tid, 4, 0)
        r = await _row(db, row.id)
        assert (r["hours_worked"], r["crew_count"]) == (32, 4)
        await _crew(ctx, dr, tid, 2, 8, trade="Drywall")
        r = await _row(db, row.id)
        assert (r["hours_worked"], r["crew_count"]) == (32, 6)
        assert (await _get(db, tenant, tid))["actual_hours_to_date"] == 32
    _run(body)


def test_voice_intake_crew_counts_toward_activity(monkeypatch):
    async def body(db, tenant):
        from routers import dr_voice_router as dvr
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid, "FW-0003")
        dr = await _report(db, tenant, pid, "2026-11-16")
        row = await _add(ctx, dr, tid)

        async def fake_extract(transcript, **kw):
            return {"crew_presence": [{"trade_code": "CARP", "company_name": "Metro Framing",
                                       "headcount": 4, "hours_per_person": 8,
                                       "linked_activity_code": "FW-0003"}]}
        monkeypatch.setattr(dvr, "extract_dr_fields", fake_extract)

        async def allow(*a, **k):
            return None
        import services.project_access as pa
        monkeypatch.setattr(pa, "check_project_access", allow)
        out = await dvr.voice_extract(dr, {"transcript": "4 carpenters on FW-0003, 8 hours"}, ctx)
        assert out["applied"]["crew_presence"] == 1
        r = await _row(db, row.id)
        assert (r["hours_worked"], r["crew_count"]) == (32, 4)
        t = await _get(db, tenant, tid)
        assert t["actual_hours_to_date"] == 32
        assert t["actual_start"] == "2026-11-16"
    _run(body)
