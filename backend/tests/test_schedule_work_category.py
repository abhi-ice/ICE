"""Feature F1 (2026-05-17) -- schedule-import AI work-category tagging.

Covers the four behaviours the F1 spec requires:
  1. The tagging step assigns every task exactly one of the 4 valid tags.
  2. A reviewer override changes a task's stored work_category.
  3. The AI-failure path: tagging never raises -- on failure every task
     defaults to field_work (flagged for review) so the import still
     completes.
  4. work_category is metadata only -- it never appears in the CPM
     engine (cpm.py / task_status.py / working_days.py).

The AI tagging is split into pure helpers (parse_model_tags,
_apply_classification, set_row_work_category) so the classification
logic is tested without a live LLM. tag_work_categories is exercised
end-to-end on the no-API-key fallback path.

Run:
    python -m pytest backend/tests/test_schedule_work_category.py
"""
import asyncio
import os
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.schedule_work_category as swc  # noqa: E402

VALID = swc.WORK_CATEGORIES


def _rows() -> list[dict]:
    """A small parsed schedule covering all 4 lifecycle phases."""
    return [
        {"activity_code": "1", "description": "Pre-Construction Submittals",
         "duration_days": 5, "predecessors": []},
        {"activity_code": "2", "description": "Mobilization & Site Setup",
         "duration_days": 2, "predecessors": [{"code": "1"}]},
        {"activity_code": "3", "description": "Selective Demolition",
         "duration_days": 4, "predecessors": [{"code": "2"}]},
        {"activity_code": "4", "description": "HVAC Submittal Approval",
         "duration_days": 3, "predecessors": [{"code": "2"}]},
        {"activity_code": "5", "description": "Punch List and Closeout",
         "duration_days": 3, "predecessors": [{"code": "3"}]},
    ]


# --- 1. Tagging step assigns one of the 4 valid tags to every task ---------

def test_tagging_assigns_one_valid_tag_to_every_task():
    """A model response covering every task -> every row carries exactly
    one of the 4 valid work_category values."""
    rows = _rows()
    model_json = (
        '{"tags": ['
        '{"activity_code": "1", "work_category": "preconstruction_paperwork"},'
        '{"activity_code": "2", "work_category": "field_work"},'
        '{"activity_code": "3", "work_category": "field_work"},'
        '{"activity_code": "4", "work_category": "construction_paperwork"},'
        '{"activity_code": "5", "work_category": "closeout_paperwork"}]}'
    )
    tag_map = swc.parse_model_tags(model_json)
    counts = swc._apply_classification(rows, tag_map)

    for r in rows:
        assert r["work_category"] in VALID, (
            f"{r['activity_code']} got invalid tag {r['work_category']!r}"
        )
        assert r["work_category_source"] == "ai"
    assert rows[0]["work_category"] == "preconstruction_paperwork"
    assert rows[3]["work_category"] == "construction_paperwork"
    assert rows[4]["work_category"] == "closeout_paperwork"
    assert counts["tagged_count"] == 5 and counts["needs_review_count"] == 0
    # No task is flagged when the model classified all of them.
    assert all(r["work_category_needs_review"] is False for r in rows)


def test_parse_model_tags_strips_fences_and_drops_invalid():
    """parse_model_tags tolerates code fences and drops any category
    that is not one of the 4 valid values."""
    fenced = (
        "```json\n"
        '{"tags": [{"activity_code": "1", "work_category": "field_work"},'
        '{"activity_code": "2", "work_category": "not_a_real_category"}]}\n'
        "```"
    )
    out = swc.parse_model_tags(fenced)
    assert out == {"1": "field_work"}  # the bogus category is dropped
    # Totally unparseable -> empty dict (the caller then defaults).
    assert swc.parse_model_tags("the model rambled, no json here") == {}


