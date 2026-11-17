"""Fixture tests for the "issues" search chunkers (no database needed).

Sample rows follow the shape of the 2026-11-17 backup (delays,
submittal_review_events) and the read models in models.py /
routers/iter4_router.py for collections the backup has no rows for.
"""
import asyncio
import re

import pytest

from services.search.chunkers import issues
from services.search.types import ChunkContext

T1 = "36cba348-98a8-4f85-857b-3f528616e505"
P1 = "ce400cc8-4513-4874-bc63-d481241c1e20"
TASK = "232a10c1-4b23-4cec-87d0-6d3e8765989a"


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, n):
        return self.rows[:n]


class _Coll:
    def __init__(self, rows):
        self.rows = rows

    def find(self, filt, proj=None):
        return _Cursor([r for r in self.rows if all(r.get(k) == v for k, v in filt.items())])


class _DB:
    def __init__(self, data):
        self.data = data

    def __getitem__(self, name):
        return _Coll(self.data.get(name, []))


class FakeCtx(ChunkContext):
    """ChunkContext whose .one() reads canned rows instead of Mongo."""

    def __init__(self, data):
        super().__init__(_DB(data))
        self.data = data

    async def one(self, collection, filt, projection=None):
        for r in self.data.get(collection, []):
            if all(r.get(k) == v for k, v in filt.items()):
                return r
        return None


BASE = {
    "projects": [{"id": P1, "tenant_id": T1, "name": "VA 6B", "status": "planning"}],
    "tenants": [{"id": T1}],
    "tasks": [{"id": TASK, "tenant_id": T1, "project_id": P1, "activity_code": "22",
               "trade_code": "FP", "description": "Sprinkler Rough-in"}],
    "locations": [{"id": "loc1", "tenant_id": T1, "name": "Level 6 East"}],
    "rfis": [{"id": "rfi-1", "tenant_id": T1, "project_id": P1, "rfi_number": "7"}],
    "submittals": [{"id": "sub-1", "tenant_id": T1, "project_id": P1}],
    "work_items": [{"id": "wi-1", "tenant_id": T1, "project_id": P1}],
    "submittal_review_events": [
        {"id": "ev1", "tenant_id": T1, "project_id": P1, "submittal_id": "sub-1",
         "event_type": "returned_to_contractor", "performed_at": "2026-06-10T12:00:00+00:00",
         "notes": "Architect wants the 2-hour rated assembly cut sheet"},
        {"id": "ev0", "tenant_id": T1, "project_id": P1, "submittal_id": "sub-1",
         "event_type": "submitted_to_ae", "performed_at": "2026-06-01T12:00:00+00:00",
         "notes": None},
    ],
    "work_item_links": [
        {"id": "l1", "tenant_id": T1, "work_item_id": "wi-1", "entity_type": "task",
         "entity_id": TASK, "notes": "Blocks sprinkler rough-in", "created_at": "2026-06-02"},
    ],
}

# Free text planted in fixtures: a dollar figure, a bare phone number and a
# storage key. Every chunk must come out with all three masked.
LEAK = " Quote $18,750.50, call 2125551234, see s3://bucket/k.pdf"

FORBIDDEN = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w-]+"),          # emails
    re.compile(r"\d{3}[\s.-]?\d{3}[\s.-]?\d{4}"),    # phones, with or without separators
    re.compile(r"s3://", re.I),                       # storage keys
    re.compile(r"18,750"),                            # the planted dollar figure
    re.compile(r"https?://", re.I),                  # links / storage urls
    re.compile(r"IMPACT:\s*cost", re.I),
    re.compile(r"\$\s?\d"),                          # money
]


def run(coro):
    return asyncio.run(coro)


def ctx(extra=None):
    data = {k: list(v) for k, v in BASE.items()}
    for k, v in (extra or {}).items():
        data[k] = v
    return FakeCtx(data)


def assert_clean(res):
    for c in res.chunks:
        for pat in FORBIDDEN:
            assert not pat.search(c.text), (pat.pattern, c.text)


