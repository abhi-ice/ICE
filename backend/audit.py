"""Append-only audit log with SHA-256 hash chain.

Master prompt §4.12: every mutation hashes prev_hash + row_data + op + table
+ timestamp into the next entry. We enforce append-only at the application
layer (no update/delete endpoint) since MongoDB doesn't have RLS triggers.

Hash-chain integrity check: GET /api/audit-log/verify — walks the chain.

Concurrency (2026-11): appends per tenant are serialized through a small
"chain head" document per tenant (collection audit_chain_heads), moved
forward with an atomic compare-and-swap. Before this, saves that landed at
the same moment read the same "latest" entry and forked the chain. Chains
that already forked before this change are left exactly as they are (no
rewrite of existing rows); new entries chain on after the latest one.
"""
import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from database import get_db
from models import gen_id

log = logging.getLogger("audit")

# One small document per tenant holds the end of that tenant's chain:
#   {_id: tenant_id, tenant_id, last_hash, last_created_at, seq}
# Every append moves it forward only if nobody else moved it first, so two
# writers (in this process or another one) can never chain onto the same
# previous entry.
HEADS_COLLECTION = "audit_chain_heads"

# How many times an append retries when another writer moved the head
# first. Writers inside one process queue on a lock, so conflicts only
# come from other processes and settle in a few tries.
_MAX_ATTEMPTS = 25

# Per-tenant lock for writers inside this process (keeps conflicts rare),
# keyed by (event loop id, tenant_id).
_local_locks: dict = {}


