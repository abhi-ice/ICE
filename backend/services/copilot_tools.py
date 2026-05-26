"""Copilot tool-use — gives the AI copilot READ + WRITE capabilities against
the project database.

The user's request is parsed by Claude (sonnet 4.5) into a JSON action plan
with one or more tool calls. Each tool maps to a curated DB operation that
respects tenant isolation + role permissions. Results are summarised back
in plain text — STRICTLY NO MARKDOWN — per user requirement.

Available tools:
  - search(query)                          → list of project entities
  - create_task(...)                       → new schedule activity
  - update_task(activity_code, ...)        → patch
  - mark_task_complete(activity_code, pct) → set pct_complete
  - create_rfi(subject, question, ...)     → new RFI
  - create_daily_report(date)              → idempotent
  - update_daily_report_row(...)           → patch one row
  - create_work_item(title, description)   → new field issue
  - create_kb_entry(title, body, tags)     → KB lesson
  - create_submittal(title, spec_section)  → new submittal
  - link_submittal_to_activity(...)        → patch link
  - approve_approval(approval_id, decision)→ approve/reject (admin only)
  - get_status(entity_type, query)         → read

The tool layer is allow-listed — Claude cannot execute arbitrary code.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, date, timezone, timedelta
from typing import Optional, Any
from emergentintegrations.llm.chat import LlmChat, UserMessage
from database import get_db, tenant_filter
from models import gen_id, utc_now

MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"

# ---- TOOL CATALOGUE shown to the LLM -----------------------------------
TOOL_CATALOGUE = """
You can call these tools. Reply with a JSON object only — no prose, no markdown.

Schema:
  {"actions": [{"tool": "<tool_name>", "args": {...}}, ...], "reply": "<plain text — NO markdown — concise summary for the user>"}

Tools:
  - search                    args: {query: str}
                              Global cross-collection text search (projects, tasks, RFIs, work items, specs).
                              Use when the user gives a free-text query like "the wall vent issue" — fuzzy.

  - get_status                args: {entity_type: 'tasks'|'rfis'|'submittals'|'work_items', filter?: str}
                              Quick top-20 list for a known entity type.

  - get_daily_report          args: {date?: str (any common format: '2026-05-22', '05/22/2026', '22 may 2026',
                              'May 22 2026' all work)}
                              Returns the fully-hydrated DR for that date: summary + activity_grid + presence +
                              delays + rfis + safety_events + qc_issues + deliveries + photos. If no date,
                              returns recent_daily_reports list. PREFER this over `search` when the user asks
                              for a daily report by date.

  - query                     args: {collection: str, filter?: dict, sort?: str, limit?: int, project_id?: str}
                              UNIVERSAL READ. Works against any of these collections:
                                projects, master_docs,
                                tasks, schedule_proposals, schedule_diffs,
                                daily_reports, daily_report_activities, daily_presence,
                                delays, safety_events, qc_issues, deliveries,
                                work_items, rfis, submittals, change_orders, coordination_events,
                                specs, spec_sections, drawings, kb_entries, photos,
                                crews, equipment, people, subcontractors, suppliers,
                                locations, floors, rooms,
                                approvals, audit_log,
                                ai_recovery_suggestions, ai_kb_recommendations.
                              `filter` is a MongoDB-style dict, e.g. {"severity": "high"} or
                              {"created_at": {"$gte": "2026-05-01"}}. `sort` is a field name; prefix
                              with "-" for descending (e.g. "-created_at"). `limit` defaults to 50, max 200.
                              Auto-scopes to active tenant + project. USE THIS when no dedicated tool fits.

  - get_project_context       args: {project_id?: str}
                              One-call snapshot of the project: meta + master_doc + KPI counts
                              (tasks total / critical / slipping / blocked, RFIs open, submittals open,
                              delays/safety/QC/work_items open, DRs filed, photos) + recent DRs + recent RFIs
                              + top blockers. Use when the user asks "what's going on" or "status update".

  - get_schedule_analysis     args: {view: 'critical_path'|'slipping'|'upcoming'|'blockers'|'completed'|'summary',
                              days?: int (for 'upcoming', default 14), project_id?: str}
                              CPM-aware schedule views — saves multiple round-trips when the user asks
                              "what's slipping", "what's blocking", "what's coming this week", "show me
                              the critical path".
  - create_task               args: {description: str, planned_start?: 'YYYY-MM-DD', planned_finish?: 'YYYY-MM-DD', duration_days?: int, trade_code?: str}
  - update_task               args: {activity_code: str, pct_complete?: int (0..100), planned_start?: 'YYYY-MM-DD', planned_finish?: 'YYYY-MM-DD', status?: 'in_progress'|'complete'|'blocked'}
  - mark_task_complete        args: {activity_code: str}
  - create_rfi                args: {subject: str, question: str, trade_code?: str}
  - create_daily_report       args: {date?: 'YYYY-MM-DD'}
  - create_work_item          args: {title: str, description: str, priority?: 'low'|'normal'|'high'|'critical', category?: str}
  - create_kb_entry           args: {title: str, body: str, tags?: [str]}
  - create_submittal          args: {title: str, spec_section_code?: str, type?: 'product_data'|'shop_drawing'|'sample'|'calculation'|'test_report'|'warranty'|'om_manual'|'mock_up'}
  - link_submittal_to_activity args: {submittal_number: str, activity_code: str}
  - create_crew               args: {name: str, trade: str, target_size?: int, is_self_perform?: bool, external_company?: str}
  - create_person             args: {first_name: str, last_name: str, email?: str, phone?: str, trade?: str, role?: str, hourly_rate?: number, employment_type?: 'ft'|'pt'|'contractor', union_local?: str}
  - create_equipment          args: {asset_tag: str, type: str, manufacturer?: str, model?: str, year?: number, ownership?: 'owned'|'rental'|'leased', hourly_rate?: number, daily_rate?: number}
  - create_floor              args: {floor_number: int, floor_name: str, elevation_z_ft?: number, floor_height_ft?: number}
  - create_location           args: {name: str, floor_id: str, code?: str, location_type?: 'room'|'zone'|'corridor'|'stair'|'mech_room', area_sqft?: number}
  - create_delay              args: {description: str, severity?: 'low'|'moderate'|'high', delay_days?: number, reason_category?: 'weather'|'rfi_pending'|'submittal_pending'|'design_change'|'site_condition'|'manpower'|'other', date?: 'YYYY-MM-DD'}
  - create_safety_event       args: {description: str, event_type?: 'near_miss'|'first_aid'|'recordable'|'lost_time'|'fatality'|'other', severity?: 'minor'|'moderate'|'major'|'critical'}
  - create_qc_issue           args: {description: str, severity?: 'minor'|'moderate'|'major'|'critical'}
  - create_delivery           args: {vendor: str, description: str, delivery_date?: 'YYYY-MM-DD', po_number?: str}
  - delete_entity             args: {entity_type: 'task'|'rfi'|'submittal'|'work_item'|'kb_entry'|'crew'|'person'|'equipment'|'floor'|'location'|'delay'|'safety_event'|'qc_issue'|'delivery'|'change_order'|'photo', id?: str, activity_code?: str, rfi_number?: str, submittal_number?: str}
  - update_entity             args: {entity_type: <same as delete_entity>, id: str, fields: {<any patchable fields>}}
  - record_presence           args: {trade: str, headcount: int, hours_per_person?: number, sub_id?: str, company_name?: str, notes?: str}  (REQUIRES active daily_report context)
  - mark_activity_status      args: {activity_code: str, pct_complete_end_of_day?: number, hours_worked?: number, delay_minutes?: int, delay_reason?: str, status_at_end_of_day?: 'not_started'|'in_progress'|'blocked'|'complete', crew_count?: int, notes?: str}  (REQUIRES active daily_report context)
  - create_unplanned_activity args: {description: str, trade_code?: str, location_id?: str, crew_id?: str, duration_days?: number}  (REQUIRES active daily_report context — task gets requires_pm_review=true)

Rules:
  - Only call tools the user explicitly authorises (e.g. user says "add task X" → create_task; user says "What's the status?" → get_status only, no writes).
  - If the user request is ambiguous, set actions=[] and ask for clarification in 'reply'.
  - 'reply' is shown verbatim to the user. ABSOLUTELY NO MARKDOWN. No asterisks, no underscores, no fenced code blocks, no headers, no bullet markers like '-' or '*'. Plain sentences only. If you need to enumerate, use commas or numbered like '1.' '2.' (no bullet points).
  - Cite activity codes in square brackets like [A1240]. Cite RFI numbers like RFI-014. Cite spec sections like 03 30 00.
