"""Unplanned work is not part of the schedule (2026-11-18).

Work added from a daily report (created_in_field) lives only on daily
reports. Checked against the local test MongoDB:
  * master schedule list hides it unless include_unplanned=true (the daily
    report screens ask for it);
  * look-ahead page, its table export and the 2-week grid leave it out,
    even when it is overdue;
  * the critical-path engine ignores it;
  * a schedule re-import neither lists it as dropped nor deletes it;
  * the assistant's schedule analysis does not count it.

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_unplanned bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_unplanned_off_schedule.py -q
"""
import asyncio
import os
import uuid
from datetime import date

import pytest

_HAS_DB = bool(os.environ.get("MONGO_URL")) and (os.environ.get("DB_NAME") or "").startswith(
    ("test_", "ice_test", "unit_"))
pytestmark = pytest.mark.skipif(not _HAS_DB, reason="needs the local test MongoDB")

# Some older test files put a stand-in "database" module in sys.modules (and
# bind it into services they import). Load the real one from its file under
# its own name, and point every module these tests exercise at it, so the
# result does not depend on test order.
import importlib.util  # noqa: E402
import pathlib  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ice_real_database", pathlib.Path(__file__).resolve().parents[1] / "database.py")
database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(database)


@pytest.fixture(autouse=True)
def _real_db(monkeypatch):
    import importlib
    for name in ("routers.tasks_router", "services.tasks_exports", "services.lookahead_grid_export",
                 "services.cpm", "services.schedule_filters"):
        mod = importlib.import_module(name)
        if hasattr(mod, "get_db"):
            monkeypatch.setattr(mod, "get_db", database.get_db)
        if hasattr(mod, "tenant_filter"):
            monkeypatch.setattr(mod, "tenant_filter", database.tenant_filter)
    yield


def _ctx(tenant):
    return {"tenant_id": tenant, "user": {"id": "u-" + tenant[:6], "role": "admin", "email": "a@example.com",
                                          "full_name": "Admin"}, "request_id": "req-test"}


def _task(tenant, pid, code, name, ps, pf, **kw):
    d = {"id": uuid.uuid4().hex, "tenant_id": tenant, "project_id": pid, "activity_code": code,
         "description": name, "planned_start": ps, "planned_finish": pf, "duration_days": 3,
         "pct_complete": 0, "status": "not_started", "work_category": "field_work", "predecessors": [],
         "created_at": "2026-11-01T00:00:00+00:00"}
    d.update(kw)
    return d


def _run(fn):
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-unpl-" + uuid.uuid4().hex[:8]
        db = database.get_db()
        pid = "p-" + uuid.uuid4().hex[:8]
        await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "Clinic",
                                      "timezone": "America/New_York"})
        await db.tasks.insert_many([
            _task(tenant, pid, "S-1", "Floor tiles", "2026-11-23", "2026-12-02"),
            # unplanned: overdue AND in the window, the worst case
            _task(tenant, pid, "AF2311", "Sketching", "2026-11-02", "2026-11-03",
                  created_in_field=True, requires_pm_review=True, lookahead_status="in_window"),
            _task(tenant, pid, "FW-0001", "Cut up duct", "2026-11-24", "2026-11-25",
                  created_in_field=True, requires_pm_review=True),
        ])
        try:
            return await fn(db, tenant, pid)
        finally:
            for c in ("tasks", "projects", "audit_log", "audit_chain_heads", "schedule_proposals"):
                await db[c].delete_many({"tenant_id": tenant})
            database._client.close()
            database._client, database._db = None, None
    return asyncio.run(wrapper())


def _codes(rows):
    return {(r.get("activity_code") if isinstance(r, dict) else r.activity_code) for r in rows}


