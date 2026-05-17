"""Migration 026 -- CPM Step 3A: recompute compute_cpm for every
project so schedule_reshape_count reflects the CORRECTED counting.

CPM Step 3A changed how compute_cpm counts schedule_reshape_count.
See services/cpm.py.

WHY (the over-report): CPM Step 2.1 added schedule_reshape_count --
the count of tasks whose imported planned_start was rolled forward
to respect predecessors. But Step 2.1 decided "reshaped" against the
predecessors' RECONCILED dates. The ES reconcile cascades (a
predecessor rolled forward pushes every downstream task forward
too), so ONE genuinely-too-early task near a root made every
downstream task's reconciled start later than its imported start --
and every one of them was counted. Migration 025 reported
schedule_reshape_count = 44 on GSA Office Test and 23 on MSU, yet
live verification confirmed those schedules were essentially fine:
the counts were cascade depth, not genuine import problems.

THE STEP-3A FIX: a task is counted ONLY when its OWN imported
planned_start is genuinely earlier than its predecessors' IMPORTED
dates allow. A task dragged forward purely by an upstream reshape
is no longer counted. The ES reconcile itself (and therefore every
float value, is_critical_path, project_finish, and the downstream
cascade) is UNCHANGED -- Step 3A touched only the counting logic.

This migration forces a one-time recompute so the stored CPM state
is refreshed and the audit captures the corrected per-project
schedule_reshape_count. Because Step 3A leaves float / CP /
projected dates byte-for-byte identical, the recompute is a no-op
for every persisted task field -- the only thing that changes is
the schedule_reshape_count REPORTED here (it is a return-payload
diagnostic, not a stored column).

Per (tenant, project) with tasks: run auto_recompute_after_event,
which chains compute_cpm -> compute_projected_finish_dates ->
compute_remaining_float_for_tasks. compute_cpm returns the corrected
schedule_reshape_count, which this migration accumulates per project
for the audit payload + console summary.

Audit op:
  RESHAPE_COUNT_CORRECTED -- one row per tenant. Payload carries the
  per-project corrected schedule_reshape_count.

Idempotent: the compute_* helpers skip writes when a value is
unchanged (and Step 3A changes no stored value at all). The
per-tenant audit existence check skips the audit re-write (same
pattern as migrations 013, 018-025). Must run AFTER 025.

Run from backend/:

  python -m migrations.026_reshape_count_corrected
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


AUDIT_OP = "RESHAPE_COUNT_CORRECTED"


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
    print("=== Migration 026: reshape-count corrected recompute (CPM Step 3A) ===")

    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    per_tenant: dict[str, dict] = {}
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        per_tenant.setdefault(tid, {
            "projects_processed": 0,
            "tasks_total": 0,
            "critical_total": 0,
            "negative_float_total": 0,
            "schedule_reshape_total": 0,
            "projects": [],
        })

        project_ids = await db.tasks.distinct(
            "project_id",
            {"tenant_id": tid, "status": {"$ne": "deleted"}},
        )
        for pid in project_ids:
            if not pid:
                continue
            try:
                # Full chain: compute_cpm (Step 3A counting) ->
                # compute_projected_finish_dates -> compute_remaining_
                # float_for_tasks. auto_recompute_after_event is
                # fail-safe -- a per-project error is logged inside
                # and does not abort the migration. It returns the
                # compute_cpm result dict (or None on a hard CPM
                # failure).
                cpm_result = await auto_recompute_after_event(tid, pid)
                res = cpm_result or {}
                n_tasks = await db.tasks.count_documents({
                    "tenant_id": tid, "project_id": pid,
                    "status": {"$ne": "deleted"},
                })
                cp = int(res.get("critical_count", 0))
                neg = int(res.get("negative_float_count", 0))
                # The Step-3A CORRECTED reshape count.
                reshapes = int(res.get("schedule_reshape_count", 0))

                per_tenant[tid]["projects_processed"] += 1
                per_tenant[tid]["tasks_total"] += int(n_tasks)
                per_tenant[tid]["critical_total"] += cp
                per_tenant[tid]["negative_float_total"] += neg
                per_tenant[tid]["schedule_reshape_total"] += reshapes
                per_tenant[tid]["projects"].append({
                    "project_id": pid,
                    "task_count": int(n_tasks),
                    "critical_count": cp,
                    "negative_float_count": neg,
                    "schedule_reshape_count": reshapes,
                })
                flag = ""
                if reshapes:
                    flag = (
                        f"  <- {reshapes} task(s) genuinely imported "
                        f"too early vs predecessors"
                    )
                print(
                    f"  project {pid}: {n_tasks} tasks / {cp} CP / "
                    f"{neg} neg-float / {reshapes} reshaped "
                    f"(corrected){flag}"
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
                record_id=f"migration_026_{int(now.timestamp())}",
                before=None,
                after={
                    "projects_processed": counts["projects_processed"],
                    "tasks_total": counts["tasks_total"],
                    "critical_total": counts["critical_total"],
                    "negative_float_total": counts["negative_float_total"],
                    "schedule_reshape_total":
                        counts["schedule_reshape_total"],
                    "projects": counts["projects"][:100],
                    "notes": (
                        "CPM Step 3A -- recompute compute_cpm for "
                        "every project so schedule_reshape_count "
                        "reflects the CORRECTED counting. Step 2.1 "
                        "decided 'reshaped' against the predecessors' "
                        "reconciled dates, so one genuine reshape "
                        "near a root cascade-inflated the count down "
                        "the whole chain (migration 025 reported 44 "
                        "on GSA / 23 on MSU). Step 3A counts a task "
                        "ONLY when its own imported planned_start is "
                        "genuinely earlier than its IMPORTED "
                        "predecessors allow. Float, is_critical_path, "
                        "projected dates and the cascade are "
                        "byte-for-byte unchanged -- only the "
                        "schedule_reshape_count diagnostic changed."
                    ),
                },
                actor_id=None, request_id="migration_026",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Tenants processed:           {len(per_tenant)}")
    total_tasks = sum(c["tasks_total"] for c in per_tenant.values())
    total_projects = sum(
        c["projects_processed"] for c in per_tenant.values()
    )
    total_cp = sum(c["critical_total"] for c in per_tenant.values())
    total_neg = sum(c["negative_float_total"] for c in per_tenant.values())
    total_reshape = sum(
        c["schedule_reshape_total"] for c in per_tenant.values()
    )
    print(f"Projects recomputed:         {total_projects}")
    print(f"Tasks refreshed:             {total_tasks}")
    print(f"Critical-path tasks:         {total_cp}")
    print(f"Negative-float tasks:        {total_neg}")
    print(f"Reshaped tasks (corrected):  {total_reshape}")
    if total_reshape:
        print(
            "  NOTE: each corrected reshape is a task whose OWN "
            "imported planned_start is genuinely earlier than its "
            "predecessors allow. Review the imported .mpp dates for "
            "the flagged tasks; downstream tasks dragged forward by "
            "these are no longer counted."
        )
    if per_tenant:
        print(f"  per-tenant / per-project corrected reshape counts:")
        for tid, counts in sorted(per_tenant.items()):
            print(
                f"    tenant {tid[:36]}: "
                f"{counts['projects_processed']} projects / "
                f"{counts['tasks_total']} tasks / "
                f"{counts['schedule_reshape_total']} reshaped"
            )
            for p in counts["projects"]:
                print(
                    f"      project {p['project_id']}: "
                    f"{p['task_count']} tasks, {p['critical_count']} CP, "
                    f"{p['negative_float_count']} neg-float, "
                    f"{p['schedule_reshape_count']} reshaped"
                )
    print(f"Tenants audited:             {audits_written}")
    print(f"Tenants already audited:     {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
