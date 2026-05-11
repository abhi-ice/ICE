"""Migration 009 -- Bucket H Phase 3 project_members backfill.

Adds the per-project membership table that backs the RBAC scoping
rules. Before this migration tenant-level roles applied project-
wide (a PM saw every project in the tenant); the new model is per-
project membership, which lets admins/PMs grant or revoke access
to specific projects via the new Project Access page.

Backfill behavior preserves the pre-migration access pattern: every
tenant_user gets a project_members row for every project in their
tenant. Operators can prune later via the Project Access page.

Mapping tenant_users.role -> project_members.role_on_project:
  - admin: SKIPPED (admins are tenant-wide; no per-project rows
    needed -- the resource-leveling helper short-circuits role=admin
    to all-access).
  - pm     -> pm
  - super  -> super
  - foreman -> foreman
  - viewer -> viewer
  - px     -> pm        (project executive senior-tier, mapped up)
  - pe     -> super     (project engineer, field-tier)

Indexes:
  - (tenant_id, project_id, user_id) unique
  - (tenant_id, user_id)
  - (tenant_id, project_id)

Idempotent: per-row skip via the unique-index lookup; re-run shows
zero new writes. Audit op: PROJECT_MEMBER_BACKFILL.

Registered in run_all_pending.py with the other auto-run migrations.
Run from backend/:

  python -m migrations.009_project_members_backfill
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tenant-role -> project-member-role mapping. admin omitted because
# admins are tenant-wide; the RBAC helper short-circuits role=admin
# to all-access without consulting project_members.
ROLE_MAP = {
    "pm": "pm",
    "super": "super",
    "foreman": "foreman",
    "viewer": "viewer",
    "px": "pm",
    "pe": "super",
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
    _database._client = client   # type: ignore
    _database._db = db           # type: ignore

    from audit import write_audit

    now = datetime.now(timezone.utc)
    stats = {
        "tenants_processed": 0,
        "memberships_examined": 0,
        "rows_added": 0,
        "rows_skipped_admin": 0,
        "rows_skipped_already_present": 0,
        "rows_skipped_unknown_role": 0,
        "rows_skipped_orphan_user": 0,
        "rows_skipped_orphan_project": 0,
        "failures": 0,
    }
    affected_tenants: set[str] = set()

    # =================================================================
    # Index creation (idempotent -- Mongo's createIndex is a no-op if
    # the spec matches an existing index).
    # =================================================================
    print("\n=== Ensure indexes on project_members ===")
    try:
        await db.project_members.create_index(
            [("tenant_id", 1), ("project_id", 1), ("user_id", 1)],
            unique=True, name="pm_tenant_project_user_unique",
        )
        await db.project_members.create_index(
            [("tenant_id", 1), ("user_id", 1)], name="pm_tenant_user",
        )
        await db.project_members.create_index(
            [("tenant_id", 1), ("project_id", 1)], name="pm_tenant_project",
        )
        print("  indexes ensured: unique(tenant,project,user) + "
              "(tenant,user) + (tenant,project)")
    except Exception as e:
        print(f"  ! index creation: {type(e).__name__}: {e}")

    # =================================================================
    # Walk every active tenant_user and project_in_tenant; insert one
    # project_members row per (user, project) tuple. Skip admins and
    # missing/orphan refs.
    # =================================================================
    print("\n=== Backfill project_members from tenant_users ===")
    tenant_users = await db.tenant_users.find(
        {"status": "active"},
        {"_id": 0, "user_id": 1, "tenant_id": 1, "role": 1},
    ).to_list(length=None)
    # Per-tenant project list cache (avoid N+1).
    project_cache: dict[str, list[dict]] = {}

    for tu in tenant_users:
        stats["memberships_examined"] += 1
        tenant_id = tu.get("tenant_id")
        user_id = tu.get("user_id")
        # Reconcile per-user role: tenant_users.role wins; fall back to
        # users.role if missing.
        tu_role = (tu.get("role") or "").lower()
        if not tu_role:
            user = await db.users.find_one(
                {"id": user_id}, {"_id": 0, "role": 1},
            )
            tu_role = (user or {}).get("role") or ""
            tu_role = tu_role.lower()
        if tu_role == "admin":
            stats["rows_skipped_admin"] += 1
            continue
        mapped = ROLE_MAP.get(tu_role)
        if not mapped:
            stats["rows_skipped_unknown_role"] += 1
            continue
        # Defensive: confirm the user still exists.
        user = await db.users.find_one(
            {"id": user_id}, {"_id": 0, "id": 1},
        )
        if not user:
            stats["rows_skipped_orphan_user"] += 1
            continue
        # Per-tenant project pull, cached.
        if tenant_id not in project_cache:
            project_cache[tenant_id] = await db.projects.find(
                {"tenant_id": tenant_id, "status": {"$ne": "archived"}},
                {"_id": 0, "id": 1},
            ).to_list(length=None)
            affected_tenants.add(tenant_id)
        for proj in project_cache[tenant_id]:
            project_id = proj["id"]
            try:
                # Idempotency: unique index does the work, but we
                # check explicitly so the stats are honest.
                existing = await db.project_members.find_one(
                    {"tenant_id": tenant_id, "project_id": project_id,
                     "user_id": user_id},
                    {"_id": 0, "id": 1},
                )
                if existing:
                    stats["rows_skipped_already_present"] += 1
                    continue
                doc = {
                    "id": uuid.uuid4().hex,
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "user_id": user_id,
                    "role_on_project": mapped,
                    "added_at": now.isoformat(),
                    "added_by": None,  # backfill, not user-initiated
                }
                await db.project_members.insert_one(doc.copy())
                stats["rows_added"] += 1
            except Exception as e:
                stats["failures"] += 1
                print(f"  ! project_members "
                       f"{tenant_id}/{project_id}/{user_id}: "
                       f"{type(e).__name__}: {e}")

    stats["tenants_processed"] = len(affected_tenants)

    # =================================================================
    # Audit: one PROJECT_MEMBER_BACKFILL entry per affected tenant.
    # Only emit when rows were added (no audit spam on idempotent re-runs).
    # =================================================================
    if stats["rows_added"]:
        for tenant_id in affected_tenants:
            try:
                await write_audit(
                    tenant_id=tenant_id, table_name="project_members",
                    op="PROJECT_MEMBER_BACKFILL",
                    record_id=f"migration_009_{int(now.timestamp())}",
                    before=None, after={
                        "rows_added": stats["rows_added"],
                        "rows_skipped_already_present":
                            stats["rows_skipped_already_present"],
                        "rows_skipped_admin": stats["rows_skipped_admin"],
                        "role_map": ROLE_MAP,
                    },
                    actor_id=None,
                    request_id="migration_009",
                )
            except Exception as e:
                print(f"  ! audit {tenant_id}: {type(e).__name__}: {e}")

    # =================================================================
    # Summary
    # =================================================================
    print("\n--- SUMMARY ---")
    print(f"Tenants processed:               {stats['tenants_processed']}")
    print(f"Memberships examined:            {stats['memberships_examined']}")
    print(f"Rows added:                      {stats['rows_added']}")
    print(f"Rows skipped (already present):  {stats['rows_skipped_already_present']}")
    print(f"Rows skipped (admin tenant_user):{stats['rows_skipped_admin']}")
    print(f"Rows skipped (unknown role):     {stats['rows_skipped_unknown_role']}")
    print(f"Rows skipped (orphan user):      {stats['rows_skipped_orphan_user']}")
    print(f"Failures:                        {stats['failures']}")
    if stats["failures"]:
        print(f"\nFAILED: {stats['failures']} record(s).", file=sys.stderr)
        sys.exit(1)
    print("\nMigration 009 complete. project_members table seeded.")


if __name__ == "__main__":
    asyncio.run(run())