""".strip()

WRITE_TOOLS = {
    "create_task", "update_task", "mark_task_complete", "create_rfi",
    "create_daily_report", "create_work_item", "create_kb_entry",
    "create_submittal", "link_submittal_to_activity",
    "create_crew", "create_person", "create_equipment",
    "create_floor", "create_location", "create_delay",
    "create_safety_event", "create_qc_issue", "create_delivery",
    "delete_entity", "update_entity",
    "record_presence", "mark_activity_status", "create_unplanned_activity",
}


def _strip_markdown(s: str) -> str:
    """Hard-strip every form of Markdown from the model's reply. Strict
    user requirement — never emit markdown."""
    if not s:
        return s
    # Code fences
    s = re.sub(r"```[a-zA-Z0-9]*\n?", "", s)
    s = s.replace("```", "")
    # Inline code
    s = re.sub(r"`([^`]+)`", r"\1", s)
    # Headers (#, ##, ###)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.MULTILINE)
    # Bold/italic markers — keep the inner text
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", s)
    # Blockquote
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.MULTILINE)
    # Horizontal rules
    s = re.sub(r"^\s*[-_*]{3,}\s*$", "", s, flags=re.MULTILINE)
    # Bullet markers ('- ', '* ', '+ ')
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)
    # Tables — collapse to plain tab-separated
    s = re.sub(r"\|", " ", s)
    s = re.sub(r"^\s*-{3,}\s*$", "", s, flags=re.MULTILINE)
    # Markdown links [text](url) → text (url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)
    # Reduce 3+ blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---- TOOL IMPLEMENTATIONS ----------------------------------------------
async def _tool_search(ctx: dict, args: dict) -> dict:
    # The iter5 router exports this as `global_search`, not `search` —
    # the prior `from routers.iter5_router import search as _search`
    # blew up with ImportError, surfaced to the user in Mason as
    # "cannot import name 'search'". Also fixed the call signature:
    # global_search(q: str, limit: int = 10, ctx: dict). pt.27 fix.
    from routers.iter5_router import global_search as _search  # type: ignore
    res = await _search(q=args.get("query", ""), ctx=ctx)  # type: ignore
    return {"results": (res.get("results") or [])[:10]}


def _parse_date_loose(s: str) -> Optional[str]:
    """Try hard to interpret a date string the user typed. Returns ISO
    YYYY-MM-DD or None. Supports US slash, ISO, dotted, dashed, and
    common natural-language formats: "22 may 2026", "May 22, 2026",
    "may 22 26", "2026-05-22", "05/22/2026", "22/05/2026", etc.
    pt.28: needed because the user can ask Mason for a daily report
    by date in any spelling and expects it to just work."""
    if not s:
        return None
    s = str(s).strip().strip('"').strip("'").replace(",", "")
    # Direct formats — try strict parses first
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y",
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%B %d %Y", "%b %d %Y",
        "%d %B %Y", "%d %b %Y",
        "%m/%d/%y", "%d/%m/%y",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Case-insensitive month names
    sl = s.lower()
    for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(sl, fmt.lower()).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: dateutil if available
    try:
        from dateutil import parser as _dp  # type: ignore
        dt = _dp.parse(s, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


async def _tool_get_daily_report(ctx: dict, args: dict) -> dict:
    """Find and fully hydrate a Daily Report by date (or list recent if
    no date given). Smart date parsing accepts every common format. The
    returned payload includes every sub-collection so the LLM can answer
    "list everything in the DR for X" in one tool call — no chasing.
    pt.28 — new tool to fix Mason's "I searched for May 22 but couldn't
    find it" failures when the user typed the date in any non-ISO form."""
    db = get_db()
    project_id = ctx.get("project_id")
    tenant_id = ctx["tenant_id"]
    raw_date = args.get("date") or args.get("report_date")
    if raw_date:
        iso = _parse_date_loose(raw_date)
        if not iso:
            return {"error": f"Could not parse '{raw_date}' as a date — try YYYY-MM-DD."}
        q = {"report_date": iso}
        if project_id:
            q["project_id"] = project_id
        dr = await db.daily_reports.find_one(tenant_filter(tenant_id, q), {"_id": 0})
        if not dr:
            # Fall back: look up by date only across all projects in tenant
            dr = await db.daily_reports.find_one(
                tenant_filter(tenant_id, {"report_date": iso}), {"_id": 0})
        if not dr:
            return {
                "error": f"no daily report found for {iso}",
                "hint": "the report may not exist yet, or may be on a different project",
                "parsed_date": iso,
                "original_input": raw_date,
            }
        return await _hydrate_dr(db, tenant_id, dr)
    # No date — return 10 most recent
    q = {"project_id": project_id} if project_id else {}
    recent = await db.daily_reports.find(
        tenant_filter(tenant_id, q),
        {"_id": 0, "id": 1, "report_date": 1, "status": 1, "project_id": 1},
    ).sort("report_date", -1).limit(10).to_list(10)
    return {"recent_daily_reports": recent}


async def _tool_query(ctx: dict, args: dict) -> dict:
    """Universal read tool — Mason can query ANY collection with optional
    filter, sort, limit. Allow-listed for security. Auto-scopes to the
    active tenant + project. pt.28: lets Mason answer questions like
    "show me all delays severity high last 14 days" or "which RFIs
    haven't been answered" without needing a dedicated tool per query."""
    ALLOWED = {
        # Project + master config
        "projects", "master_docs",
        # Schedule
        "tasks", "schedule_proposals", "schedule_diffs",
        # Daily report family
        "daily_reports", "daily_report_activities", "daily_presence",
        # Events
        "delays", "safety_events", "qc_issues", "deliveries",
        "work_items", "rfis", "submittals", "change_orders",
        "coordination_events",
        # Documents
        "specs", "spec_sections", "drawings", "kb_entries", "photos",
        # Resources
        "crews", "equipment", "people", "subcontractors", "suppliers",
        "locations", "floors", "rooms",
        # Governance
        "approvals", "audit_log", "copilot_messages",
        # Aging / metrics views (collections proper)
        "ai_recovery_suggestions", "ai_recovery_applied",
        "ai_kb_recommendations",
    }
    collection = args.get("collection") or args.get("entity_type")
    if not collection:
        return {"error": "collection arg is required", "allowed_collections": sorted(ALLOWED)}
    if collection not in ALLOWED:
        # Common alias fixups so the LLM doesn't have to guess
        aliases = {
            "kb": "kb_entries", "knowledge": "kb_entries", "lessons": "kb_entries",
            "field_issues": "work_items", "issues": "work_items",
            "presence": "daily_presence", "manpower": "daily_presence",
            "events": "delays",  # closest single collection
            "subs": "subcontractors",
            "vendors": "subcontractors",
        }
        if collection in aliases:
            collection = aliases[collection]
        else:
            return {"error": f"unknown collection '{collection}'", "allowed_collections": sorted(ALLOWED)}
    db = get_db()
    tenant_id = ctx["tenant_id"]
    project_id = args.get("project_id") or ctx.get("project_id")
    # Build query: always tenant-scoped, optionally project-scoped (for
    # collections that have a project_id). Tenant-wide collections like
    # subcontractors/suppliers/kb_entries/audit_log are NOT project-scoped.
    NOT_PROJECT_SCOPED = {"subcontractors", "suppliers", "kb_entries",
                          "audit_log", "copilot_messages", "people",
                          "equipment", "ai_kb_recommendations"}
    q = dict(tenant_filter(tenant_id))
    if project_id and collection not in NOT_PROJECT_SCOPED:
        q["project_id"] = project_id
    # Merge user-supplied filter dict (LLM produced a Mongo-like filter).
    # We do shallow validation — strip dangerous operators we don't want.
    user_filter = args.get("filter") or {}
    if not isinstance(user_filter, dict):
        return {"error": "filter must be a dict"}
    BANNED = {"$where", "$expr", "$function", "$accumulator"}
    def _scrub(node):
        if isinstance(node, dict):
            return {k: _scrub(v) for k, v in node.items() if k not in BANNED}
        if isinstance(node, list):
            return [_scrub(v) for v in node]
        return node
    safe_filter = _scrub(user_filter)
    q.update(safe_filter)
    # Sort: support "field" (asc) or "-field" (desc)
    sort_arg = args.get("sort")
    cursor = db[collection].find(q, {"_id": 0})
    if sort_arg:
        direction = -1 if str(sort_arg).startswith("-") else 1
        field = str(sort_arg).lstrip("-")
        cursor = cursor.sort(field, direction)
    limit = min(int(args.get("limit") or 50), 200)
    try:
        rows = await cursor.limit(limit).to_list(limit)
    except Exception as e:
        return {"error": f"query failed: {e}", "filter_used": safe_filter}
    return {"collection": collection, "count": len(rows), "rows": rows}


