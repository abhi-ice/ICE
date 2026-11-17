"""Fixture tests for the schedule-group search chunkers.

No database: a fake ChunkContext answers parent lookups from canned rows.
Sample documents are shaped like rows in the production backup.
"""
import asyncio
import copy

from services.search.chunkers import schedule as S
from services.search.types import ChunkContext, Chunk

TENANT = "36cba348-98a8-4f85-857b-3f528616e505"
PROJECT = "ce400cc8-4513-4874-bc63-d481241c1e20"


class FakeCtx(ChunkContext):
    """ChunkContext whose .one() reads canned rows instead of Mongo."""

    def __init__(self, rows):
        super().__init__(db=None)
        self.rows = rows      # {collection: [docs]}
        self.calls = []

    async def one(self, collection, filt, projection=None):
        self.calls.append((collection, dict(filt)))
        for d in self.rows.get(collection, []):
            if all(d.get(k) == v for k, v in filt.items()):
                if not projection:
                    return d
                # Behave like Mongo: an inclusion projection returns only the
                # named fields (plus _id), so a projection that forgets a
                # field the chunker reads makes a test fail.
                keep = {k for k, on in projection.items() if on}
                return {k: v for k, v in d.items() if k in keep or k == "_id"}
        return None


def _ctx(**extra):
    rows = {
        "projects": [{"id": PROJECT, "tenant_id": TENANT, "name": "Bronx 6B Renovation"}],
        "tasks": [
            {"id": "t-12", "tenant_id": TENANT, "project_id": PROJECT,
             "activity_code": "12", "trade_code": "DRYWALL", "status": "not_started"},
            {"id": "t-dead", "tenant_id": TENANT, "project_id": PROJECT,
             "activity_code": "99", "status": "deleted"},
        ],
        "delays": [
            {"id": "d-1", "tenant_id": TENANT, "project_id": PROJECT, "status": "open",
             "description": "Design clarification needed on partition system before submittal"},
        ],
    }
    for k, v in extra.items():
        rows.setdefault(k, []).extend(v)
    return FakeCtx(rows)


def run(coro):
    return asyncio.run(coro)


FORBIDDEN = ["@", "555-", "$", "http", "storage_key", "uploads/", "sk-ant"]


def _assert_clean(chunks):
    for c in chunks:
        assert isinstance(c, Chunk)
        for bad in FORBIDDEN:
            assert bad not in c.text, (bad, c.text)


# ---------------------------------------------------------------- tasks

TASK = {
    "_id": {"$oid": "6a1e0349a063c72985bac77f"},
    "id": "24e8d0a6-dd71-40c0-b1bf-3bd5b2a25a69", "tenant_id": TENANT,
    "project_id": PROJECT, "activity_code": "VA6B-0040",
    "description": "Quality Control Plan Submittal & Approval",
    "display_name": None, "section_path": ["Prepare Critical Submittals"],
    "wbs": "1.4.1", "trade_code": None, "required_trade": None,
    "planned_start": "2026-05-13", "planned_finish": "2026-05-19",
    "status": "not_started", "is_milestone": False,
    "resource_names": [], "budgeted_hours": 120, "created_by": "user-1",
    "open_blocker_summary": [{"label": "call 212-555-0199"}],
}


def test_task_chunk_header_and_metadata():
    r = run(S.chunk_task(_ctx(), copy.deepcopy(TASK)))
    assert r.tenant_id == TENANT and r.project_id == PROJECT
    assert r.source_id == TASK["id"]
    assert len(r.chunks) == 1
    c = r.chunks[0]
    assert c.kind == "task"
    assert c.header.startswith("Bronx 6B Renovation | Activity VA6B-0040 Quality Control Plan")
    assert "2026-05-13 to 2026-05-19" in c.header
    assert "status not started" in c.header
    assert "Section: Prepare Critical Submittals" in c.body
    assert "WBS 1.4.1" in c.body
    assert c.date == "2026-05-13"
    assert c.activity_code == ["VA6B-0040"]
    assert c.trade_code == []
    assert c.status == "not_started"
    assert c.url == f"/tasks?openId={TASK['id']}"
    assert c.title.startswith("Activity VA6B-0040 - ")
    assert c.allowed_roles is None
    assert r.requeue == []
    # derived blocker labels, user ids and numbers stay out
    assert "212" not in c.text and "user-1" not in c.text and "120" not in c.text
    _assert_clean(r.chunks)


