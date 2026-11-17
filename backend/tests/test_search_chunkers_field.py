"""Fixture tests for the field-group search chunkers (no database needed).

Sample rows are shaped like the real backup (extended JSON numbers kept where
the backup has them). A fake ChunkContext answers parent lookups from canned
dictionaries.
"""
import asyncio
import os
import re

import pytest

from services.search.chunkers import field
from services.search.types import ChunkContext, ChunkResult

TENANT = "36cba348-98a8-4f85-857b-3f528616e505"
PROJECT = "ce400cc8-4513-4874-bc63-d481241c1e20"
REPORT = "641adc57-856d-4ca5-8c55-ea9414832106"
TASK = "828ed3f4-8531-4c9c-a50e-7915043d2d38"


class FakeCtx(ChunkContext):
    """ChunkContext whose lookups come from canned rows instead of Mongo."""

    def __init__(self, rows: dict):
        super().__init__(db=None)
        self.rows = rows  # {collection: [docs]}

    async def one(self, collection, filt, projection=None):
        for d in self.rows.get(collection, []):
            if all(d.get(k) == v for k, v in filt.items()):
                return d
        return None

    async def many(self, collection, filt):
        return [d for d in self.rows.get(collection, [])
                if all(d.get(k) == v for k, v in filt.items())]


def base_rows(**extra):
    rows = {
        "projects": [{"id": PROJECT, "name": "VA 6B", "tenant_id": TENANT}],
        "daily_reports": [{
            "id": REPORT, "tenant_id": TENANT, "project_id": PROJECT,
            "report_date": "2026-06-01", "status": "draft",
        }],
        "tasks": [{"id": TASK, "tenant_id": TENANT, "activity_code": "21", "trade_code": "MECH",
                   "status": "in_progress"}],
        "daily_presence": [], "daily_report_attendance": [],
    }
    for k, v in extra.items():
        rows[k] = v
    return rows


def run(coro):
    return asyncio.run(coro)


FORBIDDEN = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                 # email
    re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"),      # phone
    re.compile(r"\$\s?\d"),                                  # money
    re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}"),  # GPS pair
    re.compile(r"ice-schedule-ops/"),                        # storage key
    re.compile(r"https?://"),                                # links
]


def assert_clean(res: ChunkResult):
    for c in res.chunks:
        for rx in FORBIDDEN:
            assert not rx.search(c.text), (rx.pattern, c.text)


# ---------------------------------------------------------------- daily_reports

DR_DOC = {
    "_id": {"$oid": "6a1e037ca063c72985bac8a2"}, "project_id": PROJECT, "report_date": "2026-06-01",
    "weather_temp_high_f": {"$numberDouble": "72.7"}, "weather_temp_low_f": {"$numberDouble": "55.6"},
    "weather_conditions": "overcast", "weather_precipitation_in": {"$numberInt": "0"},
    "notes": "All other subs that were supposed to be working today did not show up. "
             "Call Joe at 718-555-1234 or joe@example.com about the $4,500 change.",
    "id": REPORT, "tenant_id": TENANT, "status": "draft",
    "prepared_by": "8da206ae-f25b-4fb0-bc01-8e5680030e1a",
}


def test_daily_report_header_metadata_and_scrub():
    res = run(field.chunk_daily_report(FakeCtx(base_rows()), DR_DOC))
    assert (res.tenant_id, res.project_id, res.source_id) == (TENANT, PROJECT, REPORT)
    assert len(res.chunks) == 1
    c = res.chunks[0]
    assert c.kind == "daily_report"
    assert c.header == "VA 6B | Daily report 2026-06-01 | status draft | weather overcast 73/56F"
    assert "did not show up" in c.body
    assert c.date == "2026-06-01" and c.status == "draft"
    assert c.url == f"/daily-reports?openId={REPORT}"
    assert c.title == "Daily report 2026-06-01"
    assert "8da206ae" not in c.text
    assert_clean(res)