# ---------------------------------------------------------------- delays
DELAY = {
    "_id": {"$oid": "6a1e0aaaa063c72985bac8c5"}, "id": "a89f7bcd-d1be-4268-879e-92336f03260f",
    "tenant_id": T1, "project_id": P1, "daily_report_id": "641adc57",
    "date": "2026-06-01", "severity": "critical", "reason_category": "other",
    "description": "the sprinkler sub did not show iupp today . will cause at least two days delay." + LEAK,
    "linked_task_ids": [TASK], "status": "open", "owner_attribution": "sub",
    "photo_urls": ["s3://bucket/key.jpg"], "resolved_note": None,
    "ai_report": "Narrative: sprinkler crew absent. Contact pm@example.com or 212-555-1234.",
    "reported_by": "8da206ae", "reported_at": "2026-06-01T22:41:46.625775+00:00",
}


def test_delay_header_and_metadata():
    res = run(issues.chunk_delay(ctx(), DELAY))
    assert res.tenant_id == T1 and res.project_id == P1 and res.source_id == DELAY["id"]
    kinds = [c.kind for c in res.chunks]
    assert kinds == ["delay", "delay_ai_report"]
    c = res.chunks[0]
    assert c.header.startswith("VA 6B | Delay 2026-06-01 | critical other")
    assert "activities 22" in c.header and "status open" in c.header
    assert c.date == "2026-06-01" and c.activity_code == ["22"] and c.trade_code == ["FP"]
    assert c.status == "open" and c.url == "/delays/" + DELAY["id"]
    assert c.title.startswith("Delay 2026-06-01 - the sprinkler sub")
    assert "sprinkler sub did not show" in c.body
    assert c.allowed_roles is None and c.private is False
    assert_clean(res)
    assert "s3://" not in "".join(x.text for x in res.chunks)
    assert "[file]" in c.body and "[amount]" in c.body and "[phone]" in c.body
    # Every change to a delay refreshes its recovery scenarios (mitigations).
    assert res.requeue == [("mitigations", {"tenant_id": T1, "linked_delay_id": DELAY["id"]})]


def test_delay_deleted_and_orphan():
    mit = [("mitigations", {"tenant_id": T1, "linked_delay_id": DELAY["id"]})]
    res = run(issues.chunk_delay(ctx(), {**DELAY, "status": "deleted"}))
    # A soft-deleted delay still re-queues its mitigations so they drop out.
    assert res.chunks == [] and res.requeue == mit
    res = run(issues.chunk_delay(ctx(), {**DELAY, "deleted_at": "2026-06-05"}))
    assert res.chunks == [] and res.requeue == mit
    res = run(issues.chunk_delay(ctx(), {**DELAY, "project_id": "gone"}))
    assert res.chunks == [] and res.requeue == mit
    # project belongs to another company -> orphan
    res = run(issues.chunk_delay(ctx(), {**DELAY, "tenant_id": "other"}))
    assert res.chunks == [] and res.requeue == [
        ("mitigations", {"tenant_id": "other", "linked_delay_id": DELAY["id"]})]


def test_deleted_project_gives_no_chunks():
    for marker in ({"status": "deleted"}, {"deleted_at": "2026-06-05T00:00:00+00:00"}):
        gone = ctx({"projects": [{**BASE["projects"][0], **marker}]})
        for fn, doc in ((issues.chunk_delay, DELAY), (issues.chunk_rfi, RFI),
                        (issues.chunk_submittal, SUB), (issues.chunk_coordination_event, COORD),
                        (issues.chunk_work_item, WI), (issues.chunk_change_order, CO),
                        (issues.chunk_kb_entry, {**KB, "project_id": P1}),
                        (issues.chunk_master_doc, MD)):
            res = run(fn(gone, doc))
            assert res.chunks == [], fn.__name__
            assert_clean(res)


def test_soft_deleted_task_codes_are_dropped():
    for marker in ({"status": "deleted"}, {"deleted_at": "2026-06-05"}):
        c2 = ctx({"tasks": [{**BASE["tasks"][0], **marker}]})
        for fn, doc in ((issues.chunk_delay, DELAY), (issues.chunk_rfi, RFI),
                        (issues.chunk_submittal, SUB),
                        (issues.chunk_coordination_event, COORD), (issues.chunk_work_item, WI)):
            res = run(fn(c2, doc))
            assert res.chunks, fn.__name__
            for ch in res.chunks:
                assert ch.activity_code == [], fn.__name__
                assert "activities 22" not in ch.header, fn.__name__
            assert_clean(res)
        res = run(issues.chunk_change_order(c2, {**CO, "schedule_activity_id": TASK}))
        assert res.chunks[0].activity_code == [] and res.chunks[0].trade_code == []