def test_task_trade_and_display_name():
    d = copy.deepcopy(TASK)
    d.update(trade_code="HVAC", display_name="QC plan · May 13-19",
             resource_names=["Acme Mechanical"], is_milestone=True)
    c = run(S.chunk_task(_ctx(), d)).chunks[0]
    assert c.trade_code == ["HVAC"] and "trade HVAC" in c.header
    assert "Also called: QC plan" in c.body
    assert "Resources: Acme Mechanical" in c.body and "Milestone" in c.body


def test_task_deleted_orphan_and_empty_get_no_chunks():
    for change in ({"status": "deleted"}, {"deleted_at": "2026-06-01"},
                   {"project_id": "gone"}, {"tenant_id": "other-tenant"},
                   {"description": "", "display_name": None}):
        d = copy.deepcopy(TASK)
        d.update(change)
        r = run(S.chunk_task(_ctx(), d))
        assert r is not None and r.chunks == [], change


# ---------------------------------------------------------------- schedule_proposals

PROPOSAL = {
    "id": "a2722116-8425-4f8c-81a8-f21ffc493375", "tenant_id": TENANT,
    "project_id": PROJECT, "filename": "GSA_OfficeRenovation_Schedule.xlsx",
    "kind": "schedule", "status": "approved",
    "parsed_rows": [{"activity_code": "34", "description": "SECRET-PARSED-ROW"}],
    "diff": {"added": [], "summary": {"added_count": 0}},
    "ai_review": {
        "risk_level": "medium",
        "summary": "This update shifts 44 of 53 activities. Final Completion moved from August 31 to September 10.",
        "flags": ["Activity 53 Final Completion delayed 10 days to September 10"],
        "recommendation": "approve_with_caution", "model": "claude-sonnet-4-5-20250929"},
    "uploaded_by": "8dba8b1c", "uploaded_at": "2026-05-17T13:14:19.769138+00:00",
    "review_notes": "Owner agreed to the slip; call pm@example.com or 718-555-0100. Extra cost $12,500.",
}


def test_schedule_proposal_summary_chunk():
    r = run(S.chunk_schedule_proposal(_ctx(), copy.deepcopy(PROPOSAL)))
    assert len(r.chunks) == 1
    c = r.chunks[0]
    assert c.kind == "schedule_import"
    assert c.header == ("Bronx 6B Renovation | Schedule import GSA_OfficeRenovation_Schedule.xlsx "
                        "2026-05-17 | status approved")
    assert "Final Completion moved" in c.body
    assert "Flags: Activity 53" in c.body
    assert "Reviewer notes: Owner agreed to the slip" in c.body
    assert "SECRET-PARSED-ROW" not in c.text
    assert c.date == "2026-05-17" and c.status == "approved"
    assert c.url == f"/schedule-changes?id={PROPOSAL['id']}"
    _assert_clean(r.chunks)


def test_schedule_proposal_heuristic_review_without_notes_is_skipped():
    d = copy.deepcopy(PROPOSAL)
    d["ai_review"]["model"] = "heuristic"
    d["review_notes"] = ""
    assert run(S.chunk_schedule_proposal(_ctx(), d)).chunks == []
    d["review_notes"] = "Approved after the call with the owner."
    c = run(S.chunk_schedule_proposal(_ctx(), d)).chunks[0]
    assert "Final Completion" not in c.body and "Approved after the call" in c.body


def test_schedule_proposal_ai_error_review_is_not_embedded():
    # A real model name but ai_error set: the summary is not trustworthy.
    d = copy.deepcopy(PROPOSAL)
    d["ai_review"]["ai_error"] = "timeout"
    d["review_notes"] = ""
    assert run(S.chunk_schedule_proposal(_ctx(), d)).chunks == []
    d["review_notes"] = "Approved after the call with the owner."
    c = run(S.chunk_schedule_proposal(_ctx(), d)).chunks[0]
    assert "Final Completion" not in c.body and "Flags" not in c.body
    assert "Recommendation" not in c.body and "Approved after the call" in c.body


def test_schedule_proposal_orphan_project():
    d = copy.deepcopy(PROPOSAL)
    d["project_id"] = "deleted-project"
    assert run(S.chunk_schedule_proposal(_ctx(), d)).chunks == []


# ---------------------------------------------------------------- mitigations

MITIGATION = {
    "id": "51cbf292-611f-4725-b131-8f9afb1a197e", "tenant_id": TENANT,
    "project_id": PROJECT, "linked_delay_id": "d-1",
    "linked_task_ids": ["t-12", "t-dead", "t-missing"],
    "source": "ai_recovery_engine", "scenario_label": "B",
    "strategy_name": "Owner direction",
    "assumption": "Proactive owner engagement can compress the resolution timeline.",
    "proposed_action_description": "Escalate the design clarification to the owner's representative.",
    "prerequisites": ["Owner rep availability", "Designer responsiveness"],
    "estimated_cost_impact_usd": 4500.0, "actual_cost_usd": 3900.0,
    "ai_reasoning": "Owner direction can recover 2.5 of the 3.5 projected days at about $4,500.",
    "decision_status": "chosen", "proposed_at": "2026-06-05T21:08:11.966723+00:00",
    "decision_rationale": "Owner rep was on site this week.",
    "outcome_notes": "Answer came back in two days.",
    "proposed_by_user_id": "42663281",
}


