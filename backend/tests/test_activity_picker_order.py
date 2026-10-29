"""Daily report "Add activity" list (2026-10-29): schedule order, now / next
two weeks / later groups, and the one-time backfill of schedule headings onto
activities imported before headings were saved."""
import asyncio
import os
import sys
import types

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def test_wbs_sorts_in_schedule_order_not_as_text():
    from routers.schedule_review_router import wbs_sort_key
    codes = ["1.6.10", "1.6.2", "1.6.5.3.1", "1.6.16", "1.6.6.10", "1.6.6.2"]
    assert sorted(codes, key=wbs_sort_key) == ["1.6.2", "1.6.5.3.1", "1.6.6.2", "1.6.6.10", "1.6.10", "1.6.16"]


def test_picker_groups_now_soon_later_by_start_date():
    from routers.schedule_review_router import order_for_picker
    rows = [
        {"activity_code": "TEST", "wbs": "1.6.10", "planned_start": "2027-11-26", "planned_finish": "2027-11-29"},
        {"activity_code": "ELEC2", "wbs": "1.6.5.3.4", "planned_start": "2026-10-20", "planned_finish": "2026-10-23"},
        {"activity_code": "FURN", "wbs": "1.6.4", "planned_start": "2026-05-07", "planned_finish": "2026-07-01"},
        {"activity_code": "ABATE", "wbs": "1.6.5.3.5", "planned_start": "2026-11-05", "planned_finish": "2026-11-27"},
        {"activity_code": "CASE2", "wbs": "1.6.5.3.1", "planned_start": "2026-10-22", "planned_finish": "2026-11-05"},
        # A delay moved this one's start into the next two weeks.
        {"activity_code": "FIRE", "wbs": "1.6.6.1", "planned_start": "2026-12-30", "planned_finish": "2027-01-08",
         "projected_start_date": "2026-11-09", "projected_finish_date": "2026-11-17"},
    ]
    out = order_for_picker(rows, "2026-10-29")
    assert [r["activity_code"] for r in out] == ["FURN", "ELEC2", "CASE2", "ABATE", "FIRE", "TEST"]
    by = {r["activity_code"]: r for r in out}
    assert [by[c]["group"] for c in ("FURN", "ELEC2", "CASE2")] == ["now"] * 3
    assert by["ABATE"]["group"] == "soon" and by["FIRE"]["group"] == "soon"
    assert by["TEST"]["group"] == "later"
    assert by["FURN"]["late"] and by["ELEC2"]["late"] and not by["CASE2"]["late"]


# --- one-time backfill of headings -----------------------------------------
class _Cur:
    def __init__(self, rows): self.rows = rows
    def sort(self, *a, **k): return self
    async def to_list(self, n): return self.rows


class _Col:
    def __init__(self, rows=None): self.rows = rows or []

    def find(self, q=None, p=None):
        return _Cur([r for r in self.rows if all(
            (r.get(k) in v["$in"]) if isinstance(v, dict) and "$in" in v else r.get(k) == v
            for k, v in (q or {}).items())])

    async def update_many(self, q, ops):
        n = 0
        for r in self.find(q).rows:
            r.update(ops["$set"]); n += 1
        return types.SimpleNamespace(modified_count=n)


def test_backfill_puts_headings_on_existing_activities():
    from services.one_time_migrations import _backfill_section_paths
    rows = [
        {"activity_code": "VA6B-H1", "description": "6B Renovate", "wbs": "1", "excluded_as_summary": True},
        {"activity_code": "VA6B-H1.6", "description": "Construction Activties", "wbs": "1.6", "excluded_as_summary": True},
        {"activity_code": "VA6B-H1.6.5", "description": "Demolition", "wbs": "1.6.5", "excluded_as_summary": True},
        {"activity_code": "VA6B-H1.6.5.3", "description": "Demolition - Phase 2", "wbs": "1.6.5.3", "excluded_as_summary": True},
        {"activity_code": "VA6B-1680", "description": "Electrical", "wbs": "1.6.5.3.4"},
        {"activity_code": "VA6B-0010", "description": "Project NTP", "wbs": "1.1"},
    ]
    db = types.SimpleNamespace(
        projects=_Col([{"id": "p1", "tenant_id": "t1"}]),
        schedule_proposals=_Col([{"tenant_id": "t1", "project_id": "p1", "status": "approved",
                                  "parsed_rows": rows}]),
        tasks=_Col([
            {"tenant_id": "t1", "project_id": "p1", "activity_code": "VA6B-1680", "section_path": None},
            {"tenant_id": "t1", "project_id": "p1", "activity_code": "VA6B-0010"},
        ]),
    )
    out = asyncio.run(_backfill_section_paths(db))
    elec = db.tasks.rows[0]
    assert elec["section_path"] == ["Construction Activties", "Demolition", "Demolition - Phase 2"]
    assert "section_path" not in db.tasks.rows[1] or not db.tasks.rows[1]["section_path"]
    assert out["tasks_filled"] == 1
    # Running it again (or on an import already stored with its paths)
    # changes nothing: top-level rows never get the project title as heading.
    db.tasks.rows[1].pop("section_path", None)
    asyncio.run(_backfill_section_paths(db))
    assert not db.tasks.rows[1].get("section_path")
