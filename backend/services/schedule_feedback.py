"""One-way schedule feedback from Daily Reports.

* recompute_task_actuals -- actual start/finish derived from the DR rows that
  still exist (first worked day / first day at 100%).
* open_delays_for_task   -- open/in-progress delays that block adding a task
  to a DR until resolved.
* logic proposals        -- out-of-order work and unplanned work create
  PENDING relationship-change proposals (AI-drafted) for admin approval.
  Nothing here ever edits predecessors on its own.
* activity name suggestions -- heading-based names plus AI for duplicates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date
from typing import Optional

from database import tenant_filter
from models import gen_id, utc_now, pred_code, pred_relationship

log = logging.getLogger(__name__)

OPEN_DELAY_STATUSES = ["open", "in_progress"]
PAPERWORK = {"preconstruction_paperwork", "construction_paperwork", "closeout_paperwork"}
_bg_tasks: set = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _d(v) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


# ---------------------------------------------------------------- actuals
async def recompute_task_actuals(db, tenant_id: str, task_id: str) -> dict:
    """actual_start = first DR date the activity was worked (progress made or
    hours logged); actual_finish = first DR date it reached 100%. Imported
    (MS Project) actuals are never overwritten."""
    task = await db.tasks.find_one(
        tenant_filter(tenant_id, {"id": task_id}),
        {"_id": 0, "actual_start": 1, "actual_finish": 1, "actuals_source": 1},
    )
    if not task:
        return {}
    if task.get("actual_start") and task.get("actuals_source") != "daily_report":
        return {}
    rows = await db.daily_report_activities.find(
        {"schedule_activity_id": task_id},
        {"_id": 0, "daily_report_id": 1, "pct_complete_start_of_day": 1,
         "pct_complete_end_of_day": 1, "hours_worked": 1},
    ).to_list(5000)
    dates: dict = {}
    if rows:
        drs = await db.daily_reports.find(
            tenant_filter(tenant_id, {"id": {"$in": list({r["daily_report_id"] for r in rows})}}),
            {"_id": 0, "id": 1, "report_date": 1},
        ).to_list(5000)
        dates = {d["id"]: _d(d.get("report_date")) for d in drs}
    worked, done = [], []
    for r in rows:
        rd = dates.get(r["daily_report_id"])
        if not rd:
            continue
        eod = float(r.get("pct_complete_end_of_day") or 0)
        sod = float(r.get("pct_complete_start_of_day") or 0)
        if eod > sod or float(r.get("hours_worked") or 0) > 0 or eod >= 100:
            worked.append(rd)
        if eod >= 100:
            done.append(rd)
    a_start = min(worked).isoformat() if worked else None
    a_finish = min(done).isoformat() if done else None
    upd = {"actual_start": a_start, "actual_finish": a_finish,
           "actuals_source": "daily_report" if a_start else None}
    if (task.get("actual_start"), task.get("actual_finish")) != (a_start, a_finish):
        await db.tasks.update_one(tenant_filter(tenant_id, {"id": task_id}), {"$set": upd})
    return upd


# ---------------------------------------------------------------- delays
async def open_delays_for_task(db, tenant_id: str, task_id: str) -> list:
    docs = await db.delays.find(
        tenant_filter(tenant_id, {"linked_task_ids": task_id,
                                  "status": {"$in": OPEN_DELAY_STATUSES}}),
        {"_id": 0, "id": 1, "description": 1, "reason_category": 1, "status": 1,
         "date": 1, "logged_date": 1, "impact_start_date": 1, "per_task_impacts": 1,
         "daily_report_id": 1},
    ).to_list(50)
    out = []
    for d in docs:
        days = sum(float(i.get("projected_impact_days") or 0)
                   for i in (d.get("per_task_impacts") or []) if i.get("task_id") == task_id)
        out.append({"id": d["id"], "description": d.get("description"),
                    "reason_category": d.get("reason_category"), "status": d.get("status"),
                    "logged_date": d.get("logged_date") or d.get("date"),
                    "impact_start_date": d.get("impact_start_date"),
                    "daily_report_id": d.get("daily_report_id"),
                    "impact_days": days})
    return out


# ---------------------------------------------------------------- AI helper
async def _ask_ai(system: str, text: str) -> Optional[dict]:
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        return None
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(api_key=key, session_id=f"sched-fb-{gen_id()}",
                       system_message=system).with_model("anthropic", "claude-sonnet-4-5-20250929")
        raw = str(await chat.send_message(UserMessage(text=text))).strip()
        m = re.search(r"[\[{][\s\S]*[\]}]", raw)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:
        log.warning("schedule_feedback AI call failed: %s", exc)
        return None


# ---------------------------------------------------------------- logic proposals
LOGIC_SYSTEM = (
    "You are a federal construction scheduler. You receive one schedule relationship problem "
    "observed from daily reports and propose the minimal, logical relationship change. Output ONE "
    "JSON object only: {\"predecessors\": [{\"code\": str, \"type\": \"FS\"|\"SS\"|\"FF\"|\"SF\", "
    "\"lag_days\": int}], \"rationale\": \"1-3 plain sentences\"}. The predecessors list is the "
    "COMPLETE new predecessor list for the target activity. Only use activity codes given to you."
)


async def _create_proposal(db, tenant_id: str, project_id: str, task: dict, kind: str,
                           trigger: dict, suggested: list, rationale: str) -> Optional[dict]:
    dupe = await db.logic_proposals.find_one(tenant_filter(tenant_id, {
        "task_id": task["id"], "kind": kind, "status": "pending",
        "trigger.predecessor_code": trigger.get("predecessor_code")}), {"_id": 0, "id": 1})
    if dupe:
        return None
    doc = {
        "id": gen_id(), "tenant_id": tenant_id, "project_id": project_id,
        "task_id": task["id"], "activity_code": task.get("activity_code"),
        "activity_name": task.get("display_name") or task.get("description"),
        "kind": kind, "trigger": trigger, "status": "pending",
        "current_predecessors": task.get("predecessors") or [],
        "suggested_predecessors": suggested, "rationale": rationale,
        "ai_status": "pending", "created_at": utc_now().isoformat(),
    }
    await db.logic_proposals.insert_one(doc.copy())
    _spawn(_ai_refine_proposal(db, tenant_id, doc["id"]))
    return doc


async def _ai_refine_proposal(db, tenant_id: str, proposal_id: str) -> None:
    p = await db.logic_proposals.find_one(tenant_filter(tenant_id, {"id": proposal_id}), {"_id": 0})
    if not p:
        return
    nearby = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": p["project_id"], "status": {"$ne": "deleted"}}),
        {"_id": 0, "activity_code": 1, "description": 1, "display_name": 1, "wbs": 1,
         "planned_start": 1, "planned_finish": 1, "pct_complete": 1, "predecessors": 1},
    ).sort("planned_start", 1).to_list(400)
    payload = {"problem": p["kind"], "trigger": p["trigger"],
               "target_activity": p["activity_code"], "target_name": p["activity_name"],
               "current_predecessors": p["current_predecessors"],
               "default_suggestion": p["suggested_predecessors"],
               "schedule": nearby[:300]}
    out = await _ask_ai(LOGIC_SYSTEM, json.dumps(payload, default=str))
    codes = {t["activity_code"] for t in nearby}
    upd = {"ai_status": "failed"}
    if isinstance(out, dict) and isinstance(out.get("predecessors"), list):
        preds = [{"code": str(x.get("code")), "type": (x.get("type") or "FS").upper(),
                  "lag_days": int(x.get("lag_days") or 0)}
                 for x in out["predecessors"]
                 if isinstance(x, dict) and x.get("code") in codes and x.get("code") != p["activity_code"]
                 and (x.get("type") or "FS").upper() in {"FS", "SS", "FF", "SF"}]
        upd = {"ai_status": "done", "suggested_predecessors": preds,
               "rationale": str(out.get("rationale") or p["rationale"])[:800]}
    await db.logic_proposals.update_one(tenant_filter(tenant_id, {"id": proposal_id}), {"$set": upd})


async def detect_out_of_order(db, tenant_id: str, task_id: str, report_date) -> None:
    """Work logged on an activity whose FS predecessor isn't finished."""
    task = await db.tasks.find_one(tenant_filter(tenant_id, {"id": task_id}), {"_id": 0})
    if not task or not task.get("predecessors"):
        return
    codes = [pred_code(p) for p in task["predecessors"] if pred_code(p)]
    preds = {t["activity_code"]: t for t in await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": task["project_id"], "activity_code": {"$in": codes},
                                  "status": {"$ne": "deleted"}}),
        {"_id": 0, "activity_code": 1, "pct_complete": 1, "description": 1, "display_name": 1},
    ).to_list(len(codes) or 1)}
    for p in task["predecessors"]:
        code = pred_code(p)
        rel, lag = pred_relationship(p)
        pt = preds.get(code)
        if rel != "FS" or not pt or float(pt.get("pct_complete") or 0) >= 100:
            continue
        suggested = [dict(x) if isinstance(x, dict) else {"code": x, "type": "FS", "lag_days": 0}
                     for x in task["predecessors"]]
        for x in suggested:
            if x.get("code") == code:
                x["type"], x["lag_days"] = "SS", 0
        await _create_proposal(
            db, tenant_id, task["project_id"], task, "out_of_order",
            {"predecessor_code": code,
             "predecessor_name": pt.get("display_name") or pt.get("description"),
             "predecessor_pct": pt.get("pct_complete"), "report_date": str(report_date)},
            suggested,
            f"Work started on {task.get('activity_code')} on {report_date} while its finish-to-start "
            f"predecessor {code} is only {float(pt.get('pct_complete') or 0):.0f}% complete. "
            "Suggest overlapping them (start-to-start).",
        )


