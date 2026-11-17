"""Where search data lives: collection names, chunk ids, content hashes and
the indexes for the three search collections.

  search_chunks  one row per piece of searchable text (with its vector)
  search_queue   one row per source record that needs (re)indexing
  search_state   small bookkeeping rows (listener leader lease, resume
                 token, nightly run day, one-time backfill marker)

All three can be rebuilt from the source data, so they are left out of
backups (routers/backup_router.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Optional

from pymongo.errors import OperationFailure

logger = logging.getLogger(__name__)

SEARCH_CHUNKS = "search_chunks"
SEARCH_QUEUE = "search_queue"
SEARCH_STATE = "search_state"
SEARCH_COLLECTIONS = (SEARCH_CHUNKS, SEARCH_QUEUE, SEARCH_STATE)

VECTOR_INDEX_NAME = "search_chunks_vector"
TEXT_INDEX_NAME = "search_chunks_text"
EMBED_DIMS = 1024

# Fields that go into the content hash besides the text itself. If any of
# them changes, the chunk is rewritten (and re-embedded when allowed).
HASH_FIELDS = (
    "tenant_id", "project_id", "kind", "source_collection", "source_oid",
    "source_id", "parent_id", "date", "activity_code", "trade_code", "status",
    "private", "owner_user_id", "allowed_roles", "title", "url",
    "embed_allowed", "model",
)


def chunk_id(collection: str, source_oid: str, n: int) -> str:
    return f"{collection}:{source_oid}:{n}"


def job_id(collection: str, source_oid: str) -> str:
    return f"{collection}:{source_oid}"


def content_hash(text: str, meta: dict) -> str:
    """sha256 of the text plus every filter field, in a fixed order."""
    payload = {"text": text or ""}
    for k in HASH_FIELDS:
        payload[k] = meta.get(k)
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def ensure_indexes(db) -> None:
    """Regular (non-Atlas) indexes. Safe to call many times."""
    ch = db[SEARCH_CHUNKS]
    await ch.create_index([("tenant_id", 1), ("source_collection", 1), ("source_oid", 1)])
    await ch.create_index([("source_collection", 1), ("source_oid", 1), ("chunk_no", 1)])
    await ch.create_index([("tenant_id", 1), ("project_id", 1), ("kind", 1), ("date", 1)])
    await ch.create_index([("tenant_id", 1), ("parent_id", 1)])
    q = db[SEARCH_QUEUE]
    await q.create_index([("status", 1), ("available_at", 1)])
    await q.create_index([("status", 1), ("lease_until", 1)])
    await q.create_index([("tenant_id", 1), ("project_id", 1)])


def vector_index_definition() -> dict:
    filters = ["tenant_id", "project_id", "kind", "date", "activity_code",
               "trade_code", "status", "private", "allowed_roles", "owner_user_id",
               "source_collection", "model"]
    return {"fields": [
        {"type": "vector", "path": "embedding", "numDimensions": EMBED_DIMS,
         "similarity": "dotProduct", "quantization": "scalar"},
        *({"type": "filter", "path": f} for f in filters),
    ]}


def text_index_definition() -> dict:
    text = {"type": "string", "analyzer": "lucene.english"}
    token = {"type": "token"}
    fields: dict[str, Any] = {
        "text": text, "title": text, "header": text,
        "date": [token, {"type": "string", "analyzer": "lucene.keyword"}],
        "private": {"type": "boolean"},
        "owner_user_id": token,
    }
    for f in ("tenant_id", "project_id", "kind", "activity_code", "trade_code",
              "status", "source_collection", "allowed_roles"):
        fields[f] = token
    # Exact codes ("A1240", "09 29 00") also match as whole strings.
    fields["activity_code"] = [token, {"type": "string", "analyzer": "lucene.keyword"}]
    return {"mappings": {"dynamic": False, "fields": fields}}


def _covers(actual: Any, wanted: Any) -> bool:
    """True when the definition Atlas reports already holds everything we
    want. Atlas adds its own defaults to a stored definition, so extra keys
    on its side are fine; every key and list item we set must match."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            k in actual and _covers(actual[k], v) for k, v in wanted.items())
    if isinstance(wanted, list):
        if not isinstance(actual, list) or len(actual) != len(wanted):
            return False
        # Order of fields is not meaningful; every wanted item must be there.
        return all(any(_covers(a, w) for a in actual) for w in wanted)
    return actual == wanted


