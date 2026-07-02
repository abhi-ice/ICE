"""UI runtime settings (2026-07-02) -- precedence + worker-token tests.

Contract under test (services.runtime_settings + services.feature_flags):
  - a flag an admin set in the UI wins over the env var, true OR false
  - a flag never touched in the UI falls back to the env var
  - the UI-generated worker token wins over env MS_PROJECT_WORKER_TOKEN
  - ensure_flag_on turns a flag on only when it isn't already effective

In-memory single-doc Mongo stub; no network.
"""
import asyncio
import os
import sys
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Same trick as test_ms_project_sync: satisfy `from database import get_db`
# without motor installed. Tests below monkeypatch runtime_settings.get_db
# directly, so this stub's get_db is never actually called.
try:
    import database  # noqa: F401
except ModuleNotFoundError:
    _dbmod = types.ModuleType("database")
    _dbmod.get_db = lambda: None
    _dbmod.tenant_filter = lambda tid, q=None: {**(q or {}), "tenant_id": tid}
    sys.modules["database"] = _dbmod

from services import feature_flags, runtime_settings  # noqa: E402


class _Col:
    def __init__(self):
        self.doc = None
        self.writes = 0

    async def find_one(self, q):
        return dict(self.doc) if self.doc else None

    async def update_one(self, q, ops, upsert=False):
        self.writes += 1
        if self.doc is None and upsert:
            self.doc = {"_id": q.get("_id", "global")}
        for k, v in ops.get("$set", {}).items():
            parts = k.split(".")
            tgt = self.doc
            for p in parts[:-1]:
                tgt = tgt.setdefault(p, {})
            tgt[parts[-1]] = v


class _Db:
    def __init__(self):
        self.col = _Col()

    def __getitem__(self, name):
        return self.col


@pytest.fixture()
def db(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(runtime_settings, "get_db", lambda: fake)
    runtime_settings.reset_cache_for_tests()
    yield fake
    runtime_settings.reset_cache_for_tests()


def run(coro):
    return asyncio.run(coro)


def test_untouched_flag_falls_back_to_env(db, monkeypatch):
    monkeypatch.delenv("FF_SLIDER_GATING", raising=False)
    assert feature_flags.slider_gating_enabled() is False
    monkeypatch.setenv("FF_SLIDER_GATING", "1")
    assert feature_flags.slider_gating_enabled() is True


def test_ui_true_wins_with_env_unset(db, monkeypatch):
    monkeypatch.delenv("FF_PROGRESS_CASCADE", raising=False)
    run(runtime_settings.set_flags({"FF_PROGRESS_CASCADE": True}, updated_by="a@b.c"))
    assert feature_flags.progress_cascade_enabled() is True


def test_ui_false_beats_env_true(db, monkeypatch):
    # The UI is authoritative: an admin turning a feature OFF must stick even
    # on a deployment whose .env still says on.
    monkeypatch.setenv("FF_ENTERPRISE_CPM", "1")
    run(runtime_settings.set_flags({"FF_ENTERPRISE_CPM": False}))
    assert feature_flags.enterprise_cpm_enabled() is False
    run(runtime_settings.set_flags({"FF_ENTERPRISE_CPM": True}))
    assert feature_flags.enterprise_cpm_enabled() is True


def test_unknown_flag_keys_rejected_silently(db):
    run(runtime_settings.set_flags({"FF_NOT_A_THING": True}))
    assert db.col.doc is None  # nothing written


def test_ensure_flag_on_noop_when_env_already_on(db, monkeypatch):
    monkeypatch.setenv("FF_SCHEDULE_MODE_2", "true")
    run(runtime_settings.ensure_flag_on("FF_SCHEDULE_MODE_2"))
    assert db.col.doc is None  # already effective -> no write


def test_ensure_flag_on_writes_when_off(db, monkeypatch):
    monkeypatch.delenv("FF_SCHEDULE_MODE_2", raising=False)
    run(runtime_settings.ensure_flag_on("FF_SCHEDULE_MODE_2", updated_by="pm@x.y"))
    assert feature_flags.schedule_mode_2_enabled() is True
    assert db.col.doc["flags"]["FF_SCHEDULE_MODE_2"] is True


def test_worker_token_rotate_and_precedence(db, monkeypatch):
    assert runtime_settings.worker_token() is None
    tok = run(runtime_settings.rotate_worker_token(updated_by="a@b.c"))
    assert isinstance(tok, str) and len(tok) >= 32
    assert runtime_settings.worker_token() == tok
    # rotate invalidates the old token
    tok2 = run(runtime_settings.rotate_worker_token())
    assert tok2 != tok
    assert runtime_settings.worker_token() == tok2
    assert runtime_settings.worker_status()["token_set"] is True


def test_require_worker_accepts_ui_token_over_env(db, monkeypatch):
    from fastapi import HTTPException
    from routers.ms_project_internal_router import require_worker

    monkeypatch.setenv("MS_PROJECT_WORKER_TOKEN", "env-token")
    ui_tok = run(runtime_settings.rotate_worker_token())
    assert run(require_worker(f"Bearer {ui_tok}")) == {"worker": True}
    with pytest.raises(HTTPException) as e:
        run(require_worker("Bearer env-token"))  # env loses once UI token exists
    assert e.value.status_code == 401


def test_require_worker_env_fallback_and_unconfigured(db, monkeypatch):
    from fastapi import HTTPException
    from routers.ms_project_internal_router import require_worker

    monkeypatch.setenv("MS_PROJECT_WORKER_TOKEN", "env-token")
    assert run(require_worker("Bearer env-token")) == {"worker": True}
    monkeypatch.delenv("MS_PROJECT_WORKER_TOKEN", raising=False)
    with pytest.raises(HTTPException) as e:
        run(require_worker("Bearer anything"))
    assert e.value.status_code == 503


def test_touch_worker_heartbeat(db):
    run(runtime_settings.touch_worker("worker-abhi-pc"))
    st = runtime_settings.worker_status()
    assert st["worker_id"] == "worker-abhi-pc"
    assert st["last_seen"]
