"""Field-created activity codes, crew hours and status consistency (2026-11-16).

Checked here, through the real handlers against the local test MongoDB
(DB_NAME must be a throwaway test database; each test uses its own company id
and removes only its own documents):
  (5) every activity created from the field gets its own code per project,
      FW-0001, FW-0002, ... (the old rule handed out the same AF2311 to every
      one), including when several are created at the same moment, and the
      numbering continues after the highest FW-n code already there;
  (6) crew logged on a report activity (headcount x hours each) counts toward
      it: the report row's hours worked / crew count follow the crew rows on
      create / update / delete, and the task's hours to date are summed over
      all its report rows;
  (7) a task at 0% with no actual start reads 'not_started' after a report
      save; a new field-created activity at 0% is 'not_started'; the delay
      wizard's "Is it fixed yet?" answer is saved exactly as picked.

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_fix_fieldcrew bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_field_activity_crew_status.py -q
"""
import asyncio
import os
import uuid

import pytest

if not os.environ.get("MONGO_URL") or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")):
    pytest.skip("needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)", allow_module_level=True)

import database  # noqa: E402
from models import (  # noqa: E402
    DailyReportActivityUpdate, DailyPresenceCreate, DailyPresenceUpdate,
    UnplannedActivityCreate, DelayCreate,
)
from routers import daily_reports_router as drr  # noqa: E402
from routers import field_events_router as fer  # noqa: E402
from services.field_activity_codes import next_field_activity_code  # noqa: E402

COLLS = ["tasks", "projects", "daily_reports", "daily_report_activities",
         "task_sync_events", "audit_log", "productivity_observations",
         "project_access_watch", "delays", "daily_presence", "schedule_proposals",
         "field_activity_code_counters", "crew_capacity"]


def _run(coro_fn):
    """Fresh motor client per test (each test has its own event loop)."""
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-fieldcrew-" + uuid.uuid4().hex[:8]
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


async def _setup(db, tenant, pct=0.0, status=None, code="A100"):
    pid = "p-" + uuid.uuid4().hex[:8]
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Field test",
                                  "schedule_engine": "impact_days", "schedule_mode": "mode1"})
    tid = await _task_doc(db, tenant, pid, code, pct, status)
    return pid, tid


async def _task_doc(db, tenant, pid, code, pct=0.0, status=None):
    tid = "task-" + uuid.uuid4().hex[:8]
    await db.tasks.insert_one({
        "id": tid, "tenant_id": tenant, "project_id": pid, "activity_code": code,
        "description": "Framing " + code, "planned_start": "2026-11-02",
        "planned_finish": "2026-11-30", "pct_complete": pct,
        "status": status or ("in_progress" if pct else "not_started"),
    })
    return tid


async def _report(db, tenant, pid, day="2026-11-16", status="draft"):
    rid = "dr-" + uuid.uuid4().hex[:8]
    await db.daily_reports.insert_one({"id": rid, "tenant_id": tenant, "project_id": pid,
                                       "report_date": day, "status": status,
                                       "created_at": "2026-11-16T00:00:00+00:00"})
    return rid


async def _add(ctx, dr_id, tid):
    return await drr.add_grid_row(dr_id, drr.AddActivityRow(schedule_activity_id=tid), ctx)


async def _save(ctx, row_id, **fields):
    return await drr.update_grid_row(row_id, DailyReportActivityUpdate(**fields), ctx)


async def _crew(ctx, dr_id, tid, hc, hrs):
    return await drr.create_presence(dr_id, DailyPresenceCreate(
        daily_report_id=dr_id, trade="Carpentry", company_name="Metro Framing",
        headcount=hc, hours_per_person=hrs, schedule_activity_id=tid), ctx)


async def _task(db, tenant, tid):
    return await db.tasks.find_one({"tenant_id": tenant, "id": tid}, {"_id": 0})


async def _row(db, row_id):
    return await db.daily_report_activities.find_one({"id": row_id}, {"_id": 0})


