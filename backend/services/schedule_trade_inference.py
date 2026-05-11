"""Bucket G Phase 2 (2026-05-11) -- rule-based schedule trade inference.

Rule-based (not AI) producer that scans newly-inserted master schedule
tasks for trade-keyword matches in their description / activity_code
and creates Jigsaw FieldSuggestion rows for required_trade +
required_headcount on each match. PMs confirm in bulk via the
bulk-confirm endpoint.

Why rule-based and not AI?
--------------------------
- Schedule import is bulk (thousands of tasks). Calling an LLM per row
  is slow + expensive.
- Construction trade keywords are mostly stable English; pattern
  matching at confidence 0.7 is good enough to pre-fill, leaving the
  human in the loop for the final word.
- Rule-based output is deterministic + cheap to test.

Confidence
----------
Fixed at 0.7 for every match. Lower than AI Vision (which can read
context) on purpose -- pattern matching is a guess that the human
needs to confirm. 0.7 puts the badge in the "Confident" tone in the
JigsawField widget (>= 0.70 sky blue).

Authorized confirmers
---------------------
schedule_activity entity type -> ["pm", "admin", "pe"] (PE =
scheduling engineer). The default per FIELD_SUGGESTION_DEFAULT_ROLES
already covers this; no override needed.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import get_db, tenant_filter
from services import field_suggestions as fs

log = logging.getLogger("schedule_trade_inference")


# Trade -> default headcount lookup.  Headcount is a starting guess
# tuned to typical crew compositions on federal-construction sites.
# PM adjusts in the JigsawField widget if their project staffs
# differently.
_TRADE_DEFAULTS: dict[str, int] = {
    "Plumber":         2,
    "Electrician":     3,
    "Carpenter":       4,
    "Concrete":        5,
    "Ironworker":      3,
    "HVAC":            2,
    "Laborer":         4,
    "Painter":         3,
    "Drywaller":       4,
    "Roofer":          3,
    "Mason":           3,
    "Glazier":         2,
    "Sprinkler":       2,
    "Tile setter":     2,
    "Flooring":        3,
    "Insulator":       2,
    "Sheet metal":     2,
    "Surveyor":        1,
    "Operator":        2,
    "Crane operator":  1,
    "Welder":          2,
}

# (regex-free) keyword groups -> trade. Substring match against
# description.lower() OR activity_code.lower(). Order matters: first
# match wins, so more specific patterns come before generic.
_KEYWORD_GROUPS: list[tuple[tuple[str, ...], str]] = [
    # Plumbing
    (("plumb", "domestic water", "san waste",
      "waste line", "vent stack", "fixture"),       "Plumber"),
    # Electrical
    (("electric", "conduit", "panelboard", "switchgear",
      "branch circuit", "feeders", "lighting fixture",
      "receptacle"),                                "Electrician"),
    # HVAC / mechanical (before generic carpenter/laborer)
    (("hvac", "ductwork", "vav", "rtu", "rooftop",
      "ahu ", "air handler", "chiller", "mechanical"), "HVAC"),
    # Concrete
    (("concrete", "slab on grade", "footings",
      "foundation pour", "rebar tie", "form work",
      "formwork"),                                  "Concrete"),
    # Structural steel
    (("steel erect", "steel beam", "structural steel",
      "deck pour", "metal deck", "joist", "miscellaneous metals",
      "stairs"),                                    "Ironworker"),
    # Welding (specific)
    (("weld",),                                     "Welder"),
    # Roofing
    (("roof", "membrane", "tpo", "epdm",
      "single-ply"),                                "Roofer"),
    # Masonry
    (("masonry", "cmu ", "block wall", "brick",
      "veneer"),                                    "Mason"),
    # Drywall / framing
    (("drywall", "gypsum board", "gwb", "metal stud",
      "stud wall", "framing"),                      "Carpenter"),
    # Tile
    (("tile setter", "tile install", "ceramic tile",
      "porcelain tile"),                            "Tile setter"),
    # Flooring
    (("flooring", "vinyl plank", "lvt ", "vct ",
      "carpet", "wood floor"),                      "Flooring"),
    # Painting
    (("paint", "primer", "finish coat"),            "Painter"),
    # Insulation
    (("insulation", "batt ", "rigid insulation",
      "spray foam"),                                "Insulator"),
    # Glazing
    (("glazing", "storefront", "curtain wall",
      "window install"),                            "Glazier"),
    # Fire protection
    (("sprinkler", "fire protection"),              "Sprinkler"),
    # Sheet metal
    (("sheet metal", "flashings", "gutters",
      "downspout"),                                 "Sheet metal"),
    # Demo / earthwork / site
    (("demolition", "demo ", "tear out",
      "selective demo"),                            "Laborer"),
    (("earthwork", "excavation", "backfill",
      "grading"),                                   "Operator"),
    # Survey
    (("survey", "layout", "establishing benchmark"), "Surveyor"),
    # Crane / hoisting
    (("crane pick", "crane lift", "tower crane"),   "Crane operator"),
]

# Confidence baked in for the rule-based producer.
RULE_CONFIDENCE = 0.7


def _match_trade(activity_code: str, description: str) -> Optional[str]:
    """Returns the inferred trade name, or None on no match. Compared
    against the lowercased concatenation of activity_code + description
    so multi-word patterns (e.g. "slab on grade") match correctly."""
    haystack = f"{(activity_code or '').lower()} {(description or '').lower()}"
    if not haystack.strip():
        return None
    for keywords, trade in _KEYWORD_GROUPS:
        for k in keywords:
            if k in haystack:
                return trade
    return None


async def infer_trade_for_task(
    *, tenant_id: str, project_id: str, task: dict,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Per-task inference + suggestion emission. Returns a result
    dict the caller can roll up: {trade, headcount, created_suggestion}.

    Skip conditions (returns ok=True, skipped=True):
      - Task already has required_trade set (don't re-suggest).
      - Activity code + description both empty (nothing to match).
      - No keyword match (rule-set didn't catch it; that's fine).
    """
    if not task or not task.get("id"):
        return {"ok": False, "reason": "task_missing_id"}
    if task.get("required_trade"):
        return {"ok": True, "skipped": True,
                "reason": "task_already_has_trade"}
    trade = _match_trade(
        task.get("activity_code") or "", task.get("description") or "",
    )
    if not trade:
        return {"ok": True, "skipped": True,
                "reason": "no_keyword_match"}
    headcount = _TRADE_DEFAULTS.get(trade, 2)

    # Two suggestions per matched task: one for required_trade, one
    # for required_headcount. They're separate field_path rows so
    # the JigsawField widget can light up each input independently.
    result = {"ok": True, "trade": trade, "headcount": headcount,
              "task_id": task["id"], "created": []}
    try:
        r1 = await fs.create_suggestion(
            tenant_id=tenant_id, project_id=project_id,
            entity_type="schedule_activity",
            entity_id=task["id"],
            field_path="required_trade",
            suggested_value=trade,
            suggested_by="ai_schedule_analyzer",
            confidence_score=RULE_CONFIDENCE,
            source_context=(
                f"rule_match: activity_code={task.get('activity_code')}; "
                f"description={(task.get('description') or '')[:80]}"
            ),
            actor_id=actor_id, request_id=request_id,
        )
        if r1.get("ok") and not r1.get("_dedup"):
            result["created"].append("required_trade")
        r2 = await fs.create_suggestion(
            tenant_id=tenant_id, project_id=project_id,
            entity_type="schedule_activity",
            entity_id=task["id"],
            field_path="required_headcount",
            suggested_value=headcount,
            suggested_by="ai_schedule_analyzer",
            confidence_score=RULE_CONFIDENCE,
            source_context=f"default_headcount_for_{trade.lower()}",
            actor_id=actor_id, request_id=request_id,
        )
        if r2.get("ok") and not r2.get("_dedup"):
            result["created"].append("required_headcount")
    except Exception as exc:
        log.warning("schedule_trade_inference task %s failed: %s",
                    task.get("id"), exc)
        result["ok"] = False
        result["reason"] = f"producer_error: {type(exc).__name__}"
    return result


