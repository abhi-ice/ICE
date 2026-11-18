"""Look-ahead exports (2026-11-18): owner day-grid form + table fixes.

Checked here:
  * task_span (pure): actual start wins, late starts begin on day 1,
    overdue work runs to its forecast, schedule dates (not the forecast)
    decide the finish;
  * the grid gather against the local test MongoDB: Monday start, holiday
    has no X, paperwork goes to Notes (old unstarted paperwork does not),
    unreviewed field notes and finished work are left out, a stretched
    forecast does not drop the next activity, open delays are listed;
  * the table export: "Plan start" is the planned start (it printed the
    forecast start), and predecessors stored as {"code", ...} are read
    (every row used to be flagged at risk);
  * both layouts render to Excel and PDF.

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_la_grid bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_lookahead_grid_export.py -q
"""
import asyncio
import io
import os
import uuid
from datetime import date

import pytest

from services.lookahead_grid_export import monday_of, task_span

_HAS_DB = bool(os.environ.get("MONGO_URL")) and (os.environ.get("DB_NAME") or "").startswith(
    ("test_", "ice_test", "unit_"))
needs_db = pytest.mark.skipif(not _HAS_DB, reason="needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)")

START, END = date(2026, 11, 23), date(2026, 12, 6)


# ---------------------------------------------------------------- pure
def test_monday_of():
    assert monday_of(date(2026, 11, 18)) == date(2026, 11, 16)
    assert monday_of(date(2026, 11, 16)) == date(2026, 11, 16)
    assert monday_of(date(2026, 11, 22)) == date(2026, 11, 16)


def test_task_span_rules():
    # in progress: actual start before the period -> from day 1 to planned finish
    t = {"actual_start": "2026-10-08", "pct_complete": 35, "planned_start": "2026-10-08",
         "planned_finish": "2026-12-02", "projected_finish_date": "2026-12-23"}
    assert task_span(t, START, END) == (START, date(2026, 12, 2))
    # not started, planned start already passed -> starts day 1
    t = {"planned_start": "2026-11-12", "planned_finish": "2026-12-02", "pct_complete": 0}
    assert task_span(t, START, END) == (START, date(2026, 12, 2))
    # planned inside the period
    t = {"planned_start": "2026-12-03", "planned_finish": "2026-12-11"}
    assert task_span(t, START, END) == (date(2026, 12, 3), END)
    # overdue (planned finish before the period) -> runs to the forecast
    t = {"planned_start": "2026-11-02", "planned_finish": "2026-11-10", "projected_finish_date": "2026-11-25"}
    assert task_span(t, START, END) == (START, date(2026, 11, 25))
    # after the period
    assert task_span({"planned_start": "2026-12-10", "planned_finish": "2026-12-20"}, START, END) == (None, None)


# ---------------------------------------------------------------- database
import database  # noqa: E402

COLLS = ["tasks", "projects", "delays", "calendars", "audit_log"]


def _run(coro_fn):
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-lagrid-" + uuid.uuid4().hex[:8]
        db = database.get_db()
        try:
            return await coro_fn(db, tenant)
        finally:
            for c in COLLS:
                await db[c].delete_many({"tenant_id": tenant})
            database._client.close()
            database._client, database._db = None, None
    return asyncio.run(wrapper())


def _task(tenant, pid, code, name, ps, pf, **kw):
    d = {"id": uuid.uuid4().hex, "tenant_id": tenant, "project_id": pid, "activity_code": code,
         "description": name, "planned_start": ps, "planned_finish": pf, "pct_complete": 0,
         "status": "not_started", "work_category": "field_work", "predecessors": []}
    d.update(kw)
    return d


