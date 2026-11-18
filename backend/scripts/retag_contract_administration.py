"""One-time re-sort for the contract administration category (2026-11-18).

Activities sorted before the "contract_administration" work category existed
sit in a paperwork category (notice to proceed, bonds, insurance, schedule of
values, general conditions...). This runs the normal schedule classifier over
every project's schedule and moves an activity to contract_administration
only when ALL of these hold:
  * the classifier now calls it contract_administration;
  * it is not field work today (field work is never moved, whoever set it).
    A paperwork category a person picked by hand may move: when they picked
    it, contract administration did not exist yet, and it is a narrower
    kind of the same non-field work;
  * it is not unplanned field work (not part of the schedule).
Nothing else changes. Every move is recorded in the change history.

Generic: no project, company or activity name is special-cased.

Usage (dry run lists the moves; --apply writes them):
    python backend/scripts/retag_contract_administration.py [--apply] [--project <id>]
Needs MONGO_URL / DB_NAME and an AI key in the environment.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET = "contract_administration"
MOVABLE_FROM = {None, "", "preconstruction_paperwork", "construction_paperwork", "closeout_paperwork"}


async def main(apply: bool, only_project: str | None) -> None:
    from database import get_db
    from audit import write_audit
    from services.schedule_work_category import _classify_via_llm, drop_shared_root
    db = get_db()
    q = {"status": {"$nin": ["deleted", "cancelled"]}}
    if only_project:
        q["id"] = only_project
    total = 0
    async for p in db.projects.find(q, {"_id": 0, "id": 1, "tenant_id": 1, "name": 1}):
        rows = await db.tasks.find(
            {"tenant_id": p["tenant_id"], "project_id": p["id"], "status": {"$ne": "deleted"},
             "created_in_field": {"$ne": True}},
            {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "duration_days": 1,
             "predecessors": 1, "section_path": 1, "work_category": 1, "work_category_source": 1,
             "sequence": 1, "planned_start": 1},
        ).sort([("sequence", 1), ("planned_start", 1)]).to_list(20000)
        if not rows:
            continue
        try:
            rows_for_ai = drop_shared_root([dict(r) for r in rows])
        except Exception:
            rows_for_ai = rows
        tag_map, model, err = await _classify_via_llm(rows_for_ai, p.get("name") or "", "")
        if not tag_map:
            print(f"[{p.get('name')}] classifier unavailable: {err}")
            continue
        moves = [r for r in rows
                 if tag_map.get(str(r.get("activity_code"))) == TARGET
                 and r.get("work_category") in MOVABLE_FROM]
        print(f"[{p.get('name')}] {len(moves)} to move")
        for r in moves:
            print(f"    {r.get('activity_code')}  {r.get('description')}  ({r.get('work_category')} -> {TARGET})")
            if apply:
                await db.tasks.update_one(
                    {"tenant_id": p["tenant_id"], "id": r["id"]},
                    {"$set": {"work_category": TARGET,
                              "work_category_source": r.get("work_category_source") or "ai",
                              "work_category_needs_review": False}})
                await write_audit(
                    tenant_id=p["tenant_id"], table_name="tasks", op="UPDATE", record_id=r["id"],
                    before={"work_category": r.get("work_category")},
                    after={"work_category": TARGET}, actor_id=None,
                    request_id="retag_contract_administration")
        total += len(moves)
    print(("APPLIED " if apply else "DRY RUN ") + f"{total} activities")


if __name__ == "__main__":
    args = sys.argv[1:]
    proj = args[args.index("--project") + 1] if "--project" in args else None
    asyncio.run(main("--apply" in args, proj))
