"""AI extraction of structured Daily Report fields from a free-text
transcript.

Pipeline:
    transcript (str)  →  Claude Sonnet  →  StructuredDR (Pydantic) → caller

The caller (router) writes the structured DR into MongoDB by reusing the
SAME endpoints / db ops the manual UI uses (PATCH /daily-reports/{id},
POST /daily-reports/{id}/grid, POST /delays, etc). Nothing here writes
to the DB directly -- this module is pure transcript -> JSON.

Why a separate service: keeps router code thin, easy to unit-test,
swap the LLM provider, or test extraction in isolation.

2026-05-25 -- Phase A of the multi-modal voice intake feature. The
transcript may come from any source:
    - Gemini audio transcription (Route 2: async voice)
    - Gemini Live final transcript (Route 3: live conversational)
    - super pasted text (debug / fallback)
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)

# Reuse the existing Anthropic shim (services.anthropic_compat is
# registered into sys.modules at server.py boot under the namespace
# `emergentintegrations.llm.chat`). Going through that import path means
# this code automatically uses the customer's ANTHROPIC_API_KEY.
from emergentintegrations.llm.chat import LlmChat, UserMessage  # noqa: E402

CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
MAX_TRANSCRIPT_CHARS = 30_000  # ~5 min of typical speech -- generous cap


# ---------------------------------------------------------------------
# The system prompt
# ---------------------------------------------------------------------
# This is the contract between Claude and our DR DB. The schema MUST
# match what the manual UI accepts. Keys that don't appear here are
# ignored by the router; keys that DO appear are written through
# normal validation paths so values like severity / category /
# owner_attribution stay in their Pydantic enum boundaries.
_SYSTEM = """You are a construction Daily Report field extractor.

You receive ONE of two transcript formats:

  (A) A free-form voice memo from a site superintendent recapping the
      day's MANPOWER LOG (trades on site), activities, delays, events,
      and notes. Weather is ALREADY pre-populated from the local forecast
      when the DR opens, so the super rarely mentions it; only extract
      weather fields if the super explicitly volunteers a correction
      (e.g. "actually the forecast was wrong, it rained all morning").

  (B) A transcribed back-and-forth conversation between an AI voice
      assistant and the same superintendent, formatted as alternating
      lines:
          Q (AI): <question>
          A (super): <answer>
      The super's answer may be partial ("yes", "skip", "no", or just
      a number). PAIR each answer with the immediately-preceding Q to
      understand what the answer refers to.

Return a SINGLE JSON object with the structure shown below. Fill what
you can confidently extract. **Leave a field as null or omit it if
neither the questions nor the answers actually establish it -- never
invent.** Short answers like "skip" / "no" / "none" mean the super
explicitly skipped that section; do NOT generate empty records for
them. A super saying "moderate" right after the AI asks "severity?"
sets `severity = moderate`, etc.

NOTE ON FIELD NAMES: in the JSON schema below, `crew_presence` is the
LEGACY KEY for what the UI now calls the "Manpower log" -- they are
the same thing. Always emit the key as `crew_presence` for backend
compatibility; never invent a `manpower_log` key.

Required JSON shape (every key optional, use the EXACT types shown):

{
  "weather": {
    "temp_high_f": <number|null>,
    "temp_low_f": <number|null>,
    "conditions": "<one of: clear|cloudy|partly_cloudy|overcast|rain|snow|storm|fog|wind|null>",
    "precipitation_in": <number|null>,
    "wind_mph": <number|null>
  },
  "notes": "<paragraph of free-text incidents / visitors / observations or null>",

  "activity_updates": [
    // ONE entry per scheduled activity the super mentions. Match by
    // activity_code (e.g. "1.7") OR by clearest description match.
    // Skip activities the super did not mention.
    {
      "activity_code": "<e.g. 1.7>",
      "activity_description_hint": "<the words the super used; helps the matcher>",
      "pct_complete_end_of_day": <0-100 or null>,
      "crew_count": <integer or null>,
      "notes": "<super's own words about this activity or null>"
    }
  ],

  "crew_presence": [
    // ONE entry per trade/company on site (this is the "Manpower log"
    // in the UI). Headcount is required when present in transcript;
    // everything else optional. linked_activity_code maps to the
    // "Task" dropdown in the manpower row -- use it when the super
    // tied the crew to a specific scheduled activity.
    {
      "trade_code": "<e.g. ELEC, PLUMB, MECH, CARP>",
      "company_name": "<contractor name or null>",
      "linked_activity_code": "<scheduled activity code the crew worked on, e.g. '1.9', or null>",
      "headcount": <integer or null>,
      "hours_per_person": <number or null>,
      "notes": "<crew-level notes or null>"
    }
  ],

  "delays": [
    {
      "description": "<what happened>",
      "severity": "<one of: minor|moderate|major|critical>",
      "reason_category": "<one of: weather|labor|equipment|materials|coordination|rfi_pending|submittal_pending|owner_change|design_change|inspection_pending|other>",
      "owner_attribution": "<one of: gc|owner|design|sub|third_party|act_of_god|none>",
      "cause_class": "<one of: non_compensable|compensable|concurrent|excusable|inexcusable|other>",
      "status": "<one of: open|investigating|resolved>",
      "linked_activity_codes": ["<list of activity codes this delay affected, e.g. ['1.7','1.8']>"],
      "projected_impact_days": <number or null -- if super said 'X day impact' or 'delays it by X days'>
    }
  ],

  "safety_events": [
    {
      "description": "<what happened>",
      "event_type": "<one of: near_miss|first_aid|recordable|lost_time|fatality|other>",
      "severity": "<one of: minor|moderate|major|critical>",
      "linked_activity_codes": ["<activity codes if mentioned>"]
    }
  ],

  "qc_issues": [
    {
      "description": "<what>",
      "severity": "<one of: minor|moderate|major|critical>",
      "linked_activity_codes": []
    }
  ],

  "deliveries": [
    {
      "description": "<what was delivered>",
      "supplier_name": "<vendor or null>",
      "po_number": "<PO/order number or null>",
      "status": "<one of: scheduled|partial|complete|rejected|on_hold>",
      "linked_activity_codes": []
    }
  ],

  "coordination_events": [
    {
      "description": "<what happened>",
      "with_party": "<who they coordinated with or null>",
      "linked_activity_codes": []
    }
  ]
}

