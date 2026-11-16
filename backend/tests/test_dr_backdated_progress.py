"""Back-dated daily reports must not overwrite current progress (2026-11-16).

Observed: the 11/16 report was approved with the activity at 70%. A new 11/13
report was then created; its row started the day at 70 (today's value) and
moving it to 20 set the TASK to 20 while actual start stayed 11/16.

Checked here, through the real daily report handlers against the local test
MongoDB (DB_NAME must be a throwaway test database; each test uses its own
company id and removes only its own documents):
  * a row on an older-dated report is stored but does not move the task's
    current percent / status / last-synced marker, and the save response
    names the later report (task_progress_kept_from);
  * a back-dated row starts its day from the earlier report's end-of-day
    (add-row and the grid pre-fill);
  * actual start moves earlier when a back-dated worked row is added;
  * deleting the latest report (or its row) puts back the previous report's
    end-of-day; actual finish is cleared while a later row is below 100%.

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_fix_backdated bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_dr_backdated_progress.py -q
"""
import asyncio
import os
import uuid

import pytest

if not os.environ.get("MONGO_URL") or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")):
    pytest.skip("needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)", allow_module_level=True)

import database  # noqa: E402
from models import DailyReportActivityUpdate  # noqa: E402
from routers import daily_reports_router as drr  # noqa: E402

COLLS = ["tasks", "projects", "daily_reports", "daily_report_activities",
         "task_sync_events", "audit_log", "productivity_observations",
         "project_access_watch", "delays", "daily_presence", "schedule_proposals"]


def _run(coro_fn):
    """Fresh motor client per test (each test has its own event loop)."""
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-backdated-" + uuid.uuid4().hex[:8]
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
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Backdated test",
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
                                       "created_at": "2026-11-16T00:00:00+00:00"})
    return rid


async def _add(ctx, dr_id, tid):
    return await drr.add_grid_row(dr_id, drr.AddActivityRow(schedule_activity_id=tid), ctx)


async def _save(ctx, row_id, **fields):
    return await drr.update_grid_row(row_id, DailyReportActivityUpdate(**fields), ctx)


async def _task(db, tenant, tid):
    return await db.tasks.find_one({"tenant_id": tenant, "id": tid}, {"_id": 0})


def test_backdated_row_does_not_change_current_pct():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant, pct=50)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        res16 = await _save(ctx, r16.id, pct_complete_end_of_day=70)
        assert res16.task_progress_kept_from is None
        await db.daily_reports.update_one({"id": d16}, {"$set": {"status": "approved"}})
        assert (await _task(db, tenant, tid))["pct_complete"] == 70

        d13 = await _report(db, tenant, pid, "2026-11-13")
        r13 = await _add(ctx, d13, tid)
        # Starts from the value before any report, not today's 70.
        assert r13.pct_complete_start_of_day == 50
        res13 = await _save(ctx, r13.id, pct_complete_end_of_day=20)
        assert res13.task_progress_kept_from == "2026-11-16"
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 70
        assert t["status"] == "in_progress"
        assert t["last_synced_from_dr"]["daily_report_id"] == d16
        # The back-dated row itself is stored.
        row = await db.daily_report_activities.find_one({"id": r13.id})
        assert row["pct_complete_end_of_day"] == 20

        # Saving the latest report again still sets current progress.
        await db.daily_reports.update_one({"id": d16}, {"$set": {"status": "draft"}})
        res = await _save(ctx, r16.id, pct_complete_end_of_day=75)
        assert res.task_progress_kept_from is None
        assert (await _task(db, tenant, tid))["pct_complete"] == 75
    _run(body)


def test_same_date_or_newer_report_sets_progress():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d10 = await _report(db, tenant, pid, "2026-11-10")
        r10 = await _add(ctx, d10, tid)
        await _save(ctx, r10.id, pct_complete_end_of_day=30)
        d12 = await _report(db, tenant, pid, "2026-11-12")
        r12 = await _add(ctx, d12, tid)
        assert r12.pct_complete_start_of_day == 30
        res = await _save(ctx, r12.id, pct_complete_end_of_day=45)
        assert res.task_progress_kept_from is None
        assert (await _task(db, tenant, tid))["pct_complete"] == 45
    _run(body)