def test_daily_report_weather_only_makes_no_chunk_and_orphan_project():
    doc = dict(DR_DOC, notes=None)
    assert run(field.chunk_daily_report(FakeCtx(base_rows()), doc)).chunks == []
    orphan = run(field.chunk_daily_report(FakeCtx(base_rows(projects=[])), DR_DOC))
    assert orphan.chunks == []


def test_long_notes_split_on_voice_markers():
    long = "\n".join(f"[Voice intake {i}] " + ("ductwork rough-in progressing " * 60) for i in range(6))
    res = run(field.chunk_daily_report(FakeCtx(base_rows()), dict(DR_DOC, notes=long)))
    assert len(res.chunks) > 1
    assert all(len(c.body) <= field.MAX_BODY_CHARS for c in res.chunks)
    assert all(c.header.startswith("VA 6B | Daily report 2026-06-01") for c in res.chunks)


def test_manpower_rollup_includes_presence_and_attendance_notes_not_names():
    presence = [
        {"id": "p1", "tenant_id": TENANT, "daily_report_id": REPORT, "project_id": PROJECT,
         "trade": "MECH", "company_name": "Northeast Mechanical LLC", "schedule_activity_id": TASK,
         "headcount": {"$numberInt": "4"}, "hours_per_person": {"$numberDouble": "8.0"},
         "notes": "Started ductwork rough-in, finished about 40 feet", "created_at": "2026-06-01T10:00:00"},
        {"id": "p2", "tenant_id": TENANT, "daily_report_id": REPORT, "project_id": PROJECT,
         "trade": "Acoustical Ceilings", "company_name": "", "headcount": {"$numberInt": "0"},
         "hours_per_person": {"$numberDouble": "8.0"}, "notes": "", "created_at": "2026-06-01T11:00:00"},
        {"id": "p3", "tenant_id": TENANT, "daily_report_id": REPORT, "project_id": PROJECT,
         "trade": "ELEC", "company_name": "Sparks Inc", "headcount": 2, "hours_per_person": 8,
         "notes": "Auto-created from activity grid crew dropdown", "created_at": "2026-06-01T12:00:00"},
    ]
    attendance = [{"id": "a1", "tenant_id": TENANT, "daily_report_id": REPORT,
                   "name": "Maria Gonzalez", "role": "foreman", "notes": "Left early for a doctor visit",
                   "hours_on_site": 4}]
    ctx = FakeCtx(base_rows(daily_presence=presence, daily_report_attendance=attendance))
    res = run(field.chunk_daily_report(ctx, dict(DR_DOC, notes=None)))
    assert len(res.chunks) == 1
    mp = res.chunks[0]
    assert mp.kind == "manpower"
    assert mp.header == "VA 6B | Daily report 2026-06-01 | Manpower"
    assert "MECH - Northeast Mechanical LLC - 4 x 8h - Started ductwork rough-in" in mp.body
    assert "ELEC - Sparks Inc - 2 x 8h" in mp.body
    assert "Auto-created" not in mp.body
    assert "Acoustical" not in mp.body  # empty placeholder row
    assert "Left early" in mp.body
    assert "Maria" not in mp.text and "Gonzalez" not in mp.text
    assert mp.activity_code == ["21"] and mp.trade_code == ["MECH", "ELEC"]
    assert mp.parent_id == REPORT and mp.date == "2026-06-01"


def test_presence_and_attendance_requeue_parent_report():
    p = {"id": "p1", "tenant_id": TENANT, "daily_report_id": REPORT, "project_id": PROJECT, "trade": "MECH"}
    res = run(field.chunk_daily_presence(FakeCtx(base_rows()), p))
    assert res.chunks == [] and res.requeue == [("daily_reports", {"id": REPORT})]
    assert (res.tenant_id, res.project_id) == (TENANT, PROJECT)

    a = {"id": "a1", "tenant_id": TENANT, "daily_report_id": REPORT, "name": "Maria Gonzalez", "notes": "x"}
    res = run(field.chunk_attendance(FakeCtx(base_rows()), a))
    assert res.chunks == [] and res.requeue == [("daily_reports", {"id": REPORT})]
    assert res.project_id == PROJECT
    orphan = run(field.chunk_attendance(FakeCtx(base_rows(daily_reports=[])), a))
    assert orphan.requeue == [] and orphan.chunks == []


