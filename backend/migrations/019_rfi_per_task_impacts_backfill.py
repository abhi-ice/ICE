"""Migration 019 -- CPM Part J: backfill per_task_impacts on RFIs.

Pre-Part-J storage:   rfis row has no per_task_impacts field.
Post-Part-J storage:  rfis.per_task_impacts = [] (empty array default).

This migration sets per_task_impacts=[] on every RFI row that doesn't
already have the field. Idempotent: rows where per_task_impacts is
already present (even if empty) are skipped.

The downstream cascade (compute_projected_finish_dates Part J-4 +
_sum_prior_open_blocker_impacts_on_task Part J-5) BOTH walk RFIs
expecting the per_task_impacts array. Without this backfill, legacy
rows would surface as missing-field errors on Mongo reads, or
silently contribute zero impact (which is correct for legacy RFIs
that never had impact days captured, but the missing-field shape
would force every downstream consumer to defensively guard against
None).

Audit op:
  RFI_PER_TASK_IMPACTS_BACKFILLED -- one row per tenant. The audit
  payload carries the count of RFIs backfilled in that tenant so an
  operator running run_all_pending can see at a glance whether their
  RFI corpus was scoped to a small handful or thousands.

The Rfi / RfiCreate / RfiUpdate Pydantic models already accept the
backfilled shape (default_factory=list on the field; the field
validator coerces None / missing to []), so even without this
migration the API surface keeps working -- but having the storage
canonical lets Mongo queries that filter on per_task_impacts.task_id
(used by _open_blocker_impact_for_task Part J-4) be index-friendly
later if the corpus grows.

Run from backend/:

  python -m migrations.019_rfi_per_task_impacts_backfill
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# CPM Part J -- audit op name. One row per tenant; idempotent re-runs
# skip the write via the per-tenant audit existence check.
AUDIT_OP = "RFI_PER_TASK_IMPACTS_BACKFILLED"


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
    print("=== Migration 019: RFI per_task_impacts backfill ===")

    # ---- Step 1: bulk backfill ----------------------------------------
    # Find every RFI without per_task_impacts and $set it to []. Mongo's
    # {$exists: false} catches both literal-missing and never-set.
    #
    # We do the work in two passes so we can count per-tenant for the
    # audit step:
    #   Pass A: scan candidate rows with project_id (no payload mutation)
    #   Pass B: per-tenant bulk update_many
    #
    # Mongo's update_many($exists: false) is idempotent across re-runs:
    # rows already migrated (per_task_impacts present, even as empty
    # list) are not in the filter set on the second run.
    candidates_cursor = db.rfis.find(
        {"per_task_impacts": {"$exists": False}},
        {"_id": 0, "id": 1, "tenant_id": 1},
    )
    rows_by_tenant: dict[str, int] = {}
    rows_visited = 0
    async for row in candidates_cursor:
        rows_visited += 1
        tid = row.get("tenant_id") or "_unknown_"
        rows_by_tenant[tid] = rows_by_tenant.get(tid, 0) + 1

    if rows_visited == 0:
        print("  no RFI rows missing per_task_impacts; nothing to backfill")
    else:
        # Single bulk update_many covers the entire corpus -- no need to
        # iterate per-row. The filter is tenant-agnostic but the count
        # above gives us per-tenant breakdown for auditing.
        res = await db.rfis.update_many(
            {"per_task_impacts": {"$exists": False}},
            {"$set": {"per_task_impacts": []}},
        )
        print(f"  rfis scanned (missing field):  {rows_visited}")
        print(f"  rfis backfilled (matched/modified): "
              f"{res.matched_count}/{res.modified_count}")

    # ---- Step 2: per-tenant audit op (idempotent) ---------------------
    # We walk EVERY tenant (not just those with backfilled RFIs) so the
    # audit chain shows the migration ran cleanly tenant-wide. For
    # tenants with 0 backfilled rows, the audit payload carries
    # `rfis_backfilled: 0`.
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
                tenant_id=tid, table_name="rfis",
                op=AUDIT_OP,
                record_id=f"migration_019_{int(now.timestamp())}",
                before=None, after={
                    "rfis_backfilled": rows_by_tenant.get(tid, 0),
                    "schema_after": {
                        "per_task_impacts_element": {
                            "task_id": "str",
                            "projected_impact_days": "int (>= 0)",
                            # Server-stamped fields appended by
                            # _resolve_rfi_task_impacts (Part J-3) on
                            # subsequent writes; legacy backfilled rows
                            # start with just task_id + impact_days
                            # when supers later edit them.
                        },
                    },
                    "notes": (
                        "CPM Part J -- adds per_task_impacts array to "
                        "rfis collection. Legacy rows backfilled to []. "
                        "Downstream Path 1 (compute_projected_finish_dates "
                        "Part J-4) and Path 2 (Part J-5 prior-sum walk) "
                        "treat empty per_task_impacts as 'no schedule "
                        "impact captured' (binary blocker badge from "
                        "count_open_blockers still applies). Supers can "
                        "edit the array later via PATCH /rfis/{id} to "
                        "add per-task impact estimates."
                    ),
                },
                actor_id=None, request_id="migration_019",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"RFIs scanned (missing field):   {rows_visited}")
    if rows_by_tenant:
        print(f"  per-tenant backfill counts:")
        for tid, count in sorted(rows_by_tenant.items()):
            print(f"    {tid[:36]}: {count}")
    print(f"Tenants audited:                {audits_written}")
    print(f"Tenants already audited:        {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
