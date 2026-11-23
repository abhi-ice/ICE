"""Worker names on crew rows + the sign-in sheet / payroll hours export.

Through the real handlers against the local test MongoDB (DB_NAME must be a
throwaway test_ database; each test uses its own company id and removes only
its own documents):
  * a crew saved with 3 workers keeps those 3 names (trimmed, blanks dropped);
  * changing the names or the worker count saves through the edit handler;
  * old crew rows with no names read back as an empty list;
  * the export has one sign-in line per worker per day ("Name not entered"
    for workers without a name) and weekly hours per worker.
"""
import asyncio
import io
import os
import uuid
from datetime import date

import pytest

if not os.environ.get("MONGO_URL") or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")):
    pytest.skip("needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)", allow_module_level=True)

from openpyxl import load_workbook  # noqa: E402

import database  # noqa: E402
from models import DailyPresenceCreate, DailyPresenceUpdate  # noqa: E402
from routers import daily_reports_router as drr  # noqa: E402
from services.crew_signin_export import MISSING_NAME, worker_lines  # noqa: E402

COLLS = ["projects", "daily_reports", "daily_presence", "audit_log", "tasks",
         "project_access_watch", "crew_capacity", "daily_report_activities"]


def _run(coro_fn):
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-crewnames-" + uuid.uuid4().hex[:8]
        db = database.get_db()
        try:
            return await coro_fn(db, tenant)
        finally:
            for c in COLLS:
                await db[c].delete_many({"tenant_id": tenant})
            database._client.close()
            database._client, database._db = None, None
    return asyncio.run(wrapper())


def _ctx(tenant):
    return {"tenant_id": tenant, "user": {"id": "u-admin", "role": "admin"},
            "request_id": "req-" + uuid.uuid4().hex[:6]}


async def _setup(db, tenant):
    pid = "p-" + uuid.uuid4().hex[:8]
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Names test"})
    reports = {}
    for day in ("2026-11-16", "2026-11-17"):
        rid = "dr-" + uuid.uuid4().hex[:8]
        await db.daily_reports.insert_one({"id": rid, "tenant_id": tenant, "project_id": pid,
                                           "report_date": day, "status": "draft",
                                           "created_at": f"{day}T00:00:00+00:00"})
        reports[day] = rid
    return pid, reports


def test_names_saved_trimmed_and_editable():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        _, reps = await _setup(db, tenant)
        dr = reps["2026-11-16"]
        row = await drr.create_presence(dr, DailyPresenceCreate(
            daily_report_id=dr, trade="Carpentry", company_name="Metro Framing",
            headcount=3, hours_per_person=8,
            worker_names=["  Jose   Diaz ", "Ana Ruiz", "", "Lee Park"]), ctx)
        assert row.worker_names == ["Jose Diaz", "Ana Ruiz", "Lee Park"]
        row = await drr.patch_presence(row.id, DailyPresenceUpdate(
            headcount=2, worker_names=["Jose Diaz", "Ana Ruiz"]), ctx)
        assert row.headcount == 2 and row.worker_names == ["Jose Diaz", "Ana Ruiz"]
        assert row.total_hours == 16
        # A patch that does not mention names leaves them alone.
        row = await drr.patch_presence(row.id, DailyPresenceUpdate(notes="left at 2"), ctx)
        assert row.worker_names == ["Jose Diaz", "Ana Ruiz"]
        # Lowering the count alone drops the extra names.
        row = await drr.patch_presence(row.id, DailyPresenceUpdate(headcount=1), ctx)
        assert row.worker_names == ["Jose Diaz"] and row.total_hours == 8
        # More names than workers on create: cut to the count.
        extra = await drr.create_presence(dr, DailyPresenceCreate(
            daily_report_id=dr, trade="Labor", headcount=2, hours_per_person=8,
            worker_names=["A", "B", "C"]), ctx)
        assert extra.worker_names == ["A", "B"]
        # Odd shapes are tidied, never refused (offline saves must go through).
        for raw, want in (([None, "Bob", 123, {"x": 1}], ["Bob", "123"]),
                          ("Ann Lee, Bob\nCy",["Ann Lee", "Bob", "Cy"]),
                          (42, [])):
            r2 = await drr.create_presence(dr, DailyPresenceCreate(
                daily_report_id=dr, trade="Labor", headcount=5, hours_per_person=8,
                worker_names=raw), ctx)
            assert r2.worker_names == want, (raw, r2.worker_names)
            r2 = await drr.patch_presence(r2.id, DailyPresenceUpdate(worker_names=raw), ctx)
            assert r2.worker_names == want
        # A value that can't be names leaves the saved names alone.
        r3 = await drr.create_presence(dr, DailyPresenceCreate(
            daily_report_id=dr, trade="Labor", headcount=2, hours_per_person=8,
            worker_names=["Kim Ho", "Al Jo"]), ctx)
        for bad in (42, True, {"a": 1}):
            r3 = await drr.patch_presence(r3.id, DailyPresenceUpdate(worker_names=bad), ctx)
            assert r3.worker_names == ["Kim Ho", "Al Jo"]
        # Invisible formatting characters don't count as a name.
        r4 = await drr.create_presence(dr, DailyPresenceCreate(
            daily_report_id=dr, trade="Labor", headcount=2, hours_per_person=8,
            worker_names=["\u200b", "Zoe\u200d Li"]), ctx)
        assert r4.worker_names == ["Zoe Li"]
        # Old rows (no field at all) read back as an empty list.
        await db.daily_presence.update_one({"id": row.id}, {"$unset": {"worker_names": ""}})
        listed = await drr.list_presence(dr, ctx)
        assert listed[0].worker_names == []
    _run(body)


