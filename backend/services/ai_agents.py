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


async def _master_doc_block(tenant_id: str, project_id: str) -> str:
    """Master prompt §22 — project-type gating. Pulls master_docs row and
    formats a context block that AI agents prepend to every reasoning
    step. New construction vs renovation gets very different guidance."""
    db = get_db()
    md = await db.master_docs.find_one(
        tenant_filter(tenant_id, {"project_id": project_id}), {"_id": 0},
    )
    if not md:
        return ""
    blocks = [f"CONSTRUCTION TYPE: {md.get('construction_type')}"]
    if md.get("construction_type") == "renovation":
        blocks.append("RENOVATION CONTEXT — building is occupied; minimize "
                      "noise/dust during business hours (typ. 7am-6pm M-F). "
                      "Phasing & barriers required. Existing-conditions "
                      "RFIs likely. Asbestos/lead-paint surveys MUST be "
                      "verified before any demo.")
    elif md.get("construction_type") == "tenant_improvement":
        blocks.append("TENANT-IMPROVEMENT CONTEXT — base building is "
                      "operational; coordinate with landlord; lobby/elevator "
                      "use restrictions apply; after-hours work likely "
                      "required for noisy operations.")
    elif md.get("construction_type") == "new_construction":
        blocks.append("NEW-CONSTRUCTION CONTEXT — greenfield/groundbreak; "
                      "weather windows are critical; sequencing is foundation "
                      "→ shell → MEP rough-in → finishes. No existing-"
                      "conditions surprises.")
    elif md.get("construction_type") == "infrastructure":
        blocks.append("INFRASTRUCTURE CONTEXT — civil/heavy work; permitting "
                      "lead times dominate; weather impact is severe; "
                      "ROW & utility-locate are the hard constraints.")
    if md.get("delivery_method"):
        blocks.append(f"DELIVERY METHOD: {md['delivery_method']}")
    if md.get("occupancy_during_work"):
        blocks.append("BUILDING OCCUPIED DURING WORK — life-safety paths "
                      "must remain intact; dust/noise barriers required.")
    if md.get("historic_designation"):
        blocks.append("HISTORIC DESIGNATION — SHPO review on any visible "
                      "alteration; replacement-in-kind preferred.")
    if md.get("abatement_required") or md.get("asbestos_known") or md.get("lead_paint_known"):
        haz = []
        if md.get("asbestos_known"): haz.append("asbestos")
        if md.get("lead_paint_known"): haz.append("lead paint")
        blocks.append(f"HAZMAT KNOWN: {', '.join(haz)} — abatement WBS must "
                      "precede any disturbance.")
    if md.get("govt_security_level") and md["govt_security_level"] != "public":
        blocks.append(f"SECURITY LEVEL: {md['govt_security_level'].upper()} "
                      "— badging + escort required; cleared workforce only.")
    if md.get("site_constraints"):
        blocks.append("SITE CONSTRAINTS: " + "; ".join(md["site_constraints"]))
    if md.get("notes"):
        blocks.append(f"PROJECT NOTES: {md['notes']}")
    return "PROJECT MASTER DOC:\n" + "\n".join(f"  - {b}" for b in blocks)


async def _kb_block(tenant_id: str, project_id: Optional[str], query: str) -> str:
    """RAG over closed RFIs + work items + lessons learned. Master prompt §6."""
    try:
        from services.kb_search import search_kb
        hits = await search_kb(tenant_id, query, limit=3)
        if not hits:
            return ""
        return "RELEVANT PAST RESOLUTIONS (cite by [KB:title]):\n" + "\n".join(
            f"  - [KB:{h.get('title')}] (score={h.get('_score', 0)})\n    {(h.get('body') or '')[:400]}"
            for h in hits
        )
    except Exception:
        return ""


