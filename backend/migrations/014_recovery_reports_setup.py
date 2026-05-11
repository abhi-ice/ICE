"""Migration 014 -- Bucket I Recovery Report Generator setup.

Initializes the recovery_reports collection + 4 indexes. No data
backfill (recovery reports are generated on-demand from existing
collections). Single RECOVERY_REPORTS_INITIALIZED audit op per
tenant; re-runs are no-ops.

Indexes
-------
  - (tenant_id, project_id, generated_at desc)
      Project-scoped list view (Reports Generator tab default).
  - (tenant_id, report_type, status)
      "Show me all finalized friday_recovery reports."
  - (tenant_id, project_id, report_type, idempotency_key)
      Idempotency lookup for POST /generate.
  - (tenant_id, status, generated_at desc)
      Aging dashboard / draft-cleanup queries.

Registered in run_all_pending.py after 013.

Run from backend/:

  python -m migrations.014_recovery_reports_setup
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    _database._client = client   # type: ignore
    _database._db = db           # type: ignore

    from audit import write_audit

    now = datetime.now(timezone.utc)
    print("=== Migration 014: recovery_reports setup ===")

    indexes = [
        ("rr_tenant_project_generated",
            [("tenant_id", 1), ("project_id", 1), ("generated_at", -1)]),
        ("rr_tenant_type_status",
            [("tenant_id", 1), ("report_type", 1), ("status", 1)]),
        ("rr_tenant_project_type_idem",
            [("tenant_id", 1), ("project_id", 1),
             ("report_type", 1), ("idempotency_key", 1)]),
        ("rr_tenant_status_generated",
            [("tenant_id", 1), ("status", 1), ("generated_at", -1)]),
    ]
    created: list[str] = []
    skipped: list[str] = []
    for name, spec in indexes:
        try:
            await db.recovery_reports.create_index(spec, name=name)
            created.append(name)
            print(f"  + index {name}")
        except Exception as exc:
            if "already exists" in str(exc).lower() or "name already" in str(exc).lower():
                skipped.append(name)
                print(f"  = index {name} (already present)")
            else:
                print(f"  ! index {name}: {type(exc).__name__}: {exc}")

    audits_written = 0
    audits_skipped = 0
    tenants = db.tenants.find({}, {"_id": 0, "id": 1})
    async for tenant in tenants:
        tid = tenant.get("id")
        if not tid:
            continue
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": "RECOVERY_REPORTS_INITIALIZED"},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="recovery_reports",
                op="RECOVERY_REPORTS_INITIALIZED",
                record_id=f"migration_014_{int(now.timestamp())}",
                before=None, after={"indexes_created": created},
                actor_id=None, request_id="migration_014",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print("\n--- SUMMARY ---")
    print(f"Indexes created: {len(created)}, skipped: {len(skipped)}")
    print(f"Tenants audited: {audits_written}, skipped: {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