# ------------------------------------------------------------ (5) unique codes
def test_unplanned_activities_get_unique_fw_codes():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        # Schedule codes that sort AFTER "AF..." reproduced the old bug.
        pid, _ = await _setup(db, tenant, code="ZZ2310")
        await _task_doc(db, tenant, pid, "B2310")
        dr = await _report(db, tenant, pid)
        codes = []
        for i in range(3):
            res = await drr.create_unplanned_activity(
                dr, UnplannedActivityCreate(description=f"Extra work {i}"), ctx)
            codes.append(res["task"]["activity_code"])
            assert res["grid_row"]["activity_code"] == res["task"]["activity_code"]
        assert codes == ["FW-0001", "FW-0002", "FW-0003"]
        # Another project numbers on its own.
        pid2, _ = await _setup(db, tenant)
        assert await next_field_activity_code(db, tenant, pid2) == "FW-0001"
    _run(body)


def test_fw_codes_continue_after_existing_and_are_unique_under_concurrency():
    async def body(db, tenant):
        pid, _ = await _setup(db, tenant)
        # Old data: an earlier FW code and an AF code are left exactly as is.
        await _task_doc(db, tenant, pid, "FW-0007")
        af = await _task_doc(db, tenant, pid, "AF2311")
        codes = await asyncio.gather(*[next_field_activity_code(db, tenant, pid) for _ in range(12)])
        assert len(set(codes)) == 12
        assert sorted(codes) == [f"FW-{n:04d}" for n in range(8, 20)]
        assert (await _task(db, tenant, af))["activity_code"] == "AF2311"
    _run(body)


def test_copilot_tool_uses_fw_codes_and_not_started():
    async def body(db, tenant):
        from services import copilot_tools as ct

        async def _no_guard(*a, **k):
            return None
        orig = ct._guard
        ct._guard = _no_guard
        try:
            pid, _ = await _setup(db, tenant)
            dr = await _report(db, tenant, pid)
            ctx = {**_ctx(tenant), "daily_report_id": dr}
            r1 = await ct._tool_create_unplanned_activity(ctx, {"description": "Patch slab"})
            r2 = await ct._tool_create_unplanned_activity(ctx, {"description": "Patch wall"})
        finally:
            ct._guard = orig
        assert [r1["created"]["activity_code"], r2["created"]["activity_code"]] == ["FW-0001", "FW-0002"]
        t = await _task(db, tenant, r1["created"]["id"])
        assert t["status"] == "not_started"
    _run(body)


# ------------------------------------------------------------ (7) statuses
def test_new_unplanned_activity_is_not_started():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, _ = await _setup(db, tenant)
        dr = await _report(db, tenant, pid)
        res = await drr.create_unplanned_activity(
            dr, UnplannedActivityCreate(description="Unexpected pipe reroute"), ctx)
        t = await _task(db, tenant, res["task"]["id"])
        assert t["status"] == "not_started"
        assert t["pct_complete"] == 0
        assert res["grid_row"]["status_at_end_of_day"] == "not_started"
    _run(body)


def test_report_save_at_zero_without_actual_start_is_not_started():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        # Stale "in_progress" from before (for example the old field-created
        # default) at 0% with no actual start.
        pid, tid = await _setup(db, tenant, pct=0, status="in_progress")
        dr = await _report(db, tenant, pid)
        row = await _add(ctx, dr, tid)
        await _save(ctx, row.id, pct_complete_end_of_day=0, notes="no work today")
        t = await _task(db, tenant, tid)
        assert t["pct_complete"] == 0 and not t.get("actual_start")
        assert t["status"] == "not_started"

        # Progress made -> in progress with an actual start.
        await _save(ctx, row.id, pct_complete_end_of_day=20)
        t = await _task(db, tenant, tid)
        assert t["status"] == "in_progress" and t["actual_start"] == "2026-11-16"

        # Back to 0 on the same report: no worked day left -> not started.
        await _save(ctx, row.id, pct_complete_end_of_day=0)
        t = await _task(db, tenant, tid)
        assert not t.get("actual_start")
        assert t["status"] == "not_started"

        # 0% but hours logged = worked: stays in progress (it has started).
        await _save(ctx, row.id, hours_worked=6)
        t = await _task(db, tenant, tid)
        assert t["actual_start"] == "2026-11-16"
        assert t["status"] == "in_progress"
    _run(body)


