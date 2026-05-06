"""Bucket C3a Phase 1 — one-time backfill of gating_entities for
existing RFI / Submittal / WorkItem records.

For every existing record without a gating_entity_id, create a
gating_entities row mirroring the entity's current status / dates and
write the new id back onto the source record. Idempotent — safe to
re-run; per-record skip if gating_entity_id is already set.

Run from backend/ with the runtime env vars (MONGO_URL, DB_NAME):

  python -m migrations.002_gating_entities_backfill

Per-entity-type counts: examined / created / skipped / failures.
Exits non-zero if any failures so it's CI-safe.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path

# Allow `python -m migrations.002_gating_entities_backfill` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Lazy import after sys.path adjustment so the service can resolve.
from services.gating_entities import create_for_entity  # noqa: E402


# (entity_type, mongo collection name, source-of-submitted-at field)
ENTITY_PLAN = [
    ("rfi", "rfis", "date_initiated"),
    ("submittal", "submittals", "submitted_at"),  # may be null until first submit
    ("field_issue", "work_items", "opened_at"),
]


def _to_dt(val):
    """Coerce Mongo's whatever-the-field-was into a datetime, falling
    back to epoch-zero so the row is at least in the calendar."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime.combine(val, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            try:
                return datetime.combine(date.fromisoformat(val[:10]), datetime.min.time(), tzinfo=timezone.utc)
            except Exception:
                return None
    return None


async def run():
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        sys.exit(2)
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    # Patch services.gating_entities.get_db so create_for_entity uses our db.
    import services.gating_entities as _ge
    import database as _database
    _database._client = client  # type: ignore
    _database._db = db          # type: ignore

    grand_totals = {"examined": 0, "created": 0, "skipped": 0, "failures": 0}
    failures: list[dict] = []

    for entity_type, collection_name, submitted_field in ENTITY_PLAN:
        examined = created = skipped = local_failures = 0
        async for entity in db[collection_name].find({}, {"_id": 0}):
            examined += 1
            try:
                if entity.get("gating_entity_id"):
                    skipped += 1
                    continue
                # Compute submitted_at — prefer the source field, fall back to created_at.
                raw = entity.get(submitted_field) or entity.get("created_at")
                submitted_dt = _to_dt(raw) or datetime.now(timezone.utc)

                # Pull task linkage from whatever field the entity uses.
                gates: list[str] = []
                if entity.get("schedule_activity_id"):
                    gates = [entity["schedule_activity_id"]]

                ge = await create_for_entity(
                    tenant_id=entity["tenant_id"],
                    project_id=entity.get("project_id"),
                    entity_type=entity_type, entity=entity,
                    submitted_at=submitted_dt, gates_task_ids=gates,
                )
                await db[collection_name].update_one(
                    {"id": entity["id"]},
                    {"$set": {"gating_entity_id": ge["id"]}},
                )
                created += 1
            except Exception as e:
                local_failures += 1
                failures.append({"entity_type": entity_type, "id": entity.get("id"), "error": str(e)})

        print(f"[{entity_type:>13}] examined={examined}  created={created}  skipped={skipped}  failures={local_failures}")
        grand_totals["examined"] += examined
        grand_totals["created"] += created
        grand_totals["skipped"] += skipped
        grand_totals["failures"] += local_failures

    print(f"\nTOTAL examined={grand_totals['examined']}  created={grand_totals['created']}  "
          f"skipped={grand_totals['skipped']}  failures={grand_totals['failures']}")
    for f in failures:
        print(f"  - {f['entity_type']} {f['id']}: {f['error']}")
    if grand_totals["failures"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
