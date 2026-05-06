"""One-time migration for the Phase A Delays restructure.

Idempotent — safe to re-run. Walks every `delays` document and:

  - Backfills `linked_task_ids` from the legacy single `schedule_activity_id`
    when the new array is missing/empty.
  - Backfills `per_task_impacts` with one entry per linked task, copying
    `delay_days` into `projected_impact_days` and snapshotting the task's
    current `total_float_days` and `is_critical_path` for the
    consumes_float_only / pushes_critical_path flags.
  - Sets `logged_date` from the existing `date` field if missing.
  - Ensures `status` defaults to `open` if missing.

Run from the backend dir with the runtime's MONGO_URL/DB_NAME env vars set:

  python -m migrations.001_delays_restructure

Reports a count of records examined / updated / skipped.
"""
import asyncio
import os
import sys
from pathlib import Path

# Allow `python -m migrations.001_delays_restructure` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


async def run():
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        sys.exit(2)

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    examined = 0
    updated = 0
    skipped_already_migrated = 0
    failures: list[dict] = []

    async for d in db.delays.find({}, {"_id": 0}):
        examined += 1
        try:
            already_has_linked = bool(d.get("linked_task_ids"))
            already_has_impacts = bool(d.get("per_task_impacts"))
            has_logged = bool(d.get("logged_date"))
            has_status = bool(d.get("status"))
            if already_has_linked and already_has_impacts and has_logged and has_status:
                skipped_already_migrated += 1
                continue

            sched_id = d.get("schedule_activity_id")
            linked_ids = d.get("linked_task_ids") or ([sched_id] if sched_id else [])
            linked_ids = [x for x in linked_ids if x]

            # Build per_task_impacts if missing.
            existing_impacts = d.get("per_task_impacts") or []
            existing_by_id = {i.get("task_id"): i for i in existing_impacts if i.get("task_id")}

            impacts: list[dict] = []
            for tid in linked_ids:
                if tid in existing_by_id:
                    impacts.append(existing_by_id[tid])
                    continue
                t = await db.tasks.find_one(
                    {"tenant_id": d["tenant_id"], "id": tid},
                    {"_id": 0, "total_float_days": 1, "is_critical_path": 1},
                )
                f = (t or {}).get("total_float_days")
                f_val = float(f) if f is not None else None
                proj = float(d.get("delay_days") or 0)
                consumes = f_val is not None and proj <= f_val
                pushes_cp = (not consumes) and bool((t or {}).get("is_critical_path"))
                impacts.append({
                    "task_id": tid,
                    "projected_impact_days": proj,
                    "float_at_logging_days": f_val,
                    "consumes_float_only": consumes,
                    "pushes_critical_path": pushes_cp,
                })

            updates: dict = {}
            if not already_has_linked:
                updates["linked_task_ids"] = linked_ids
            if not already_has_impacts:
                updates["per_task_impacts"] = impacts
            if not has_logged:
                updates["logged_date"] = d.get("date")
            if not has_status:
                updates["status"] = "open"
            # Ensure new optional fields exist with None defaults so reads
            # don't 500 on missing keys for older records.
            for key in ("resolved_date", "resolved_note", "resolved_by_user_id",
                        "ai_report", "ai_report_generated_at"):
                if key not in d:
                    updates[key] = None

            if updates:
                await db.delays.update_one({"id": d["id"]}, {"$set": updates})
                updated += 1
            else:
                skipped_already_migrated += 1
        except Exception as e:
            failures.append({"id": d.get("id"), "error": str(e)})

    print(f"examined: {examined}")
    print(f"updated: {updated}")
    print(f"skipped (already migrated): {skipped_already_migrated}")
    print(f"failures: {len(failures)}")
    for f in failures:
        print(f"  - {f['id']}: {f['error']}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