def test_mitigation_chunk():
    r = run(S.chunk_mitigation(_ctx(), copy.deepcopy(MITIGATION)))
    assert r.project_id == PROJECT and r.source_id == MITIGATION["id"]
    assert len(r.chunks) == 1
    c = r.chunks[0]
    assert c.kind == "mitigation"
    assert c.header.startswith('Bronx 6B Renovation | Mitigation B for delay "Design clarification')
    assert "2026-06-05" in c.header and "status chosen" in c.header and "activities 12" in c.header
    assert c.activity_code == ["12"]          # deleted and missing tasks dropped
    assert c.trade_code == ["DRYWALL"]
    assert c.status == "chosen" and c.date == "2026-06-05"
    assert c.parent_id == "d-1" and c.url == "/delays/d-1"
    assert "Decision rationale: Owner rep was on site" in c.body
    assert "Outcome: Answer came back" in c.body
    assert "Prerequisites: Owner rep availability; Designer responsiveness" in c.body
    assert "4500" not in c.text and "3900" not in c.text and "4,500" not in c.text
    assert "42663281" not in c.text
    assert r.requeue == []
    _assert_clean(r.chunks)


def test_mitigation_orphans_get_no_chunks():
    d = copy.deepcopy(MITIGATION)
    d["linked_delay_id"] = "gone"
    assert run(S.chunk_mitigation(_ctx(), d)).chunks == []
    ctx = _ctx(delays=[{"id": "d-del", "tenant_id": TENANT, "status": "deleted",
                        "description": "x"}])
    d["linked_delay_id"] = "d-del"
    assert run(S.chunk_mitigation(ctx, d)).chunks == []
    d = copy.deepcopy(MITIGATION)
    d["project_id"] = "gone"
    assert run(S.chunk_mitigation(_ctx(), d)).chunks == []


def test_mitigation_on_other_project_than_its_delay_gets_no_chunks():
    other = "other-project"
    ctx = _ctx(projects=[{"id": other, "tenant_id": TENANT, "name": "Other job"}],
               delays=[{"id": "d-other", "tenant_id": TENANT, "project_id": other,
                        "status": "open", "description": "Late steel"}])
    # mitigation says PROJECT, its delay lives on another project
    d = copy.deepcopy(MITIGATION)
    d["linked_delay_id"] = "d-other"
    assert run(S.chunk_mitigation(ctx, d)).chunks == []
    # and the reverse: mitigation filed on the other project, delay on PROJECT
    d = copy.deepcopy(MITIGATION)
    d["project_id"] = other
    assert run(S.chunk_mitigation(ctx, d)).chunks == []


def test_mitigation_soft_deleted_delay_flag_is_honored():
    ctx = _ctx(delays=[{"id": "d-flag", "tenant_id": TENANT, "project_id": PROJECT,
                        "status": "open", "is_deleted": True, "description": "x"}])
    d = copy.deepcopy(MITIGATION)
    d["linked_delay_id"] = "d-flag"
    assert run(S.chunk_mitigation(ctx, d)).chunks == []


def test_mitigation_lookups_are_tenant_scoped():
    ctx = _ctx()
    run(S.chunk_mitigation(ctx, copy.deepcopy(MITIGATION)))
    for coll, filt in ctx.calls:
        assert filt.get("tenant_id") == TENANT, (coll, filt)


# ---------------------------------------------------------------- recovery_reports

REPORT = {
    "id": "rr-1", "tenant_id": TENANT, "project_id": PROJECT,
    "report_type": "friday_recovery", "generated_at": "2026-11-13T21:00:00+00:00",
    "period_start": "2026-11-09", "period_end": "2026-11-13",
    "data_snapshot": {"cost": 99999}, "status": "finalized",
    "ai_narrative": "Drywall is three days behind on floor 3.\n\nAdding a second crew costs $8,000 and recovers two days.",
    "recovery_actions": [
        {"action": "Add a second drywall crew on Saturday", "owner": "Drywall foreman",
         "rationale": "Recovers two of three days", "due_date": "2026-11-15",
         "source_mitigation_id": "m-1"},
        {"action": "", "rationale": "empty action is skipped"},
    ],
    "exhibit_artifacts": [{"storage_key": "uploads/tenant/rr-1.pdf"}],
    "audit_op_label": "Friday Recovery Report 2026-W46",
    "ai_cost_estimate_usd": 0.42,
}


