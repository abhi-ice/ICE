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
    return {"created_task": {"id": doc["id"], "activity_code": activity_code,
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
    return {"updated_task": {"activity_code": code, **updates}}


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
        "question": args["question"],
        "trade_code": args.get("trade_code"),
        "spec_section_codes": [],
        "drawing_revision_ids": [],
        "status": "draft",
        "date_initiated": date.today().isoformat(),
        "answer": None,
        "answered_at": None,
        "initiated_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    }
    await db.rfis.insert_one(rfi.copy())
    return {"created_rfi": {"rfi_number": rfi["rfi_number"], "subject": args["subject"]}}


async def _tool_create_daily_report(ctx: dict, args: dict) -> dict:
    db = get_db()
    project_id = ctx.get("project_id")
    rd = args.get("date") or date.today().isoformat()
    existing = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "report_date": rd}),
        {"_id": 0},
    )
    if existing:
        return {"daily_report": {"id": existing["id"], "report_date": rd, "status": existing["status"], "already_existed": True}}
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
    return {"created_daily_report": {"id": doc["id"], "report_date": rd}}


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
    return {"created_work_item": {"id": doc["id"], "title": args["title"], "priority": args.get("priority", "normal")}}


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
    return {"created_kb_entry": {"id": doc["id"], "title": args["title"]}}


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
    return {"created_submittal": {"submittal_number": doc["submittal_number"], "title": args["title"]}}


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
    return {"linked": {"submittal_number": sn, "activity_code": code}}


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
                                message: str) -> dict:
    """Single turn: parse → execute tools → format reply.

    1. Inject project context (master-doc + KB + lookahead + RFIs)
    2. Ask Claude for a JSON action plan
    3. Execute each action in order against the DB
    4. Return {reply, actions_executed, tool_results}
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
