"""Photo tray delete + no-AI-key stub captions (Railway move, 2026-11-16).

Covers three fixes:
  1. DELETE /api/photo-tray/{id} refuses (409) when a photo row links to
     the tray entry. Photo rows store that link as `tray_photo_id`.
  2. The same delete removes the stored image (by the tray's
     `storage_key`) through services.storage.remove_object, only when
     nothing else still points at that key, and logs failures.
  3. With no AI key the stub no longer invents a caption, and an old
     "[dev stub] ..." caption is never copied onto a photo row or printed
     in the daily report PDF.

Offline: a small in-memory fake database stands in for Mongo, and the
storage and PDF calls are replaced with fakes.
"""
import asyncio
import logging

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------
# Tiny in-memory Mongo stand-in (only what these routes use)
# ---------------------------------------------------------------------

def _match(doc: dict, flt: dict) -> bool:
    for k, v in (flt or {}).items():
        val = doc.get(k)
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$in" and val not in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
        elif val != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    async def to_list(self, n=None):
        return [dict(d) for d in self._docs[: n or None]]


class _Result:
    def __init__(self, n=0):
        self.deleted_count = n
        self.modified_count = n
        self.matched_count = n


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, flt=None, _proj=None, **_k):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    def find(self, flt=None, _proj=None, **_k):
        return _Cursor([d for d in self.docs if _match(d, flt)])

    async def count_documents(self, flt=None, **_k):
        return sum(1 for d in self.docs if _match(d, flt))

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return _Result(1)

    async def delete_one(self, flt):
        for i, d in enumerate(self.docs):
            if _match(d, flt):
                del self.docs[i]
                return _Result(1)
        return _Result(0)

    async def update_one(self, flt, upd, **_k):
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set") or {})
                return _Result(1)
        return _Result(0)

    async def update_many(self, flt, upd, **_k):
        n = 0
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set") or {})
                n += 1
        return _Result(n)


class FakeDB:
    def __init__(self):
        self._cols: dict[str, FakeCollection] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._cols.setdefault(name, FakeCollection())


TENANT = "t1"
CTX = {"tenant_id": TENANT, "user": {"id": "u1", "role": "admin", "full_name": "Pat"},
       "request_id": "r1"}


@pytest.fixture
def router_env(monkeypatch):
    """photo_tray_router wired to a fake db, fake audit and fake storage."""
    import routers.photo_tray_router as ptr
    import services.storage as storage

    db = FakeDB()
    removed: list[str] = []

    async def _no_audit(**_k):
        return None

    def _fake_remove(path):
        removed.append(path)
        return "deleted"

    monkeypatch.setattr(ptr, "get_db", lambda: db)
    monkeypatch.setattr(ptr, "write_audit", _no_audit)
    monkeypatch.setattr(storage, "remove_object", _fake_remove)
    return ptr, db, removed


def _tray(**over):
    doc = {"id": "tray1", "tenant_id": TENANT, "project_id": "p1",
           "status": "untagged", "storage_key": "ice/tray/t1/abc.jpg",
           "filename": "a.jpg", "uploaded_at": "2026-11-16T10:00:00+00:00",
           "uploaded_by": "u1"}
    doc.update(over)
    return doc


# ---------------------------------------------------------------------
# 1. Tagged guard
# ---------------------------------------------------------------------

def test_delete_refused_when_photo_row_links_by_tray_photo_id(router_env):
    ptr, db, removed = router_env
    db.photo_tray.docs.append(_tray())
    db.photos.docs.append({"id": "ph1", "tenant_id": TENANT,
                           "tray_photo_id": "tray1",
                           "storage_path": "ice/tray/t1/abc.jpg"})
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert ei.value.status_code == 409
    assert "already tagged to 1 event" in ei.value.detail
    # Nothing deleted: tray row and stored image both kept.
    assert len(db.photo_tray.docs) == 1
    assert removed == []


def test_delete_not_blocked_by_other_tenants_photo(router_env):
    ptr, db, removed = router_env
    db.photo_tray.docs.append(_tray())
    db.photos.docs.append({"id": "ph9", "tenant_id": "other",
                           "tray_photo_id": "tray1"})
    out = asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert out == {"ok": True, "deleted": True}


# ---------------------------------------------------------------------
# 2. Stored image removal
# ---------------------------------------------------------------------

def test_delete_untagged_removes_row_and_storage_object(router_env):
    ptr, db, removed = router_env
    db.photo_tray.docs.append(_tray())
    db.files.docs.append({"id": "f1", "tenant_id": TENANT,
                          "storage_path": "ice/tray/t1/abc.jpg",
                          "is_deleted": False})
    out = asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert out == {"ok": True, "deleted": True}
    assert db.photo_tray.docs == []
    assert removed == ["ice/tray/t1/abc.jpg"]
    assert db.files.docs[0]["is_deleted"] is True


def test_delete_runs_storage_call_off_the_event_loop(router_env, monkeypatch):
    ptr, db, removed = router_env
    db.photo_tray.docs.append(_tray())
    calls = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn, *a, **k):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(ptr.asyncio, "to_thread", _spy)
    asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert "_fake_remove" in calls
    assert removed == ["ice/tray/t1/abc.jpg"]


