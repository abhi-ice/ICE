"""Migration 024 -- CPM Step 2 (Issue 3.D): recompute every task off
the IMPORTED-DATE-ANCHORED forward pass.

CPM Step 2 changed compute_cpm's forward pass: it now ANCHORS ES/EF
on each task's IMPORTED planned_start/planned_finish (the contract
.mpp truth) for EVERY task, instead of re-deriving a non-root's ES
from its predecessors via MAX-convergence. See services/cpm.py.

Pre-Step-2 storage on tasks:
  total_float_days   -- derived from a RE-DERIVED early-start
                        baseline that diverged from the imported
                        planned dates the cascade + displayed
                        columns used (Issue 3.D root cause).
  is_critical_path   -- (re-derived float <= 0); a task could be
                        flagged critical/non-critical off a baseline
                        that did not match its contract dates.

Post-Step-2 storage:
  total_float_days   -- working_days_between(imported_ES, LS); MAY
                        BE NEGATIVE when the imported schedule is
                        internally inconsistent (a successor's
                        imported start precedes a predecessor's
                        imported finish). NOT clamped -- a negative
                        structural float is a real contract defect
                        a federal tool must EXPOSE.
  is_critical_path   -- (float <= 0); rule unchanged, but now off
                        the imported-date baseline.

The Step-2 change is forward-going: every auto_recompute_after_event
from this point uses the imported-date anchor. But tasks last
recomputed before the deploy carry stale re-derived floats. This
migration forces a one-time clean refresh AND surfaces any
import-quality problems (negative float, duration mismatches) so
the operator running run_all_pending sees which projects carry
inconsistent contract schedules.

Per (tenant, project) with tasks: run auto_recompute_after_event,
which chains:
  1. compute_cpm                     -- imported-date-anchored float
                                        + CP flag; returns
                                        negative_float_count +
                                        duration_mismatch_count.
  2. compute_projected_finish_dates  -- projected dates + slip_days
                                        (UNCHANGED by Step 2).
  3. compute_remaining_float_for_tasks -- remaining_float_days
                                        (UNCHANGED by Step 2).

auto_recompute_after_event returns the compute_cpm result dict, so
this migration accumulates negative_float_count +
duration_mismatch_count per tenant for the audit payload + console
summary.

Audit op:
  CPM_IMPORTED_ANCHOR_RECOMPUTED -- one row per tenant. Payload
  carries per-tenant counts INCLUDING negative_float_total and
  duration_mismatch_total -- the import-quality signal Step 2
  exists to surface.

Idempotent: the compute_* helpers each skip writes when a value is
unchanged. Re-running re-walks every project but writes nothing if
the values are already import-anchored. The per-tenant audit
existence check skips the audit re-write (same pattern as
migrations 013, 018-023). Must run AFTER 023 -- 023 sets the
cascade-aware remaining float; 024 re-anchors the structural float
the cascade reads. (compute_projected_finish_dates +
compute_remaining_float_for_tasks are UNCHANGED by Step 2, so
re-running them here is a no-op beyond a fresh structural float.)

Run from backend/:

  python -m migrations.024_imported_anchor_recompute
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


AUDIT_OP = "CPM_IMPORTED_ANCHOR_RECOMPUTED"


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
    print("=== Migration 024: imported-date-anchor recompute (CPM Step 2) ===")

    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    per_tenant: dict[str, dict] = {}
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        per_tenant.setdefault(tid, {
            "projects_processed": 0,
            "tasks_total": 0,
            "negative_float_total": 0,
            "duration_mismatch_total": 0,
            "flagged_projects": [],
        })

        project_ids = await db.tasks.distinct(
            "project_id",
            {"tenant_id": tid, "status": {"$ne": "deleted"}},
        )
        for pid in project_ids:
            if not pid:
                continue
            try:
                # Full chain: compute_cpm (imported-date anchor) ->
                # compute_projected_finish_dates -> compute_remaining_
                # float_for_tasks. auto_recompute_after_event is
                # fail-safe -- a per-project error is logged inside
                # and does not abort the migration. It returns the
                # compute_cpm result dict (or None on a hard CPM
                # failure).
                cpm_result = await auto_recompute_after_event(tid, pid)
                per_tenant[tid]["projects_processed"] += 1
                n = await db.tasks.count_documents({
                    "tenant_id": tid, "project_id": pid,
                    "status": {"$ne": "deleted"},
                })
                per_tenant[tid]["tasks_total"] += int(n)

                # Step 2 import-quality signal. cpm_result may be None
                # (compute_cpm raised) or lack the keys (no tasks /
                # cycle_detected) -- .get(..., 0) covers all cases.
                neg = int((cpm_result or {}).get("negative_float_count", 0))
                mismatch = int(
                    (cpm_result or {}).get("duration_mismatch_count", 0)
                )
                per_tenant[tid]["negative_float_total"] += neg
                per_tenant[tid]["duration_mismatch_total"] += mismatch
                if neg or mismatch:
                    per_tenant[tid]["flagged_projects"].append({
                        "project_id": pid,
                        "negative_float_count": neg,
                        "duration_mismatch_count": mismatch,
                    })
                    print(
                        f"  ~ tenant={tid} project={pid}: "
                        f"{neg} negative-float task(s), "
                        f"{mismatch} duration mismatch(es) "
                        f"-- inconsistent imported schedule"
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
                record_id=f"migration_024_{int(now.timestamp())}",
                before=None,
                after={
                    "projects_processed": counts["projects_processed"],
                    "tasks_total": counts["tasks_total"],
                    "negative_float_total": counts["negative_float_total"],
                    "duration_mismatch_total":
                        counts["duration_mismatch_total"],
                    # Cap the flagged-projects list so a tenant with
                    # many inconsistent schedules doesn't bloat the
                    # audit row; the console summary prints all.
                    "flagged_projects": counts["flagged_projects"][:50],
                    "notes": (
                        "CPM Step 2 (Issue 3.D) -- recompute every "
                        "task's structural float off the IMPORTED-"
                        "DATE-ANCHORED forward pass (ES/EF read from "
                        "imported planned_start/planned_finish, no "
                        "longer re-derived from predecessors). "
                        "total_float_days may now be NEGATIVE where "
                        "the imported schedule is internally "
                        "inconsistent -- surfaced, not clamped. "
                        "negative_float_total / duration_mismatch_"
                        "total quantify import quality. Forward-going "
                        "every auto_recompute_after_event uses the "
                        "imported-date anchor."
                    ),
                },
                actor_id=None, request_id="migration_024",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Tenants processed:           {len(per_tenant)}")
    total_tasks = sum(c["tasks_total"] for c in per_tenant.values())
    total_projects = sum(c["projects_processed"] for c in per_tenant.values())
    total_neg = sum(c["negative_float_total"] for c in per_tenant.values())
    total_mismatch = sum(
        c["duration_mismatch_total"] for c in per_tenant.values()
    )
    print(f"Projects recomputed:         {total_projects}")
    print(f"Tasks refreshed:             {total_tasks}")
    print(f"Negative-float tasks:        {total_neg}")
    print(f"Duration mismatches:         {total_mismatch}")
    if total_neg or total_mismatch:
        print(
            "  NOTE: negative float / duration mismatches indicate "
            "imported schedules whose contract dates are internally "
            "inconsistent. Step 2 SURFACES these (it does not hide "
            "or clamp them) -- review the flagged projects below."
        )
    if per_tenant:
        print(f"  per-tenant counts:")
        for tid, counts in sorted(per_tenant.items()):
            print(
                f"    {tid[:36]}: "
                f"{counts['projects_processed']} projects / "
                f"{counts['tasks_total']} tasks / "
                f"{counts['negative_float_total']} neg-float / "
                f"{counts['duration_mismatch_total']} mismatch"
            )
            for fp in counts["flagged_projects"]:
                print(
                    f"      flagged project {fp['project_id']}: "
                    f"{fp['negative_float_count']} neg-float, "
                    f"{fp['duration_mismatch_count']} mismatch"
                )
    print(f"Tenants audited:             {audits_written}")
    print(f"Tenants already audited:     {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
