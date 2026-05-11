"""Migration 015 -- Bucket J Phase 1 role enum extension.

Enables submittal_tech + scheduling_engineer as valid values on
tenant_users.role + project_members.role_on_project. No automatic
role re-assignment: existing users keep their current role. The
migration just records that the enum was extended + writes a
ROLE_ENUM_EXTENDED audit op per tenant for the chain.

Per-tenant op is idempotent via existence check; re-runs are no-ops.

Registered in run_all_pending.py after 014.

Run from backend/:

  python -m migrations.015_role_enum_extended
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
    _database._client = client
    _database._db = db

    from audit import write_audit

    now = datetime.now(timezone.utc)
    print("=== Migration 015: role enum extended ===")

    NEW_ROLES = ["submittal_tech", "scheduling_engineer"]
    print(f"Enabling new roles: {NEW_ROLES}")
    print(
        "No automatic role assignment. Existing tenant_users.role + "
        "project_members.role_on_project rows preserved. Admins can "
        "now assign these values via Project Access UI."
    )

    audits_written = 0
    audits_skipped = 0
    tenants = db.tenants.find({}, {"_id": 0, "id": 1})
    async for tenant in tenants:
        tid = tenant.get("id")
        if not tid:
            continue
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": "ROLE_ENUM_EXTENDED"},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="tenant_users",
                op="ROLE_ENUM_EXTENDED",
                record_id=f"migration_015_{int(now.timestamp())}",
                before=None, after={
                    "new_roles_enabled": NEW_ROLES,
                    "automatic_assignment": False,
                    "compatibility_note": (
                        "submittal_tech forward-defined in Bucket G "
                        "FIELD_SUGGESTION_DEFAULT_ROLES['submittal']. "
                        "scheduling_engineer is new in Bucket J Phase 1."
                    ),
                },
                actor_id=None, request_id="migration_015",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---\nTenants audited: {audits_written}, "
          f"skipped: {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
