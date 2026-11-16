"""One-time, idempotent data jobs requested by the owner. Each runs once per
database (recorded in db.migrations) and is a no-op where its project is
absent (e.g. preview)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

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


# 2026-11-16: actual start / finish used to be recomputed only when a row was
# saved, so a task touched only by an older (back-dated) report could be left
# without its actual dates. Recompute them once for every task that has
# daily report rows, then run one schedule recompute per project where any
# task's actual dates changed (a second run therefore recomputes nothing).
# Only actual_start / actual_finish / actuals_source are written; percent
# complete is never changed. Imported (Microsoft Project) actuals and actual
# dates a person set by hand (actuals_source "manual") are kept. A row whose
# end-of-day nobody entered (submitted untouched, start-of-day = end-of-day)
# never counts as progress, so it cannot pull an actual start earlier; any
# task whose actual start still moves earlier is logged with both dates.
ACTUALS_JOB_ID = "2026-11-16-recompute-actuals-from-reports"
_ACTUALS_BATCH = 500


async def _recompute_actuals_from_reports(db) -> dict:
    from services.cpm import auto_recompute_after_event
    from services.schedule_feedback import recompute_task_actuals

    counts = {"tasks_with_rows": 0, "tasks_checked": 0, "tasks_changed": 0,
              "tasks_start_moved_earlier": 0,
              "tasks_skipped": 0, "tasks_failed": 0,
              "projects_recomputed": 0, "projects_failed": 0}
    # Distinct task ids straight from the database (no row documents are
    # loaded), so this stays light on a large report history.
    task_ids = [g["_id"] async for g in db.daily_report_activities.aggregate(
        [{"$match": {"schedule_activity_id": {"$nin": [None, ""]}}},
         {"$group": {"_id": "$schedule_activity_id"}}],
        allowDiskUse=True)]
    counts["tasks_with_rows"] = len(task_ids)
    projects: dict = {}  # (tenant_id, project_id) -> None, kept in first-seen order
    for i in range(0, len(task_ids), _ACTUALS_BATCH):
        batch = task_ids[i:i + _ACTUALS_BATCH]
        tasks = await db.tasks.find(
            {"id": {"$in": batch}},
            {"_id": 0, "id": 1, "tenant_id": 1, "project_id": 1,
             "actual_start": 1, "actual_finish": 1},
        ).to_list(None)
        for t in tasks:
            if not t.get("tenant_id"):
                counts["tasks_skipped"] += 1
                continue
            counts["tasks_checked"] += 1
            try:
                upd = await recompute_task_actuals(db, t["tenant_id"], t["id"])
            except Exception as exc:
                counts["tasks_failed"] += 1
                log.warning("actual-date recompute failed for task %s: %s", t["id"], exc)
                continue
            if not upd:  # imported actuals, kept as they are
                counts["tasks_skipped"] += 1
                continue
            if (t.get("actual_start"), t.get("actual_finish")) != (
                    upd.get("actual_start"), upd.get("actual_finish")):
                counts["tasks_changed"] += 1
                old_start, new_start = t.get("actual_start"), upd.get("actual_start")
                if old_start and new_start and str(new_start)[:10] < str(old_start)[:10]:
                    counts["tasks_start_moved_earlier"] += 1
                    log.warning("actual-date recompute: task %s (project %s) actual start "
                                "moved earlier from %s to %s", t["id"], t.get("project_id"),
                                old_start, new_start)
                # Only a project whose actual dates moved needs a recompute.
                if t.get("project_id"):
                    projects[(t["tenant_id"], t["project_id"])] = None
        log.info("actual-date recompute: %d of %d tasks done",
                 min(i + _ACTUALS_BATCH, len(task_ids)), len(task_ids))
    for tenant_id, project_id in projects:
        try:
            await auto_recompute_after_event(tenant_id, project_id,
                                             request_id=f"migration:{ACTUALS_JOB_ID}")
            counts["projects_recomputed"] += 1
        except Exception as exc:
            counts["projects_failed"] += 1
            log.warning("schedule recompute failed for project %s: %s", project_id, exc)
    log.info("actual-date recompute finished: %s", counts)
    return counts


# A claim left at "running" for this long belongs to a process that was
# stopped part-way (a restart or a failed health probe); it may be retaken.
STALE_CLAIM_SECONDS = 2 * 3600


async def _claim(db, job_id: str) -> bool:
    """Claim the job for this process. True when it should run here."""
    now = utc_now()
    try:
        # _id claim: only one pod ever runs the job.
        await db.migrations.insert_one({"_id": job_id, "status": "running",
                                        "started_at": now.isoformat()})
        return True
    except Exception:
        pass
    # Retake a stale "running" claim (the process that held it is gone).
    # The filter on the old started_at makes this a single winner.
    doc = await db.migrations.find_one({"_id": job_id}) or {}
    if doc.get("status") != "running" or not doc.get("started_at"):
        return False
    try:
        started = datetime.fromisoformat(str(doc["started_at"]))
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (now - started).total_seconds() < STALE_CLAIM_SECONDS:
        return False
    res = await db.migrations.update_one(
        {"_id": job_id, "status": "running", "started_at": doc["started_at"]},
        {"$set": {"started_at": now.isoformat(), "retaken_from": doc["started_at"]}},
    )
    if res.modified_count:
        log.warning("one-time job %s: retaking a stale claim from %s", job_id, doc["started_at"])
    return bool(res.modified_count)


async def _run_job(db, job_id: str, job) -> None:
    if not await _claim(db, job_id):
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


async def _settings_loaded(timeout: float = 30.0) -> bool:
    """Load the UI-managed settings (feature flags) before a job that runs
    schedule recomputes: the server starts these jobs before its own first
    settings load, and with the flags not loaded a Microsoft Project (Mode 2)
    or enterprise project would be recomputed by the impact-days engine."""
    from services import runtime_settings
    try:
        await asyncio.wait_for(runtime_settings.refresh_cache(), timeout=timeout)
        return True
    except Exception as exc:
        log.warning("settings not loaded (%s); schedule jobs wait for the next start", exc)
        return False


async def run_one_time_jobs() -> None:
    db = get_db()
    await _run_job(db, SECTION_PATH_JOB_ID, _backfill_section_paths)
    # The actual-date job recomputes schedules: only with the settings
    # loaded (it is not claimed otherwise, so the next start runs it), and
    # with the history indexes in place so each per-task lookup is indexed.
    if not await _settings_loaded():
        return
    from services.schedule_feedback import ensure_history_indexes
    try:
        await ensure_history_indexes(db)
    except Exception as exc:
        log.warning("history indexes not created before the actual-date job: %s", exc)
    await _run_job(db, ACTUALS_JOB_ID, _recompute_actuals_from_reports)
