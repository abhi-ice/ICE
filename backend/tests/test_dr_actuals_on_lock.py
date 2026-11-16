"""Actual dates for tasks touched by older daily reports (2026-11-16).

Actual start / finish used to be recomputed only when a single row was
saved, so a task touched only by an older (back-dated) report could be left
without its actual dates. Checked here, through the real handlers against
the local test MongoDB (DB_NAME must be a throwaway test database; each test
uses its own company id and removes only its own documents):
  * submitting a report recomputes actual dates for every task on its rows,
    then runs exactly one schedule recompute for the project;
  * approving a report does the same;
  * percent complete is never changed, and imported (Microsoft Project)
    actuals are kept;
  * the one-time startup job recomputes actual dates for every task that has
    report rows, recomputes once each project whose actual dates changed, records counts, and
    runs only once per database.

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_fix_actuals bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_dr_actuals_on_lock.py -q
"""
import asyncio
import os
import uuid

import pytest

if not os.environ.get("MONGO_URL") or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")):
    pytest.skip("needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)", allow_module_level=True)

import database  # noqa: E402
import services.cpm as cpm  # noqa: E402
from routers import daily_reports_router as drr  # noqa: E402
from services import one_time_migrations as otm  # noqa: E402

COLLS = ["tasks", "projects", "daily_reports", "task_sync_events", "audit_log",
         "productivity_observations", "delays", "daily_presence", "crew_capacity"]


@pytest.fixture
def recompute_calls(monkeypatch):
    """Record every schedule recompute (tenant, project) and still run it."""
    calls = []
    real = cpm.auto_recompute_after_event

    async def spy(tenant_id, project_id, **kw):
        calls.append((tenant_id, project_id))
        return await real(tenant_id, project_id, **kw)
    monkeypatch.setattr(cpm, "auto_recompute_after_event", spy)
    return calls


def _run(coro_fn):
    """Fresh motor client per test (each test has its own event loop)."""
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-actlock-" + uuid.uuid4().hex[:8]
        db = database.get_db()
        try:
            return await coro_fn(db, tenant)
        finally:
            rows = await db.daily_reports.find({"tenant_id": tenant}, {"id": 1}).to_list(None)
            await db.daily_report_activities.delete_many(
                {"daily_report_id": {"$in": [r["id"] for r in rows]}})
            for c in COLLS:
                await db[c].delete_many({"tenant_id": tenant})
            database._client.close()
            database._client, database._db = None, None
    return asyncio.run(wrapper())


def _ctx(tenant):
    return {"tenant_id": tenant, "user": {"id": "u-admin", "role": "admin"},
            "request_id": "req-" + uuid.uuid4().hex[:6]}


async def _project(db, tenant):
    pid = "p-" + uuid.uuid4().hex[:8]
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Actuals test",
                                  "schedule_engine": "impact_days", "schedule_mode": "mode1"})
    return pid


async def _task(db, tenant, pid, pct, **extra):
    tid = "task-" + uuid.uuid4().hex[:8]
    await db.tasks.insert_one({
        "id": tid, "tenant_id": tenant, "project_id": pid, "activity_code": "A" + tid[-4:],
        "description": "Work", "planned_start": "2026-11-02", "planned_finish": "2026-11-30",
        "pct_complete": pct, "status": "in_progress" if pct else "not_started", **extra,
    })
    return tid


async def _report(db, tenant, pid, day, status="draft"):
    rid = "dr-" + uuid.uuid4().hex[:8]
    await db.daily_reports.insert_one({"id": rid, "tenant_id": tenant, "project_id": pid,
                                       "report_date": day, "status": status,
                                       "created_at": "2026-11-16T00:00:00+00:00"})
    return rid


async def _row(db, dr_id, tid, sod, eod, hours=0.0):
    """A report row written straight to the database, the way rows saved
    before this fix (or by an older flow) sit there: no row save ran, so the
    task's actual dates were never recomputed from it."""
    await db.daily_report_activities.insert_one({
        "id": "row-" + uuid.uuid4().hex[:8], "daily_report_id": dr_id,
        "schedule_activity_id": tid, "position": 0,
        "pct_complete_start_of_day": sod, "pct_complete_end_of_day": eod,
        "hours_worked": hours, "last_sync_at": "2026-11-16T12:00:00+00:00",
    })


async def _get(db, tenant, tid):
    return await db.tasks.find_one({"tenant_id": tenant, "id": tid}, {"_id": 0})


def test_submit_recomputes_actuals_once_and_keeps_pct(recompute_calls):
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        a = await _task(db, tenant, pid, 70)
        b = await _task(db, tenant, pid, 40)
        d13 = await _report(db, tenant, pid, "2026-11-13")
        await _row(db, d13, a, 40, 50, hours=8)
        await _row(db, d13, b, 30, 40)
        assert (await _get(db, tenant, a)).get("actual_start") is None

        await drr.submit_daily_report(d13, drr.SubmitPayload(), ctx)

        ta, tb = await _get(db, tenant, a), await _get(db, tenant, b)
        assert ta["actual_start"] == "2026-11-13"
        assert ta["actuals_source"] == "daily_report"
        assert ta.get("actual_finish") is None
        assert tb["actual_start"] == "2026-11-13"
        # Percent complete never moves.
        assert ta["pct_complete"] == 70 and tb["pct_complete"] == 40
        # One schedule recompute for the report, not one per task.
        assert recompute_calls == [(tenant, pid)]
    _run(body)