# ---------------------------------------------------- daily_report_activities

ACT_DOC = {
    "_id": {"$oid": "6a1e037ca063c72985bac8a6"}, "id": "fb567e43-54ea-44cd-8f78-ea4bf4670dff",
    "daily_report_id": REPORT, "schedule_activity_id": TASK, "position": {"$numberInt": "3"},
    "pct_complete_start_of_day": {"$numberDouble": "0.0"}, "pct_complete_end_of_day": {"$numberDouble": "34.0"},
    "crew_count": {"$numberInt": "0"}, "hours_worked": {"$numberInt": "0"}, "delay_reason": None,
    "notes": "Started the ductwork rough-in and was able to finish about 40 feet",
    "status_at_end_of_day": "in_progress", "activity_code": "21",
    "activity_description": "MEP Rough-in — HVAC Ductwork & VAVs", "trade_code": None,
}


def test_activity_scope_from_parent_and_header():
    res = run(field.chunk_dr_activity(FakeCtx(base_rows()), ACT_DOC))
    assert (res.tenant_id, res.project_id) == (TENANT, PROJECT)
    c = res.chunks[0]
    assert c.kind == "dr_activity"
    assert c.header == ("VA 6B | Daily report 2026-06-01 | Activity 21 MEP Rough-in — HVAC Ductwork & VAVs"
                        " | trade MECH | 0%->34% | in progress")
    assert c.activity_code == ["21"] and c.trade_code == ["MECH"]
    assert c.status == "in_progress" and c.parent_id == REPORT and c.date == "2026-06-01"
    assert c.url == f"/daily-reports?openId={REPORT}&openEvent=21&openEventType=dr"
    assert_clean(res)


def test_activity_orphan_and_empty_notes():
    orphan = run(field.chunk_dr_activity(FakeCtx(base_rows(daily_reports=[])), ACT_DOC))
    assert orphan.chunks == [] and orphan.tenant_id is None
    empty = run(field.chunk_dr_activity(FakeCtx(base_rows()), dict(ACT_DOC, notes=None)))
    assert empty.chunks == [] and empty.tenant_id == TENANT
    delay = run(field.chunk_dr_activity(FakeCtx(base_rows()),
                                         dict(ACT_DOC, notes=None, delay_reason="Waiting on duct delivery")))
    assert delay.chunks[0].body == "Delay: Waiting on duct delivery"


# ----------------------------------------------- deliveries / safety / qc

def test_delivery_chunk_and_soft_delete():
    doc = {"id": "d1", "tenant_id": TENANT, "project_id": PROJECT, "delivery_date": "2026-06-02",
           "vendor": "Ferguson", "description": "Spiral duct, 40 lf, staged on 2nd floor",
           "po_number": "PO-1188", "status": "received", "received_by": "Tom Smith",
           "linked_task_ids": [TASK], "daily_report_id": REPORT}
    res = run(field.chunk_delivery(FakeCtx(base_rows()), doc))
    c = res.chunks[0]
    assert c.header == "VA 6B | Delivery 2026-06-02 | Ferguson | PO PO-1188 | received | activities 21"
    assert "Tom" not in c.text
    assert c.activity_code == ["21"] and c.date == "2026-06-02"
    assert c.url == f"/daily-reports?openId={REPORT}&openEvent=d1&openEventType=delivery"
    assert run(field.chunk_delivery(FakeCtx(base_rows()), dict(doc, status="deleted"))).chunks == []
    no_dr = run(field.chunk_delivery(FakeCtx(base_rows()), dict(doc, daily_report_id=None)))
    assert no_dr.chunks[0].url == "/deliveries"