async def _tool_get_project_context(ctx: dict, args: dict) -> dict:
    """One-call full snapshot of the active (or specified) project — meta,
    master doc, KPI counts, recent DRs, recent RFIs, top blockers. Useful
    when the user asks "what's going on with this project" or "give me a
    status update". Mason can call this first then drill into specifics."""
    db = get_db()
    tenant_id = ctx["tenant_id"]
    project_id = args.get("project_id") or ctx.get("project_id")
    if not project_id:
        # No active project — list projects to choose from
        projects = await db.projects.find(
            tenant_filter(tenant_id), {"_id": 0, "id": 1, "name": 1, "status": 1},
        ).sort("name", 1).limit(20).to_list(20)
        return {"error": "no active project", "available_projects": projects}
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}), {"_id": 0})
    if not proj:
        return {"error": f"project {project_id} not found"}
    master_doc = await db.master_docs.find_one(
        tenant_filter(tenant_id, {"project_id": project_id}), {"_id": 0})
    base = tenant_filter(tenant_id, {"project_id": project_id})
    async def _count(coll, extra=None):
        q = dict(base)
        if extra: q.update(extra)
        try: return await db[coll].count_documents(q)
        except Exception: return 0
    counts = {
        "tasks_total": await _count("tasks", {"status": {"$ne": "deleted"}}),
        "tasks_critical": await _count("tasks", {"is_critical": True, "status": {"$ne": "deleted"}}),
        "tasks_slipping": await _count("tasks", {"slip_days": {"$gt": 0}}),
        "tasks_blocked": await _count("tasks", {"open_blocker_count": {"$gt": 0}}),
        "rfis_open": await _count("rfis", {"status": {"$nin": ["answered", "closed"]}}),
        "submittals_open": await _count("submittals", {"status": {"$nin": ["approved", "closed"]}}),
        "delays_open": await _count("delays", {"status": {"$ne": "resolved"}}),
        "safety_events_open": await _count("safety_events", {"status": {"$ne": "closed"}}),
        "qc_issues_open": await _count("qc_issues", {"status": {"$ne": "closed"}}),
        "work_items_open": await _count("work_items", {"status": {"$ne": "closed"}}),
        "daily_reports_filed": await _count("daily_reports"),
        "photos_total": await _count("photos"),
    }
    recent_drs = await db.daily_reports.find(
        base, {"_id": 0, "id": 1, "report_date": 1, "status": 1, "ppc": 1},
    ).sort("report_date", -1).limit(5).to_list(5)
    recent_rfis = await db.rfis.find(
        base, {"_id": 0, "rfi_number": 1, "subject": 1, "status": 1, "date_initiated": 1},
    ).sort("date_initiated", -1).limit(5).to_list(5)
    top_blockers = await db.tasks.find(
        {**base, "open_blocker_count": {"$gt": 0}},
        {"_id": 0, "activity_code": 1, "description": 1, "open_blocker_count": 1, "open_blocker_summary": 1},
    ).sort("open_blocker_count", -1).limit(5).to_list(5)
    return {
        "project": proj,
        "master_doc": master_doc,
        "counts": counts,
        "recent_daily_reports": recent_drs,
        "recent_rfis": recent_rfis,
        "top_blockers": top_blockers,
    }


async def _tool_get_schedule_analysis(ctx: dict, args: dict) -> dict:
    """CPM-aware schedule views: critical_path / slipping / upcoming /
    blockers / summary. Mason uses this when the user asks "what's the
    critical path", "what's slipping", "what's blocked", "what's coming
    up this week" — saves N round-trips to the generic query tool."""
    db = get_db()
    project_id = args.get("project_id") or ctx.get("project_id")
    if not project_id:
        return {"error": "no active project"}
    view = (args.get("view") or "summary").lower()
    base = tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}})
    proj_fields = {"_id": 0, "activity_code": 1, "description": 1,
                   "planned_start": 1, "planned_finish": 1, "pct_complete": 1,
                   "status": 1, "is_critical": 1, "slip_days": 1,
                   "open_blocker_count": 1, "open_blocker_summary": 1,
                   "trade": 1, "duration_days": 1}
    if view == "critical_path":
        rows = await db.tasks.find({**base, "is_critical": True}, proj_fields).sort("planned_start", 1).limit(80).to_list(80)
    elif view == "slipping":
        rows = await db.tasks.find({**base, "slip_days": {"$gt": 0}}, proj_fields).sort("slip_days", -1).limit(30).to_list(30)
    elif view == "upcoming":
        days = int(args.get("days") or 14)
        until = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
        rows = await db.tasks.find(
            {**base, "planned_start": {"$lte": until}, "status": {"$in": ["not_started", "in_progress"]}},
            proj_fields,
        ).sort("planned_start", 1).limit(50).to_list(50)
    elif view == "blockers":
        rows = await db.tasks.find({**base, "open_blocker_count": {"$gt": 0}}, proj_fields).sort("open_blocker_count", -1).limit(30).to_list(30)
    elif view == "completed":
        rows = await db.tasks.find({**base, "status": "complete"}, proj_fields).sort("planned_finish", -1).limit(50).to_list(50)
    else:
        # summary — counts + 3 top-of-mind activities for each pivot
        async def _cnt(extra):
            try: return await db.tasks.count_documents({**base, **extra})
            except Exception: return 0
        return {
            "view": "summary",
            "counts": {
                "total": await _cnt({}),
                "critical_path": await _cnt({"is_critical": True}),
                "slipping": await _cnt({"slip_days": {"$gt": 0}}),
                "blocked": await _cnt({"open_blocker_count": {"$gt": 0}}),
                "in_progress": await _cnt({"status": "in_progress"}),
                "complete": await _cnt({"status": "complete"}),
                "not_started": await _cnt({"status": "not_started"}),
            },
            "top_slipping": await db.tasks.find({**base, "slip_days": {"$gt": 0}}, proj_fields).sort("slip_days", -1).limit(3).to_list(3),
            "top_blocked": await db.tasks.find({**base, "open_blocker_count": {"$gt": 0}}, proj_fields).sort("open_blocker_count", -1).limit(3).to_list(3),
        }
    return {"view": view, "count": len(rows), "rows": rows}


async def _hydrate_dr(db, tenant_id: str, dr: dict) -> dict:
    """Pull every sub-collection tied to a DR so the LLM gets one
    complete payload instead of having to call 6 more tools."""
    drid = dr.get("id")
    if not drid:
        return {"report": dr}
    # Activity grid (sorted by position so AI lists in scheduled order)
    grid = await db.daily_report_activities.find(
        {"daily_report_id": drid}, {"_id": 0}
    ).sort("position", 1).to_list(100)
    presence = await db.daily_presence.find(
        tenant_filter(tenant_id, {"daily_report_id": drid}), {"_id": 0}
    ).to_list(50)
    # Events filed against this DR (delays, RFIs, safety, QC, deliveries)
    delays = await db.delays.find(
        tenant_filter(tenant_id, {"daily_report_id": drid}), {"_id": 0}
    ).to_list(30)
    rfis = await db.rfis.find(
        tenant_filter(tenant_id, {"daily_report_id": drid}), {"_id": 0}
    ).to_list(30)
    safety = await db.safety_events.find(
        tenant_filter(tenant_id, {"daily_report_id": drid}), {"_id": 0}
    ).to_list(30)
    qc = await db.qc_issues.find(
        tenant_filter(tenant_id, {"daily_report_id": drid}), {"_id": 0}
    ).to_list(30)
    deliveries = await db.deliveries.find(
        tenant_filter(tenant_id, {"daily_report_id": drid}), {"_id": 0}
    ).to_list(30)
    photos = await db.photos.find(
        tenant_filter(tenant_id, {"daily_report_id": drid}),
        {"_id": 0, "id": 1, "caption": 1, "captured_at": 1, "photo_class": 1}
    ).to_list(30)
    summary = {
        "date": dr.get("report_date"),
        "status": dr.get("status"),
        "weather": dr.get("weather_conditions"),
        "temp_high_f": dr.get("weather_temp_high_f"),
        "temp_low_f": dr.get("weather_temp_low_f"),
        "precip_in": dr.get("weather_precipitation_in"),
        "activities_total": len(grid),
        "presence_rows": len(presence),
        "total_headcount": sum(int(p.get("headcount") or 0) for p in presence),
        "total_crew_hours": sum(int(p.get("headcount") or 0) * float(p.get("hours_per_person") or 0) for p in presence),
        "delays": len(delays),
        "rfis": len(rfis),
        "safety_events": len(safety),
        "qc_issues": len(qc),
        "deliveries": len(deliveries),
        "photos": len(photos),
    }
    return {
        "summary": summary,
        "report": dr,
        "activity_grid": grid,
        "presence": presence,
        "delays": delays,
        "rfis": rfis,
        "safety_events": safety,
        "qc_issues": qc,
        "deliveries": deliveries,
        "photos": photos,
    }


