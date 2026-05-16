"""Migration 023 -- CPM Bug 3.E/3.F/3.G: recompute the cascade-aware
remaining_float_days + is_currently_critical + slip_days on every
existing task.

Pre-Bug-3.E storage:
  remaining_float_days  -- clamped at 0, computed from DIRECT blocker
                           impacts only (missed inherited cascade).
  is_currently_critical -- field did not exist.
  slip_days             -- field did not exist / always None.

Post-Bug-3.E storage:
  remaining_float_days  -- SIGNED (may be negative), derived from the
                           cascade's projected_finish_date:
                           total_float_days - working_days_between(
                             planned_finish, projected_finish_date).
  is_currently_critical -- (remaining_float_days <= 0).
  slip_days             -- working-day slip, persisted by
                           compute_projected_finish_dates.

The Bug-3.E formula change is forward-going: every
auto_recompute_after_event from this point writes the new values.
But tasks last recomputed before the deploy carry stale clamped
remaining_float_days, no is_currently_critical, and no slip_days.
This migration forces a one-time clean refresh.

Per (tenant, project) with tasks: run auto_recompute_after_event,
which chains:
  1. compute_cpm                     -- structural float + CP flag
  2. compute_projected_finish_dates  -- projected dates + slip_days
  3. compute_remaining_float_for_tasks -- remaining_float_days
     (signed) + is_currently_critical

Audit op:
  REM_FLOAT_CASCADE_RECOMPUTED -- one row per tenant. Payload carries
  per-tenant counts so the operator running run_all_pending sees
  progress.

Idempotent: the three compute_* helpers each skip writes when a
value is unchanged. Re-running re-walks every project but writes
nothing if the values are already cascade-correct. The per-tenant
audit existence check skips the audit re-write (same pattern as
migrations 013, 018-022).

Run from backend/:

  python -m migrations.023_rem_float_cascade_recompute
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


AUDIT_OP = "REM_FLOAT_CASCADE_RECOMPUTED"


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
    from services.cpm import auto_recompute_after_event

    now = datetime.now(timezone.utc)
    print("=== Migration 023: rem-float cascade recompute (Bug 3.E/3.F/3.G) ===")

    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    per_tenant: dict[str, dict[str, int]] = {}
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        per_tenant.setdefault(tid, {
            "projects_processed": 0,
            "tasks_total": 0,
        })

        project_ids = await db.tasks.distinct(
            "project_id",
            {"tenant_id": tid, "status": {"$ne": "deleted"}},
        )
        for pid in project_ids:
            if not pid:
                continue
            try:
                # Full chain: compute_cpm -> compute_projected_finish_
                # dates (writes slip_days) -> compute_remaining_float_
                # for_tasks (writes signed remaining_float_days +
                # is_currently_critical). auto_recompute_after_event
                # is fail-safe -- a per-project error is logged inside
                # and does not abort the migration.
                await auto_recompute_after_event(tid, pid)
                per_tenant[tid]["projects_processed"] += 1
                n = await db.tasks.count_documents({
                    "tenant_id": tid, "project_id": pid,
                    "status": {"$ne": "deleted"},
                })
                per_tenant[tid]["tasks_total"] += int(n)
            except Exception as exc:
                print(
                    f"  ! tenant={tid} project={pid}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    # ---- Per-tenant audit op (idempotent) ----------------------------
    audits_written = 0
    audits_skipped = 0
    for tid, counts in per_tenant.items():
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
                record_id=f"migration_023_{int(now.timestamp())}",
                before=None,
                after={
                    "projects_processed": counts["projects_processed"],
                    "tasks_total": counts["tasks_total"],
                    "notes": (
                        "CPM Bug 3.E/3.F/3.G -- recompute cascade-aware "
                        "remaining_float_days (SIGNED, may be negative; "
                        "derived from projected_finish_date, counts "
                        "inherited cascade slip), is_currently_critical "
                        "(remaining <= 0), and slip_days (persisted "
                        "working-day slip). Forward-going every "
                        "auto_recompute_after_event keeps them current."
                    ),
                },
                actor_id=None, request_id="migration_023",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Tenants processed:           {len(per_tenant)}")
    total_tasks = sum(c["tasks_total"] for c in per_tenant.values())
    total_projects = sum(c["projects_processed"] for c in per_tenant.values())
    print(f"Projects recomputed:         {total_projects}")
    print(f"Tasks refreshed:             {total_tasks}")
    if per_tenant:
        print(f"  per-tenant counts:")
        for tid, counts in sorted(per_tenant.items()):
            print(
                f"    {tid[:36]}: "
                f"{counts['projects_processed']} projects / "
                f"{counts['tasks_total']} tasks"
            )
    print(f"Tenants audited:             {audits_written}")
    print(f"Tenants already audited:     {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
