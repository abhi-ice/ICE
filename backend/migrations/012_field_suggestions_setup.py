"""Migration 012 -- Bucket G Phase 1 Jigsaw Pre-fill UX setup.

Initializes the `field_suggestions` collection + indexes. No data
backfill -- suggestions are runtime-only. Audits a single
FIELD_SUGGESTIONS_INITIALIZED op per tenant so compliance has proof
of activation. Idempotent: re-running creates no duplicate indexes
(Mongo dedupes on name) and emits no extra audit rows.

Indexes
-------
  - (tenant_id, entity_type, entity_id, status)
      Drives the JigsawField widget's "what's pending on THIS entity"
      lookup. Tight enough to serve every form on every page.

  - (tenant_id, status, created_at)
      Drives the per-user review queue ("show me everything pending
      across the tenant, newest first").

  - (tenant_id, suggested_by)
      Drives the per-source acceptance-rate roll-up in Phase 5 ("how
      good is the photo-vision producer? the rfi drafter?").

  - (tenant_id, authorized_confirmer_roles)
      Drives the review queue's role-aware filter. Multikey (Mongo
      handles it implicitly because authorized_confirmer_roles is an
      array).

  - (created_at) -- no TTL. Auto-expiration is handled by the
      services.field_suggestions.expire_stale_pending background job
      (status flip + audit, not collection drop) because the audit
      chain needs the row to stick around for evidence.

Audit
-----
Single FIELD_SUGGESTIONS_INITIALIZED op per tenant that already exists
in the system. Captures the index list + the default role mapping so
future schema audits can replay the boot state.

Registered in run_all_pending.py after 011.

Run from backend/:

  python -m migrations.012_field_suggestions_setup
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
    from models import FIELD_SUGGESTION_DEFAULT_ROLES

    now = datetime.now(timezone.utc)
    print("=== Migration 012: field_suggestions setup ===")

    # ---- Indexes ----
    indexes = [
        ("fs_tenant_entity_status",
            [("tenant_id", 1), ("entity_type", 1),
              ("entity_id", 1), ("status", 1)]),
        ("fs_tenant_status_created",
            [("tenant_id", 1), ("status", 1), ("created_at", -1)]),
        ("fs_tenant_source",
            [("tenant_id", 1), ("suggested_by", 1)]),
        ("fs_tenant_roles",
            [("tenant_id", 1), ("authorized_confirmer_roles", 1)]),
        ("fs_created_at",
            [("created_at", -1)]),
    ]
    created: list[str] = []
    skipped: list[str] = []
    for name, spec in indexes:
        try:
            await db.field_suggestions.create_index(spec, name=name)
            created.append(name)
            print(f"  + index {name}")
        except Exception as exc:
            # Mongo treats re-create of same-name same-spec as a no-op;
            # only different-spec same-name throws. Log + continue.
            msg = str(exc)
            if "already exists" in msg.lower() or "name already" in msg.lower():
                skipped.append(name)
                print(f"  = index {name} (already present)")
            else:
                print(f"  ! index {name}: {type(exc).__name__}: {exc}")

    # ---- Audit one row per tenant ----
    # Pull every tenant; if a tenant has zero rows in field_suggestions
    # AND no INITIALIZED op yet, write one. Idempotent via existence
    # check on the audit op.
    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    audits_written = 0
    audits_skipped = 0
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": "FIELD_SUGGESTIONS_INITIALIZED"},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="field_suggestions",
                op="FIELD_SUGGESTIONS_INITIALIZED",
                record_id=f"migration_012_{int(now.timestamp())}",
                before=None, after={
                    "indexes_created": created,
                    "indexes_skipped": skipped,
                    "default_role_mapping": FIELD_SUGGESTION_DEFAULT_ROLES,
                },
                actor_id=None, request_id="migration_012",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print("\n--- SUMMARY ---")
    print(f"Indexes created: {len(created)}, skipped (already present): {len(skipped)}")
    print(f"Tenants audited: {audits_written}, skipped (already initialized): {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