async def _tool_get_status(ctx: dict, args: dict) -> dict:
    db = get_db()
    et = args.get("entity_type", "tasks")
    f = tenant_filter(ctx["tenant_id"], {"project_id": ctx.get("project_id")} if ctx.get("project_id") else {})
    if et == "tasks":
        docs = await db.tasks.find(f, {"_id": 0, "activity_code": 1, "description": 1,
                                        "pct_complete": 1, "status": 1, "planned_finish": 1}
                                    ).limit(20).to_list(20)
    elif et == "rfis":
        docs = await db.rfis.find(f, {"_id": 0, "rfi_number": 1, "subject": 1, "status": 1, "date_initiated": 1}).limit(20).to_list(20)
    elif et == "submittals":
        docs = await db.submittals.find(f, {"_id": 0, "submittal_number": 1, "title": 1, "status": 1}).limit(20).to_list(20)
    elif et == "work_items":
        docs = await db.work_items.find(f, {"_id": 0, "title": 1, "priority": 1, "status": 1}).limit(20).to_list(20)
    else:
        return {"error": f"unknown entity_type: {et}"}
    return {"entity_type": et, "rows": docs}


async def _tool_create_task(ctx: dict, args: dict) -> dict:
    if "description" not in args:
        return {"error": "description required"}
    db = get_db()
    project_id = ctx.get("project_id")
    if not project_id:
        return {"error": "no active project — set project_id"}

    # Auto-assign next activity_code
    last = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}),
        {"_id": 0, "activity_code": 1},
    ).sort("activity_code", -1).limit(1).to_list(1)
    next_n = 1
    for r in last:
        m = re.search(r"(\d+)$", r.get("activity_code", "") or "")
        if m:
            next_n = int(m.group(1)) + 1
    activity_code = f"A{next_n:04d}"
    today = date.today()
    ps = args.get("planned_start") or today.isoformat()
    dur = int(args.get("duration_days") or 5)
    pf = args.get("planned_finish") or (date.fromisoformat(ps) + timedelta(days=dur)).isoformat()

    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "activity_code": activity_code,
        "description": args["description"],
        "wbs": args.get("wbs"),
        "planned_start": ps,
        "planned_finish": pf,
        "duration_days": dur,
        "status": "not_started",
        "pct_complete": 0,
        "trade_code": args.get("trade_code"),
        "predecessors": [],
        "successors": [],
        "is_milestone": False,
        "is_critical_path": False,
        "lookahead_status": "in_window",
        "spec_section_codes": [],
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.tasks.insert_one(doc.copy())
    return {"created": {"resource": "task", "id": doc["id"], "activity_code": activity_code,
                        "description": args["description"], "planned_start": ps,
                        "planned_finish": pf}}


async def _tool_update_task(ctx: dict, args: dict) -> dict:
    code = args.get("activity_code")
    if not code:
        return {"error": "activity_code required"}
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"activity_code": code,
                                         "project_id": ctx.get("project_id")})
    task = await db.tasks.find_one(f, {"_id": 0})
    if not task:
        return {"error": f"task {code} not found"}
    updates: dict = {}
    for k in ("pct_complete", "planned_start", "planned_finish", "status",
              "duration_days", "trade_code"):
        if k in args:
            updates[k] = args[k]
    if "pct_complete" in updates and updates["pct_complete"] >= 100:
        updates["status"] = "complete"
    if not updates:
        return {"error": "no fields to update"}
    await db.tasks.update_one(f, {"$set": updates})
    return {"updated": {"resource": "task", "activity_code": code, "id": task["id"], "fields": updates}}


async def _tool_mark_task_complete(ctx: dict, args: dict) -> dict:
    return await _tool_update_task(ctx, {"activity_code": args.get("activity_code"),
                                          "pct_complete": 100, "status": "complete"})


async def _tool_create_rfi(ctx: dict, args: dict) -> dict:
    if "subject" not in args or "question" not in args:
        return {"error": "subject and question required"}
    db = get_db()
    project_id = ctx.get("project_id")
    last = await db.rfis.find(tenant_filter(ctx["tenant_id"], {"project_id": project_id}),
                              {"_id": 0, "rfi_number": 1}).sort("rfi_number", -1).limit(1).to_list(1)
    n = 1
    if last:
        m = re.search(r"(\d+)$", last[0].get("rfi_number", "") or "")
        if m: n = int(m.group(1)) + 1
    rfi = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "rfi_number": f"RFI-{n:03d}",
        "subject": args["subject"],
        "description": args["question"],  # canonical field name on Rfi model
        "trade_code": args.get("trade_code"),
        "spec_section_refs": [],
        "schedule_activity_id": None,
        "location_id": None,
        "status": "draft",
        "priority": "normal",
        "date_initiated": date.today().isoformat(),
        "date_required": None,
        "date_responded": None,
        "response_text": None,
        "cost_impact_estimated_usd": None,
        "time_impact_estimated_days": None,
        "to_party": None, "from_party": None,
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    }
    await db.rfis.insert_one(rfi.copy())
    return {"created": {"resource": "rfi", "id": rfi["id"], "rfi_number": rfi["rfi_number"], "subject": args["subject"]}}


async def _tool_create_daily_report(ctx: dict, args: dict) -> dict:
    db = get_db()
    project_id = ctx.get("project_id")
    rd = args.get("date") or date.today().isoformat()
    existing = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "report_date": rd}),
        {"_id": 0},
    )
    if existing:
        return {"created": {"resource": "daily_report", "id": existing["id"], "report_date": rd, "status": existing["status"], "already_existed": True}}
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "report_date": rd,
        "status": "draft",
        "weather_conditions": None,
        "weather_temp_high_f": None, "weather_temp_low_f": None,
        "weather_precipitation_in": 0, "weather_impact_minutes": 0,
        "notes": None,
        "submitted_at": None, "submitted_by": None,
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.daily_reports.insert_one(doc.copy())
    return {"created": {"resource": "daily_report", "id": doc["id"], "report_date": rd}}


async def _tool_create_work_item(ctx: dict, args: dict) -> dict:
    if "title" not in args or "description" not in args:
        return {"error": "title + description required"}
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": ctx.get("project_id"),
        "title": args["title"],
        "description": args["description"],
        "priority": args.get("priority", "normal"),
        "category": args.get("category", "general"),
        "status": "open",
        "opened_by": ctx["user"]["id"],
        "opened_at": utc_now().isoformat(),
        "owned_by": ctx["user"]["id"],
        "actual_cost_impact": None, "actual_time_impact_days": None,
        "closed_at": None, "closure_summary": None, "root_cause": None,
        "created_at": utc_now().isoformat(),
    }
    await db.work_items.insert_one(doc.copy())
    return {"created": {"resource": "work_item", "id": doc["id"], "title": args["title"], "priority": args.get("priority", "normal")}}


async def _tool_create_kb_entry(ctx: dict, args: dict) -> dict:
    if "title" not in args or "body" not in args:
        return {"error": "title + body required"}
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": ctx.get("project_id"),
        "title": args["title"],
        "body": args["body"],
        "tags": args.get("tags") or [],
        "source_kind": "lesson",
        "source_id": None,
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
        "embedding_version": 1,
    }
    await db.kb_entries.insert_one(doc.copy())
    try:
        from services.kb_search import invalidate_index
        invalidate_index(ctx["tenant_id"])
    except Exception:
        pass
    return {"created": {"resource": "kb_entry", "id": doc["id"], "title": args["title"]}}


async def _tool_create_submittal(ctx: dict, args: dict) -> dict:
    if "title" not in args:
        return {"error": "title required"}
    db = get_db()
    project_id = ctx.get("project_id")
    last = await db.submittals.find(tenant_filter(ctx["tenant_id"], {"project_id": project_id}),
                                    {"_id": 0, "submittal_number": 1}).sort("submittal_number", -1).limit(1).to_list(1)
    n = 1
    if last:
        m = re.search(r"(\d+)$", last[0].get("submittal_number", "") or "")
        if m: n = int(m.group(1)) + 1
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "submittal_number": f"SUB-{n:03d}",
        "title": args["title"],
        "spec_section_code": args.get("spec_section_code"),
        "spec_section_id": None,
        "type": args.get("type", "product_data"),
        "status": "not_yet_submitted",
        "sub_responsible": args.get("sub_responsible"),
        "schedule_activity_id": None,
        "submitted_at": None, "approved_at": None,
        "auto_generated": False,
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.submittals.insert_one(doc.copy())
    return {"created": {"resource": "submittal", "submittal_number": doc["submittal_number"], "title": args["title"], "id": doc["id"]}}


