"""A report filed as a no-work day says "No work day — <reason>" in its
heading everywhere it is shown or exported (2026-11-16).

Covers the shared wording (services/no_work_day), the rich PDF (internal and
external), the plain-text export, the weekly rollup and its PDF, the client
template data and instructions, and the calendar reconciliation rows + CSV
(which feed the time impact analysis report).

The PDF renderer is replaced by a stub that keeps the HTML, so the tests
check the page source without needing WeasyPrint. Runs against the local
test MongoDB (DB_NAME must be a throwaway test database; each test uses its
own company id and removes only its own documents).
"""
import asyncio
import os
import uuid
from datetime import date

import pytest

from services.no_work_day import no_work_days_summary, no_work_heading


# ---------------------------------------------------------------- wording
def test_heading_wording():
    assert no_work_heading({"is_no_work_day": True, "no_work_reason": "weather"}) \
        == "No work day — Weather"
    assert no_work_heading({"is_no_work_day": True, "no_work_reason": "holiday",
                            "no_work_note": "Thanksgiving"}) == "No work day — Holiday: Thanksgiving"
    # "Other" uses the typed note as the reason.
    assert no_work_heading({"is_no_work_day": True, "no_work_reason": "other",
                            "no_work_note": "  Building closed\nfor inspection "}) \
        == "No work day — Building closed for inspection"
    assert no_work_heading({"is_no_work_day": True, "no_work_reason": "other"}) \
        == "No work day — Other"
    # No reason stored.
    assert no_work_heading({"is_no_work_day": True}) == "No work day"
    # Not a no-work day.
    assert no_work_heading({"is_no_work_day": False, "no_work_reason": "weather"}) is None
    assert no_work_heading({}) is None
    assert no_work_heading(None) is None


def test_weekly_summary_lists_only_no_work_days_oldest_first():
    out = no_work_days_summary([
        {"id": "b", "report_date": "2026-11-12", "is_no_work_day": True,
         "no_work_reason": "crew_unavailable"},
        {"id": "x", "report_date": "2026-11-11", "is_no_work_day": False},
        {"id": "a", "report_date": "2026-11-10", "is_no_work_day": True,
         "no_work_reason": "weather"},
    ])
    assert out == [
        {"daily_report_id": "a", "report_date": "2026-11-10", "heading": "No work day — Weather"},
        {"daily_report_id": "b", "report_date": "2026-11-12",
         "heading": "No work day — Crew unavailable"},
    ]


def test_client_template_data_and_instructions_carry_heading():
    from services import dr_template_export as dte
    snap = dte._build_dr_snapshot(
        dr={"id": "d1", "report_date": "2026-11-10", "status": "submitted",
            "is_no_work_day": True, "no_work_reason": "weather"},
        project={"name": "P"}, activities=[], presence=[], delays=[], signer_name="S",
    )
    assert snap["no_work_day_heading"] == "No work day — Weather"
    plain = dte._build_dr_snapshot(
        dr={"id": "d2", "report_date": "2026-11-11", "status": "submitted"},
        project={"name": "P"}, activities=[], presence=[], delays=[], signer_name="S",
    )
    assert plain["no_work_day_heading"] is None
    assert "no_work_day_heading" in dte.SYSTEM_PROMPT
    assert "FIRST" in dte.SYSTEM_PROMPT.split("NO WORK DAY", 1)[1]


# ---------------------------------------------------------------- exports
needs_db = pytest.mark.skipif(
    not os.environ.get("MONGO_URL")
    or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")),
    reason="needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)",
)

COLLS = ["projects", "daily_reports", "daily_report_activities", "users",
         "project_calendar_days"]

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_backend_module(name):
    """True when `name` is one of the backend's own modules (database,
    routers.x, services.y, ...), whether loaded for real or as a fake."""
    path = os.path.join(_BACKEND, *name.split("."))
    return name.split(".")[0] != "tests" and (
        os.path.isfile(path + ".py") or os.path.isdir(path))


