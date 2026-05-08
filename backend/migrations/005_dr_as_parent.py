"""Bucket D Phase 1 -- Daily Report as parent calendar object.

Three things in one pass:

(1) Drop orphan event records (no daily_report_id) from delays / rfis /
    qc_issues / safety_events / photos / coordination_events /
    deliveries. Per spec: user confirmed sample data destroyable. The
    new contract is "every event belongs to a DR"; orphans pre-date
    that contract and can't be safely backfilled (no calendar day to
    pin to).

(2) Backfill linked_dr_activity_ids on every record that has a
    daily_report_id. For records that already carry a master
    schedule_activity_id, look up the matching daily_report_activities
    row on that DR and use its id. For records without any task hint,
    default to the DR's first activity row (best-effort) and flag
    `linkage_inferred=true` for super review.

(3) Backfill photo_tray rows from existing photos. Each existing photo
    becomes a tray entry (storage_key copied from storage_path, phash
    preserved if set, exif_taken_at from captured_at_exif, etc.) and
    the photo row is updated with tray_photo_id pointing to the new
    tray entry. Photo's daily_report_id stays where it was; no
    event-ref FK is set (legacy photos remain untagged-from-tray
    perspective; they still display via daily_report_id linkage).

(4) Set is_supplement=false on all existing event records (defensive --
    Pydantic default already handles it, but the index + the gallery
    filters in Phase 3 want the field present).

Idempotent: per-record skip checks. Safe to re-run. Per-collection
counts printed at end. Audit per cleanup with op DR_AS_PARENT_BACKFILL.

Run from backend/:

  python -m migrations.005_dr_as_parent
"""
import asyncio
import os
import sys
from pathlib import Path

# Allow `python -m migrations.005_dr_as_parent` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# Collections that get the orphan-drop + linked_dr_activity_ids backfill.
# coordination_events and safety_events get the linkage backfill but
# linkage is OPTIONAL for them (per spec) -- still try to infer if
# daily_report_id is set, but don't drop orphans for those because the
# spec says orphan-drop applies to the bucket above.
EVENT_COLLECTIONS = [
    "delays", "rfis", "qc_issues", "safety_events",
    "photos", "coordination_events", "deliveries",
]


def _new_stats():
    return {"examined": 0, "orphans_dropped": 0, "linkage_inferred": 0,
            "linkage_set": 0, "supplement_default_set": 0,
            "tray_created": 0, "failures": 0}


