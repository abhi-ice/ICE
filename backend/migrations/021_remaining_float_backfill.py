"""Migration 021 -- CPM Part L-2: backfill remaining_float_days on
every existing task.

Pre-Part-L storage:  tasks row has no remaining_float_days field
                     (Float column on Tasks page showed only
                     structural total_float_days).
Post-Part-L storage: tasks.remaining_float_days = max(0,
                     total_float_days - sum_open_blocker_impacts).

This migration computes the value once per existing task and writes
it. After the migration runs, every auto_recompute_after_event call
keeps the field live (Part L-2 wired into services/cpm.py::
auto_recompute_after_event Step 3).

Idempotent: tasks with remaining_float_days already set are
SKIPPED. Re-running the migration is a no-op besides reading the
tasks collection (the open-blocker walks happen for skipped tasks
but the result isn't written if it equals the existing value -- the
helper already handles that).

Audit op:
  REMAINING_FLOAT_BACKFILLED -- one row per tenant. Payload carries
  per-tenant counts (tasks_scanned, tasks_stamped) so the operator
  running run_all_pending sees migration progress at a glance.

Run from backend/:

  python -m migrations.021_remaining_float_backfill
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# CPM Part L-2 -- audit op name. One row per tenant; idempotent re-runs
# skip the write via the per-tenant audit existence check (same
# pattern as migrations 013, 018, 019, 020).
AUDIT_OP = "REMAINING_FLOAT_BACKFILLED"


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
    from services.task_status import compute_remaining_float_for_tasks

    now = datetime.now(timezone.utc)
    print("=== Migration 021: remaining_float_days backfill ===")

    # Walk every (tenant, project) pair that has tasks. Run the
    # compute_remaining_float_for_tasks helper once per project --
    # it loops every task in that project, computes the open-impact
    # sum across delays + RFIs + over-SLA submittals, and stamps
    # remaining_float_days. The helper is itself idempotent (skips
    # the write when the value equals the existing one), so tasks
    # already carrying a remaining_float_days value from earlier
    # writes are no-ops.
    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    per_tenant_counts: dict[str, dict[str, int]] = {}
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        per_tenant_counts.setdefault(tid, {
            "projects_processed": 0,
            "tasks_scanned": 0,
            "tasks_stamped": 0,
        })

        # Pull unique project_ids that this tenant has tasks for.
        project_ids = await db.tasks.distinct(
            "project_id",
            {"tenant_id": tid, "status": {"$ne": "deleted"}},
        )
        for pid in project_ids:
            if not pid:
                continue
            try:
                res = await compute_remaining_float_for_tasks(
                    db, tid, pid,
                )
                per_tenant_counts[tid]["projects_processed"] += 1
                per_tenant_counts[tid]["tasks_scanned"] += int(
                    res.get("scanned") or 0
                )
                per_tenant_counts[tid]["tasks_stamped"] += int(
                    res.get("changed") or 0
                )
            except Exception as exc:
                print(
                    f"  ! tenant={tid} project={pid}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    # ---- Per-tenant audit op (idempotent) ----------------------------
    # Walks every tenant we touched. Re-run safety: per-tenant audit
    # existence check skips a second write.
    audits_written = 0
    audits_skipped = 0
    for tid, counts in per_tenant_counts.items():
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
                record_id=f"migration_021_{int(now.timestamp())}",
                before=None,
                after={
                    "projects_processed": counts["projects_processed"],
                    "tasks_scanned": counts["tasks_scanned"],
                    "tasks_stamped": counts["tasks_stamped"],
                    "notes": (
                        "CPM Part L-2 -- backfill remaining_float_days "
                        "= max(0, total_float_days - sum_open_blocker_"
                        "impacts) on every task. compute_remaining_"
                        "float_for_tasks now fires on every "
                        "auto_recompute_after_event so the field stays "
                        "live as delays / RFIs / submittals are added "
                        "or resolved. Tasks page Rem Float column "
                        "reads directly from this field."
                    ),
                },
                actor_id=None, request_id="migration_021",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Tenants processed:           {len(per_tenant_counts)}")
    total_scanned = sum(c["tasks_scanned"] for c in per_tenant_counts.values())
    total_stamped = sum(c["tasks_stamped"] for c in per_tenant_counts.values())
    print(f"Tasks scanned:               {total_scanned}")
    print(f"Tasks stamped:               {total_stamped}")
    if per_tenant_counts:
        print(f"  per-tenant counts:")
        for tid, counts in sorted(per_tenant_counts.items()):
            print(
                f"    {tid[:36]}: "
                f"{counts['projects_processed']} projects / "
                f"{counts['tasks_scanned']} scanned / "
                f"{counts['tasks_stamped']} stamped"
            )
    print(f"Tenants audited:             {audits_written}")
    print(f"Tenants already audited:     {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
