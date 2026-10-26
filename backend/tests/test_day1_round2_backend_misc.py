"""Day-1 audit, round 2 (2026-10-25) -- backend-misc group.

Covers:
  1. A validator that raises ValueError (future daily report date, custom
     model validator) gives a JSON 422 with the plain "detail" string --
     not a 500 -- and the submitted "input" / docs "url" are not echoed.
  2. Startup index creation keeps going when one index fails, and logs
     the failed index by name (the daily_reports unique index still runs).
  3. Startup warns when PLATFORM_ADMIN_EMAILS is unset.
  4. Photo upload, photo file route and DR template upload await the
     async storage wrappers (storage call runs off the event loop thread).
  5. Rejecting an AI photo suggestion saves the training record (the old
     code used an undefined _gen_id); a failed save is logged.
  6. GET/DELETE /projects/{id}/dr-template on a real project with no
     template gives {"present": False} / already_empty, not a 404.

Tests 4 and 6 use the MongoDB named by MONGO_URL / DB_NAME (a throwaway
unit_ database); they are skipped when it cannot be reached.

Run:  cd backend && python -m pytest tests/test_day1_round2_backend_misc.py -q
"""
import asyncio
import io
import logging
import os
import threading
import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, model_validator
from starlette.datastructures import Headers, UploadFile

import database
import server
from models import DailyReportCreate


# ------------------------------------------------------------------
# 1. Validation handler is JSON-safe
# ------------------------------------------------------------------
class _ImpactIn(BaseModel):
    impact_days: float = 0

    @model_validator(mode="after")
    def _whole_days(self):
        if self.impact_days != int(self.impact_days):
            raise ValueError("Schedule impact must be a whole number of days.")
        return self


def _validation_app():
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, server.validation_exception_handler)

    @app.post("/impact")
    async def impact(payload: _ImpactIn):
        return {"ok": True}

    @app.post("/dr")
    async def dr(payload: DailyReportCreate):
        return {"ok": True}

    return app


def test_validator_value_error_returns_json_422_not_500():
    client = TestClient(_validation_app(), raise_server_exceptions=False)
    r = client.post("/impact", json={"impact_days": 0.5})
    assert r.status_code == 422, r.text
    body = r.json()
    assert isinstance(body["detail"], str)
    assert "whole number of days" in body["detail"]
    for e in body["errors"]:
        assert "input" not in e and "url" not in e
    # ctx.error (the exception object) is now a plain string
    ctx = body["errors"][0].get("ctx") or {}
    if "error" in ctx:
        assert isinstance(ctx["error"], str)


def test_future_report_date_returns_json_422():
    client = TestClient(_validation_app(), raise_server_exceptions=False)
    future = (date.today() + timedelta(days=30)).isoformat()
    r = client.post("/dr", json={"project_id": "p1", "report_date": future})
    assert r.status_code == 422, r.text
    body = r.json()
    assert "report_date" in body["detail"]
    assert "more than 1 day in the future" in body["detail"]
    for e in body["errors"]:
        assert "input" not in e and "url" not in e


def test_plain_missing_field_still_named_in_detail():
    client = TestClient(_validation_app(), raise_server_exceptions=False)
    r = client.post("/dr", json={"report_date": date.today().isoformat()})
    assert r.status_code == 422
    assert "project_id: Field required" in r.json()["detail"]


# ------------------------------------------------------------------
# 2. Index creation resilient to one failure
# ------------------------------------------------------------------
class _FakeColl:
    def __init__(self, name, calls, fail_on):
        self.name = name
        self._calls = calls
        self._fail_on = fail_on

    async def create_index(self, keys, **kwargs):
        self._calls.append((self.name, keys, kwargs))
        if (self.name, str(keys)) in self._fail_on:
            raise RuntimeError("simulated index failure")
        return "ok"


class _FakeDb:
    def __init__(self, fail_on):
        self.calls = []
        self._fail_on = fail_on

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _FakeColl(name, self.calls, self._fail_on)

    __getitem__ = __getattr__


def test_one_failed_index_does_not_skip_the_rest(monkeypatch, caplog):
    fake = _FakeDb(fail_on={("users", "email"), ("tenants", "slug")})
    monkeypatch.setattr(server, "get_db", lambda: fake)
    with caplog.at_level(logging.ERROR, logger="ice_schedule_ops"):
        asyncio.run(server._ensure_indexes())
    dr_keys = [("tenant_id", 1), ("project_id", 1), ("report_date", 1)]
    assert any(
        c[0] == "daily_reports" and c[1] == dr_keys and c[2].get("unique")
        for c in fake.calls
    ), "daily_reports unique index was skipped"
    # the last index in the list still ran
    assert any(c[2].get("name") == "rr_tenant_status_generated" for c in fake.calls)
    text = caplog.text
    assert "users.email_1" in text and "tenants.slug_1" in text
    assert "simulated index failure" in text


