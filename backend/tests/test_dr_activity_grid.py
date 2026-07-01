"""Features F2 / F3 / F4 (2026-05-18) -- DR Activity Grid.

Covers the behaviours the F2-F5 spec requires that are unit-testable
without a live DB / FastAPI app (the established pattern for feature
tests in this repo -- see test_schedule_work_category.py):

  F2  The grid groups tasks into 3 sections by work_category.
      - preconstruction_paperwork + construction_paperwork -> Section 1
        (two F1 tags collapse into one section).
      - field_work -> Section 2; closeout_paperwork -> Section 3.
      - null / unknown -> Section 2 (Field Work) so no row vanishes.
      - Only Section 2 is editable; Sections 1 & 3 are read-only.

  F3  The submit-time constraint validation was removed (the
      Constraints column is gone -- redundant with the blocker badge).
      enrich_grid_row stamps the blocker count from the linked task.

  F4  Automatic status: blocked > complete > in_progress > not_started.
      An open blocker overrides progress; auto-status overrides any
      manually-stored status_at_end_of_day.

  Guard  The CPM engine (cpm.py / task_status.py / working_days.py)
         never references the DR Activity Grid view layer, and the
         Tasks page was not roped into the grid sectioning.

Run:
    python -m pytest backend/tests/test_dr_activity_grid.py
"""
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.dr_activity_grid as dag  # noqa: E402


# --- F2: three sections by work_category ----------------------------------

def test_section_for_groups_the_four_work_categories():
    """The 4 F1 work-category tags map onto exactly 3 grid sections --
    the two paperwork tags collapse into Section 1."""
    assert dag.section_for("preconstruction_paperwork") == 1
    assert dag.section_for("construction_paperwork") == 1   # two tags -> one
    assert dag.section_for("field_work") == 2
    assert dag.section_for("closeout_paperwork") == 3


def test_section_for_null_or_unknown_defaults_to_field_work():
    """A task with no work_category (pre-F1) or an unrecognised value
    falls into Section 2 (Field Work) so it never disappears."""
    assert dag.section_for(None) == 2
    assert dag.section_for("") == 2
    assert dag.section_for("something_unexpected") == 2


def test_only_the_field_work_section_is_editable():
    """F2 edit permission: the super edits Section 2 (Field Work);
    Sections 1 & 3 are visible but read-only."""
    assert dag.is_editable_section(2) is True
    assert dag.is_editable_section(1) is False
    assert dag.is_editable_section(3) is False
    # The editable section is exactly the one Field Work maps to.
    assert dag.EDITABLE_SECTION == dag.section_for("field_work")


def test_section_titles_cover_all_three_sections():
    assert set(dag.SECTION_TITLES) == {1, 2, 3}
    assert "Field Work" == dag.SECTION_TITLES[2]
    # Section 1 surfaces BOTH paperwork tags in its label.
    assert "Preconstruction" in dag.SECTION_TITLES[1]
    assert "Construction" in dag.SECTION_TITLES[1]
    assert "Closeout" in dag.SECTION_TITLES[3]


# --- F4: automatic end-of-day status --------------------------------------

def test_auto_status_each_rule_fires():
    """Each precedence rule produces its status."""
    assert dag.compute_auto_status(0, 0) == "not_started"     # rule 4
    assert dag.compute_auto_status(0, 1) == "in_progress"     # rule 3 (>0%)
    assert dag.compute_auto_status(0, 55) == "in_progress"    # rule 3
    assert dag.compute_auto_status(0, 100) == "complete"      # rule 2 (==100)
    assert dag.compute_auto_status(1, 0) == "blocked"         # rule 1


def test_auto_status_blocker_overrides_progress():
    """Rule 1 wins: an open blocker shows 'blocked' even at 100%."""
    assert dag.compute_auto_status(1, 100) == "blocked"
    assert dag.compute_auto_status(3, 50) == "blocked"