async def _seed(db, tenant):
    pid = "p-" + uuid.uuid4().hex[:8]
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Test Clinic Renovation",
                                  "contract_number": "C-1", "station_id": "P-9", "client_name": "Owner Hospital",
                                  "address_line1": "1 Main St", "city": "Town", "state": "NY", "postal_code": "10000",
                                  "contractor_name": "Our Co", "timezone": "America/New_York"})
    tasks = [
        _task(tenant, pid, "A-1", "Abatement", "2026-10-08", "2026-12-02", pct_complete=35,
              status="in_progress", actual_start="2026-10-08", projected_start_date="2026-11-18",
              projected_finish_date="2026-12-23"),
        _task(tenant, pid, "A-2", "Floor tiles", "2026-11-12", "2026-12-02",
              projected_start_date="2026-11-18", projected_finish_date="2026-12-08",
              predecessors=[{"code": "A-1", "type": "SS", "lag_days": 2}]),
        # forecast pushed past the period, schedule says 12/03: must still show
        _task(tenant, pid, "A-3", "Fireproofing", "2026-12-03", "2026-12-11",
              projected_start_date="2026-12-24", projected_finish_date="2027-01-05",
              predecessors=[{"code": "A-2", "type": "FS", "lag_days": 0}]),
        _task(tenant, pid, "A-4", "Plumbing", "2026-10-27", "2026-10-29", pct_complete=100, status="complete"),
        _task(tenant, pid, "S-1", "Shop drawings", "2026-10-21", "2026-12-16", pct_complete=20,
              status="in_progress", actual_start="2026-10-21", work_category="construction_paperwork"),
        _task(tenant, pid, "S-2", "Old submittal", "2026-05-07", "2026-06-17", work_category="construction_paperwork"),
        _task(tenant, pid, "F-1", "Sketching", "2026-11-02", "2026-11-03", created_in_field=True,
              requires_pm_review=True, work_category=None),
    ]
    await db.tasks.insert_many(tasks)
    await db.delays.insert_one({"id": uuid.uuid4().hex, "tenant_id": tenant, "project_id": pid,
                                "description": "Waiting on owner finish selection", "status": "open",
                                "reported_at": "2026-11-13T12:00:00+00:00"})
    await db.delays.insert_one({"id": uuid.uuid4().hex, "tenant_id": tenant, "project_id": pid,
                                "description": "Old resolved delay", "status": "resolved",
                                "reported_at": "2026-11-01T12:00:00+00:00"})
    return pid


@needs_db
def test_grid_gather_rows_days_notes_and_delays():
    from services.lookahead_grid_export import gather_lookahead_grid

    async def go(db, tenant):
        pid = await _seed(db, tenant)
        g = await gather_lookahead_grid(tenant_id=tenant, project_id=pid, start_date=date(2026, 11, 25))
        assert g["start"] == START and g["end"] == END  # snapped to Monday
        rows = {r["code"]: "".join("X" if m else "." for m in r["marks"]) for r in g["rows"]}
        assert set(rows) == {"A-1", "A-2", "A-3"}
        # Thanksgiving 11/26 (Thursday) and the weekends get no X
        assert rows["A-1"] == "XXX.X..XXX...."
        assert rows["A-2"] == "XXX.X..XXX...."
        assert rows["A-3"] == "..........XX.."
        assert any("Shop drawings" in n and "20%" in n for n in g["notes"])
        assert not any("Old submittal" in n for n in g["notes"])
        assert g["delaying"] == ["Waiting on owner finish selection"]
        assert g["holidays_in_period"] == [date(2026, 11, 26)]
        assert g["contract_number"] == "C-1" and g["project_number"] == "P-9"
    _run(go)


@needs_db
def test_grid_renders_excel_and_pdf():
    from openpyxl import load_workbook
    from services.tasks_exports import render_lookahead_export

    async def go(db, tenant):
        pid = await _seed(db, tenant)
        blob, ct, fn, meta = await render_lookahead_export(
            tenant_id=tenant, project_id=pid, fmt="xlsx", layout="grid", start_date=START)
        assert fn.startswith("two_week_look_ahead_20261123") and meta["count"] == 3
        ws = load_workbook(io.BytesIO(blob)).active
        cells = [c.value for row in ws.iter_rows() for c in row if c.value]
        assert "Period: 11/23/26 to 12/6/26" in cells and "Fireproofing" in cells
        pdf, ct, fn, _ = await render_lookahead_export(
            tenant_id=tenant, project_id=pid, fmt="pdf", layout="grid", start_date=START)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        with pytest.raises(ValueError):
            await render_lookahead_export(tenant_id=tenant, project_id=pid, fmt="xlsx", layout="bogus")
    _run(go)


@needs_db
def test_table_export_plan_start_and_predecessor_risk():
    from services import tasks_exports

    async def go(db, tenant):
        pid = await _seed(db, tenant)
        await db.tasks.insert_many([
            # start-to-start link to abatement, which has started -> satisfied
            _task(tenant, pid, "B-1", "Ceiling grid", "2026-11-20", "2026-11-24",
                  predecessors=[{"code": "A-1", "type": "SS", "lag_days": 0}]),
            # finish-to-start link to unfinished abatement -> at risk
            _task(tenant, pid, "B-2", "Paint", "2026-11-20", "2026-11-24",
                  predecessors=[{"code": "A-1", "type": "FS", "lag_days": 0}]),
        ])
        pay = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=date(2026, 11, 18))
        rows = {r["activity_code"]: r for r in pay["rows"]}
        # "Plan start" is the planned start, never the forecast start
        assert rows["A-2"]["planned_start"] == "2026-11-12"
        assert rows["A-2"]["impacted_start"] == "2026-11-18"
        assert rows["B-1"]["at_risk_flag"] is False
        assert rows["B-2"]["at_risk_flag"] is True
        # started work is not flagged for an open predecessor
        assert rows["S-1"]["at_risk_flag"] is False
        assert pay["project"]["timezone"] == "America/New_York"
    _run(go)