def test_schedule_list_and_lookahead_leave_out_unplanned(monkeypatch):
    from routers import tasks_router as tr
    from services import tasks_exports
    from services.lookahead_grid_export import gather_lookahead_grid
    import services.site_time as st
    monkeypatch.setattr(tr, "project_today", lambda *a, **k: date(2026, 11, 18), raising=False)

    async def go(db, tenant, pid):
        rows = await tr.list_tasks(pid, ctx=_ctx(tenant), include_unplanned=False)
        assert _codes(rows) == {"S-1"}
        rows = await tr.list_tasks(pid, ctx=_ctx(tenant), include_unplanned=True)
        assert _codes(rows) == {"S-1", "AF2311", "FW-0001"}
        la = await tr.lookahead(pid, days=14, start=date(2026, 11, 18), ctx=_ctx(tenant))
        assert _codes(la) == {"S-1"}
        pay = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=date(2026, 11, 18))
        assert _codes(pay["rows"]) == {"S-1"}
        g = await gather_lookahead_grid(tenant_id=tenant, project_id=pid, start_date=date(2026, 11, 23))
        assert {r["code"] for r in g["rows"]} == {"S-1"}
    _run(go)


def test_cpm_ignores_unplanned():
    from services.cpm import compute_cpm

    async def go(db, tenant, pid):
        await compute_cpm(tenant, pid)
        for code in ("AF2311", "FW-0001"):
            t = await db.tasks.find_one({"tenant_id": tenant, "activity_code": code})
            assert not t.get("is_critical_path") and t.get("total_float_days") is None
    _run(go)


def test_reimport_keeps_unplanned_and_does_not_list_it_dropped():
    from services.schedule_diff import compute_schedule_diff
    from routers import schedule_proposals_router as spr  # noqa: F401  (import check)

    async def go(db, tenant, pid):
        current = await db.tasks.find(
            {"tenant_id": tenant, "project_id": pid, "status": {"$ne": "deleted"},
             "created_in_field": {"$ne": True}}, {"_id": 0}).to_list(100)
        diff = compute_schedule_diff(current, [{"activity_code": "S-1", "description": "Floor tiles",
                                                "planned_start": "2026-11-23", "planned_finish": "2026-12-02"}])
        assert diff["dropped"] == []
        # the approve path's soft-delete filter skips unplanned work
        import inspect
        src = inspect.getsource(spr.approve_proposal)
        assert '"created_in_field": {"$ne": True}' in src
    _run(go)


def test_assistant_schedule_analysis_skips_unplanned():
    from services import copilot_tools as ct
    import inspect
    src = inspect.getsource(ct._tool_get_schedule_analysis)
    assert '"created_in_field": {"$ne": True}' in src


# ---------------------------------------------------------------- ongoing overhead
def test_overhead_rule_by_dates_only():
    from services.schedule_filters import overhead_ids_from
    tasks = [
        {"id": "gc", "planned_start": "2026-05-06", "planned_finish": "2027-12-28"},   # whole job
        {"id": "sub", "planned_start": "2026-03-10", "planned_finish": "2026-05-01"},  # early submittal
        {"id": "end", "planned_start": "2027-12-28", "planned_finish": "2027-12-28"},  # milestone
        {"id": "ahu", "planned_start": "2026-10-30", "planned_finish": "2027-03-04"},  # long, not whole job
    ]
    assert overhead_ids_from(tasks) == {"gc"}
    # a short project: nothing is overhead below the minimum length
    assert overhead_ids_from([{"id": "a", "planned_start": "2026-01-01", "planned_finish": "2026-01-20"},
                              {"id": "b", "planned_start": "2026-01-05", "planned_finish": "2026-01-10"}]) == set()
    assert overhead_ids_from([]) == set()