def test_delay_status_saved_as_picked():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant, pct=10)
        impacts = [{"task_id": tid, "projected_impact_days": 2}]

        async def make(**extra):
            return await fer.create_delay(DelayCreate(
                project_id=pid, date="2026-11-16", description="Rain",
                linked_task_ids=[tid], per_task_impacts=impacts, **extra), ctx)

        assert (await make(status="open")).status == "open"
        assert (await make(status="resolved")).status == "resolved"
        assert (await make(status="in_progress")).status == "in_progress"
        # Skipped (no status sent): a delay with impact days defaults to
        # In progress, as before.
        assert (await make()).status == "in_progress"
        saved = await db.delays.find({"tenant_id": tenant}, {"_id": 0, "status": 1}).to_list(None)
        assert sorted(d["status"] for d in saved) == ["in_progress", "in_progress", "open", "resolved"]
    _run(body)


# ------------------------------------------------------------ (6) crew hours
def test_crew_rows_drive_row_hours_and_task_hours():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, tid = await _setup(db, tenant)
        d1 = await _report(db, tenant, pid, "2026-11-13")
        r1 = await _add(ctx, d1, tid)
        await _save(ctx, r1.id, hours_worked=10)   # typed by a person
        d2 = await _report(db, tenant, pid, "2026-11-16")
        r2 = await _add(ctx, d2, tid)

        c1 = await _crew(ctx, d2, tid, 3, 8)       # 24 hours
        assert c1.total_hours == 24
        row = await _row(db, r2.id)
        assert (row["hours_worked"], row["crew_count"], row["hours_from_presence"]) == (24, 3, True)
        t = await _task(db, tenant, tid)
        assert t["actual_hours_to_date"] == 34    # 10 + 24, across both reports
        # Hours logged = worked: actual start is the earliest worked report
        # (the typed 10 hours on 11/13), and a started task reads in progress.
        assert t["actual_start"] == "2026-11-13"
        assert t["status"] == "in_progress"

        c2 = await _crew(ctx, d2, tid, 2, 4)       # +8 hours
        row = await _row(db, r2.id)
        assert (row["hours_worked"], row["crew_count"]) == (32, 5)
        assert (await _task(db, tenant, tid))["actual_hours_to_date"] == 42

        await drr.patch_presence(c1.id, DailyPresenceUpdate(headcount=4), ctx)  # 32 hours
        row = await _row(db, r2.id)
        assert (row["hours_worked"], row["crew_count"]) == (40, 6)
        assert (await _task(db, tenant, tid))["actual_hours_to_date"] == 50

        await drr.delete_presence(c2.id, ctx)
        row = await _row(db, r2.id)
        assert (row["hours_worked"], row["crew_count"]) == (32, 4)
        await drr.delete_presence(c1.id, ctx)
        row = await _row(db, r2.id)
        assert (row["hours_worked"], row["crew_count"], row["hours_from_presence"]) == (0, 0, False)
        t = await _task(db, tenant, tid)
        assert t["actual_hours_to_date"] == 10
        # The typed hours on the older report are untouched.
        assert (await _row(db, r1.id))["hours_worked"] == 10
    _run(body)


def test_crew_moved_between_activities_and_typed_hours_kept():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, ta = await _setup(db, tenant)
        tb = await _task_doc(db, tenant, pid, "B200")
        dr = await _report(db, tenant, pid)
        ra = await _add(ctx, dr, ta)
        rb = await _add(ctx, dr, tb)
        await _save(ctx, rb.id, hours_worked=5)    # typed on B
        c = await _crew(ctx, dr, ta, 2, 8)
        assert (await _row(db, ra.id))["hours_worked"] == 16

        # Crew row moved from A to B: A goes back to 0. B takes the crew
        # count, but the 5 hours a person typed on B are kept (typed hours
        # are never replaced by crew hours).
        await drr.patch_presence(c.id, DailyPresenceUpdate(schedule_activity_id=tb), ctx)
        a, b = await _row(db, ra.id), await _row(db, rb.id)
        assert (a["hours_worked"], a["crew_count"]) == (0, 0)
        assert (b["hours_worked"], b["crew_count"]) == (5, 2)
        assert (await _task(db, tenant, ta))["actual_hours_to_date"] == 0
        assert (await _task(db, tenant, tb))["actual_hours_to_date"] == 5
        assert (await _task(db, tenant, ta))["status"] == "not_started"

        # A person then types hours on B: they stay when the crew row goes.
        await _save(ctx, rb.id, hours_worked=12)
        await drr.delete_presence(c.id, ctx)
        b = await _row(db, rb.id)
        assert b["hours_worked"] == 12 and b["hours_from_presence"] is False
        assert (await _task(db, tenant, tb))["actual_hours_to_date"] == 12
    _run(body)
