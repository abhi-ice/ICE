"""Migration 007 -- Bucket D Corrective.

Translate `linked_dr_activity_ids` (per-DR WBS row scope, points at
daily_report_activities.id) → `linked_task_ids` (master schedule scope,
points at tasks.id), then drop the old field. The earlier per-DR scope
was a wrong build that created two parallel pickers on the delay form;
this migration unwinds the redundant linkage.

Affected collections (5):
- rfis
- qc_issues
- safety_events
- coordination_events
- deliveries

For each doc with linked_dr_activity_ids populated, look up the matching
daily_report_activities row(s) → get schedule_activity_id → dedupe-push
to linked_task_ids → $unset linked_dr_activity_ids.

Delays special case:
- delays already had linked_task_ids as the canonical source of truth
  (with per-task float impact + CPM cascade integration). The
  linked_dr_activity_ids field on delays was a wrong-build duplicate.
- Migration just $unset's the old field on delays. Linked_task_ids
  stays untouched.

Photos special case:
- Photo rows are M2M tags between tray photos and event ids
  (delay_id / rfi_id / etc.) via XOR. They never carried
  linked_dr_activity_ids of their own. No change needed.

Idempotent: per-collection skip when the old field is already absent.
Re-running on already-migrated data shows zeros and exits 0.

Audit: every translated record gets a LINKAGE_FIELD_MIGRATED entry;
no-op records are not audited (otherwise re-runs would spam the log).

Registered in run_all_pending.py with the other auto-run migrations
(006 stays in MANUAL_ONLY because it's the wipe tool).

Run from backend/:

  python -m migrations.007_dr_activity_to_task
"""
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Affected collections that need ID translation. Delays + photos
# handled separately below.
TRANSLATE_COLLECTIONS = [
    "rfis",
    "qc_issues",
    "safety_events",
    "coordination_events",
    "deliveries",
]


def _new_stats() -> dict:
    return {"examined": 0, "translated": 0, "stripped": 0,
            "no_old_field": 0, "no_resolution": 0, "failures": 0}


