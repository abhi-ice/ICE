"""Once-a-day forecast refresh (2026-11-16).

The impact-days forecast starts unfinished work no earlier than the data date
(the project's site-clock today), so the stored projected dates, slip days and
remaining float go stale as the calendar moves on. They used to be rewritten
only when an event (a slider save, a delay, a request for information)
triggered a recompute, so a quiet project showed yesterday's forecast.

refresh_due_forecasts() runs the normal schedule recompute once per project
per site-clock day, for every active project our own impact-days engine
drives. Projects driven by Microsoft Project (Mode 2) or the enterprise
engine are skipped: those engines forecast on their own, and a recompute
there would queue a Microsoft Project job for every project each day.
The day a project was last refreshed is stored on the project
(forecast_refreshed_on), so a restart or a second process does not repeat it.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import tenant_filter

log = logging.getLogger(__name__)

INACTIVE_STATUSES = ["complete", "cancelled", "deleted"]


def _uses_impact_days(project: dict) -> bool:
    from services.feature_flags import (
        enterprise_cpm_enabled, schedule_engine_for_project,
        schedule_mode_2_enabled, schedule_mode_for_project,
    )
    if schedule_mode_2_enabled() and schedule_mode_for_project(project) == "mode2":
        return False
    if enterprise_cpm_enabled() and schedule_engine_for_project(project) == "enterprise":
        return False
    return True


async def refresh_due_forecasts(db, tenant_id: Optional[str] = None) -> dict:
    """Recompute every impact-days project whose forecast was not refreshed
    yet on its site-clock today. tenant_id limits the run to one company
    (tests). Returns counts."""
    from services.cpm import auto_recompute_after_event
    from services.site_time import project_today_for

    q: dict = {"status": {"$nin": INACTIVE_STATUSES}}
    if tenant_id:
        q["tenant_id"] = tenant_id
    projects = await db.projects.find(
        q, {"_id": 0, "id": 1, "tenant_id": 1, "schedule_mode": 1,
            "schedule_engine": 1, "forecast_refreshed_on": 1},
    ).to_list(None)
    counts = {"projects": len(projects), "refreshed": 0, "skipped_engine": 0,
              "already_today": 0, "failed": 0}
    for p in projects:
        tid, pid = p.get("tenant_id"), p.get("id")
        if not tid or not pid:
            continue
        if not _uses_impact_days(p):
            counts["skipped_engine"] += 1
            continue
        try:
            today = (await project_today_for(db, tid, pid)).isoformat()
            if p.get("forecast_refreshed_on") == today:
                counts["already_today"] += 1
                continue
            # Claim the day first (only one process refreshes a project).
            res = await db.projects.update_one(
                tenant_filter(tid, {"id": pid, "forecast_refreshed_on": p.get("forecast_refreshed_on")}),
                {"$set": {"forecast_refreshed_on": today}},
            )
            if not res.modified_count:
                counts["already_today"] += 1
                continue
            await auto_recompute_after_event(tid, pid, request_id="daily_forecast_refresh")
            counts["refreshed"] += 1
        except Exception as exc:
            counts["failed"] += 1
            log.warning("daily forecast refresh failed for project %s: %s", pid, exc)
    if counts["refreshed"] or counts["failed"]:
        log.info("daily forecast refresh: %s", counts)
    return counts
