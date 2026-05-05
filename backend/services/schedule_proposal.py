"""Single source of truth for proposed schedule payloads.

Master prompt §3 + §20.1:
> When two API paths need to build the same payload (e.g., synchronous import
> path and LangGraph agent path both needing parsed_to_task_create_payload),
> share one helper module. Do not duplicate.

Both `routers/imports.py` and `services/ai_agents.py::ingest_schedule_agent`
MUST import from this module. Drift between them caused real production bugs
in the Railway codebase.

CRITICAL:
- Do NOT include 'prev_pct' in this payload. TaskCreate is a StrictModel
  (extra='forbid') and 'prev_pct' is set post-creation by the applier.
- Use parsed.activity_code as the primary ref. Predecessors reference
  activity_code, NOT hierarchical wbs.
"""
from typing import Any
from models import TaskCreate


def parsed_to_task_create_payload(parsed: dict, project_id: str) -> dict:
    """Convert a parsed schedule row into a TaskCreate-compatible payload.

    `parsed` is the dict produced by schedule_parser.py. It must contain at
    least activity_code and description.
    """
    payload = {
        "project_id": project_id,
        "activity_code": str(parsed["activity_code"]),
        "description": str(parsed["description"]),
        "wbs": parsed.get("wbs"),
        "trade_code": parsed.get("trade_code"),
        "planned_start": parsed.get("planned_start"),
        "planned_finish": parsed.get("planned_finish"),
        "duration_days": parsed.get("duration_days"),
        "is_milestone": bool(parsed.get("is_milestone", False)),
        "predecessors": list(parsed.get("predecessors", [])),
        "resource_names": list(parsed.get("resource_names", [])),
        "source": parsed.get("source", "master_schedule"),
    }
    # Drop None values so StrictModel validation doesn't trip on Optional fields
    # that don't accept None at construction time. BUT keep keys that exist
    # in TaskCreate (the model declares Optional with default None, so this
    # is mostly cosmetic — kept for parity with Railway codebase).
    return {k: v for k, v in payload.items() if v is not None or k == "predecessors"}


def validate_payload(payload: dict) -> TaskCreate:
    """Pin the regression test path: TaskCreate must accept this payload
    cleanly. extra='forbid' will raise ValidationError if 'prev_pct' or any
    extra field slips in (this is the bug we're guarding against)."""
    return TaskCreate.model_validate(payload)