def test_delay_paperwork_blocker_label():
    res = run(issues.chunk_delay(ctx(), {**DELAY, "source": "paperwork_blocker", "ai_report": None}))
    assert "paperwork blocker (automatic)" in res.chunks[0].header
    assert len(res.chunks) == 1
    assert_clean(res)


def test_long_delay_splits():
    long = "\n".join(["Paragraph %d " % i + "word " * 200 for i in range(10)])
    res = run(issues.chunk_delay(ctx(), {**DELAY, "description": long, "ai_report": None}))
    assert len(res.chunks) >= 2
    assert all(c.header == res.chunks[0].header for c in res.chunks)
    assert all(len(c.body) <= issues.MAX_BODY_CHARS for c in res.chunks)
    assert_clean(res)


# ---------------------------------------------------------------- rfis
RFI = {
    "id": "rfi-1", "tenant_id": T1, "project_id": P1, "rfi_number": "7",
    "subject": "Stair rail height", "description": "Drawing A-501 shows 34in, spec says 36in." + LEAK,
    "trade_code": "MISC", "to_party": "Architect", "from_party": "ICE",
    "spec_section_refs": ["05 52 00"], "date_initiated": "2026-06-03",
    "response_text": "Use 36in. Questions: arch@firm.com." + LEAK, "status": "answered",
    "priority": "high", "cost_impact_estimated_usd": 12500.0, "linked_task_ids": [TASK],
}


def test_rfi():
    res = run(issues.chunk_rfi(ctx(), RFI))
    c = res.chunks[0]
    assert c.kind == "rfi"
    assert c.header.startswith("VA 6B | RFI #7 Stair rail height 2026-06-03")
    assert "spec 05 52 00" in c.header and "trade MISC" in c.header
    assert "Response: Use 36in." in c.body and "Sent from ICE to Architect" in c.body
    assert c.trade_code == ["MISC"] and c.activity_code == ["22"]
    assert c.title == "RFI #7 - Stair rail height" and c.url == "/rfis?openId=rfi-1"
    assert "12500" not in c.text
    assert_clean(res)
    assert "[amount]" in c.body and "[phone]" in c.body and "[file]" in c.body
    res = run(issues.chunk_rfi(ctx(), {**RFI, "status": "deleted"}))
    assert res.chunks == []
    assert_clean(res)


# ---------------------------------------------------------------- submittals
SUB = {
    "id": "sub-1", "tenant_id": T1, "project_id": P1, "submittal_number": "SUB-0002",
    "title": "Fire-rated wall assembly", "type": "product_data", "status": "revise_resubmit",
    "spec_section_code": "09 21 16", "discipline": "Drywall", "sub_responsible": "Acme Drywall",
    "schedule_activity_id": TASK, "revision_number": 1, "sla_status": "breached",
    "sla_breach_reason": "A/E reviewer out." + LEAK, "submitted_at": "2026-06-01T12:00:00+00:00",
    "created_at": "2026-05-20T00:00:00+00:00", "days_in_review": 14,
}


def test_submittal_with_review_notes():
    res = run(issues.chunk_submittal(ctx(), SUB))
    c = res.chunks[0]
    assert c.header.startswith("VA 6B | Submittal SUB-0002 rev 1 Fire-rated wall assembly 2026-06-01")
    assert "spec 09 21 16" in c.header and "sub Acme Drywall" in c.header
    assert "SLA breached" in c.header
    assert "2026-06-10 returned to contractor: Architect wants" in c.body
    assert "Late because: A/E reviewer out" in c.body and "[amount]" in c.body
    assert c.trade_code == ["Drywall"] and c.activity_code == ["22"]
    assert c.url == "/submittals?openId=sub-1"
    assert_clean(res)


def test_review_event_requeues_parent():
    ev = BASE["submittal_review_events"][0]
    res = run(issues.chunk_submittal_review_event(ctx(), ev))
    assert res.chunks == [] and res.requeue == [("submittals", {"tenant_id": T1, "id": "sub-1"})]
    assert res.project_id == P1
    assert_clean(res)
    # no note -> nothing to roll up
    res = run(issues.chunk_submittal_review_event(ctx(), BASE["submittal_review_events"][1]))
    assert res.requeue == []
    # backup orphan: tenant 3965249c no longer exists, submittal missing
    orphan = {"id": "88f03b6f", "tenant_id": "3965249c", "project_id": "5e8b8cee",
              "submittal_id": "47a818a6", "event_type": "submitted_to_ae",
              "performed_at": "2026-05-12T22:37:23+00:00", "notes": "x"}
    res = run(issues.chunk_submittal_review_event(ctx(), orphan))
    assert res.chunks == [] and res.requeue == []


