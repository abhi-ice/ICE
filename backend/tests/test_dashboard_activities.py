"""Dashboard "Active project" table endpoint (2026-11-16).

GET /projects/{project_id}/dashboard-activities returns at most `limit` rows
in priority order: overdue unfinished work, then work in progress, then
not-started work in the next `days` days. Checked against the local test
MongoDB (DB_NAME must be a throwaway test database; each test uses its own
company id and removes only its own documents).

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_fix_dashboard bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_dashboard_activities.py -q
"""
import asyncio
import os
import uuid
from datetime import date

import pytest

_HAS_DB = bool(os.environ.get("MONGO_URL")) and (os.environ.get("DB_NAME") or "").startswith(
    ("test_", "ice_test", "unit_"))
needs_db = pytest.mark.skipif(not _HAS_DB, reason="needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)")

import database  # noqa: E402

COLLS = ["tasks", "projects", "audit_log"]
TODAY = date(2026, 11, 16)


def _run(coro_fn):
    async def wrapper():
        database._client, database._db = None, None
        tenant = "t-dash-" + uuid.uuid4().hex[:8]
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


@pytest.fixture
def frozen_today(monkeypatch):
    from routers import tasks_router
    seen = []

    def fake_today(tz=None):
        seen.append(tz)
        return TODAY
    monkeypatch.setattr(tasks_router, "project_today", fake_today)
    return seen


async def _seed(db, tenant, docs_spec, tz=None):
    pid = "p-" + uuid.uuid4().hex[:8]
    proj = {"id": pid, "tenant_id": tenant, "name": "Dashboard test",
            "schedule_engine": "impact_days", "schedule_mode": "mode1"}
    if tz:
        proj["timezone"] = tz
    await db.projects.insert_one(proj)
    docs = []
    for seq, (code, ps, pf, extra) in enumerate(docs_spec):
        docs.append({"id": "task-" + code + "-" + uuid.uuid4().hex[:6], "tenant_id": tenant,
                     "project_id": pid, "activity_code": code, "description": code,
                     "sequence": seq, "planned_start": ps, "planned_finish": pf,
                     "pct_complete": 0, "status": "not_started", "actual_start": None,
                     "actual_finish": None, **extra})
    if docs:
        await db.tasks.insert_many(docs)
    return pid


SPEC = [
    # Finished paperwork at the top of the import order: must not appear.
    ("DONE1", "2026-09-01", "2026-09-05", {"pct_complete": 100, "status": "complete",
                                            "actual_start": "2026-09-01", "actual_finish": "2026-09-05"}),
    ("DONE2", "2026-09-01", "2026-09-05", {"pct_complete": 100, "status": "in_progress"}),
    ("DELETED", "2026-11-01", "2026-11-05", {"status": "deleted"}),
    # Overdue: finish before today, not done. Older finish first.
    ("OD_NEW", "2026-11-02", "2026-11-12", {"pct_complete": 40, "status": "in_progress",
                                             "actual_start": "2026-11-02"}),
    ("OD_OLD", "2026-10-20", "2026-10-30", {}),
    # Slipped into the window: not overdue, in progress.
    ("IP_SLIP", "2026-11-02", "2026-11-06", {"projected_start_date": "2026-11-10",
                                              "projected_finish_date": "2026-11-25",
                                              "pct_complete": 20, "status": "in_progress",
                                              "actual_start": "2026-11-10"}),
    # In progress, finishing earlier than IP_SLIP.
    ("IP_EARLY", "2026-11-10", "2026-11-20", {"pct_complete": 50, "status": "in_progress",
                                               "actual_start": "2026-11-10"}),
    # Progress from a sync but no actual start: still counts as in progress.
    ("IP_PCT", "2026-11-12", "2026-11-28", {"pct_complete": 10}),
    # Starting soon (inside 14 days) and a late start (window already open).
    ("SOON_B", "2026-11-25", "2026-12-02", {}),
    ("SOON_A", "2026-11-18", "2026-11-20", {}),
    ("LATE_START", "2026-11-13", "2026-11-19", {}),
    # Outside the 14-day window.
    ("FAR", "2026-12-15", "2026-12-20", {}),
]


