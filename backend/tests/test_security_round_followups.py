"""Railway move (2026-11-16): follow-up security fixes.

Covers:
  * AI assistant query tool: audit_log / copilot_messages are admin-only,
    every returned row passes through auth.redact_secrets (invite codes on
    team_invites audit rows too);
  * POST /ai/copilot and POST /approvals refuse another company's project;
  * audit extract export is admin-only and masks secrets in the recorded
    values (hash / prev_hash columns stay); /audit-log/verify-chain is
    admin-only;
  * POST /photos refuses a file key outside the caller's company BEFORE
    the object is read;
  * photo EXIF extract, drawing polygon suggest and drawing OCR refuse a
    file key outside the company before reading; weekly-report thumbnails
    skip such keys;
  * photo AI preprocessing refuses a decompression bomb as
    ImageTooLargeError (analysis skipped, photo kept);
  * POST /kb/entries, safety-event-from-photo check the project;
    generic CRUD PATCH quietly drops a foreign project id;
  * scripts/reset_password bumps token_version;
  * server._ensure_indexes creates the sign-in throttling indexes;
  * audit_chain_heads goes with audit_log in the close-account purge and
    is preserved with it by migration 006.

No network, no real Mongo: a small in-memory fake database stands in.
"""
import asyncio
import importlib
import io
import types

import pytest
from fastapi import HTTPException

import auth
from services import project_scope, copilot_tools, exhibit_exports, photo_ai
from services import storage
from routers import (
    system_router, exhibit_exports_router, iter6_router, iter7_router,
    iter8_router, field_events_router, photo_tray_router, auth_router,
)

TID = "tenant-a"
OTHER = "tenant-b"
OWN_PID = "11111111-2222-3333-4444-555555555555"
FOREIGN_PID = "99999999-8888-7777-6666-555555555555"
ADMIN = {"tenant_id": TID, "user": {"id": "u1", "role": "admin"}, "request_id": "r1"}
PM = {"tenant_id": TID, "user": {"id": "u2", "role": "pm"}, "request_id": "r2"}
SUPER = {"tenant_id": TID, "user": {"id": "u3", "role": "super"}, "request_id": "r3"}
OWN_KEY = f"ice/photos/{TID}/abc.jpg"
FOREIGN_KEY = f"ice/photos/{OTHER}/abc.jpg"


def _run(coro):
    return asyncio.run(coro)


def _match(doc, flt):
    for k, v in (flt or {}).items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        elif isinstance(v, dict):
            continue  # range filters etc. are not needed here
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    async def to_list(self, n=None, length=None):
        return [dict(d) for d in self.docs]


class _Coll:
    def __init__(self):
        self.docs = []

    async def find_one(self, flt, proj=None, **kw):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    def find(self, flt=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, flt, upd, upsert=False):
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set", {}))
                for k, n in upd.get("$inc", {}).items():
                    d[k] = d.get(k, 0) + n
                return types.SimpleNamespace(matched_count=1)
        return types.SimpleNamespace(matched_count=0)

    async def create_index(self, *a, **kw):
        return "ix"


class _DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.fixture
def db(monkeypatch):
    d = _DB()
    d.projects.docs.append({"id": OWN_PID, "tenant_id": TID, "name": "Own"})
    d.projects.docs.append({"id": FOREIGN_PID, "tenant_id": OTHER, "name": "Theirs"})
    for mod in (project_scope, copilot_tools, exhibit_exports, system_router,
                exhibit_exports_router, iter6_router, iter7_router, iter8_router,
                field_events_router, photo_tray_router):
        monkeypatch.setattr(mod, "get_db", lambda: d)

    async def _no_audit(**kw):
        return None
    for mod in (system_router, exhibit_exports_router, field_events_router,
                photo_tray_router):
        if hasattr(mod, "write_audit"):
            monkeypatch.setattr(mod, "write_audit", _no_audit)
    return d


def _status(coro):
    with pytest.raises(HTTPException) as e:
        _run(coro)
    return e.value.status_code