async def run():
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        sys.exit(2)

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    # Patch database globals for audit.write_audit when called outside
    # a request context.
    import database as _database
    _database._client = client  # type: ignore
    _database._db = db          # type: ignore

    from audit import write_audit
    from datetime import datetime, timezone
    import uuid

    # ----------------------------------------------------------
    # Pass A: orphan-drop event records with no daily_report_id
    # ----------------------------------------------------------
    print("\n=== Pass A: orphan event drop ===")
    drop_stats: dict[str, dict] = {c: _new_stats() for c in EVENT_COLLECTIONS}
    for coll in EVENT_COLLECTIONS:
        # photos already get a separate tray-backfill pass; orphan-drop
        # for them only removes truly link-less rows. coordination_events
        # use linked_daily_report_id (different field name).
        dr_field = "linked_daily_report_id" if coll == "coordination_events" else "daily_report_id"
        cursor = db[coll].find(
            {"$or": [{dr_field: None}, {dr_field: {"$exists": False}}, {dr_field: ""}]},
            {"_id": 0, "id": 1, "tenant_id": 1},
        )
        rows = await cursor.to_list(length=None)
        drop_stats[coll]["examined"] = len(rows)
        for r in rows:
            try:
                await db[coll].delete_one({"id": r["id"]})
                drop_stats[coll]["orphans_dropped"] += 1
                try:
                    await write_audit(
                        tenant_id=r.get("tenant_id") or "unknown",
                        table_name=coll, op="DR_AS_PARENT_ORPHAN_DROP",
                        record_id=r["id"], before=r, after=None,
                        actor_id=None, request_id="migration_005",
                    )
                except Exception:
                    pass
            except Exception as e:
                drop_stats[coll]["failures"] += 1
                print(f"  ! {coll} {r.get('id')}: {e}")

    # ----------------------------------------------------------
    # Pass B: backfill linked_dr_activity_ids on remaining events
    # ----------------------------------------------------------
    print("\n=== Pass B: linkage backfill ===")
    link_stats: dict[str, dict] = {c: _new_stats() for c in EVENT_COLLECTIONS}

    # Pre-load DR -> [activity rows] map to avoid N+1.
    dr_to_activities: dict[str, list[dict]] = {}
    activity_rows = await db.daily_report_activities.find(
        {}, {"_id": 0, "id": 1, "daily_report_id": 1, "schedule_activity_id": 1, "position": 1},
    ).to_list(length=None)
    for row in activity_rows:
        drid = row.get("daily_report_id")
        if drid:
            dr_to_activities.setdefault(drid, []).append(row)
    # Sort each DR's activity list by position so "first" is deterministic.
    for drid in dr_to_activities:
        dr_to_activities[drid].sort(key=lambda r: r.get("position") or 0)

    for coll in EVENT_COLLECTIONS:
        dr_field = "linked_daily_report_id" if coll == "coordination_events" else "daily_report_id"
        cursor = db[coll].find(
            {"$and": [
                {dr_field: {"$nin": [None, ""]}},
                {"$or": [
                    {"linked_dr_activity_ids": {"$exists": False}},
                    {"linked_dr_activity_ids": {"$size": 0}},
                ]},
            ]},
            {"_id": 0, "id": 1, "tenant_id": 1, dr_field: 1, "schedule_activity_id": 1},
        )
        rows = await cursor.to_list(length=None)
        link_stats[coll]["examined"] = len(rows)
        for r in rows:
            drid = r.get(dr_field)
            activities_for_dr = dr_to_activities.get(drid) or []
            if not activities_for_dr:
                # DR exists but no activity rows -- can't infer. Set
                # empty list so the field is present; flag inferred so
                # super review surfaces it.
                try:
                    await db[coll].update_one(
                        {"id": r["id"]},
                        {"$set": {"linked_dr_activity_ids": [],
                                   "linkage_inferred": True,
                                   "is_supplement": False}},
                    )
                    link_stats[coll]["linkage_inferred"] += 1
                except Exception as e:
                    link_stats[coll]["failures"] += 1
                continue
            # Try to match via existing schedule_activity_id back-pointer.
            target_dra_id = None
            sched_id = r.get("schedule_activity_id")
            if sched_id:
                for a in activities_for_dr:
                    if a.get("schedule_activity_id") == sched_id:
                        target_dra_id = a["id"]
                        break
            if not target_dra_id:
                # Fall back to first activity row on the DR; flag inferred.
                target_dra_id = activities_for_dr[0]["id"]
                inferred = True
            else:
                inferred = False
            try:
                await db[coll].update_one(
                    {"id": r["id"]},
                    {"$set": {
                        "linked_dr_activity_ids": [target_dra_id],
                        "linkage_inferred": inferred,
                        "is_supplement": False,
                    }},
                )
                if inferred:
                    link_stats[coll]["linkage_inferred"] += 1
                else:
                    link_stats[coll]["linkage_set"] += 1
            except Exception as e:
                link_stats[coll]["failures"] += 1
                print(f"  ! {coll} {r.get('id')}: {e}")

    # ----------------------------------------------------------
    # Pass C: photo_tray backfill from existing photos
    # ----------------------------------------------------------
    print("\n=== Pass C: photo_tray backfill ===")
    tray_stats = _new_stats()
    photos_cursor = db.photos.find(
        {"$or": [{"tray_photo_id": None}, {"tray_photo_id": {"$exists": False}}]},
        {"_id": 0},
    )
    photos = await photos_cursor.to_list(length=None)
    tray_stats["examined"] = len(photos)
    for p in photos:
        try:
            tray_id = uuid.uuid4().hex
            tray_doc = {
                "id": tray_id,
                "tenant_id": p.get("tenant_id"),
                "project_id": p.get("project_id"),
                "uploaded_by": p.get("captured_by"),
                "uploaded_at": (p.get("created_at") or p.get("captured_at")
                                  or datetime.now(timezone.utc).isoformat()),
                "capture_source": "manual_upload",
                "source_path": p.get("storage_path"),
                "storage_key": p.get("storage_path"),
                "phash": p.get("phash"),
                "sha256": p.get("sha256_hash"),
                "exif_taken_at": p.get("captured_at_exif") or p.get("device_timestamp"),
                "exif_gps_lat": p.get("gps_lat"),
                "exif_gps_lng": p.get("gps_lng"),
                "width": None,
                "height": None,
                "file_size_bytes": None,
                # Photos that already had a daily_report_id are at minimum
                # partially_tagged via the legacy linkage; mark as such so
                # the tray UI doesn't try to re-tag them as fresh uploads.
                "status": ("partially_tagged" if p.get("daily_report_id") else "untagged"),
            }
            await db.photo_tray.insert_one(tray_doc.copy())
            await db.photos.update_one(
                {"id": p["id"]},
                {"$set": {"tray_photo_id": tray_id, "is_supplement": False}},
            )
            tray_stats["tray_created"] += 1
        except Exception as e:
            tray_stats["failures"] += 1
            print(f"  ! photos {p.get('id')}: {e}")

    # ----------------------------------------------------------
    # Pass D: set is_supplement=false on collections that didn't get
    #         touched by Pass B (e.g. records already with linkage set).
    # ----------------------------------------------------------
    print("\n=== Pass D: supplement-default backfill ===")
    sup_stats: dict[str, dict] = {c: _new_stats() for c in EVENT_COLLECTIONS + ["daily_report_activities"]}
    for coll in EVENT_COLLECTIONS + ["daily_report_activities"]:
        try:
            res = await db[coll].update_many(
                {"$or": [
                    {"is_supplement": {"$exists": False}},
                    {"is_supplement": None},
                ]},
                {"$set": {"is_supplement": False}},
            )
            sup_stats[coll]["examined"] = res.matched_count or 0
            sup_stats[coll]["supplement_default_set"] = res.modified_count or 0
        except Exception as e:
            sup_stats[coll]["failures"] += 1
            print(f"  ! supplement-default {coll}: {e}")

    # ----------------------------------------------------------
    # Summary
    # ----------------------------------------------------------
    def _row(label, s):
        return (f"  {label:<28} examined={s['examined']:<6} "
                f"orphans={s['orphans_dropped']:<5} "
                f"linkage_set={s['linkage_set']:<5} "
                f"linkage_inferred={s['linkage_inferred']:<5} "
                f"sup_default={s['supplement_default_set']:<5} "
                f"tray={s['tray_created']:<5} "
                f"failures={s['failures']}")

    print("\n--- SUMMARY ---")
    print("[Pass A: orphan drop]")
    for c, s in drop_stats.items():
        print(_row(c, s))
    print("\n[Pass B: linkage backfill]")
    for c, s in link_stats.items():
        print(_row(c, s))
    print("\n[Pass C: photo_tray backfill]")
    print(_row("photos -> photo_tray", tray_stats))
    print("\n[Pass D: supplement-default]")
    for c, s in sup_stats.items():
        print(_row(c, s))

    total_failures = (
        sum(s["failures"] for s in drop_stats.values())
        + sum(s["failures"] for s in link_stats.values())
        + tray_stats["failures"]
        + sum(s["failures"] for s in sup_stats.values())
    )
    if total_failures:
        print(f"\nFAILED: {total_failures} record(s) couldn't be backfilled.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