# ---- Resource CRUD tools ------------------------------------------------
async def _tool_create_crew(ctx: dict, args: dict) -> dict:
    if "name" not in args or "trade" not in args:
        return {"error": "name + trade required"}
    db = get_db()
    # Auto-generate crew_code if missing
    crew_code = args.get("crew_code")
    if not crew_code:
        last = await db.crews.find(tenant_filter(ctx["tenant_id"]),
                                    {"_id": 0, "crew_code": 1}).sort("crew_code", -1).limit(1).to_list(1)
        n = 1
        if last:
            m = re.search(r"(\d+)$", last[0].get("crew_code", "") or "")
            if m: n = int(m.group(1)) + 1
        crew_code = f"CR-{n:03d}"
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "crew_code": crew_code,
        "name": args["name"],
        "trade": args["trade"],
        "foreman_person_id": args.get("foreman_id"),
        "home_yard_address": None,
        "hourly_burdened_cost": args.get("hourly_burdened_cost"),
        "target_size": int(args.get("target_size", 4)),
        "notes": args.get("notes"),
        "status": "active",
        "created_at": utc_now().isoformat(),
    }
    await db.crews.insert_one(doc.copy())
    return {"created": {"resource": "crew", "id": doc["id"], "crew_code": crew_code, "name": doc["name"], "trade": doc["trade"]}}


async def _tool_create_person(ctx: dict, args: dict) -> dict:
    if "full_name" not in args and "first_name" not in args:
        return {"error": "first_name + last_name OR full_name required"}
    if "full_name" in args:
        parts = args["full_name"].split(maxsplit=1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
    else:
        first_name = args["first_name"]
        last_name = args.get("last_name", "")
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "employee_number": args.get("employee_number"),
        "first_name": first_name,
        "last_name": last_name,
        "email": args.get("email"),
        "phone": args.get("phone"),
        "role": args.get("role"),
        "trade": args.get("trade"),
        "employment_type": args.get("employment_type", "ft"),
        "union_local": args.get("union_local"),
        "prevailing_wage_classification": args.get("prevailing_wage_classification"),
        "hourly_rate": args.get("hourly_rate") or args.get("hourly_rate_usd"),
        "e_verify_status": "pending",
        "i9_on_file": False,
        "cleared_for_federal": False,
        "status": "active",
        "created_at": utc_now().isoformat(),
    }
    await db.people.insert_one(doc.copy())
    return {"created": {"resource": "person", "id": doc["id"],
                        "first_name": first_name, "last_name": last_name,
                        "role": doc.get("role"), "trade": doc.get("trade"),
                        "hourly_rate": doc.get("hourly_rate")}}


async def _tool_create_equipment(ctx: dict, args: dict) -> dict:
    if "name" not in args and "asset_tag" not in args:
        return {"error": "asset_tag or name required"}
    db = get_db()
    asset_tag = args.get("asset_tag") or f"E-{gen_id()[:6].upper()}"
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "asset_tag": asset_tag,
        "type": args.get("equipment_type") or args.get("type") or "general",
        "manufacturer": args.get("manufacturer"),
        "model": args.get("model") or args.get("name"),
        "year": args.get("year"),
        "ownership": args.get("ownership") or args.get("owner") or "owned",
        "hourly_rate": args.get("hourly_rate") or args.get("hourly_rate_usd"),
        "daily_rate": args.get("daily_rate"),
        "monthly_rate": args.get("monthly_rate"),
        "status": "available",
        "created_at": utc_now().isoformat(),
    }
    await db.equipment.insert_one(doc.copy())
    return {"created": {"resource": "equipment", "id": doc["id"],
                        "asset_tag": asset_tag, "type": doc["type"],
                        "manufacturer": doc.get("manufacturer"),
                        "model": doc.get("model"), "year": doc.get("year"),
                        "ownership": doc.get("ownership"),
                        "hourly_rate": doc.get("hourly_rate")}}


async def _tool_create_floor(ctx: dict, args: dict) -> dict:
    if "floor_number" not in args or "floor_name" not in args:
        return {"error": "floor_number + floor_name required"}
    db = get_db()
    project_id = ctx.get("project_id")
    if not project_id:
        return {"error": "no active project"}
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "floor_number": int(args["floor_number"]),
        "floor_name": args["floor_name"],
        "elevation_z_ft": float(args.get("elevation_z_ft", 0)),
        "floor_height_ft": float(args.get("floor_height_ft", 12)),
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.floors.insert_one(doc.copy())
    return {"created": {"resource": "floor", "id": doc["id"], "floor_number": doc["floor_number"], "floor_name": doc["floor_name"]}}


async def _tool_create_location(ctx: dict, args: dict) -> dict:
    if "name" not in args or "floor_id" not in args:
        return {"error": "name + floor_id required (call get_status entity_type=floors first to find the floor_id)"}
    db = get_db()
    floor = await db.floors.find_one(tenant_filter(ctx["tenant_id"], {"id": args["floor_id"]}), {"_id": 0})
    if not floor:
        return {"error": f"floor {args['floor_id']} not found"}
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": floor["project_id"],
        "floor_id": args["floor_id"],
        "zone_id": None,
        "code": args.get("code") or args["name"][:24],
        "name": args["name"],
        "location_type": args.get("location_type", "room"),
        "area_sqft": args.get("area_sqft"),
        "spatial_centroid": None,
        "polygon_geojson": None,
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.locations.insert_one(doc.copy())
    return {"created": {"resource": "location", "id": doc["id"], "name": doc["name"], "code": doc["code"]}}


async def _tool_create_delay(ctx: dict, args: dict) -> dict:
    if "description" not in args:
        return {"error": "description required"}
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": ctx.get("project_id"),
        "daily_report_id": ctx.get("daily_report_id") or args.get("daily_report_id"),
        "date": args.get("date") or date.today().isoformat(),
        "description": args["description"],
        "severity": args.get("severity", "moderate"),
        "delay_days": float(args.get("delay_days", args.get("delay_minutes", 0)) or 0) if args.get("delay_days") is not None or args.get("delay_minutes") is not None else None,
        "reason_category": args.get("reason_category", "weather"),
        "status": "open",
        "reported_by": ctx["user"]["id"],
        "reported_at": utc_now().isoformat(),
        "created_at": utc_now().isoformat(),
    }
    await db.delays.insert_one(doc.copy())
    return {"created": {"resource": "delay", "id": doc["id"], "description": doc["description"]}}


async def _tool_create_safety_event(ctx: dict, args: dict) -> dict:
    if "description" not in args:
        return {"error": "description required"}
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": ctx.get("project_id"),
        "daily_report_id": ctx.get("daily_report_id") or args.get("daily_report_id"),
        "date": args.get("date") or date.today().isoformat(),
        "description": args["description"],
        "event_type": args.get("event_type", "near_miss"),
        "severity": args.get("severity", "minor"),
        "status": "open",
        "reported_by": ctx["user"]["id"],
        "reported_at": utc_now().isoformat(),
        "created_at": utc_now().isoformat(),
    }
    await db.safety_events.insert_one(doc.copy())
    # Bucket G Phase 2 -- AI safety-analyzer producer. The description
    # (+ severity / event_type if AI-derived) lands as pending Jigsaw
    # suggestions on the freshly-created safety event so a human
    # confirms what the copilot wrote. Audit trail preserves AI
    # authorship + the confirmation decision.
    await _emit_copilot_suggestions(
        ctx=ctx, entity_type="safety_event", entity_id=doc["id"],
        project_id=doc.get("project_id"),
        fields={"description": doc["description"],
                 "severity": doc.get("severity"),
                 "event_type": doc.get("event_type")},
        source="ai_safety_analyzer",
    )
    return {"created": {"resource": "safety_event", "id": doc["id"], "description": doc["description"]}}