CRITICAL RULES:
- Output VALID JSON only. No markdown fences, no commentary.
- Every enum value (severity, category, status, etc) MUST be lowercase
  and EXACTLY match one of the options listed. If you can't classify
  confidently, pick the safest default per field:
    severity -> "minor"
    reason_category -> "other"
    owner_attribution -> "none"
    cause_class -> "other"
    status -> "open" for delays, "scheduled" for deliveries
  But only emit the event AT ALL if the super clearly described it.
- activity_code matches: look for spoken patterns like "one point seven",
  "task seven", "1.7", "1-7". Normalize to "1.7" format.
- If transcript is empty or just chatter / non-DR content, return:
    {"notes": null}
"""


def _clip_transcript(text: str) -> str:
    """Trim very long transcripts to MAX_TRANSCRIPT_CHARS, keeping the
    most recent content (supers tend to wrap-up summarize at the end,
    so the tail is the most field-dense)."""
    if not text:
        return ""
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    return "...[transcript truncated]...\n" + text[-MAX_TRANSCRIPT_CHARS:]


async def extract_dr_fields(
    transcript: str,
    *,
    activity_codes_in_dr: Optional[list[str]] = None,
    activities_context: Optional[list[dict]] = None,
    project_name: Optional[str] = None,
    report_date_iso: Optional[str] = None,
) -> dict[str, Any]:
    """Run the transcript through Claude and return the parsed JSON dict.

    Parameters
    ----------
    transcript : raw speech-to-text output (already cleaned by the
        upstream Gemini transcriber, or pasted by user).
    activity_codes_in_dr : OPTIONAL list of activity codes currently on
        the DR grid. When present, Claude is told to match the super's
        spoken hints against this list (improves activity_code accuracy).
    activities_context : OPTIONAL list of {code, name, trade} for each
        activity on the grid -- enables fuzzy description matching so
        the super can say "electrical rough in" and Claude maps it to
        the right activity_code without the super reciting the code.
    project_name, report_date_iso : OPTIONAL context Claude can use to
        disambiguate references like "yesterday" or to skip a project
        name the super mentioned in passing.

    Returns
    -------
    dict matching the schema in the system prompt. May be {} or
    {"notes": null} if the transcript was empty / non-DR.

    Raises
    ------
    RuntimeError on Claude failure (auth, rate-limit, parse error).
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return {}

    clipped = _clip_transcript(transcript)

    # Build the per-call context block. Putting it on the USER message
    # keeps the system prompt static (cheaper + cacheable on Anthropic's
    # side once they ship prompt caching for our tier).
    ctx_lines: list[str] = []
    if project_name:
        ctx_lines.append(f"Project: {project_name}")
    if report_date_iso:
        ctx_lines.append(f"Report date: {report_date_iso}")
    if activities_context:
        # Send the FULL grid (up to first 60 rows) with code+name+trade
        # so Claude can map spoken descriptions to activity codes.
        rows = []
        for a in activities_context[:60]:
            line = f"  - {a.get('code')}"
            if a.get("name"):
                line += f"  {a['name']}"
            if a.get("trade"):
                line += f"  [{a['trade']}]"
            rows.append(line)
        ctx_lines.append(
            "Activities on this DR grid (super may refer to these by code OR by name):\n"
            + "\n".join(rows)
            + "\nWhen the super describes an activity in their own words, "
            "MATCH it to the closest activity_code from this list -- e.g. "
            "spoken 'electrical rough in' should match an activity with "
            "'electrical' in the name."
        )
    elif activity_codes_in_dr:
        codes_str = ", ".join(activity_codes_in_dr[:60])
        ctx_lines.append(
            f"Activity codes present on this DR grid: {codes_str}. "
            "Match spoken activity references against these first."
        )
    ctx_block = "\n".join(ctx_lines)
    user_msg = (
        (f"{ctx_block}\n\n" if ctx_block else "")
        + "TRANSCRIPT:\n"
        + clipped
        + "\n\nReturn the JSON object now."
    )

    chat = LlmChat(
        api_key="",   # ignored by the shim
        session_id=f"dr-extract-{uuid.uuid4().hex[:8]}",
        system_message=_SYSTEM,
    ).with_model("anthropic", CLAUDE_MODEL)

    try:
        raw = await chat.send_message(UserMessage(text=user_msg))
    except Exception as exc:
        log.error("dr_extract: Claude call failed: %s", exc)
        raise RuntimeError(f"AI extraction failed: {exc}") from exc

    raw = (raw or "").strip()
    if not raw:
        return {}

    # Strip ```json fences just in case Claude regresses to adding them.
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("dr_extract: invalid JSON from Claude: %s\nRaw: %s",
                  exc, raw[:500])
        raise RuntimeError(
            f"AI returned invalid JSON: {exc}; raw start={raw[:200]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"AI returned non-object JSON (got {type(parsed).__name__})"
        )

    return parsed
