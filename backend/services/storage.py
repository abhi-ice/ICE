"""File storage helper (photos, drawings, specs, .mpp files, client forms,
voice recordings, reports).

Two interchangeable backends sit behind the same public functions
(put_object / get_object / make_path / record_file and the async and
cached versions), so no caller changes when the backend changes:

  s3        Any S3-compatible bucket (Railway Storage Bucket, Cloudflare R2,
            AWS S3, MinIO). Chosen with STORAGE_BACKEND=s3. Settings:
              S3_ENDPOINT_URL       bucket endpoint (leave empty for AWS)
              S3_BUCKET             bucket name (required)
              S3_ACCESS_KEY_ID      access key
              S3_SECRET_ACCESS_KEY  secret key
              S3_REGION             default "auto"
              S3_ADDRESSING_STYLE   "virtual" (default) or "path"
  emergent  Emergent's object store. Chosen with STORAGE_BACKEND=emergent,
            or by default when STORAGE_BACKEND is not set and
            EMERGENT_LLM_KEY is present -- so this branch still runs
            unchanged if it is deployed on Emergent.

When STORAGE_BACKEND is not set and there is no EMERGENT_LLM_KEY, the S3
backend is used if S3_BUCKET is set; otherwise storage is "unconfigured"
and every read or write fails with a clear message.

Both backends use the same key layout,
{app_name}/{kind}/{tenant_id}/{uuid}.{ext}, so a file copied from one to
the other keeps its path and no database row has to change. The database
is the source of truth; files are soft-deleted via is_deleted=True on the
file record.
"""
import os
import uuid
import asyncio
import logging
import threading
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"
APP_NAME = "ice-schedule-ops"

BACKEND_S3 = "s3"
BACKEND_EMERGENT = "emergent"
BACKEND_UNCONFIGURED = "unconfigured"

_storage_key: str | None = None


def get_db():
    """The database handle. Imported on first use so the storage helpers
    (and their tests) load without the database driver."""
    from database import get_db as _get_db
    return _get_db()


# =====================================================================
# Which backend
# =====================================================================

_warned_unknown_backend = False


def storage_backend() -> str:
    """The backend in use: "s3", "emergent" or "unconfigured". Read from
    the environment on every call (cheap), so a test can change it."""
    raw = (os.environ.get("STORAGE_BACKEND") or "").strip().lower()
    if raw == BACKEND_S3:
        return BACKEND_S3
    if raw == BACKEND_EMERGENT:
        return BACKEND_EMERGENT
    if raw:
        global _warned_unknown_backend
        if not _warned_unknown_backend:
            _warned_unknown_backend = True
            log.warning("Unknown STORAGE_BACKEND value; expected 's3' or 'emergent'")
    # A bucket wins over the Emergent key, so setting EMERGENT_LLM_KEY on
    # Railway (e.g. to run the file copy) can never send uploads back to
    # Emergent. Emergent never sets S3_BUCKET, so it is unaffected.
    if (os.environ.get("S3_BUCKET") or "").strip():
        return BACKEND_S3
    if (os.environ.get("EMERGENT_LLM_KEY") or "").strip():
        return BACKEND_EMERGENT
    return BACKEND_UNCONFIGURED


def is_emergent_backend() -> bool:
    return storage_backend() == BACKEND_EMERGENT


def _unconfigured_error() -> RuntimeError:
    return RuntimeError(
        "File storage is not set up: set STORAGE_BACKEND=s3 and the S3_* "
        "variables (or EMERGENT_LLM_KEY for the Emergent store)"
    )


# =====================================================================
# Emergent backend
# =====================================================================

def init_storage() -> str:
    """Emergent only. Init once at startup. Returns reusable storage_key."""
    global _storage_key
    if _storage_key:
        return _storage_key
    emergent_key = os.environ.get("EMERGENT_LLM_KEY")
    if not emergent_key:
        raise RuntimeError("EMERGENT_LLM_KEY not set")
    resp = requests.post(
        f"{STORAGE_URL}/init",
        json={"emergent_key": emergent_key},
        timeout=30,
    )
    resp.raise_for_status()
    _storage_key = resp.json()["storage_key"]
    log.info("Storage initialized")
    return _storage_key


def emergent_put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data,
        timeout=180,
    )
    if resp.status_code == 403:
        # storage_key may be expired — re-init and retry once
        global _storage_key
        _storage_key = None
        key = init_storage()
        resp = requests.put(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key, "Content-Type": content_type},
            data=data,
            timeout=180,
        )
    resp.raise_for_status()
    return resp.json()


