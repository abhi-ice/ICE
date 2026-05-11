"""Migration 011 -- Bucket F Phase 5 pull-forward Risk #1.

Denormalizes the tray's `ai_user_overridden` flag onto the Photo tag row
as `user_overridden_at_tag_time` so the gallery + TIA Section 8
indicators stay accurate even after a tray row is later edited,
archived, or its override state is changed.

Why a snapshot?
---------------
Photo tag rows are the durable, evidentiary record. The tray row above
them is the staging area users freely re-touch (overriding, rejecting,
re-analyzing). Reading "AI was overridden" off the tray at gallery
render time drifts as users continue working in the tray after a tag
was committed. The snapshot captures the truth at tag-creation time,
which is what TIA, evidence packs, and gallery indicators all want.

Backfill strategy
-----------------
Single pass over `photos` rows missing the field. For each, look up the
tray row by `tray_photo_id` (if present) and copy
`photo_tray.ai_user_overridden` -> `photos.user_overridden_at_tag_time`.
When no tray row exists (legacy photos pre-Bucket D), the default is
False. Idempotent via $exists check.

Audit
-----
Single PHOTO_AI_OVERRIDE_SNAPSHOT_BACKFILL op per affected tenant when
writes happen. Re-runs against an already-migrated DB emit nothing.

Registered in run_all_pending.py after 010.

Run from backend/:

  python -m migrations.011_photo_override_snapshot
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    _database._client = client   # type: ignore
    _database._db = db           # type: ignore

    from audit import write_audit

    now = datetime.now(timezone.utc)

    stats = {"examined": 0, "updated_from_tray": 0,
             "updated_default_false": 0, "already_present": 0,
             "failures": 0}
    affected_tenants: set = set()

    # Cache tray override states to avoid N round-trips when many
    # photos share the same tray (multi-tag flows). Keyed by tray id.
    tray_cache: dict[str, bool] = {}

    async def _tray_overridden(tray_id: str) -> bool:
        if not tray_id:
            return False
        if tray_id in tray_cache:
            return tray_cache[tray_id]
        row = await db.photo_tray.find_one(
            {"id": tray_id},
            {"_id": 0, "ai_user_overridden": 1},
        )
        val = bool((row or {}).get("ai_user_overridden", False))
        tray_cache[tray_id] = val
        return val

    print("=== Pass: photos -> user_overridden_at_tag_time snapshot ===")
    cursor = db.photos.find(
        {"user_overridden_at_tag_time": {"$exists": False}},
        {"_id": 0, "id": 1, "tenant_id": 1, "tray_photo_id": 1},
    )
    async for row in cursor:
        stats["examined"] += 1
        try:
            val = await _tray_overridden(row.get("tray_photo_id") or "")
            await db.photos.update_one(
                {"id": row["id"]},
                {"$set": {"user_overridden_at_tag_time": val}},
            )
            if val:
                stats["updated_from_tray"] += 1
            else:
                stats["updated_default_false"] += 1
            if row.get("tenant_id"):
                affected_tenants.add(row["tenant_id"])
        except Exception as exc:
            stats["failures"] += 1
            print(f"  ! photos {row.get('id')}: {type(exc).__name__}: {exc}")

    # Idempotent re-run path -- count already-present rows for the summary.
    already = await db.photos.count_documents(
        {"user_overridden_at_tag_time": {"$exists": True}},
    )
    stats["already_present"] = max(0, already - stats["examined"])

    # Single audit op per affected tenant.
    if stats["updated_from_tray"] + stats["updated_default_false"]:
        for tenant_id in affected_tenants:
            try:
                await write_audit(
                    tenant_id=tenant_id, table_name="system",
                    op="PHOTO_AI_OVERRIDE_SNAPSHOT_BACKFILL",
                    record_id=f"migration_011_{int(now.timestamp())}",
                    before=None, after={
                        "examined_in_run": stats["examined"],
                        "updated_from_tray": stats["updated_from_tray"],
                        "updated_default_false": stats["updated_default_false"],
                    },
                    actor_id=None, request_id="migration_011",
                )
            except Exception as exc:
                print(f"  ! audit {tenant_id}: {type(exc).__name__}: {exc}")

    print("\n--- SUMMARY ---")
    print(f"Tenants affected: {len(affected_tenants)}")
    print(f"  examined={stats['examined']:<6} "
          f"from_tray={stats['updated_from_tray']:<6} "
          f"default_false={stats['updated_default_false']:<6} "
          f"already_present={stats['already_present']:<6} "
          f"failures={stats['failures']}")

    if stats["failures"]:
        print(f"FAILED ({stats['failures']} rows)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
