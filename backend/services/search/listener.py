"""Change-stream listener: turns every write on an indexed collection into a
search queue job.

Only one app replica listens at a time. The leader holds a lease row in
search_state and renews it while it listens; if it stops (crash, deploy),
another replica takes over when the lease runs out and continues from the
saved resume token, so no write is missed. Each event becomes
enqueue("<collection>", "<Mongo _id>"); the worker reads the row itself, so
duplicate or no-op events cost almost nothing (the content hash check).

If the saved token is too old to resume from (the database no longer has
that history), the token is cleared and a full re-queue of every indexed
record is requested (services.search.reconcile).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

from services.search import queue
from services.search.registry import INDEXED_COLLECTIONS
from services.search.store import SEARCH_STATE

logger = logging.getLogger(__name__)

LEADER_ID = "listener_leader"
RESUME_ID = "listener_resume"
LEASE_SECONDS = 30
SAVE_EVERY_SECONDS = 3
FLAG_OFF_SLEEP_SECONDS = 15
FOLLOWER_SLEEP_SECONDS = 10
# Wait before the first look at the flag, like the worker and the nightly
# loop: the switches saved in the app's Settings page load a little after
# startup, and until then only the server environment's value is known.
FIRST_SLEEP_SECONDS = 75
MAX_BACKOFF_SECONDS = 300

WATCHED_OPS = ("insert", "update", "replace", "delete")
# Server error codes meaning "cannot resume from this token".
_RESUME_LOST_CODES = {280, 286}
# "The $changeStream stage is only supported on replica sets" (local mongod).
_NO_CHANGE_STREAM_CODES = {40573}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_pipeline(collections: list[str]) -> list[dict]:
    return [{"$match": {
        "ns.coll": {"$in": list(collections)},
        "operationType": {"$in": list(WATCHED_OPS)},
    }}]


def event_to_job(event: dict, indexed: Optional[set] = None) -> Optional[tuple[str, str, str]]:
    """(collection, Mongo _id as text, op) for one change event, or None
    when the event is not for an indexed record."""
    op_type = event.get("operationType")
    if op_type not in WATCHED_OPS:
        return None
    coll = (event.get("ns") or {}).get("coll")
    if not coll or (indexed is not None and coll not in indexed):
        return None
    key = event.get("documentKey") or {}
    if "_id" not in key:
        return None
    return coll, queue.oid_text(key["_id"]), "delete" if op_type == "delete" else "upsert"


async def handle_event(db, event: dict, indexed: Optional[set] = None) -> bool:
    job = event_to_job(event, indexed)
    if not job:
        return False
    coll, oid, op = job
    await queue.enqueue(db, coll, oid, op=op)
    return True


async def try_lead(db, holder: str) -> bool:
    """Take or renew the leader lease. True when we hold it."""
    now = _now()
    try:
        doc = await db[SEARCH_STATE].find_one_and_update(
            {"_id": LEADER_ID, "$or": [{"holder": holder}, {"lease_until": {"$lt": now}}]},
            {"$set": {"holder": holder, "lease_until": now + timedelta(seconds=LEASE_SECONDS),
                      "renewed_at": now}},
            upsert=True, return_document=True,
        )
    except DuplicateKeyError:
        return False  # someone else holds a live lease
    return bool(doc) and doc.get("holder") == holder


async def release_lead(db, holder: str) -> None:
    try:
        await db[SEARCH_STATE].update_one({"_id": LEADER_ID, "holder": holder},
                                          {"$set": {"lease_until": _now()}})
    except Exception:
        pass


async def load_token(db) -> Optional[Any]:
    doc = await db[SEARCH_STATE].find_one({"_id": RESUME_ID})
    return (doc or {}).get("token")


async def save_token(db, token: Any) -> None:
    await db[SEARCH_STATE].update_one({"_id": RESUME_ID},
                                      {"$set": {"token": token, "saved_at": _now()}}, upsert=True)


async def clear_token(db) -> None:
    await db[SEARCH_STATE].update_one({"_id": RESUME_ID},
                                      {"$set": {"token": None, "saved_at": _now()}}, upsert=True)


def _holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


async def _listen(db, holder: str, flag_on) -> None:
    """Listen until the lease is lost or the flag turns off."""
    indexed = INDEXED_COLLECTIONS()
    if not indexed:
        await asyncio.sleep(FOLLOWER_SLEEP_SECONDS)
        return
    indexed_set = set(indexed)
    token = await load_token(db)
    kwargs: dict = {"max_await_time_ms": 1000}
    if token:
        kwargs["resume_after"] = token
    try:
        async with db.watch(build_pipeline(indexed), **kwargs) as stream:
            last_save = time.monotonic()
            dirty = False
            while True:
                change = await stream.try_next()
                if change is not None:
                    await handle_event(db, change, indexed_set)
                    dirty = True
                if time.monotonic() - last_save >= SAVE_EVERY_SECONDS:
                    tok = stream.resume_token
                    if tok is not None and (dirty or token is None):
                        await save_token(db, tok)
                        token = tok
                    dirty = False
                    last_save = time.monotonic()
                    if not await try_lead(db, holder) or not flag_on():
                        return
    except OperationFailure as exc:
        if token and (exc.code in _RESUME_LOST_CODES or "resume" in str(exc).lower()):
            logger.warning("search listener: cannot resume from the saved point (code %s); "
                           "clearing it and asking for a full re-queue", exc.code)
            await clear_token(db)
            from services.search.reconcile import request_full_reconcile
            await request_full_reconcile(db, reason="change stream history lost")
            return
        raise


async def run_listener_loop(get_db_fn) -> None:
    """Long-running loop started from server.py lifespan. Idles while the
    flag is off; never raises except on cancel."""
    from services.feature_flags import search_everything_enabled
    from services.search.store import ensure_ready

    holder = _holder_id()
    backoff = 5
    leading = False
    await asyncio.sleep(FIRST_SLEEP_SECONDS)
    while True:
        db = None
        try:
            if not search_everything_enabled():
                if leading:
                    await release_lead(get_db_fn(), holder)
                    leading = False
                await asyncio.sleep(FLAG_OFF_SLEEP_SECONDS)
                continue
            db = get_db_fn()
            await ensure_ready(db)
            leading = await try_lead(db, holder)
            if not leading:
                await asyncio.sleep(FOLLOWER_SLEEP_SECONDS)
                continue
            await _listen(db, holder, search_everything_enabled)
            backoff = 5
        except asyncio.CancelledError:
            logger.info("search listener cancelled")
            if leading and db is not None:
                await release_lead(db, holder)
            raise
        except OperationFailure as exc:
            if exc.code in _NO_CHANGE_STREAM_CODES:
                logger.info("search listener: this database has no change streams; waiting")
                await asyncio.sleep(MAX_BACKOFF_SECONDS)
            else:
                logger.warning("search listener failed (code %s); retrying in %ss", exc.code, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except (PyMongoError, Exception) as exc:
            logger.warning("search listener failed (%s); retrying in %ss", type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