async def ensure_search_indexes(db) -> dict:
    """Create the Atlas vector and keyword indexes if they are missing, and
    update them when our definition changed since they were made.

    Returns {"vector": status, "text": status}; a status starting with
    "failed" means it should be tried again later. A server without Atlas
    Search (a local mongod) answers with OperationFailure; that is logged
    and reported as "skipped". Never raises.
    """
    from pymongo.operations import SearchIndexModel

    out = {"vector": "skipped", "text": "skipped"}
    coll = db[SEARCH_CHUNKS]
    try:
        # The collection must exist before a search index can be created.
        if SEARCH_CHUNKS not in await db.list_collection_names():
            await db.create_collection(SEARCH_CHUNKS)
        existing: dict[str, dict] = {}
        async for ix in coll.list_search_indexes():
            existing[ix.get("name")] = ix
    except OperationFailure as exc:
        logger.info("search: this database has no Atlas Search support (%s)", exc.code)
        return out
    except Exception as exc:
        logger.warning("search: could not list search indexes: %s", type(exc).__name__)
        return {"vector": "failed", "text": "failed"}

    wanted = (
        ("vector", VECTOR_INDEX_NAME, vector_index_definition(), "vectorSearch"),
        ("text", TEXT_INDEX_NAME, text_index_definition(), "search"),
    )
    for key, name, definition, typ in wanted:
        try:
            if name in existing:
                current = existing[name].get("latestDefinition") or existing[name].get("definition")
                if _covers(current, definition):
                    out[key] = "exists"
                    continue
                await coll.update_search_index(name, definition)
                out[key] = "updated"
                logger.info("search: updated Atlas index %s to the current definition", name)
                continue
            await coll.create_search_index(SearchIndexModel(definition=definition, name=name, type=typ))
            out[key] = "created"
            logger.info("search: created Atlas index %s", name)
        except OperationFailure as exc:
            out[key] = f"failed:{exc.code}"
            logger.warning("search: could not create or update Atlas index %s (code %s)", name, exc.code)
        except Exception as exc:
            out[key] = "failed"
            logger.warning("search: could not create or update Atlas index %s: %s",
                           name, type(exc).__name__)
    return out


# A failed Atlas index step is tried again after this long (not on every
# worker tick, which would flood the log).
SEARCH_INDEX_RETRY_SECONDS = 600

_ready_lock: Optional[asyncio.Lock] = None
_ready_done: set[int] = set()
_indexes_done: set[int] = set()
_retry_at: dict[int, float] = {}


async def ensure_ready(db) -> None:
    """Create every search index once per process, the first time the
    feature is used (never at import, never blocking startup). If creating
    an Atlas index fails, it is tried again later instead of being
    forgotten until the next restart."""
    global _ready_lock
    key = id(db)
    if key in _ready_done:
        return
    if key in _retry_at and time.monotonic() < _retry_at[key]:
        return
    if _ready_lock is None:
        _ready_lock = asyncio.Lock()
    async with _ready_lock:
        if key in _ready_done:
            return
        if key not in _indexes_done:
            try:
                await ensure_indexes(db)
            except Exception as exc:
                logger.warning("search: regular index creation failed (will retry): %s", exc)
                return
            _indexes_done.add(key)
        out = await ensure_search_indexes(db)
        if any(str(v).startswith("failed") for v in out.values()):
            _retry_at[key] = time.monotonic() + SEARCH_INDEX_RETRY_SECONDS
            return
        _retry_at.pop(key, None)
        _ready_done.add(key)