async def _tool_create_qc_issue(ctx: dict, args: dict) -> dict:
    if "description" not in args:
        return {"error": "description required"}
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": ctx.get("project_id"),
        "daily_report_id": ctx.get("daily_report_id") or args.get("daily_report_id"),
        "date": args.get("date") or date.today().isoformat(),
        "description": args["description"],
        "severity": args.get("severity", "minor"),
        "status": "open",
        "reported_by": ctx["user"]["id"],
        "reported_at": utc_now().isoformat(),
        "resolved_at": None,
        "resolution_notes": None,
        "created_at": utc_now().isoformat(),
    }
    await db.qc_issues.insert_one(doc.copy())
    # Bucket G Phase 2 -- AI qc-analyzer producer. Same pattern as
    # safety: emit pending suggestions on the description + severity
    # so a human confirms what the copilot wrote.
    await _emit_copilot_suggestions(
        ctx=ctx, entity_type="qc_issue", entity_id=doc["id"],
        project_id=doc.get("project_id"),
        fields={"description": doc["description"],
                 "severity": doc.get("severity")},
        source="ai_qc_analyzer",
    )
    return {"created": {"resource": "qc_issue", "id": doc["id"], "description": doc["description"]}}


# =====================================================================
# Bucket G Phase 2 -- shared helper for copilot tool producers.
# Suggested defaults (severity="minor", event_type="near_miss") are
# excluded; the suggestion framework should only flag fields that
# the AI provided substantively, otherwise users get a sea of
# "AI suggested 'minor'" badges that are just defaults.
# =====================================================================
_COPILOT_DEFAULT_VALUES = {"minor", "near_miss"}


async def _emit_copilot_suggestions(
    *, ctx: dict, entity_type: str, entity_id: str,
    project_id: Optional[str], fields: dict, source: str,
) -> None:
    if not project_id:
        return
    try:
        from services import field_suggestions as fs
        for fpath, val in (fields or {}).items():
            if val is None:
                continue
            # Skip stamp-only defaults.
            if isinstance(val, str) and val in _COPILOT_DEFAULT_VALUES:
                continue
            await fs.create_suggestion(
                tenant_id=ctx["tenant_id"], project_id=project_id,
                entity_type=entity_type, entity_id=entity_id,
                field_path=fpath, suggested_value=val,
                suggested_by=source,
                source_context=(
                    f"copilot_tool=create_{entity_type}; "
                    f"actor={ctx['user']['id']}"
                ),
                actor_id=ctx["user"]["id"],
                request_id=ctx.get("request_id"),
            )
    except Exception as exc:
        import logging
        logging.getLogger("copilot_tools").warning(
            "field_suggestion producer (%s) failed: %s", source, exc,
        )


async def _tool_create_delivery(ctx: dict, args: dict) -> dict:
    if "vendor" not in args or "description" not in args:
        return {"error": "vendor + description required"}
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": ctx.get("project_id"),
        "delivery_date": args.get("delivery_date") or (date.today() + timedelta(days=7)).isoformat(),
        "vendor": args["vendor"],
        "description": args["description"],
        "po_number": args.get("po_number"),
        "status": "scheduled",
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.deliveries.insert_one(doc.copy())
    return {"created": {"resource": "delivery", "id": doc["id"], "vendor": doc["vendor"], "description": doc["description"]}}


# ---- Generic delete (works for any allow-listed entity) -----------------
DELETABLE_ENTITIES = {
    "task": "tasks", "rfi": "rfis", "submittal": "submittals",
    "work_item": "work_items", "kb_entry": "kb_entries",
    "crew": "crews", "person": "people", "equipment": "equipment",
    "floor": "floors", "location": "locations", "delay": "delays",
    "safety_event": "safety_events", "qc_issue": "qc_issues",
    "delivery": "deliveries", "change_order": "change_orders",
    "photo": "photos",
}


async def _tool_delete_entity(ctx: dict, args: dict) -> dict:
    """Soft-delete (status='deleted') any record by id or by code lookup."""
    et = args.get("entity_type")
    coll_name = DELETABLE_ENTITIES.get(et)
    if not coll_name:
        return {"error": f"unknown entity_type '{et}'. Allowed: {sorted(DELETABLE_ENTITIES)}"}
    db = get_db()
    coll = db[coll_name]
    # Lookup
    target_id = args.get("id")
    if not target_id:
        # try lookup by code
        if et == "task" and args.get("activity_code"):
            d = await coll.find_one(tenant_filter(ctx["tenant_id"],
                                                  {"activity_code": args["activity_code"]}), {"_id": 0})
            if d: target_id = d["id"]
        elif et == "rfi" and args.get("rfi_number"):
            d = await coll.find_one(tenant_filter(ctx["tenant_id"],
                                                  {"rfi_number": args["rfi_number"]}), {"_id": 0})
            if d: target_id = d["id"]
        elif et == "submittal" and args.get("submittal_number"):
            d = await coll.find_one(tenant_filter(ctx["tenant_id"],
                                                  {"submittal_number": args["submittal_number"]}), {"_id": 0})
            if d: target_id = d["id"]
    if not target_id:
        return {"error": "id (or activity_code/rfi_number/submittal_number) required"}
    res = await coll.update_one(
        tenant_filter(ctx["tenant_id"], {"id": target_id}),
        {"$set": {"status": "deleted",
                  "deleted_at": utc_now().isoformat(),
                  "deleted_by": ctx["user"]["id"]}},
    )
    if not res.matched_count:
        return {"error": f"{et} {target_id} not found"}
    return {"deleted": {"resource": et, "id": target_id}}


async def _tool_update_entity(ctx: dict, args: dict) -> dict:
    """Patch any allow-listed entity by id with arbitrary fields."""
    et = args.get("entity_type")
    coll_name = DELETABLE_ENTITIES.get(et)
    if not coll_name:
        return {"error": f"unknown entity_type '{et}'"}
    db = get_db()
    coll = db[coll_name]
    target_id = args.get("id")
    if not target_id:
        return {"error": "id required"}
    fields = args.get("fields") or {}
    for k in ("id", "tenant_id", "created_at", "created_by", "_id"):
        fields.pop(k, None)
    if not fields:
        return {"error": "fields required"}
    fields["updated_at"] = utc_now().isoformat()
    fields["updated_by"] = ctx["user"]["id"]
    res = await coll.update_one(
        tenant_filter(ctx["tenant_id"], {"id": target_id}), {"$set": fields},
    )
    if not res.matched_count:
        return {"error": f"{et} {target_id} not found"}
    return {"updated": {"resource": et, "id": target_id, "fields": fields}}


async def _tool_link_submittal_to_activity(ctx: dict, args: dict) -> dict:
    sn = args.get("submittal_number")
    code = args.get("activity_code")
    if not sn or not code:
        return {"error": "submittal_number + activity_code required"}
    db = get_db()
    sub = await db.submittals.find_one(
        tenant_filter(ctx["tenant_id"], {"submittal_number": sn,
                                         "project_id": ctx.get("project_id")}),
        {"_id": 0},
    )
    if not sub:
        return {"error": f"submittal {sn} not found"}
    task = await db.tasks.find_one(
        tenant_filter(ctx["tenant_id"], {"activity_code": code,
                                         "project_id": ctx.get("project_id")}),
        {"_id": 0},
    )
    if not task:
        return {"error": f"task {code} not found"}
    await db.submittals.update_one({"id": sub["id"]},
                                   {"$set": {"schedule_activity_id": task["id"]}})
    return {"updated": {"resource": "submittal", "submittal_number": sn, "id": sub["id"], "linked_activity_code": code, "schedule_activity_id": task["id"]}}


# ---- DAILY-REPORT-SPECIFIC TOOLS ---------------------------------------
async def _tool_record_presence(ctx: dict, args: dict) -> dict:
    """Insert a daily_presence row for the active daily_report_id."""
    if "trade" not in args:
        return {"error": "trade required"}
    dr_id = ctx.get("daily_report_id") or args.get("daily_report_id")
    if not dr_id:
        return {"error": "no active daily_report_id"}
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": dr_id}), {"_id": 0, "status": 1})
    if not dr:
        return {"error": f"daily report {dr_id} not found"}
    if dr.get("status") == "submitted":
        return {"error": "daily report locked"}
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "daily_report_id": dr_id,
        "trade": args["trade"],
        "sub_id": args.get("sub_id") or args.get("crew_id"),
        "company_name": args.get("company_name"),
        "headcount": int(args.get("headcount", 0)),
        "hours_per_person": float(args.get("hours_per_person", 8)),
        "notes": args.get("notes"),
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    }
    await db.daily_presence.insert_one(doc.copy())
    return {"created": {"resource": "presence", "id": doc["id"],
                        "trade": doc["trade"], "headcount": doc["headcount"],
                        "hours_per_person": doc["hours_per_person"]}}


