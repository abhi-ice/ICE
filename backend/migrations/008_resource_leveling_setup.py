"""Migration 008 -- Bucket H Phase 1 Resource Leveling setup.

Lays down the supply / demand foundation. Five passes:

  A. Add required_trade / required_headcount / required_hours fields
     to existing rows in the `tasks` collection (master schedule).
     Defaults to None for all three. Where a task already carries
     trade_code, we backfill required_trade from it as a sensible
     default (the operator can still override).
  B. Add default_headcount / company_name / home_project_id fields
     to existing rows in the `crews` collection. default_headcount
     backfilled from target_size; company_name + home_project_id
     left None.
  C. Backfill 90 days of crew_capacity rows starting from today for
     every active crew. total_headcount = default_headcount,
     committed_headcount = 0, available_headcount = total. Saturdays
     and Sundays default to is_holiday=true (federal construction
     calendar -- operator overrides per crew/date later).
  D. Backfill crew_demands rows from tasks where required_trade is
     populated. One row per (project, trade, date) tuple in the
     task's planned_start..planned_finish range. source_task_ids
     accumulates contributors. is_critical_path mirrors whether ANY
     contributing task is on the CP.
  E. Smoke-verify the gap computation reaches sensible numbers. NO
     persistence in Phase 1 -- gaps are computed on read by the
     Phase 2 service. This pass just walks (tenant, trade, date) in
     scope and prints supply/demand distribution so the operator
     can eyeball the migration health.

Idempotent: per-pass skip checks. Re-running on already-migrated
data shows zero new writes. Audit entries fire only on real
changes (no audit spam on no-op re-runs).

Audit op: RESOURCE_LEVELING_BACKFILL (single op across all passes
so the chain-of-custody record reads cleanly).

Registered in run_all_pending.py with the other auto-run
migrations. Run from backend/:

  python -m migrations.008_resource_leveling_setup
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BACKFILL_WINDOW_DAYS = 90


def _new_stats() -> dict:
    return {"examined": 0, "added": 0, "skipped_already_present": 0,
            "skipped_no_data": 0, "failures": 0}


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5   # 5 = Sat, 6 = Sun


def _date_range(start: date, end: date):
    """Inclusive on both ends. Caller bounds the window."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


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
    today = now.date()
    horizon = today + timedelta(days=BACKFILL_WINDOW_DAYS - 1)

    # =================================================================
    # PASS A: tasks -> add demand fields (required_trade/headcount/hours)
    # =================================================================
    print("\n=== Pass A: tasks -> add required_trade/headcount/hours fields ===")
    pass_a = _new_stats()
    cursor = db.tasks.find(
        {"$or": [
            {"required_trade": {"$exists": False}},
            {"required_headcount": {"$exists": False}},
            {"required_hours": {"$exists": False}},
        ]},
        {"_id": 0, "id": 1, "tenant_id": 1, "trade_code": 1,
         "required_trade": 1, "required_headcount": 1, "required_hours": 1},
    )
    async for t in cursor:
        pass_a["examined"] += 1
        try:
            updates: dict = {}
            if "required_trade" not in t:
                # Backfill from trade_code if present so the operator
                # has a useful default; null otherwise.
                updates["required_trade"] = t.get("trade_code") or None
            if "required_headcount" not in t:
                updates["required_headcount"] = None
            if "required_hours" not in t:
                updates["required_hours"] = None
            if not updates:
                pass_a["skipped_already_present"] += 1
                continue
            await db.tasks.update_one({"id": t["id"]}, {"$set": updates})
            pass_a["added"] += 1
        except Exception as e:
            pass_a["failures"] += 1
            print(f"  ! tasks {t.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS B: crews -> add supply fields (default_headcount/company/home)
    # =================================================================
    print("\n=== Pass B: crews -> add default_headcount/company_name/home_project_id ===")
    pass_b = _new_stats()
    cursor = db.crews.find(
        {"$or": [
            {"default_headcount": {"$exists": False}},
            {"company_name": {"$exists": False}},
            {"home_project_id": {"$exists": False}},
        ]},
        {"_id": 0, "id": 1, "tenant_id": 1, "target_size": 1,
         "default_headcount": 1, "company_name": 1, "home_project_id": 1},
    )
    async for c in cursor:
        pass_b["examined"] += 1
        try:
            updates: dict = {}
            if "default_headcount" not in c:
                updates["default_headcount"] = int(c.get("target_size") or 0)
            if "company_name" not in c:
                updates["company_name"] = None
            if "home_project_id" not in c:
                updates["home_project_id"] = None
            if not updates:
                pass_b["skipped_already_present"] += 1
                continue
            await db.crews.update_one({"id": c["id"]}, {"$set": updates})
            pass_b["added"] += 1
        except Exception as e:
            pass_b["failures"] += 1
            print(f"  ! crews {c.get('id')}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS C: crew_capacity backfill for 90 days from today
    # =================================================================
    print(f"\n=== Pass C: backfill crew_capacity for {BACKFILL_WINDOW_DAYS} days from {today} ===")
    pass_c = _new_stats()
    crews = await db.crews.find(
        {"status": {"$ne": "archived"}},
        {"_id": 0, "id": 1, "tenant_id": 1, "trade": 1, "company_name": 1,
         "default_headcount": 1, "target_size": 1},
    ).to_list(length=None)
    for c in crews:
        cid = c["id"]
        trade = c.get("trade") or "(unknown)"
        company = c.get("company_name")
        total = int(c.get("default_headcount") or c.get("target_size") or 0)
        for d in _date_range(today, horizon):
            pass_c["examined"] += 1
            try:
                # Idempotency: per-(crew, date) skip if a row exists.
                existing = await db.crew_capacity.find_one(
                    {"crew_id": cid, "date": d.isoformat()},
                    {"_id": 0, "id": 1},
                )
                if existing:
                    pass_c["skipped_already_present"] += 1
                    continue
                holiday = _is_weekend(d)
                doc = {
                    "id": uuid.uuid4().hex,
                    "tenant_id": c["tenant_id"],
                    "crew_id": cid,
                    "trade": trade,
                    "company_name": company,
                    "date": d.isoformat(),
                    "total_headcount": total if not holiday else 0,
                    "committed_headcount": 0,
                    "available_headcount": total if not holiday else 0,
                    "hours_per_person": 8.0,
                    "is_holiday": holiday,
                    "notes": None,
                    "computed_at": now.isoformat(),
                    "computed_from": "migration_backfill",
                }
                await db.crew_capacity.insert_one(doc.copy())
                pass_c["added"] += 1
            except Exception as e:
                pass_c["failures"] += 1
                print(f"  ! crew_capacity {cid}/{d}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS D: crew_demands backfill from tasks where required_trade set
    # =================================================================
    print(f"\n=== Pass D: backfill crew_demands from tasks with required_trade set ===")
    pass_d = _new_stats()
    pass_d["skipped_no_trade"] = 0
    tasks = await db.tasks.find(
        {"required_trade": {"$nin": [None, ""]}},
        {"_id": 0, "id": 1, "tenant_id": 1, "project_id": 1, "activity_code": 1,
         "required_trade": 1, "required_headcount": 1, "required_hours": 1,
         "planned_start": 1, "planned_finish": 1, "duration_days": 1,
         "is_critical_path": 1, "status": 1},
    ).to_list(length=None)
    for t in tasks:
        pass_d["examined"] += 1
        if (t.get("status") or "") == "deleted":
            pass_d["skipped_no_data"] += 1
            continue
        rtrade = t.get("required_trade")
        rcount = int(t.get("required_headcount") or 0)
        rhours = float(t.get("required_hours") or 0)
        if not rtrade or rcount <= 0:
            # Backfilled trade with no headcount yields no demand --
            # task author must complete the demand declaration.
            pass_d["skipped_no_trade"] += 1
            continue
        ps = t.get("planned_start")
        pf = t.get("planned_finish")
        if not ps or not pf:
            pass_d["skipped_no_data"] += 1
            continue
        try:
            ps_d = date.fromisoformat(str(ps)[:10])
            pf_d = date.fromisoformat(str(pf)[:10])
        except Exception:
            pass_d["skipped_no_data"] += 1
            continue
        if pf_d < ps_d:
            pass_d["skipped_no_data"] += 1
            continue
        cp = bool(t.get("is_critical_path"))
        # One demand row per (project, trade, date). Aggregate across
        # tasks contributing on the same day: headcount sums, source
        # task ids accumulate, CP-flag ORs.
        for d in _date_range(ps_d, pf_d):
            try:
                existing = await db.crew_demands.find_one(
                    {"tenant_id": t["tenant_id"], "project_id": t["project_id"],
                     "trade": rtrade, "date": d.isoformat()},
                    {"_id": 0, "id": 1, "source_task_ids": 1,
                     "demanded_headcount": 1, "hours_demanded": 1,
                     "is_critical_path": 1},
                )
                if existing:
                    if t["id"] in (existing.get("source_task_ids") or []):
                        # Already accounted for this task. Idempotent skip.
                        pass_d["skipped_already_present"] += 1
                        continue
                    new_ids = list(existing.get("source_task_ids") or []) + [t["id"]]
                    new_count = int(existing.get("demanded_headcount") or 0) + rcount
                    new_hours = float(existing.get("hours_demanded") or 0) + (
                        rhours / max(1.0, (pf_d - ps_d).days + 1)
                    )
                    await db.crew_demands.update_one(
                        {"id": existing["id"]},
                        {"$set": {
                            "source_task_ids": new_ids,
                            "demanded_headcount": new_count,
                            "hours_demanded": new_hours,
                            "is_critical_path": bool(existing.get("is_critical_path")) or cp,
                            "computed_at": now.isoformat(),
                        }},
                    )
                    pass_d["added"] += 1
                    continue
                doc = {
                    "id": uuid.uuid4().hex,
                    "tenant_id": t["tenant_id"],
                    "project_id": t["project_id"],
                    "date": d.isoformat(),
                    "trade": rtrade,
                    "demanded_headcount": rcount,
                    "source_task_ids": [t["id"]],
                    "hours_demanded": rhours / max(1.0, (pf_d - ps_d).days + 1),
                    "is_critical_path": cp,
                    "computed_at": now.isoformat(),
                }
                await db.crew_demands.insert_one(doc.copy())
                pass_d["added"] += 1
            except Exception as e:
                pass_d["failures"] += 1
                print(f"  ! crew_demands {t.get('id')}/{d}: {type(e).__name__}: {e}")

    # =================================================================
    # PASS E: gap computation smoke-verify (NO PERSISTENCE in Phase 1)
    # =================================================================
    print(f"\n=== Pass E: verify gap computation walks supply + demand cleanly ===")
    pass_e = {"trade_date_tuples": 0, "supply_only": 0, "demand_only": 0,
              "both": 0, "shortage": 0, "surplus": 0, "balanced": 0,
              "cp_shortage": 0, "failures": 0}
    # Build (tenant, trade, date) tuples across both collections.
    tuples: set = set()
    async for r in db.crew_capacity.find(
        {"date": {"$gte": today.isoformat(), "$lte": horizon.isoformat()}},
        {"_id": 0, "tenant_id": 1, "trade": 1, "date": 1},
    ):
        tuples.add((r["tenant_id"], r["trade"], r["date"]))
    async for r in db.crew_demands.find(
        {"date": {"$gte": today.isoformat(), "$lte": horizon.isoformat()}},
        {"_id": 0, "tenant_id": 1, "trade": 1, "date": 1},
    ):
        tuples.add((r["tenant_id"], r["trade"], r["date"]))

    for tenant_id, trade, d in tuples:
        pass_e["trade_date_tuples"] += 1
        try:
            supply_rows = await db.crew_capacity.find(
                {"tenant_id": tenant_id, "trade": trade, "date": d},
                {"_id": 0, "available_headcount": 1},
            ).to_list(length=None)
            demand_rows = await db.crew_demands.find(
                {"tenant_id": tenant_id, "trade": trade, "date": d},
                {"_id": 0, "demanded_headcount": 1, "is_critical_path": 1},
            ).to_list(length=None)
            supply = sum(int(r.get("available_headcount") or 0) for r in supply_rows)
            demand = sum(int(r.get("demanded_headcount") or 0) for r in demand_rows)
            cp = any(bool(r.get("is_critical_path")) for r in demand_rows)
            gap = demand - supply
            if supply and not demand:
                pass_e["supply_only"] += 1
            elif demand and not supply:
                pass_e["demand_only"] += 1
            else:
                pass_e["both"] += 1
            if gap > 0:
                pass_e["shortage"] += 1
                if cp:
                    pass_e["cp_shortage"] += 1
            elif gap < 0:
                pass_e["surplus"] += 1
            else:
                pass_e["balanced"] += 1
        except Exception as e:
            pass_e["failures"] += 1
            print(f"  ! gap-verify {trade}@{d}: {type(e).__name__}: {e}")

    # =================================================================
    # Audit: single RESOURCE_LEVELING_BACKFILL entry summarising the run.
    # Only fires if something actually changed (no spam on idempotent
    # re-runs).
    # =================================================================
    total_writes = (pass_a["added"] + pass_b["added"]
                    + pass_c["added"] + pass_d["added"])
    if total_writes:
        # Single tenant_id is awkward when multi-tenant ran; we emit one
        # audit row per tenant we touched, so the per-tenant chain stays
        # honest. Collect tenant ids from any successful write source.
        affected_tenants: set = set()
        async for r in db.crews.find({}, {"_id": 0, "tenant_id": 1}):
            affected_tenants.add(r["tenant_id"])
        async for r in db.tasks.find({}, {"_id": 0, "tenant_id": 1}):
            affected_tenants.add(r["tenant_id"])
        for tenant_id in affected_tenants:
            try:
                await write_audit(
                    tenant_id=tenant_id, table_name="system",
                    op="RESOURCE_LEVELING_BACKFILL",
                    record_id=f"migration_008_{int(now.timestamp())}",
                    before=None, after={
                        "pass_a_tasks_field_added": pass_a["added"],
                        "pass_b_crews_field_added": pass_b["added"],
                        "pass_c_capacity_rows_created": pass_c["added"],
                        "pass_d_demand_rows_created_or_aggregated": pass_d["added"],
                        "pass_e_trade_date_tuples_walked": pass_e["trade_date_tuples"],
                        "backfill_window_days": BACKFILL_WINDOW_DAYS,
                        "horizon_start": today.isoformat(),
                        "horizon_end": horizon.isoformat(),
                    },
                    actor_id=None,
                    request_id="migration_008",
                )
            except Exception as e:
                print(f"  ! audit {tenant_id}: {type(e).__name__}: {e}")

    # =================================================================
    # Summary
    # =================================================================
    def _row(label: str, s: dict) -> str:
        extras = ""
        if "skipped_no_trade" in s:
            extras = f" no_trade={s['skipped_no_trade']:<5}"
        return (f"  {label:<32} examined={s['examined']:<6} "
                f"added={s['added']:<6} "
                f"already={s['skipped_already_present']:<6} "
                f"no_data={s['skipped_no_data']:<5}{extras} "
                f"failures={s['failures']}")

    print("\n--- SUMMARY ---")
    print(f"Horizon: {today} -> {horizon} ({BACKFILL_WINDOW_DAYS} days)")
    print(_row("[A] tasks demand-field add", pass_a))
    print(_row("[B] crews supply-field add", pass_b))
    print(_row("[C] crew_capacity backfill", pass_c))
    print(_row("[D] crew_demands backfill", pass_d))
    print()
    print("[E] gap computation smoke-verify:")
    print(f"    trade_date_tuples = {pass_e['trade_date_tuples']}")
    print(f"    supply_only       = {pass_e['supply_only']}")
    print(f"    demand_only       = {pass_e['demand_only']}")
    print(f"    both              = {pass_e['both']}")
    print(f"    shortage          = {pass_e['shortage']}  (cp_shortage={pass_e['cp_shortage']})")
    print(f"    surplus           = {pass_e['surplus']}")
    print(f"    balanced          = {pass_e['balanced']}")
    print(f"    failures          = {pass_e['failures']}")

    total_failures = (
        pass_a["failures"] + pass_b["failures"]
        + pass_c["failures"] + pass_d["failures"]
        + pass_e["failures"]
    )
    if total_failures:
        print(f"\nFAILED: {total_failures} record(s) couldn't be migrated.", file=sys.stderr)
        sys.exit(1)
    print("\nMigration 008 complete. Resource-leveling supply/demand foundation seeded.")


if __name__ == "__main__":
    asyncio.run(run())
