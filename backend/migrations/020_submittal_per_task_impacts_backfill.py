"""Migration 020 -- CPM Part K-1: backfill per_task_impacts on submittals.

Pre-Part-K storage:   submittals row has no per_task_impacts field.
Post-Part-K storage:  submittals.per_task_impacts = [] (empty array default).

This migration sets per_task_impacts=[] on every submittal row that
doesn't already have the field. Idempotent: rows where
per_task_impacts is already present (even if empty) are skipped.

Mirrors migration 019 (RFI per_task_impacts backfill) in structure.

Downstream Path 1 (compute_projected_finish_dates Part K-4 extension)
and Path 2 (_sum_prior_open_blocker_impacts_on_task Part K-5 extension)
both walk submittals expecting the per_task_impacts array. Without
this backfill, legacy rows would surface as missing-field reads or
silently contribute zero impact (which IS the correct behavior for
legacy submittals that have no impact captured -- but having the
storage canonical lets Mongo queries that filter on
per_task_impacts.task_id be index-friendly later if the corpus grows).

Audit op:
  SUBMITTAL_PER_TASK_IMPACTS_BACKFILLED -- one row per tenant. Payload
  carries per-tenant counts so the operator running run_all_pending
  sees at a glance how big the corpus was.

The Submittal / SubmittalCreate / SubmittalUpdate Pydantic models
already accept the backfilled shape (default_factory=list on the
field; validator coerces None / missing to []), so even without this
migration the API surface keeps working. The migration is for
storage canonicality + future query / index efficiency.

Run from backend/:

  python -m migrations.020_submittal_per_task_impacts_backfill
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# CPM Part K-1 -- audit op name. One row per tenant; idempotent re-runs
# skip the write via the per-tenant audit existence check (same
# pattern as migrations 013, 018, 019).
AUDIT_OP = "SUBMITTAL_PER_TASK_IMPACTS_BACKFILLED"


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
    print("=== Migration 020: submittal per_task_impacts backfill ===")

    # ---- Step 1: per-tenant count + bulk backfill --------------------
    # Two-pass: pre-count for the per-tenant audit payload, then a
    # single update_many for the actual write. Mirrors migration 019.
    #
    # Idempotency: {per_task_impacts: {$exists: false}} matches zero
    # rows on a second run (the first run set the field to []), so
    # the bulk write is a no-op on re-runs.
    candidates_cursor = db.submittals.find(
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
        print("  no submittal rows missing per_task_impacts; nothing to backfill")
    else:
        res = await db.submittals.update_many(
            {"per_task_impacts": {"$exists": False}},
            {"$set": {"per_task_impacts": []}},
        )
        print(f"  submittals scanned (missing field):  {rows_visited}")
        print(f"  submittals backfilled (matched/modified): "
              f"{res.matched_count}/{res.modified_count}")

    # ---- Step 2: per-tenant audit op (idempotent) ---------------------
    # Walk EVERY tenant (not just those with backfilled submittals) so
    # the audit chain shows the migration ran tenant-wide. Tenants
    # with 0 backfilled rows still get an audit row with
    # submittals_backfilled: 0.
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
                tenant_id=tid, table_name="submittals",
                op=AUDIT_OP,
                record_id=f"migration_020_{int(now.timestamp())}",
                before=None, after={
                    "submittals_backfilled": rows_by_tenant.get(tid, 0),
                    "schema_after": {
                        "per_task_impacts_element": {
                            "task_id": "str",
                            "projected_impact_days": "int (>= 0)",
                            "source": (
                                "system_suggested | super_overridden "
                                "(required; auto-suggested by "
                                "detect_breaches, super edit flips to "
                                "super_overridden)"
                            ),
                            # Server-stamped fields appended by
                            # _resolve_submittal_task_impacts (Part K-3)
                            # post-recompute on subsequent writes.
                        },
                    },
                    "notes": (
                        "CPM Part K -- adds per_task_impacts array to "
                        "submittals collection. Legacy rows backfilled "
                        "to []. The hybrid auto-suggest pattern: "
                        "detect_breaches (Part K-2) stamps "
                        "system_suggested entries on SLA-breach moment; "
                        "super edits flip source to super_overridden "
                        "and re-breach events suppress auto-suggest "
                        "for super-overridden task_ids. Downstream "
                        "Path 1 (compute_projected_finish_dates K-4) "
                        "and Path 2 (cumulative remaining-float "
                        "verdict K-5) treat over-SLA submittals' "
                        "per_task_impacts identically to delay/RFI "
                        "impacts."
                    ),
                },
                actor_id=None, request_id="migration_020",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Submittals scanned (missing field):  {rows_visited}")
    if rows_by_tenant:
        print(f"  per-tenant backfill counts:")
        for tid, count in sorted(rows_by_tenant.items()):
            print(f"    {tid[:36]}: {count}")
    print(f"Tenants audited:                     {audits_written}")
    print(f"Tenants already audited:             {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
