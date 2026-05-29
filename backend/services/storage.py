"""Emergent Object Storage helper.

Pattern from playbook: init once → reuse storage_key → put/get with
{app_name}/uploads/{tenant_id}/{uuid}.ext path scheme. DB is source of
truth; soft-delete via is_deleted=True on the file record.
"""
import os
import uuid
import logging
import requests
from datetime import datetime, timezone
from database import get_db

log = logging.getLogger(__name__)

STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"
APP_NAME = "ice-schedule-ops"

_storage_key: str | None = None


def init_storage() -> str:
    """Init once at startup. Returns reusable storage_key."""
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


def put_object(path: str, data: bytes, content_type: str) -> dict:
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


def get_object(path: str) -> tuple[bytes, str]:
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