async def copilot_answer(*, tenant_id: str, project_id: Optional[str], session_id: str, message: str) -> dict:
    """Multi-step (was: LangGraph) — node 1 retrieve, node 2 reason."""
    db = get_db()
    # Node 1: retrieval
    context_blocks: list[str] = []
    if project_id:
        # Master-doc context (project-type gating)
        md_block = await _master_doc_block(tenant_id, project_id)
        if md_block:
            context_blocks.append(md_block)
        # Knowledge-base RAG over historical RFIs/work items
        kb_block = await _kb_block(tenant_id, project_id, message)
        if kb_block:
            context_blocks.append(kb_block)
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
        # SPEC RETRIEVAL (TF-IDF) — top 3 sections similar to the user message
        try:
            from services.spec_search import search_all_specs_in_project
            spec_hits = await search_all_specs_in_project(
                tenant_id=tenant_id, project_id=project_id, query=message, top_k=3,
            )
            if spec_hits:
                context_blocks.append("RELEVANT SPEC SECTIONS (cite via <spec_citation>):\n" + "\n".join(
                    f"  [{h['section_code']}] {h['section_title']} (id={h['spec_section_id']}, p.{h['page_start']}, score={h['score']})\n    {h['snippet']}"
                    for h in spec_hits
                ))
        except Exception as e:
            pass
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

    md_block = await _master_doc_block(tenant_id, project_id)
    kb_block = await _kb_block(tenant_id, project_id,
                                f"{target.get('description', '')} recovery slip")
    user_text = (
        (md_block + "\n\n" if md_block else "")
        + (kb_block + "\n\n" if kb_block else "")
        + f"TARGET ACTIVITY [{target['activity_code']}]: {target['description']}\n"
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


# ---------- DELAY REPORT (Phase B) ----------
DELAY_REPORT_SYSTEM = """You are a federal-construction project controls analyst writing
a formal delay analysis report for the project's records. The report MUST follow this
exact structure (use the section headings verbatim, no markdown):

  Summary
  Linked tasks and float analysis
  CPM cascade
  Projected vs actual impact
  Recommendations

Write professional, factual prose. Reference activity codes in brackets, e.g. [A1240].
Where the data shows a task pushes the critical path, say so explicitly. Where the
projected impact is absorbed by float, note the residual float remaining. Keep each
section to 3-6 short sentences. Do NOT use markdown bullets, asterisks, or hashes —
plain text only. Do NOT speculate beyond the data provided."""


async def generate_delay_report(*, tenant_id: str, delay: dict) -> str:
    """Produce a structured delay narrative for a single delay record. Pulls
    the linked tasks (current state) + project + master doc and asks Claude
    for a 5-section report. Returns the report text. Caller persists it on
    the delay record (caller also handles auth + tenant filtering)."""
    db = get_db()
    project_id = delay.get("project_id")
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}), {"_id": 0},
    ) if project_id else None

    md_block = await _master_doc_block(tenant_id, project_id) if project_id else ""

    linked_ids = delay.get("linked_task_ids") or []
    impacts_by_id = {i.get("task_id"): i for i in (delay.get("per_task_impacts") or [])}
    tasks: list[dict] = []
    if linked_ids:
        tasks = await db.tasks.find(
            tenant_filter(tenant_id, {"id": {"$in": linked_ids}}),
            {"_id": 0, "id": 1, "activity_code": 1, "description": 1,
             "pct_complete": 1, "is_critical_path": 1, "total_float_days": 1,
             "planned_start": 1, "planned_finish": 1, "predecessors": 1, "trade_code": 1},
        ).to_list(len(linked_ids))

    # Cascade — for each linked task, walk down its successors so the AI has
    # the same downstream context the recovery agent uses.
    cascade: list[dict] = []
    visited: set[str] = set()
    queue = [t.get("activity_code") for t in tasks if t.get("activity_code")]
    while queue and len(cascade) < 30:
        code = queue.pop(0)
        if not code or code in visited:
            continue
        visited.add(code)
        succs = await db.tasks.find(
            tenant_filter(tenant_id, {"project_id": project_id, "predecessors": code}),
            {"_id": 0, "activity_code": 1, "description": 1,
             "is_critical_path": 1, "planned_finish": 1, "total_float_days": 1},
        ).limit(10).to_list(10)
        for s in succs:
            cascade.append(s)
            if s.get("activity_code"):
                queue.append(s["activity_code"])

    task_lines = []
    for t in tasks:
        impact = impacts_by_id.get(t["id"]) or {}
        task_lines.append(
            f"  [{t.get('activity_code')}] {t.get('description')}\n"
            f"    pct_complete={t.get('pct_complete', 0)}%  "
            f"current_float_days={t.get('total_float_days')}  "
            f"is_critical_path={bool(t.get('is_critical_path'))}\n"
            f"    impact_logged={impact.get('projected_impact_days', 0)}d  "
            f"float_at_logging={impact.get('float_at_logging_days')}d  "
            f"consumes_float_only={impact.get('consumes_float_only', False)}  "
            f"pushes_critical_path={impact.get('pushes_critical_path', False)}"
        )
    cascade_lines = [
        f"  [{c.get('activity_code')}] {c.get('description')} "
        f"(CP={bool(c.get('is_critical_path'))}, float={c.get('total_float_days')}d)"
        for c in cascade[:20]
    ]

    # Bucket C1 Phase 5 — when the delay has been resolved, surface the
    # actual-vs-projected breakdown so the AI report's "Projected vs
    # actual impact" section grounds in real numbers instead of guesses.
    cpm_impact = delay.get("cpm_impact_summary") or []
    released = delay.get("released_float_on_resolution") or []
    resolution_block = ""
    if delay.get("status") == "resolved" and cpm_impact:
        impact_lines = []
        for imp in cpm_impact:
            t = next((tt for tt in tasks if tt["id"] == imp.get("task_id")), {})
            label = f"[{t.get('activity_code')}] {t.get('description')}" if t else imp.get("task_id", "?")[:8]
            impact_lines.append(
                f"  {label}\n"
                f"    projected_impact={imp.get('projected_impact_days')}d  "
                f"actual_days_open={imp.get('actual_days_open')}  "
                f"absorbed={imp.get('absorbed_days')}d  "
                f"restored_float={imp.get('restored_float_days')}d"
            )
        resolution_block = (
            f"\n\nRESOLUTION ANALYSIS (delay was resolved {delay.get('resolved_date')}, "
            f"resolved_note: {delay.get('resolved_note') or 'none'}):\n"
            + "\n".join(impact_lines)
            + (f"\n  TASKS WITH RESTORED FLOAT (no actual schedule consumption): {len(released)}"
               if released else "")
        )

    user_text = (
        (md_block + "\n\n" if md_block else "")
        + (f"PROJECT: {proj['name']} (status={proj.get('status')})\n\n" if proj else "")
        + f"DELAY RECORD\n"
        f"  logged_date={delay.get('logged_date') or delay.get('date')}\n"
        f"  status={delay.get('status')}\n"
        f"  severity={delay.get('severity')}\n"
        f"  reason_category={delay.get('reason_category')}\n"
        f"  description: {delay.get('description')}\n"
        f"  resolved_date={delay.get('resolved_date')}\n"
        f"  duration_days_so_far={delay.get('duration_days')}\n\n"
        f"LINKED TASKS ({len(tasks)}):\n" + ("\n".join(task_lines) or "  (none)")
        + f"\n\nCPM DOWNSTREAM CASCADE ({len(cascade)} reached):\n"
        + ("\n".join(cascade_lines) or "  (none)")
        + resolution_block
        + "\n\nWrite the structured delay analysis report now."
    )
    started = datetime.now(timezone.utc)
    request_id = gen_id()
    try:
        chat = _new_chat(f"delay-report-{delay.get('id') or request_id}", DELAY_REPORT_SYSTEM)
        resp = await chat.send_message(UserMessage(text=user_text))
        ok = True
    except Exception as e:
        resp = f"AI delay report temporarily unavailable: {e}"
        ok = False
    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    await _track_usage(tenant_id, request_id, latency_ms, ok)
    return resp
