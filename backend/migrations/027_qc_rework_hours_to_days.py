"""Migration 027 -- QC rework duration: convert stored HOURS to DAYS.

QC rework days (2026-05-17) -- the QC rework duration input was
changed from hours to working days (consistent with delay / RFI
impact units). The qc_issues fields were renamed:

    rework_duration_hours -> rework_duration_days
    rework_actual_hours   -> rework_actual_days
    ai_benchmark_hours    -> ai_benchmark_days   (vestigial; converted
                                                  for completeness)

Existing qc_issues rows still carry the OLD hours-named fields with
HOURS values. If left as-is, the renamed code would either not see
them (field renamed) or -- worse -- read an old `8` (meaning 8
hours) as 8 DAYS. This migration converts every existing row so the
effective rework duration is unchanged.

CONVERSION -- the exact rule the now-removed
services/qc_rework_cascade.py::_hours_to_days used:

    days = ceil(hours / 8.0 * 2) / 2.0      (round UP to nearest 0.5)
    hours None / <= 0  ->  0.0

so an old 8-hour row becomes 1.0 day, 16h -> 2.0d, 4h -> 0.5d,
12h -> 1.5d. The synthetic quality_rework delays already store
delay_days in days (the conversion was applied at creation time),
so the delays collection is NOT touched.

Per qc_issues row carrying any *_hours field: $set the *_days
equivalent + $unset the *_hours field. Idempotent: a row with no
*_hours field (already migrated, or a non-rework QC) is skipped.

Audit op:
  QC_REWORK_HOURS_TO_DAYS_MIGRATED -- one row per tenant. Payload
  carries the converted-row count. Idempotent via per-tenant audit
  existence check (same pattern as migrations 013, 018-023).

NOTE: must be run on Emergent (the deployment environment) against
the live database -- it cannot run here (no MONGO_URL/DB_NAME).

Run from backend/:

  python -m migrations.027_qc_rework_hours_to_days
"""
import asyncio
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


AUDIT_OP = "QC_REWORK_HOURS_TO_DAYS_MIGRATED"

# (old hours field -> new days field) pairs converted by this migration.
_FIELD_PAIRS = [
    ("rework_duration_hours", "rework_duration_days"),
    ("rework_actual_hours", "rework_actual_days"),
    ("ai_benchmark_hours", "ai_benchmark_days"),
]


def hours_to_days(hours) -> float:
    """The exact pre-change _hours_to_days rule: hours / 8, rounded UP
    to the nearest 0.5 working day. None / <= 0 -> 0.0."""
    if hours is None:
        return 0.0
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return 0.0
    if h <= 0:
        return 0.0
    return math.ceil(h / 8.0 * 2) / 2.0


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
    print("=== Migration 027: QC rework duration hours -> days ===")

    tenants_cursor = db.tenants.find({}, {"_id": 0, "id": 1})
    per_tenant: dict[str, int] = {}
    async for tenant in tenants_cursor:
        tid = tenant.get("id")
        if not tid:
            continue
        per_tenant.setdefault(tid, 0)

        # Only rows that still carry at least one OLD *_hours field
        # need converting -- idempotent: a re-run finds none.
        old_fields = [old for old, _ in _FIELD_PAIRS]
        cursor = db.qc_issues.find(
            {"tenant_id": tid, "$or": [{f: {"$exists": True}} for f in old_fields]},
            {"_id": 0, "id": 1, **{f: 1 for f in old_fields}},
        )
        async for row in cursor:
            set_ops: dict = {}
            unset_ops: dict = {}
            for old, new in _FIELD_PAIRS:
                if old in row:
                    set_ops[new] = hours_to_days(row.get(old))
                    unset_ops[old] = ""
            if not set_ops:
                continue
            update: dict = {"$set": set_ops}
            if unset_ops:
                update["$unset"] = unset_ops
            try:
                await db.qc_issues.update_one(
                    {"tenant_id": tid, "id": row["id"]}, update,
                )
                per_tenant[tid] += 1
            except Exception as exc:
                print(
                    f"  ! tenant={tid} qc_issue={row.get('id')}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    # ---- Per-tenant audit op (idempotent) ----------------------------
    audits_written = 0
    audits_skipped = 0
    for tid, converted in per_tenant.items():
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": AUDIT_OP},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="qc_issues",
                op=AUDIT_OP,
                record_id=f"migration_027_{int(now.timestamp())}",
                before=None,
                after={
                    "qc_issues_converted": converted,
                    "notes": (
                        "QC rework duration changed from HOURS to "
                        "WORKING DAYS. Converted existing qc_issues "
                        "rows: rework_duration_hours/rework_actual_"
                        "hours/ai_benchmark_hours -> *_days via "
                        "ceil(hours/8*2)/2 (the pre-change _hours_to_"
                        "days rule), so each issue keeps its effective "
                        "rework duration (8h -> 1.0d, 16h -> 2.0d)."
                    ),
                },
                actor_id=None, request_id="migration_027",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"Tenants processed:           {len(per_tenant)}")
    total = sum(per_tenant.values())
    print(f"QC issues converted:         {total}")
    if per_tenant:
        for tid, n in sorted(per_tenant.items()):
            print(f"    {tid[:36]}: {n} qc_issue(s) converted")
    print(f"Tenants audited:             {audits_written}")
    print(f"Tenants already audited:     {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
