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
  - get_status                args: {entity_type: 'tasks'|'rfis'|'submittals'|'work_items', filter?: str}
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
  - create_delay              args: {description: str, severity?: 'low'|'moderate'|'high', delay_minutes?: int, reason_category?: 'weather'|'rfi_pending'|'submittal_pending'|'design_change'|'site_condition'|'manpower'|'other', date?: 'YYYY-MM-DD'}
  - create_safety_event       args: {description: str, event_type?: 'near_miss'|'first_aid'|'recordable'|'lost_time', severity?: 'low'|'moderate'|'high', osha_recordable?: bool}
  - create_qc_issue           args: {description: str, severity?: 'minor'|'major'|'critical', spec_section_ref?: str}
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
    from routers.iter5_router import search as _search  # type: ignore
    res = await _search(args.get("query", ""), ctx)  # type: ignore
    return {"results": (res.get("results") or [])[:10]}


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
        "date": args.get("date") or date.today().isoformat(),
        "description": args["description"],
        "severity": args.get("severity", "moderate"),
        "delay_minutes": int(args.get("delay_minutes", 60)),
        "reason_category": args.get("reason_category", "weather"),
        "status": "open",
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
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
        "date": args.get("date") or date.today().isoformat(),
        "description": args["description"],
        "event_type": args.get("event_type", "near_miss"),
        "severity": args.get("severity", "moderate"),
        "osha_recordable": bool(args.get("osha_recordable", False)),
        "status": "investigating",
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.safety_events.insert_one(doc.copy())
    return {"created": {"resource": "safety_event", "id": doc["id"], "description": doc["description"]}}


async def _tool_create_qc_issue(ctx: dict, args: dict) -> dict:
    if "description" not in args:
        return {"error": "description required"}
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": ctx.get("project_id"),
        "date": args.get("date") or date.today().isoformat(),
        "description": args["description"],
        "severity": args.get("severity", "minor"),
        "spec_section_ref": args.get("spec_section_ref"),
        "status": "open",
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    }
    await db.qc_issues.insert_one(doc.copy())
    return {"created": {"resource": "qc_issue", "id": doc["id"], "description": doc["description"]}}


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
    "You are the AI copilot inside ICE Schedule Ops, a federal-construction "
    "project-management platform. You can READ and WRITE to the project's "
    "database via the tool calls below. ALWAYS reply with a JSON object only — "
    "no prose outside the JSON. Plain text only inside 'reply' (NO markdown, "
    "no asterisks, no fenced code blocks, no headers, no bullet markers like "
    "'-' or '*' or '+', no tables). Use [A1240] for activity codes, RFI-014 "
    "for RFI numbers.\n\n"
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
    user_text = "\n\n".join(blocks) + f"\n\nUser request: {message}"
    if not blocks:
        user_text = f"User request: {message}"

    chat = LlmChat(
        api_key=os.environ.get("EMERGENT_LLM_KEY", ""),
        session_id=session_id,
        system_message=SYSTEM_PROMPT,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    started = datetime.now(timezone.utc)
    try:
        raw = await chat.send_message(UserMessage(text=user_text))
    except Exception as e:
        return {"reply": f"AI error: {e}", "actions_executed": [], "tool_results": []}

    raw = str(raw).strip()
    # Strip ```json ... ``` fences if present
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE)
    plan: dict
    try:
        plan = json.loads(cleaned)
    except Exception:
        # last resort — try to extract { ... }
        m = re.search(r"\{[\s\S]+\}", cleaned)
        plan = json.loads(m.group(0)) if m else {"actions": [], "reply": "Sorry, I could not interpret that."}

    actions = plan.get("actions") or []
    tool_results: list[dict] = []

    # Permission gate: voice role can only READ
    if user_role == "voice":
        actions = [a for a in actions if a.get("tool") not in WRITE_TOOLS]

    ctx = {"tenant_id": tenant_id, "project_id": project_id,
           "daily_report_id": daily_report_id,
           "user": {"id": user_id, "role": user_role}}

    for a in actions:
        tool = a.get("tool")
        args = a.get("args") or {}
        impl = TOOL_REGISTRY.get(tool)
        if not impl:
            tool_results.append({"tool": tool, "error": "unknown tool"})
            continue
        try:
            res = await impl(ctx, args)
            tool_results.append({"tool": tool, "args": args, "result": res})
        except Exception as e:
            tool_results.append({"tool": tool, "args": args, "error": str(e)})

    reply = _strip_markdown(plan.get("reply") or "Done.")
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
