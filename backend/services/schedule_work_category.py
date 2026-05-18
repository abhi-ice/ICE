"""Feature F1 (2026-05-17) -- AI work-category tagging for schedule import.

After the schedule parser produces the parsed task list and BEFORE the
schedule-diff review is shown, every task is classified onto a single
work-category axis (exactly ONE tag per task, no overlap):

  preconstruction_paperwork -- paperwork BEFORE mobilization & site
      setup (early submittals, procurement, approvals, long-lead).
  construction_paperwork    -- paperwork DURING construction (a
      submittal/procurement/approval task that must clear before a
      field-work task).
  field_work                -- physical construction by field crews
      (demo, framing, rough-in, finishes, etc.).
  closeout_paperwork        -- paperwork AFTER substantial completion
      (testing/commissioning docs, punch list, as-builts, O&M).

The tag is PURE METADATA. It never feeds CPM, float, the cascade,
critical-path, dates, or the Tasks page -- only the schedule-diff
review surfaces it (with a reviewer override + an AI re-review
affordance).

ROBUSTNESS: the import must never break on the AI step.
  * No API key / LLM error / unparseable response -> EVERY task
    defaults to field_work and is flagged for review (ai_error set).
  * A task the model omits or mis-classifies -> that one task
    defaults to field_work and is flagged for review.

LLM client: the app's existing Emergent gateway (emergentintegrations
.llm.chat.LlmChat) -- the same provider services/schedule_ai_review.py
uses. No new provider. The import is LAZY (inside _classify_via_llm)
so this module loads even where the package is absent (e.g. unit
tests of the pure helpers + the no-key fallback path).
"""
from __future__ import annotations
import json
import os
import re
from typing import Optional

MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"

# The 4 valid tags (single axis). field_work is the safe default for
# any task that cannot be classified.
WORK_CATEGORIES = frozenset({
    "preconstruction_paperwork",
    "construction_paperwork",
    "field_work",
    "closeout_paperwork",
})
DEFAULT_CATEGORY = "field_work"

