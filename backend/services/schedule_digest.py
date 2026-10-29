"""Friday schedule digest: pending logic changes, open delays and slipped
activities per project, emailed to the tenant's admins + project managers at
7 AM site time. Triggered hourly by the platform cron; each project sends at
most once per local Friday (db.digest_runs _id claim)."""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from database import tenant_filter
from models import utc_now
from services.schedule_feedback import OPEN_DELAY_STATUSES

log = logging.getLogger(__name__)
DIGEST_ROLES = {"admin", "pm"}


async def build_digest(db, tenant_id: str, project_id: str) -> dict:
    tf = lambda q: tenant_filter(tenant_id, {"project_id": project_id, **q})  # noqa: E731
    logic = await db.logic_proposals.find(tf({"status": "pending"}), {"_id": 0}).sort("created_at", -1).to_list(50)
    names_pending = await db.activity_name_suggestions.count_documents(tf({"status": "pending"}))
    delays = await db.delays.find(tf({"status": {"$in": OPEN_DELAY_STATUSES}}), {"_id": 0}).sort("logged_date", 1).to_list(100)
    slipped = await db.tasks.find(
        tf({"status": {"$nin": ["deleted", "complete"]}, "slip_days": {"$gt": 0}}),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "display_name": 1,
         "planned_finish": 1, "projected_finish_date": 1, "slip_days": 1, "is_currently_critical": 1},
    ).sort("slip_days", -1).to_list(30)
    tids = list({t for d in delays for t in (d.get("linked_task_ids") or [])})
    names = {t["id"]: t.get("display_name") or t.get("description") for t in await db.tasks.find(
        tenant_filter(tenant_id, {"id": {"$in": tids}}), {"_id": 0, "id": 1, "display_name": 1, "description": 1},
    ).to_list(len(tids) or 1)}
    return {
        "logic_changes": [{"activity": p.get("activity_name"), "kind": p.get("kind"),
                           "rationale": p.get("rationale")} for p in logic],
        "names_pending": names_pending,
        "open_delays": [{"activities": [names.get(t) for t in d.get("linked_task_ids") or [] if names.get(t)],
                         "impact_days": max([float(i.get("projected_impact_days") or 0)
                                             for i in d.get("per_task_impacts") or []] or [0]),
                         "since": str(d.get("logged_date") or d.get("date") or "")[:10],
                         "description": d.get("description")} for d in delays],
        "slipped": [{"activity": t.get("display_name") or t.get("description"),
                     "planned_finish": str(t.get("planned_finish") or "")[:10],
                     "projected_finish": str(t.get("projected_finish_date") or "")[:10],
                     "slip_days": t.get("slip_days"), "critical": bool(t.get("is_currently_critical"))}
                    for t in slipped],
    }


def render_digest(project_name: str, d: dict, link: str) -> tuple[str, str, str]:
    e = html.escape
    subject = (f"{project_name}: {len(d['logic_changes'])} logic change(s) to approve, "
               f"{len(d['open_delays'])} open delay(s), {len(d['slipped'])} slipped")
    def section(title, rows, empty):
        body = "".join(f"<li style='margin:4px 0'>{r}</li>" for r in rows) or f"<li style='color:#6b7280'>{empty}</li>"
        return f"<h3 style='margin:20px 0 6px;font-size:15px'>{title}</h3><ul style='padding-left:18px;margin:0'>{body}</ul>"
    logic = [f"<b>{e(x['activity'] or '')}</b> — {'work out of order' if x['kind'] == 'out_of_order' else 'unplanned work'}" for x in d["logic_changes"]]
    delays = [f"<b>{e(', '.join(x['activities']) or 'Delay')}</b> — {x['impact_days']:g} impact day(s), open since {e(x['since'])}" for x in d["open_delays"]]
    slips = [f"<b>{e(x['activity'] or '')}</b> — {x['slip_days']} working day(s) late: {e(x['planned_finish'])} → {e(x['projected_finish'])}{' · <span style=color:#dc2626>critical</span>' if x['critical'] else ''}" for x in d["slipped"]]
    names = (f"<p style='margin-top:16px'>{d['names_pending']} activity name suggestion(s) are waiting too.</p>"
             if d["names_pending"] else "")
    body = (f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#111827;max-width:640px'>"
            f"<h2 style='margin:0 0 4px'>Weekly schedule digest</h2><div style='color:#6b7280'>{e(project_name)}</div>"
            + section("Logic changes waiting for approval", logic, "None")
            + section("Open delays", delays, "None")
            + section("Slipped activities", slips, "Nothing has slipped")
            + names + f"<p style='margin-top:20px'><a href='{e(link)}' style='background:#059669;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none'>Open Schedule review</a></p></div>")
    text = "\n".join([f"Weekly schedule digest — {project_name}", "", "Logic changes:"]
                     + [f"- {x['activity']} ({x['kind']})" for x in d["logic_changes"]]
                     + ["", "Open delays:"] + [f"- {', '.join(x['activities'])}: {x['impact_days']:g} days since {x['since']}" for x in d["open_delays"]]
                     + ["", "Slipped:"] + [f"- {x['activity']}: {x['slip_days']} days late" for x in d["slipped"]]
                     + ["", link])
    return subject, body, text