def test_failed_index_logged_by_explicit_name(monkeypatch, caplog):
    keys = [("tenant_id", 1), ("phash", 1)]
    fake = _FakeDb(fail_on={("photo_ai_locks", str(keys))})
    monkeypatch.setattr(server, "get_db", lambda: fake)
    with caplog.at_level(logging.ERROR, logger="ice_schedule_ops"):
        asyncio.run(server._ensure_indexes())
    assert "photo_ai_locks.pal_tenant_phash_unique" in caplog.text
    assert any(c[2].get("name") == "pal_locked_at_ttl" for c in fake.calls)


# ------------------------------------------------------------------
# 3. Platform admin warning
# ------------------------------------------------------------------
def test_platform_admin_warning_when_unset(monkeypatch, caplog):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    with caplog.at_level(logging.WARNING, logger="ice_schedule_ops"):
        assert server._warn_if_platform_admins_unset() is True
    assert "set PLATFORM_ADMIN_EMAILS" in caplog.text


def test_no_platform_admin_warning_when_set(monkeypatch, caplog):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "owner@example.com")
    with caplog.at_level(logging.WARNING, logger="ice_schedule_ops"):
        assert server._warn_if_platform_admins_unset() is False
    assert "PLATFORM_ADMIN_EMAILS" not in caplog.text


# ------------------------------------------------------------------
# Real-Mongo helpers (tests 4 and 6)
# ------------------------------------------------------------------
def _mongo_or_skip():
    if not os.environ.get("MONGO_URL") or not os.environ.get("DB_NAME"):
        pytest.skip("MONGO_URL / DB_NAME not set")
    if not os.environ["DB_NAME"].lower().startswith(("unit_", "test_", "ice_test", "smoke")):
        pytest.skip("DB_NAME is not a throwaway test database")
    from pymongo import MongoClient
    try:
        MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=1500).admin.command("ping")
    except Exception:
        pytest.skip("MongoDB not reachable")


def _run_db(coro_fn):
    """Run coro_fn() on a fresh loop with a fresh motor client."""
    async def _wrap():
        database._client = None
        database._db = None
        try:
            return await coro_fn()
        finally:
            if database._client is not None:
                database._client.close()
            database._client = None
            database._db = None
    return asyncio.run(_wrap())


def _ctx(tenant_id, role="admin"):
    return {"tenant_id": tenant_id,
            "user": {"id": "u-" + tenant_id[:6], "role": role, "email": "a@example.com"},
            "request_id": "req-test"}


# ------------------------------------------------------------------
# 4. Photo paths use the async storage wrappers
# ------------------------------------------------------------------
def test_photo_routes_import_only_async_storage():
    from routers import iter3_router as it3
    assert not hasattr(it3, "put_object")
    assert not hasattr(it3, "get_object")
    assert hasattr(it3, "aput_object") and hasattr(it3, "aget_object")


def test_photo_upload_and_file_route_run_storage_off_loop(monkeypatch):
    _mongo_or_skip()
    from routers import iter3_router as it3
    from services import storage

    store = {}
    threads = []
    main_ident = threading.get_ident()

    def fake_put(path, data, ct):
        threads.append(("put", threading.get_ident()))
        store[path] = (data, ct)
        return {"path": path}

    def fake_get(path):
        threads.append(("get", threading.get_ident()))
        return store[path]

    monkeypatch.setattr(storage, "put_object", fake_put)
    monkeypatch.setattr(storage, "get_object", fake_get)

    tenant = "t-" + uuid.uuid4().hex
    pid = "p-" + uuid.uuid4().hex
    body = b"not really a jpeg " + uuid.uuid4().bytes

    async def go():
        db = database.get_db()
        await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "R2"})
        try:
            up = UploadFile(file=io.BytesIO(body), filename="site.jpg",
                            headers=Headers({"content-type": "image/jpeg"}))
            photo = await it3.upload_photo(
                project_id=pid, location_id=None, schedule_activity_id=None,
                daily_report_id=None, caption="c", photo_class="progress",
                drawing_pin_id=None, pin_drawing_id=None, pin_x=None, pin_y=None,
                file=up, ctx=_ctx(tenant),
            )
            resp = await it3.stream_photo(photo.id, ctx=_ctx(tenant))
            return resp
        finally:
            await db.projects.delete_many({"tenant_id": tenant})
            await db.photos.delete_many({"tenant_id": tenant})
            await db.files.delete_many({"tenant_id": tenant})

    resp = _run_db(go)
    assert resp.body == body
    assert [k for k, _ in threads] == ["put", "get"]
    assert all(t != main_ident for _, t in threads), "storage ran on the event loop thread"


