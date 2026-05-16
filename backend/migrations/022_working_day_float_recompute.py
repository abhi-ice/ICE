"""Migration 022 -- CPM Bug 3.B: recompute structural float +
remaining float in WORKING days for every existing task.

Pre-Bug-3.B storage:  tasks.total_float_days / remaining_float_days
                      were CALENDAR-day counts (LS - ES).days.
Post-Bug-3.B storage: both are WORKING-day counts -- structural via
                      working_days_between(ES, LS), remaining via
                      structural - sum_open_blocker_impacts (both
                      working days).

The Bug-3.B FORMULA change (services/cpm.py) is forward-going: every
compute_cpm call from this point on writes working-day float. But
tasks last recomputed before the Bug-3.B deploy still carry stale
CALENDAR-day total_float_days / remaining_float_days. This migration
forces a one-time clean refresh.

Per (tenant, project) with tasks:
  1. compute_cpm()                  -- refreshes total_float_days
     (working-day) + is_critical_path on every task.
  2. compute_remaining_float_for_tasks() -- refreshes
     remaining_float_days = max(0, working-day structural -
     working-day sum_open_impacts).

Audit op:
  WORKING_DAY_FLOAT_RECOMPUTED -- one row per tenant. Payload carries
  per-tenant counts (projects_processed, tasks_updated, critical_
  count) so the operator running run_all_pending sees progress.

Idempotent: compute_cpm + compute_remaining_float are themselves
idempotent (they skip writes when a value is unchanged). Re-running
the migration re-walks every project but writes nothing if float is
already working-day-correct. The per-tenant audit existence check
skips the audit re-write (same pattern as migrations 013, 018-021).

Run from backend/:

  python -m migrations.022_working_day_float_recompute
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# CPM Bug 3.B -- audit op name. One row per tenant; idempotent re-runs
# skip the write via the per-tenant audit existence check.
AUDIT_OP = "WORKING_DAY_FLOAT_RECOMPUTED"


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
    from services.cpm import compute_cpm
    from services.task_status import compute_remaining_float_for_tasks

    now = datetime.now(timezone.utc)
    print("=== Migration 022: working-day float recompute (Bug 3.B) ===")

    # Walk every (tenant, project) pair that has tasks. compute_cpm
    # refreshes total_float_days to the working-day count; then
    # compute_remaining_float_for_tasks refreshes remaining_float_days
    # off that fresh structural float.
    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    per_tenant: dict[str, dict[str, int]] = {}
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        per_tenant.setdefault(tid, {
            "projects_processed": 0,
            "tasks_updated": 0,
            "critical_count": 0,
            "remaining_float_updated": 0,
        })

        project_ids = await db.tasks.distinct(
            "project_id",
            {"tenant_id": tid, "status": {"$ne": "deleted"}},
        )
        for pid in project_ids:
            if not pid:
                continue
            try:
                # Step 1 -- working-day structural float + is_critical_path.
                cpm_res = await compute_cpm(tid, pid)
                # Step 2 -- working-day remaining float.
                rf_res = await compute_remaining_float_for_tasks(
                    db, tid, pid,
                )
                per_tenant[tid]["projects_processed"] += 1
                per_tenant[tid]["tasks_updated"] += int(
                    cpm_res.get("updated") or 0
                )
                per_tenant[tid]["critical_count"] += int(
                    cpm_res.get("critical_count") or 0
                )
                per_tenant[tid]["remaining_float_updated"] += int(
                    rf_res.get("changed") or 0
                )
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
                record_id=f"migration_022_{int(now.timestamp())}",
                before=None,
                after={
                    "projects_processed": counts["projects_processed"],
                    "tasks_updated": counts["tasks_updated"],
                    "critical_count": counts["critical_count"],
                    "remaining_float_updated":
                        counts["remaining_float_updated"],
                    "notes": (
                        "CPM Bug 3.B -- recompute structural float "
                        "(total_float_days) + remaining_float_days in "
                        "WORKING days. Pre-Bug-3.B both were "
                        "calendar-day counts; the mismatch against "
                        "working-day delay/RFI/submittal impact "
                        "produced wrong 'within float' verdicts. "
                        "compute_cpm now writes working_days_between("
                        "ES, LS); compute_remaining_float subtracts "
                        "working-day open impacts. Forward-going every "
                        "Recompute CPM keeps them working-day-correct."
                    ),
                },
                actor_id=None, request_id="migration_022",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Tenants processed:           {len(per_tenant)}")
    total_tasks = sum(c["tasks_updated"] for c in per_tenant.values())
    total_rf = sum(c["remaining_float_updated"] for c in per_tenant.values())
    print(f"Tasks recomputed (float):    {total_tasks}")
    print(f"Remaining-float values updated: {total_rf}")
    if per_tenant:
        print(f"  per-tenant counts:")
        for tid, counts in sorted(per_tenant.items()):
            print(
                f"    {tid[:36]}: "
                f"{counts['projects_processed']} projects / "
                f"{counts['tasks_updated']} tasks / "
                f"{counts['critical_count']} on CP / "
                f"{counts['remaining_float_updated']} rem-float updated"
            )
    print(f"Tenants audited:             {audits_written}")
    print(f"Tenants already audited:     {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
