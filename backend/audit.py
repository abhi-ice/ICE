"""Append-only audit log with SHA-256 hash chain.

Master prompt §4.12: every mutation hashes prev_hash + row_data + op + table
+ timestamp into the next entry. We enforce append-only at the application
layer (no update/delete endpoint) since MongoDB doesn't have RLS triggers.

Hash-chain integrity check: GET /api/audit-log/verify — walks the chain.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, Any
from database import get_db
from models import gen_id


def _stable_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


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
    db = get_db()
    last = await db.audit_log.find_one(
        {"tenant_id": tenant_id},
        {"_id": 0, "hash": 1},
        sort=[("created_at", -1)],
    )
    prev_hash = last["hash"] if last else ""
    now = datetime.now(timezone.utc)
    body = (
        prev_hash
        + _stable_dumps(after or {})
        + _stable_dumps(before or {})
        + op
        + table_name
        + now.isoformat()
    )
    new_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

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
        "prev_hash": prev_hash,
        "hash": new_hash,
        "created_at": now.isoformat(),
    }
    await db.audit_log.insert_one(entry.copy())
    return entry


async def verify_chain(tenant_id: str, limit: int = 1000) -> dict:
    db = get_db()
    cursor = db.audit_log.find({"tenant_id": tenant_id}, {"_id": 0}).sort("created_at", 1).limit(limit)
    prev = ""
    count = 0
    broken_at = None
    async for entry in cursor:
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
    return {"valid": broken_at is None, "entries_checked": count, "broken_at": broken_at}