async def digest_recipients(db, tenant_id: str) -> list:
    users = await db.users.find({"tenant_id": tenant_id}, {"_id": 0, "id": 1, "email": 1, "full_name": 1, "role": 1}).to_list(2000)
    memberships = {m["user_id"]: (m.get("role") or "").lower() for m in await db.tenant_users.find(
        {"tenant_id": tenant_id, "status": {"$ne": "inactive"}}, {"_id": 0, "user_id": 1, "role": 1}).to_list(2000)}
    out = []
    for u in users:
        role = memberships.get(u["id"]) or (u.get("role") or "").lower()
        if role in DIGEST_ROLES and u.get("email"):
            out.append(u)
    return out


async def send_project_digest(db, tenant_id: str, project: dict) -> dict:
    from services.email_provider import get_email_provider
    d = await build_digest(db, tenant_id, project["id"])
    base = (os.environ.get("INVITE_BASE_URL") or "").rstrip("/")
    subject, body, text = render_digest(project.get("name") or "Project", d, f"{base}/schedule-review")
    provider = get_email_provider()
    sent, failed = [], []
    for u in await digest_recipients(db, tenant_id):
        try:
            r = provider.send(to_email=u["email"], to_name=u.get("full_name") or "", subject=subject, html=body, text=text)
            (sent if r.get("sent") else failed).append(u["email"])
        except Exception as exc:
            log.warning("digest send failed to %s: %s", u["email"], exc)
            failed.append(u["email"])
    return {"sent": sent, "failed": failed, "subject": subject}


async def run_due_digests(db, now: datetime | None = None) -> list:
    """Send for every project whose site-local time is Friday 7:xx AM."""
    now = now or datetime.now(timezone.utc)
    done = []
    projects = await db.projects.find({"status": {"$ne": "archived"}},
                                      {"_id": 0, "id": 1, "tenant_id": 1, "name": 1, "timezone": 1}).to_list(5000)
    for p in projects:
        try:
            local = now.astimezone(ZoneInfo(p.get("timezone") or "America/New_York"))
        except Exception:
            local = now.astimezone(ZoneInfo("America/New_York"))
        if local.weekday() != 4 or local.hour != 7:
            continue
        if not await db.tasks.find_one({"project_id": p["id"], "status": {"$ne": "deleted"}}, {"_id": 0, "id": 1}):
            continue
        try:
            await db.digest_runs.insert_one({"_id": f"{p['id']}:{local.date().isoformat()}",
                                             "started_at": utc_now().isoformat()})
        except Exception:
            continue
        res = await send_project_digest(db, p["tenant_id"], p)
        await db.digest_runs.update_one({"_id": f"{p['id']}:{local.date().isoformat()}"},
                                        {"$set": {"result": res, "finished_at": utc_now().isoformat()}})
        done.append({"project_id": p["id"], **res})
    return done
