"""Migration 028 -- Project header + client-template fields.

Two related Daily Report features (2026-05-20) add 6 new optional
fields to the projects collection:

  Feature 1 (DR Header Auto-Population)
    station_id              -- e.g. "526-25-100", VA station / facility code
    contractor_name         -- e.g. "Integrated Construction Enterprises"

  Feature 2 (Client DR Template Export)
    dr_template_file_url    -- storage URL of the per-project client template
    dr_template_file_name   -- original filename for display
    dr_template_uploaded_at -- when the template was uploaded
    dr_template_uploaded_by -- user ID who uploaded it

Existing projects predate the schema. The fields are all Optional and
the Project ReadModel uses extra='ignore', so reads on legacy docs are
already safe (each field defaults to None). This migration is purely
DEFENSIVE -- it makes the schema explicit in mongo by $set-ing each new
field to null on any project that lacks it, so queries that filter on
these fields don't have to special-case "field missing" vs "field
null". Idempotent: a re-run finds no projects needing updates.

NOTE: `contract_number` and `client_name` already existed on the
Project model -- this migration does NOT touch them.

Audit op:
  PROJECT_HEADER_TEMPLATE_FIELDS_BACKFILLED -- one row per tenant.
  Payload carries the per-tenant count. Idempotent via per-tenant
  audit existence check (same pattern as migrations 013, 018-023, 027).

NOTE: must be run on Emergent (the deployment environment) against
the live database -- it cannot run here (no MONGO_URL/DB_NAME).

Run from backend/:

  python -m migrations.028_project_header_and_template_fields
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


AUDIT_OP = "PROJECT_HEADER_TEMPLATE_FIELDS_BACKFILLED"

# The 6 new optional fields this migration ensures are present (as
# null) on every existing project document.
_NEW_FIELDS = [
    "station_id",
    "contractor_name",
    "dr_template_file_url",
    "dr_template_file_name",
    "dr_template_uploaded_at",
    "dr_template_uploaded_by",
]


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
    print("=== Migration 028: project header + client-template fields ===")

    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    per_tenant: dict[str, int] = {}
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        per_tenant.setdefault(tid, 0)

        # Only projects that lack at least one of the new fields need
        # the $set -- idempotent: a re-run finds none.
        cursor = db.projects.find(
            {
                "tenant_id": tid,
                "$or": [{f: {"$exists": False}} for f in _NEW_FIELDS],
            },
            {"_id": 0, "id": 1, **{f: 1 for f in _NEW_FIELDS}},
        )
        async for row in cursor:
            set_ops: dict = {}
            for f in _NEW_FIELDS:
                if f not in row:
                    set_ops[f] = None
            if not set_ops:
                continue
            try:
                await db.projects.update_one(
                    {"tenant_id": tid, "id": row["id"]},
                    {"$set": set_ops},
                )
                per_tenant[tid] += 1
            except Exception as exc:
                print(
                    f"  ! tenant={tid} project={row.get('id')}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    # ---- Per-tenant audit op (idempotent) ----------------------------
    audits_written = 0
    audits_skipped = 0
    for tid, backfilled in per_tenant.items():
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": AUDIT_OP},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="projects",
                op=AUDIT_OP,
                record_id=f"migration_028_{int(now.timestamp())}",
                before=None,
                after={
                    "projects_backfilled": backfilled,
                    "fields_added": _NEW_FIELDS,
                    "notes": (
                        "Set station_id, contractor_name, and dr_template_* "
                        "fields to null on legacy projects. Defensive only "
                        "-- Project ReadModel already tolerates missing "
                        "fields (extra='ignore' + Optional defaults). Makes "
                        "the schema explicit in mongo so queries can filter "
                        "on these fields without {$exists: false} special-"
                        "casing."
                    ),
                },
                actor_id=None, request_id="migration_028",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print("\n--- SUMMARY ---")
    print(f"Tenants processed:           {len(per_tenant)}")
    total = sum(per_tenant.values())
    print(f"Projects backfilled:         {total}")
    if per_tenant:
        for tid, n in sorted(per_tenant.items()):
            print(f"    {tid[:36]}: {n} project(s) backfilled")
    print(f"Tenants audited:             {audits_written}")
    print(f"Tenants already audited:     {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
