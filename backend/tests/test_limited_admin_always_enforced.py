"""An admin limited to specific projects is held to them even while
FF_PROJECT_ACCESS is off (watch only). Everyone else is unchanged."""
import asyncio

import pytest
from fastapi import HTTPException

from services import project_access as pa


def _ctx(role, scope, allowed):
    c = {"tenant_id": "t1", "role": role, "access_scope": scope,
         "user": {"id": "u1", "role": role, "access_scope": scope}}
    c["_pa_allowed"] = allowed
    return c


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch):
    from services import feature_flags
    monkeypatch.setattr(feature_flags, "project_access_enabled", lambda: False)


def test_who_is_enforced_with_switch_off():
    assert pa.is_enforced(_ctx("admin", "assigned", frozenset())) is True
    assert pa.is_enforced(_ctx("admin", "all", None)) is False
    assert pa.is_enforced(_ctx("super", "assigned", frozenset())) is False
    assert pa.is_enforced() is False


def test_limited_admin_list_filter_applies_with_switch_off():
    q = asyncio.run(pa.project_scope_filter(_ctx("admin", "assigned", frozenset({"p1"})), {}))
    assert q == {"project_id": {"$in": ["p1"]}}
    q = asyncio.run(pa.project_scope_filter(_ctx("super", "assigned", frozenset({"p1"})), {}))
    assert q == {}


def test_limited_admin_refused_on_other_project_with_switch_off(monkeypatch):
    class _Projects:
        async def find_one(self, *a, **k):
            return {"id": "p2"}

    class _Watch:
        async def update_one(self, *a, **k):
            return None

    class _DB:
        projects = _Projects()
        project_access_watch = _Watch()

    monkeypatch.setattr(pa, "get_db", lambda: _DB())

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(pa, "_record_watch", _noop)
    ctx = _ctx("admin", "assigned", frozenset({"p1"}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(pa.check_project_access(ctx, "p2", write=True))
    assert e.value.status_code == 403
    # A supervisor with the switch off is still only watched.
    asyncio.run(pa.check_project_access(_ctx("super", "assigned", frozenset({"p1"})), "p2", write=True))
