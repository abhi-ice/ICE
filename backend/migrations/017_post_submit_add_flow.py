"""Migration 017 -- Bucket D Addition 5 "add to locked DR" flow.

Adds two fields to every event collection (and the activity grid):

    post_submit_added: bool = False
    add_reason: str | None    = None

Stamped at write time by services/edit_permissions.stamp_add_metadata
when a new event/grid row is INSERTED while the parent DR is locked
(submitted/approved). Backfill here exists for grep-cleanliness +
TIA renderer simplicity (the `_annotate_edits` helper reads the flag
unconditionally; pre-existing rows lacking the field would otherwise
fall through `r.get("post_submit_added")` -> None which is falsy and
harmless, but the explicit False is preferable).

Collections touched: delays, rfis, qc_issues, safety_events,
coordination_events, deliveries, photos, daily_report_activities.

Idempotent: `$exists: false` guard per field on each collection. A
re-run scans the same collections but the update_many matches no
documents the second time.

Audits one POST_SUBMIT_ADD_FIELDS_INITIALIZED row per tenant for the
SHA-256 chain.

Run from backend/:

  python -m migrations.017_post_submit_add_flow
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


COLLECTIONS = [
    "delays",
    "rfis",
    "qc_issues",
    "safety_events",
    "coordination_events",
    "deliveries",
    "photos",
    "daily_report_activities",
]

# Fields to backfill. Defaults preserve legacy semantics (the row was
# NOT added post-submit; reason absent). post_submit_added=False is
# the safe default for every legacy row -- if anything was actually
# back-filled post-submit prior to Addition 5 landing, the audit log
# still has the INSERT op + supplement metadata for forensic recovery.
DEFAULTS = {
    "post_submit_added": False,
    "add_reason": None,
    "added_at_post_submit": None,
    "added_by_post_submit": None,
}


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
    print("=== Migration 017: post-submit add flow ===")

    # ---- Step 1: backfill defaults on every collection -----------------
    per_collection_touched: dict[str, int] = {}
    for coll in COLLECTIONS:
        # Match documents missing ANY of the new fields. $or works for
        # idempotency: once the field is set on a row, future runs
        # won't re-touch it because the row no longer matches.
        match: dict = {"$or": [{k: {"$exists": False}} for k in DEFAULTS.keys()]}
        # $setOnInsert won't work for update_many; use $set with the
        # field-missing guard above so we only touch rows that need it.
        res = await db[coll].update_many(match, {"$set": DEFAULTS})
        touched = int(res.modified_count or 0)
        per_collection_touched[coll] = touched
        if touched:
            print(f"  {coll}: backfilled {touched} row(s)")
        else:
            print(f"  {coll}: nothing to backfill")

    # ---- Step 2: per-tenant audit op (idempotent) ----------------------
    audits_written = 0
    audits_skipped = 0
    tenants = db.tenants.find({}, {"_id": 0, "id": 1})
    async for tenant in tenants:
        tid = tenant.get("id")
        if not tid:
            continue
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": "POST_SUBMIT_ADD_FIELDS_INITIALIZED"},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="daily_reports",
                op="POST_SUBMIT_ADD_FIELDS_INITIALIZED",
                record_id=f"migration_017_{int(now.timestamp())}",
                before=None, after={
                    "collections_touched": COLLECTIONS,
                    "fields_added": list(DEFAULTS.keys()),
                    "defaults": DEFAULTS,
                    "notes": (
                        "Bucket D Addition 5 -- post-submit ADD flow. "
                        "Replaces the supplement-wizard pattern with "
                        "direct add-to-locked-DR + add_reason capture. "
                        "Existing supplement rows preserved for audit; "
                        "new events use post_submit_added=true instead."
                    ),
                },
                actor_id=None, request_id="migration_017",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    for coll, n in per_collection_touched.items():
        print(f"{coll:30s}: {n}")
    print(f"\nTenants audited:         {audits_written}")
    print(f"Tenants already audited: {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
