"""Day-1 audit (2026-10-25) -- daily-report router fixes.

Covers, without a live MongoDB or server (handlers are called directly
against a small in-memory fake of the motor collections):

  * #49/#51  submit on an APPROVED report -> 409 with a plain message,
             and the report stays approved.
  * #49/#51  submit on an already SUBMITTED report -> harmless no-op
             (same report back, submitted_at not re-stamped, no audit).
  * #49/#51  submit race: an approve that lands between the read and the
             write is never overwritten.
  * #49/#51  every plain lock check (header PATCH, refresh-grid,
             weather-autofill, presence create/patch/delete) blocks an
             approved report as well as a submitted one.
  * #67      duplicate-key race on "open today's report" returns the
             existing report instead of a 500.
  * #4/#34   activity PATCH returns the linked task's FINAL updated_at
             (after the status recompute bumped it) as task_updated_at,
             and a chained second edit using it does not 409 -- while an
             edit with a truly stale stamp still does.
  * #21/#62  weather autofill never returns a 5xx: geocoder crash,
             geocoder unreachable, timeouts, weather fetch crash, and a
             project with no address all give a 4xx with a short message.

Run:  cd backend && python -m pytest tests/test_day1_backend_dr.py -q
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest
import requests as _requests
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from routers import daily_reports_router as drr
from models import (
    DailyReportCreate, DailyReportUpdate, DailyReportActivityUpdate,
    DailyPresenceCreate, DailyPresenceUpdate,
)


# ------------------------------------------------------------------
# Minimal in-memory stand-in for motor collections
# ------------------------------------------------------------------
def _get_path(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _matches(doc, flt):
    for k, cond in (flt or {}).items():
        val = _get_path(doc, k)
        if isinstance(cond, dict) and any(op.startswith("$") for op in cond):
            for op, arg in cond.items():
                if op == "$nin" and val in arg:
                    return False
                if op == "$in" and val not in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
        elif val != cond:
            return False
    return True


def _project(doc, projection):
    out = copy.deepcopy(doc)
    if projection:
        keep = [k for k, v in projection.items() if v and k != "_id"]
        if keep:
            out = {k: out[k] for k in keep if k in out}
    return out


class _Res:
    def __init__(self, matched=0):
        self.matched_count = matched
        self.modified_count = matched
        self.deleted_count = matched


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    async def to_list(self, *a, **kw):
        return self._docs


class FakeColl:
    def __init__(self, docs=None):
        self.docs = [copy.deepcopy(d) for d in (docs or [])]
        self.inserted = []

    async def find_one(self, flt=None, projection=None):
        for d in self.docs:
            if _matches(d, flt):
                return _project(d, projection)
        return None

    def find(self, flt=None, projection=None):
        return _Cursor([_project(d, projection) for d in self.docs if _matches(d, flt)])

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        self.inserted.append(doc)
        return _Res(1)

    def _apply(self, d, update):
        for k, v in (update.get("$set") or {}).items():
            parts = k.split(".")
            cur = d
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = copy.deepcopy(v)
        for k, v in (update.get("$push") or {}).items():
            d.setdefault(k, []).append(copy.deepcopy(v))

    async def update_one(self, flt, update):
        for d in self.docs:
            if _matches(d, flt):
                self._apply(d, update)
                return _Res(1)
        return _Res(0)

    async def update_many(self, flt, update):
        n = 0
        for d in self.docs:
            if _matches(d, flt):
                self._apply(d, update)
                n += 1
        return _Res(n)

    async def delete_one(self, flt):
        for i, d in enumerate(self.docs):
            if _matches(d, flt):
                self.docs.pop(i)
                return _Res(1)
        return _Res(0)

    async def count_documents(self, flt=None, limit=None):
        return sum(1 for d in self.docs if _matches(d, flt))


class FakeDB:
    def __init__(self, **colls):
        self._colls = {k: (v if isinstance(v, FakeColl) else FakeColl(v)) for k, v in colls.items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeColl())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


TID = "t1"
CTX = {"tenant_id": TID, "user": {"id": "u-super", "role": "superintendent"}, "request_id": "req-1"}


def _dr(status="draft", **extra):
    d = {
        "id": "dr1", "tenant_id": TID, "project_id": "p1",
        "report_date": "2026-10-26", "status": status,
        "prepared_by": "u-super", "submitted_by": None, "submitted_at": None,
        "approved_by": None, "approved_at": None,
        "created_at": "2026-10-26T11:00:00+00:00",
    }
    d.update(extra)
    return d


@pytest.fixture
def audit_calls(monkeypatch):
    calls = []

    async def _fake_audit(**kw):
        calls.append(kw)

    monkeypatch.setattr(drr, "write_audit", _fake_audit)
    return calls


def _use_db(monkeypatch, db):
    monkeypatch.setattr(drr, "get_db", lambda: db)
    return db


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------
# #49/#51 -- submit state machine
# ------------------------------------------------------------------
def test_submit_on_approved_report_is_refused_and_stays_approved(monkeypatch, audit_calls):
    approved = _dr("approved", submitted_by="u-super", submitted_at="2026-10-26T20:00:00+00:00",
                   approved_by="u-pm", approved_at="2026-10-26T21:00:00+00:00")
    db = _use_db(monkeypatch, FakeDB(daily_reports=[approved]))
    with pytest.raises(HTTPException) as ei:
        _run(drr.submit_daily_report("dr1", drr.SubmitPayload(), CTX))
    assert ei.value.status_code == 409
    assert ei.value.detail == "This report was already approved and can't be submitted again."
    stored = db.daily_reports.docs[0]
    assert stored["status"] == "approved"
    assert stored["submitted_at"] == "2026-10-26T20:00:00+00:00"
    assert audit_calls == []


def test_submit_on_submitted_report_is_a_harmless_noop(monkeypatch, audit_calls):
    submitted = _dr("submitted", submitted_by="u-super", submitted_at="2026-10-26T20:00:00+00:00")
    db = _use_db(monkeypatch, FakeDB(daily_reports=[submitted], tasks=[
        {"id": "t-x", "tenant_id": TID, "updated_at": "2026-10-26T19:00:00+00:00",
         "last_synced_from_dr": {"daily_report_id": "dr1", "sync_state": "confirmed"}},
    ]))
    out = _run(drr.submit_daily_report("dr1", drr.SubmitPayload(notes="replayed"), CTX))
    assert out.status == "submitted"
    assert out.id == "dr1"
    stored = db.daily_reports.docs[0]
    # Not re-stamped, notes from the replay not written, no side effects.
    assert stored["submitted_at"] == "2026-10-26T20:00:00+00:00"
    assert "notes" not in stored or stored.get("notes") is None
    assert db.tasks.docs[0]["updated_at"] == "2026-10-26T19:00:00+00:00"
    assert audit_calls == []


def test_submit_draft_still_submits(monkeypatch, audit_calls):
    async def _no_lock(**kw):
        return {"locked_count": 0}
    monkeypatch.setattr("services.resource_leveling.lock_capacity_for_dr_submit", _no_lock)
    db = _use_db(monkeypatch, FakeDB(
        daily_reports=[_dr("draft")],
        daily_report_activities=[{"id": "row1", "daily_report_id": "dr1", "sync_state": "provisional"}],
    ))
    out = _run(drr.submit_daily_report("dr1", drr.SubmitPayload(), CTX))
    assert out.status == "submitted"
    assert db.daily_reports.docs[0]["submitted_by"] == "u-super"
    assert db.daily_report_activities.docs[0]["sync_state"] == "confirmed"
    assert any(c.get("op") == "UPDATE" for c in audit_calls)


class _ApproveRacesColl(FakeColl):
    """First read sees a draft; the stored doc is already approved (a PM
    approved between the submit's read and its write)."""

    def __init__(self, docs):
        super().__init__(docs)
        self._first = True

    async def find_one(self, flt=None, projection=None):
        doc = await super().find_one(flt, projection)
        if self._first and doc:
            self._first = False
            doc = dict(doc, status="draft")
        return doc


def test_submit_never_overwrites_an_approve_that_raced_in(monkeypatch, audit_calls):
    coll = _ApproveRacesColl([_dr("approved", approved_by="u-pm")])
    db = _use_db(monkeypatch, FakeDB(
        daily_reports=coll,
        daily_report_activities=[{"id": "row1", "daily_report_id": "dr1"}],
    ))
    with pytest.raises(HTTPException) as ei:
        _run(drr.submit_daily_report("dr1", drr.SubmitPayload(), CTX))
    assert ei.value.status_code == 409
    assert db.daily_reports.docs[0]["status"] == "approved"
    assert audit_calls == []


# ------------------------------------------------------------------
# #49/#51 -- plain lock checks now cover approved too
# ------------------------------------------------------------------
@pytest.mark.parametrize("status", ["submitted", "approved"])
def test_header_patch_blocked_when_locked(monkeypatch, audit_calls, status):
    db = _use_db(monkeypatch, FakeDB(daily_reports=[_dr(status)]))
    with pytest.raises(HTTPException) as ei:
        _run(drr.patch_daily_report("dr1", DailyReportUpdate(notes="changed"), CTX))
    assert ei.value.status_code == 409
    assert status in ei.value.detail
    assert db.daily_reports.docs[0].get("notes") is None


def test_header_patch_draft_still_saves(monkeypatch, audit_calls):
    db = _use_db(monkeypatch, FakeDB(daily_reports=[_dr("draft")]))
    out = _run(drr.patch_daily_report("dr1", DailyReportUpdate(notes="hello"), CTX))
    assert out.notes == "hello"
    assert db.daily_reports.docs[0]["notes"] == "hello"


@pytest.mark.parametrize("status", ["submitted", "approved"])
def test_refresh_grid_blocked_when_locked(monkeypatch, status):
    _use_db(monkeypatch, FakeDB(daily_reports=[_dr(status)]))
    with pytest.raises(HTTPException) as ei:
        _run(drr.refresh_grid("dr1", CTX))
    assert ei.value.status_code == 409


@pytest.mark.parametrize("status", ["submitted", "approved"])
def test_weather_autofill_blocked_when_locked(monkeypatch, status):
    _use_db(monkeypatch, FakeDB(daily_reports=[_dr(status)]))
    with pytest.raises(HTTPException) as ei:
        _run(drr.weather_autofill("dr1", CTX))
    assert ei.value.status_code == 409


@pytest.mark.parametrize("status", ["submitted", "approved"])
def test_presence_create_patch_delete_blocked_when_locked(monkeypatch, audit_calls, status):
    pres = {"id": "pr1", "tenant_id": TID, "daily_report_id": "dr1", "trade": "concrete",
            "headcount": 4, "hours_per_person": 8.0}
    db = _use_db(monkeypatch, FakeDB(daily_reports=[_dr(status)], daily_presence=[pres]))
    with pytest.raises(HTTPException) as e1:
        _run(drr.create_presence("dr1", DailyPresenceCreate(daily_report_id="dr1", trade="steel", headcount=3), CTX))
    with pytest.raises(HTTPException) as e2:
        _run(drr.patch_presence("pr1", DailyPresenceUpdate(headcount=9), CTX))
    with pytest.raises(HTTPException) as e3:
        _run(drr.delete_presence("pr1", CTX))
    assert (e1.value.status_code, e2.value.status_code, e3.value.status_code) == (409, 409, 409)
    assert len(db.daily_presence.docs) == 1
    assert db.daily_presence.docs[0]["headcount"] == 4
    assert audit_calls == []


# ------------------------------------------------------------------
# #67 -- duplicate "open today's report"
# ------------------------------------------------------------------
class _RacingReportsColl(FakeColl):
    """find_one misses the first time (both requests raced past it), then
    insert_one hits the unique index because the winner already inserted."""

    def __init__(self, winner):
        super().__init__([winner])
        self._miss_once = True

    async def find_one(self, flt=None, projection=None):
        if self._miss_once:
            self._miss_once = False
            return None
        return await super().find_one(flt, projection)

    async def insert_one(self, doc):
        raise DuplicateKeyError("E11000 duplicate key error", 11000)


def test_create_duplicate_key_returns_existing_report(monkeypatch, audit_calls):
    winner = _dr("draft", id="dr-winner", report_date="2026-10-25")
    db = _use_db(monkeypatch, FakeDB(
        projects=[{"id": "p1", "tenant_id": TID, "name": "P"}],
        daily_reports=_RacingReportsColl(winner),
    ))
    prepop = {"n": 0}

    async def _no_prepop(*a, **kw):
        prepop["n"] += 1
        return 0
    monkeypatch.setattr(drr, "_prepopulate_grid", _no_prepop)

    payload = DailyReportCreate(project_id="p1", report_date="2026-10-25")
    out = _run(drr.create_or_get_daily_report(payload, CTX))
    assert out.id == "dr-winner"
    assert prepop["n"] == 0, "loser must not pre-populate the grid again"
    assert audit_calls == [], "loser must not write a second INSERT audit row"
    assert len(db.daily_reports.docs) == 1


# ------------------------------------------------------------------
# #4/#34 -- activity PATCH returns the final task stamp
# ------------------------------------------------------------------
@pytest.fixture
def grid_env(monkeypatch, audit_calls):
    # Must be in the past: the handler stamps "now", and the stale check
    # is a plain "server stamp sorts later than the client's" compare.
    t0 = "2020-01-01T12:00:00.000000+00:00"
    db = _use_db(monkeypatch, FakeDB(
        daily_reports=[_dr("draft")],
        daily_report_activities=[{
            "id": "row1", "daily_report_id": "dr1", "schedule_activity_id": "task1",
            "position": 0, "pct_complete_start_of_day": 10.0, "pct_complete_end_of_day": 10.0,
            "crew_count": 0, "hours_worked": 0, "status_at_end_of_day": "in_progress",
        }],
        tasks=[{"id": "task1", "tenant_id": TID, "project_id": "p1", "activity_code": "A100",
                "description": "Pour slab", "pct_complete": 10.0, "status": "in_progress",
                "updated_at": t0}],
    ))

    async def _policy(**kw):
        return ({"id": "dr1", "status": "draft"}, False, None)
    monkeypatch.setattr("services.edit_permissions.enforce_locked_dr_edit_policy", _policy)
    monkeypatch.setattr("services.feature_flags.slider_gating_enabled", lambda: False)
    monkeypatch.setattr("services.feature_flags.progress_recompute_enabled_for", lambda p: False)

    bumps = {"n": 0}

    async def _status_recompute(db_, tenant_id, task_id, **kw):
        # Mirrors services/task_status.compute_and_persist_status when a
        # value moved: it stamps a LATER naive utcnow() updated_at.
        bumps["n"] += 1
        later = (datetime.utcnow() + timedelta(seconds=2)).isoformat()
        await db_.tasks.update_one({"tenant_id": tenant_id, "id": task_id},
                                   {"$set": {"updated_at": later, "computed_status": "on_track"}})
    monkeypatch.setattr("services.task_status.compute_and_persist_status", _status_recompute)
    return {"db": db, "t0": t0, "bumps": bumps}


def test_activity_patch_returns_final_task_stamp(grid_env):
    db = grid_env["db"]
    out = _run(drr.update_grid_row(
        "row1",
        DailyReportActivityUpdate(pct_complete_end_of_day=40, task_last_modified_at=grid_env["t0"]),
        CTX,
    ))
    assert grid_env["bumps"]["n"] == 1
    stored = db.tasks.docs[0]["updated_at"]
    assert out.task_updated_at is not None
    # The returned stamp is the task's CURRENT value (post-recompute),
    # not the earlier last_sync_at the handler wrote first.
    assert out.task_updated_at == datetime.fromisoformat(stored)
    assert out.last_sync_at is not None  # existing field kept
    assert out.task_updated_at > out.last_sync_at.replace(tzinfo=None)


def test_chained_edit_with_returned_stamp_does_not_409(grid_env):
    first = _run(drr.update_grid_row(
        "row1",
        DailyReportActivityUpdate(pct_complete_end_of_day=40, task_last_modified_at=grid_env["t0"]),
        CTX,
    ))
    # Client sends back exactly what it received (JSON round-trip).
    token = first.model_dump(mode="json")["task_updated_at"]
    second = _run(drr.update_grid_row(
        "row1",
        DailyReportActivityUpdate(pct_complete_end_of_day=50, task_last_modified_at=token),
        CTX,
    ))
    assert second.pct_complete_end_of_day == 50
    assert grid_env["db"].tasks.docs[0]["pct_complete"] == 50


def test_stale_stamp_still_409s(grid_env):
    _run(drr.update_grid_row(
        "row1",
        DailyReportActivityUpdate(pct_complete_end_of_day=40, task_last_modified_at=grid_env["t0"]),
        CTX,
    ))
    # Guard intact: replaying the ORIGINAL (now stale) stamp is refused.
    with pytest.raises(HTTPException) as ei:
        _run(drr.update_grid_row(
            "row1",
            DailyReportActivityUpdate(pct_complete_end_of_day=60, task_last_modified_at=grid_env["t0"]),
            CTX,
        ))
    assert ei.value.status_code == 409
    assert grid_env["db"].tasks.docs[0]["pct_complete"] == 40


# ------------------------------------------------------------------
# #21/#62 -- weather autofill never 5xx
# ------------------------------------------------------------------
@pytest.fixture
def wx_env(monkeypatch):
    monkeypatch.setattr(drr, "_wx_coalesce_locks", {})
    monkeypatch.setattr(drr, "_wx_coalesce_locks_mutex", None)
    monkeypatch.setattr(drr, "_wx_outbound_sem", None)
    drr._GEOCODE_CACHE.clear()
    drr._WEATHER_CACHE.clear()
    db = _use_db(monkeypatch, FakeDB(
        daily_reports=[_dr("draft")],
        projects=[{"id": "p1", "tenant_id": TID, "city": "Bronx", "state": "NY"}],
    ))
    yield db
    drr._GEOCODE_CACHE.clear()
    drr._WEATHER_CACHE.clear()


def _assert_soft_fail(coro):
    with pytest.raises(HTTPException) as ei:
        _run(coro)
    assert 400 <= ei.value.status_code < 500, ei.value.status_code
    assert isinstance(ei.value.detail, str) and 0 < len(ei.value.detail) < 200
    assert "geocode" not in ei.value.detail.lower()
    return ei.value


def _raise(exc):
    def _f(*a, **kw):
        raise exc
    return _f


def test_weather_geocoder_unreachable_is_4xx(wx_env, monkeypatch):
    monkeypatch.setattr(drr.requests, "get", _raise(_requests.exceptions.ConnectionError("dns")))
    _assert_soft_fail(drr.weather_autofill("dr1", CTX))
    # An outage is negative-cached briefly (60s), not for 5 minutes.
    (_, expires_at), = drr._GEOCODE_CACHE.values()
    import time
    assert expires_at - time.time() <= 61


def test_weather_geocoder_crash_is_4xx(wx_env, monkeypatch):
    monkeypatch.setattr(drr, "_geocode", _raise(RuntimeError("bug")))
    _assert_soft_fail(drr.weather_autofill("dr1", CTX))


def test_weather_geocoder_timeout_is_4xx(wx_env, monkeypatch):
    monkeypatch.setattr(drr, "_geocode", _raise(asyncio.TimeoutError()))
    _assert_soft_fail(drr.weather_autofill("dr1", CTX))


def test_weather_fetch_unreachable_crash_and_timeout_are_4xx(wx_env, monkeypatch):
    monkeypatch.setattr(drr, "_geocode", lambda *a, **kw: (40.8, -73.9))
    monkeypatch.setattr(drr, "_fetch_weather", lambda *a, **kw: None)
    _assert_soft_fail(drr.weather_autofill("dr1", CTX))
    monkeypatch.setattr(drr, "_fetch_weather", _raise(ValueError("bad provider data")))
    _assert_soft_fail(drr.weather_autofill("dr1", CTX))
    monkeypatch.setattr(drr, "_fetch_weather", _raise(asyncio.TimeoutError()))
    _assert_soft_fail(drr.weather_autofill("dr1", CTX))


def test_weather_project_without_address_is_4xx(wx_env, monkeypatch):
    wx_env.projects.docs[0].pop("city")
    wx_env.projects.docs[0].pop("state")
    err = _assert_soft_fail(drr.weather_autofill("dr1", CTX))
    assert "city or state" in err.detail


def test_weather_success_path_unchanged(wx_env, monkeypatch):
    monkeypatch.setattr(drr, "_geocode", lambda *a, **kw: (40.8, -73.9))
    monkeypatch.setattr(drr, "_fetch_weather", lambda *a, **kw: {
        "weather_temp_high_f": 61.0, "weather_temp_low_f": 48.0,
        "weather_conditions": "overcast", "weather_precipitation_in": 0,
        "weather_wind_mph": 9.0,
    })
    out = _run(drr.weather_autofill("dr1", CTX))
    assert out.weather_conditions == "overcast"
    assert out.weather_fetched_at is not None