def test_dr_template_upload_runs_storage_off_loop(monkeypatch):
    _mongo_or_skip()
    from routers import projects_router as pr
    from services import storage

    threads = []
    main_ident = threading.get_ident()

    def fake_put(path, data, ct):
        threads.append(threading.get_ident())
        return {"path": path}

    monkeypatch.setattr(storage, "put_object", fake_put)
    tenant = "t-" + uuid.uuid4().hex
    pid = "p-" + uuid.uuid4().hex

    async def go():
        db = database.get_db()
        await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "R2"})
        try:
            up = UploadFile(file=io.BytesIO(b"PK\x03\x04fake"), filename="client.xlsx")
            out = await pr.upload_dr_template(pid, file=up, ctx=_ctx(tenant))
            meta = await pr.get_dr_template_meta(pid, ctx=_ctx(tenant))
            return out, meta
        finally:
            await db.projects.delete_many({"tenant_id": tenant})
            await db.audit_log.delete_many({"tenant_id": tenant})

    out, meta = _run_db(go)
    assert out["file_name"] == "client.xlsx"
    assert meta["present"] is True and meta["file_name"] == "client.xlsx"
    assert len(threads) == 1 and threads[0] != main_ident


# ------------------------------------------------------------------
# 6. dr-template on a project with no template
# ------------------------------------------------------------------
def test_dr_template_absent_is_not_404():
    _mongo_or_skip()
    from routers import projects_router as pr

    tenant = "t-" + uuid.uuid4().hex
    pid = "p-" + uuid.uuid4().hex

    async def go():
        db = database.get_db()
        await db.projects.insert_one({"id": pid, "tenant_id": tenant, "name": "No template"})
        try:
            meta = await pr.get_dr_template_meta(pid, ctx=_ctx(tenant))
            deleted = await pr.delete_dr_template(pid, ctx=_ctx(tenant))
            missing = None
            try:
                await pr.get_dr_template_meta("p-does-not-exist", ctx=_ctx(tenant))
            except HTTPException as e:
                missing = e.status_code
            return meta, deleted, missing
        finally:
            await db.projects.delete_many({"tenant_id": tenant})

    meta, deleted, missing = _run_db(go)
    assert meta == {"present": False}
    assert deleted == {"ok": True, "already_empty": True}
    assert missing == 404  # a project that really does not exist is still 404


# ------------------------------------------------------------------
# 5. Photo AI rejection training record
# ------------------------------------------------------------------
class _UpdRes:
    matched_count = 1


class _TrayColl:
    async def find_one(self, *a, **k):
        return {"id": "tray1", "project_id": "p1", "ai_suggested_class": "damage",
                "ai_suggested_caption": "crack", "ai_confidence_score": 0.8,
                "ai_safety_concerns_detected": ["edge"]}

    async def update_one(self, *a, **k):
        return _UpdRes()


class _CalColl:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    async def insert_one(self, doc):
        if self.fail:
            raise RuntimeError("simulated insert failure")
        self.rows.append(doc)


class _PhotoAiDb:
    def __init__(self, fail=False):
        self.photo_tray = _TrayColl()
        self.photo_ai_calibration = _CalColl(fail)


def test_reject_analysis_saves_calibration_record(monkeypatch):
    from services import photo_ai
    fake = _PhotoAiDb()
    monkeypatch.setattr(photo_ai, "get_db", lambda: fake)
    res = asyncio.run(photo_ai.reject_analysis(
        tenant_id="t1", tray_id="tray1",
        reason="This is not damage, it is a shadow.", actor_id="u1",
    ))
    assert res["ok"] is True
    assert len(fake.photo_ai_calibration.rows) == 1
    row = fake.photo_ai_calibration.rows[0]
    assert row["kind"] == "reject" and row["tray_id"] == "tray1"
    assert isinstance(row["id"], str) and len(row["id"]) >= 32
    assert row["original_ai"]["photo_class"] == "damage"


def test_reject_analysis_logs_failed_calibration_but_still_succeeds(monkeypatch, caplog):
    from services import photo_ai
    fake = _PhotoAiDb(fail=True)
    monkeypatch.setattr(photo_ai, "get_db", lambda: fake)
    with caplog.at_level(logging.WARNING, logger="photo_ai"):
        res = asyncio.run(photo_ai.reject_analysis(
            tenant_id="t1", tray_id="tray1",
            reason="This is not damage, it is a shadow.", actor_id="u1",
        ))
    assert res["ok"] is True
    assert "calibration record not saved" in caplog.text
    assert "simulated insert failure" in caplog.text
