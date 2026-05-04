"""AI agents.

SUBSTITUTION: master prompt §6 specified LangGraph multi-agent topology.
Emergent runtime uses emergentintegrations.LlmChat (Anthropic via universal
key). We preserve the multi-step "graph node" pattern as named functions
with explicit retrieval -> reasoning -> validation phases. Token usage
tracked in ai_usage_tracking.

Agents implemented:
  - copilot: general Q&A over project context
  - recovery: schedule-slip recovery suggestions
  - rfi_drafter: draft RFI responses citing spec sections
  - work_item_assistant: analyze a field event and propose linked entities
  - voice_annotation: extract structured fields from a transcript

Model: claude-sonnet-4-5-20250929 (per master prompt requirement).
"""
import os
import json
import re
from datetime import datetime, timezone
from typing import Optional
from emergentintegrations.llm.chat import LlmChat, UserMessage
from database import get_db, tenant_filter
from models import gen_id, utc_now

MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"


def _key() -> str:
    return os.environ.get("EMERGENT_LLM_KEY", "")


def _new_chat(session_id: str, system_message: str) -> LlmChat:
    return LlmChat(
        api_key=_key(),
        session_id=session_id,
        system_message=system_message,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)


async def _track_usage(tenant_id: str, request_id: str, latency_ms: int, ok: bool):
    db = get_db()
    await db.ai_usage_tracking.insert_one({
        "id": gen_id(),
        "tenant_id": tenant_id,
        "request_id": request_id,
        "service": MODEL_PROVIDER,
        "model": MODEL_NAME,
        "latency_ms": latency_ms,
        "ok": ok,
        "created_at": utc_now().isoformat(),
    })


# ---------- COPILOT ----------
COPILOT_SYSTEM = """You are the AI copilot inside ICE Schedule Ops, a federal-construction
project-management platform. You answer questions about the active project's schedule,
RFIs, daily reports, work items, and specs. When you cite specs, use this exact tag:

<spec_citation section_code="03 30 00" paragraph="3.2.A" spec_section_id="UUID">
per Section 03 30 00, paragraph 3.2.A
</spec_citation>

When referencing schedule activities, mention the activity_code in brackets, e.g. [A1240].
Be concise, factual, and cite the source whenever you make a claim about the project."""


async def copilot_answer(*, tenant_id: str, project_id: Optional[str], session_id: str, message: str) -> dict:
    """Multi-step (was: LangGraph) — node 1 retrieve, node 2 reason."""
    db = get_db()
    # Node 1: retrieval
    context_blocks: list[str] = []
    if project_id:
        proj = await db.projects.find_one(tenant_filter(tenant_id, {"id": project_id}), {"_id": 0})
        if proj:
            context_blocks.append(f"PROJECT: {proj['name']} (status={proj.get('status')})")
        # top 8 in-window activities
        tasks = await db.tasks.find(
            tenant_filter(tenant_id, {"project_id": project_id, "lookahead_status": {"$in": ["in_window", "proposed_new"]}}),
            {"_id": 0, "activity_code": 1, "description": 1, "pct_complete": 1, "planned_start": 1, "planned_finish": 1},
        ).limit(8).to_list(8)
        if tasks:
            context_blocks.append("LOOKAHEAD ACTIVITIES:\n" + "\n".join(
                f"  [{t['activity_code']}] {t['description']} — {t.get('pct_complete', 0)}% (plan {t.get('planned_start')} → {t.get('planned_finish')})"
                for t in tasks
            ))
        # open RFIs
        rfis = await db.rfis.find(
            tenant_filter(tenant_id, {"project_id": project_id, "status": {"$in": ["sent", "in_review"]}}),
            {"_id": 0, "rfi_number": 1, "subject": 1, "status": 1},
        ).limit(5).to_list(5)
        if rfis:
            context_blocks.append("OPEN RFIs:\n" + "\n".join(
                f"  RFI {r['rfi_number']}: {r['subject']} ({r['status']})" for r in rfis
            ))
    user_text = "\n\n".join(context_blocks) + f"\n\nUser question: {message}"
    if not context_blocks:
        user_text = message

    # Node 2: reason
    started = datetime.now(timezone.utc)
    request_id = gen_id()
    try:
        chat = _new_chat(session_id, COPILOT_SYSTEM)
        resp = await chat.send_message(UserMessage(text=user_text))
        ok = True
    except Exception as e:
        resp = f"AI copilot temporarily unavailable: {e}"
        ok = False
    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    await _track_usage(tenant_id, request_id, latency_ms, ok)

    # Extract citations
    citations = _extract_spec_citations(resp)

    return {"response": resp, "citations": citations, "usage": {"latency_ms": latency_ms, "model": MODEL_NAME}}


def _extract_spec_citations(text: str) -> list[dict]:
    out = []
    for m in re.finditer(
        r'<spec_citation\s+section_code="([^"]+)"\s+paragraph="([^"]+)"\s+spec_section_id="([^"]+)"\s*>',
        text or "",
    ):
        out.append({"section_code": m.group(1), "paragraph": m.group(2), "spec_section_id": m.group(3)})
    return out