def test_recovery_report_chunks_and_roles():
    r = run(S.chunk_recovery_report(_ctx(), copy.deepcopy(REPORT)))
    kinds = [c.kind for c in r.chunks]
    assert kinds == ["recovery_report", "recovery_action"]
    narr, act = r.chunks
    assert narr.header == ("Bronx 6B Renovation | Recovery report Friday recovery "
                           "2026-11-09 to 2026-11-13 | status finalized")
    assert "Drywall is three days behind" in narr.body
    assert "8,000" not in narr.text
    assert "Action: Add a second drywall crew" in act.body
    assert "Owner: Drywall foreman" in act.body and "Due 2026-11-15" in act.body
    assert act.parent_id == "rr-1"
    for c in r.chunks:
        assert c.date == "2026-11-13" and c.status == "finalized"
        assert c.url == "/weekly-reports?tab=generator&openId=rr-1"
        assert c.allowed_roles is not None
        assert "submittal_tech" not in c.allowed_roles
        assert "scheduling_engineer" not in c.allowed_roles
        assert set(c.allowed_roles) == {"admin", "pm", "super", "px", "pe", "foreman", "viewer"}
        assert "99999" not in c.text and "0.42" not in c.text
    assert narr.title == "Friday Recovery Report 2026-W46"
    _assert_clean(r.chunks)


def test_recovery_report_skips_archived_placeholder_orphan():
    for change in ({"status": "archived"}, {"_placeholder": True},
                   {"project_id": "gone"}, {"status": None}):
        d = copy.deepcopy(REPORT)
        d.update(change)
        assert run(S.chunk_recovery_report(_ctx(), d)).chunks == [], change


def test_recovery_report_long_narrative_is_split():
    d = copy.deepcopy(REPORT)
    d["recovery_actions"] = []
    d["ai_narrative"] = "\n\n".join(["Paragraph about the slip on floor three. " * 40] * 8)
    chunks = run(S.chunk_recovery_report(_ctx(), d)).chunks
    assert len(chunks) > 1
    assert all(len(c.body) <= S._MAX_BODY_CHARS for c in chunks)


# ---------------------------------------------------------------- registry entries

def test_entries_cover_the_group():
    by = {e.collection: e for e in S.ENTRIES}
    required = {"tasks", "schedule_proposals", "schedule_update_diffs", "logic_proposals",
                "recovery_reports", "ai_recovery_suggestions", "mitigations",
                "tia_generations", "cascade_snapshots", "task_sync_events",
                "project_calendar_days", "mitigation_outcomes"}
    assert required <= set(by)
    # 'calendars' belongs to docs_company.py; listing it here too would log a
    # "listed twice" warning on every start.
    assert "calendars" not in by
    assert by["mitigation_outcomes"].decision == "exclude"
    assert len(by) == len(S.ENTRIES), "duplicate collection entries"
    for e in S.ENTRIES:
        assert e.decision in ("index", "query_only", "exclude")
        assert e.reason.strip()
        assert (e.chunker is not None) == (e.decision == "index"), e.collection
    assert by["tia_generations"].decision == "exclude"
    assert by["recovery_reports"].decision == "index"


def test_scrub_removes_contact_and_money():
    s = S._scrub("Call 718-555-0100 or (212) 555-0199, mail a.b@x.com, see https://x.io/a, pay $1,250.00")
    assert "555" not in s and "@" not in s and "http" not in s and "$" not in s
    # dates and activity codes survive
    assert S._scrub("VA6B-1680 starts 2026-05-12") == "VA6B-1680 starts 2026-05-12"
    # other money spellings
    for text in ("costs USD 2,500 more", "costs 2,500 USD more", "costs 2,500 dollars more",
                 "costs 2.5k USD more", "costs USD2500 more", "costs 1 dollar more",
                 "The $2,500 cost reflects overtime"):
        s = S._scrub(text)
        assert "2,500" not in s and "2500" not in s and "2.5" not in s, (text, s)
        assert "USD" not in s and "dollar" not in s.lower() and "$" not in s, (text, s)
        assert "[amount removed]" in s, (text, s)
    # a ten-digit number with no separators is a phone number
    s = S._scrub("call 2125550199 today, or 12125550199")
    assert "2125550199" not in s and s.count("[phone removed]") == 2, s
    # but codes, plain counts and long ids stay
    for keep in ("VA6B-1680", "A2125550199", "2125550199-B", "53 activities",
                 "job 123456789012"):
        assert S._scrub(keep) == keep, keep
