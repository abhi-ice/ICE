"""Features F2 / F3 / F4 (2026-05-18) -- Daily Report Activity Grid view layer.

Pure helpers for the DR Activity Grid. None of this feeds the CPM engine
-- cpm.py, task_status.py and working_days.py never import this module.
The values produced here are METADATA / VIEW ONLY: they shape how the
grid is displayed and never influence float, cascade, or critical-path.

F2 -- three visible sections
  The grid is split into 3 sections by the linked task's work_category:
    Section 1  Preconstruction & Construction Paperwork
               (preconstruction_paperwork OR construction_paperwork --
                two F1 tags collapse into one section)
    Section 2  Field Work          (field_work)
    Section 3  Closeout Paperwork  (closeout_paperwork)
  A task with a null / unrecognised work_category falls into Section 2
  so nothing ever disappears from the grid. Only Section 2 is editable
  by the super; Sections 1 & 3 are visible but read-only.

F4 -- automatic end-of-day status
  compute_auto_status() derives the status a grid row should show from
  the linked task's open-blocker count + the row's EOD%. Auto-status
  overrides any manually-picked value. Precedence:
    1. >= 1 open blocker  -> blocked
    2. else EOD% >= 100   -> complete
    3. else EOD% > 0      -> in_progress
    4. else (EOD% == 0)   -> not_started

F2/F3/F4 -- read-time enrichment
  enrich_grid_row() stamps the view fields (work_category,
  open_blocker_count, auto_status, task_updated_at) onto a grid row
  from its linked task. The fields are NOT persisted to mongo; the
  /grid and /context endpoints inject them on every read.
"""
from __future__ import annotations

# --- F2: the three visible grid sections ----------------------------------

SECTION_PAPERWORK = 1    # preconstruction_paperwork OR construction_paperwork
SECTION_FIELD_WORK = 2   # field_work (+ null / unknown default)
SECTION_CLOSEOUT = 3     # closeout_paperwork

SECTION_TITLES = {
    SECTION_PAPERWORK: "Preconstruction & Construction Paperwork",
    SECTION_FIELD_WORK: "Field Work",
    SECTION_CLOSEOUT: "Closeout Paperwork",
}

# Only the Field Work section is editable by the super. Sections 1 & 3
# are visible but read-only.
EDITABLE_SECTION = SECTION_FIELD_WORK

_CATEGORY_TO_SECTION = {
    "preconstruction_paperwork": SECTION_PAPERWORK,
    "construction_paperwork": SECTION_PAPERWORK,
    "field_work": SECTION_FIELD_WORK,
    "closeout_paperwork": SECTION_CLOSEOUT,
}


def section_for(work_category) -> int:
    """Return the grid section (1, 2 or 3) for a work_category tag.

    A null or unrecognised category defaults to Section 2 (Field Work)
    so a task whose F1 tagging never ran -- or ran to an unexpected
    value -- still appears on the grid rather than vanishing."""
    return _CATEGORY_TO_SECTION.get(work_category, SECTION_FIELD_WORK)


def is_editable_section(section: int) -> bool:
    """True only for the Field Work section. The super edits Section 2;
    Sections 1 & 3 render read-only (no editable inputs)."""
    return section == EDITABLE_SECTION


# --- F4: automatic end-of-day status --------------------------------------

def compute_auto_status(open_blocker_count, pct_complete_end_of_day) -> str:
    """The status a grid row should display, recomputed live.

    Precedence (blocked > complete > in_progress > not_started):
      1. >= 1 open blocker -> 'blocked'   (overrides progress entirely)
      2. else EOD% >= 100  -> 'complete'
      3. else EOD% > 0     -> 'in_progress'
      4. else (EOD% == 0)  -> 'not_started'

    Tolerant of None / non-numeric inputs (treated as 0)."""
    try:
        count = int(open_blocker_count or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        pct = float(pct_complete_end_of_day or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if count >= 1:
        return "blocked"
    if pct >= 100:
        return "complete"
    if pct > 0:
        return "in_progress"
    return "not_started"


# --- F2/F3/F4: read-time enrichment ---------------------------------------

def enrich_grid_row(row: dict, task: dict | None) -> dict:
    """Stamp the DR Activity Grid view fields onto one grid row from its
    linked task. Mutates and returns `row`.

    `task` may be None when the linked task is missing -- the row then
    gets a null work_category (Section 2), a zero blocker count, and an
    auto_status derived from EOD% alone.

    The four stamped fields are read-time only -- never written back to
    daily_report_activities."""
    task = task or {}
    row["work_category"] = task.get("work_category")
    # Day-1 round 2 (2026-10-25) -- the optimistic-lock token is the
    # task's HUMAN-edit stamp (progress_lock_at), not updated_at: schedule
    # recomputes (enterprise CPM, MS Project sync, status derivation)
    # restamp updated_at on every task, which made a save on one activity
    # 409 the next save on a different one. When no person has edited the
    # task yet, hand back updated_at instead of None so the client still
    # carries "what I saw as of this read" -- a person's first edit after
    # this read stamps a later progress_lock_at and is still caught.
    row["task_updated_at"] = task.get("progress_lock_at") or task.get("updated_at")
    count = task.get("open_blocker_count") or 0
    row["open_blocker_count"] = count
    # 2026-05-19 -- also surface open_blocker_summary so the DR Activity
    # Grid blocker badge can render the same click-popover the Tasks
    # page uses (entity_type / severity / label / link per item).
    row["open_blocker_summary"] = task.get("open_blocker_summary") or []
    row["auto_status"] = compute_auto_status(
        count, row.get("pct_complete_end_of_day"),
    )
    return row
