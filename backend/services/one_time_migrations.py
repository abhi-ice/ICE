"""One-time, idempotent data jobs requested by the owner. Each runs once per
database (recorded in db.migrations) and is a no-op where its project is
absent (e.g. preview)."""
from __future__ import annotations

import logging

from database import get_db, tenant_filter
from models import utc_now

log = logging.getLogger(__name__)

# 2026-11-15 (Railway move): the 2026-10-29 owner-request job that cleared
# every daily report and delay on project 6007bdf1 has been removed. It ran
# whenever its record was missing from db.migrations, so on a freshly copied
# database it would have wiped that project's reports again. Its record
# ("2026-10-29-reset-dr-and-name-activities") stays in db.migrations where it
# already ran; nothing reads it any more.


# 2026-10-29: activities imported before headings were saved on tasks carry
# no section_path, so the daily report cannot say "Demolition - Phase 2"
# under a bare "Electrical". Rebuild it from each project's approved
# schedule imports, whose stored rows still hold the heading rows.
SECTION_PATH_JOB_ID = "2026-10-29-backfill-section-paths"


async def _backfill_section_paths(db) -> dict:
    from services.schedule_work_category import attach_section_paths

    projects = await db.projects.find({}, {"_id": 0, "id": 1, "tenant_id": 1}).to_list(10000)
    filled = 0
    for p in projects:
        props = await db.schedule_proposals.find(
            tenant_filter(p["tenant_id"], {"project_id": p["id"], "status": "approved"}),
            {"_id": 0, "parsed_rows": 1, "reviewed_at": 1},
        ).sort("reviewed_at", 1).to_list(500)
        paths: dict = {}
        for prop in props:  # oldest first, so the latest import wins
            rows = prop.get("parsed_rows") or []
            attach_section_paths(rows)
            for r in rows:
                if not r.get("excluded_as_summary") and r.get("section_path") and r.get("activity_code"):
                    paths[r["activity_code"]] = r["section_path"]
        for code, path in paths.items():
            res = await db.tasks.update_many(
                tenant_filter(p["tenant_id"], {"project_id": p["id"], "activity_code": code,
                                               "section_path": {"$in": [None, []]}}),
                {"$set": {"section_path": path}})
            filled += res.modified_count
    return {"tasks_filled": filled}


async def _run_job(db, job_id: str, job) -> None:
    try:
        # _id claim: only one pod ever runs the job.
        await db.migrations.insert_one({"_id": job_id, "status": "running",
                                        "started_at": utc_now().isoformat()})
    except Exception:
        return
    try:
        result = await job(db)
        upd = {"status": "done", "result": result}
    except Exception as exc:
        log.warning("one-time job %s failed: %s", job_id, exc)
        upd = {"status": "failed", "error": str(exc)[:500]}
    upd["finished_at"] = utc_now().isoformat()
    await db.migrations.update_one({"_id": job_id}, {"$set": upd})
    log.info("one-time job %s: %s", job_id, upd)


async def run_one_time_jobs() -> None:
    db = get_db()
    await _run_job(db, SECTION_PATH_JOB_ID, _backfill_section_paths)
