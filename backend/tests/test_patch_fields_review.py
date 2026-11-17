"""Security review follow-ups (2026-11-16).

  * services.patch_fields: the protected-field list shared by the generic
    edit endpoint, the subcontractor / supplier edit and the assistant's
    update_entity tool;
  * PATCH /subcontractors/{id}: cannot move a record to another company or
    write server-owned fields; "$" / dotted names give 400; normal edits save;
  * assistant update_entity: same rules, and a project id outside the
    caller's company is dropped;
  * MS Project worker token check still accepts the right token and
    refuses a wrong one.

Offline: the in-memory fake database from test_storage_crud_hardening.
"""
import asyncio

import pytest
from fastapi import HTTPException

from services import patch_fields
from tests.test_storage_crud_hardening import _DB, TID, OTHER, OWN_KEY, OTHER_KEY

CTX = {"tenant_id": TID, "user": {"id": "u1", "role": "pm"}, "request_id": "r1"}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch):
    from routers import vendors_router
    from services import copilot_tools
    import audit
    d = _DB()
    d.projects.docs.append({"id": "p1", "tenant_id": TID})
    d.projects.docs.append({"id": "p-other", "tenant_id": OTHER})
    d.subcontractors.name = "subcontractors"
    d.suppliers.name = "suppliers"
    for mod in (vendors_router, copilot_tools):
        monkeypatch.setattr(mod, "get_db", lambda: d)

    async def _no_audit(**kw):
        return {}
    monkeypatch.setattr(vendors_router, "write_audit", _no_audit)
    monkeypatch.setattr(audit, "write_audit", _no_audit)
    return d


def test_split_patch_fields_drops_protected_and_reports_bad_names():
    clean, bad = patch_fields.split_patch_fields(
        {"name": "Acme", "tenant_id": OTHER, "storage_path": OTHER_KEY,
         "$set": {"x": 1}, "a.b": 2, "notes": None})
    assert clean == {"name": "Acme", "notes": None}
    assert set(bad) == {"$set", "a.b"}


def test_iter8_uses_the_shared_list():
    from routers import iter8_router
    assert iter8_router._CRUD_PROTECTED_FIELDS is patch_fields.PROTECTED_FIELDS


def test_vendor_patch_cannot_move_record_to_another_company(db):
    from routers import vendors_router
    db.subcontractors.docs.append({"id": "s1", "tenant_id": TID, "name": "Old"})
    out = _run(vendors_router.patch_sub(
        "s1", {"name": "New", "tenant_id": OTHER, "id": "zzz",
               "created_at": "1999-01-01"}, ctx=CTX))
    assert out["name"] == "New"
    assert out["tenant_id"] == TID and out["id"] == "s1"
    sent = db.subcontractors.updates[0]["$set"]
    assert "tenant_id" not in sent and "id" not in sent and "created_at" not in sent


@pytest.mark.parametrize("bad", [{"$where": "1"}, {"a.b": 1}])
def test_vendor_patch_bad_field_name_is_400(db, bad):
    from routers import vendors_router
    db.suppliers.docs.append({"id": "s2", "tenant_id": TID, "name": "Old"})
    with pytest.raises(HTTPException) as ei:
        _run(vendors_router.patch_supplier("s2", bad, ctx=CTX))
    assert ei.value.status_code == 400
    assert db.suppliers.updates == []


def test_vendor_patch_normal_edit_saves(db):
    from routers import vendors_router
    db.suppliers.docs.append({"id": "s3", "tenant_id": TID, "name": "Old"})
    out = _run(vendors_router.patch_supplier(
        "s3", {"name": "Concrete Co", "trades": ["03"], "notes": None}, ctx=CTX))
    assert out["name"] == "Concrete Co" and out["trades"] == ["03"]


def test_assistant_update_cannot_repoint_photo_file(db):
    from services import copilot_tools
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "storage_path": OWN_KEY,
                           "project_id": "p1"})
    out = _run(copilot_tools._tool_update_entity(CTX, {
        "entity_type": "photo", "id": "ph1",
        "fields": {"caption": "east wall", "storage_path": OTHER_KEY,
                   "tenant_id": OTHER, "project_id": "p-other"}}))
    assert "error" not in out, out
    doc = db.photos.docs[0]
    assert doc["caption"] == "east wall"
    assert doc["storage_path"] == OWN_KEY
    assert doc["tenant_id"] == TID and doc["project_id"] == "p1"


def test_assistant_update_bad_field_name_is_an_error_not_a_write(db):
    from services import copilot_tools
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "storage_path": OWN_KEY})
    out = _run(copilot_tools._tool_update_entity(CTX, {
        "entity_type": "photo", "id": "ph1", "fields": {"$set": {"tenant_id": OTHER}}}))
    assert "error" in out
    assert db.photos.updates == []


def test_assistant_update_own_project_move_is_kept(db):
    from services import copilot_tools
    db.projects.docs.append({"id": "p2", "tenant_id": TID})
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "project_id": "p1"})
    out = _run(copilot_tools._tool_update_entity(CTX, {
        "entity_type": "photo", "id": "ph1", "fields": {"project_id": "p2"}}))
    assert "error" not in out
    assert db.photos.docs[0]["project_id"] == "p2"


def test_worker_token_check(monkeypatch):
    from routers import ms_project_internal_router as r
    monkeypatch.setattr(r.runtime_settings, "worker_token", lambda: "s3cret-token")
    assert _run(r.require_worker("Bearer s3cret-token")) == {"worker": True}
    assert _run(r.require_worker("  Bearer s3cret-token ")) == {"worker": True}
    for wrong in ("Bearer s3cret-tokeX", "Bearer ", "", None, "Bearer s3cret-token-longer"):
        with pytest.raises(HTTPException) as ei:
            _run(r.require_worker(wrong))
        assert ei.value.status_code == 401