async def propose_for_unplanned(db, tenant_id: str, task: dict, report_date) -> None:
    await _create_proposal(
        db, tenant_id, task["project_id"], task, "unplanned_work",
        {"predecessor_code": None, "report_date": str(report_date)}, [],
        f"Unplanned work '{task.get('description')}' was logged on {report_date}. "
        "AI will suggest where it ties into the schedule.",
    )


async def apply_logic_proposal(db, tenant_id: str, proposal: dict, predecessors: list) -> None:
    await db.tasks.update_one(
        tenant_filter(tenant_id, {"id": proposal["task_id"]}),
        {"$set": {"predecessors": predecessors, "updated_at": utc_now().isoformat()}},
    )


# ---------------------------------------------------------------- names
NAME_SYSTEM = (
    "You name construction schedule activities so a field superintendent can tell them apart. "
    "Input: a JSON list of activities in schedule order with code, description, wbs, headings "
    "(section_path) and the name of the nearest heading-like rows around them. Output ONE JSON "
    "array only: [{\"code\": str, \"name\": str}] for EVERY input activity. Format names as "
    "'Area or phase › Activity' (max 70 chars, use ' › '). Keep the original trade words; add the "
    "phase/area/floor/system that distinguishes duplicates. Never invent scope. NEVER include WBS "
    "numbers, outline numbers or activity codes in a name."
)


