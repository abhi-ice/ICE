"""Migration 018 -- CPM Part A: predecessor schema with type + lag.

Pre-Part-A storage:   tasks.predecessors = ["1.5", "1.6"]
Post-Part-A storage:  tasks.predecessors = [
                        {"code": "1.5", "type": "FS", "lag_days": 0},
                        {"code": "1.6", "type": "FS", "lag_days": 0}
                      ]

This migration converts every task's predecessors list element-by-
element. A bare string becomes {code: str, type: "FS", lag_days: 0}.
A partial dict gets defaults filled in (any missing key from
{type, lag_days}). A fully-formed dict passes through unchanged.

Idempotency:
- Per-row check: if EVERY element in predecessors is already a dict
  with the three expected keys (`code`, `type`, `lag_days`) AND none
  of them are bare strings, the row is skipped (no write, not counted).
- Per-tenant audit: PREDECESSOR_SCHEMA_MIGRATED is written exactly
  once per tenant. Re-runs detect the existing audit row and skip
  the audit write.

Backward compat for downstream readers is provided by the
`pred_code()` helper in models.py + the @field_validator on
TaskCreate / TaskUpdate / Task, both of which accept the legacy
bare-string form. This migration brings the at-rest data into the
canonical shape; reader-side coercion handles in-flight rows during
the deploy window.

The CPM forward/backward math is NOT changed in this migration --
that's Part C. Part A only changes the storage shape + the
@field_validator + the two Mongo queries in services/ai_agents.py
that look up tasks by predecessor.code (changed from `predecessors`
to `predecessors.code` to match the new shape).

Run from backend/:

  python -m migrations.018_predecessor_relationship_lag
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# CPM Part A -- audit op name. One row per tenant; idempotent re-runs
# skip the write.
AUDIT_OP = "PREDECESSOR_SCHEMA_MIGRATED"


def _coerce_one(item: Any) -> dict | None:
    """Per-element coercion. Mirrors _normalize_predecessors_list in
    models.py but operates one element at a time (clearer at the
    migration level + lets us count "already canonical" rows).

    Returns:
      - dict in canonical shape {code, type, lag_days}
      - None to drop the entry (malformed)

    A "canonical" element is a dict whose code is a non-empty string
    and whose type / lag_days are present.
    """
    if isinstance(item, str):
        stripped = item.strip()
        if not stripped:
            return None
        return {"code": stripped, "type": "FS", "lag_days": 0}
    if isinstance(item, dict):
        code = item.get("code")
        if not isinstance(code, str) or not code.strip():
            return None
        return {
            "code": code.strip(),
            "type": item.get("type", "FS"),
            "lag_days": item.get("lag_days", 0),
        }
    return None


def _is_already_canonical(preds: Any) -> bool:
    """A row's predecessors is already in the canonical post-Part-A
    shape iff every element is a dict with exactly the three expected
    keys + non-empty string code. Used by the migration's idempotency
    fast path to skip rewrites on already-migrated rows."""
    if not isinstance(preds, list):
        return False
    if not preds:
        return True  # empty list is trivially canonical
    for item in preds:
        if not isinstance(item, dict):
            return False
        code = item.get("code")
        if not isinstance(code, str) or not code.strip():
            return False
        if "type" not in item or "lag_days" not in item:
            return False
    return True


async def run() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        sys.exit(2)

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    import database as _database
    _database._client = client
    _database._db = db

    from audit import write_audit

    now = datetime.now(timezone.utc)
    print("=== Migration 018: predecessor relationship + lag schema ===")

    # ---- Step 1: per-task conversion ----------------------------------
    # Pull every task with a non-empty predecessors array. Skip rows
    # whose array is already in canonical dict form (idempotency).
    #
    # Performance: one bulk read of all tasks in the project, then
    # per-task update_one for the non-canonical ones. For projects
    # with thousands of activities this is acceptable for a one-shot
    # migration run (offhours; deploy-time). Bulk-write batching
    # could be added if profiling shows it's needed.
    cursor = db.tasks.find(
        {"predecessors": {"$exists": True, "$ne": []}},
        {"_id": 0, "id": 1, "predecessors": 1, "tenant_id": 1},
    )
    rows_touched_by_tenant: dict[str, int] = {}
    rows_skipped_canonical = 0
    rows_dropped_malformed = 0
    rows_visited = 0
    async for row in cursor:
        rows_visited += 1
        preds = row.get("predecessors")
        if _is_already_canonical(preds):
            rows_skipped_canonical += 1
            continue
        # Coerce element-by-element. Drop malformed entries silently
        # (defensive); count the drop so the operator can audit.
        new_list: list[dict] = []
        had_drop = False
        for item in preds:
            coerced = _coerce_one(item)
            if coerced is None:
                had_drop = True
                continue
            new_list.append(coerced)
        if had_drop:
            rows_dropped_malformed += 1
        await db.tasks.update_one(
            {"id": row["id"]},
            {"$set": {"predecessors": new_list}},
        )
        tid = row.get("tenant_id") or "_unknown_"
        rows_touched_by_tenant[tid] = rows_touched_by_tenant.get(tid, 0) + 1

    total_touched = sum(rows_touched_by_tenant.values())
    print(f"  tasks scanned:                 {rows_visited}")
    print(f"  tasks rewritten (per tenant):  {total_touched}")
    print(f"  tasks already canonical:       {rows_skipped_canonical}")
    print(f"  tasks with malformed entries:  {rows_dropped_malformed}")

    # ---- Step 2: per-tenant audit op (idempotent) ----------------------
    audits_written = 0
    audits_skipped = 0
    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": AUDIT_OP},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="tasks",
                op=AUDIT_OP,
                record_id=f"migration_018_{int(now.timestamp())}",
                before=None, after={
                    "tasks_rewritten": rows_touched_by_tenant.get(tid, 0),
                    "schema_after": {
                        "predecessors_element": {
                            "code": "str",
                            "type": "FS|SS|FF|SF",
                            "lag_days": "int (negative allowed for lead)",
                        },
                    },
                    "notes": (
                        "CPM Part A -- migrates tasks.predecessors from "
                        "list[str] (activity_code only) to list[dict] "
                        "with relationship type + lag. Defaults to FS+0 "
                        "for legacy bare-string entries, preserving "
                        "current cascade behavior. Forward/backward CPM "
                        "math change ships in Part C; this migration "
                        "only changes storage + Pydantic acceptance."
                    ),
                },
                actor_id=None, request_id="migration_018",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Tasks scanned:                  {rows_visited}")
    print(f"Tasks rewritten (total):        {total_touched}")
    print(f"  per-tenant breakdown:")
    for tid, count in sorted(rows_touched_by_tenant.items()):
        print(f"    {tid[:36]}: {count}")
    print(f"Tasks already canonical:        {rows_skipped_canonical}")
    print(f"Tasks with malformed entries:   {rows_dropped_malformed}")
    print(f"Tenants audited:                {audits_written}")
    print(f"Tenants already audited:        {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