def test_unclassified_task_defaults_to_field_work_and_is_flagged():
    """A task the model omits (or mis-classifies) defaults to field_work
    and is flagged for review -- the import never crashes on it."""
    rows = _rows()
    # Model only classified tasks 1-3; 4 and 5 are missing.
    partial = {"1": "preconstruction_paperwork", "2": "field_work",
               "3": "field_work"}
    swc._apply_classification(rows, partial)

    by_code = {r["activity_code"]: r for r in rows}
    for code in ("4", "5"):
        assert by_code[code]["work_category"] == "field_work"
        assert by_code[code]["work_category_needs_review"] is True
    # The classified ones are not flagged.
    assert by_code["1"]["work_category_needs_review"] is False


# --- 2. Reviewer override changes the stored work_category -----------------

def test_user_override_changes_stored_work_category():
    rows = _rows()
    swc._apply_classification(rows, {r["activity_code"]: "field_work"
                                     for r in rows})
    # Reviewer overrides task 4 to construction_paperwork.
    found = swc.set_row_work_category(rows, "4", "construction_paperwork")
    assert found is True
    task4 = next(r for r in rows if r["activity_code"] == "4")
    assert task4["work_category"] == "construction_paperwork"
    assert task4["work_category_source"] == "user"
    assert task4["work_category_needs_review"] is False
    # Unknown activity_code -> returns False (caller raises 404).
    assert swc.set_row_work_category(rows, "999", "field_work") is False
    # An invalid category is rejected.
    try:
        swc.set_row_work_category(rows, "4", "bogus_category")
        assert False, "expected ValueError on invalid category"
    except ValueError:
        pass


def test_retag_preserves_user_overrides_unless_redo_all():
    """An AI re-review keeps reviewer overrides when skip_user_overrides
    is set, and overwrites them when it is not (the 'redo all' path)."""
    rows = _rows()
    swc._apply_classification(rows, {r["activity_code"]: "field_work"
                                     for r in rows})
    swc.set_row_work_category(rows, "1", "preconstruction_paperwork")  # user

    # Re-review: the model now wants task 1 = field_work, but the user
    # override must survive (skip_user_overrides=True).
    new_map = {r["activity_code"]: "field_work" for r in rows}
    swc._apply_classification(rows, new_map, skip_user_overrides=True)
    assert rows[0]["work_category"] == "preconstruction_paperwork"
    assert rows[0]["work_category_source"] == "user"

    # redo_all -> skip_user_overrides=False -> the override is replaced.
    swc._apply_classification(rows, new_map, skip_user_overrides=False)
    assert rows[0]["work_category"] == "field_work"
    assert rows[0]["work_category_source"] == "ai"


# --- 3. AI-failure path: import still completes, default field_work --------

def test_ai_failure_defaults_all_to_field_work():
    """With no EMERGENT_LLM_KEY the LLM cannot be reached.
    tag_work_categories must NOT raise -- it returns normally with every
    task defaulted to field_work and flagged for review (ai_error set)."""
    os.environ.pop("EMERGENT_LLM_KEY", None)
    rows = _rows()
    result = asyncio.run(
        swc.tag_work_categories(rows, project_name="Test Project"),
    )
    assert result["model"] == "fallback"
    assert result["ai_error"]  # a non-empty error string
    assert result["needs_review_count"] == len(rows)
    for r in rows:
        assert r["work_category"] == "field_work"
        assert r["work_category"] in VALID
        assert r["work_category_needs_review"] is True
        assert r["work_category_source"] == "ai"


def test_tag_work_categories_empty_rows_is_safe():
    """No rows -> no crash, empty summary."""
    result = asyncio.run(swc.tag_work_categories([], project_name="x"))
    assert result["tagged_count"] == 0
    assert result["needs_review_count"] == 0


# --- 4. work_category is metadata -- never read by the CPM engine ---------

def test_work_category_absent_from_cpm_engine():
    """Hard guardrail: the CPM engine must not reference work_category.
    A future edit that wires it into float / cascade / critical-path
    breaks this test."""
    for name in ("cpm.py", "task_status.py", "working_days.py"):
        text = (Path(_BACKEND) / "services" / name).read_text(encoding="utf-8")
        assert "work_category" not in text, (
            f"services/{name} references work_category -- the CPM engine "
            f"must never read this metadata field"
        )