def test_approve_recomputes_actuals_including_finish(recompute_calls):
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid, 100, status="complete")
        d10 = await _report(db, tenant, pid, "2026-11-10", status="submitted")
        d11 = await _report(db, tenant, pid, "2026-11-11", status="submitted")
        await _row(db, d10, tid, 60, 90)
        await _row(db, d11, tid, 90, 100)

        await drr.approve_daily_report(d10, ctx)

        t = await _get(db, tenant, tid)
        assert t["actual_start"] == "2026-11-10"
        assert t["actual_finish"] == "2026-11-11"
        assert t["pct_complete"] == 100
        assert recompute_calls == [(tenant, pid)]
    _run(body)


def test_submit_keeps_imported_actuals(recompute_calls):
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid = await _project(db, tenant)
        tid = await _task(db, tenant, pid, 55, actual_start="2026-11-03",
                          actuals_source="ms_project")
        d12 = await _report(db, tenant, pid, "2026-11-12")
        await _row(db, d12, tid, 50, 55, hours=4)

        await drr.submit_daily_report(d12, drr.SubmitPayload(), ctx)

        t = await _get(db, tenant, tid)
        assert t["actual_start"] == "2026-11-03"
        assert t["actuals_source"] == "ms_project"
        assert t["pct_complete"] == 55
    _run(body)


def test_report_without_task_rows_skips_recompute(recompute_calls):
    async def body(db, tenant):
        pid = await _project(db, tenant)
        dr_id = await _report(db, tenant, pid, "2026-11-12", status="submitted")
        await drr.approve_daily_report(dr_id, _ctx(tenant))
        assert recompute_calls == []
    _run(body)


def test_one_time_job_recomputes_actuals_and_each_project_once(recompute_calls):
    async def body(db, tenant):
        p1, p2 = await _project(db, tenant), await _project(db, tenant)
        # No actuals yet, touched only by an older report.
        a = await _task(db, tenant, p1, 70)
        # Wrong actual start (later than its first worked report).
        b = await _task(db, tenant, p1, 30, actual_start="2026-11-16",
                        actuals_source="daily_report")
        # Imported actuals: kept.
        c = await _task(db, tenant, p2, 20, actual_start="2026-11-02",
                        actuals_source="ms_project")
        # No report rows: not touched.
        d = await _task(db, tenant, p2, 10)
        r1 = await _report(db, tenant, p1, "2026-11-09", status="approved")
        r2 = await _report(db, tenant, p2, "2026-11-10", status="submitted")
        await _row(db, r1, a, 50, 60, hours=8)
        await _row(db, r1, b, 0, 10)
        await _row(db, r2, c, 10, 20)

        result = await otm._recompute_actuals_from_reports(db)

        ta, tb, tc, td = [await _get(db, tenant, x) for x in (a, b, c, d)]
        assert ta["actual_start"] == "2026-11-09"
        assert tb["actual_start"] == "2026-11-09"
        assert tc["actual_start"] == "2026-11-02" and tc["actuals_source"] == "ms_project"
        assert td.get("actual_start") is None
        assert [t["pct_complete"] for t in (ta, tb, tc, td)] == [70, 30, 20, 10]
        mine = [call for call in recompute_calls if call[0] == tenant]
        # p1 had actual dates change; p2 only has imported actuals, so it
        # needs no recompute.
        assert mine == [(tenant, p1)]
        for key in ("tasks_with_rows", "tasks_checked", "tasks_changed", "tasks_skipped",
                    "tasks_failed", "projects_recomputed", "projects_failed"):
            assert key in result
        assert result["tasks_changed"] >= 2
        assert result["tasks_skipped"] >= 1
        assert result["projects_recomputed"] >= 1

        # Running it again changes nothing for this company.
        recompute_calls.clear()
        again = await otm._recompute_actuals_from_reports(db)
        assert [(await _get(db, tenant, x)).get("actual_start") for x in (a, b, c, d)] == \
            ["2026-11-09", "2026-11-09", "2026-11-02", None]
        assert again["tasks_failed"] == 0
        assert [call for call in recompute_calls if call[0] == tenant] == []
    _run(body)


def test_one_time_job_runs_once_and_is_registered(monkeypatch):
    async def body(db, tenant):
        job_id = "test-actuals-" + uuid.uuid4().hex[:8]
        runs = []

        async def job(_db):
            runs.append(1)
            return {"tasks_changed": 0}
        try:
            await otm._run_job(db, job_id, job)
            await otm._run_job(db, job_id, job)
            rec = await db.migrations.find_one({"_id": job_id})
            assert runs == [1]
            assert rec["status"] == "done" and rec["result"] == {"tasks_changed": 0}
        finally:
            await db.migrations.delete_one({"_id": job_id})

        # The startup runner includes the actual-date job.
        seen = []

        async def fake_run_job(_db, jid, _job):
            seen.append(jid)
        monkeypatch.setattr(otm, "_run_job", fake_run_job)
        await otm.run_one_time_jobs()
        assert otm.ACTUALS_JOB_ID in seen
    _run(body)
