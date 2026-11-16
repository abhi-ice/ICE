"""Full backup download (2026-11-16): round trip + who may download.

Round trip runs against the local test MongoDB (MONGO_URL, test_* names
only): seed awkward types, build the zip, restore it into a second test
database, and compare every document field-for-field, types included.
"""
import asyncio
import datetime as dt
import io
import json
import os
import sys
import zipfile

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from bson import Decimal128, Int64, ObjectId  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from routers import auth_router, backup_router  # noqa: E402  (import now: later test modules stub `auth`)

SRC = "test_backup_src"
DST = "test_backup_dst"


def _uri():
    return os.environ.get("MONGO_URL") or "mongodb://127.0.0.1:27017"


def _docs():
    return {
        "daily_reports": [
            {"_id": ObjectId(), "id": "dr1", "tenant_id": "t1",
             "created_at": dt.datetime(2026, 11, 9, 14, 54, 30, 755000),
             "notes": "Line one\nline two — dash, quote \" and émoji 🚧",
             "big": Int64(2 ** 40), "ratio": 0.1, "money": Decimal128("14743224.07"),
             "nested": {"list": [1, "a", None, {"d": dt.datetime(2026, 1, 1)}]},
             "empty": {}, "flag": False},
            {"_id": "string-id", "id": "dr2", "tenant_id": "t2", "bytes": b"\x00\x01binary"},
        ],
        "audit_log": [{"id": f"a{i}", "hash": f"h{i}", "created_at": f"2026-11-0{i % 9 + 1}"} for i in range(1200)],
        "empty_coll": [],
    }


@pytest.fixture()
def dbs():
    pymongo = pytest.importorskip("pymongo")
    motor = pytest.importorskip("motor.motor_asyncio")
    try:
        pymongo.MongoClient(_uri(), serverSelectionTimeoutMS=2000).admin.command("ping")
    except Exception:
        pytest.skip("no local MongoDB")
    sync = pymongo.MongoClient(_uri())
    for n in (SRC, DST):
        assert n.startswith("test_")
        sync.drop_database(n)
    src = sync[SRC]
    for name, docs in _docs().items():
        if docs:
            src[name].insert_many([dict(d) for d in docs])
        else:
            src.create_collection(name)
    src.audit_log.create_index([("tenant_id", 1), ("created_at", -1)], name="audit_tenant_created")
    src.daily_reports.create_index("id", name="dr_id_unique", unique=True)
    yield sync, motor.AsyncIOMotorClient(_uri())
    for n in (SRC, DST):
        sync.drop_database(n)


def test_round_trip_keeps_every_document_and_type(dbs):
    sync, amotor = dbs
    buf = io.BytesIO()

    manifest = backup_router.build_backup(sync[SRC], buf)
    assert manifest["collections"]["daily_reports"]["count"] == 2
    assert manifest["collections"]["audit_log"]["count"] == 1200
    assert manifest["collections"]["empty_coll"]["count"] == 0
    assert manifest["total_documents"] == 1202

    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        assert json.loads(zf.read("manifest.json"))["format"] == "ice-backup-v1"
    assert manifest["collections"]["daily_reports"]["indexes"]
    path = os.path.join(os.path.dirname(__file__), "_tmp_backup_test.zip")
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    try:
        from scripts.restore_backup import restore
        res = restore(path, sync[DST], log=lambda *_: None)
    finally:
        os.remove(path)

    assert all(exp == got for exp, got in res.values())
    for name in ("daily_reports", "audit_log"):
        a = list(sync[SRC][name].find().sort("_id", 1))
        b = list(sync[DST][name].find().sort("_id", 1))
        assert a == b
    dr = sync[DST].daily_reports.find_one({"id": "dr1"})
    assert isinstance(dr["created_at"], dt.datetime)
    assert isinstance(dr["_id"], ObjectId)
    assert isinstance(dr["big"], int) and dr["big"] == 2 ** 40
    assert isinstance(dr["money"], Decimal128)
    assert "empty_coll" in sync[DST].list_collection_names()
    ix = sync[DST].daily_reports.index_information()
    assert ix["dr_id_unique"].get("unique") is True
    assert "audit_tenant_created" in sync[DST].audit_log.index_information()


def test_restore_refuses_non_empty_target_without_drop(dbs, tmp_path):
    sync, amotor = dbs
    buf = io.BytesIO()
    backup_router.build_backup(sync[SRC], buf)
    p = tmp_path / "b.zip"
    p.write_bytes(buf.getvalue())
    from scripts.restore_backup import restore
    with pytest.raises(SystemExit):
        restore(str(p), sync[SRC], log=lambda *_: None)  # SRC already has data


