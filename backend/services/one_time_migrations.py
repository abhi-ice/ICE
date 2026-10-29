"""One-time, idempotent data jobs requested by the owner. Each runs once per
database (recorded in db.migrations) and is a no-op where its project is
absent (e.g. preview)."""
from __future__ import annotations

import logging

from database import get_db, tenant_filter
from models import gen_id, utc_now

log = logging.getLogger(__name__)

# 2026-10-29 owner request: clear every daily report + delay on this project
# (schedule effects reversed) and load AI/heading activity names.
RESET_PROJECT_ID = "__SET_AFTER_EXPORT__"
RESET_JOB_ID = "2026-10-29-reset-dr-and-name-activities"


async def _reset_project(db) -> dict:
    proj = await db.projects.find_one({"id": RESET_PROJECT_ID}, {"_id": 0, "id": 1, "tenant_id": 1})
    if not proj:
        return {"skipped": "project_absent"}
    tenant_id = proj["tenant_id"]
    ctx = {"tenant_id": tenant_id, "user": {"id": "system:owner-request", "role": "admin"},
           "request_id": gen_id()}
    from routers.daily_reports_router import delete_daily_report
    drs = await db.daily_reports.find(tenant_filter(tenant_id, {"project_id": RESET_PROJECT_ID}),
                                      {"_id": 0, "id": 1}).to_list(5000)
    for d in drs:
        await delete_daily_report(daily_report_id=d["id"], ctx=ctx,
                                  reason="Owner request 2026-10-29: clear all reports on this project")
    delays = await db.delays.find(tenant_filter(tenant_id, {"project_id": RESET_PROJECT_ID}),
                                  {"_id": 0}).to_list(5000)
    for d in delays:
        await db.delays.delete_one(tenant_filter(tenant_id, {"id": d["id"]}))
        from audit import write_audit
        await write_audit(tenant_id=tenant_id, table_name="delays", op="DELETE_OWNER_REQUEST",
                          record_id=d["id"], before=d, after=None,
                          actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    from services.schedule_feedback import generate_name_suggestions
    names = await generate_name_suggestions(db, tenant_id, RESET_PROJECT_ID, auto_approve=True)
    from services.cpm import auto_recompute_after_event
    await auto_recompute_after_event(tenant_id, RESET_PROJECT_ID, actor_id=ctx["user"]["id"],
                                     request_id=ctx["request_id"])
    return {"daily_reports_deleted": len(drs), "delays_deleted": len(delays), "names": names}


async def run_one_time_jobs() -> None:
    db = get_db()
    if not await db.projects.find_one({"id": RESET_PROJECT_ID}, {"_id": 0, "id": 1}):
        return
    try:
        # _id claim: only one pod ever runs the job.
        await db.migrations.insert_one({"_id": RESET_JOB_ID, "status": "running",
                                        "started_at": utc_now().isoformat()})
    except Exception:
        return
    try:
        result = await _reset_project(db)
        upd = {"status": "done", "result": result}
    except Exception as exc:
        log.warning("one-time job %s failed: %s", RESET_JOB_ID, exc)
        upd = {"status": "failed", "error": str(exc)[:500]}
    upd["finished_at"] = utc_now().isoformat()
    await db.migrations.update_one({"_id": RESET_JOB_ID}, {"$set": upd})
    log.info("one-time job %s: %s", RESET_JOB_ID, upd)
