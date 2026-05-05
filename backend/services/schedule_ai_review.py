"""Claude review of a schedule diff.

Returns a structured JSON dict the proposal page can render. NEVER returns
markdown. The PM uses this as a quick sanity-check before approving.
"""
from __future__ import annotations
import json
import os
import re
from typing import Optional
from emergentintegrations.llm.chat import LlmChat, UserMessage

MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"

SYSTEM = (
    "You review changes to a federal-construction project schedule (NIST/GSA/USACE/USMS). "
    "You receive a structured diff produced by our parser comparing the new upload to the "
    "currently-saved schedule. You output a SINGLE JSON object — no markdown, no code "
    "fences, no surrounding prose. Plain English in field values, no special characters "
    "like # or * or backticks. Schema:\n"
    "{\n"
    "  \"risk_level\": \"low\" | \"medium\" | \"high\",\n"
    "  \"summary\": \"2-4 sentence plain-English summary of the change.\",\n"
    "  \"flags\": [\"short bullet 1\", \"short bullet 2\", ...],\n"
    "  \"recommendation\": \"approve\" | \"approve_with_caution\" | \"reject\",\n"
    "  \"justification_required\": true | false\n"
    "}\n"
    "Rules:\n"
    "- Mark risk_level=high if any of: total dropped activities >= 5, max_shift_days >= 14, "
    "  >25% of shared activities have date shifts, or any milestone-looking activity (description "
    "  contains 'milestone' or 'substantial completion' or 'final inspection') is dropped or "
    "  shifted by more than 7 days.\n"
    "- Mark risk_level=medium if max_shift_days between 7 and 14, or shifts touch >10% of activities.\n"
    "- Otherwise low.\n"
    "- justification_required=true if risk_level is high.\n"
    "- flags should call out specific concerning items by activity_code (e.g., 'A0123 finish moved 21 days later').\n"
    "- Keep flags <= 6 items, each <= 80 characters.\n"
    "- Never include markdown characters.\n"
)


def _strip_markdown(s: str) -> str:
    """Defense-in-depth — strip backticks/asterisks/hashes if model slips."""
    return re.sub(r"[`*#]+", "", s or "").strip()


async def review_schedule_diff(diff: dict, project_name: str = "") -> dict:
    """Returns the structured AI review dict. Falls back to a rules-only
    review if the LLM call fails (so the proposal flow never blocks)."""
    payload = {
        "project_name": project_name,
        "summary": diff.get("summary", {}),
        # Truncate detail lists so we don't blow the context for huge schedules.
        "added_sample": diff.get("added", [])[:25],
        "dropped_sample": diff.get("dropped", [])[:25],
        "date_shifts_sample": sorted(
            diff.get("date_shifts", []),
            key=lambda r: abs(r.get("biggest_shift_days") or 0),
            reverse=True,
        )[:25],
        "duration_changes_sample": diff.get("duration_changes", [])[:15],
        "predecessor_changes_count": len(diff.get("predecessor_changes", [])),
    }

    api_key = os.environ.get("EMERGENT_LLM_KEY", "")
    if not api_key:
        return _heuristic_review(diff)

    chat = LlmChat(
        api_key=api_key,
        session_id=f"sched-review-{int(__import__('time').time())}",
        system_message=SYSTEM,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    user_text = "Schedule diff for review:\n" + json.dumps(payload, default=str)
    try:
        raw = await chat.send_message(UserMessage(text=user_text))
    except Exception as e:
        return {**_heuristic_review(diff), "ai_error": f"LLM unavailable: {e}"[:240]}

    raw = str(raw).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE)
    try:
        out = json.loads(cleaned)
    except Exception:
        m = re.search(r"\{[\s\S]+\}", cleaned)
        if not m:
            return _heuristic_review(diff)
        try:
            out = json.loads(m.group(0))
        except Exception:
            return _heuristic_review(diff)

    risk = (out.get("risk_level") or "").lower()
    if risk not in {"low", "medium", "high"}:
        risk = _heuristic_risk(diff)
    rec = (out.get("recommendation") or "").lower()
    if rec not in {"approve", "approve_with_caution", "reject"}:
        rec = "approve_with_caution" if risk in {"medium", "high"} else "approve"
    flags = out.get("flags") or []
    if not isinstance(flags, list):
        flags = [str(flags)]
    flags = [_strip_markdown(str(f))[:120] for f in flags][:6]
    return {
        "risk_level": risk,
        "summary": _strip_markdown(out.get("summary") or "")[:600] or
                   "Schedule review completed.",
        "flags": flags,
        "recommendation": rec,
        "justification_required": bool(out.get("justification_required", risk == "high")),
        "model": MODEL_NAME,
    }


def _heuristic_risk(diff: dict) -> str:
    s = diff.get("summary", {})
    if (s.get("dropped_count", 0) >= 5 or s.get("max_shift_days", 0) >= 14):
        return "high"
    if s.get("max_shift_days", 0) >= 7:
        return "medium"
    return "low"


def _heuristic_review(diff: dict) -> dict:
    s = diff.get("summary", {})
    risk = _heuristic_risk(diff)
    flags: list[str] = []
    if s.get("dropped_count"):
        flags.append(f"{s['dropped_count']} activity(ies) dropped from schedule")
    if s.get("added_count"):
        flags.append(f"{s['added_count']} new activity(ies) added")
    if s.get("max_shift_days"):
        flags.append(f"max date shift {s['max_shift_days']} day(s)")
    if s.get("duration_changed_count"):
        flags.append(f"{s['duration_changed_count']} duration change(s)")
    return {
        "risk_level": risk,
        "summary": (
            f"Heuristic review (LLM unavailable). {s.get('added_count', 0)} added, "
            f"{s.get('dropped_count', 0)} dropped, {s.get('shifted_count', 0)} shifted, "
            f"max shift {s.get('max_shift_days', 0)} days."
        ),
        "flags": flags[:6],
        "recommendation": "approve_with_caution" if risk in {"medium", "high"} else "approve",
        "justification_required": risk == "high",
        "model": "heuristic",
    }
