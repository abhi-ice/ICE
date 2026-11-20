"""Overdue unfinished work stays visible (2026-11-16).

A task whose finish is before the window start, but which is still not done,
used to disappear from the look-ahead, its export and the daily report
activity picker. "Overdue" is judged on the PLANNED finish plus progress (the
forecast moves every unfinished task to the data date, so projected dates are
never in the past right after a recompute). Checked here:
  * the shared filter helpers (pure, no database);
  * the look-ahead endpoint, export gather and daily report picker against the
    local test MongoDB (DB_NAME must be a throwaway test database; each test
    uses its own company id and removes only its own documents).

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_fix_overdue bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_lookahead_overdue.py -q
"""
import asyncio
import os
import uuid
from datetime import date

import pytest

from services.schedule_filters import is_overdue, overdue_unfinished, window_or_overdue

_HAS_DB = bool(os.environ.get("MONGO_URL")) and (os.environ.get("DB_NAME") or "").startswith(
    ("test_", "ice_test", "unit_"))
needs_db = pytest.mark.skipif(not _HAS_DB, reason="needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)")

WIN_START = "2026-11-16"
WIN_END = "2026-11-30"


# ---------------------------------------------------------------- pure helpers

def test_is_overdue_basic_cases():
    base = {"planned_start": "2026-11-02", "planned_finish": "2026-11-10", "pct_complete": 40,
            "status": "in_progress"}
    assert is_overdue(base, WIN_START) is True
    assert is_overdue({**base, "pct_complete": 100}, WIN_START) is False
    assert is_overdue({**base, "status": "complete"}, WIN_START) is False
    assert is_overdue({**base, "status": "deleted"}, WIN_START) is False
    # Finishing on the window start is in the window, not overdue.
    assert is_overdue({**base, "planned_finish": WIN_START}, WIN_START) is False
    # Planned finish passed: still overdue when forecast / slipped into the
    # window (the forecast always moves unfinished work to the data date).
    assert is_overdue({**base, "projected_finish_date": "2026-11-18"}, WIN_START) is True
    # Missing percent counts as 0; date objects work too.
    assert is_overdue({"planned_finish": date(2026, 11, 10), "status": "not_started"}, WIN_START) is True
    assert is_overdue({"planned_start": "2026-11-02"}, WIN_START) is False


def test_window_or_overdue_shape():
    expr = window_or_overdue(WIN_START, WIN_END)
    assert "$or" in expr and len(expr["$or"]) == 2
    od = overdue_unfinished(WIN_START, WIN_END)["$and"]
    assert {"$lt": ["$planned_finish", WIN_START]} in od


# ---------------------------------------------------------------- database

import database  # noqa: E402

COLLS = ["tasks", "projects", "daily_reports", "daily_report_activities", "audit_log"]


def _run(coro_fn):
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-overdue-" + uuid.uuid4().hex[:8]
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


async def _seed(db, tenant, tz=None):
    pid = "p-" + uuid.uuid4().hex[:8]
    proj = {"id": pid, "tenant_id": tenant, "name": "Overdue test",
            "schedule_engine": "impact_days", "schedule_mode": "mode1"}
    if tz:
        proj["timezone"] = tz
    await db.projects.insert_one(proj)

    def task(code, ps, pf, **extra):
        return {"id": "task-" + code + "-" + uuid.uuid4().hex[:6], "tenant_id": tenant,
                "project_id": pid, "activity_code": code, "description": code,
                "planned_start": ps, "planned_finish": pf, "pct_complete": 0,
                "status": "not_started", "created_at": "2026-11-01T00:00:00+00:00", **extra}
    docs = [
        task("OVERDUE", "2026-11-02", "2026-11-10", pct_complete=40, status="in_progress"),
        task("NOTSTARTED_LATE", "2026-11-03", "2026-11-06"),
        task("DONE_PCT", "2026-11-02", "2026-11-10", pct_complete=100, status="in_progress"),
        task("DONE_STATUS", "2026-11-02", "2026-11-10", status="complete"),
        task("DELETED", "2026-11-02", "2026-11-10", status="deleted"),
        task("IN_WINDOW", "2026-11-18", "2026-11-20"),
        task("FUTURE", "2026-12-15", "2026-12-20"),
        # Planned before the window but slipped into it: in window, and
        # overdue (its planned finish has passed and it is not done).
        task("SLIPPED_IN", "2026-11-02", "2026-11-06",
             projected_start_date="2026-11-12", projected_finish_date="2026-11-17"),
    ]
    await db.tasks.insert_many([dict(d) for d in docs])
    return pid


def _codes(rows, key="activity_code"):
    return [r[key] if isinstance(r, dict) else getattr(r, key) for r in rows]