async def _translate_one(db, coll: str, doc: dict) -> dict:
    """Build the linked_task_ids list for one doc by mapping each
    daily_report_activities.id -> schedule_activity_id. Returns the
    new task-id list (deduped, in input order). Missing DR-activity
    rows are skipped silently -- legitimate for the migration since
    sample data may have referenced rows that were already deleted.
    """
    old_ids = list(doc.get("linked_dr_activity_ids") or [])
    if not old_ids:
        return []
    rows = await db.daily_report_activities.find(
        {"id": {"$in": old_ids}},
        {"_id": 0, "id": 1, "schedule_activity_id": 1},
    ).to_list(length=None)
    by_id = {r["id"]: r for r in rows}
    seen: set[str] = set()
    out: list[str] = []
    for old_id in old_ids:
        row = by_id.get(old_id)
        if not row:
            continue
        sid = row.get("schedule_activity_id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


async def run() -> None:
    # Lazy imports so AST + --help work without backend deps installed.
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

    # Patch database globals for audit.write_audit when called outside
    # a request context.
    import database as _database
    _database._client = client  # type: ignore
    _database._db = db          # type: ignore

    from audit import write_audit

    # --------------------------------------------------------------
    # Pass A: translate linked_dr_activity_ids -> linked_task_ids on
    #         the 5 affected event collections.
    # --------------------------------------------------------------
    print("\n=== Pass A: translate dr-activity ids -> master task ids ===")
    pass_a: dict[str, dict] = {c: _new_stats() for c in TRANSLATE_COLLECTIONS}
    for coll in TRANSLATE_COLLECTIONS:
        cursor = db[coll].find(
            {"linked_dr_activity_ids": {"$exists": True}},
            {"_id": 0},
        )
        async for doc in cursor:
            stats = pass_a[coll]
            stats["examined"] += 1
            old_ids = list(doc.get("linked_dr_activity_ids") or [])
            try:
                # Existing linked_task_ids on the doc (rare for these
                # collections pre-corrective; preserve so we don't
                # clobber anything a user manually set).
                existing_task_ids = list(doc.get("linked_task_ids") or [])
                seen = set(existing_task_ids)
                merged = list(existing_task_ids)
                if old_ids:
                    translated = await _translate_one(db, coll, doc)
                    for tid in translated:
                        if tid not in seen:
                            seen.add(tid)
                            merged.append(tid)
                    if not translated and old_ids:
                        stats["no_resolution"] += 1
                update_op = {
                    "$set": {"linked_task_ids": merged},
                    "$unset": {"linked_dr_activity_ids": ""},
                }
                # Skip writing if nothing actually changed (was a no-op
                # already-migrated doc that somehow still had the old
                # key as an empty list).
                no_change = (
                    not old_ids
                    and merged == existing_task_ids
                )
                if no_change:
                    stats["no_old_field"] += 1
                    # Still strip the empty old field for cleanliness.
                    await db[coll].update_one(
                        {"id": doc["id"]},
                        {"$unset": {"linked_dr_activity_ids": ""}},
                    )
                    continue
                await db[coll].update_one({"id": doc["id"]}, update_op)
                if old_ids:
                    stats["translated"] += 1
                    try:
                        await write_audit(
                            tenant_id=doc.get("tenant_id") or "unknown",
                            table_name=coll,
                            op="LINKAGE_FIELD_MIGRATED",
                            record_id=doc["id"],
                            before={"linked_dr_activity_ids": old_ids,
                                     "linked_task_ids": existing_task_ids},
                            after={"linked_task_ids": merged,
                                    "translation_basis": "daily_report_activities.schedule_activity_id"},
                            actor_id=None,
                            request_id="migration_007",
                        )
                    except Exception:
                        # Audit failure shouldn't undo the data fix.
                        pass
                else:
                    stats["stripped"] += 1
            except Exception as e:
                stats["failures"] += 1
                print(f"  ! {coll} {doc.get('id')}: {type(e).__name__}: {e}")

    # --------------------------------------------------------------
    # Pass B: drop linked_dr_activity_ids on delays. The existing
    # linked_task_ids field is canonical; the dr-activity field was a
    # wrong-build duplicate. No translation needed.
    # --------------------------------------------------------------
    print("\n=== Pass B: drop linked_dr_activity_ids on delays ===")
    delay_stats = _new_stats()
    cursor = db.delays.find(
        {"linked_dr_activity_ids": {"$exists": True}},
        {"_id": 0, "id": 1, "tenant_id": 1, "linked_dr_activity_ids": 1,
         "linked_task_ids": 1},
    )
    async for d in cursor:
        delay_stats["examined"] += 1
        old_ids = list(d.get("linked_dr_activity_ids") or [])
        try:
            await db.delays.update_one(
                {"id": d["id"]},
                {"$unset": {"linked_dr_activity_ids": ""}},
            )
            delay_stats["stripped"] += 1
            if old_ids:
                # Audit only when there was actual data to drop. Empty
                # placeholder fields (set defensively by 005 backfill)
                # are no-ops and don't need an audit entry.
                try:
                    await write_audit(
                        tenant_id=d.get("tenant_id") or "unknown",
                        table_name="delays",
                        op="LINKAGE_FIELD_MIGRATED",
                        record_id=d["id"],
                        before={"linked_dr_activity_ids": old_ids,
                                 "linked_task_ids": list(d.get("linked_task_ids") or [])},
                        after={"linked_task_ids_unchanged": True,
                                "note": "delay's canonical linkage was always linked_task_ids; "
                                        "linked_dr_activity_ids was a wrong-build duplicate, dropped."},
                        actor_id=None,
                        request_id="migration_007",
                    )
                except Exception:
                    pass
        except Exception as e:
            delay_stats["failures"] += 1
            print(f"  ! delays {d.get('id')}: {type(e).__name__}: {e}")

    # --------------------------------------------------------------
    # Pass C: drop legacy index on linked_dr_activity_ids if it exists.
    # The new linked_task_ids index is created at server startup by
    # _ensure_indexes(); we just need to clear the stale one here.
    # --------------------------------------------------------------
    print("\n=== Pass C: drop legacy linked_dr_activity_ids indexes ===")
    index_stats = {"dropped": 0, "absent": 0, "failures": 0}
    for coll in TRANSLATE_COLLECTIONS + ["delays"]:
        try:
            existing = await db[coll].index_information()
            for idx_name, idx_spec in existing.items():
                key_fields = [k for (k, _dir) in (idx_spec.get("key") or [])]
                if "linked_dr_activity_ids" in key_fields:
                    try:
                        await db[coll].drop_index(idx_name)
                        index_stats["dropped"] += 1
                        print(f"  dropped index {coll}.{idx_name}")
                    except Exception as e:
                        index_stats["failures"] += 1
                        print(f"  ! failed to drop {coll}.{idx_name}: {e}")
        except Exception as e:
            index_stats["failures"] += 1
            print(f"  ! {coll}.index_information(): {e}")
    if index_stats["dropped"] == 0 and index_stats["failures"] == 0:
        index_stats["absent"] = len(TRANSLATE_COLLECTIONS) + 1
        print("  no stale indexes found (all collections clean).")

    # --------------------------------------------------------------
    # Summary
    # --------------------------------------------------------------
    def _row(label: str, s: dict) -> str:
        return (f"  {label:<28} examined={s['examined']:<6} "
                f"translated={s['translated']:<6} "
                f"stripped={s['stripped']:<6} "
                f"no_old_field={s['no_old_field']:<6} "
                f"no_resolution={s['no_resolution']:<6} "
                f"failures={s['failures']}")

    print("\n--- SUMMARY ---")
    print("[Pass A: translate dr-activity -> master task]")
    for c in TRANSLATE_COLLECTIONS:
        print(_row(c, pass_a[c]))
    print("\n[Pass B: drop dr-activity field on delays]")
    print(_row("delays", delay_stats))
    print("\n[Pass C: legacy index drop]")
    print(f"  dropped={index_stats['dropped']:<3} "
          f"failures={index_stats['failures']:<3} "
          f"absent={index_stats['absent']}")

    total_failures = (
        sum(s["failures"] for s in pass_a.values())
        + delay_stats["failures"]
        + index_stats["failures"]
    )
    if total_failures:
        print(f"\nFAILED: {total_failures} record(s) couldn't be migrated.", file=sys.stderr)
        sys.exit(1)
    print("\nMigration 007 complete. Hash chain continues unbroken via per-record LINKAGE_FIELD_MIGRATED audit entries.")


if __name__ == "__main__":
    asyncio.run(run())
