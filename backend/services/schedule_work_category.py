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
Do NOT decide field vs non-field from a phase label or the task's
position in the file. A commissioning, testing, or inspection task
scheduled in the middle of the construction phase is STILL non-field
work -- it is construction_paperwork, never field_work. The NATURE of
a task decides field vs non-field; its TIMING only decides which
non-field bucket it lands in. Task ordering is, at most, a weak
tie-breaking hint.

THE "section" FIELD -- WHAT A BARE NAME MEANS
---------------------------------------------
A task may carry "section": the schedule's own headings above it,
outermost first (e.g. "Prepare Critical Submittals > Mechanical
Equipment"). Many schedules repeat a bare equipment or material name
("Air Handling Unit", "Controls", "Lighting") once under a submittals
heading, again under a fabrication/delivery heading, and again under
an installation heading. A bare name like that says WHAT, not what is
being DONE -- the section says what is being done. Under a submittal,
approval, procurement, fabrication or delivery heading it is non-field
work; under an installation/construction heading it is field work.
The section never turns commissioning, testing or inspection into
field work.

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


def drop_shared_root(rows: list) -> list:
    """When every task sits under the same outermost heading, that heading is
    the project's own title row ("6B Renovate Outpatient Mental Health"),
    not a section: remove it from every section_path so a word in the
    project's name can never label the whole schedule. Mutates and returns
    `rows`."""
    paths = [r["section_path"] for r in rows if r.get("section_path")]
    real = [r for r in rows if not r.get("excluded_as_summary")]
    if len(paths) < 2 or len(paths) < len(real):
        return rows
    root = paths[0][0]
    if all(p[0] == root for p in paths):
        for r in rows:
            if r.get("section_path"):
                r["section_path"] = r["section_path"][1:]
                if not r["section_path"]:
                    del r["section_path"]
    return rows


def attach_section_paths(rows: list) -> list:
    """Give each real task the names of the heading rows above it
    (outermost first) as `section_path`, matched by WBS: a heading whose WBS
    is a dotted prefix of the task's WBS. Rows that already carry a
    section_path (an MS Project pull reads it from the file's outline) are
    left alone. Mutates and returns `rows`."""
    headings = {}
    for r in rows:
        if r.get("excluded_as_summary") and r.get("wbs"):
            headings[str(r["wbs"])] = str(r.get("description") or "").strip()
    if not headings:
        return rows
    for r in rows:
        if r.get("excluded_as_summary") or r.get("section_path"):
            continue
        parts = str(r.get("wbs") or "").split(".")
        path = [headings[w] for w in (".".join(parts[:i]) for i in range(1, len(parts)))
                if headings.get(w)]
        if path:
            r["section_path"] = path
    return drop_shared_root(rows)


# Section headings that say what the work under them is, read from the
# innermost heading outward. A heading that names field work stops the
# search (an "Erection" or "Installation" heading beats an outer
# "Fabricate & Erect"); a heading naming both ("Furnish, Deliver & Install")
# decides nothing, and the AI judges those tasks.
_FIELD_HEADING = re.compile(
    r"\binstall|\berect|(?<!pre-)(?<!pre )\bconstruct|\bdemoli|\bdemo\b|\babatement\b"
    r"|\brough[\s-]?in\b|\bfinishes\b|\bframing\b|\bpaving\b|\bsite\s*work\b"
    r"|\bmobiliz|\bchange\s+orders?\b",
    re.IGNORECASE)
_CLOSEOUT_HEADING = re.compile(r"\bclose[\s-]?out\b", re.IGNORECASE)
_PAPERWORK_HEADING = re.compile(
    r"\bsubmittals?\b|\bshop\s*drawings?\b|\bprocure(?:ment)?\b|\bfabricat(?:e|es|ed|ion)\b"
    r"|\bdeliver(?:y|ies)?\b|\blong[\s-]?lead\b|\bapprovals?\b",
    re.IGNORECASE)


def section_category(section_path) -> Optional[str]:
    """The non-field category a task's headings put it in, or None when the
    headings do not say. Used so a bare name ("Air Handling Unit") under
    "Prepare Critical Submittals" or "Fabricate/Delivery" is not left as
    field work on a superintendent's daily report."""
    for heading in reversed(list(section_path or [])):
        h = str(heading or "")
        if _FIELD_HEADING.search(h):
            return None
        if _CLOSEOUT_HEADING.search(h):
            return "closeout_paperwork"
        if _PAPERWORK_HEADING.search(h):
            return "construction_paperwork"
    return None


def _build_user_payload(rows: list, project_name: str, context_note: str) -> dict:
    """Compact the ordered task list for the model: sequence index,
    activity_code, name, section headings, duration, and predecessor codes."""
    tasks = []
    for i, r in enumerate(rows):
        preds = []
        for p in (r.get("predecessors") or []):
            code = p.get("code") if isinstance(p, dict) else p
            if code:
                preds.append(str(code))
        task = {
            "seq": i,
            "activity_code": str(r.get("activity_code")),
            "name": str(r.get("description") or ""),
            "duration_days": r.get("duration_days"),
            "predecessors": preds,
        }
        if r.get("section_path"):
            task["section"] = " > ".join(str(s) for s in r["section_path"])
        tasks.append(task)
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
    - A task whose section headings say submittals / procurement /
      fabrication / delivery / closeout (section_category): when the AI
      also calls it non-field, the AI's bucket wins (source 'ai'); when the
      AI says field work or skips it, the heading's category stands but is
      flagged for review so a person settles the disagreement; when the AI
      is unavailable, the heading's category stands (source 'section').
    - skip_user_overrides=True leaves rows whose work_category_source is
      'user' or 'existing' untouched (an AI re-review that preserves manual
      overrides and labels already on the project's tasks).

    Mutates rows in place. Returns {tagged_count, needs_review_count,
    skipped_override_count}."""
    ai_answered = bool(tag_map)
    tag_map = tag_map or {}
    tagged = needs_review = skipped = 0
    for r in rows:
        if skip_user_overrides and r.get("work_category_source") in ("user", "existing"):
            skipped += 1
            continue
        cat = tag_map.get(str(r.get("activity_code")))
        by_section = section_category(r.get("section_path"))
        if by_section and cat not in WORK_CATEGORIES - {DEFAULT_CATEGORY}:
            r["work_category"] = by_section
            r["work_category_source"] = "section"
            r["work_category_needs_review"] = ai_answered
            if ai_answered:
                needs_review += 1
            else:
                tagged += 1
        elif cat in WORK_CATEGORIES:
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
    if skip_user_overrides and all(
            r.get("work_category_source") in ("user", "existing") for r in rows):
        # Every task keeps a label a person set or the project already has.
        tag_map, model_label, ai_error = {}, "kept labels", None
    else:
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