def test_auto_status_is_bidirectional_when_a_blocker_resolves():
    """Blocker added -> blocked; blocker resolved -> re-evaluates the
    progress rules (the F4 dynamic / bidirectional requirement)."""
    # 1 open blocker, 60% done -> blocked.
    assert dag.compute_auto_status(1, 60) == "blocked"
    # Same row after the blocker resolves (count 1 -> 0) -> in_progress.
    assert dag.compute_auto_status(0, 60) == "in_progress"
    # A 100%-done row reverts to complete once unblocked.
    assert dag.compute_auto_status(0, 100) == "complete"


def test_auto_status_thresholds_are_exact():
    """>0 and <100 -> in_progress; exactly 100 -> complete; exactly
    0 -> not_started."""
    assert dag.compute_auto_status(0, 0) == "not_started"
    assert dag.compute_auto_status(0, 99.9) == "in_progress"
    assert dag.compute_auto_status(0, 100) == "complete"
    assert dag.compute_auto_status(0, 100.0) == "complete"


def test_auto_status_tolerates_none_and_garbage():
    """The import must never break on the auto-status step."""
    assert dag.compute_auto_status(None, None) == "not_started"
    assert dag.compute_auto_status(None, 100) == "complete"
    assert dag.compute_auto_status("", "") == "not_started"
    assert dag.compute_auto_status("bad", "bad") == "not_started"


# --- F2/F3/F4: read-time enrichment ---------------------------------------

def test_enrich_grid_row_stamps_all_view_fields_from_the_task():
    """A row enriched from its linked task carries work_category (F2),
    open_blocker_count (F3) and auto_status (F4)."""
    row = {"id": "r1", "pct_complete_end_of_day": 50}
    task = {
        "id": "t1", "updated_at": "2026-05-18T12:00:00",
        "work_category": "construction_paperwork", "open_blocker_count": 0,
    }
    out = dag.enrich_grid_row(row, task)
    assert out["work_category"] == "construction_paperwork"
    assert out["open_blocker_count"] == 0
    assert out["auto_status"] == "in_progress"          # 50% , no blocker
    assert out["task_updated_at"] == "2026-05-18T12:00:00"


def test_enrich_grid_row_blocker_count_drives_blocked_status():
    """F3 badge + F4 status: a linked task with open blockers makes the
    row's auto_status 'blocked' and exposes the count for the badge."""
    row = {"id": "r2", "pct_complete_end_of_day": 80}
    task = {"id": "t2", "work_category": "field_work", "open_blocker_count": 2}
    out = dag.enrich_grid_row(row, task)
    assert out["open_blocker_count"] == 2               # badge shows 2
    assert out["auto_status"] == "blocked"              # blocker overrides 80%


def test_enrich_grid_row_zero_blockers_means_no_badge_count():
    """F3: 0 open blockers -> count 0 (the frontend renders no badge)."""
    row = {"id": "r3", "pct_complete_end_of_day": 0}
    task = {"id": "t3", "work_category": "field_work", "open_blocker_count": 0}
    out = dag.enrich_grid_row(row, task)
    assert out["open_blocker_count"] == 0
    assert out["auto_status"] == "not_started"


def test_enrich_grid_row_auto_status_overrides_manual_stored_status():
    """F4 'auto overrides manual': enrich_grid_row recomputes auto_status
    and ignores whatever status_at_end_of_day was manually stored."""
    # Row was manually marked 'complete' but it has 0% progress and the
    # linked task has an open blocker -> auto_status must be 'blocked'.
    row = {"id": "r4", "pct_complete_end_of_day": 0,
           "status_at_end_of_day": "complete"}
    task = {"id": "t4", "work_category": "field_work", "open_blocker_count": 1}
    out = dag.enrich_grid_row(row, task)
    assert out["auto_status"] == "blocked"
    # The stored (manual) value is left untouched -- only auto_status,
    # the computed view field, is what the grid displays.
    assert out["status_at_end_of_day"] == "complete"