async def infer_trades_for_project(
    *, tenant_id: str, project_id: str,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
    limit: int = 5000,
) -> dict:
    """Bulk pass over a project's tasks. Used right after schedule
    import / proposal apply -- walks every task with no required_trade
    and emits suggestions. Idempotent: dedup logic in
    create_suggestion skips re-running on already-pending or already-
    confirmed rows.
    """
    db = get_db()
    cursor = db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$ne": "deleted"},
            "$or": [
                {"required_trade": {"$exists": False}},
                {"required_trade": None},
                {"required_trade": ""},
            ],
        }),
        {"_id": 0, "id": 1, "activity_code": 1, "description": 1,
         "required_trade": 1},
    ).limit(max(1, min(int(limit), 10000)))

    out = {"examined": 0, "matched": 0, "skipped_no_match": 0,
           "skipped_existing_trade": 0, "failures": 0,
           "trades_suggested": 0, "headcounts_suggested": 0}
    async for task in cursor:
        out["examined"] += 1
        r = await infer_trade_for_task(
            tenant_id=tenant_id, project_id=project_id, task=task,
            actor_id=actor_id, request_id=request_id,
        )
        if not r.get("ok"):
            out["failures"] += 1
            continue
        if r.get("skipped"):
            if r.get("reason") == "task_already_has_trade":
                out["skipped_existing_trade"] += 1
            else:
                out["skipped_no_match"] += 1
            continue
        out["matched"] += 1
        created = r.get("created") or []
        if "required_trade" in created:
            out["trades_suggested"] += 1
        if "required_headcount" in created:
            out["headcounts_suggested"] += 1
    return out