# --------------------------------------------------------------- 1. copilot
def _seed_audit(db):
    db.audit_log.docs.append({
        "id": "a1", "tenant_id": TID, "table_name": "team_invites",
        "op": "INSERT", "hash": "h1", "prev_hash": "h0",
        "after_data": {"code": "INVITE-123", "email": "x@y.z"},
    })


def test_copilot_query_audit_log_admin_only(db):
    _seed_audit(db)
    out = _run(copilot_tools._tool_query(SUPER, {"collection": "audit_log"}))
    assert "error" in out and "audit_log" not in out["allowed_collections"]
    assert "copilot_messages" not in out["allowed_collections"]
    out = _run(copilot_tools._tool_query(PM, {"collection": "copilot_messages"}))
    assert "error" in out
    out = _run(copilot_tools._tool_query(ADMIN, {"collection": "audit_log"}))
    assert out["count"] == 1
    row = out["rows"][0]
    assert row["after_data"]["code"] == auth.REDACTED
    assert row["after_data"]["email"] == "x@y.z"


def test_copilot_query_never_lists_secret_collections(db):
    out = _run(copilot_tools._tool_query(ADMIN, {"collection": "nope"}))
    allowed = set(out["allowed_collections"])
    for coll in ("users", "tenant_users", "team_invites", "auth_attempts",
                 "payments", "payment_transactions", "worker_tokens"):
        assert coll not in allowed


def test_copilot_query_redacts_every_row(db):
    db.people.docs.append({"id": "p1", "tenant_id": TID, "name": "Pat",
                           "pin": "1234", "code": "C-1", "api_key": "k"})
    out = _run(copilot_tools._tool_query(SUPER, {"collection": "people"}))
    row = out["rows"][0]
    assert row["pin"] == auth.REDACTED and row["api_key"] == auth.REDACTED
    assert row["name"] == "Pat" and row["code"] == "C-1"


def test_ai_copilot_refuses_foreign_project(db, monkeypatch):
    called = []

    async def _fake_turn(**kw):
        called.append(kw)
        return {"reply": "ok"}
    monkeypatch.setattr(copilot_tools, "execute_copilot_turn", _fake_turn)
    msg = system_router.CopilotMessage(message="hi", project_id=FOREIGN_PID)
    assert _status(system_router.ai_copilot(msg, SUPER)) == 404
    assert called == []
    _run(system_router.ai_copilot(system_router.CopilotMessage(message="hi"), SUPER))
    _run(system_router.ai_copilot(
        system_router.CopilotMessage(message="hi", project_id=OWN_PID), SUPER))
    assert len(called) == 2


def test_create_approval_refuses_foreign_project(db):
    body = dict(kind="schedule_proposal", subject_entity_type="task",
                subject_entity_id="t1")
    try:
        payload = system_router.ApprovalCreate(project_id=FOREIGN_PID, **body)
    except Exception:
        payload = types.SimpleNamespace(project_id=FOREIGN_PID)
    assert _status(system_router.create_approval(payload, SUPER)) == 404
    assert db.approvals.docs == []


# ---------------------------------------------------- 2. audit extract
def test_audit_extract_admin_only(db):
    for ctx in (PM, SUPER):
        assert _status(exhibit_exports_router.export_audit_extract(
            OWN_PID, format="json", date_range_start=None, date_range_end=None,
            op_filter=None, actor_filter=None, entity_type=None, entity_id=None,
            ctx=ctx)) == 403