def test_enrich_grid_row_handles_missing_linked_task():
    """A grid row whose linked task is gone still enriches safely:
    null work_category (-> Field Work section), zero blockers, and an
    auto_status from EOD% alone."""
    row = {"id": "r5", "pct_complete_end_of_day": 100}
    out = dag.enrich_grid_row(row, None)
    assert out["work_category"] is None
    assert dag.section_for(out["work_category"]) == 2   # -> Field Work
    assert out["open_blocker_count"] == 0
    assert out["auto_status"] == "complete"


# --- Guard: the CPM engine + Tasks page stay out of the grid view layer ----

def test_cpm_engine_never_references_the_dr_grid_view_layer():
    """Hard guardrail: the CPM engine must not read the DR Activity Grid VIEW
    layer -- the dr_activity_grid module and its display fields (auto_status,
    section_for/grid sectioning). A future edit that wires the grid view into
    float / cascade / critical-path breaks this test.

    Refined 2026-06-12 -- two tiers:
      * Pure math (cpm.py, working_days.py): forbid ALL grid tokens INCLUDING
        work_category. These files stay 100% free of any domain/view metadata.
      * task_status.py: forbid the DR-grid VIEW tokens (dr_activity_grid,
        auto_status, section_for) but PERMIT work_category -- its status
        machine reads that Task domain field for the pt.79 paperwork shortcut
        (see test_work_category_absent_from_cpm_pure_math for the rationale).
        The cascade itself still never reads it (B2's paperwork skip lives in
        services/progress_cascade.py)."""
    grid_view_tokens = ("dr_activity_grid", "auto_status", "section_for")
    # Pure math: also forbid work_category.
    for name in ("cpm.py", "working_days.py"):
        text = (Path(_BACKEND) / "services" / name).read_text(encoding="utf-8")
        for token in ("work_category",) + grid_view_tokens:
            assert token not in text, (
                f"services/{name} references {token!r} -- the pure CPM math "
                f"must never read DR Activity Grid view metadata"
            )
    # Status + cascade file: forbid the grid VIEW layer (work_category is a
    # permitted Task domain field, used only by the status machine).
    text = (Path(_BACKEND) / "services" / "task_status.py").read_text(encoding="utf-8")
    for token in grid_view_tokens:
        assert token not in text, (
            f"services/task_status.py references {token!r} -- the CPM engine "
            f"must never read the DR Activity Grid view layer"
        )


def test_tasks_page_not_roped_into_grid_sectioning():
    """The Tasks page must stay untouched by the DR Activity Grid SECTIONING /
    auto-status view logic. It must not pick up GRID_SECTIONS / gridSectionOf
    / auto_status.

    Refined 2026-06-12: work_category is PERMITTED -- the N1 feature renders a
    small WorkCategoryBadge so a task can display its own AI-assigned category.
    Displaying a task's category is not grid sectioning; the forbidden tokens
    are the actual grid-section view machinery."""
    tasks_jsx = Path(_BACKEND).parent / "frontend" / "src" / "pages" / "Tasks.jsx"
    text = tasks_jsx.read_text(encoding="utf-8")
    for token in ("GRID_SECTIONS", "gridSectionOf", "auto_status"):
        assert token not in text, (
            f"frontend Tasks.jsx references {token!r} -- the DR Activity Grid "
            f"sectioning / auto-status view logic must not leak into the Tasks page"
        )


def test_f3_submit_no_longer_requires_a_constraint():
    """F3: the submit-time 'zero-progress row needs a constraint' gate
    was removed from the daily reports router."""
    router = (Path(_BACKEND) / "routers" / "daily_reports_router.py"
              ).read_text(encoding="utf-8")
    assert "Set a constraint type or record progress" not in router
    assert "row_validation_failed" not in router


def test_f4_patch_stamps_status_from_auto_status():
    """F4: the grid-row PATCH recomputes status_at_end_of_day via
    compute_auto_status (so auto-status overrides any manual value)."""
    router = (Path(_BACKEND) / "routers" / "daily_reports_router.py"
              ).read_text(encoding="utf-8")
    assert "compute_auto_status" in router
    assert 'row_updates["status_at_end_of_day"] = compute_auto_status(' in router
