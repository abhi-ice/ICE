"""Site-clock date helpers (2026-07-06).

USA construction date capture must follow the JOB SITE's local timezone,
not the server's UTC clock. The app is Railway-hosted, so the server wall
clock is UTC: at ~8 PM Eastern (and earlier further west) the UTC calendar
date has already rolled to *tomorrow*. Any stored/rendered CALENDAR DATE
derived from `date.today()` / `datetime.utcnow().date()` therefore lands a
day ahead for a US user working late in the day — which mis-dates Daily
Reports, delay/RFI/QC/safety records, impact anchors, and the schedule
math that keys off them.

Every project carries an IANA `timezone` (models.py, default
America/New_York). These helpers resolve "today" on that site clock. They
are the shared, canonical version of the pattern already used correctly in
`ms_project_sync._project_today` and `docs_router._project_today_iso`;
those two are left untouched (they have their own tests + monkeypatch
points) — new call sites use this module.

IMPORTANT: this is for CALENDAR DATES only. Audit/event *timestamps*
(created_at, updated_at, reported_at, issued_at, queue createdAt, ...) stay
UTC `datetime` — they are instants, not site-local days, and must not be
shifted.
"""

from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from database import tenant_filter

DEFAULT_SITE_TZ = "America/New_York"


def project_today(tz_name: Optional[str] = None) -> date:
    """Today's DATE on the project's site clock. Missing tz ->
    America/New_York (every project defaults to it); an unparseable IANA
    name falls back to the UTC date. Mirrors
    ms_project_sync._project_today; module-level so tests can freeze it."""
    try:
        return datetime.now(ZoneInfo(tz_name or DEFAULT_SITE_TZ)).date()
    except Exception:
        return datetime.utcnow().date()


def project_today_iso(tz_name: Optional[str] = None) -> str:
    return project_today(tz_name).isoformat()


async def project_tz(db, tenant_id: str, project_id: Optional[str]) -> Optional[str]:
    """The project's IANA timezone, or None if it can't be read (caller
    then gets the America/New_York default). Cheap projection read; any DB
    error is swallowed so a stub/offline db degrades to the default rather
    than raising into a create path."""
    if not project_id:
        return None
    try:
        proj = await db.projects.find_one(
            tenant_filter(tenant_id, {"id": project_id}),
            {"_id": 0, "timezone": 1},
        ) or {}
        return proj.get("timezone")
    except Exception:
        return None


async def project_today_for(db, tenant_id: str, project_id: Optional[str]) -> date:
    """Today's DATE on a project's site clock, resolved by id."""
    return project_today(await project_tz(db, tenant_id, project_id))


async def project_today_iso_for(db, tenant_id: str, project_id: Optional[str]) -> str:
    """Today's ISO date string on a project's site clock, resolved by id."""
    return project_today(await project_tz(db, tenant_id, project_id)).isoformat()