SYSTEM = """\
You classify the activities of a construction-project schedule by WORK
CATEGORY. You receive the full, ordered task list and return a SINGLE
JSON object -- no markdown, no code fences, no prose around it.

THE GOVERNING RULE -- FIELD WORK vs NON-FIELD WORK
--------------------------------------------------
Every task is either FIELD WORK or NON-FIELD WORK. Decide that first
-- it is the primary axis of this classification.

FIELD WORK is physical, hands-on construction performed on site by
trade crews working under the site superintendent: demolition,
excavation, framing, rough-in, equipment setting, installation,
finishes, and the like. The superintendent directs this work and
reports its progress day to day.

NON-FIELD WORK is everything else -- paperwork, submittals,
procurement, approvals, design reviews, commissioning, testing &
balancing, functional testing, inspections, certifications, owner
training, close-out documentation, and milestones. The superintendent
does NOT perform or report progress on non-field work; it is owned by
other roles (the project manager, the commissioning agent, the
architect/engineer, the authority having jurisdiction, the owner).

Only once a task is identified as NON-FIELD do you place it into one
of the three non-field buckets, chosen by WHEN the work occurs.

CLASSIFY BY THE NATURE OF THE WORK, NOT BY ITS PHASE
----------------------------------------------------
Do NOT decide field vs non-field from a section header, a phase
label, or the task's position in the file. A commissioning, testing,
or inspection task scheduled in the middle of the construction phase
is STILL non-field work -- it is construction_paperwork, never
field_work. The NATURE of a task decides field vs non-field; its
TIMING only decides which non-field bucket it lands in. Treat section
headers and task ordering as, at most, a weak tie-breaking hint.

THE FOUR CATEGORIES -- assign EXACTLY ONE to every task
-------------------------------------------------------
field_work -- physical, hands-on construction by trade crews on site.
    The only category for work the superintendent personally directs
    and reports.

preconstruction_paperwork -- NON-FIELD work BEFORE the project
    mobilizes on site: early submittals, procurement, long-lead
    fabrication and delivery, plan and permit approvals,
    pre-construction QC planning.

construction_paperwork -- NON-FIELD work DURING the construction
    phase. Two kinds: (a) submittals, procurement, and approvals for
    work not yet started; and (b) commissioning, testing & balancing
    (TAB), functional or performance testing, inspections,
    certifications, owner training, and occupancy certificates.
    Non-field, but mid-project.

closeout_paperwork -- NON-FIELD work AFTER substantial completion:
    punch-list closure, as-built / record drawings, O&M manuals,
    final close-out documentation, final completion.

ILLUSTRATIVE EXAMPLES -- PATTERNS, NOT A FIXED KEYWORD LIST
-----------------------------------------------------------
The task names below illustrate the KIND of work in each bucket. They
are NOT exhaustive and NOT a keyword list -- real schedules use many
different names. Generalize from the NATURE of the work; never
classify by matching a word.

  field_work, e.g.: "Selective Demolition", "Metal Stud Framing",
  "HVAC Rough-in", "Drywall Hanging", "LVT Flooring",
  "Electrical Trim", "Millwork Installation".

  KNOWN FAILURE MODE -- non-field work frequently mis-tagged as
  field_work: "Commissioning", "Test, Adjust & Balance (TAB)",
  "Functional Testing", "Fire Alarm Test & Certification",
  "Life-Safety Inspection", "Final Inspection", "GSA Walk",
  "Certificate of Occupancy", "Owner Training". These occur during or
  after construction, but they are NOT field-crew work -- a
  commissioning agent, an inspector, or an authority performs them.
  Tag them construction_paperwork, or closeout_paperwork when they
  fall after substantial completion. NEVER field_work.

  preconstruction_paperwork, e.g.: "Submittal Preparation",
  "Submittal Approval", "Equipment Fabrication & Delivery",
  "QC Plan Approval", "Permit".

  closeout_paperwork, e.g.: "Punch List Completion",
  "As-Built Drawings", "O&M Manuals", "Final Completion".

EDGE CASES
----------
- Milestones -- zero-duration markers such as "Notice to Proceed",
  "Substantial Completion", "Warranty Start" -- are not field work.
  Bucket each milestone with the paperwork phase matching its timing.
- A submittal or procurement task appearing MID-schedule (e.g. a
  submittal that must clear before a later trade can start) is
  construction_paperwork, not preconstruction_paperwork.
- If a task name is genuinely ambiguous, do NOT fall back to
  field_work. When the work is clearly not physical crew work, choose
  the best-fitting non-field bucket. Only if a task is truly
  impossible to judge may you leave it out of the output -- the
  import's review step automatically flags every task you do not
  return, so a person can set it. Prefer to classify; omitting is a
  last resort.

OUTPUT
------
Return one JSON object, with one entry for every task you classify:

{"tags": [{"activity_code": "<code>", "work_category": "<one of the 4 values>"}, ...]}

- work_category must be EXACTLY one of: preconstruction_paperwork,
  construction_paperwork, field_work, closeout_paperwork (lowercase).
- Use the exact activity_code from the input for each entry.
- Output JSON only -- no markdown, no code fences, no commentary."""


def _build_user_payload(rows: list, project_name: str, context_note: str) -> dict:
    """Compact the ordered task list for the model: sequence index,
    activity_code, name, duration, and predecessor codes."""
    tasks = []
    for i, r in enumerate(rows):
        preds = []
        for p in (r.get("predecessors") or []):
            code = p.get("code") if isinstance(p, dict) else p
            if code:
                preds.append(str(code))
        tasks.append({
            "seq": i,
            "activity_code": str(r.get("activity_code")),
            "name": str(r.get("description") or ""),
            "duration_days": r.get("duration_days"),
            "predecessors": preds,
        })
    payload: dict = {"project_name": project_name, "tasks": tasks}
    if context_note:
        # Reviewer-supplied context for an AI re-review pass.
        payload["reviewer_context_note"] = context_note
    return payload


def parse_model_tags(raw) -> dict:
    """Parse the model's JSON into {activity_code: work_category}.

    Defensive: strips code fences, falls back to regex object extraction.
    Only the 4 valid categories survive -- anything else is dropped and
    the caller defaults that task. Returns {} on a totally unparseable
    response."""
    s = str(raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.MULTILINE)
    obj = None
    try:
        obj = json.loads(s)
    except Exception:
        m = re.search(r"\{[\s\S]+\}", s)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return {}
    out: dict = {}
    for entry in (obj.get("tags") or []):
        if not isinstance(entry, dict):
            continue
        code = entry.get("activity_code")
        cat = entry.get("work_category")
        if code is None or cat is None:
            continue
        cat = str(cat).strip().lower()
        if cat in WORK_CATEGORIES:
            out[str(code)] = cat
    return out


