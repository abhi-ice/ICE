"""Backfill and the nightly check that keeps the search index honest.

* backfill_enqueue: queue every row of every indexed collection. It only
  queues; the worker does the work and skips rows whose chunks are already
  up to date (content hash), so running it again is cheap.
* nightly (about 02:00 New York time, one replica only):
    - queue rows whose updated_at moved since the last run (rows with no
      updated_at are left to the change listener and the weekly pass),
    - once a week instead: queue every row (a full pass; cheap, because
      the worker skips chunks whose content hash did not change). This
      catches anything the listener missed and records that copy text
      from another record without being written themselves,
    - delete chunks whose source row no longer exists, and chunks of a
      project or company that no longer exists,
    - re-check each project's AI-sharing setting and re-queue on change,
    - log how many queue rows have failed for good.
* The first time the flag is seen ON, a one-time full backfill runs
  (recorded in search_state, so it happens once per database).
* The listener can ask for a full re-queue (request_full_reconcile) when it
  lost its place in the change history.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pymongo.errors import DuplicateKeyError

from services.search import gate, queue
from services.search.registry import INDEXED_COLLECTIONS
from services.search.store import SEARCH_CHUNKS, SEARCH_STATE
from services.search.types import ChunkContext

logger = logging.getLogger(__name__)

NIGHTLY_ID = "nightly"
BACKFILL_ID = "backfill"
FULL_REQUEST_ID = "full_reconcile_request"
NIGHTLY_HOUR = 2
TIMEZONE = "America/New_York"
TICK_SECONDS = 60
FIRST_SLEEP_SECONDS = 90
FULL_PASS_DAYS = 7
PAGE = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def backfill_enqueue(db, collections: Optional[list[str]] = None) -> int:
    """Queue every row of every indexed collection. Returns rows queued."""
    total = 0
    for coll in collections or INDEXED_COLLECTIONS():
        batch = []
        async for row in db[coll].find({}, {"_id": 1}).batch_size(PAGE):
            batch.append(row["_id"])
            if len(batch) >= PAGE:
                total += await queue.enqueue_many(db, coll, batch)
                batch = []
        total += await queue.enqueue_many(db, coll, batch)
    logger.info("search backfill: queued %d record(s)", total)
    return total


async def enqueue_changed_since(db, since: datetime, collections: Optional[list[str]] = None) -> int:
    """Queue rows whose updated_at is at or after `since`.

    updated_at is stored three ways in real data: a real date, ISO text
    with a time zone, and ISO text without one. Text is compared by day
    (from the day before `since`), so any text form matches and a missing
    time zone never hides a change; that re-queues a little more, which the
    content hash makes cheap. Rows with no updated_at at all are not queued
    here: the change listener queues them on every write, and the weekly
    full pass catches anything it missed."""
    since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    since_day = (since_utc - timedelta(days=1)).date().isoformat()
    filt = {"$or": [
        {"updated_at": {"$gte": since_day}},   # ISO text, with or without a time zone
        {"updated_at": {"$gte": since_utc}},   # a real date
    ]}
    total = 0
    for coll in collections or INDEXED_COLLECTIONS():
        total += await queue.enqueue_filter(db, coll, filt, limit=10_000_000)
    return total


def _id_forms(oids: list[str]) -> list:
    out = []
    for o in oids:
        out.append(o)
        v = queue.oid_value(o)
        if v is not o:
            out.append(v)
    return out


async def delete_orphan_scopes(db) -> int:
    """Delete chunks (and waiting queue rows) whose company or project no
    longer exists. Deleting a project or closing a company does not delete
    every record under it, so the record-by-record check alone would keep
    those chunks searchable."""
    ch = db[SEARCH_CHUNKS]
    removed = 0
    pairs = await ch.aggregate([
        {"$group": {"_id": {"t": "$tenant_id", "p": "$project_id"}}},
    ]).to_list(None)
    tenant_ok: dict = {}
    for g in pairs:
        t, p = g["_id"].get("t"), g["_id"].get("p")
        if t not in tenant_ok:
            tenant_ok[t] = bool(t) and await db.tenants.find_one({"id": t}, {"_id": 1}) is not None
        if not tenant_ok[t]:
            continue  # handled below for the whole company
        if p and await db.projects.find_one({"id": p, "tenant_id": t}, {"_id": 1}) is None:
            removed += (await ch.delete_many({"tenant_id": t, "project_id": p})).deleted_count
            await queue.delete_scope(db, t, p)
    for t, ok in tenant_ok.items():
        if not ok:
            removed += (await ch.delete_many({"tenant_id": t})).deleted_count
            await queue.delete_scope(db, t)
    if removed:
        logger.info("search reconcile: deleted %d chunk(s) of deleted projects or companies", removed)
    return removed


async def delete_orphan_chunks(db) -> int:
    """Delete chunks whose source row is gone, whose collection is no
    longer indexed, or whose project or company no longer exists. Source
    rows are checked in batches by source_oid."""
    indexed = set(INDEXED_COLLECTIONS())
    ch = db[SEARCH_CHUNKS]
    removed = await delete_orphan_scopes(db)
    colls = await ch.distinct("source_collection")
    for coll in colls:
        if coll not in indexed:
            removed += (await ch.delete_many({"source_collection": coll})).deleted_count
            continue
        pending: list[str] = []
        seen: set[str] = set()

        async def flush(oids: list[str]) -> int:
            if not oids:
                return 0
            found = set()
            async for r in db[coll].find({"_id": {"$in": _id_forms(oids)}}, {"_id": 1}):
                found.add(queue.oid_text(r["_id"]))
            gone = [o for o in oids if o not in found]
            if not gone:
                return 0
            res = await ch.delete_many({"source_collection": coll, "source_oid": {"$in": gone}})
            return res.deleted_count

        async for c in ch.find({"source_collection": coll}, {"source_oid": 1}).sort("source_oid", 1):
            o = c.get("source_oid")
            if o is None or o in seen:
                continue
            seen.add(o)
            pending.append(o)
            if len(pending) >= PAGE:
                removed += await flush(pending)
                pending = []
        removed += await flush(pending)
    if removed:
        logger.info("search reconcile: deleted %d orphan chunk(s)", removed)
    return removed


async def recheck_gates(db) -> int:
    """Re-check the AI-sharing answer for every project that has chunks
    (catches company or project switches flipped without any record write)."""
    from services.search.worker import requeue_project_sources
    ctx = ChunkContext(db)
    n = 0
    pairs = await db[SEARCH_CHUNKS].aggregate([
        {"$group": {"_id": {"t": "$tenant_id", "p": "$project_id"}}},
    ]).to_list(None)
    for g in pairs:
        t, p = g["_id"].get("t"), g["_id"].get("p")
        allowed = await gate.project_allows_embedding(ctx, t, p)
        if not allowed:
            # Sharing with the outside AI service is off for this project
            # (controlled government information, or the company turned AI
            # off): drop its stored vectors at once, not record by record.
            await db[SEARCH_CHUNKS].update_many(
                {"tenant_id": t, "project_id": p, "embedding": {"$ne": None}},
                {"$set": {"embedding": None, "embedded": False, "model": None}})
        if await gate.note_gate_state(db, t, p, allowed):
            n += await requeue_project_sources(db, t, p)
    return n


async def run_nightly(db, since: Optional[datetime], full: bool = False) -> dict:
    """since=None or full=True queues every row (the weekly full pass)."""
    out = {"full": bool(full or since is None)}
    if out["full"]:
        out["queued"] = await backfill_enqueue(db)
    else:
        out["queued"] = await enqueue_changed_since(db, since)
    out["orphans_deleted"] = await delete_orphan_chunks(db)
    out["gate_requeued"] = await recheck_gates(db)
    out["failed_jobs"] = await queue.failed_count(db)
    if out["failed_jobs"]:
        logger.warning("search reconcile: %d queue job(s) have failed for good", out["failed_jobs"])
    logger.info("search nightly reconcile: %s", out)
    return out


async def request_full_reconcile(db, reason: str = "") -> None:
    await db[SEARCH_STATE].update_one(
        {"_id": FULL_REQUEST_ID},
        {"$set": {"requested": True, "reason": reason, "requested_at": _now()}},
        upsert=True,
    )


async def _claim_full_request(db) -> bool:
    res = await db[SEARCH_STATE].update_one(
        {"_id": FULL_REQUEST_ID, "requested": True},
        {"$set": {"requested": False, "claimed_at": _now()}},
    )
    return res.modified_count == 1


async def _claim_one_time_backfill(db) -> bool:
    """True exactly once per database (across replicas)."""
    now = _now()
    try:
        await db[SEARCH_STATE].insert_one({"_id": BACKFILL_ID, "started_at": now})
        return True
    except DuplicateKeyError:
        pass
    # A run that started long ago and never finished (crash, deploy) is redone.
    res = await db[SEARCH_STATE].update_one(
        {"_id": BACKFILL_ID, "done_at": None, "started_at": {"$lt": now - timedelta(hours=2)}},
        {"$set": {"started_at": now}},
    )
    return res.modified_count == 1


def _local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:  # pragma: no cover - no tz database
        return datetime.now(timezone.utc) - timedelta(hours=5)


async def _claim_nightly(db, day: str) -> Optional[dict]:
    """Claim tonight's run. Returns the previous state row, or None when
    another replica already ran (or is running) tonight."""
    try:
        prev = await db[SEARCH_STATE].find_one_and_update(
            {"_id": NIGHTLY_ID, "last_day": {"$ne": day}},
            {"$set": {"last_day": day, "started_at": _now()}},
            upsert=True,
        )
    except DuplicateKeyError:
        return None
    return prev or {}


async def nightly_tick(db) -> Optional[str]:
    """One pass of the loop. Returns what ran (for logs and tests)."""
    if await _claim_one_time_backfill(db):
        started = _now()
        await backfill_enqueue(db)
        await db[SEARCH_STATE].update_one({"_id": BACKFILL_ID}, {"$set": {"done_at": _now()}})
        # The next nightly run only needs what changed after this point, and
        # the next full pass is a week away.
        await db[SEARCH_STATE].update_one({"_id": NIGHTLY_ID},
                                          {"$set": {"last_started_at": started,
                                                    "last_full_at": started}}, upsert=True)
        return "backfill"
    if await _claim_full_request(db):
        await backfill_enqueue(db)
        return "full"
    local = _local_now()
    if local.hour < NIGHTLY_HOUR:
        return None
    day = local.date().isoformat()
    prev = await _claim_nightly(db, day)
    if prev is None:
        return None
    since = prev.get("last_started_at")
    last_full = prev.get("last_full_at")
    started = _now()
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if last_full is not None and last_full.tzinfo is None:
        last_full = last_full.replace(tzinfo=timezone.utc)
    full = since is None or last_full is None or started - last_full >= timedelta(days=FULL_PASS_DAYS)
    # Look back a little further than the last run so nothing slips between.
    await run_nightly(db, since - timedelta(minutes=30) if since else None, full=full)
    sets = {"last_started_at": started, "finished_at": _now()}
    if full:
        sets["last_full_at"] = started
    await db[SEARCH_STATE].update_one({"_id": NIGHTLY_ID}, {"$set": sets})
    return "full_nightly" if full else "nightly"


async def run_nightly_loop(get_db_fn) -> None:
    """Long-running loop started from server.py lifespan. Idles while the
    flag is off; never raises except on cancel."""
    from services.feature_flags import search_everything_enabled
    from services.search.store import ensure_ready

    await asyncio.sleep(FIRST_SLEEP_SECONDS)
    while True:
        try:
            if search_everything_enabled():
                db = get_db_fn()
                await ensure_ready(db)
                await nightly_tick(db)
            await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("search nightly loop cancelled")
            raise
        except Exception as exc:
            logger.warning("search nightly tick failed (non-fatal): %s", exc)
            await asyncio.sleep(TICK_SECONDS)