@pytest.fixture(autouse=True)
def _real_backend_modules():
    """Other test files (test_ms_project_sync.py and friends) swap in a fake
    in-memory `database` module for the rest of the run, and routers imported
    after that keep using it. When that has happened, load fresh copies of
    the backend modules for this test so it talks to the real test MongoDB,
    then put every module back so later tests see what they expect."""
    import sys
    db_mod = sys.modules.get("database")
    if db_mod is None or getattr(db_mod, "__file__", None):
        yield
        return
    saved = dict(sys.modules)
    for name in list(sys.modules):
        if _is_backend_module(name):
            del sys.modules[name]
    try:
        yield
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _run(coro_fn):
    import database

    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-nowork-" + uuid.uuid4().hex[:8]
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
    return {"tenant_id": tenant, "user": {"id": "u-admin", "role": "admin", "full_name": "Ann"},
            "request_id": "req-" + uuid.uuid4().hex[:6]}


async def _seed(db, tenant):
    pid = "p-" + uuid.uuid4().hex[:8]
    await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "No-work test",
                                  "status": "active"})
    base = {"tenant_id": tenant, "project_id": pid, "created_at": "2026-11-10T00:00:00+00:00",
            "weather_conditions": "Heavy snow"}
    await db.daily_reports.insert_many([
        {**base, "id": "nw-" + tenant, "report_date": "2026-11-10", "status": "submitted",
         "is_no_work_day": True, "no_work_reason": "weather", "no_work_note": "Snowstorm"},
        {**base, "id": "ok-" + tenant, "report_date": "2026-11-11", "status": "submitted"},
    ])
    return pid, "nw-" + tenant, "ok-" + tenant


@pytest.fixture
def captured_html(monkeypatch):
    import services.pdf_safety as ps
    pages = []

    async def fake_render(html, **kw):
        pages.append(html)
        return b"%PDF-stub"
    monkeypatch.setattr(ps, "arender_pdf", fake_render)
    return pages


@needs_db
@pytest.mark.parametrize("mode", ["internal", "external"])
def test_rich_pdf_heading(captured_html, mode):
    from routers import iter6_router

    async def body(db, tenant):
        pid, nw, ok = await _seed(db, tenant)
        await iter6_router.export_daily_report_pdf(nw, mode=mode, ctx=_ctx(tenant))
        await iter6_router.export_daily_report_pdf(ok, mode=mode, ctx=_ctx(tenant))
    _run(body)
    nw_html, ok_html = captured_html
    assert "<div class='no-work-heading'>No work day — Weather: Snowstorm</div>" in nw_html
    # Heading sits right under the title; weather is still printed.
    assert nw_html.index("no-work-heading'>") < nw_html.index("<table class='header-meta'>")
    assert "Heavy snow" in nw_html
    assert "No work day" not in ok_html


@needs_db
def test_plain_text_export_heading():
    from routers import daily_reports_router as drr

    async def body(db, tenant):
        pid, nw, ok = await _seed(db, tenant)
        a = await drr.export_daily_pdf(nw, ctx=_ctx(tenant))
        b = await drr.export_daily_pdf(ok, ctx=_ctx(tenant))
        return a.body.decode("utf-8"), b.body.decode("utf-8")
    nw_text, ok_text = _run(body)
    lines = nw_text.splitlines()
    assert lines[1] == "No work day — Weather: Snowstorm"
    assert "Weather: Heavy snow" in nw_text
    assert "No work day" not in ok_text


@needs_db
def test_weekly_rollup_lists_no_work_days(captured_html):
    from routers import daily_reports_router as drr
    from routers import iter6_router

    async def body(db, tenant):
        pid, nw, ok = await _seed(db, tenant)
        roll = await drr.weekly_rollup(pid, date(2026, 11, 9), ctx=_ctx(tenant))
        await iter6_router.weekly_rollup_pdf(pid, date(2026, 11, 9), ctx=_ctx(tenant))
        return roll, nw
    roll, nw = _run(body)
    assert roll["daily_report_count"] == 2
    assert roll["no_work_days"] == [{"daily_report_id": nw, "report_date": "2026-11-10",
                                     "heading": "No work day — Weather: Snowstorm"}]
    html = captured_html[0]
    assert "<h2>No work days</h2>" in html
    assert "11/10/2026" in html and "No work day — Weather: Snowstorm" in html