def test_backdated_row_prefill_uses_earlier_report_value():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d10 = await _report(db, tenant, pid, "2026-11-10")
        r10 = await _add(ctx, d10, tid)
        await _save(ctx, r10.id, pct_complete_end_of_day=30)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _save(ctx, r16.id, pct_complete_end_of_day=70)

        # Add-row on a report between the two.
        d13 = await _report(db, tenant, pid, "2026-11-13")
        r13 = await _add(ctx, d13, tid)
        assert r13.pct_complete_start_of_day == 30
        assert r13.pct_complete_end_of_day == 30

        # Grid pre-fill (refresh) on another back-dated report.
        d14 = await _report(db, tenant, pid, "2026-11-14")
        out = await drr.refresh_grid(d14, ctx)
        assert out["inserted"] == 1
        row = await db.daily_report_activities.find_one({"daily_report_id": d14}, {"_id": 0})
        assert row["pct_complete_start_of_day"] == 30

        # A report dated after the latest one starts from current progress.
        d17 = await _report(db, tenant, pid, "2026-11-17")
        r17 = await _add(ctx, d17, tid)
        assert r17.pct_complete_start_of_day == 70
        assert (await _task(db, tenant, tid))["pct_complete"] == 70
    _run(body)


def test_actual_start_moves_earlier_with_backdated_worked_row():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _save(ctx, r16.id, pct_complete_end_of_day=40)
        assert (await _task(db, tenant, tid))["actual_start"] == "2026-11-16"

        d12 = await _report(db, tenant, pid, "2026-11-12")
        r12 = await _add(ctx, d12, tid)
        assert r12.pct_complete_start_of_day == 0
        res = await _save(ctx, r12.id, pct_complete_end_of_day=10)
        assert res.task_progress_kept_from == "2026-11-16"
        t = await _task(db, tenant, tid)
        assert t["actual_start"] == "2026-11-12"
        assert t["pct_complete"] == 40
    _run(body)


def test_old_bad_start_of_day_still_counts_as_worked():
    """A row saved before this fix may carry today's value as its stored
    start-of-day (70) with a lower end-of-day (20). Actual start judges it
    from the previous report's end-of-day instead, so it still counts."""
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _save(ctx, r16.id, pct_complete_end_of_day=70)
        d13 = await _report(db, tenant, pid, "2026-11-13")
        r13 = await _add(ctx, d13, tid)
        await db.daily_report_activities.update_one(
            {"id": r13.id}, {"$set": {"pct_complete_start_of_day": 70}})
        await _save(ctx, r13.id, pct_complete_end_of_day=20)
        t = await _task(db, tenant, tid)
        assert t["actual_start"] == "2026-11-13"
        assert t["pct_complete"] == 70
    _run(body)


def test_deleting_latest_report_restores_previous_value():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d13 = await _report(db, tenant, pid, "2026-11-13")
        r13 = await _add(ctx, d13, tid)
        await _save(ctx, r13.id, pct_complete_end_of_day=30)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _save(ctx, r16.id, pct_complete_end_of_day=70)
        assert (await _task(db, tenant, tid))["pct_complete"] == 70

        await drr.delete_daily_report(d16, None, ctx)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 30
        assert t["last_synced_from_dr"]["daily_report_id"] == d13

        await drr.delete_daily_report(d13, None, ctx)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 0
        assert t["last_synced_from_dr"] is None
        assert t.get("actual_start") is None
    _run(body)


def test_deleting_latest_row_restores_previous_value():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d13 = await _report(db, tenant, pid, "2026-11-13")
        r13 = await _add(ctx, d13, tid)
        await _save(ctx, r13.id, pct_complete_end_of_day=30, hours_worked=4)
        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _save(ctx, r16.id, pct_complete_end_of_day=70, hours_worked=6)
        assert (await _task(db, tenant, tid))["actual_hours_to_date"] == 10

        await drr.remove_grid_row(r16.id, ctx)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 30
        assert t["actual_hours_to_date"] == 4

        # Deleting an OLDER row leaves current progress alone.
        r16b = await _add(ctx, d16, tid)
        assert r16b.pct_complete_start_of_day == 30
        await _save(ctx, r16b.id, pct_complete_end_of_day=55)
        await drr.remove_grid_row(r13.id, ctx)
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 55
        assert t["actual_start"] == "2026-11-16"
    _run(body)


def test_actual_finish_needs_no_later_row_below_100():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d14 = await _report(db, tenant, pid, "2026-11-14")
        r14 = await _add(ctx, d14, tid)
        await _save(ctx, r14.id, pct_complete_end_of_day=100)
        assert (await _task(db, tenant, tid))["actual_finish"] == "2026-11-14"

        d16 = await _report(db, tenant, pid, "2026-11-16")
        r16 = await _add(ctx, d16, tid)
        await _save(ctx, r16.id, pct_complete_end_of_day=90)
        t = await _task(db, tenant, tid)
        assert t["actual_finish"] is None
        assert t["pct_complete"] == 90

        await drr.delete_daily_report(d16, None, ctx)
        t = await _task(db, tenant, tid)
        assert t["actual_finish"] == "2026-11-14"
        assert t["pct_complete"] == 100
    _run(body)