@needs_db
def test_lookahead_endpoint_includes_and_flags_overdue_first():
    from routers import tasks_router

    async def body(db, tenant):
        pid = await _seed(db, tenant)
        rows = await tasks_router.lookahead(pid, days=14, start=date(2026, 11, 16), ctx=_ctx(tenant))
        codes = _codes(rows)
        assert set(codes) == {"OVERDUE", "NOTSTARTED_LATE", "IN_WINDOW", "SLIPPED_IN"}
        flags = {r.activity_code: r.overdue for r in rows}
        assert flags == {"OVERDUE": True, "NOTSTARTED_LATE": True,
                         "IN_WINDOW": False, "SLIPPED_IN": True}
        # Overdue rows come first.
        assert set(codes[:3]) == {"OVERDUE", "NOTSTARTED_LATE", "SLIPPED_IN"}
        # Only the in-window rows are marked in_window.
        marks = {d["activity_code"]: d.get("lookahead_status") for d in await db.tasks.find(
            {"tenant_id": tenant, "project_id": pid}, {"_id": 0}).to_list(None)}
        assert marks["IN_WINDOW"] == "in_window" and marks["SLIPPED_IN"] == "in_window"
        assert marks["OVERDUE"] != "in_window" and marks["NOTSTARTED_LATE"] != "in_window"
    _run(body)


@needs_db
def test_lookahead_endpoint_uses_project_timezone(monkeypatch):
    from routers import tasks_router
    seen = []

    def fake_today(tz=None):
        seen.append(tz)
        return date(2026, 11, 16)
    monkeypatch.setattr(tasks_router, "project_today", fake_today)

    async def body(db, tenant):
        pid = await _seed(db, tenant, tz="America/Los_Angeles")
        rows = await tasks_router.lookahead(pid, days=14, start=None, ctx=_ctx(tenant))
        assert "OVERDUE" in _codes(rows)
    _run(body)
    assert seen == ["America/Los_Angeles"]


@needs_db
def test_lookahead_export_includes_overdue_column():
    from services import tasks_exports

    async def body(db, tenant):
        pid = await _seed(db, tenant)
        payload = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=date(2026, 11, 16))
        rows = payload["rows"]
        codes = _codes(rows)
        assert set(codes) == {"OVERDUE", "NOTSTARTED_LATE", "IN_WINDOW", "SLIPPED_IN"}
        assert set(codes[:3]) == {"OVERDUE", "NOTSTARTED_LATE", "SLIPPED_IN"}
        by = {r["activity_code"]: r for r in rows}
        assert by["OVERDUE"]["overdue"] is True and by["OVERDUE"]["overdue_label"] == "Overdue"
        assert by["OVERDUE"]["at_risk_flag"] is True
        assert by["IN_WINDOW"]["overdue"] is False and by["IN_WINDOW"]["overdue_label"] == ""
        assert ("overdue_label", "Overdue") in tasks_exports.LOOKAHEAD_COLUMNS
        # The rendered workbook carries the column without errors.
        blob = tasks_exports.render_lookahead_xlsx(payload)
        assert blob[:2] == b"PK"
    _run(body)


@needs_db
def test_daily_report_picker_includes_overdue():
    from routers import daily_reports_router as drr

    async def body(db, tenant):
        pid = await _seed(db, tenant)
        rid = "dr-" + uuid.uuid4().hex[:8]
        await db.daily_reports.insert_one({"id": rid, "tenant_id": tenant, "project_id": pid,
                                           "report_date": "2026-11-18", "status": "draft"})
        out = await drr.list_available_tasks(rid, ctx=_ctx(tenant))
        codes = _codes(out)
        # SLIPPED_IN's planned finish 11/06 is before the 11/18 report.
        assert set(codes) == {"OVERDUE", "NOTSTARTED_LATE", "SLIPPED_IN", "IN_WINDOW"}
        assert codes[-1] == "IN_WINDOW"
        by = {r["activity_code"]: r for r in out}
        assert by["OVERDUE"]["overdue"] is True and by["SLIPPED_IN"]["overdue"] is True
        assert by["IN_WINDOW"]["overdue"] is False
    _run(body)


# ---------------------------------------------------------------- field work only
# Overdue paperwork is not pulled into the lists (it buried the field work on
# real schedules); untagged tasks still count as field work.

def test_overdue_paperwork_is_not_overdue():
    late = {"planned_start": "2026-11-02", "planned_finish": "2026-11-10", "status": "not_started"}
    for cat in ("preconstruction_paperwork", "construction_paperwork", "closeout_paperwork"):
        assert is_overdue({**late, "work_category": cat}, WIN_START) is False
    assert is_overdue({**late, "work_category": "field_work"}, WIN_START) is True
    assert is_overdue({**late, "work_category": None}, WIN_START) is True
    assert is_overdue(late, WIN_START) is True