_WBS_RE = re.compile(r"\(?\b(?:WBS\s*)?\d+(?:\.\d+)+\b\)?", re.IGNORECASE)


def _clean_name(name: str, code: Optional[str]) -> str:
    """Strip WBS/outline numbers and the activity code from a name."""
    out = _WBS_RE.sub("", name)
    if code:
        out = out.replace(code, "")
    out = re.sub(r"\s*›\s*(›\s*)+", " › ", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ›·-–,")
    return out[:120]


def _window_label(t: dict) -> Optional[str]:
    ps = _d(t.get("planned_start"))
    pf = _d(t.get("planned_finish"))
    if not ps:
        return None
    if pf and pf != ps:
        return f"{ps.strftime('%b')} {ps.day}–{pf.strftime('%b')} {pf.day}" if ps.month != pf.month \
            else f"{ps.strftime('%b')} {ps.day}–{pf.day}"
    return f"{ps.strftime('%b')} {ps.day}"


def heading_name(t: dict) -> Optional[str]:
    path = [s for s in (t.get("section_path") or []) if s]
    if not path:
        return None
    return _clean_name(" › ".join(path[-2:] + [t.get("description") or t.get("activity_code")]),
                       t.get("activity_code")) or None


async def generate_name_suggestions(db, tenant_id: str, project_id: str, *, auto_approve: bool = False) -> dict:
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"},
                                  "work_category": {"$nin": list(PAPERWORK)}}),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1, "wbs": 1,
         "section_path": 1, "display_name": 1, "planned_start": 1, "planned_finish": 1},
    ).to_list(5000)
    tasks.sort(key=lambda t: (str(t.get("wbs") or ""), str(t.get("activity_code") or "")))
    names = {t["id"]: (heading_name(t), "headings") for t in tasks}
    ai_input = [{"code": t["activity_code"], "description": t.get("description"),
                 "wbs": t.get("wbs"), "section_path": t.get("section_path"),
                 "planned_start": t.get("planned_start")} for t in tasks]
    by_code = {t["activity_code"]: t for t in tasks}
    for i in range(0, len(ai_input), 120):
        out = await _ask_ai(NAME_SYSTEM, json.dumps(ai_input[i:i + 120], default=str))
        for item in out if isinstance(out, list) else []:
            t = by_code.get(str(item.get("code"))) if isinstance(item, dict) else None
            if t and item.get("name"):
                clean = _clean_name(str(item["name"]), t.get("activity_code"))
                if clean:
                    names[t["id"]] = (clean, "ai")
    # No AI (or AI failed): tell duplicate names apart by their planned window.
    from collections import Counter
    dup = Counter((t.get("description") or "").strip().lower() for t in tasks)
    for t in tasks:
        if names[t["id"]][0] is None and dup[(t.get("description") or "").strip().lower()] > 1:
            win = _window_label(t)
            if win:
                names[t["id"]] = (f"{t.get('description')} · {win}"[:120], "dates")
    now = utc_now().isoformat()
    await db.activity_name_suggestions.update_many(
        tenant_filter(tenant_id, {"project_id": project_id, "status": "pending"}),
        {"$set": {"status": "superseded"}})
    made = 0
    for t in tasks:
        name, src = names[t["id"]]
        if not name or name == (t.get("display_name") or t.get("description")):
            continue
        doc = {"id": gen_id(), "tenant_id": tenant_id, "project_id": project_id,
               "task_id": t["id"], "activity_code": t["activity_code"],
               "current_name": t.get("display_name") or t.get("description"),
               "suggested_name": name, "source": src,
               "status": "approved" if auto_approve else "pending", "created_at": now}
        await db.activity_name_suggestions.insert_one(doc.copy())
        if auto_approve:
            await db.tasks.update_one(tenant_filter(tenant_id, {"id": t["id"]}),
                                      {"$set": {"display_name": name}})
        made += 1
    return {"suggested": made, "total": len(tasks)}