# ---------------------------------------------------------------- review fixes + branding
def test_task_span_overdue_with_stale_forecast_stays():
    # planned finish AND forecast both before the period: still to be done
    t = {"actual_start": "2026-11-02", "pct_complete": 40, "planned_start": "2026-11-02",
         "planned_finish": "2026-11-10", "projected_finish_date": "2026-11-13"}
    assert task_span(t, START, END) == (START, END)
    # started with no finish on record
    assert task_span({"actual_start": "2026-11-02", "pct_complete": 10, "planned_start": "2026-11-02"},
                     START, END) == (START, END)


@needs_db
def test_grid_branding_and_project_calendar():
    from services.lookahead_grid_export import gather_lookahead_grid, render_grid_xlsx
    from openpyxl import load_workbook

    async def go(db, tenant):
        pid = await _seed(db, tenant)
        # a 7-day calendar exists for the project but is NOT its calendar:
        # it must not be used
        await db.calendars.insert_one({"id": "cal-7", "tenant_id": tenant, "project_id": pid,
                                       "name": "Curing", "workweek": [0, 1, 2, 3, 4, 5, 6], "use_federal": False})
        g = await gather_lookahead_grid(tenant_id=tenant, project_id=pid, start_date=START)
        rows = {r["code"]: "".join("X" if m else "." for m in r["marks"]) for r in g["rows"]}
        assert rows["A-1"] == "XXX.X..XXX...."
        assert g["header"][0] == ("Test Clinic Renovation", "title")
        assert ("Prepared by: Our Co", "line") in g["header"] and g["footer"] == ""
        # the project's own calendar, saved by the calendar screen: holidays + 6-day week
        await db.calendars.insert_one({"id": "cal-p", "tenant_id": tenant, "project_id": pid, "name": "Site",
                                       "holidays": ["2026-11-27"], "work_days_per_week": 6})
        await db.projects.update_one({"id": pid}, {"$set": {
            "calendar_id": "cal-p", "owner_rep_name": "Rep Group, LLC", "owner_rep_tagline": "Construction and Management",
            "owner_form_footer": "Rep Group is an Equal Opportunity Employer", "prime_contractor_name": "Prime Co"}})
        g = await gather_lookahead_grid(tenant_id=tenant, project_id=pid, start_date=START)
        rows = {r["code"]: "".join("X" if m else "." for m in r["marks"]) for r in g["rows"]}
        assert rows["A-1"] == "XXX..X.XXX...."  # Thanksgiving + 11/27 off, Saturday worked
        assert g["header"][:2] == [("Rep Group, LLC", "org"), ("Construction and Management", "org_sub")]
        assert ("GC: Prime Co", "line") in g["header"]
        assert not any(t.startswith("Prepared by") for t, _ in g["header"])
        ws = load_workbook(io.BytesIO(render_grid_xlsx(g))).active
        cells = [c.value for row in ws.iter_rows() for c in row if c.value]
        assert "Rep Group, LLC" in cells and "Rep Group is an Equal Opportunity Employer" in cells
    _run(go)


@needs_db
def test_started_work_is_not_flagged_as_starting_soon():
    from services import tasks_exports

    async def go(db, tenant):
        pid = await _seed(db, tenant)
        # started early; planned start 2 days out; FS predecessor unfinished
        await db.tasks.insert_one(_task(tenant, pid, "C-1", "Early start", "2026-11-20", "2026-11-24",
                                        actual_start="2026-11-17", pct_complete=20, status="in_progress",
                                        predecessors=[{"code": "A-1", "type": "FS", "lag_days": 0}]))
        pay = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=date(2026, 11, 18))
        rows = {r["activity_code"]: r for r in pay["rows"]}
        assert rows["C-1"]["at_risk_flag"] is False
    _run(go)


def test_set_text_cell_drops_control_characters():
    from openpyxl import Workbook
    from services.xlsx_safe import set_text_cell
    ws = Workbook().active
    set_text_cell(ws["A1"], "Sketch\x0bing\x0c plan\tok")
    assert ws["A1"].value == "Sketching plan\tok"