# ---------------------------------------------------------------- coordination
COORD = {
    "id": "co-1", "tenant_id": T1, "project_id": P1,
    "occurred_at": "2026-06-04T15:00:00+00:00", "channel": "phone", "direction": "outbound",
    "other_party_name": "Dana Lee", "other_party_org": "VA Engineering", "other_party_role": "COR",
    "other_party_email": "dana.lee@va.gov", "other_party_phone": "(718) 555-0101",
    "topic": "tenant_access", "summary": "Asked for access to 6A panel",
    "detail_text": "VA will decide by Friday. Reach me at 718.555.0101 or dana.lee@va.gov." + LEAK,
    "outcome": "pending", "outcome_notes": "Follow up Friday", "linked_task_ids": [TASK],
    "deleted_at": None,
}


def test_coordination():
    res = run(issues.chunk_coordination_event(ctx(), COORD))
    c = res.chunks[0]
    assert c.header.startswith("VA 6B | Coordination phone outbound 2026-06-04")
    assert "with Dana Lee (VA Engineering, COR)" in c.header
    assert "topic tenant access" in c.header and "outcome pending" in c.header
    assert "Asked for access to 6A panel" in c.body and "Outcome: Follow up Friday" in c.body
    assert c.url == "/coordination/co-1" and c.activity_code == ["22"]
    assert "va.gov" not in c.text and "0101" not in c.text
    assert_clean(res)
    assert "[amount]" in c.body and "[file]" in c.body
    assert run(issues.chunk_coordination_event(ctx(), {**COORD, "deleted_at": "2026-06-05"})).chunks == []


def test_coordination_legacy_topic_fallback():
    legacy = {**COORD, "topic": "elevator shutdown schedule", "summary": None, "detail_text": None,
              "outcome_notes": None}
    res = run(issues.chunk_coordination_event(ctx(), legacy))
    c = res.chunks[0]
    assert c.body.startswith("elevator shutdown schedule")
    assert_clean(res)
    assert "topic other" in c.header


# ---------------------------------------------------------------- work items
WI = {
    "id": "wi-1", "tenant_id": T1, "project_id": P1, "location_id": "loc1",
    "title": "Leak at riser", "description": "Water at the 6th floor riser after test.",
    "status": "resolved", "priority": "urgent", "root_cause": "Loose coupling",
    "closure_summary": "Coupling replaced." + LEAK, "estimated_cost_impact": 4000.0,
    "actual_cost_impact": 3500.0, "opened_at": "2026-06-02T10:00:00+00:00",
}


def test_work_item_and_link():
    res = run(issues.chunk_work_item(ctx(), WI))
    c = res.chunks[0]
    assert c.header.startswith("VA 6B | Field issue Leak at riser 2026-06-02")
    assert "location Level 6 East" in c.header and "activities 22" in c.header
    assert "Root cause: Loose coupling" in c.body and "Resolution: Coupling replaced" in c.body
    assert "Link note: Blocks sprinkler rough-in" in c.body
    assert "3500" not in c.text and "4000" not in c.text
    assert c.url == "/work-items?openId=wi-1"
    assert_clean(res)
    assert "[amount]" in c.body and "[phone]" in c.body and "[file]" in c.body
    assert run(issues.chunk_work_item(ctx(), {**WI, "status": "deleted"})).chunks == []

    link = BASE["work_item_links"][0]
    res = run(issues.chunk_work_item_link(ctx(), link))
    assert res.chunks == [] and res.requeue == [("work_items", {"tenant_id": T1, "id": "wi-1"})]
    assert_clean(res)
    assert res.project_id == P1 and res.tenant_id == T1
    res = run(issues.chunk_work_item_link(ctx(), {**link, "work_item_id": "missing"}))
    assert res.requeue == [] and res.chunks == []


# ---------------------------------------------------------------- change orders
CO = {
    "id": "c-1", "tenant_id": T1, "project_id": P1, "co_number": "CO-003",
    "title": "Added fire dampers", "description": "Owner directed two extra dampers." + LEAK,
    "type": "additive", "originating_rfi_id": "rfi-1", "amount_usd": 18750.5,
    "time_extension_days": 3, "status": "proposed", "proposed_at": "2026-06-06T00:00:00+00:00",
}


