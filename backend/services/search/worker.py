"""The search worker: turns queued records into stored, embedded chunks.

For each claimed job "<collection>:<_id>":
  1. Load the source row by its Mongo _id.
  2. Missing row (hard delete): remove its chunks. A deleted project also
     takes every chunk filed under it. The queue row is removed afterwards.
  3. Otherwise call the collection's chunker, check the outside-provider
     gate, and compare each new chunk's content hash with the stored one.
  4. Only chunks whose hash changed are rewritten; only those that are
     allowed to leave are sent to Voyage, all in one embed call per batch.
  5. Chunk numbers that no longer exist are deleted, re-queue requests from
     the chunker are queued, and the job is marked done.
  6. Whenever a record's chunks changed or went away, the records whose
     chunks point to it through parent_id (activity notes of a daily
     report, mitigations of a delay, ...) are re-queued, so each child's
     chunker decides again (they may copy the parent's date, status or
     header, or disappear with it). When a project's chunk changed, every
     record in that project is re-queued (its name is in their headers).

If Voyage is unavailable, jobs that need a vector stay pending with backoff
(they are never marked failed for that reason). If Voyage rejects the
input (an HTTP 4xx other than 401/403/429), the batch is split until the
one job it rejects is found; only that job counts a failed attempt.

The worker renews its leases while it works, and checks that it still holds
a job right before writing, so a worker whose job was taken over never
writes over newer results. Everything here is behind the default-OFF flag
FF_SEARCH_EVERYTHING.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from pymongo import DeleteMany, ReplaceOne

from services.search import gate, queue
from services.search.registry import get_registry
from services.search.store import SEARCH_CHUNKS, chunk_id, content_hash
from services.search.types import ChunkContext, ChunkResult

logger = logging.getLogger(__name__)

BATCH_SIZE = 64
POLL_SECONDS = 3
FIRST_SLEEP_SECONDS = 60
FLAG_OFF_SLEEP_SECONDS = 15
GATE_CHECK_SECONDS = 300

Embedder = Callable[..., Awaitable[tuple[list[list[float]], int]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _model_name() -> str:
    try:
        from services.search.voyage_client import EMBED_MODEL
        return EMBED_MODEL
    except Exception:  # pragma: no cover
        return "voyage-4"


async def _default_embed(texts, input_type="document"):
    from services.search import voyage_client
    return await voyage_client.embed(texts, input_type=input_type)


def _is_voyage_unavailable(exc: BaseException) -> bool:
    try:
        from services.search.voyage_client import VoyageUnavailable
        return isinstance(exc, VoyageUnavailable)
    except Exception:  # pragma: no cover
        return False


# 401/403 mean the key is wrong or not allowed (not the record's fault, so
# those jobs wait like an outage); 408/429 are "slow down / try again".
_NOT_A_REJECTION = {401, 403, 408, 429}


def _rejected_status(exc: BaseException) -> Optional[int]:
    """The HTTP status when Voyage refused this input (a 4xx that says the
    request itself was bad: 400, 413, 422 ...), else None.

    Uses the error's `status_code` when it has one; otherwise reads the
    status from a VoyageUnavailable message ("Voyage returned HTTP 400")."""
    code = getattr(exc, "status_code", None)
    if code is None and _is_voyage_unavailable(exc):
        m = re.search(r"HTTP (\d{3})", str(exc))
        code = m.group(1) if m else None
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        return None
    if code is not None and 400 <= code < 500 and code not in _NOT_A_REJECTION:
        return code
    return None


class _Plan:
    """What one job will write."""

    def __init__(self, job: dict):
        self.job = job
        self.collection: str = job.get("collection") or job["_id"].split(":", 1)[0]
        self.source_oid: str = job.get("source_oid") or job["_id"].split(":", 1)[1]
        self.docs: list[dict] = []          # chunk documents to write
        self.need_vector: list[int] = []    # indexes into docs that need embedding
        self.keep_ids: list[str] = []       # every chunk id that should exist
        self.result: Optional[ChunkResult] = None
        self.deleted = False                # source row is gone
        self.error: Optional[str] = None
        self.embed_failed = False
        # After a hard delete: the record's company and app id (for re-queuing
        # the records that point to it).
        self.gone_tenant_id: Optional[str] = None
        self.gone_source_id: Optional[str] = None


def _source_filter(collection: str, source_oid: str) -> dict:
    return {"source_collection": collection, "source_oid": source_oid}


async def _delete_source(db, plan: _Plan) -> int:
    """Hard delete: remove this record's chunks. Records that point to it
    through parent_id are re-queued afterwards (not deleted here: photos,
    safety events and mitigations outlive their parent and decide for
    themselves). A deleted project also takes every chunk filed under it,
    because some of its records are not deleted with it."""
    ch = db[SEARCH_CHUNKS]
    filt = _source_filter(plan.collection, plan.source_oid)
    # The record's app id: from one of its chunks, else from the done job
    # (the queue row remembers it).
    any_chunk = await ch.find_one(filt, {"source_id": 1, "tenant_id": 1})
    source_id = (any_chunk or {}).get("source_id") or plan.job.get("source_id")
    tenant_id = (any_chunk or {}).get("tenant_id") or plan.job.get("tenant_id")
    plan.gone_source_id, plan.gone_tenant_id = source_id, tenant_id
    n = (await ch.delete_many(filt)).deleted_count
    if plan.collection == "projects" and source_id and tenant_id:
        n += (await ch.delete_many({"tenant_id": tenant_id, "project_id": source_id})).deleted_count
        await queue.delete_scope(db, tenant_id, source_id)
    return n


async def _build_plan(db, ctx: ChunkContext, job: dict, registry: dict,
                      gate_seen: dict) -> _Plan:
    plan = _Plan(job)
    entry = registry.get(plan.collection)
    if entry is None or entry.decision != "index" or entry.chunker is None:
        plan.result = ChunkResult(tenant_id=None, project_id=None, source_id=None)
        return plan  # not indexed (any more): its chunks are removed

    row = await db[plan.collection].find_one({"_id": queue.oid_value(plan.source_oid)})
    if row is None:
        plan.deleted = True
        return plan

    result = await entry.chunker(ctx, row)
    if result is None or not result.tenant_id:
        # Nothing to index (soft-deleted, orphan, empty, or no company).
        plan.result = ChunkResult(
            tenant_id=getattr(result, "tenant_id", None),
            project_id=getattr(result, "project_id", None),
            source_id=getattr(result, "source_id", None),
            requeue=list(getattr(result, "requeue", None) or []),
        )
        return plan
    plan.result = result

    allowed = await gate.should_embed(ctx, result.tenant_id, result.project_id,
                                      plan.collection, row)
    key = (result.tenant_id, result.project_id)
    if key not in gate_seen:
        gate_seen[key] = await gate.project_allows_embedding(ctx, *key)

    existing = {}
    async for c in db[SEARCH_CHUNKS].find(_source_filter(plan.collection, plan.source_oid),
                                          {"_id": 1, "content_hash": 1}):
        existing[c["_id"]] = c.get("content_hash")

    model = _model_name() if allowed else None
    now = _now()
    for n, chunk in enumerate(result.chunks):
        text = chunk.text
        if not text:
            continue
        cid = chunk_id(plan.collection, plan.source_oid, n)
        doc: dict[str, Any] = {
            "_id": cid,
            "tenant_id": result.tenant_id,
            "project_id": result.project_id,
            "kind": chunk.kind,
            "source_collection": plan.collection,
            "source_oid": plan.source_oid,
            "source_id": result.source_id,
            "chunk_no": n,
            "parent_id": chunk.parent_id,
            "date": chunk.date,
            "activity_code": list(chunk.activity_code or []),
            "trade_code": list(chunk.trade_code or []),
            "status": chunk.status,
            "private": bool(chunk.private),
            "owner_user_id": chunk.owner_user_id,
            "allowed_roles": list(chunk.allowed_roles) if chunk.allowed_roles is not None else None,
            "title": chunk.title,
            "url": chunk.url,
            "header": chunk.header,
            "body": chunk.body,
            "text": text,
            "embed_allowed": allowed,
            "model": model,
        }
        doc["content_hash"] = content_hash(text, doc)
        plan.keep_ids.append(cid)
        if existing.get(cid) == doc["content_hash"]:
            continue  # unchanged: nothing to write, nothing to embed
        doc.pop("embed_allowed", None)
        doc["embedding"] = None
        doc["embedded"] = False
        doc["indexed_at"] = now
        plan.docs.append(doc)
        if allowed:
            plan.need_vector.append(len(plan.docs) - 1)
    return plan


async def _log_usage(db, tokens_by_tenant: dict[str, int], latency_ms: int, ok: bool) -> None:
    """Same shape as services/ai_agents.py _track_usage, plus token counts."""
    from models import gen_id
    rows = []
    request_id = gen_id()
    for tenant_id, tokens in tokens_by_tenant.items():
        rows.append({
            "id": gen_id(),
            "tenant_id": tenant_id,
            "request_id": request_id,
            "service": "voyage",
            "model": _model_name(),
            "feature": "search_index",
            "tokens": int(tokens),
            "latency_ms": latency_ms,
            "ok": ok,
            "created_at": _now().isoformat(),
        })
    if rows:
        try:
            await db.ai_usage_tracking.insert_many(rows)
        except Exception as exc:  # usage logging must never stop indexing
            logger.warning("search worker: usage log failed: %s", exc)


async def _embed_plans(db, plans: list[_Plan], embedder: Embedder) -> None:
    group = [p for p in plans if not p.error and p.need_vector]
    if group:
        await _embed_group(db, group, embedder)


async def _embed_group(db, group: list[_Plan], embedder: Embedder) -> None:
    """Embed every text these jobs need, in one call. If Voyage rejects the
    input, split the group in half and try each half, down to the single
    job it rejects; only that job is marked as failed."""
    todo = [(p, i) for p in group for i in p.need_vector]
    texts = [p.docs[i]["text"] for p, i in todo]
    t0 = time.monotonic()
    try:
        vectors, tokens = await embedder(texts, input_type="document")
        if len(vectors) != len(texts):
            raise RuntimeError("embedder returned the wrong number of vectors")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"embed: {type(exc).__name__}: {exc}"[:300]
        rejected = _rejected_status(exc)
        if rejected is not None:
            if len(group) > 1:
                mid = len(group) // 2
                await _embed_group(db, group[:mid], embedder)
                await _embed_group(db, group[mid:], embedder)
                return
            group[0].error = msg
            group[0].embed_failed = False  # counts as a real attempt
            logger.warning("search worker: the embedding service refused job %s (HTTP %s)",
                           group[0].job["_id"], rejected)
            return
        wait = _is_voyage_unavailable(exc)
        for p in group:
            p.error = msg
            p.embed_failed = wait
        logger.warning("search worker: embedding %d text(s) failed (%s)", len(texts),
                       "service unavailable, will retry" if wait else type(exc).__name__)
        return
    latency_ms = int((time.monotonic() - t0) * 1000)
    total_chars = sum(len(t) for t in texts) or 1
    by_tenant: dict[str, int] = {}
    for (p, i), vec in zip(todo, vectors):
        p.docs[i]["embedding"] = [float(x) for x in vec]
        p.docs[i]["embedded"] = True
        tid = p.docs[i]["tenant_id"]
        by_tenant[tid] = by_tenant.get(tid, 0) + len(p.docs[i]["text"])
    by_tenant = {t: round(tokens * c / total_chars) for t, c in by_tenant.items()}
    await _log_usage(db, by_tenant, latency_ms, True)


async def _write_plan(db, plan: _Plan) -> dict:
    """Write one job's chunks. stats["changed"] says whether anything this
    record had in the index was added, rewritten or removed."""
    ch = db[SEARCH_CHUNKS]
    stats = {"written": 0, "deleted": 0, "changed": False}
    if plan.deleted:
        stats["deleted"] = await _delete_source(db, plan)
        stats["changed"] = True
        return stats
    ops: list = [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in plan.docs]
    stale = _source_filter(plan.collection, plan.source_oid)
    stale["_id"] = {"$nin": plan.keep_ids}
    ops.append(DeleteMany(stale))
    res = await ch.bulk_write(ops, ordered=True)
    stats["written"] = res.upserted_count + res.modified_count
    stats["deleted"] = res.deleted_count
    stats["changed"] = bool(plan.docs) or res.deleted_count > 0
    return stats


async def requeue_children(db, tenant_id: Optional[str], source_id: Optional[str],
                           exclude: Optional[tuple[str, str]] = None) -> int:
    """Re-queue every record whose chunks point to `source_id` through
    parent_id (same company). Each child's chunker then decides again."""
    if not source_id:
        return 0
    match: dict[str, Any] = {"parent_id": source_id}
    if tenant_id:
        match["tenant_id"] = tenant_id
    return await _requeue_sources(db, match, exclude)