async def _seed_with_paperwork(db, tenant):
    pid = await _seed(db, tenant)
    base = {"tenant_id": tenant, "project_id": pid, "pct_complete": 0, "status": "not_started",
            "created_at": "2026-11-01T00:00:00+00:00"}
    await db.tasks.insert_many([
        {**base, "id": "task-PW-LATE-" + uuid.uuid4().hex[:6], "activity_code": "PW_LATE",
         "description": "Submit shop drawings", "planned_start": "2026-11-02",
         "planned_finish": "2026-11-06", "work_category": "construction_paperwork"},
        {**base, "id": "task-PW-IN-" + uuid.uuid4().hex[:6], "activity_code": "PW_IN",
         "description": "Approve samples", "planned_start": "2026-11-18",
         "planned_finish": "2026-11-20", "work_category": "construction_paperwork"},
        {**base, "id": "task-FW-LATE-" + uuid.uuid4().hex[:6], "activity_code": "FW_LATE",
         "description": "Frame walls", "planned_start": "2026-11-02",
         "planned_finish": "2026-11-06", "work_category": "field_work"},
    ])
    return pid


@needs_db
def test_lookahead_skips_overdue_paperwork_but_keeps_in_window_paperwork():
    from routers import tasks_router
    from services import tasks_exports

    async def body(db, tenant):
        pid = await _seed_with_paperwork(db, tenant)
        rows = await tasks_router.lookahead(pid, days=14, start=date(2026, 11, 16), ctx=_ctx(tenant))
        codes = set(_codes(rows))
        assert "PW_LATE" not in codes
        assert {"PW_IN", "FW_LATE", "OVERDUE"} <= codes
        payload = await tasks_exports.gather_lookahead_for_export(
            tenant_id=tenant, project_id=pid, days=14, start_date=date(2026, 11, 16))
        ecodes = set(_codes(payload["rows"]))
        pcodes = set(_codes(payload["paperwork_rows"]))
        # in-window paperwork is listed in its own section, never as a field row
        assert "PW_LATE" not in ecodes | pcodes and "FW_LATE" in ecodes
        assert "PW_IN" in pcodes and "PW_IN" not in ecodes
    _run(body)


@needs_db
def test_daily_report_picker_skips_overdue_paperwork():
    from routers import daily_reports_router as drr

    async def body(db, tenant):
        pid = await _seed_with_paperwork(db, tenant)
        rid = "dr-" + uuid.uuid4().hex[:8]
        await db.daily_reports.insert_one({"id": rid, "tenant_id": tenant, "project_id": pid,
                                           "report_date": "2026-11-18", "status": "draft"})
        codes = set(_codes(await drr.list_available_tasks(rid, ctx=_ctx(tenant))))
        assert "PW_LATE" not in codes and "FW_LATE" in codes
    _run(body)


@needs_db
def test_assistant_overdue_view_uses_planned_finish_not_slip(monkeypatch):
    """The assistant's "overdue" list follows the look-ahead rule: a task
    planned for the future with forecast slip is NOT overdue; overdue
    paperwork is left out."""
    from services import copilot_tools

    async def body(db, tenant):
        pid = await _seed_with_paperwork(db, tenant)
        await db.tasks.insert_one({
            "id": "task-FUT-" + uuid.uuid4().hex[:6], "tenant_id": tenant, "project_id": pid,
            "activity_code": "FUTURE_SLIP", "description": "Startup", "planned_start": "2027-11-26",
            "planned_finish": "2027-12-01", "slip_days": 23, "pct_complete": 0, "status": "not_started"})

        async def today(*a, **k):
            return date(2026, 11, 16)
        monkeypatch.setattr(copilot_tools, "project_today_for", today)

        async def no_guard(*a, **k):
            return None
        monkeypatch.setattr(copilot_tools, "_guard", no_guard)
        # Use this test's database connection (other test files can leave an
        # older one behind in copilot_tools).
        monkeypatch.setattr(copilot_tools, "get_db", lambda: db)
        out = await copilot_tools._tool_get_schedule_analysis(
            {"tenant_id": tenant, "project_id": pid, "user": {"id": "u", "role": "admin"}},
            {"view": "overdue"})
        codes = {r["activity_code"] for r in out["rows"]}
        assert "FUTURE_SLIP" not in codes and "PW_LATE" not in codes
        assert {"OVERDUE", "NOTSTARTED_LATE", "FW_LATE"} <= codes
        by = {r["activity_code"]: r for r in out["rows"]}
        assert by["OVERDUE"]["days_past_planned_finish"] == 6  # planned finish 11/10
    _run(body)