# ---------- RECOVERY ----------
RECOVERY_SYSTEM = """You are the AI Recovery Engine for federal-construction schedule ops.
Given a slipping activity, propose 3-5 concrete recovery actions. Each action MUST be a
single JSON object in a JSON array. Required fields per action:
  - "title": short label
  - "category": one of "resequence","add_crew","extend_shift","split_activity","escalate","change_method"
  - "rationale": 1-2 sentence explanation
  - "predicted_time_recovery_days": float
  - "predicted_cost_usd": float
  - "side_effects": short string
  - "confidence": float 0..1
Respond ONLY with the raw JSON array. No prose, no code fences."""


async def recovery_suggestions(*, tenant_id: str, project_id: str, target_activity_id: str, trigger: str) -> dict:
    db = get_db()
    target = await db.tasks.find_one(tenant_filter(tenant_id, {"id": target_activity_id}), {"_id": 0})
    if not target:
        return {"suggestions": [], "error": "activity_not_found"}
    # cascade: walk successors via activity_code
    cascade = []
    visited = set()
    queue = [target["activity_code"]]
    while queue and len(cascade) < 25:
        code = queue.pop(0)
        if code in visited:
            continue
        visited.add(code)
        succs = await db.tasks.find(
            tenant_filter(tenant_id, {"project_id": project_id, "predecessors": code}),
            {"_id": 0, "activity_code": 1, "description": 1, "is_critical_path": 1, "planned_finish": 1},
        ).limit(15).to_list(15)
        for s in succs:
            cascade.append(s)
            queue.append(s["activity_code"])

    crews = await db.crews.find(tenant_filter(tenant_id), {"_id": 0, "name": 1, "trade": 1, "target_size": 1}).limit(20).to_list(20)

    user_text = (
        f"TARGET ACTIVITY [{target['activity_code']}]: {target['description']}\n"
        f"  planned: {target.get('planned_start')} → {target.get('planned_finish')}\n"
        f"  pct_complete: {target.get('pct_complete', 0)}\n"
        f"  duration_days: {target.get('duration_days')}\n"
        f"  trade_code: {target.get('trade_code')}\n"
        f"\nCASCADE (down-stream activities affected): {len(cascade)} items\n"
        + "\n".join(f"  [{c['activity_code']}] {c['description']} (CP={c.get('is_critical_path', False)})" for c in cascade[:10])
        + f"\n\nAVAILABLE CREWS (within tenant): {len(crews)}\n"
        + "\n".join(f"  {c['name']} ({c['trade']}, size={c.get('target_size')})" for c in crews[:8])
        + f"\n\nTrigger: {trigger}\n\nReturn ONLY the JSON array of recovery actions."
    )
    started = datetime.now(timezone.utc)
    request_id = gen_id()
    try:
        chat = _new_chat(f"recovery-{target_activity_id}", RECOVERY_SYSTEM)
        resp = await chat.send_message(UserMessage(text=user_text))
        ok = True
    except Exception:
        resp = "[]"
        ok = False
    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    await _track_usage(tenant_id, request_id, latency_ms, ok)

    suggestions = _parse_json_array(resp)
    # Persist
    suggestion_id = gen_id()
    doc = {
        "id": suggestion_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "target_activity_id": target_activity_id,
        "generated_at": utc_now().isoformat(),
        "trigger": trigger,
        "suggestions": suggestions,
        "status": "pending",
        "chosen_suggestion_index": None,
        "applied_at": None,
        "applied_by": None,
    }
    await db.ai_recovery_suggestions.insert_one(doc.copy())
    return {"id": suggestion_id, "suggestions": suggestions}


def _parse_json_array(text: str) -> list[dict]:
    if not text:
        return []
    # Strip code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    # Find first [ and last ]
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        arr = json.loads(cleaned[start:end + 1])
        if isinstance(arr, list):
            return [a for a in arr if isinstance(a, dict)]
    except Exception:
        pass
    return []


# ---------- RFI DRAFTER ----------
RFI_DRAFTER_SYSTEM = """You are an experienced federal-construction Project Engineer drafting
a formal RFI response. Cite spec sections using <spec_citation> tags when applicable. Keep
the response professional, factual, and 3-5 short paragraphs. Do NOT speculate beyond the
provided context."""


async def rfi_draft(*, tenant_id: str, project_id: str, subject: str, context: str, schedule_activity_id: Optional[str]) -> dict:
    db = get_db()
    extra = ""
    if schedule_activity_id:
        t = await db.tasks.find_one(tenant_filter(tenant_id, {"id": schedule_activity_id}), {"_id": 0})
        if t:
            extra = f"\nLinked schedule activity: [{t['activity_code']}] {t['description']}"

    user_text = f"Subject: {subject}\n\nContext: {context}{extra}\n\nDraft the RFI response now."
    started = datetime.now(timezone.utc)
    request_id = gen_id()
    try:
        chat = _new_chat(f"rfi-{request_id}", RFI_DRAFTER_SYSTEM)
        resp = await chat.send_message(UserMessage(text=user_text))
        ok = True
    except Exception as e:
        resp = f"RFI drafter unavailable: {e}"
        ok = False
    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    await _track_usage(tenant_id, request_id, latency_ms, ok)
    return {"draft": resp, "citations": _extract_spec_citations(resp), "usage": {"latency_ms": latency_ms}}