def test_change_order():
    res = run(issues.chunk_change_order(ctx(), CO))
    c = res.chunks[0]
    assert c.header.startswith("VA 6B | Change order CO-003 Added fire dampers 2026-06-06")
    assert "time extension 3 days" in c.header and "from RFI #7" in c.header
    assert "18750" not in c.text and c.url == "/change-orders?openId=c-1"
    assert "[amount]" in c.body
    assert_clean(res)
    assert run(issues.chunk_change_order(ctx(), {**CO, "status": "deleted"})).chunks == []


# ---------------------------------------------------------------- kb
KB = {
    "id": "kb-1", "tenant_id": T1, "project_id": None, "title": "Order dampers early",
    "body": "Fire dampers took 8 weeks.\nIMPACT: cost=$3500, time=4d\nSee https://files.example.com/x\n" + LEAK,
    "tags": ["procurement"], "source_kind": "manual", "created_at": "2026-06-07T00:00:00+00:00",
}


def test_kb_entry():
    res = run(issues.chunk_kb_entry(ctx(), KB))
    c = res.chunks[0]
    assert res.project_id is None
    assert c.header.startswith("Company-wide | Lesson learned Order dampers early 2026-06-07")
    assert "Fire dampers took 8 weeks." in c.body
    assert c.url == "/kb?openId=kb-1"
    assert_clean(res)
    # imported copies of RFIs / field issues are not indexed again
    assert run(issues.chunk_kb_entry(ctx(), {**KB, "source_kind": "work_item"})).chunks == []
    # company gone -> orphan
    assert run(issues.chunk_kb_entry(ctx(), {**KB, "tenant_id": "gone"})).chunks == []
    # project-level entry uses the project name
    res = run(issues.chunk_kb_entry(ctx(), {**KB, "project_id": P1}))
    assert res.project_id == P1 and res.chunks[0].header.startswith("VA 6B |")
    assert_clean(res)


# ---------------------------------------------------------------- master docs
MD = {
    "id": "masterdoc_" + P1, "tenant_id": T1, "project_id": P1,
    "construction_type": "renovation", "delivery_method": "design_bid_build",
    "site_constraints": ["Occupied hospital", "No work during patient transport"],
    "abatement_required": True, "govt_security_level": "public",
    "primary_subs": ["Acme Drywall"], "notes": "Badge office opens 7am." + LEAK,
    "updated_at": "2026-06-08T00:00:00+00:00",
}


def test_master_doc():
    res = run(issues.chunk_master_doc(ctx(), MD))
    c = res.chunks[0]
    assert c.header.startswith("VA 6B | Master document 2026-06-08 | renovation design bid build")
    assert "Site constraints: Occupied hospital" in c.body and "Abatement required" in c.body
    assert c.url == "/master-doc"
    assert_clean(res)
    for level in ("fouo", "secret", "ts", None):
        assert run(issues.chunk_master_doc(ctx(), {**MD, "govt_security_level": level})).chunks == []


# ---------------------------------------------------------------- registry
def test_entries_shape():
    names = [e.collection for e in issues.ENTRIES]
    assert len(names) == len(set(names))
    for e in issues.ENTRIES:
        assert e.decision in ("index", "query_only", "exclude") and e.reason
        assert (e.chunker is not None) == (e.decision == "index")


def test_scrub():
    s = issues.scrub("call (212) 555-1234 or a.b@c.com, see www.x.com/y; date 2026-06-01")
    assert "555" not in s and "@" not in s and "www" not in s and "2026-06-01" in s
    # Cases the field chunker already masked and this one used to miss.
    assert issues.scrub("CO total $18,750.50 approved") == "CO total [amount] approved"
    assert issues.scrub("call 2125551234") == "call [phone]"
    assert issues.scrub("call 212-5551234") == "call [phone]"
    assert issues.scrub("see s3://bucket/tenants/x/y.pdf") == "see [file]"
    assert issues.scrub("tenants/abc/obj-1.pdf") == "[file]"
    assert issues.scrub("pin 40.712345, -73.987654") == "pin [location]"
    assert "2.5" not in issues.scrub("about US$ 2.5m")
    # Ordinary construction text is left alone.
    keep = "Drawing A-501, spec 05 52 00, 2026-06-01, 34in rail, 6th floor"
    assert issues.scrub(keep) == keep