async def _tool_mark_activity_status(ctx: dict, args: dict) -> dict:
    """Update a daily_report_activities row (pct, status, hours, delay)
    by activity_code, scoped to the current daily_report_id."""
    code = args.get("activity_code")
    if not code:
        return {"error": "activity_code required"}
    dr_id = ctx.get("daily_report_id") or args.get("daily_report_id")
    if not dr_id:
        return {"error": "no active daily_report_id"}
    db = get_db()
    row = await db.daily_report_activities.find_one(
        {"daily_report_id": dr_id, "activity_code": code}, {"_id": 0},
    )
    if not row:
        return {"error": f"activity {code} not in this daily report grid"}
    updates: dict = {}
    if "pct_complete_end_of_day" in args:
        updates["pct_complete_end_of_day"] = float(args["pct_complete_end_of_day"])
    if "hours_worked" in args:
        updates["hours_worked"] = float(args["hours_worked"])
    if "delay_minutes" in args:
        updates["delay_minutes"] = int(args["delay_minutes"])
    if "delay_reason" in args:
        updates["delay_reason"] = args["delay_reason"]
    if "status_at_end_of_day" in args:
        updates["status_at_end_of_day"] = args["status_at_end_of_day"]
    if "notes" in args:
        updates["notes"] = args["notes"]
    if "crew_count" in args:
        updates["crew_count"] = int(args["crew_count"])
    if not updates:
        return {"error": "no fields to update"}
    await db.daily_report_activities.update_one({"id": row["id"]}, {"$set": updates})
    # cascade pct back to task
    if "pct_complete_end_of_day" in updates:
        new_pct = float(updates["pct_complete_end_of_day"])
        await db.tasks.update_one(
            tenant_filter(ctx["tenant_id"], {"id": row["schedule_activity_id"]}),
            {"$set": {
                "prev_pct_complete": row.get("pct_complete_end_of_day", 0),
                "pct_complete": new_pct,
                "status": "complete" if new_pct >= 100 else "in_progress",
            }},
        )
    return {"updated": {"resource": "activity_grid_row", "activity_code": code,
                        "id": row["id"], "fields": updates}}


async def _tool_create_unplanned_activity(ctx: dict, args: dict) -> dict:
    """Create a Task with created_in_field=true + push to current DR grid."""
    if "description" not in args:
        return {"error": "description required"}
    dr_id = ctx.get("daily_report_id") or args.get("daily_report_id")
    if not dr_id:
        return {"error": "no active daily_report_id"}
    db = get_db()
    dr = await db.daily_reports.find_one(tenant_filter(ctx["tenant_id"], {"id": dr_id}), {"_id": 0})
    if not dr:
        return {"error": f"daily report {dr_id} not found"}
    if dr.get("status") == "submitted":
        return {"error": "daily report locked"}
    last = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": dr["project_id"]}),
        {"_id": 0, "activity_code": 1},
    ).sort("activity_code", -1).limit(50).to_list(50)
    next_n = 1
    for r in last:
        m = re.search(r"(\d+)$", r.get("activity_code", "") or "")
        if m:
            next_n = max(next_n, int(m.group(1)) + 1)
    activity_code = f"AF{next_n:04d}"
    today_iso = dr["report_date"]
    dur = float(args.get("duration_days") or 1)
    finish = (date.fromisoformat(today_iso) + timedelta(days=int(dur))).isoformat()
    task_doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": dr["project_id"],
        "activity_code": activity_code,
        "description": args["description"],
        "trade_code": args.get("trade_code"),
        "location_id": args.get("location_id"),
        "crew_id": args.get("crew_id"),
        "status": "in_progress",
        "planned_start": today_iso,
        "planned_finish": finish,
        "duration_days": dur,
        "actual_hours": 0,
        "actual_quantity": 0,
        "pct_complete": 0,
        "prev_pct_complete": 0,
        "is_milestone": False,
        "is_critical_path": False,
        "lookahead_status": "in_window",
        "predecessors": [],
        "created_in_field": True,
        "requires_pm_review": True,
        "created_from_daily_report_id": dr_id,
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.tasks.insert_one(task_doc.copy())
    pos_max = await db.daily_report_activities.find({"daily_report_id": dr_id}, {"position": 1}).sort("position", -1).limit(1).to_list(1)
    next_pos = (pos_max[0]["position"] + 1) if pos_max else 0
    row = {
        "id": gen_id(),
        "daily_report_id": dr_id,
        "schedule_activity_id": task_doc["id"],
        "position": next_pos,
        "pct_complete_start_of_day": 0,
        "pct_complete_end_of_day": 0,
        "crew_count": 0,
        "crew_id": args.get("crew_id"),
        "hours_worked": 0,
        "delay_minutes": 0,
        "delay_reason": None,
        "weather_impact_minutes": 0,
        "notes": None,
        "status_at_end_of_day": "in_progress",
        "activity_code": activity_code,
        "activity_description": args["description"],
        "location_name": None,
        "trade_code": args.get("trade_code"),
    }
    await db.daily_report_activities.insert_one(row.copy())
    return {"created": {"resource": "unplanned_activity", "id": task_doc["id"],
                        "activity_code": activity_code,
                        "description": args["description"],
                        "requires_pm_review": True}}


TOOL_REGISTRY = {
    "search": _tool_search,
    "get_status": _tool_get_status,
    "get_daily_report": _tool_get_daily_report,
    "query": _tool_query,
    "get_project_context": _tool_get_project_context,
    "get_schedule_analysis": _tool_get_schedule_analysis,
    "create_task": _tool_create_task,
    "update_task": _tool_update_task,
    "mark_task_complete": _tool_mark_task_complete,
    "create_rfi": _tool_create_rfi,
    "create_daily_report": _tool_create_daily_report,
    "create_work_item": _tool_create_work_item,
    "create_kb_entry": _tool_create_kb_entry,
    "create_submittal": _tool_create_submittal,
    "link_submittal_to_activity": _tool_link_submittal_to_activity,
    "create_crew": _tool_create_crew,
    "create_person": _tool_create_person,
    "create_equipment": _tool_create_equipment,
    "create_floor": _tool_create_floor,
    "create_location": _tool_create_location,
    "create_delay": _tool_create_delay,
    "create_safety_event": _tool_create_safety_event,
    "create_qc_issue": _tool_create_qc_issue,
    "create_delivery": _tool_create_delivery,
    "delete_entity": _tool_delete_entity,
    "update_entity": _tool_update_entity,
    "record_presence": _tool_record_presence,
    "mark_activity_status": _tool_mark_activity_status,
    "create_unplanned_activity": _tool_create_unplanned_activity,
}


# ---- ORCHESTRATOR -------------------------------------------------------
SYSTEM_PROMPT = (
    "You are Mason, the AI craftsman inside ICE Schedule Ops, a federal-"
    "construction project-management platform. You can READ and WRITE to "
    "the project's database via the tool calls below. ALWAYS reply with a "
    "JSON object only — no prose outside the JSON. Plain text only inside "
    "'reply' (NO markdown, no asterisks, no fenced code blocks, no headers, "
    "no bullet markers like '-' or '*' or '+', no tables). Use [A1240] for "
    "activity codes, RFI-014 for RFI numbers.\n\n"
    "AGENTIC LOOP (important): when you call tools, you will be sent the "
    "tool results back in a follow-up message. At that point you MUST write "
    "the final answer for the user — actions=[] and a clean 'reply' that "
    "uses the data the tools returned. Do NOT keep saying 'I will retrieve' "
    "or 'one moment'. Either call tools immediately OR write the final reply. "
    "After your first turn, the system will hand you results — use them.\n\n"
    "DATES: when the user mentions a date in any format (\"may 22 2026\", "
    "\"22/5/2026\", \"5/22/26\"), prefer the get_daily_report tool which "
    "accepts any common date format and returns the fully-hydrated DR.\n\n"
    + TOOL_CATALOGUE
)