def test_delete_keeps_object_still_used_by_another_row(router_env):
    ptr, db, removed = router_env
    db.photo_tray.docs.append(_tray())
    # A second tray row (for example from an older duplicate) shares the key.
    db.photo_tray.docs.append(_tray(id="tray2"))
    asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert [d["id"] for d in db.photo_tray.docs] == ["tray2"]
    assert removed == []


def test_delete_keeps_object_referenced_by_a_photo_row(router_env):
    ptr, db, removed = router_env
    db.photo_tray.docs.append(_tray())
    # A photo row using the same image but not linked by tray_photo_id.
    db.photos.docs.append({"id": "ph2", "tenant_id": TENANT,
                           "storage_path": "ice/tray/t1/abc.jpg"})
    asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert db.photo_tray.docs == []
    assert removed == []


def test_delete_logs_storage_failure_and_still_succeeds(router_env, monkeypatch, caplog):
    ptr, db, _removed = router_env
    import services.storage as storage

    def _boom(_path):
        raise RuntimeError("http_500")

    monkeypatch.setattr(storage, "remove_object", _boom)
    db.photo_tray.docs.append(_tray())
    with caplog.at_level(logging.WARNING, logger="photo_tray_router"):
        out = asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert out["deleted"] is True
    assert db.photo_tray.docs == []
    assert any("could not remove storage object" in r.getMessage()
               for r in caplog.records)


def test_delete_without_storage_key_skips_storage(router_env):
    ptr, db, removed = router_env
    t = _tray()
    t.pop("storage_key")
    db.photo_tray.docs.append(t)
    out = asyncio.run(ptr.delete_tray_entry("tray1", ctx=CTX))
    assert out["deleted"] is True
    assert removed == []


# ---------------------------------------------------------------------
# 3. Stub captions
# ---------------------------------------------------------------------

def test_stub_analysis_has_no_caption():
    from services.photo_ai import _stub_analysis
    out = _stub_analysis({"id": "abcdef1234"})
    assert out["ai_suggested_caption"] is None
    # Class and event-type suggestions are still produced.
    assert out["ai_suggested_class"]
    assert out["ai_suggested_event_type"]


def test_usable_ai_caption_filters_stub_text():
    from routers.photo_tray_router import _usable_ai_caption
    assert _usable_ai_caption({"ai_suggested_caption": "[dev stub] progress photo from tray 12345678"}) is None
    assert _usable_ai_caption({"ai_suggested_caption": "  [DEV STUB] x"}) is None
    assert _usable_ai_caption({"ai_suggested_caption": ""}) is None
    assert _usable_ai_caption({}) is None
    assert _usable_ai_caption({"ai_suggested_caption": "Rebar placed at level 2"}) == "Rebar placed at level 2"


def test_attach_to_dr_does_not_copy_stub_caption(router_env, monkeypatch):
    ptr, db, _removed = router_env
    import services.edit_permissions as ep

    async def _policy(**_k):
        return ({}, False)

    monkeypatch.setattr(ep, "enforce_locked_dr_add_policy", _policy)
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": TENANT,
                                  "project_id": "p1", "status": "draft"})
    db.photo_tray.docs.append(_tray(ai_suggested_caption="[dev stub] damage photo from tray tray1"))
    db.photo_tray.docs.append(_tray(id="tray2", storage_key="ice/tray/t1/def.jpg",
                                    ai_suggested_caption="Formwork stripped on east wall"))
    payload = ptr._AttachToDrPayload(tray_ids=["tray1", "tray2"], daily_report_id="dr1")
    out = asyncio.run(ptr.attach_trays_to_dr(payload, ctx=CTX))
    assert out["created"] == 2
    by_tray = {p["tray_photo_id"]: p for p in db.photos.docs}
    assert by_tray["tray1"]["caption"] is None
    assert by_tray["tray1"]["tag_caption"] is None
    assert by_tray["tray2"]["caption"] == "Formwork stripped on east wall"
    assert by_tray["tray2"]["tag_caption"] == "Formwork stripped on east wall"


def test_pdf_rich_never_prints_stub_caption(monkeypatch):
    import routers.iter6_router as r6
    import services.pdf_safety as pdf_safety
    import services.storage as storage

    db = FakeDB()
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": TENANT, "project_id": "p1",
                                  "report_date": "2026-11-16", "status": "draft"})
    db.projects.docs.append({"id": "p1", "tenant_id": TENANT, "name": "Test project"})
    db.photos.docs.append({"id": "ph1", "tenant_id": TENANT, "daily_report_id": "dr1",
                           "caption": "[dev stub] progress photo from tray abcd1234",
                           "captured_at": "2026-11-16T09:00:00"})
    db.photos.docs.append({"id": "ph2", "tenant_id": TENANT, "daily_report_id": "dr1",
                           "caption": "Slab pour complete",
                           "captured_at": "2026-11-16T10:00:00"})
    captured = {}

    async def _fake_render(html, *a, **k):
        captured["html"] = html
        return b"%PDF-fake"

    async def _no_object(_path):
        raise RuntimeError("no storage in tests")

    monkeypatch.setattr(r6, "get_db", lambda: db)
    monkeypatch.setattr(pdf_safety, "arender_pdf", _fake_render)
    monkeypatch.setattr(storage, "aget_object", _no_object)

    resp = asyncio.run(r6.export_daily_report_pdf("dr1", mode="external", ctx=CTX))
    assert resp.body == b"%PDF-fake"
    html = captured["html"]
    assert "[dev stub]" not in html
    assert "Slab pour complete" in html
    assert "No caption" in html