async def _requeue_sources(db, match: dict, exclude: Optional[tuple[str, str]] = None) -> int:
    pipeline = [
        {"$match": match},
        {"$group": {"_id": {"c": "$source_collection", "o": "$source_oid"}}},
    ]
    by_coll: dict[str, list[str]] = {}
    async for g in db[SEARCH_CHUNKS].aggregate(pipeline):
        c, o = g["_id"].get("c"), g["_id"].get("o")
        if not c or o is None or (c, o) == exclude:
            continue
        by_coll.setdefault(c, []).append(o)
    n = 0
    for coll, oids in by_coll.items():
        for i in range(0, len(oids), 500):
            n += await queue.enqueue_many(db, coll, oids[i:i + 500])
    return n


async def _requeue_dependents(db, plan: _Plan) -> int:
    """After a record's chunks changed: re-queue its children, and for a
    project every record filed under it."""
    me = (plan.collection, plan.source_oid)
    if plan.deleted:
        tenant_id, source_id, project_id = plan.gone_tenant_id, plan.gone_source_id, None
    else:
        res = plan.result
        tenant_id = res.tenant_id if res else None
        source_id = res.source_id if res else None
        project_id = res.project_id if res else None
    n = await requeue_children(db, tenant_id, source_id, exclude=me)
    if plan.collection == "projects" and not plan.deleted and tenant_id and (project_id or source_id):
        n += await requeue_project_sources(db, tenant_id, project_id or source_id, exclude=me)
    return n


