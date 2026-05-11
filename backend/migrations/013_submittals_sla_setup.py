"""Migration 013 -- Bucket E Phase 1 Submittals SLA + register depth.

Initializes the submittal_review_events collection + indexes, backfills
SLA + register depth defaults onto existing submittal rows, and seeds
project.submittal_review_sla_days on projects that didn't carry it.
Each pass is idempotent (re-running emits no extra writes).

Passes
------
  A. projects: submittal_review_sla_days = 14 +
     submittal_sla_uses_calendar_days = False on rows missing them.
     Default per FAR 52.236-15 / GSAM A/E review SLA standard.

  B. submittals: register-depth defaults. Stamps revision_number=0,
     review_cycle_count=0, total_review_*=0, is_overdue=false,
     urgency_flag=false, sla_status="n_a" on rows where these fields
     are absent. Also stamps discipline=None, procurement_lead_time_
     days=None so the schema is uniform without invented values.

  C. submittals: opportunistic aging backfill. For rows where
     submitted_at exists but submitted_to_ae_at does not, copy
     submitted_at -> submitted_to_ae_at so the SLA clock has a
     starting point. Aging_days computed in-line for status visibility
     even before the Phase 2 service runs the full re-compute.

  D. submittal_review_events: collection bootstrap. Mongo creates it
     on first insert; we touch the indexes here so a fresh deploy
     has them ready (the lifespan _ensure_indexes path also creates
     them on every boot for redundancy).

  E. SUBMITTAL_SLA_INITIALIZED audit op per affected tenant. Single
     row per tenant with the boot snapshot (which passes touched
     how many rows). Idempotent via existence check.

Registered in run_all_pending.py after 012.

Run from backend/:

  python -m migrations.013_submittals_sla_setup
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
    from models import SUBMITTAL_DEFAULT_SLA_DAYS

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    affected_tenants: set = set()

    pass_a = {"examined": 0, "updated": 0, "already_present": 0, "failures": 0}
    pass_b = {"examined": 0, "updated": 0, "already_present": 0, "failures": 0}
    pass_c = {"examined": 0, "updated": 0, "skipped_no_submitted_at": 0,
              "failures": 0}
    pass_d = {"indexes_created": [], "indexes_skipped": []}

    print("=== Migration 013: submittals SLA + register depth ===")

    # =================================================================
    # PASS A -- projects: SLA defaults.
    # =================================================================
    print("\n=== Pass A: projects -> submittal_review_sla_days defaults ===")
    cursor = db.projects.find(
        {"$or": [
            {"submittal_review_sla_days": {"$exists": False}},
            {"submittal_sla_uses_calendar_days": {"$exists": False}},
        ]},
        {"_id": 0, "id": 1, "tenant_id": 1,
         "submittal_review_sla_days": 1,
         "submittal_sla_uses_calendar_days": 1},
    )
    async for row in cursor:
        pass_a["examined"] += 1
        try:
            updates: dict = {}
            if "submittal_review_sla_days" not in row:
                updates["submittal_review_sla_days"] = SUBMITTAL_DEFAULT_SLA_DAYS
            if "submittal_sla_uses_calendar_days" not in row:
                updates["submittal_sla_uses_calendar_days"] = False
            if not updates:
                pass_a["already_present"] += 1
                continue
            await db.projects.update_one(
                {"id": row["id"]}, {"$set": updates},
            )
            pass_a["updated"] += 1
            if row.get("tenant_id"):
                affected_tenants.add(row["tenant_id"])
        except Exception as e:
            pass_a["failures"] += 1
            print(f"  ! projects {row.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS B -- submittals: register-depth + SLA-state defaults.
    # =================================================================
    print("\n=== Pass B: submittals -> register-depth + SLA defaults ===")
    REGISTER_DEFAULTS = {
        "discipline": None,
        "revision_number": 0,
        "superseded_by": None,
        "type_other_description": None,
        "procurement_lead_time_days": None,
        "must_approve_by": None,
        "urgency_flag": False,
        "review_cycle_count": 0,
        "total_review_calendar_days": 0,
        "total_review_working_days": 0,
        "submitted_to_ae_at": None,
        "a_e_review_started_at": None,
        "a_e_review_due_at": None,
        "a_e_review_completed_at": None,
        "received_back_at": None,
        "forwarded_to_sub_at": None,
        "sla_status": "n_a",
        "sla_breach_reason": None,
        "sla_days_override": None,
        "sla_uses_calendar_days_override": None,
        "aging_days": None,
        "aging_working_days": None,
        "is_overdue": False,
        "overdue_days": None,
    }
    cursor = db.submittals.find(
        {},   # walk every submittal; idempotent by per-field $exists check
        {"_id": 0, "id": 1, "tenant_id": 1, **{k: 1 for k in REGISTER_DEFAULTS}},
    )
    async for row in cursor:
        pass_b["examined"] += 1
        try:
            updates: dict = {
                k: v for k, v in REGISTER_DEFAULTS.items() if k not in row
            }
            if not updates:
                pass_b["already_present"] += 1
                continue
            await db.submittals.update_one(
                {"id": row["id"]}, {"$set": updates},
            )
            pass_b["updated"] += 1
            if row.get("tenant_id"):
                affected_tenants.add(row["tenant_id"])
        except Exception as e:
            pass_b["failures"] += 1
            print(f"  ! submittals {row.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS C -- opportunistic aging backfill on already-submitted rows.
    # =================================================================
    print("\n=== Pass C: submittals -> opportunistic aging backfill ===")
    cursor = db.submittals.find(
        {"submitted_at": {"$ne": None},
         "submitted_to_ae_at": None},
        {"_id": 0, "id": 1, "tenant_id": 1, "submitted_at": 1,
         "status": 1, "approved_at": 1},
    )
    async for row in cursor:
        pass_c["examined"] += 1
        try:
            submitted_at = row.get("submitted_at")
            if not submitted_at:
                pass_c["skipped_no_submitted_at"] += 1
                continue
            # Aging is days between submitted_at and now (or
            # approved_at if the submittal is closed).
            try:
                sub_dt = datetime.fromisoformat(
                    str(submitted_at).replace("Z", "+00:00"),
                )
                if sub_dt.tzinfo is None:
                    sub_dt = sub_dt.replace(tzinfo=timezone.utc)
                end_ts = now
                if row.get("status") in ("approved", "approved_as_noted",
                                          "closed") and row.get("approved_at"):
                    end_dt = datetime.fromisoformat(
                        str(row["approved_at"]).replace("Z", "+00:00"),
                    )
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    end_ts = end_dt
                aging = max(0, (end_ts - sub_dt).days)
            except Exception:
                aging = None
            updates = {
                "submitted_to_ae_at": submitted_at,
                "aging_days": aging,
            }
            await db.submittals.update_one(
                {"id": row["id"]}, {"$set": updates},
            )
            pass_c["updated"] += 1
            if row.get("tenant_id"):
                affected_tenants.add(row["tenant_id"])
        except Exception as e:
            pass_c["failures"] += 1
            print(f"  ! submittals aging {row.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS D -- indexes on submittals + submittal_review_events.
    # =================================================================
    print("\n=== Pass D: indexes ===")
    indexes_submittals = [
        ("sub_tenant_project_status",
            [("tenant_id", 1), ("project_id", 1), ("status", 1)]),
        ("sub_tenant_sla_due",
            [("tenant_id", 1), ("a_e_review_due_at", 1)]),
        ("sub_tenant_overdue",
            [("tenant_id", 1), ("is_overdue", 1), ("project_id", 1)]),
        ("sub_tenant_submitted_to_ae",
            [("tenant_id", 1), ("submitted_to_ae_at", -1)]),
        ("sub_tenant_discipline",
            [("tenant_id", 1), ("project_id", 1), ("discipline", 1)]),
    ]
    indexes_events = [
        ("sre_tenant_submittal_performed",
            [("tenant_id", 1), ("submittal_id", 1), ("performed_at", 1)]),
        ("sre_tenant_event_performed",
            [("tenant_id", 1), ("event_type", 1), ("performed_at", -1)]),
        ("sre_tenant_project_event",
            [("tenant_id", 1), ("project_id", 1), ("event_type", 1)]),
    ]

    async def _ensure(coll, name, spec):
        try:
            await coll.create_index(spec, name=name)
            pass_d["indexes_created"].append(f"{coll.name}.{name}")
            print(f"  + index {coll.name}.{name}")
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "name already" in msg:
                pass_d["indexes_skipped"].append(f"{coll.name}.{name}")
                print(f"  = index {coll.name}.{name} (already present)")
            else:
                print(f"  ! index {coll.name}.{name}: {type(exc).__name__}: {exc}")

    for name, spec in indexes_submittals:
        await _ensure(db.submittals, name, spec)
    for name, spec in indexes_events:
        await _ensure(db.submittal_review_events, name, spec)

    # =================================================================
    # PASS E -- audit one row per affected tenant.
    # =================================================================
    total_writes = (pass_a["updated"] + pass_b["updated"] + pass_c["updated"])
    audits_written = 0
    audits_skipped = 0
    if total_writes or pass_d["indexes_created"]:
        for tenant_id in affected_tenants:
            already = await db.audit_log.find_one(
                {"tenant_id": tenant_id, "op": "SUBMITTAL_SLA_INITIALIZED"},
                {"_id": 0, "id": 1},
            )
            if already:
                audits_skipped += 1
                continue
            try:
                await write_audit(
                    tenant_id=tenant_id, table_name="submittals",
                    op="SUBMITTAL_SLA_INITIALIZED",
                    record_id=f"migration_013_{int(now.timestamp())}",
                    before=None, after={
                        "projects_seeded": pass_a["updated"],
                        "submittals_seeded": pass_b["updated"],
                        "aging_backfilled": pass_c["updated"],
                        "indexes_created": pass_d["indexes_created"],
                        "default_sla_days": SUBMITTAL_DEFAULT_SLA_DAYS,
                    },
                    actor_id=None, request_id="migration_013",
                )
                audits_written += 1
            except Exception as exc:
                print(f"  ! audit {tenant_id}: {type(exc).__name__}: {exc}")

    # =================================================================
    # Summary
    # =================================================================
    def _row(label: str, s: dict) -> str:
        return (f"  {label:<22} examined={s.get('examined', 0):<5} "
                f"updated={s.get('updated', 0):<5} "
                f"already={s.get('already_present', 0):<5} "
                f"skipped={s.get('skipped_no_submitted_at', 0):<5} "
                f"failures={s.get('failures', 0)}")

    print("\n--- SUMMARY ---")
    print(f"Tenants affected: {len(affected_tenants)}")
    print(_row("[A] projects", pass_a))
    print(_row("[B] submittals", pass_b))
    print(_row("[C] aging backfill", pass_c))
    print(f"  [D] indexes_created={len(pass_d['indexes_created'])} "
          f"indexes_skipped={len(pass_d['indexes_skipped'])}")
    print(f"  [E] audits_written={audits_written} audits_skipped={audits_skipped}")

    failures = (pass_a["failures"] + pass_b["failures"] + pass_c["failures"])
    if failures:
        print(f"FAILED ({failures} rows)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