def test_audit_extract_masks_secrets_keeps_hashes(db):
    _seed_audit(db)
    db.audit_log.docs.append({
        "id": "a2", "tenant_id": TID, "table_name": "users", "op": "UPDATE",
        "hash": "h2", "prev_hash": "h1",
        "after_data": {"password_hash": "$2b$xyz", "code": "kept",
                       "project_id": OWN_PID},
    })
    out = _run(exhibit_exports.gather_audit_extract(tenant_id=TID, project_id=OWN_PID))
    rows = {r["id"]: r for r in out["rows"]}
    assert rows["a1"]["after_data"]["code"] == auth.REDACTED
    assert rows["a2"]["after_data"]["password_hash"] == auth.REDACTED
    assert rows["a2"]["after_data"]["code"] == "kept"
    assert rows["a2"]["hash"] == "h2" and rows["a2"]["prev_hash"] == "h1"
    assert out["hash_chain_intact"] is True


def test_audit_extract_project_filter_reads_after_data(db):
    db.audit_log.docs.append({"id": "x", "tenant_id": TID, "table_name": "rfis",
                              "after_data": {"project_id": FOREIGN_PID}})
    out = _run(exhibit_exports.gather_audit_extract(tenant_id=TID, project_id=OWN_PID))
    assert [r["id"] for r in out["rows"]] == []


def test_verify_chain_admin_only(db):
    assert _status(iter7_router.verify_audit_chain(PM, limit=10)) == 403
    out = _run(iter7_router.verify_audit_chain(ADMIN, limit=10))
    assert out["ok"] is True


# ------------------------------------------------------ 3. photo create
def test_create_photo_refuses_foreign_key_before_reading(db, monkeypatch):
    read = []

    async def _hash(path):
        read.append(path)
        return None
    monkeypatch.setattr(field_events_router, "_hash_storage_object", _hash)
    for key in (FOREIGN_KEY, "../../etc/passwd", "", "https://x/y"):
        payload = types.SimpleNamespace(project_id=OWN_PID, storage_path=key)
        with pytest.raises(HTTPException) as e:
            _run(field_events_router.create_photo(payload, SUPER))
        assert e.value.status_code == 404 and e.value.detail == "File not found"
    assert read == [] and db.photos.docs == []


# ------------------------------------------ 4. file reads in iter6 / iter7
def test_extract_exif_refuses_foreign_key(db, monkeypatch):
    pytest.importorskip("imagehash")
    read = []

    async def _get(key):
        read.append(key)
        return b"", "image/jpeg"
    monkeypatch.setattr(storage, "aget_object", _get)
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "project_id": OWN_PID,
                           "storage_path": FOREIGN_KEY})
    assert _status(iter6_router.extract_photo_exif("ph1", SUPER)) == 404
    assert read == []


@pytest.mark.parametrize("fn,payload_cls", [
    ("suggest_polygon", "PolygonSuggestRequest"),
    ("drawing_ocr", "DrawingOcrRequest"),
])
def test_drawing_reads_refuse_foreign_key(db, monkeypatch, fn, payload_cls):
    pytest.importorskip("pdf2image")
    if fn == "drawing_ocr":
        pytest.importorskip("pytesseract")
    read = []

    def _get(key):
        read.append(key)
        return b"%PDF", "application/pdf"
    monkeypatch.setattr(storage, "get_object", _get)
    db.drawing_revisions.docs.append({"id": "rev1", "tenant_id": TID,
                                      "storage_path": f"ice/drawings/{OTHER}/d.pdf"})
    payload = getattr(iter7_router, payload_cls)(drawing_revision_id="rev1", page=1)
    assert _status(getattr(iter7_router, fn)(payload, SUPER)) == 404
    assert read == []