async def execute_copilot_turn(*, tenant_id: str, project_id: Optional[str],
                                user_id: str, user_role: str, session_id: str,
                                message: str,
                                daily_report_id: Optional[str] = None) -> dict:
    """Single turn: parse → execute tools → format reply.

    1. Inject project context (master-doc + KB + lookahead + RFIs)
    2. Inject daily report context if daily_report_id is passed
    3. Ask Claude for a JSON action plan
    4. Execute each action in order against the DB
    5. Return {reply, actions_executed, tool_results}
    """
    from services.ai_agents import _master_doc_block, _kb_block
    db = get_db()
    blocks: list[str] = []
    if project_id:
        md = await _master_doc_block(tenant_id, project_id)
        if md: blocks.append(md)
        kb = await _kb_block(tenant_id, project_id, message)
        if kb: blocks.append(kb)
        proj = await db.projects.find_one(tenant_filter(tenant_id, {"id": project_id}), {"_id": 0})
        if proj:
            blocks.append(f"PROJECT: {proj['name']} (status={proj.get('status')})")
        recent_tasks = await db.tasks.find(
            tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
            {"_id": 0, "activity_code": 1, "description": 1, "pct_complete": 1,
             "planned_start": 1, "planned_finish": 1, "status": 1},
        ).sort("planned_start", -1).limit(15).to_list(15)
        if recent_tasks:
            blocks.append("CURRENT ACTIVITIES:\n" + "\n".join(
                f"  [{t['activity_code']}] {t['description']} — {t.get('pct_complete', 0)}% ({t.get('status')})"
                for t in recent_tasks
            ))

    # Daily-report scoping — drives the DR-only tools and gives the LLM
    # the current grid + presence + open delays/safety/qc so it can act.
    if daily_report_id:
        dr = await db.daily_reports.find_one(
            tenant_filter(tenant_id, {"id": daily_report_id}), {"_id": 0},
        )
        if dr:
            grid = await db.daily_report_activities.find(
                {"daily_report_id": daily_report_id}, {"_id": 0},
            ).sort("position", 1).to_list(50)
            presence = await db.daily_presence.find(
                tenant_filter(tenant_id, {"daily_report_id": daily_report_id}), {"_id": 0},
            ).to_list(40)
            grid_lines = "\n".join(
                f"    [{r.get('activity_code')}] {(r.get('activity_description') or '')[:60]} — "
                f"SOD {r.get('pct_complete_start_of_day') or 0}% / EOD {r.get('pct_complete_end_of_day') or 0}% / "
                f"crew {r.get('crew_count') or 0} / {r.get('hours_worked') or 0}h / status {r.get('status_at_end_of_day') or 'not_started'}"
                for r in grid
            ) or "    (grid empty)"
            pres_lines = "\n".join(
                f"    {p['trade']}: headcount {p.get('headcount', 0)}, {p.get('hours_per_person', 0)}h/person"
                for p in presence
            ) or "    (no presence rows)"
            blocks.append(
                f"DAILY REPORT IN FOCUS: {dr['report_date']} (status={dr['status']}, id={daily_report_id})\n"
                f"WEATHER: {dr.get('weather_conditions') or 'n/a'} · "
                f"high {dr.get('weather_temp_high_f') or '—'}°F / low {dr.get('weather_temp_low_f') or '—'}°F · "
                f"precip {dr.get('weather_precipitation_in') or 0}in\n"
                f"ACTIVITY GRID:\n{grid_lines}\n"
                f"PRESENCE:\n{pres_lines}"
            )
    # Phase D — when a daily report is in focus, attach today's photos as
    # vision context. Capped at 6 photos (token cost). Each photo becomes
    # an ImageContent on the UserMessage, letting the LLM ground draft
    # descriptions ("draft a QC issue from today's photos") in the actual
    # field imagery rather than guessing from text alone.
    photo_attachments: list = []
    photo_summary_lines: list[str] = []
    if daily_report_id:
        photos = await db.photos.find(
            tenant_filter(tenant_id, {"daily_report_id": daily_report_id}),
            {"_id": 0, "id": 1, "storage_path": 1, "caption": 1, "captured_at": 1},
        ).sort("captured_at", -1).limit(6).to_list(6)
        if photos:
            from emergentintegrations.llm.chat import ImageContent  # type: ignore
            from services.storage import get_object
            import base64 as _b64
            for p in photos:
                if not p.get("storage_path"):
                    continue
                try:
                    data, _ct = get_object(p["storage_path"])
                    b64 = _b64.b64encode(data).decode()
                    photo_attachments.append(ImageContent(image_base64=b64))
                    photo_summary_lines.append(
                        f"  photo {p.get('id', '?')[:8]} captured {p.get('captured_at')}"
                        + (f" — {p['caption']}" if p.get("caption") else "")
                    )
                except Exception:
                    # If a single photo fails to load (storage hiccup), skip it
                    # rather than failing the whole copilot turn.
                    continue
            if photo_summary_lines:
                blocks.append(
                    "TODAY'S DAILY-REPORT PHOTOS (attached as vision context, in order):\n"
                    + "\n".join(photo_summary_lines)
                    + "\nWhen the user asks to draft a QC issue or safety event from photos, "
                    "examine them visually and produce a concrete description (no markdown). "
                    "When generating description text the user will paste into a form, put it "
                    "verbatim into the 'reply' field — do NOT call create_qc_issue / "
                    "create_safety_event tools yourself; the user will use the Send-to-form "
                    "button to insert it."
                )

    user_text = "\n\n".join(blocks) + f"\n\nUser request: {message}"
    if not blocks:
        user_text = f"User request: {message}"

    chat = LlmChat(
        api_key=os.environ.get("EMERGENT_LLM_KEY", ""),
        session_id=session_id,
        system_message=SYSTEM_PROMPT,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    started = datetime.now(timezone.utc)

    ctx = {"tenant_id": tenant_id, "project_id": project_id,
           "daily_report_id": daily_report_id,
           "user": {"id": user_id, "role": user_role}}

    # pt.28 AGENTIC LOOP: keep round-tripping with the LLM until either
    # it returns no more tool calls (final reply) or we hit the safety
    # cap. Before pt.28 this was single-shot — the LLM wrote its reply
    # BEFORE tools ran, so the reply never actually contained tool data.
    # Result was "I will retrieve…" + silence. Now after each tool batch
    # we feed the results back in and ask for a follow-up reply.
    MAX_ITERATIONS = 4
    all_actions: list[dict] = []
    all_tool_results: list[dict] = []
    reply = ""

    # First message — user request with full context
    next_msg = UserMessage(text=user_text, file_contents=photo_attachments) if photo_attachments \
        else UserMessage(text=user_text)

    for iteration in range(MAX_ITERATIONS):
        try:
            raw = await chat.send_message(next_msg)
        except Exception as e:
            reply = f"AI error: {e}"
            break

        raw = str(raw).strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE)
        try:
            plan = json.loads(cleaned)
        except Exception:
            m = re.search(r"\{[\s\S]+\}", cleaned)
            plan = json.loads(m.group(0)) if m else {"actions": [], "reply": "Sorry, I could not interpret that."}

        actions = plan.get("actions") or []
        # Permission gate: voice role can only READ
        if user_role == "voice":
            actions = [a for a in actions if a.get("tool") not in WRITE_TOOLS]

        # No actions → AI is done, capture the final reply and break
        if not actions:
            reply = plan.get("reply") or "Done."
            break

        # Execute every action this round
        turn_results: list[dict] = []
        for a in actions:
            tool = a.get("tool")
            args = a.get("args") or {}
            impl = TOOL_REGISTRY.get(tool)
            if not impl:
                rec = {"tool": tool, "error": "unknown tool"}
            else:
                try:
                    res = await impl(ctx, args)
                    rec = {"tool": tool, "args": args, "result": res}
                except Exception as e:
                    rec = {"tool": tool, "args": args, "error": str(e)}
            turn_results.append(rec)
            all_actions.append(a)
            all_tool_results.append(rec)

        # If this is the last allowed iteration we'll use the in-plan
        # reply as-is (no more LLM turns). Otherwise we hand the tool
        # results back and ask for a clean summary or more tools.
        if iteration == MAX_ITERATIONS - 1:
            reply = plan.get("reply") or "Done."
            break

        # Cap the tool-results payload so we don't blow the LLM context
        try:
            results_json = json.dumps(turn_results, default=str)
        except Exception:
            results_json = str(turn_results)
        if len(results_json) > 12000:
            results_json = results_json[:12000] + " …(truncated)"
        next_msg = UserMessage(text=(
            "Here are the results from the tool calls you just made:\n"
            f"{results_json}\n\n"
            "Now write the final answer for the user using these results. "
            "Reply with the same JSON schema: actions=[] and a 'reply' field "
            "containing a clean plain-text summary (NO markdown, NO bullet "
            "markers). If you genuinely need to call MORE tools first, you "
            "may return more actions instead — but only if essential."
        ))

    if not reply:
        reply = "Done."
    reply = _strip_markdown(reply)
    actions = all_actions
    tool_results = all_tool_results

    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    # audit
    await db.copilot_messages.insert_one({
        "id": gen_id(),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "request_text": message,
        "reply_text": reply,
        "actions": actions,
        "tool_results": tool_results,
        "latency_ms": latency_ms,
        "created_at": utc_now().isoformat(),
    })
    return {"reply": reply, "actions_executed": actions, "tool_results": tool_results}
