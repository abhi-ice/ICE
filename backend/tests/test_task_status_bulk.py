"""Task list status refresh in bulk (2026-11-17).

GET /projects/{id}/tasks used to call compute_and_persist_status once per
activity (~7 database round trips each: about 4.5 s for 235 activities on
the hosted database). It now uses refresh_statuses / open_blockers_for_tasks,
a few queries for the whole list. These tests check, on randomised data
against the local test MongoDB, that the bulk path gives every task exactly
what the per-task path gives: same blocker count, same summary (order and
the 20-per-source caps included), same derived status, other companies'
records ignored, and only changed tasks written.

Run (Git Bash):
  MSYS_NO_PATHCONV=1 wsl env DB_NAME=test_task_bulk bash <rt.sh> \
      /mnt/c/Users/admin/Desktop/ICE/backend tests/test_task_status_bulk.py -q
"""
import asyncio
import os
import random
import uuid
from datetime import date, timedelta

import pytest

if not os.environ.get("MONGO_URL") or not (os.environ.get("DB_NAME") or "").startswith(("test_", "ice_test", "unit_")):
    pytest.skip("needs the local test MongoDB (MONGO_URL + a test_ DB_NAME)", allow_module_level=True)

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

import services.task_status as ts  # noqa: E402

COLLS = ["tasks", "delays", "rfis", "submittals", "work_item_links", "work_items", "projects"]


