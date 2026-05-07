"""Followup-fix 1+3 (2026-05-07) — Orphan cleanup pass.

Two pre-existing orphan classes from earlier testing that the live
cascade hooks (Bucket A item 8 + Bucket C3a Phase 5) didn't catch
because they were created before those hooks landed:

(1) daily_report_activities + daily_presence rows whose daily_report_id
    no longer resolves -> hard-delete (parent DR is gone).
(2) delays / qc_issues / safety_events / rfis / photos /
    voice_annotations rows whose daily_report_id is set but doesn't
    resolve -> null the field (the entity itself is independent and
    survives; only the back-pointer is stale).
(3) gating_entities rows whose underlying entity (rfis / submittals /
    work_items) no longer exists or is soft-deleted -> hard-delete
    the gating_entity row, AND null any
    daily_report_activities.constraint_reference_entity_id that
    pointed at it.

Plus a small Phase 4 followup:

(4) Photos with photo_class in (discovery, damage, quality) but
    evidentiary_flag=false -> set flag=true. Phase 4 originally
    computed evidentiary_flag with AND semantics (class AND pin);
    Followup-fix 8 switched to OR. Existing photos uploaded under the
    old logic stay false until this backfill runs.

Idempotent — re-running is a safe no-op besides reading every
collection once. Audit-logs each cascade with op
GATING_ENTITY_ORPHAN_CLEANUP / DR_ORPHAN_CLEANUP.

Run from backend/ with runtime env vars (MONGO_URL, DB_NAME):

  python -m migrations.004_orphan_cleanup
"""
import asyncio
import os
import sys
from pathlib import Path

# Allow `python -m migrations.004_orphan_cleanup` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# Per-bucket reset for stats (operator-readable summary at the end).
def _new_stats():
    return {"examined": 0, "orphans_found": 0, "cleaned": 0, "failures": 0}


# Underlying-entity collection per gating_entities.entity_type. Mirrors
# routers.gating_entities_router._UNDERLYING_COLLECTION; hard-coded here
# so the migration runs without importing FastAPI bits.
UNDERLYING = {
    "rfi": "rfis",
    "submittal": "submittals",
    "field_issue": "work_items",
    "work_item": "work_items",
    "bulletin": "bulletins",
    "coordination_request": "coordination_events",
    "mop": "mops",
    "hot_work_permit": "hot_work_permits",
}