def _stable_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _hash_entry(prev_hash: str, after, before, op: str, table_name: str,
                created_at: str) -> str:
    body = (
        prev_hash
        + _stable_dumps(after or {})
        + _stable_dumps(before or {})
        + op
        + table_name
        + created_at
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    # Always six decimal places so text order matches time order.
    return dt.isoformat(timespec="microseconds")


def _next_created_at(last_created_at: Optional[str]) -> str:
    """Now, but always strictly after the chain's last entry, so sorting by
    created_at (what verify_chain does) walks entries in chain order even
    if two servers' clocks disagree slightly."""
    now = datetime.now(timezone.utc)
    if last_created_at:
        try:
            last = datetime.fromisoformat(str(last_created_at).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now <= last:
                now = last + timedelta(microseconds=1)
        except (TypeError, ValueError):
            pass
    return _iso(now)


async def _load_head(db, tenant_id: str) -> dict:
    """Read this tenant's chain head. On first use, create it from the
    latest chained entry already written, so new entries continue the
    existing history."""
    heads = db[HEADS_COLLECTION]
    head = await heads.find_one({"_id": tenant_id})
    if head:
        return head
    last = await db.audit_log.find_one(
        {"tenant_id": tenant_id, "hash": {"$exists": True}, "unchained": {"$ne": True}},
        {"_id": 0, "hash": 1, "created_at": 1},
        sort=[("created_at", -1)],
    )
    seed = {
        "tenant_id": tenant_id,
        "last_hash": (last or {}).get("hash") or "",
        "last_created_at": (last or {}).get("created_at"),
        "seq": 0,
    }
    try:
        await heads.update_one({"_id": tenant_id}, {"$setOnInsert": seed}, upsert=True)
    except Exception as e:  # another writer created it at the same moment
        log.debug("audit head seed race for %s: %s", tenant_id, e)
    head = await heads.find_one({"_id": tenant_id})
    return head or {"_id": tenant_id, **seed}


async def _append_chained(db, entry: dict) -> Optional[dict]:
    """Chain `entry` onto the tenant's head. Returns the stored entry, or
    None if other writers kept winning the race."""
    tenant_id = entry["tenant_id"]
    heads = db[HEADS_COLLECTION]
    for attempt in range(_MAX_ATTEMPTS):
        head = await _load_head(db, tenant_id)
        prev_hash = head.get("last_hash") or ""
        seq = int(head.get("seq") or 0)
        created_at = _next_created_at(head.get("last_created_at"))
        new_hash = _hash_entry(prev_hash, entry["after_data"], entry["before_data"],
                               entry["op"], entry["table_name"], created_at)
        # Compare-and-swap: move the head only if it is still where we
        # read it.
        moved = await heads.find_one_and_update(
            {"_id": tenant_id, "seq": seq, "last_hash": prev_hash},
            {"$set": {"last_hash": new_hash, "last_created_at": created_at,
                      "seq": seq + 1}},
        )
        if moved is None:
            # Someone else appended first; wait a moment and try again.
            await asyncio.sleep(random.uniform(0, 0.002 * (attempt + 1)))
            continue
        stored = {**entry, "prev_hash": prev_hash, "hash": new_hash,
                  "created_at": created_at, "seq": seq + 1}
        try:
            await db.audit_log.insert_one(stored.copy())
        except Exception:
            # Put the head back so the chain does not point at a row that
            # was never written (only possible if nobody appended since).
            try:
                await heads.find_one_and_update(
                    {"_id": tenant_id, "seq": seq + 1, "last_hash": new_hash},
                    {"$set": {"last_hash": prev_hash, "seq": seq,
                              "last_created_at": head.get("last_created_at")}},
                )
            except Exception:
                pass
            raise
        return stored
    return None


async def _append_unchained(db, entry: dict, reason: str) -> dict:
    """Last resort: keep the record of what happened, outside the chain.
    The row is marked `unchained` so verify_chain skips it instead of
    reporting a break."""
    created_at = _iso(datetime.now(timezone.utc))
    stored = {
        **entry,
        "prev_hash": "",
        "hash": _hash_entry("", entry["after_data"], entry["before_data"],
                            entry["op"], entry["table_name"], created_at),
        "created_at": created_at,
        "unchained": True,
        "unchained_reason": reason,
    }
    try:
        await db.audit_log.insert_one(stored.copy())
    except Exception:
        log.exception("audit row could not be written (tenant=%s table=%s record=%s)",
                      entry.get("tenant_id"), entry.get("table_name"), entry.get("record_id"))
    return stored


async def write_audit(
    *,
    tenant_id: str,
    table_name: str,
    op: str,  # INSERT / UPDATE / DELETE
    record_id: str,
    before: Optional[dict],
    after: Optional[dict],
    actor_id: Optional[str],
    request_id: Optional[str],
) -> dict:
    """Append one audit entry to the tenant's hash chain.

    Never raises: the caller's save has already happened and must not fail
    because of the audit trail. If the chain cannot be extended, the row is
    still written, marked `unchained`."""
    entry = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "table_name": table_name,
        "op": op,
        "record_id": record_id,
        "before_data": before,
        "after_data": after,
        "actor_id": actor_id,
        "request_id": request_id,
    }
    try:
        db = get_db()
    except Exception:
        log.exception("audit: database unavailable; entry not written")
        return {**entry, "prev_hash": "", "hash": "",
                "created_at": _iso(datetime.now(timezone.utc))}
    try:
        _make_storable(entry)
    except Exception:
        pass  # the append below falls back to an unchained row if needed
    # Run the append as its own task and shield it: if the request that
    # triggered it is cancelled (phone drops the connection), the append
    # still finishes. Otherwise the chain head could move forward for a
    # row that never gets written, and the chain would break for good.
    task = asyncio.ensure_future(_locked_append(db, entry))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("audit append failed for tenant %s", tenant_id)
        return {**entry, "prev_hash": "", "hash": "",
                "created_at": _iso(datetime.now(timezone.utc))}


def _make_storable(entry: dict) -> None:
    """The database must be able to store before/after, or the insert
    would fail AFTER the chain head moved. Anything it cannot store (sets,
    custom objects) is turned into the same text the hash is built from,
    so verify_chain still recomputes the same hash."""
    try:
        import bson
        bson.encode({"b": entry.get("before_data"), "a": entry.get("after_data")})
        return
    except ImportError:
        return
    except Exception:
        pass
    for k in ("before_data", "after_data"):
        if entry.get(k) is not None:
            entry[k] = json.loads(_stable_dumps(entry[k]))


async def _locked_append(db, entry: dict) -> dict:
    tenant_id = entry["tenant_id"]
    # Keyed by event loop too: an asyncio lock belongs to one loop.
    key = (id(asyncio.get_running_loop()), tenant_id)
    lock = _local_locks.get(key)
    if lock is None:
        lock = _local_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            stored = await _append_chained(db, entry)
        if stored is not None:
            return stored
        log.warning("audit chain busy for tenant %s; writing row unchained", tenant_id)
        return await _append_unchained(db, entry, "chain_conflict")
    except Exception:
        log.exception("audit chain append failed for tenant %s; writing row unchained", tenant_id)
        return await _append_unchained(db, entry, "chain_error")


async def verify_chain(tenant_id: str, limit: int = 1000) -> dict:
    db = get_db()
    cursor = db.audit_log.find({"tenant_id": tenant_id}, {"_id": 0}).sort("created_at", 1).limit(limit)
    prev = ""
    count = 0
    unchained = 0
    broken_at = None
    async for entry in cursor:
        # Rows that were never part of the chain (written directly without
        # a hash, or marked unchained by write_audit's fallback) are
        # skipped, not counted as breaks.
        if entry.get("unchained") or "hash" not in entry:
            unchained += 1
            continue
        if entry.get("prev_hash", "") != prev:
            broken_at = entry["id"]
            break
        body = (
            prev
            + _stable_dumps(entry.get("after_data") or {})
            + _stable_dumps(entry.get("before_data") or {})
            + entry["op"]
            + entry["table_name"]
            + entry["created_at"]
        )
        recomputed = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if recomputed != entry["hash"]:
            broken_at = entry["id"]
            break
        prev = entry["hash"]
        count += 1
    return {"valid": broken_at is None, "entries_checked": count, "broken_at": broken_at,
            "unchained_entries": unchained}