def _ctx(email):
    return {"user": {"id": "u1", "email": email, "role": "admin"}, "tenant_id": "t1"}


def test_no_platform_owner_set_means_no_backup_even_for_admins(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(backup_router.download_backup(_ctx("admin@x.com")))
    assert e.value.status_code == 403


def test_other_admins_are_refused(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "owner@ice.com")
    with pytest.raises(HTTPException) as e:
        asyncio.run(backup_router.download_backup(_ctx("someone@else.com")))
    assert e.value.status_code == 403


def test_second_backup_while_one_runs_is_refused(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "Owner@ICE.com")

    async def go():
        await backup_router._LOCK.acquire()
        try:
            with pytest.raises(HTTPException) as e:
                await backup_router.download_backup(_ctx("owner@ice.com"))
            return e.value.status_code
        finally:
            backup_router._LOCK.release()

    backup_router._LOCK = asyncio.Lock()
    assert asyncio.run(go()) == 409
    backup_router._LOCK = asyncio.Lock()


def test_owner_gets_a_zip(dbs, monkeypatch):
    sync, amotor = dbs
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "owner@ice.com")
    monkeypatch.setattr(backup_router, "get_db", lambda: amotor[SRC])
    backup_router._LOCK = asyncio.Lock()

    async def go():
        resp = await backup_router.download_backup(_ctx("OWNER@ice.com"))
        body = b""
        async for chunk in resp.body_iterator:
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
        return resp, body

    resp, body = asyncio.run(go())
    assert resp.headers["content-disposition"].startswith('attachment; filename="ice-backup-')
    assert int(resp.headers["content-length"]) == len(body)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        m = json.loads(zf.read("manifest.json"))
    assert m["collections"]["daily_reports"]["count"] == 2
    run = sync[SRC].backup_runs.find_one()
    assert run["by"] == "owner@ice.com" and run["total_documents"] == 1202


# ---- restore command-line safety --------------------------------------

def _cli(monkeypatch, argv, env):
    from scripts import restore_backup
    for k in ("RESTORE_TARGET_MONGO_URL", "MONGO_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["restore_backup.py", *argv])
    return restore_backup.main()


def _zip(sync, tmp_path):
    buf = io.BytesIO()
    backup_router.build_backup(sync[SRC], buf)
    p = tmp_path / "b.zip"
    p.write_bytes(buf.getvalue())
    return str(p)


def test_restore_never_uses_the_apps_mongo_url(dbs, tmp_path, monkeypatch):
    sync, _ = dbs
    z = _zip(sync, tmp_path)
    assert _cli(monkeypatch, [z, DST], {"MONGO_URL": _uri()}) == 2
    assert DST not in sync.list_database_names()


def test_drop_needs_the_database_name_repeated(dbs, tmp_path, monkeypatch):
    sync, _ = dbs
    z = _zip(sync, tmp_path)
    assert _cli(monkeypatch, [z, DST, "--drop"], {"RESTORE_TARGET_MONGO_URL": _uri()}) == 2
    assert _cli(monkeypatch, [z, DST, "--drop", "--confirm", "wrong"], {"RESTORE_TARGET_MONGO_URL": _uri()}) == 2
    assert _cli(monkeypatch, [z, DST, "--drop", "--confirm", DST], {"RESTORE_TARGET_MONGO_URL": _uri()}) == 0
    assert sync[DST].audit_log.count_documents({}) == 1200


def test_refuses_to_restore_over_its_own_source(dbs, tmp_path, monkeypatch):
    sync, _ = dbs
    z = _zip(sync, tmp_path)
    env = {"RESTORE_TARGET_MONGO_URL": _uri(), "MONGO_URL": _uri()}
    assert _cli(monkeypatch, [z, SRC, "--drop", "--confirm", SRC], env) == 2
    assert sync[SRC].audit_log.count_documents({}) == 1200


# ---- the owner's email is off-limits to company team tools -------------

class _Payload:
    def __init__(self, email):
        self.email = email


def test_owner_email_cannot_be_invited_or_registered(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "owner@ice.com")
    assert auth_router._is_platform_owner_email("Owner@ICE.com ")
    assert not auth_router._is_platform_owner_email("someone@ice.com")
    assert not auth_router._is_platform_owner_email(None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth_router.create_invite(_Payload("OWNER@ice.com"), None, _ctx("admin@other.com")))
    assert e.value.status_code == 403


def test_owner_guard_off_when_no_owner_configured(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    assert not auth_router._is_platform_owner_email("owner@ice.com")