async def _requeue_gate_changes(db, gate_seen: dict) -> int:
    """If a project's gate answer changed since last time, re-queue every
    record that has chunks in it, so vectors are added or removed."""
    n = 0
    for (tenant_id, project_id), allowed in gate_seen.items():
        if not await gate.note_gate_state(db, tenant_id, project_id, allowed):
            continue
        n += await requeue_project_sources(db, tenant_id, project_id)
    return n


async def requeue_project_sources(db, tenant_id: Optional[str], project_id: Optional[str],
                                  exclude: Optional[tuple[str, str]] = None) -> int:
    """Re-queue every record that has chunks in this project."""
    n = await _requeue_sources(db, {"tenant_id": tenant_id, "project_id": project_id}, exclude)
    if n:
        logger.info("search worker: re-queued %d record(s) of one project", n)
    return n


async def _heartbeat(db, jobs: list[dict]) -> None:
    """Keep this batch's leases alive while the worker is busy (a slow
    embedding call can take several minutes)."""
    while True:
        await asyncio.sleep(queue.HEARTBEAT_SECONDS)
        try:
            await queue.renew_leases(db, jobs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("search worker: lease renewal failed: %s", type(exc).__name__)


async def process_once(db, embedder: Optional[Embedder] = None, *, worker_id: Optional[str] = None,
                       batch_size: int = BATCH_SIZE) -> dict:
    """Claim one batch and process it. Returns counts (for logs and tests)."""
    embedder = embedder or _default_embed
    worker_id = worker_id or _worker_id()
    await queue.reap_expired(db)
    jobs = await queue.claim_batch(db, worker_id, batch_size)
    stats = {"jobs": len(jobs), "done": 0, "retry": 0, "written": 0, "deleted": 0,
             "embedded": 0, "requeued": 0, "lost": 0}
    if not jobs:
        return stats
    beat = asyncio.ensure_future(_heartbeat(db, jobs))
    try:
        await _process_jobs(db, jobs, embedder, stats)
    finally:
        beat.cancel()
        try:
            await beat
        except (asyncio.CancelledError, Exception):
            pass
    return stats


async def _process_jobs(db, jobs: list[dict], embedder: Embedder, stats: dict) -> None:
    registry = get_registry()
    ctx = ChunkContext(db)
    gate_seen: dict = {}
    plans: list[_Plan] = []
    for job in jobs:
        try:
            plan = await _build_plan(db, ctx, job, registry, gate_seen)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            plan = _Plan(job)
            plan.error = f"chunker: {type(exc).__name__}: {exc}"[:300]
            logger.warning("search worker: building chunks for %s failed: %s",
                           job["_id"], type(exc).__name__)
        plans.append(plan)

    await _embed_plans(db, plans, embedder)

    for plan in plans:
        if plan.error:
            status = await queue.mark_failed(db, plan.job, plan.error, embed_wait=plan.embed_failed)
            stats["lost" if status == "lost" else "retry"] += 1
            continue
        try:
            # Another worker may have taken this job over (our lease ran
            # out): then it owns the record and we must not write.
            if not await queue.still_holds(db, plan.job):
                stats["lost"] += 1
                logger.info("search worker: job %s was taken over; skipping it", plan.job["_id"])
                continue
            w = await _write_plan(db, plan)
            stats["written"] += w["written"]
            stats["deleted"] += w["deleted"]
            stats["embedded"] += sum(1 for d in plan.docs if d.get("embedded"))
            if w["changed"]:
                stats["requeued"] += await _requeue_dependents(db, plan)
            res = plan.result
            targets = list(res.requeue if res else [])
            if plan.deleted:
                # The row is gone, so its chunker can't say what it fed:
                # replay what it said the last time it was indexed.
                targets += [tuple(t) for t in (plan.job.get("requeue_on_delete") or [])
                            if isinstance(t, (list, tuple)) and len(t) == 2]
            for coll, filt in targets:
                if coll in registry and registry[coll].decision == "index" and isinstance(filt, dict):
                    stats["requeued"] += await queue.enqueue_filter(db, coll, filt)
            if plan.deleted and await queue.finish_delete(db, plan.job):
                stats["done"] += 1
                continue
            await queue.mark_done(
                db, plan.job,
                tenant_id=res.tenant_id if res else None,
                project_id=res.project_id if res else None,
                source_id=res.source_id if res else None,
                requeue=list(res.requeue) if res else None,
            )
            stats["done"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.mark_failed(db, plan.job, f"write: {type(exc).__name__}: {exc}"[:300])
            stats["retry"] += 1

    try:
        stats["requeued"] += await _requeue_gate_changes(db, gate_seen)
    except Exception as exc:
        logger.warning("search worker: gate check failed: %s", exc)


async def run_worker_loop(get_db_fn) -> None:
    """Long-running loop started from server.py lifespan. Idles while the
    flag is off; never raises except on cancel."""
    from services.feature_flags import search_everything_enabled
    from services.search.store import ensure_ready

    worker_id = _worker_id()
    await asyncio.sleep(FIRST_SLEEP_SECONDS)
    last_gate_check = 0.0
    while True:
        try:
            if not search_everything_enabled():
                await asyncio.sleep(FLAG_OFF_SLEEP_SECONDS)
                continue
            db = get_db_fn()
            await ensure_ready(db)
            # Every few minutes: a project newly marked as controlled
            # government information (or a company that turned AI off)
            # loses its stored vectors right away, not at the nightly check.
            if time.monotonic() - last_gate_check >= GATE_CHECK_SECONDS:
                last_gate_check = time.monotonic()
                from services.search.reconcile import recheck_gates
                await recheck_gates(db)
            stats = await process_once(db, worker_id=worker_id)
            if stats["jobs"]:
                logger.debug("search worker: %s", stats)
            if stats["jobs"] < BATCH_SIZE:
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            logger.info("search worker cancelled")
            raise
        except Exception as exc:
            logger.warning("search worker tick failed (non-fatal): %s", exc)
            await asyncio.sleep(30)