def _png(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("1", (w, h)).save(buf, format="PNG")
    return buf.getvalue()


def test_photo_ai_preprocess_refuses_bomb_softly():
    with pytest.raises(photo_ai.ImageTooLargeError):
        photo_ai._preprocess_image_for_vision(_png(8000, 8000))  # 64 MP
    jpeg, mt, meta = photo_ai._preprocess_image_for_vision(_png(100, 80))
    assert mt == "image/jpeg" and meta["width"] == 100


# ---------------------------------------------------- 5. project checks
def test_kb_entry_project_checked(db):
    bad = iter7_router.KbEntryCreate(**_kb_body(FOREIGN_PID))
    assert _status(iter7_router.kb_create(bad, SUPER)) == 404
    assert db.kb_entries.docs == []


def _kb_body(pid):
    fields = iter7_router.KbEntryCreate.model_fields
    body = {"project_id": pid}
    for name, f in fields.items():
        if f.is_required() and name not in body:
            body[name] = "x"
    return body


def test_safety_from_photo_checks_tray_project(db, monkeypatch):
    async def _ok(*a, **kw):
        return None
    monkeypatch.setattr(photo_tray_router, "_enforce_tray_access", _ok)
    db.photo_tray.docs.append({"id": "t1", "tenant_id": TID, "project_id": FOREIGN_PID})
    db.daily_reports.docs.append({"id": "dr1", "tenant_id": TID, "project_id": FOREIGN_PID})
    payload = photo_tray_router._CreateSafetyFromPhotoPayload(daily_report_id="dr1")
    assert _status(photo_tray_router.create_safety_event_from_photo("t1", payload, SUPER)) == 404
    assert db.safety_events.docs == []


def test_crud_patch_drops_foreign_project_id(db):
    entity = next(iter(iter8_router.ENTITY_CONFIG))
    coll = iter8_router.ENTITY_CONFIG[entity]["collection"]
    db[coll].docs.append({"id": "e1", "tenant_id": TID, "project_id": OWN_PID, "name": "a"})
    bg = types.SimpleNamespace(add_task=lambda *a, **kw: None)
    _run(iter8_router.crud_patch(entity, "e1", {"project_id": FOREIGN_PID, "name": "b"}, bg, SUPER))
    doc = db[coll].docs[0]
    assert doc["project_id"] == OWN_PID and doc["name"] == "b"


# ------------------------------------------------- 6. reset_password CLI
def test_reset_password_bumps_token_version(monkeypatch):
    import motor.motor_asyncio as mm
    import audit
    from scripts import reset_password
    fake = _DB()
    fake.users.docs.append({"id": "u9", "tenant_id": TID, "email": "a@b.c",
                            "password_hash": "$2b$old", "token_version": 2})

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __getitem__(self, name):
            return fake
    monkeypatch.setattr(mm, "AsyncIOMotorClient", _Client)

    async def _no_audit(**kw):
        return None
    monkeypatch.setattr(audit, "write_audit", _no_audit)
    monkeypatch.setenv("MONGO_URL", "mongodb://fake")
    monkeypatch.setenv("DB_NAME", "fake")
    import database
    monkeypatch.setattr(database, "_client", getattr(database, "_client", None), raising=False)
    monkeypatch.setattr(database, "_db", getattr(database, "_db", None), raising=False)
    rc = _run(reset_password.main(["--email", "a@b.c", "--password", "NewPass123!",
                                   "--executed-by", "ops"]))
    assert rc == 0
    assert fake.users.docs[0]["token_version"] == 3


# --------------------------------------------- 7. sign-in throttle indexes
def test_ensure_indexes_creates_auth_attempt_indexes(monkeypatch):
    import server
    calls = []

    async def _safe(*a, **kw):
        return True

    async def _ensure(db=None):
        calls.append(db)
    monkeypatch.setattr(server, "_safe_create_index", _safe)
    monkeypatch.setattr(server, "get_db", lambda: _DB(), raising=False)
    monkeypatch.setattr(auth, "ensure_auth_attempt_indexes", _ensure)
    _run(server._ensure_indexes())
    assert len(calls) == 1


# ------------------------------------------------- 8. audit_chain_heads
def test_audit_chain_heads_follow_audit_log():
    assert "audit_chain_heads" in auth_router.TENANT_SCOPED_COLLECTIONS
    mig = importlib.import_module("migrations.006_test_data_reset")
    assert "audit_log" in mig.PRESERVED_COLLECTIONS
    assert "audit_chain_heads" in mig.PRESERVED_COLLECTIONS
    assert all(c != "audit_chain_heads" for c, _ in mig.WIPE_COLLECTIONS)
