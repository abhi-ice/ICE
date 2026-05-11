"""Migration 010 -- Bucket F Phase 1 Photo AI setup.

Adds the AI lifecycle fields to existing rows in 4 passes. Each pass
is idempotent ($exists checks) and emits a single
PHOTO_AI_BACKFILL audit op per affected tenant on a real write.

Passes
------
  A. photo_tray: stamp ai_analysis_status="skipped" on pre-existing
     tray rows so we never accidentally bill for analyzing photos
     uploaded before AI shipped. Fresh uploads after migration land
     with "pending" via the Pydantic default. Other AI fields get
     their typed defaults (None / [] / False / etc.).

  B. photos (tag rows): add created_via_ai_suggestion=false +
     ai_suggestion_id=None + ai_confidence_at_tag_time=None. Pure
     additive; no semantics change for legacy tags.

  C. projects: add is_cui=false + ai_enabled=true. Safe defaults.

  D. tenants: add ai_enabled=true + ai_monthly_budget_usd=100.00 +
     ai_current_month_spend_usd=0 + ai_current_month_resets_at=first
     of next month. Conservative budget; ops admins raise via the
     Phase 5 settings UI.

Audit
-----
Single PHOTO_AI_BACKFILL op per tenant that actually had writes.
Idempotent re-runs emit nothing (matches the existing pattern from
migrations 005 / 008 / 009).

Registered in run_all_pending.py after 009.

Run from backend/:

  python -m migrations.010_photo_ai_setup
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _first_of_next_month(now: datetime) -> datetime:
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)


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
    now_iso = now.isoformat()
    reset_at = _first_of_next_month(now).isoformat()

    pass_a = {"examined": 0, "updated": 0, "already_present": 0,
              "failures": 0}
    pass_b = {"examined": 0, "updated": 0, "already_present": 0,
              "failures": 0}
    pass_c = {"examined": 0, "updated": 0, "already_present": 0,
              "failures": 0}
    pass_d = {"examined": 0, "updated": 0, "already_present": 0,
              "failures": 0}
    affected_tenants: set = set()

    # =================================================================
    # PASS A: photo_tray -- stamp ai_analysis_status="skipped" on rows
    # that don't yet have the AI field set. Fresh uploads after this
    # land with "pending" via the Pydantic default.
    # =================================================================
    print("\n=== Pass A: photo_tray -> AI field defaults (skipped) ===")
    cursor = db.photo_tray.find(
        {"$or": [
            {"ai_analysis_status": {"$exists": False}},
            {"ai_analysis_status": None},
        ]},
        {"_id": 0, "id": 1, "tenant_id": 1},
    )
    async for row in cursor:
        pass_a["examined"] += 1
        try:
            await db.photo_tray.update_one(
                {"id": row["id"]},
                {"$set": {
                    "ai_analysis_status": "skipped",
                    "ai_analysis_started_at": None,
                    "ai_analysis_completed_at": None,
                    "ai_suggested_class": None,
                    "ai_suggested_caption": None,
                    "ai_suggested_event_type": None,
                    "ai_suggested_trade": None,
                    "ai_confidence_score": None,
                    "ai_raw_response": None,
                    "ai_model_version": None,
                    "ai_user_overridden": False,
                    "ai_user_override_reason": None,
                    "ai_safety_concerns_detected": [],
                    "ai_failure_reason": "pre_ai_upload",
                    "ai_cost_tokens_input": None,
                    "ai_cost_tokens_output": None,
                    "ai_reused_from_phash": False,
                }},
            )
            pass_a["updated"] += 1
            if row.get("tenant_id"):
                affected_tenants.add(row["tenant_id"])
        except Exception as e:
            pass_a["failures"] += 1
            print(f"  ! photo_tray {row.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS B: photos -- AI lineage defaults on legacy tags.
    # =================================================================
    print("\n=== Pass B: photos -> AI lineage defaults ===")
    cursor = db.photos.find(
        {"$or": [
            {"created_via_ai_suggestion": {"$exists": False}},
            {"ai_suggestion_id": {"$exists": False}},
            {"ai_confidence_at_tag_time": {"$exists": False}},
        ]},
        {"_id": 0, "id": 1, "tenant_id": 1},
    )
    async for row in cursor:
        pass_b["examined"] += 1
        try:
            await db.photos.update_one(
                {"id": row["id"]},
                {"$set": {
                    "created_via_ai_suggestion": False,
                    "ai_suggestion_id": None,
                    "ai_confidence_at_tag_time": None,
                }},
            )
            pass_b["updated"] += 1
            if row.get("tenant_id"):
                affected_tenants.add(row["tenant_id"])
        except Exception as e:
            pass_b["failures"] += 1
            print(f"  ! photos {row.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS C: projects -- is_cui=false + ai_enabled=true.
    # =================================================================
    print("\n=== Pass C: projects -> is_cui + ai_enabled defaults ===")
    cursor = db.projects.find(
        {"$or": [
            {"is_cui": {"$exists": False}},
            {"ai_enabled": {"$exists": False}},
        ]},
        {"_id": 0, "id": 1, "tenant_id": 1, "is_cui": 1, "ai_enabled": 1},
    )
    async for row in cursor:
        pass_c["examined"] += 1
        try:
            updates: dict = {}
            if "is_cui" not in row:
                updates["is_cui"] = False
            if "ai_enabled" not in row:
                updates["ai_enabled"] = True
            if not updates:
                pass_c["already_present"] += 1
                continue
            await db.projects.update_one(
                {"id": row["id"]}, {"$set": updates},
            )
            pass_c["updated"] += 1
            if row.get("tenant_id"):
                affected_tenants.add(row["tenant_id"])
        except Exception as e:
            pass_c["failures"] += 1
            print(f"  ! projects {row.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS D: tenants -- AI budget fields.
    # =================================================================
    print("\n=== Pass D: tenants -> AI budget defaults ===")
    cursor = db.tenants.find(
        {"$or": [
            {"ai_enabled": {"$exists": False}},
            {"ai_monthly_budget_usd": {"$exists": False}},
            {"ai_current_month_spend_usd": {"$exists": False}},
            {"ai_current_month_resets_at": {"$exists": False}},
        ]},
        {"_id": 0, "id": 1, "ai_enabled": 1,
         "ai_monthly_budget_usd": 1,
         "ai_current_month_spend_usd": 1,
         "ai_current_month_resets_at": 1},
    )
    async for row in cursor:
        pass_d["examined"] += 1
        try:
            updates: dict = {}
            if "ai_enabled" not in row:
                updates["ai_enabled"] = True
            if "ai_monthly_budget_usd" not in row:
                updates["ai_monthly_budget_usd"] = 100.0
            if "ai_current_month_spend_usd" not in row:
                updates["ai_current_month_spend_usd"] = 0.0
            if "ai_current_month_resets_at" not in row:
                updates["ai_current_month_resets_at"] = reset_at
            if not updates:
                pass_d["already_present"] += 1
                continue
            await db.tenants.update_one(
                {"id": row["id"]}, {"$set": updates},
            )
            pass_d["updated"] += 1
            affected_tenants.add(row["id"])
        except Exception as e:
            pass_d["failures"] += 1
            print(f"  ! tenants {row.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # Audit: single PHOTO_AI_BACKFILL per affected tenant (only when
    # something was written).
    # =================================================================
    total_writes = (pass_a["updated"] + pass_b["updated"]
                    + pass_c["updated"] + pass_d["updated"])
    if total_writes:
        for tenant_id in affected_tenants:
            try:
                await write_audit(
                    tenant_id=tenant_id, table_name="system",
                    op="PHOTO_AI_BACKFILL",
                    record_id=f"migration_010_{int(now.timestamp())}",
                    before=None, after={
                        "pass_a_tray_rows": pass_a["updated"],
                        "pass_b_photo_tag_rows": pass_b["updated"],
                        "pass_c_project_rows": pass_c["updated"],
                        "pass_d_tenant_rows": pass_d["updated"],
                        "default_monthly_budget_usd": 100.0,
                        "reset_at": reset_at,
                    },
                    actor_id=None, request_id="migration_010",
                )
            except Exception as e:
                print(f"  ! audit {tenant_id}: {type(e).__name__}: {e}")

    # =================================================================
    # Summary
    # =================================================================
    def _row(label: str, s: dict) -> str:
        return (f"  {label:<24} examined={s['examined']:<6} "
                f"updated={s['updated']:<6} "
                f"already={s['already_present']:<5} "
                f"failures={s['failures']}")

    print("\n--- SUMMARY ---")
    print(f"Tenants affected: {len(affected_tenants)}")
    print(_row("[A] photo_tray", pass_a))
    print(_row("[B] photos", pass_b))
    print(_row("[C] projects", pass_c))
    print(_row("[D] tenants", pass_d))

    failures = (pass_a["failures"] + pass_b["failures"]
                + pass_c["failures"] + pass_d["failures"])
    if failures:
        print(f"\nFAILED: {failures} record(s).", file=sys.stderr)
        sys.exit(1)
    print("\nMigration 010 complete. Photo AI fields seeded.")


if __name__ == "__main__":
    asyncio.run(run())