async def run():
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        sys.exit(2)

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    # Patch database globals so audit.write_audit resolves the right
    # client when called outside a request context.
    import database as _database
    _database._client = client  # type: ignore
    _database._db = db          # type: ignore

    from audit import write_audit
    from datetime import datetime, timezone

    # Existing daily_report ids -- single fetch reused across passes.
    dr_ids: set[str] = set(
        d["id"] async for d in db.daily_reports.find({}, {"_id": 0, "id": 1})
    )
    print(f"Loaded {len(dr_ids)} live daily_reports")

    # ----------------------------------------------------------
    # Pass 1: hard-delete orphan daily_report_activities
    # ----------------------------------------------------------
    s_act = _new_stats()
    async for r in db.daily_report_activities.find({}, {"_id": 0, "id": 1, "daily_report_id": 1, "tenant_id": 1}):
        s_act["examined"] += 1
        drid = r.get("daily_report_id")
        if not drid or drid in dr_ids:
            continue
        s_act["orphans_found"] += 1
        try:
            await db.daily_report_activities.delete_one({"id": r["id"]})
            s_act["cleaned"] += 1
            try:
                await write_audit(
                    tenant_id=r.get("tenant_id") or "unknown",
                    table_name="daily_report_activities", op="DR_ORPHAN_CLEANUP",
                    record_id=r["id"], before=r, after=None,
                    actor_id=None, request_id="migration_004",
                )
            except Exception:
                pass
        except Exception as e:
            s_act["failures"] += 1
            print(f"  ! daily_report_activities {r['id']}: {e}")

    # ----------------------------------------------------------
    # Pass 2: hard-delete orphan daily_presence
    # ----------------------------------------------------------
    s_pres = _new_stats()
    async for r in db.daily_presence.find({}, {"_id": 0, "id": 1, "daily_report_id": 1, "tenant_id": 1}):
        s_pres["examined"] += 1
        drid = r.get("daily_report_id")
        if not drid or drid in dr_ids:
            continue
        s_pres["orphans_found"] += 1
        try:
            await db.daily_presence.delete_one({"id": r["id"]})
            s_pres["cleaned"] += 1
            try:
                await write_audit(
                    tenant_id=r.get("tenant_id") or "unknown",
                    table_name="daily_presence", op="DR_ORPHAN_CLEANUP",
                    record_id=r["id"], before=r, after=None,
                    actor_id=None, request_id="migration_004",
                )
            except Exception:
                pass
        except Exception as e:
            s_pres["failures"] += 1
            print(f"  ! daily_presence {r['id']}: {e}")

    # ----------------------------------------------------------
    # Pass 3: null stale daily_report_id on independent entities
    # ----------------------------------------------------------
    LINKED_COLLECTIONS = ["delays", "qc_issues", "safety_events", "rfis",
                           "photos", "voice_annotations"]
    s_link: dict[str, dict] = {c: _new_stats() for c in LINKED_COLLECTIONS}
    for coll in LINKED_COLLECTIONS:
        async for r in db[coll].find(
            {"daily_report_id": {"$nin": [None, ""]}},
            {"_id": 0, "id": 1, "daily_report_id": 1, "tenant_id": 1},
        ):
            s_link[coll]["examined"] += 1
            drid = r.get("daily_report_id")
            if drid in dr_ids:
                continue
            s_link[coll]["orphans_found"] += 1
            try:
                await db[coll].update_one(
                    {"id": r["id"]}, {"$set": {"daily_report_id": None}},
                )
                s_link[coll]["cleaned"] += 1
            except Exception as e:
                s_link[coll]["failures"] += 1
                print(f"  ! {coll} {r['id']}: {e}")

    # ----------------------------------------------------------
    # Pass 4: hard-delete orphan gating_entities (Fix 3)
    # ----------------------------------------------------------
    s_ge = _new_stats()
    cleared_ref_count = 0
    async for ge in db.gating_entities.find({}, {"_id": 0}):
        s_ge["examined"] += 1
        etype = ge.get("entity_type")
        eid = ge.get("entity_id")
        coll = UNDERLYING.get(etype)
        # Unknown entity_type -> leave the row alone (defensive; we don't
        # want to delete data we don't understand).
        if not coll or not eid:
            continue
        u = await db[coll].find_one({"id": eid}, {"_id": 0, "id": 1, "status": 1})
        # Underlying exists AND not soft-deleted -> healthy, skip.
        if u and u.get("status") != "deleted":
            continue
        s_ge["orphans_found"] += 1
        try:
            await db.gating_entities.delete_one({"id": ge["id"]})
            # Reset constraint_reference_entity_id on any DR row that
            # pointed at this gating_entity. Safer to null than leave a
            # dangling reference; user can re-pick via the constraint
            # cell on the daily report.
            res = await db.daily_report_activities.update_many(
                {"constraint_reference_entity_id": ge["id"]},
                {"$set": {"constraint_reference_entity_id": None,
                           "constraint_reference_entity_type": None,
                           "constraint_auto_filled": False}},
            )
            cleared_ref_count += res.modified_count or 0
            s_ge["cleaned"] += 1
            try:
                await write_audit(
                    tenant_id=ge.get("tenant_id") or "unknown",
                    table_name="gating_entities",
                    op="GATING_ENTITY_ORPHAN_CLEANUP",
                    record_id=ge["id"], before=ge,
                    after={"reason": (
                        "underlying entity missing" if not u
                        else "underlying entity soft-deleted"),
                            "constraint_refs_cleared": res.modified_count or 0},
                    actor_id=None, request_id="migration_004",
                )
            except Exception:
                pass
        except Exception as e:
            s_ge["failures"] += 1
            print(f"  ! gating_entities {ge['id']}: {e}")

    # ----------------------------------------------------------
    # Pass 5: backfill photo evidentiary_flag (Fix 8 followup)
    # ----------------------------------------------------------
    s_photo = _new_stats()
    res = await db.photos.update_many(
        {"photo_class": {"$in": ["discovery", "damage", "quality"]},
         "evidentiary_flag": {"$ne": True}},
        {"$set": {"evidentiary_flag": True}},
    )
    s_photo["examined"] = await db.photos.count_documents({})
    s_photo["orphans_found"] = res.matched_count or 0
    s_photo["cleaned"] = res.modified_count or 0

    # ----------------------------------------------------------
    # Summary
    # ----------------------------------------------------------
    def _row(label, s):
        return f"  {label:<32} examined={s['examined']:<6} orphans={s['orphans_found']:<5} cleaned={s['cleaned']:<5} failures={s['failures']}"

    print("\n--- SUMMARY ---")
    print(_row("daily_report_activities", s_act))
    print(_row("daily_presence", s_pres))
    for coll, s in s_link.items():
        print(_row(coll, s))
    print(_row("gating_entities (orphans)", s_ge))
    print(f"  daily_report_activities.constraint_ref cleared: {cleared_ref_count}")
    print(_row("photos evidentiary_flag fix", s_photo))

    total_failures = (
        s_act["failures"] + s_pres["failures"]
        + sum(s["failures"] for s in s_link.values())
        + s_ge["failures"]
    )
    if total_failures:
        print(f"\nFAILED: {total_failures} record(s) couldn't be cleaned.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