@needs_db
def test_weekly_rollup_pdf_without_no_work_days_has_no_section(captured_html):
    from routers import iter6_router

    async def body(db, tenant):
        pid, nw, ok = await _seed(db, tenant)
        await db.daily_reports.delete_one({"id": nw})
        await iter6_router.weekly_rollup_pdf(pid, date(2026, 11, 9), ctx=_ctx(tenant))
    _run(body)
    assert "No work days" not in captured_html[0]


@needs_db
def test_calendar_reconciliation_rows_and_csv():
    from routers import calendar_reconciliation_router as crr

    async def body(db, tenant):
        pid, nw, ok = await _seed(db, tenant)
        rows = await crr._build_rows(tenant, pid, date(2026, 11, 10), date(2026, 11, 11))
        csv_resp = await crr.calendar_reconciliation_csv(
            pid, date_range_start="2026-11-10", date_range_end="2026-11-11", ctx=_ctx(tenant))
        return rows, csv_resp.body.decode("utf-8")
    rows, csv_text = _run(body)
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-11-10"]["no_work_heading"] == "No work day — Weather: Snowstorm"
    assert by_date["2026-11-11"]["no_work_heading"] is None
    lines = csv_text.splitlines()
    assert lines[0].endswith(",no_work_day")
    assert lines[1].endswith(",No work day — Weather: Snowstorm")
    assert lines[2].endswith(",submitted,")


def test_time_impact_report_uses_heading_column():
    import inspect
    from services import tia_generator
    src = inspect.getsource(tia_generator)
    assert '("no_work_heading", "No-work day")' in src


# ------------------------------------------------ work added after submit
@needs_db
def test_work_added_after_submit_clears_the_no_work_heading(captured_html):
    """A report filed as a no-work day that later gets an activity row (an
    admin adds it after submit) is no longer a no-work day: the flag, reason
    and note are cleared (with an audit row) and the client PDF no longer
    says "No work day"."""
    from routers import daily_reports_router as drr
    from routers import iter6_router

    async def body(db, tenant):
        pid, nw, ok = await _seed(db, tenant)
        tid = "t-" + uuid.uuid4().hex[:8]
        await db.tasks.insert_one({"id": tid, "tenant_id": tenant, "project_id": pid,
                                   "activity_code": "A100", "description": "Footings",
                                   "status": "in_progress"})
        try:
            await drr.add_grid_row(nw, drr.AddActivityRow(schedule_activity_id=tid),
                                   ctx=_ctx(tenant))
            doc = await db.daily_reports.find_one({"id": nw}, {"_id": 0})
            audit = await db.audit_log.find_one(
                {"tenant_id": tenant, "record_id": nw, "op": "NO_WORK_DAY_CLEARED"}, {"_id": 0})
            await iter6_router.export_daily_report_pdf(nw, mode="external", ctx=_ctx(tenant))
            return doc, audit
        finally:
            await db.tasks.delete_many({"tenant_id": tenant})
    doc, audit = _run(body)
    assert doc["is_no_work_day"] is False
    assert "no_work_reason" not in doc and "no_work_note" not in doc
    assert audit and audit["before_data"]["no_work_reason"] == "weather"
    assert "No work day" not in captured_html[0]


@needs_db
def test_no_work_day_without_added_work_keeps_the_heading():
    from services.no_work_day import clear_no_work_day_if_work_added

    async def body(db, tenant):
        pid, nw, ok = await _seed(db, tenant)
        changed = await clear_no_work_day_if_work_added(
            db, tenant_id=tenant, daily_report_id=nw, actor_id="u-admin")
        doc = await db.daily_reports.find_one({"id": nw}, {"_id": 0})
        return changed, doc
    changed, doc = _run(body)
    assert changed is False and doc["is_no_work_day"] is True
