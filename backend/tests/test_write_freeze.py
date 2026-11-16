"""Write-freeze switch (Railway move, 2026-11-15).

While FF_WRITE_FREEZE is on:
  - POST / PUT / PATCH / DELETE under /api get 503 with the moving message;
  - reads, sign-in and the admin settings save (to turn it off) still work;
  - with the switch off, nothing changes.
The switch is read like the other flags: admin setting first, then env var.
In-memory settings stub; no network, no database.
"""
import os
import sys
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

try:
    import database  # noqa: F401
except ModuleNotFoundError:
    _dbmod = types.ModuleType("database")
    _dbmod.get_db = lambda: None
    _dbmod.tenant_filter = lambda tid, q=None: {**(q or {}), "tenant_id": tid}
    sys.modules["database"] = _dbmod

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from services import runtime_settings, write_freeze  # noqa: E402

MESSAGE = ("ICE is moving to a new server. Your changes are saved on this device "
           "and will send automatically.")


class _Col:
    def __init__(self):
        self.doc = None

    async def find_one(self, q):
        return dict(self.doc) if self.doc else None


class _Db:
    def __init__(self):
        self.col = _Col()

    def __getitem__(self, name):
        return self.col


@pytest.fixture()
def db(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(runtime_settings, "get_db", lambda: fake)
    monkeypatch.delenv("FF_WRITE_FREEZE", raising=False)
    runtime_settings.reset_cache_for_tests()
    write_freeze.reset_for_tests()
    # Re-read the settings on every request so each test sees its own change.
    monkeypatch.setattr(write_freeze, "REFRESH_SECONDS", 0.0)
    yield fake
    runtime_settings.reset_cache_for_tests()
    write_freeze.reset_for_tests()


def _app():
    app = FastAPI()
    api = APIRouter(prefix="/api")
    hits = []

    @api.get("/daily-reports")
    async def list_reports():
        return {"ok": "read"}

    @api.post("/daily-reports")
    async def create_report():
        hits.append("create")
        return {"ok": "created"}

    @api.put("/daily-reports/{rid}")
    async def put_report(rid: str):
        hits.append("put")
        return {"ok": rid}

    @api.patch("/daily-reports/{rid}")
    async def patch_report(rid: str):
        hits.append("patch")
        return {"ok": rid}

    @api.delete("/daily-reports/{rid}")
    async def delete_report(rid: str):
        hits.append("delete")
        return {"ok": rid}

    @api.post("/auth/login")
    async def login():
        hits.append("login")
        return {"ok": "signed-in"}

    @api.post("/auth/register")
    async def register():
        hits.append("register")
        return {"ok": "registered"}

    @api.patch("/admin/runtime-settings")
    async def patch_settings():
        hits.append("settings")
        return {"ok": "saved"}

    app.include_router(api)

    @app.post("/not-api")
    async def outside():
        hits.append("outside")
        return {"ok": "outside"}

    # Same order as server.py: write-freeze first, CORS wraps it.
    app.add_middleware(write_freeze.WriteFreezeMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=["https://app.example.test"],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    return app, hits


def _freeze_via_settings(db, on=True):
    db.col.doc = {"_id": "global", "flags": {"FF_WRITE_FREEZE": on}}


def test_off_by_default_changes_nothing(db):
    app, hits = _app()
    c = TestClient(app)
    assert c.post("/api/daily-reports").status_code == 200
    assert c.delete("/api/daily-reports/1").status_code == 200
    assert hits == ["create", "delete"]


def test_frozen_blocks_every_write_with_message(db):
    _freeze_via_settings(db)
    app, hits = _app()
    c = TestClient(app)
    for method in ("post", "put", "patch", "delete"):
        url = "/api/daily-reports" if method == "post" else "/api/daily-reports/r1"
        r = c.request(method.upper(), url, json={"x": 1})
        assert r.status_code == 503, method
        assert r.json() == {"detail": MESSAGE}
        assert r.headers["retry-after"]
    # Trailing slash does not get around it.
    assert c.post("/api/daily-reports/").status_code == 503
    # Signing up is a write too.
    assert c.post("/api/auth/register").status_code == 503
    assert hits == []


def test_frozen_passes_reads_signin_and_settings(db):
    _freeze_via_settings(db)
    app, hits = _app()
    c = TestClient(app)
    assert c.get("/api/daily-reports").json() == {"ok": "read"}
    assert c.post("/api/auth/login", json={}).json() == {"ok": "signed-in"}
    assert c.patch("/api/admin/runtime-settings", json={}).json() == {"ok": "saved"}
    assert c.post("/not-api").status_code == 200
    assert hits == ["login", "settings", "outside"]


def test_cors_headers_on_503(db):
    _freeze_via_settings(db)
    app, _ = _app()
    c = TestClient(app)
    r = c.post("/api/daily-reports", headers={"Origin": "https://app.example.test"})
    assert r.status_code == 503
    assert r.headers.get("access-control-allow-origin") == "https://app.example.test"
    pre = c.options("/api/daily-reports", headers={
        "Origin": "https://app.example.test", "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 200


def test_settings_value_wins_over_env(db, monkeypatch):
    app, hits = _app()
    c = TestClient(app)
    monkeypatch.setenv("FF_WRITE_FREEZE", "1")
    assert c.post("/api/daily-reports").status_code == 503
    _freeze_via_settings(db, on=False)  # admin turned it off: wins over env
    assert c.post("/api/daily-reports").status_code == 200
    _freeze_via_settings(db, on=True)
    monkeypatch.delenv("FF_WRITE_FREEZE")
    assert c.post("/api/daily-reports").status_code == 503
    db.col.doc = {"_id": "global", "flags": {}}  # never touched -> env (unset) -> off
    assert c.post("/api/daily-reports").status_code == 200


def test_settings_read_failure_keeps_last_value(db, monkeypatch):
    _freeze_via_settings(db)
    app, _ = _app()
    c = TestClient(app)
    assert c.post("/api/daily-reports").status_code == 503

    async def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr(runtime_settings, "refresh_cache", boom)
    assert c.post("/api/daily-reports").status_code == 503


def test_server_registers_middleware_inside_cors():
    src = open(os.path.join(_BACKEND, "server.py"), encoding="utf-8").read()
    i_freeze = src.index("app.add_middleware(WriteFreezeMiddleware)")
    i_cors = src.index("app.add_middleware(\r\n    CORSMiddleware") if "\r\n" in src else \
        src.index("app.add_middleware(\n    CORSMiddleware")
    assert i_freeze < i_cors, "write-freeze must be added before CORS so CORS wraps it"
