"""The search work queue (collection search_queue).

One row per source record, _id "<collection>:<Mongo _id as text>". Every
write to the record re-queues the same row, so a burst of edits collapses
into one job. Rows are kept after they are done: they remember the record's
app id, company and project, which the worker needs when the record is
later hard-deleted (a delete event carries only the Mongo _id). Once that
delete job is finished, the row itself is removed (finish_delete).

Each enqueue bumps a counter `gen`. A job claimed at gen 3 that finishes
after a new write moved gen to 4 goes back to pending instead of done, so
the newer change is never lost. A job already being worked on stays
claimed (no second worker picks it up at the same time).

Every update a worker makes to a claimed job also checks `claimed_by`, so a
worker whose lease ran out (and whose job another worker took over) can
never undo or overwrite the new worker's claim. Workers renew their leases
while they wait on the embedding service (renew_leases).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from pymongo import ReturnDocument, UpdateOne

from services.search.store import SEARCH_QUEUE, job_id

logger = logging.getLogger(__name__)

LEASE_SECONDS = 300
# How often a busy worker pushes its leases forward. Well under
# LEASE_SECONDS, so a slow embedding call never lets a lease run out.
HEARTBEAT_SECONDS = 60
BACKOFF_SECONDS = (30, 120, 600, 3600, 3600, 3600)
MAX_ATTEMPTS = 6
# Waiting for the embedding service is not the record's fault: those jobs
# are retried with the same backoff but never marked failed.
EMBED_WAIT_MAX_SECONDS = 3600
# A lease that runs out means the worker process vanished (crash, deploy),
# not that the record is bad, so it is counted apart from `attempts`. Only a
# record that keeps killing workers is given up on, after this many.
MAX_LEASE_EXPIRIES = 20

PENDING, CLAIMED, DONE, FAILED = "pending", "claimed", "done", "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def oid_text(value: Any) -> str:
    """The Mongo _id as text (hex for ObjectIds)."""
    return str(value)


def oid_value(text: str) -> Any:
    """Turn the stored text back into the _id to look up."""
    if isinstance(text, str) and len(text) == 24 and ObjectId.is_valid(text):
        return ObjectId(text)
    return text


def _lit(v: Any) -> dict:
    return {"$literal": v}


def _enqueue_update(collection: str, oid: str, *, tenant_id=None, project_id=None,
                    op: str = "upsert", now: Optional[datetime] = None) -> tuple[dict, list]:
    """Filter and pipeline update that (re)queues one record."""
    now = now or _now()
    claimed = {"$eq": ["$status", CLAIMED]}
    sets: dict[str, Any] = {
        "collection": _lit(collection),
        "source_oid": _lit(oid),
        "op": _lit(op),
        "enqueued_at": _lit(now),
        "gen": {"$add": [{"$ifNull": ["$gen", 0]}, 1]},
        "status": {"$cond": [claimed, CLAIMED, PENDING]},
        "available_at": {"$cond": [claimed, "$available_at", _lit(now)]},
        "attempts": {"$cond": [claimed, {"$ifNull": ["$attempts", 0]}, 0]},
        "embed_waits": {"$cond": [claimed, {"$ifNull": ["$embed_waits", 0]}, 0]},
        "lease_expiries": {"$cond": [claimed, {"$ifNull": ["$lease_expiries", 0]}, 0]},
    }
    if tenant_id is not None:
        sets["tenant_id"] = _lit(tenant_id)
    if project_id is not None:
        sets["project_id"] = _lit(project_id)
    return {"_id": job_id(collection, oid)}, [{"$set": sets}]


async def enqueue(db, collection: str, oid: Any, tenant_id: Optional[str] = None,
                  project_id: Optional[str] = None, op: str = "upsert") -> None:
    filt, pipe = _enqueue_update(collection, oid_text(oid), tenant_id=tenant_id,
                                 project_id=project_id, op=op)
    await db[SEARCH_QUEUE].update_one(filt, pipe, upsert=True)


async def enqueue_many(db, collection: str, oids: list, op: str = "upsert") -> int:
    if not oids:
        return 0
    now = _now()
    ops = []
    for oid in oids:
        filt, pipe = _enqueue_update(collection, oid_text(oid), op=op, now=now)
        ops.append(UpdateOne(filt, pipe, upsert=True))
    await db[SEARCH_QUEUE].bulk_write(ops, ordered=False)
    return len(ops)


async def enqueue_filter(db, collection: str, filt: dict, limit: int = 20000) -> int:
    """Re-queue every row of `collection` matching an app-level filter
    (used for ChunkResult.requeue, e.g. ("daily_reports", {"id": rid}))."""
    oids = []
    n = 0
    async for row in db[collection].find(filt, {"_id": 1}).limit(limit):
        oids.append(row["_id"])
        if len(oids) >= 500:
            n += await enqueue_many(db, collection, oids)
            oids = []
    n += await enqueue_many(db, collection, oids)
    return n


async def reap_expired(db) -> int:
    """Put jobs whose worker vanished (lease ran out) back to pending.

    Lease expiries are counted in `lease_expiries`, not `attempts`: they
    say the worker died, not that the record failed. Only a record whose
    lease runs out MAX_LEASE_EXPIRIES times in a row is marked failed."""
    now = _now()
    q = db[SEARCH_QUEUE]
    n = 0
    async for j in q.find({"status": CLAIMED, "lease_until": {"$lt": now}},
                          {"_id": 1, "lease_expiries": 1, "claimed_by": 1}).limit(1000):
        expiries = int(j.get("lease_expiries") or 0) + 1
        if expiries >= MAX_LEASE_EXPIRIES:
            upd = {"status": FAILED, "lease_expiries": expiries, "lease_until": None,
                   "claimed_by": None,
                   "last_error": "the worker stopped while holding this job too many times"}
        else:
            upd = {"status": PENDING, "lease_expiries": expiries, "lease_until": None,
                   "claimed_by": None, "available_at": now}
        res = await q.update_one({"_id": j["_id"], "status": CLAIMED,
                                  "claimed_by": j.get("claimed_by"),
                                  "lease_until": {"$lt": now}}, {"$set": upd})
        n += res.modified_count
    if n:
        logger.info("search queue: %d expired job(s) put back", n)
    return n


def _held(job: dict) -> dict:
    """Filter matching the job only while this worker still holds it."""
    return {"_id": job["_id"], "status": CLAIMED, "claimed_by": job.get("claimed_by")}


async def renew_leases(db, jobs: list[dict]) -> set:
    """Push the lease forward for every job this worker still holds.
    Returns the _ids still held. A job missing from the set was taken over
    by another worker, and this worker must not write anything for it."""
    if not jobs:
        return set()
    until = _now() + timedelta(seconds=LEASE_SECONDS)
    held = set()
    for job in jobs:
        res = await db[SEARCH_QUEUE].update_one(_held(job), {"$set": {"lease_until": until}})
        if res.matched_count == 1:
            held.add(job["_id"])
    return held


async def still_holds(db, job: dict) -> bool:
    """True (and the lease renewed) when this worker still holds the job."""
    return job["_id"] in await renew_leases(db, [job])


async def claim_batch(db, worker_id: str, n: int = 64) -> list[dict]:
    """Atomically claim up to n due jobs, oldest first."""
    out: list[dict] = []
    q = db[SEARCH_QUEUE]
    for _ in range(max(0, n)):
        now = _now()
        job = await q.find_one_and_update(
            {"status": PENDING, "available_at": {"$lte": now}},
            [{"$set": {"status": CLAIMED, "claimed_by": _lit(worker_id),
                       "claimed_at": _lit(now),
                       "lease_until": _lit(now + timedelta(seconds=LEASE_SECONDS)),
                       "claimed_gen": {"$ifNull": ["$gen", 0]}}}],
            sort=[("available_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if not job:
            break
        out.append(job)
    return out


async def _release_if_requeued(db, job: dict) -> bool:
    """If a new write arrived while the job ran, put it back to pending.
    Returns True when that happened."""
    res = await db[SEARCH_QUEUE].update_one(
        {**_held(job), "gen": {"$ne": job.get("claimed_gen")}},
        {"$set": {"status": PENDING, "available_at": _now(), "lease_until": None,
                  "claimed_by": None}},
    )
    return res.modified_count == 1


async def mark_done(db, job: dict, *, tenant_id=None, project_id=None, source_id=None,
                    requeue: Optional[list] = None) -> None:
    sets: dict[str, Any] = {"status": DONE, "lease_until": None, "claimed_by": None,
                            "attempts": 0, "embed_waits": 0, "last_error": None,
                            "done_at": _now()}
    # Remember which other records this one feeds (e.g. a crew line feeds
    # its report's manpower piece), so a later hard delete -- when the row
    # can no longer be read -- still rebuilds them.
    sets["requeue_on_delete"] = [[c, f] for c, f in (requeue or [])][:50]
    if tenant_id is not None:
        sets["tenant_id"] = tenant_id
    if project_id is not None:
        sets["project_id"] = project_id
    if source_id is not None:
        sets["source_id"] = source_id
    sets["lease_expiries"] = 0
    res = await db[SEARCH_QUEUE].update_one({**_held(job), "gen": job.get("claimed_gen")},
                                            {"$set": sets})
    if res.modified_count == 0:
        # A newer write arrived meanwhile: keep the remembered ids, run again.
        # (Nothing at all happens when another worker has taken the job over.)
        keep = {k: v for k, v in sets.items() if k in ("tenant_id", "project_id", "source_id")}
        if keep:
            await db[SEARCH_QUEUE].update_one(_held(job), {"$set": keep})
        await _release_if_requeued(db, job)


async def finish_delete(db, job: dict) -> bool:
    """A hard-delete job is finished and the record's chunks are gone:
    remove the queue row itself so the queue does not grow forever.
    Returns False (row kept) when a new write arrived meanwhile or another
    worker holds the job; the caller then falls back to mark_done."""
    res = await db[SEARCH_QUEUE].delete_one({**_held(job), "gen": job.get("claimed_gen")})
    return res.deleted_count == 1


async def delete_scope(db, tenant_id: Optional[str], project_id: Optional[str] = None) -> int:
    """Remove waiting queue rows of a deleted company or project. Rows being
    worked on right now are left alone; they finish on their own."""
    if not tenant_id:
        return 0
    filt: dict[str, Any] = {"tenant_id": tenant_id, "status": {"$ne": CLAIMED}}
    if project_id:
        filt["project_id"] = project_id
    return (await db[SEARCH_QUEUE].delete_many(filt)).deleted_count


def backoff_seconds(attempt: int) -> int:
    i = max(1, attempt) - 1
    return BACKOFF_SECONDS[min(i, len(BACKOFF_SECONDS) - 1)]


async def mark_failed(db, job: dict, error: str, *, embed_wait: bool = False) -> str:
    """Record a failure and schedule the retry. Returns the new status.

    embed_wait=True means the embedding service was unavailable: retry with
    backoff (capped at an hour) and never give up on the record."""
    error = (error or "")[:500]
    now = _now()
    if await _release_if_requeued(db, job):
        await db[SEARCH_QUEUE].update_one({"_id": job["_id"], "status": PENDING},
                                          {"$set": {"last_error": error}})
        return PENDING
    if embed_wait:
        waits = int(job.get("embed_waits") or 0) + 1
        delay = min(backoff_seconds(waits), EMBED_WAIT_MAX_SECONDS)
        sets = {"status": PENDING, "embed_waits": waits,
                "available_at": now + timedelta(seconds=delay)}
    else:
        attempts = int(job.get("attempts") or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            sets = {"status": FAILED, "attempts": attempts}
            logger.warning("search queue: job %s failed for good: %s", job["_id"], error)
        else:
            sets = {"status": PENDING, "attempts": attempts,
                    "available_at": now + timedelta(seconds=backoff_seconds(attempts))}
    sets.update({"last_error": error, "lease_until": None, "claimed_by": None})
    res = await db[SEARCH_QUEUE].update_one(_held(job), {"$set": sets})
    if res.matched_count == 0:
        return "lost"  # another worker took the job over; leave its claim alone
    return sets["status"]


async def failed_count(db) -> int:
    return await db[SEARCH_QUEUE].count_documents({"status": FAILED})