def emergent_get_object(path: str) -> tuple[bytes, str]:
    key = init_storage()
    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key},
        timeout=120,
    )
    if resp.status_code == 403:
        global _storage_key
        _storage_key = None
        key = init_storage()
        resp = requests.get(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key},
            timeout=120,
        )
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


def emergent_remove_object(path: str) -> str:
    """Delete one object from the Emergent store. Returns "deleted" or
    "already_gone"; raises on any other answer."""
    key = init_storage()
    resp = requests.delete(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key},
        timeout=30,
    )
    if resp.status_code in (200, 204):
        return "deleted"
    if resp.status_code == 404:
        return "already_gone"
    raise RuntimeError(f"http_{resp.status_code}")


# =====================================================================
# S3-compatible backend
# =====================================================================

_s3_client = None
_s3_client_config: tuple | None = None
_s3_lock = threading.Lock()


def s3_settings() -> dict:
    """The S3 settings from the environment. Secret values are returned for
    the client only; never log this dict."""
    style = (os.environ.get("S3_ADDRESSING_STYLE") or "virtual").strip().lower()
    if style not in ("virtual", "path", "auto"):
        style = "virtual"
    return {
        "endpoint_url": (os.environ.get("S3_ENDPOINT_URL") or "").strip() or None,
        "bucket": (os.environ.get("S3_BUCKET") or "").strip(),
        "access_key_id": (os.environ.get("S3_ACCESS_KEY_ID") or "").strip() or None,
        "secret_access_key": (os.environ.get("S3_SECRET_ACCESS_KEY") or "").strip() or None,
        "region": (os.environ.get("S3_REGION") or "").strip() or "auto",
        "addressing_style": style,
    }