def _seed(rng: random.Random, tenant: str, other: str, project: str):
    today = date(2026, 11, 17)
    tasks = []
    for i in range(40):
        ps = today + timedelta(days=rng.randint(-60, 30))
        tasks.append({
            "id": f"t-{uuid.uuid4().hex[:8]}", "tenant_id": tenant, "project_id": project,
            "activity_code": f"A{i:03d}", "status": rng.choice(["open", "in_progress", "blocked", "open"]),
            "pct_complete": rng.choice([0, 0, 10, 50, 100]),
            "planned_start": ps.isoformat(), "planned_finish": (ps + timedelta(days=rng.randint(1, 40))).isoformat(),
            "work_category": rng.choice([None, "field_work", "construction_paperwork"]),
            "computed_status": rng.choice([None, "on_track", "behind"]),
            "has_open_blocker": False, "open_blocker_count": 0, "open_blocker_summary": [],
        })
    ids = [t["id"] for t in tasks]
    hot = ids[0]  # gets more than 20 of each source, to check the caps
    delays, rfis, subs, links, wis = [], [], [], [], []
    for n in range(60):
        linked = rng.sample(ids, rng.randint(1, 3)) + ([hot] if n < 25 else [])
        delays.append({"id": f"d{n}", "tenant_id": rng.choice([tenant, tenant, other]),
                       "linked_task_ids": linked, "status": rng.choice(["open", "in_progress", "resolved"]),
                       "severity": rng.choice(["low", "high"]), "description": f"delay {n} " + "x" * rng.randint(0, 100)})
    for n in range(50):
        doc = {"id": f"r{n}", "tenant_id": rng.choice([tenant, tenant, other]),
               "status": rng.choice(["draft", "sent", "in_review", "answered"]),
               "rfi_number": n, "subject": f"subject {n}", "priority": rng.choice([None, "high"])}
        form = rng.choice(["linked", "single", "both"])
        tgt = hot if n < 24 else rng.choice(ids)
        if form in ("linked", "both"):
            doc["linked_task_ids"] = [tgt] + rng.sample(ids, 1)
        if form in ("single", "both"):
            doc["schedule_activity_id"] = tgt
        rfis.append(doc)
    for n in range(40):
        subs.append({"id": f"s{n}", "tenant_id": rng.choice([tenant, tenant, other]),
                     "schedule_activity_id": hot if n < 22 else rng.choice(ids),
                     "status": rng.choice(["submitted", "in_review", "approved"]),
                     "sla_status": rng.choice(["breached", "ok"]), "submittal_number": f"S-{n}",
                     "title": f"title {n}", "urgency_flag": rng.choice([True, False]),
                     "overdue_days": rng.choice([0, 3, None])})
    for n in range(30):
        wis.append({"id": f"w{n}", "tenant_id": rng.choice([tenant, tenant, other]),
                    "status": rng.choice(["open", "closed", "resolved", "in_progress"]),
                    "title": f"work item {n}", "priority": rng.choice([None, "low"])})
    for n in range(80):
        links.append({"entity_type": rng.choice(["task", "task", "rfi"]),
                      "entity_id": hot if n < 25 else rng.choice(ids), "work_item_id": f"w{rng.randint(0, 29)}"})
    return tasks, delays, rfis, subs, links, wis


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_bulk_matches_per_task(seed, monkeypatch):
    async def run():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
        tenant, other, project = f"T-{uuid.uuid4().hex[:6]}", f"O-{uuid.uuid4().hex[:6]}", f"P-{uuid.uuid4().hex[:6]}"
        rng = random.Random(seed)
        tasks, delays, rfis, subs, links, wis = _seed(rng, tenant, other, project)
        await db.projects.insert_one({"id": project, "tenant_id": tenant, "timezone": "America/New_York"})
        for coll, docs in (("tasks", tasks), ("delays", delays), ("rfis", rfis), ("submittals", subs),
                           ("work_item_links", links), ("work_items", wis)):
            await db[coll].insert_many([dict(d) for d in docs])
        audits = []

        async def fake_audit(**kw):
            audits.append(kw["record_id"])
        monkeypatch.setattr("audit.write_audit", fake_audit)
        try:
            ids = [t["id"] for t in tasks]
            bulk = await ts.open_blockers_for_tasks(db, tenant, ids)
            today = await ts.project_today_for(db, tenant, project)
            expected = {}
            for t in tasks:
                c, s = await ts.count_open_blockers(db, tenant, t["id"])
                if bulk[t["id"]] != (c, s):
                    b = bulk[t["id"]][1]
                    raise AssertionError(f"{t['id']} bulk={[x['entity_id'] for x in b]} per={[x['entity_id'] for x in s]}")
                expected[t["id"]] = {
                    "computed_status": ts.derive_status(
                        pct=float(t.get("pct_complete") or 0), planned=ts.planned_pct_at(t, today),
                        has_blocker=c > 0, work_category=t.get("work_category"), task_status=t.get("status")),
                    "has_open_blocker": c > 0, "open_blocker_count": c, "open_blocker_summary": s,
                }
            assert bulk[ids[0]][0] > 20  # the caps were exercised
            docs = [dict(t) for t in tasks]
            await ts.refresh_statuses(db, tenant, docs, actor_id="u", request_id="r")
            changed = {t["id"] for t in tasks if any(t.get(k) != v for k, v in expected[t["id"]].items())}
            assert set(audits) == changed and len(audits) == len(changed)
            for d in docs:
                stored = await db.tasks.find_one({"id": d["id"]}, {"_id": 0})
                for k, v in expected[d["id"]].items():
                    assert d[k] == v
                    assert stored[k] == v
            # A second pass changes nothing.
            audits.clear()
            await ts.refresh_statuses(db, tenant, docs, actor_id="u", request_id="r")
            assert audits == []
        finally:
            for coll in COLLS:
                await db[coll].delete_many({"tenant_id": {"$in": [tenant, other]}})
            await db.work_item_links.delete_many({"work_item_id": {"$in": [w["id"] for w in wis]},
                                                  "entity_id": {"$in": [t["id"] for t in tasks]}})
    asyncio.run(run())


def test_empty_list():
    async def run():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
        assert await ts.open_blockers_for_tasks(db, "T", []) == {}
        await ts.refresh_statuses(db, "T", [])
    asyncio.run(run())
