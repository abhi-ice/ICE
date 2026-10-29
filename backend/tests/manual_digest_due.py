# Iter19: run_due_digests sends only at Friday 7 AM site-local, once per local Friday per project
import asyncio, os, sys, uuid
from datetime import datetime, timezone
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient
from services.schedule_digest import run_due_digests


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    tid = "TEST_tenant_" + uuid.uuid4().hex[:6]
    pid = "TEST_proj_" + uuid.uuid4().hex[:6]
    await db.projects.insert_one({"id": pid, "tenant_id": tid, "name": "TEST digest", "timezone": "America/Los_Angeles", "status": "active"})
    await db.tasks.insert_one({"id": "TEST_t_" + pid, "tenant_id": tid, "project_id": pid, "status": "not_started", "description": "X"})
    ok = True
    # Fri 2026-01-09 15:00 UTC = 07:00 PST
    r1 = [x for x in await run_due_digests(db, datetime(2026, 1, 9, 15, 5, tzinfo=timezone.utc)) if x["project_id"] == pid]
    r2 = [x for x in await run_due_digests(db, datetime(2026, 1, 9, 15, 40, tzinfo=timezone.utc)) if x["project_id"] == pid]
    r3 = [x for x in await run_due_digests(db, datetime(2026, 1, 9, 12, 5, tzinfo=timezone.utc)) if x["project_id"] == pid]  # 4 AM PST
    r4 = [x for x in await run_due_digests(db, datetime(2026, 1, 8, 15, 5, tzinfo=timezone.utc)) if x["project_id"] == pid]  # Thursday
    print("first 7AM run sends:", "PASS" if len(r1) == 1 else "FAIL", r1)
    print("second run same Friday deduped:", "PASS" if not r2 else "FAIL")
    print("4AM local not sent:", "PASS" if not r3 else "FAIL")
    print("Thursday not sent:", "PASS" if not r4 else "FAIL")
    await db.projects.delete_one({"id": pid}); await db.tasks.delete_many({"project_id": pid})
    await db.digest_runs.delete_many({"_id": {"$regex": f"^{pid}:"}})


asyncio.run(main())