def _s3() -> tuple:
    """(client, bucket). One client per settings combination, reused across
    threads (boto3 clients are safe to share between threads)."""
    global _s3_client, _s3_client_config
    cfg = s3_settings()
    if not cfg["bucket"]:
        raise RuntimeError("S3_BUCKET not set")
    sig = (cfg["endpoint_url"], cfg["access_key_id"], cfg["secret_access_key"],
           cfg["region"], cfg["addressing_style"])
    with _s3_lock:
        if _s3_client is None or _s3_client_config != sig:
            import boto3
            from botocore.config import Config
            _s3_client = boto3.client(
                "s3",
                endpoint_url=cfg["endpoint_url"],
                aws_access_key_id=cfg["access_key_id"],
                aws_secret_access_key=cfg["secret_access_key"],
                region_name=cfg["region"],
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": cfg["addressing_style"]},
                    connect_timeout=10,
                    read_timeout=180,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
            _s3_client_config = sig
        return _s3_client, cfg["bucket"]


def _is_not_found(exc: Exception) -> bool:
    resp = getattr(exc, "response", None) or {}
    code = str((resp.get("Error") or {}).get("Code") or "")
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


def s3_put_object(path: str, data: bytes, content_type: str) -> dict:
    client, bucket = _s3()
    resp = client.put_object(
        Bucket=bucket,
        Key=path,
        Body=data,
        ContentType=content_type or "application/octet-stream",
    )
    return {"path": path, "size": len(data), "etag": (resp or {}).get("ETag")}


def s3_get_object(path: str) -> tuple[bytes, str]:
    client, bucket = _s3()
    resp = client.get_object(Bucket=bucket, Key=path)
    body = resp["Body"]
    try:
        data = body.read()
    finally:
        try:
            body.close()
        except Exception:
            pass
    return data, resp.get("ContentType") or "application/octet-stream"


def s3_head_object(path: str) -> dict | None:
    """Size and type of one object, or None when it does not exist."""
    client, bucket = _s3()
    try:
        resp = client.head_object(Bucket=bucket, Key=path)
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise
    return {"size": resp.get("ContentLength"), "content_type": resp.get("ContentType")}


def s3_remove_object(path: str) -> str:
    """Delete one object from the bucket. S3 does not say whether the key
    existed, so check first to report "already_gone" honestly."""
    if s3_head_object(path) is None:
        return "already_gone"
    client, bucket = _s3()
    client.delete_object(Bucket=bucket, Key=path)
    return "deleted"


# =====================================================================
# Public functions (same names and shapes as before)
# =====================================================================

def put_object(path: str, data: bytes, content_type: str) -> dict:
    backend = storage_backend()
    if backend == BACKEND_S3:
        return s3_put_object(path, data, content_type)
    if backend == BACKEND_EMERGENT:
        return emergent_put_object(path, data, content_type)
    raise _unconfigured_error()


def get_object(path: str) -> tuple[bytes, str]:
    backend = storage_backend()
    if backend == BACKEND_S3:
        return s3_get_object(path)
    if backend == BACKEND_EMERGENT:
        return emergent_get_object(path)
    raise _unconfigured_error()


def remove_object(path: str) -> str:
    """Hard-delete one stored object. Only for operator scripts (migration
    006); the app itself soft-deletes through the file record. Returns
    "deleted" or "already_gone"; raises on failure."""
    backend = storage_backend()
    if backend == BACKEND_S3:
        return s3_remove_object(path)
    if backend == BACKEND_EMERGENT:
        return emergent_remove_object(path)
    raise _unconfigured_error()


# Day-1 audit findings 39/42/45 -- put_object / get_object are blocking
# calls (timeouts up to 180s). Called straight from an
# `async def` route they freeze the single event loop for EVERY user
# (same pattern as the weather 520 incident). Async routes must await
# these wrappers instead; they run the unchanged sync functions in a
# worker thread (same precedent as ms_project_internal_router).
async def aput_object(path: str, data: bytes, content_type: str) -> dict:
    return await asyncio.to_thread(put_object, path, data, content_type)


async def aget_object(path: str) -> tuple[bytes, str]:
    return await asyncio.to_thread(get_object, path)


# pt.77 -- small in-process LRU cache for served blobs (PDFs). Range requests
# from PDF.js issue many small GETs for the SAME object; without a cache each
# would re-download the entire object from object storage. Bounded by total
# bytes; evicts least-recently-used. Per-worker (fine: a miss just re-fetches).
from collections import OrderedDict as _OrderedDict

_BLOB_CACHE: "_OrderedDict[str, tuple]" = _OrderedDict()
_BLOB_CACHE_MAX_BYTES = 256 * 1024 * 1024
_blob_cache_bytes = 0


def get_object_cached(path: str) -> tuple:
    """get_object() with an LRU cache so repeated range requests for the same
    PDF don't re-download the full object each time."""
    global _blob_cache_bytes
    hit = _BLOB_CACHE.get(path)
    if hit is not None:
        _BLOB_CACHE.move_to_end(path)
        return hit
    data, ct = get_object(path)
    size = len(data)
    if size <= _BLOB_CACHE_MAX_BYTES:
        _BLOB_CACHE[path] = (data, ct)
        _blob_cache_bytes += size
        while _blob_cache_bytes > _BLOB_CACHE_MAX_BYTES and len(_BLOB_CACHE) > 1:
            _old_path, (old_data, _oct) = _BLOB_CACHE.popitem(last=False)
            _blob_cache_bytes -= len(old_data)
    return data, ct


async def aget_object_cached(path: str) -> tuple:
    """Async get_object_cached. Only the storage download runs in a
    worker thread; the cache itself is read and updated on the event
    loop thread, so it is never touched by two threads at once."""
    global _blob_cache_bytes
    hit = _BLOB_CACHE.get(path)
    if hit is not None:
        _BLOB_CACHE.move_to_end(path)
        return hit
    data, ct = await aget_object(path)
    size = len(data)
    if size <= _BLOB_CACHE_MAX_BYTES and path not in _BLOB_CACHE:
        _BLOB_CACHE[path] = (data, ct)
        _blob_cache_bytes += size
        while _blob_cache_bytes > _BLOB_CACHE_MAX_BYTES and len(_BLOB_CACHE) > 1:
            _old_path, (old_data, _oct) = _BLOB_CACHE.popitem(last=False)
            _blob_cache_bytes -= len(old_data)
    return data, ct


def make_path(tenant_id: str, kind: str, ext: str) -> str:
    """Build a tenant-scoped storage path. NO leading slash."""
    safe_ext = (ext or "bin").lstrip(".").lower()
    return f"{APP_NAME}/{kind}/{tenant_id}/{uuid.uuid4().hex}.{safe_ext}"


async def record_file(
    *,
    tenant_id: str,
    storage_path: str,
    original_filename: str,
    content_type: str,
    size: int,
    kind: str,
    actor_id: str | None = None,
) -> dict:
    db = get_db()
    doc = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "storage_path": storage_path,
        "original_filename": original_filename,
        "content_type": content_type,
        "size": size,
        "kind": kind,
        "is_deleted": False,
        "uploaded_by": actor_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.files.insert_one(doc.copy())
    return doc