def _apply_classification(
    rows: list, tag_map: Optional[dict], skip_user_overrides: bool = False,
) -> dict:
    """Stamp work_category / work_category_source / work_category_needs_
    review onto each row.

    - tag_map None or {} -> every (non-skipped) task defaults to
      field_work + needs_review (the AI-failed-entirely path).
    - A task present in tag_map with a valid category -> tagged, source
      'ai', needs_review False.
    - A task missing from tag_map or with an invalid category ->
      field_work, source 'ai', needs_review True.
    - skip_user_overrides=True leaves rows whose work_category_source is
      'user' untouched (an AI re-review that preserves manual overrides).

    Mutates rows in place. Returns {tagged_count, needs_review_count,
    skipped_override_count}."""
    tag_map = tag_map or {}
    tagged = needs_review = skipped = 0
    for r in rows:
        if skip_user_overrides and r.get("work_category_source") == "user":
            skipped += 1
            continue
        cat = tag_map.get(str(r.get("activity_code")))
        if cat in WORK_CATEGORIES:
            r["work_category"] = cat
            r["work_category_source"] = "ai"
            r["work_category_needs_review"] = False
            tagged += 1
        else:
            r["work_category"] = DEFAULT_CATEGORY
            r["work_category_source"] = "ai"
            r["work_category_needs_review"] = True
            needs_review += 1
    return {
        "tagged_count": tagged,
        "needs_review_count": needs_review,
        "skipped_override_count": skipped,
    }


async def _classify_via_llm(
    rows: list, project_name: str, context_note: str,
) -> tuple:
    """Call Claude (Emergent gateway) to classify the task list.

    Returns (tag_map, model_label, ai_error). tag_map is None on any
    hard failure (no key, package missing, network error, unparseable
    response) -- the caller then defaults every task."""
    api_key = os.environ.get("EMERGENT_LLM_KEY", "")
    if not api_key:
        return None, "fallback", "AI tagging skipped: no API key configured."
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except Exception as e:  # package absent in this environment
        return None, "fallback", f"AI tagging unavailable: {e}"[:240]

    payload = _build_user_payload(rows, project_name, context_note)
    chat = LlmChat(
        api_key=api_key,
        session_id=f"sched-tag-{int(__import__('time').time())}",
        system_message=SYSTEM,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    try:
        raw = await chat.send_message(
            UserMessage(text="Classify these schedule tasks:\n"
                        + json.dumps(payload, default=str)),
        )
    except Exception as e:
        return None, "fallback", f"AI tagging call failed: {e}"[:240]

    tag_map = parse_model_tags(raw)
    if not tag_map:
        return None, "fallback", "AI tagging returned no usable classifications."
    return tag_map, MODEL_NAME, None


async def tag_work_categories(
    rows: list, project_name: str = "", context_note: str = "",
    skip_user_overrides: bool = False,
) -> dict:
    """Classify every row in `rows` with a work_category (mutates each
    row in place). `rows` is the list of REAL tasks (the parser's
    visible, non-summary rows).

    Never raises -- on any AI failure every task defaults to field_work
    and is flagged needs_review, so the import always proceeds.

    skip_user_overrides=True preserves rows a reviewer manually set
    (used by the 'Ask AI to re-review' affordance).

    Returns a summary dict {model, ai_error, tagged_count,
    needs_review_count, skipped_override_count} for the proposal record
    + the review UI banner."""
    rows = list(rows)
    if not rows:
        return {"model": "n/a", "ai_error": None, "tagged_count": 0,
                "needs_review_count": 0, "skipped_override_count": 0}
    tag_map, model_label, ai_error = await _classify_via_llm(
        rows, project_name, context_note,
    )
    counts = _apply_classification(rows, tag_map, skip_user_overrides)
    return {"model": model_label, "ai_error": ai_error, **counts}


def set_row_work_category(rows: list, activity_code: str, category: str) -> bool:
    """Reviewer override of one row's work_category. Marks the row
    work_category_source='user' so a later AI re-review can preserve it.
    Returns True if the row was found. Raises ValueError on a bad
    category."""
    if category not in WORK_CATEGORIES:
        raise ValueError(f"invalid work_category: {category!r}")
    code = str(activity_code)
    for r in rows:
        if str(r.get("activity_code")) == code:
            r["work_category"] = category
            r["work_category_source"] = "user"
            r["work_category_needs_review"] = False
            return True
    return False