@needs_db
def test_priority_order_and_exclusions(frozen_today):
    from routers import tasks_router

    async def body(db, tenant):
        pid = await _seed(db, tenant, SPEC, tz="America/Chicago")
        out = await tasks_router.dashboard_activities(pid, days=14, limit=8, ctx=_ctx(tenant))
        codes = [r["activity_code"] for r in out["items"]]
        # IP_SLIP's planned finish (11/06) has passed: overdue, even though
        # it was forecast into the window. Oldest planned finish first.
        assert codes == ["OD_OLD", "IP_SLIP", "OD_NEW", "IP_EARLY", "IP_PCT",
                         "LATE_START", "SOON_A", "SOON_B"]
        by = {r["activity_code"]: r for r in out["items"]}
        assert by["OD_OLD"]["overdue"] is True and by["OD_OLD"]["group"] == "overdue"
        assert by["IP_SLIP"]["overdue"] is True and by["IP_SLIP"]["group"] == "overdue"
        assert by["IP_EARLY"]["overdue"] is False and by["IP_EARLY"]["group"] == "in_progress"
        assert by["SOON_A"]["group"] == "starting_soon"
        assert by["IP_SLIP"]["projected_finish_date"] == "2026-11-25"
        assert out["window_start"] == "2026-11-16" and out["window_end"] == "2026-11-30"
        # Only the light-weight fields come back (no _id, no sort helper).
        assert "_id" not in by["OD_OLD"] and "_sort_date" not in by["OD_OLD"]
        assert "tenant_id" not in by["OD_OLD"]
    _run(body)
    # The window starts on the project's own site-clock today.
    assert frozen_today == ["America/Chicago"]


@needs_db
def test_limit_fills_by_priority(frozen_today):
    from routers import tasks_router

    async def body(db, tenant):
        pid = await _seed(db, tenant, SPEC)
        out = await tasks_router.dashboard_activities(pid, days=14, limit=3, ctx=_ctx(tenant))
        assert [r["activity_code"] for r in out["items"]] == ["OD_OLD", "IP_SLIP", "OD_NEW"]
        # A shorter window drops the later start.
        out = await tasks_router.dashboard_activities(pid, days=5, limit=20, ctx=_ctx(tenant))
        codes = [r["activity_code"] for r in out["items"]]
        assert "SOON_B" not in codes and "FAR" not in codes and "SOON_A" in codes
        # Each task appears once even though OD_NEW is also "in progress".
        assert len(codes) == len(set(codes))
    _run(body)


@needs_db
def test_other_company_and_empty_project(frozen_today):
    from routers import tasks_router

    async def body(db, tenant):
        pid = await _seed(db, tenant, SPEC)
        # A different company sees nothing for this project.
        out = await tasks_router.dashboard_activities(pid, days=14, limit=8,
                                                      ctx=_ctx(tenant + "-other"))
        assert out["items"] == []
        empty = await _seed(db, tenant, [])
        out = await tasks_router.dashboard_activities(empty, days=14, limit=8, ctx=_ctx(tenant))
        assert out["items"] == []
    _run(body)


@needs_db
def test_read_only_and_route_registered(frozen_today):
    from routers import tasks_router

    async def body(db, tenant):
        pid = await _seed(db, tenant, SPEC)
        before = await db.tasks.find({"tenant_id": tenant}, {"_id": 0}).to_list(100)
        await tasks_router.dashboard_activities(pid, days=14, limit=8, ctx=_ctx(tenant))
        after = await db.tasks.find({"tenant_id": tenant}, {"_id": 0}).to_list(100)
        assert before == after
        assert await db.audit_log.count_documents({"tenant_id": tenant}) == 0
    _run(body)
    paths = {getattr(r, "path", None) for r in tasks_router.router.routes}
    assert "/projects/{project_id}/dashboard-activities" in paths