def test_event_chunks_are_not_children_of_their_report():
    """Deleting a report must not wipe the search entries of events filed on
    it, so their chunks carry no parent_id; a dead report link falls back to
    the list page."""
    doc = {"id": "d1", "tenant_id": TENANT, "project_id": PROJECT, "delivery_date": "2026-06-02",
           "vendor": "Ferguson", "description": "Spiral duct staged on 2nd floor",
           "daily_report_id": REPORT}
    c = run(field.chunk_delivery(FakeCtx(base_rows()), doc)).chunks[0]
    assert c.parent_id is None
    assert c.url == f"/daily-reports?openId={REPORT}&openEvent=d1&openEventType=delivery"
    dead = run(field.chunk_delivery(FakeCtx(base_rows()), dict(doc, daily_report_id="gone"))).chunks[0]
    assert dead.parent_id is None and dead.url == "/deliveries"
    # Supplement linked only through parent_daily_report_id still links while the report lives.
    supp = run(field.chunk_delivery(FakeCtx(base_rows()),
                                    dict(doc, daily_report_id=None, parent_daily_report_id=REPORT))).chunks[0]
    assert supp.parent_id is None and supp.url.startswith(f"/daily-reports?openId={REPORT}")
    # A report of another company is treated as dead.
    other = base_rows(daily_reports=[{"id": REPORT, "tenant_id": "other", "project_id": PROJECT,
                                      "report_date": "2026-06-01"}])
    assert run(field.chunk_delivery(FakeCtx(other), doc)).chunks[0].url == "/deliveries"

    s = {"id": "s1", "tenant_id": TENANT, "project_id": PROJECT, "date": "2026-06-03",
         "description": "Near miss on ladder", "daily_report_id": "gone"}
    sc = run(field.chunk_safety_event(FakeCtx(base_rows()), s)).chunks[0]
    assert sc.parent_id is None and sc.url == "/safety-events"
    q = {"id": "q1", "tenant_id": TENANT, "project_id": PROJECT, "date": "2026-06-03",
         "description": "Duct seams not sealed", "daily_report_id": REPORT}
    qc = run(field.chunk_qc_issue(FakeCtx(base_rows()), q)).chunks[0]
    assert qc.parent_id is None and qc.url == f"/daily-reports?openId={REPORT}&openEvent=q1&openEventType=qc"
    ph = run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[TRAY])), PHOTO)).chunks[0]
    assert ph.parent_id is None
    ph_dead = run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[TRAY])),
                                    dict(PHOTO, daily_report_id="gone"))).chunks[0]
    assert ph_dead.url == "/photos"


def test_report_children_keep_parent_id():
    """dr_activity, manpower and photo_review chunks really belong to the
    report: the worker's child purge on a report delete relies on this."""
    act = run(field.chunk_dr_activity(FakeCtx(base_rows()), ACT_DOC)).chunks[0]
    assert act.parent_id == REPORT
    rv = run(field.chunk_photo_review(FakeCtx(base_rows()), REVIEW)).chunks[0]
    assert rv.parent_id == REPORT