def test_worker_lines_fill_missing_names():
    lines = worker_lines("2026-11-16", {"headcount": 3, "hours_per_person": 8,
                                        "worker_names": ["A One"], "trade": "Labor"})
    assert [ln["name"] for ln in lines] == [
        "A One", f"{MISSING_NAME} (worker 2 of 3)", f"{MISSING_NAME} (worker 3 of 3)"]
    # The worker count decides the lines, so hours match the report.
    lines = worker_lines("2026-11-16", {"headcount": 1, "hours_per_person": 8,
                                        "worker_names": ["A", "B"]})
    assert [ln["name"] for ln in lines] == ["A"]
    # No count: each name is one worker. A huge count is capped.
    assert len(worker_lines("2026-11-16", {"headcount": 0, "worker_names": ["A", "B"]})) == 2
    assert len(worker_lines("2026-11-16", {"headcount": 99999})) == 500


def test_workbook_text_safe_and_unnamed_rows_separate():
    from services.crew_signin_export import build_workbook
    lines = worker_lines("2026-11-23", {"id": "c1", "headcount": 3, "hours_per_person": 8,
                                        "company_name": "=cmd|x", "trade": "Carp",
                                        "worker_names": ["=SUM(A1)"]})
    blob = build_workbook("P", date(2026, 11, 23), date(2026, 11, 23), lines)
    wb = load_workbook(io.BytesIO(blob))
    ws, ps = wb["Sign-in sheet"], wb["Payroll hours"]
    assert ws.cell(row=5, column=4).value == "=SUM(A1)" and ws.cell(row=5, column=4).data_type == "s"
    assert ws.cell(row=5, column=2).data_type == "s"
    pay = [r for r in ps.iter_rows(min_row=5, values_only=True) if r[0] and r[1]]
    # Two unnamed workers stay two rows of 8 hours, never one row of 16.
    assert len(pay) == 3 and all(r[4] == 8 for r in pay)
    assert ps.cell(row=5, column=2).data_type == "s"


def test_titles_text_duplicates_and_zero_count():
    from services.crew_signin_export import build_workbook
    lines = worker_lines("2026-11-23", {"id": "c1", "headcount": 2, "hours_per_person": 8,
                                        "worker_names": ["Bob", "Bob"]})
    assert [ln["name"] for ln in lines] == ["Bob", "Bob (2)"]
    # No worker count: names listed with 0 hours, matching the report total.
    zero = worker_lines("2026-11-23", {"headcount": 0, "hours_per_person": 8, "worker_names": ["A"]})
    assert len(zero) == 1 and zero[0]["hours"] == 0
    blob = build_workbook("=HYPERLINK(\"http://x\",\"y\")", date(2026, 11, 23), date(2026, 11, 23), lines)
    wb = load_workbook(io.BytesIO(blob))
    assert wb["Sign-in sheet"]["A1"].data_type == "s" and wb["Payroll hours"]["A1"].data_type == "s"
    pay = [r for r in wb["Payroll hours"].iter_rows(min_row=5, values_only=True) if r[0] and r[1]]
    assert len(pay) == 2 and all(r[4] == 8 for r in pay)


def test_export_sign_in_and_payroll():
    async def body(db, tenant):
        ctx = _ctx(tenant)
        pid, reps = await _setup(db, tenant)
        for day, names in (("2026-11-16", ["Jose Diaz", "Ana Ruiz", "Lee Park"]),
                           ("2026-11-17", ["Jose Diaz", "Ana Ruiz"])):
            await drr.create_presence(reps[day], DailyPresenceCreate(
                daily_report_id=reps[day], trade="Carpentry", company_name="Metro Framing",
                headcount=3, hours_per_person=8, worker_names=names), ctx)
        resp = await drr.export_crew_sign_in(pid, date(2026, 11, 16), date(2026, 11, 22), ctx)
        wb = load_workbook(io.BytesIO(resp.body))
        ws = wb["Sign-in sheet"]
        rows = [r for r in ws.iter_rows(min_row=5, values_only=True) if r[0]]
        assert len(rows) == 6
        assert [r[3] for r in rows[:3]] == ["Ana Ruiz", "Jose Diaz", "Lee Park"]
        assert rows[-1][3] == f"{MISSING_NAME} (worker 3 of 3)"
        ps = wb["Payroll hours"]
        pay = {r[1]: r for r in ps.iter_rows(min_row=5, values_only=True) if r[0] and r[1]}
        assert pay["Jose Diaz"][4] == 8 and pay["Jose Diaz"][5] == 8   # Mon, Tue
        assert pay["Lee Park"][4] == 8 and pay["Lee Park"][5] is None
        assert any(str(k).startswith(MISSING_NAME) for k in pay)
        assert "Crew sign-in" in resp.headers["content-disposition"]
        # Range checks.
        with pytest.raises(Exception):
            await drr.export_crew_sign_in(pid, date(2026, 11, 22), date(2026, 11, 16), ctx)
    _run(body)
