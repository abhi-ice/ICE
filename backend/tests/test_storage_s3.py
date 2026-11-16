"""S3 file store (Railway move, 2026-11-15).

Checks, with no network and no database:
  - which backend is picked from the environment;
  - put / get round trip on the S3 backend, against an in-memory stand-in
    for the bucket, and against moto's fake S3 when moto is installed;
  - the cached get and the async wrappers still go through the S3 backend;
  - the file-copy script copies under the same key, skips keys already in
    the bucket, writes the manifest, honours --dry-run and --limit, and
    never deletes anything.
No Emergent host is contacted: the Emergent read is replaced by a stub.
"""
import asyncio
import csv
import hashlib
import io
import os
import sys
import uuid

import pytest

from services import storage

S3_ENV = ("STORAGE_BACKEND", "EMERGENT_LLM_KEY", "S3_BUCKET", "S3_ENDPOINT_URL",
          "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_REGION", "S3_ADDRESSING_STYLE")


@pytest.fixture
def clean_env(monkeypatch):
    for k in S3_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(storage, "_s3_client", None)
    monkeypatch.setattr(storage, "_s3_client_config", None)
    return monkeypatch


class FakeBucket:
    """Just enough of a boto3 S3 client for storage.py."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def _missing(self, op):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"},
                            "ResponseMetadata": {"HTTPStatusCode": 404}}, op)

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = (bytes(Body), ContentType)
        return {"ETag": '"x"'}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self._missing("GetObject")
        data, ct = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(data), "ContentType": ct}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self._missing("HeadObject")
        data, ct = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data), "ContentType": ct}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def fake_s3(clean_env):
    bucket = FakeBucket()
    clean_env.setenv("STORAGE_BACKEND", "s3")
    clean_env.setenv("S3_BUCKET", "test-bucket")
    clean_env.setattr(storage, "_s3", lambda: (bucket, "test-bucket"))
    return bucket


# ---------------------------------------------------------------------
# Backend choice
# ---------------------------------------------------------------------
def test_backend_choice(clean_env):
    assert storage.storage_backend() == "unconfigured"
    clean_env.setenv("EMERGENT_LLM_KEY", "k")
    assert storage.storage_backend() == "emergent"  # unchanged on Emergent
    clean_env.setenv("S3_BUCKET", "b")
    assert storage.storage_backend() == "s3"  # a configured bucket wins
    clean_env.setenv("STORAGE_BACKEND", "s3")
    assert storage.storage_backend() == "s3"
    clean_env.setenv("STORAGE_BACKEND", "EMERGENT")
    assert storage.storage_backend() == "emergent"
    clean_env.delenv("STORAGE_BACKEND")
    clean_env.delenv("EMERGENT_LLM_KEY")
    assert storage.storage_backend() == "s3"  # bucket set, no Emergent key


def test_unconfigured_storage_fails_clearly(clean_env):
    with pytest.raises(RuntimeError, match="not set up"):
        storage.put_object("a/b", b"x", "text/plain")
    with pytest.raises(RuntimeError, match="not set up"):
        storage.get_object("a/b")


def test_s3_settings_defaults(clean_env):
    clean_env.setenv("S3_BUCKET", "b")
    cfg = storage.s3_settings()
    assert cfg["region"] == "auto"
    assert cfg["addressing_style"] == "virtual"
    assert cfg["endpoint_url"] is None
    clean_env.setenv("S3_ADDRESSING_STYLE", "PATH")
    assert storage.s3_settings()["addressing_style"] == "path"
    clean_env.setenv("S3_ADDRESSING_STYLE", "nonsense")
    assert storage.s3_settings()["addressing_style"] == "virtual"


def test_s3_client_uses_settings(clean_env):
    pytest.importorskip("boto3")
    clean_env.setenv("S3_BUCKET", "b")
    clean_env.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9")
    clean_env.setenv("S3_ACCESS_KEY_ID", "test-id")
    clean_env.setenv("S3_SECRET_ACCESS_KEY", "test-secret")
    clean_env.setenv("S3_ADDRESSING_STYLE", "path")
    client, bucket = storage._s3()
    assert bucket == "b"
    assert client.meta.endpoint_url == "http://127.0.0.1:9"
    assert client.meta.config.s3["addressing_style"] == "path"
    again, _ = storage._s3()
    assert again is client  # reused while settings are unchanged


def test_key_layout_unchanged():
    p = storage.make_path("tenant-1", "photos", ".JPG")
    parts = p.split("/")
    assert parts[0] == "ice-schedule-ops" and parts[1] == "photos" and parts[2] == "tenant-1"
    assert parts[3].endswith(".jpg") and len(parts[3]) == 32 + 4
    assert not p.startswith("/")


# ---------------------------------------------------------------------
# Round trip on the S3 backend
# ---------------------------------------------------------------------
def test_s3_round_trip_stub(fake_s3):
    path = storage.make_path("t1", "photos", "jpg")
    body = b"\xff\xd8 jpeg " + uuid.uuid4().bytes
    res = storage.put_object(path, body, "image/jpeg")
    assert res["path"] == path and res["size"] == len(body)
    assert storage.get_object(path) == (body, "image/jpeg")
    assert storage.s3_head_object(path)["size"] == len(body)
    assert storage.s3_head_object(path + ".nope") is None
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        storage.get_object(path + ".nope")


def test_async_and_cached_use_s3(fake_s3):
    path = storage.make_path("t1", "specs", "pdf")
    body = b"%PDF-1.7 " + uuid.uuid4().bytes

    async def go():
        await storage.aput_object(path, body, "application/pdf")
        a = await storage.aget_object(path)
        b = await storage.aget_object_cached(path)
        return a, b

    try:
        a, b = asyncio.run(go())
        assert a == b == (body, "application/pdf")
        # A cached read survives the object disappearing from the bucket.
        fake_s3.objects.clear()
        assert storage.get_object_cached(path) == (body, "application/pdf")
    finally:
        hit = storage._BLOB_CACHE.pop(path, None)
        if hit is not None:
            storage._blob_cache_bytes -= len(hit[0])
    assert storage._BLOB_CACHE_MAX_BYTES == 256 * 1024 * 1024


def test_remove_object_s3(fake_s3):
    storage.put_object("ice-schedule-ops/x/t/a.bin", b"1", "application/octet-stream")
    assert storage.remove_object("ice-schedule-ops/x/t/a.bin") == "deleted"
    assert storage.remove_object("ice-schedule-ops/x/t/a.bin") == "already_gone"


def test_s3_round_trip_moto(clean_env):
    moto = pytest.importorskip("moto")
    import boto3
    clean_env.setenv("STORAGE_BACKEND", "s3")
    clean_env.setenv("S3_BUCKET", "ice-test-bucket")
    clean_env.setenv("S3_REGION", "us-east-1")
    clean_env.setenv("S3_ACCESS_KEY_ID", "testing")
    clean_env.setenv("S3_SECRET_ACCESS_KEY", "testing")
    mock = getattr(moto, "mock_aws", None) or getattr(moto, "mock_s3")
    with mock():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="ice-test-bucket")
        for style in ("virtual", "path"):
            clean_env.setenv("S3_ADDRESSING_STYLE", style)
            path = storage.make_path("t1", "drawings", "pdf")
            body = b"%PDF " + uuid.uuid4().bytes
            storage.put_object(path, body, "application/pdf")
            assert storage.get_object(path) == (body, "application/pdf")
            assert storage.s3_head_object(path)["size"] == len(body)
            assert storage.s3_head_object(path + "x") is None


# ---------------------------------------------------------------------
# File-copy script
# ---------------------------------------------------------------------
class FakeColl:
    def __init__(self, docs):
        self.docs = docs

    def find(self, flt=None, proj=None):
        return [dict(d) for d in self.docs]

    def __getattr__(self, name):  # any write would show up here
        raise AssertionError(f"copy script must not call {name}")


class FakeDB:
    def __init__(self, data):
        self.data = {k: FakeColl(v) for k, v in data.items()}

    def list_collection_names(self):
        return list(self.data)

    def __getitem__(self, name):
        return self.data[name]


def _src_blob(path):
    return ("bytes of " + path).encode()


@pytest.fixture
def copy_env(fake_s3, monkeypatch):
    from scripts import copy_emergent_files as cef
    reads = []

    def fake_emergent_get(path):
        reads.append(path)
        if path.endswith("missing.pdf"):
            import requests
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError("404", response=resp)
        return _src_blob(path), "application/pdf"

    monkeypatch.setattr(storage, "emergent_get_object", fake_emergent_get)
    monkeypatch.setattr(storage, "emergent_put_object",
                        lambda *a, **k: pytest.fail("must not write to Emergent"))
    monkeypatch.setattr(storage, "emergent_remove_object",
                        lambda *a, **k: pytest.fail("must not delete from Emergent"))
    monkeypatch.setenv("EMERGENT_LLM_KEY", "stub-not-used")
    return cef, reads


def _db():
    p = "ice-schedule-ops/photos/t1/aaa.jpg"
    return FakeDB({
        "files": [
            {"storage_path": p, "size": len(_src_blob(p))},
            {"storage_path": "ice-schedule-ops/specs/t1/bbb.pdf", "size": 1},  # wrong size on purpose
        ],
        "photos": [{"storage_path": p, "sha256_hash": hashlib.sha256(_src_blob(p)).hexdigest(),
                    "thumbnail_path": None}],
        "photo_tray": [{"storage_key": "ice-schedule-ops/tray/t1/ccc.jpg"}],
        "drawing_revisions": [{"storage_path": None, "pdf_url": "ice-schedule-ops/drawings/t1/ddd.pdf"}],
        "voice_annotations": [{"audio_url": "ice-schedule-ops/audio/voice/t1/eee.webm"}],
        "recovery_reports": [{"exhibit_artifacts": [
            {"storage_key": "ice-schedule-ops/recovery/t1/fff.pdf", "sha256": "0" * 64}]}],
        "tia_generations": [{"extra": {"nested": ["ice-schedule-ops/tia/t1/ggg.pdf"]}}],
        "project_specs": [{"pdf_url": "ice-schedule-ops/specs/t1/missing.pdf"}],
        "projects": [{"dr_template_file_url": "https://example.invalid/not-a-key.xlsx"},
                     {"dr_template_file_url": "ice-schedule-ops/dr_templates/t1/hhh.xlsx"}],
        "unlisted": [{"x": "ice-schedule-ops/other/t1/iii.bin"}],
    })


def _manifest(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["path"]: r for r in csv.DictReader(fh)}


def test_copy_script_collects_every_listed_field(copy_env):
    cef, _ = copy_env
    paths = cef.collect_paths(_db()).paths()
    assert "ice-schedule-ops/tray/t1/ccc.jpg" in paths
    assert "ice-schedule-ops/drawings/t1/ddd.pdf" in paths
    assert "ice-schedule-ops/audio/voice/t1/eee.webm" in paths
    assert "ice-schedule-ops/recovery/t1/fff.pdf" in paths
    assert "ice-schedule-ops/tia/t1/ggg.pdf" in paths
    assert "ice-schedule-ops/dr_templates/t1/hhh.xlsx" in paths
    assert not any(p.startswith("http") for p in paths)
    assert "ice-schedule-ops/other/t1/iii.bin" not in paths
    assert "ice-schedule-ops/other/t1/iii.bin" in cef.collect_paths(_db(), scan_all=True).paths()
    assert len(paths) == len(set(paths))


def test_copy_script_dry_run_reads_and_writes_nothing(copy_env, tmp_path):
    cef, reads = copy_env
    m = tmp_path / "m.csv"
    res = cef.run(_db(), dry_run=True, limit=None, manifest=str(m), workers=2,
                  verify_existing=False, scan_all=False, out=lambda *_: None)
    assert reads == []
    assert storage._s3()[0].objects == {}
    assert set(r["status"] for r in _manifest(m).values()) == {"would_copy"}
    assert res["total"] == 9


def test_copy_script_copies_same_key_and_is_idempotent(copy_env, tmp_path):
    cef, reads = copy_env
    bucket = storage._s3()[0]
    # One file is already in the bucket and must be skipped.
    storage.put_object("ice-schedule-ops/tray/t1/ccc.jpg", b"already", "image/jpeg")

    m1 = tmp_path / "m1.csv"
    cef.run(_db(), dry_run=False, limit=None, manifest=str(m1), workers=3,
            verify_existing=False, scan_all=False, out=lambda *_: None)
    rows = _manifest(m1)
    assert list(rows[next(iter(rows))].keys())[:4] == ["path", "bytes", "sha256", "status"]
    p = "ice-schedule-ops/photos/t1/aaa.jpg"
    assert rows[p]["status"] == "copied"
    assert rows[p]["sha256"] == hashlib.sha256(_src_blob(p)).hexdigest()
    assert rows[p]["check"] == "ok"
    assert rows["ice-schedule-ops/specs/t1/bbb.pdf"]["check"] == "size_mismatch"
    assert rows["ice-schedule-ops/recovery/t1/fff.pdf"]["check"] == "sha256_mismatch"
    assert rows["ice-schedule-ops/specs/t1/missing.pdf"]["status"] == "missing_in_source"
    assert rows["ice-schedule-ops/tray/t1/ccc.jpg"]["status"] == "exists"
    assert "ice-schedule-ops/tray/t1/ccc.jpg" not in reads
    # Same key, same bytes.
    assert storage.get_object(p) == (_src_blob(p), "application/pdf")
    assert storage.get_object("ice-schedule-ops/tray/t1/ccc.jpg")[0] == b"already"
    assert bucket.deleted == []

    reads.clear()
    m2 = tmp_path / "m2.csv"
    cef.run(_db(), dry_run=False, limit=None, manifest=str(m2), workers=2,
            verify_existing=False, scan_all=False, out=lambda *_: None)
    rows2 = _manifest(m2)
    assert rows2[p]["status"] == "exists"
    assert reads == ["ice-schedule-ops/specs/t1/missing.pdf"]  # only the one not copied yet
    assert bucket.deleted == []


def test_copy_script_limit(copy_env, tmp_path):
    cef, reads = copy_env
    m = tmp_path / "m.csv"
    res = cef.run(_db(), dry_run=False, limit=2, manifest=str(m), workers=1,
                  verify_existing=False, scan_all=False, out=lambda *_: None)
    assert res["total"] == 2 and len(reads) == 2
    assert len(_manifest(m)) == 2


def test_copy_script_needs_settings(copy_env, tmp_path, monkeypatch):
    cef, _ = copy_env
    monkeypatch.delenv("MONGO_URL", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    assert cef.main(["--dry-run", "--manifest", str(tmp_path / "x.csv")]) == 2