def test_lookups_never_cross_companies():
    """A task or tray row of another company must never feed this chunk."""
    foreign_task = [{"id": TASK, "tenant_id": "other-co", "activity_code": "99", "trade_code": "XX"}]
    doc = {"id": "d1", "tenant_id": TENANT, "project_id": PROJECT, "delivery_date": "2026-06-02",
           "vendor": "Ferguson", "description": "Spiral duct", "linked_task_ids": [TASK],
           "schedule_activity_id": TASK}
    c = run(field.chunk_delivery(FakeCtx(base_rows(tasks=foreign_task)), doc)).chunks[0]
    assert c.activity_code == [] and "99" not in c.header

    s = {"id": "s1", "tenant_id": TENANT, "project_id": PROJECT, "description": "Near miss on ladder",
         "linked_task_ids": [TASK]}
    assert run(field.chunk_safety_event(FakeCtx(base_rows(tasks=foreign_task)), s)).chunks[0].activity_code == []

    act = run(field.chunk_dr_activity(FakeCtx(base_rows(tasks=foreign_task)), dict(ACT_DOC, trade_code=None)))
    assert act.chunks[0].trade_code == []

    foreign_tray = dict(TRAY, tenant_id="other-co")
    ph = run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[foreign_tray], tasks=foreign_task)),
                               dict(PHOTO, caption="Ceiling grid at room 204")))
    assert "AI caption" not in ph.chunks[0].body and ph.chunks[0].activity_code == []

    # Activity code through daily_report_activity_id: only when that activity's
    # report belongs to the same company.
    other_report = {"id": "r-other", "tenant_id": "other-co", "project_id": "p-other",
                    "report_date": "2026-06-01"}
    acts = [{"id": "act-x", "daily_report_id": "r-other", "activity_code": "77"},
            {"id": "act-y", "daily_report_id": REPORT, "activity_code": "21"}]
    rows = base_rows(daily_report_activities=acts, tasks=[])
    rows["daily_reports"].append(other_report)
    photo = dict(PHOTO, caption="Ceiling grid at room 204", tray_photo_id=None, schedule_activity_id=None)
    bad = run(field.chunk_photo(FakeCtx(rows), dict(photo, daily_report_activity_id="act-x")))
    assert bad.chunks[0].activity_code == []
    good = run(field.chunk_photo(FakeCtx(rows), dict(photo, daily_report_activity_id="act-y")))
    assert good.chunks[0].activity_code == ["21"]


def test_photo_user_override_keeps_person_caption_over_ai():
    tray = dict(TRAY, ai_user_overridden=True)
    own = run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[tray])),
                                dict(PHOTO, caption="Ductwork hung in corridor B")))
    body = own.chunks[0].body
    assert body.startswith("Ductwork hung in corridor B")
    assert "AI caption" not in body
    assert "AI safety concerns: exposed wiring overhead" in body  # concerns still kept
    # Overridden but the person left no caption of their own: the AI caption is used.
    blank = run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[tray])), PHOTO))
    assert "AI caption: Overhead ductwork" in blank.chunks[0].body


def test_safety_and_qc_chunks():
    s = {"id": "s1", "tenant_id": TENANT, "project_id": PROJECT, "date": "2026-06-03",
         "description": "Near miss: ladder slipped on wet floor, no injury", "severity": "moderate",
         "event_type": "near_miss", "status": "open", "linked_task_ids": [TASK], "daily_report_id": REPORT}
    res = run(field.chunk_safety_event(FakeCtx(base_rows()), s))
    c = res.chunks[0]
    assert c.header == "VA 6B | Safety near miss 2026-06-03 | moderate | open | activities 21"
    assert c.url == f"/daily-reports?openId={REPORT}&openEvent=s1&openEventType=safety"
    assert run(field.chunk_safety_event(FakeCtx(base_rows()), dict(s, deleted_at="2026-06-04"))).chunks == []
    assert run(field.chunk_safety_event(FakeCtx(base_rows(projects=[])), s)).chunks == []

    q = {"id": "q1", "tenant_id": TENANT, "project_id": PROJECT, "date": "2026-06-03",
         "description": "Duct seams not sealed at VAV-3", "severity": "minor", "status": "closed",
         "resolution_notes": "Resealed with mastic", "rework_outcome_notes": "Took half a day",
         "ai_benchmark_basis": "AI says 0.5 days", "linked_task_ids": []}
    res = run(field.chunk_qc_issue(FakeCtx(base_rows()), q))
    c = res.chunks[0]
    assert c.header == "VA 6B | Quality issue 2026-06-03 | minor | closed"
    assert "Resolution: Resealed with mastic" in c.body and "Rework outcome: Took half a day" in c.body
    assert "AI says" not in c.body
    assert c.url == "/qc-issues"