def test_lookahead_and_exports_leave_out_overhead():
    from routers import tasks_router as tr
    from services import tasks_exports
    from services.lookahead_grid_export import gather_lookahead_grid

    async def go(db, tenant, pid):
        await db.tasks.insert_many([
            _task(tenant, pid, "GC", "General Conditions", "2026-05-06", "2027-12-28", pct_complete=30,
                  status="in_progress", actual_start="2026-05-06"),
            _task(tenant, pid, "PM", "Project Management", "2026-05-06", "2027-12-28",
                  work_category="preconstruction_paperwork", pct_complete=30, status="in_progress",
                  actual_start="2026-05-06"),
            _task(tenant, pid, "SD", "Shop drawings", "2026-10-21", "2026-12-16", pct_complete=20,
                  status="in_progress", actual_start="2026-10-21", work_category="construction_paperwork"),
        ])
        la = await tr.lookahead(pid, days=14, start=date(2026, 11, 18), ctx=_ctx(tenant))
        assert _codes(la) == {"S-1", "SD"}
        pay = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=date(2026, 11, 18))
        # field work in the table; paperwork in its own section
        assert _codes(pay["rows"]) == {"S-1"}
        assert _codes(pay["paperwork_rows"]) == {"SD"}
        assert pay["count"] == 1 and pay["paperwork_count"] == 1
        for blob in (tasks_exports.render_lookahead_xlsx(pay), tasks_exports.render_lookahead_pdf(pay)):
            assert blob[:4] in (b"PK\x03\x04", b"%PDF") or b"Paperwork in this window" in blob
        import io
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(tasks_exports.render_lookahead_xlsx(pay))).active
        vals = [c for r in ws.iter_rows(values_only=True) for c in r if c]
        assert any("Paperwork in this window" in str(v) for v in vals)
        assert vals.index("SD") > vals.index("S-1")
        g = await gather_lookahead_grid(tenant_id=tenant, project_id=pid, start_date=date(2026, 11, 23))
        assert {r["code"] for r in g["rows"]} == {"S-1"}
        assert not any("General Conditions" in n or "Project Management" in n for n in g["notes"])
        assert any("Shop drawings" in n for n in g["notes"])
        # still in the schedule
        rows = await tr.list_tasks(pid, ctx=_ctx(tenant), include_unplanned=False)
        assert {"GC", "PM"} <= _codes(rows)
    _run(go)


# ---------------------------------------------------------------- contract administration
def test_contract_administration_never_in_lookahead():
    from routers import tasks_router as tr
    from services import tasks_exports
    from services.lookahead_grid_export import gather_lookahead_grid
    from services.schedule_work_category import WORK_CATEGORIES
    from services.dr_activity_grid import section_for, SECTION_PAPERWORK
    assert "contract_administration" in WORK_CATEGORIES
    assert section_for("contract_administration") == SECTION_PAPERWORK

    async def go(db, tenant, pid):
        # not done yet and right inside the window: must still stay out
        await db.tasks.insert_many([
            _task(tenant, pid, "BI", "Bonds & Insurance", "2026-11-24", "2026-11-25",
                  work_category="contract_administration"),
            _task(tenant, pid, "NTP", "Notice to Proceed", "2026-11-23", "2026-11-23",
                  work_category="contract_administration", is_milestone=True),
            # overdue contract admin must not be pulled in either
            _task(tenant, pid, "SOV", "Schedule of Values", "2026-10-01", "2026-10-05",
                  work_category="contract_administration"),
        ])
        la = await tr.lookahead(pid, days=14, start=date(2026, 11, 18), ctx=_ctx(tenant))
        assert _codes(la) == {"S-1"}
        pay = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=date(2026, 11, 18))
        assert _codes(pay["rows"]) == {"S-1"}
        g = await gather_lookahead_grid(tenant_id=tenant, project_id=pid, start_date=date(2026, 11, 23))
        assert {r["code"] for r in g["rows"]} == {"S-1"}
        assert not any("Bonds" in n or "Notice" in n or "Schedule of Values" in n for n in g["notes"])
        rows = await tr.list_tasks(pid, ctx=_ctx(tenant), include_unplanned=False)
        assert {"BI", "NTP", "SOV"} <= _codes(rows)   # still in the schedule
    _run(go)