# ------------------------------------------------------------------- photos

TRAY = {"id": "tray1", "tenant_id": TENANT, "project_id": PROJECT,
        "storage_key": "ice-schedule-ops/tray/x/abc.jpg", "exif_gps_lat": 40.712812, "exif_gps_lng": -74.006012,
        "ai_model_version": "claude-sonnet-4-5-20250929", "ai_analysis_status": "complete",
        "ai_suggested_caption": "Overhead ductwork installed on hangers in corridor.",
        "ai_safety_concerns_detected": ["exposed wiring overhead", "debris on floor"],
        "ai_raw_response": {"text": "..."}}

PHOTO = {"id": "ph1", "tenant_id": TENANT, "project_id": PROJECT, "daily_report_id": REPORT,
         "tray_photo_id": "tray1", "tag_caption": None, "daily_report_activity_id": None,
         "storage_path": "ice-schedule-ops/tray/x/abc.jpg", "captured_at": "2026-06-01T22:56:18.098472+00:00",
         "gps_lat": 40.712812, "gps_lng": -74.006012, "photo_class": "progress",
         "schedule_activity_id": TASK, "tags": [], "caption": "aaaa", "rfi_id": "r1"}


def test_photo_joins_tray_caption_and_drops_junk_and_gps():
    res = run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[TRAY])), PHOTO))
    c = res.chunks[0]
    assert c.header == "VA 6B | Photo progress 2026-06-01 | activity 21 | linked RFI"
    assert "aaaa" not in c.body
    assert "AI caption: Overhead ductwork" in c.body
    assert "AI safety concerns: exposed wiring overhead; debris on floor" in c.body
    assert "40.71" not in c.text and "ice-schedule-ops" not in c.text
    assert c.url == f"/daily-reports?openId={REPORT}&openEvent=ph1&openEventType=photo"
    assert_clean(res)


def test_photo_dev_stub_tray_rejected_deleted_and_no_text():
    stub = dict(TRAY, ai_model_version="dev-stub-v1")
    assert run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[stub])), PHOTO)).chunks == []
    rejected = dict(TRAY, ai_analysis_status="rejected_by_user")
    assert run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[rejected])), PHOTO)).chunks == []
    assert run(field.chunk_photo(FakeCtx(base_rows(photo_tray=[TRAY])), dict(PHOTO, status="deleted"))).chunks == []
    own = run(field.chunk_photo(FakeCtx(base_rows()), dict(PHOTO, caption="Ceiling grid at room 204",
                                                             tray_photo_id=None, daily_report_id=None)))
    assert own.chunks[0].body == "Ceiling grid at room 204" and own.chunks[0].url == "/photos"


def test_photo_tray_requeues_linked_photos_only():
    res = run(field.chunk_photo_tray(FakeCtx(base_rows()), TRAY))
    assert res.chunks == [] and res.requeue == [("photos", {"tray_photo_id": "tray1"})]


REVIEW = {"_id": {"$oid": "6a1a85f5e71da815fec4ab06"}, "id": "bc346dcc", "tenant_id": TENANT,
          "daily_report_id": REPORT, "lens": "combined", "severity": "major", "label": "2 safety",
          "text": "The image shows an active interior construction zone with exposed ceiling structure.",
          "safety_findings": [{"issue": "Worker on A-frame ladder without visible fall protection",
                               "severity": "moderate", "osha_code": "1926.1053"}],
          "qc_findings": [], "photo_ids": ["a4cc933e"], "photo_count": {"$numberInt": "1"},
          "created_at": "2026-05-30T06:38:45.083402+00:00", "created_by": "8dba8b1c"}


def test_photo_review_chunk_orphan_and_dev_stub():
    res = run(field.chunk_photo_review(FakeCtx(base_rows()), REVIEW))
    assert (res.tenant_id, res.project_id) == (TENANT, PROJECT)
    c = res.chunks[0]
    assert c.header == "VA 6B | Photo review (combined) report 2026-06-01 | major"
    assert "Safety finding: Worker on A-frame ladder without visible fall protection (OSHA 1926.1053, moderate)" in c.body
    assert c.parent_id == REPORT and c.url == f"/daily-reports?openId={REPORT}"
    assert run(field.chunk_photo_review(FakeCtx(base_rows(daily_reports=[])), REVIEW)).chunks == []
    other_tenant = dict(REVIEW, tenant_id="someone-else")
    assert run(field.chunk_photo_review(FakeCtx(base_rows()), other_tenant)).chunks == []
    stub = dict(REVIEW, text="(Dev stub) made up review")
    assert run(field.chunk_photo_review(FakeCtx(base_rows()), stub)).chunks == []


# ------------------------------------------------------------------ registry

def test_entries_cover_group_with_reasons():
    by = {e.collection: e for e in field.ENTRIES}
    for name in ("daily_reports", "daily_report_activities", "daily_presence", "daily_report_attendance",
                 "deliveries", "safety_events", "qc_issues", "photos", "photo_tray", "photo_reviews",
                 "voice_annotations"):
        assert name in by
    assert by["voice_annotations"].decision == "exclude"
    assert len(by) == len(field.ENTRIES)
    for e in field.ENTRIES:
        assert e.reason
        assert (e.chunker is not None) == (e.decision == "index")


@pytest.mark.parametrize("raw,expected", [
    ("call 718-555-1234", "call [phone]"),
    ("mail a.b@c.com now", "mail [email] now"),
    ("cost was $12,500.00", "cost was [amount]"),
    ("at 40.712812, -74.006012", "at [location]"),
    ("see https://x.y/z", "see [link]"),
    ("spec 09 29 00 on 2026-06-01", "spec 09 29 00 on 2026-06-01"),
])
def test_scrub(raw, expected):
    assert field._scrub(raw) == expected


# ------------------------------------------- real ChunkContext on the test mongod

DB = os.environ.get("DB_NAME", "test_search_field")


def _run_db(coro_fn):
    """Fresh client + clean throwaway database. Exercises the production read
    path (ChunkContext.one with projections, and _many through ctx.db)."""
    if not DB.startswith("test_"):
        pytest.skip("needs a throwaway test database")
    motor = pytest.importorskip("motor.motor_asyncio")

    async def go():
        client = motor.AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017"),
                                          serverSelectionTimeoutMS=3000)
        await client.drop_database(DB)
        try:
            return await coro_fn(client[DB])
        finally:
            await client.drop_database(DB)
            client.close()
    return asyncio.run(go())


async def _seed(db):
    await db.projects.insert_one({"id": PROJECT, "tenant_id": TENANT, "name": "VA 6B", "number": "6B"})
    await db.daily_reports.insert_one({
        "id": REPORT, "tenant_id": TENANT, "project_id": PROJECT, "report_date": "2026-06-01",
        "status": "draft", "notes": None, "prepared_by": "u1"})
    await db.tasks.insert_many([
        {"id": TASK, "tenant_id": TENANT, "activity_code": "21", "trade_code": "MECH", "status": "in_progress"},
        {"id": "task-foreign", "tenant_id": "other-co", "activity_code": "99", "trade_code": "XX"},
    ])
    await db.daily_presence.insert_many([
        {"id": "p1", "tenant_id": TENANT, "project_id": PROJECT, "daily_report_id": REPORT,
         "trade": "MECH", "company_name": "Northeast Mechanical LLC", "schedule_activity_id": TASK,
         "headcount": 4, "hours_per_person": 8.0, "notes": "Started ductwork rough-in",
         "created_at": "2026-06-01T10:00:00"},
        {"id": "p2", "tenant_id": TENANT, "project_id": PROJECT, "daily_report_id": REPORT,
         "trade": "ELEC", "company_name": "Sparks Inc", "schedule_activity_id": "task-foreign",
         "headcount": 2, "hours_per_person": 8, "notes": "", "created_at": "2026-06-01T11:00:00"},
    ])
    await db.daily_report_attendance.insert_one({
        "id": "a1", "tenant_id": TENANT, "daily_report_id": REPORT, "name": "Maria Gonzalez",
        "role": "foreman", "notes": "Left early for a doctor visit"})
    await db.daily_report_activities.insert_one(dict(
        {k: v for k, v in ACT_DOC.items() if k != "_id"}, activity_code=None,
        pct_complete_start_of_day=0.0, pct_complete_end_of_day=34.0))
    await db.photo_tray.insert_many([
        {"id": "tray-stub", "tenant_id": TENANT, "project_id": PROJECT,
         "ai_model_version": "claude-sonnet-4-5", "ai_analysis_status": "complete",
         "ai_suggested_caption": "Made up dev caption about scaffolding.",
         "ai_raw_response": {"_dev_mode": True, "text": "..."}},
        dict(TRAY),
    ])
    await db.photos.insert_one(dict(PHOTO, tray_photo_id="tray-stub", caption="Ceiling grid at room 204"))


@pytest.mark.destructive
def test_real_context_manpower_activity_and_dev_stub_tray():
    from services.search.types import ChunkContext as RealCtx

    async def body(db):
        await _seed(db)
        ctx = RealCtx(db)
        assert not hasattr(ctx, "many")  # _many must take the ctx.db path
        report = await db.daily_reports.find_one({"id": REPORT}, {"_id": 0})
        res = await field.chunk_daily_report(ctx, report)
        assert len(res.chunks) == 1
        mp = res.chunks[0]
        assert mp.kind == "manpower" and mp.date == "2026-06-01"
        assert mp.header == "VA 6B | Daily report 2026-06-01 | Manpower"
        assert "MECH - Northeast Mechanical LLC - 4 x 8h - Started ductwork rough-in" in mp.body
        assert "ELEC - Sparks Inc - 2 x 8h" in mp.body
        assert "Attendance note (foreman): Left early for a doctor visit" in mp.body
        assert "Maria" not in mp.text
        assert mp.activity_code == ["21"]  # the other company's "99" never appears

        act = await db.daily_report_activities.find_one({"id": ACT_DOC["id"]}, {"_id": 0})
        ares = await field.chunk_dr_activity(RealCtx(db), act)
        c = ares.chunks[0]
        assert (ares.tenant_id, ares.project_id) == (TENANT, PROJECT)
        assert c.activity_code == ["21"] and c.trade_code == ["MECH"]  # from the task row
        assert c.header.startswith("VA 6B | Daily report 2026-06-01 | Activity 21")

        photo = await db.photos.find_one({"id": "ph1"}, {"_id": 0})
        pres = await field.chunk_photo(RealCtx(db), photo)
        pc = pres.chunks[0]
        assert pc.body == "Ceiling grid at room 204"  # dev-stub tray text dropped
        assert "scaffolding" not in pc.text
        assert pc.activity_code == ["21"] and pc.date == "2026-06-01"
        assert pc.url == f"/daily-reports?openId={REPORT}&openEvent=ph1&openEventType=photo"

        # The real tray row (not a dev stub) does feed the caption and concerns.
        await db.photos.update_one({"id": "ph1"}, {"$set": {"tray_photo_id": "tray1", "caption": ""}})
        photo = await db.photos.find_one({"id": "ph1"}, {"_id": 0})
        pc2 = (await field.chunk_photo(RealCtx(db), photo)).chunks[0]
        assert "AI caption: Overhead ductwork" in pc2.body
        assert "AI safety concerns: exposed wiring overhead; debris on floor" in pc2.body
    _run_db(body)
