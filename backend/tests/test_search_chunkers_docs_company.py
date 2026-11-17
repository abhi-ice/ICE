"""Fixture tests for the docs_company search chunkers (specs, projects,
vendors, crews, equipment, locations). No database: a fake ChunkContext
hands back canned parent rows. Sample rows follow the shape of the real
backup (contact details and rates included, so we can prove they stay out).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re

import pytest

from services.search.chunkers import docs_company as dc
from services.search.types import ChunkContext, ChunkResult, Entry

TENANT = "3965249c-6912-4ad2-9bc8-083752fc2edc"
PROJECT = "5e8b8cee-1b57-41f9-b289-405e152cdc57"
SPEC = "10f04b65-5196-4f81-803d-fc48100a8adc"


class FakeContext(ChunkContext):
    """ChunkContext whose .one() answers from a dict of canned rows:
    {collection: [row, ...]}; a row matches when every plain (non-operator)
    filter value equals the row's value."""

    def __init__(self, rows: dict[str, list[dict]]):
        super().__init__(db=None)
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    async def one(self, collection, filt, projection=None):
        self.calls.append((collection, dict(filt)))
        for row in self.rows.get(collection, []):
            if all(row.get(k) == v for k, v in filt.items() if not isinstance(v, dict)):
                return dict(row)
        return None


PARENTS = {
    "projects": [{"id": PROJECT, "tenant_id": TENANT, "name": "NYCHA Interior Renovation"}],
    "project_specs": [{"id": SPEC, "tenant_id": TENANT, "project_id": PROJECT,
                       "name": "vol1", "revision_label": "Rev 2",
                       "pdf_url": "ice-schedule-ops/specs/x/67897befd5d04f9abb97dd1173de377c.pdf"}],
    "trade_taxonomy": [{"code": "08", "tenant_id": None, "name": "Openings"}],
    "floors": [{"id": "floor-1", "tenant_id": TENANT, "project_id": PROJECT,
                "floor_name": "Level 3", "floor_number": 3}],
}


def run(chunker, doc, rows=None):
    ctx = FakeContext(PARENTS if rows is None else rows)
    return asyncio.run(chunker(ctx, doc))


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Any US phone form, including space-separated and unbroken 10-digit numbers.
PHONE_RE = re.compile(r"(?:\(\d{3}\)|\b\d{3})[\s.-]?\d{3}[\s.-]?\d{4}\b")


def assert_clean(result: ChunkResult, *forbidden: str):
    for c in result.chunks:
        for value in (c.text, c.title or ""):
            assert not EMAIL_RE.search(value), value
            assert not PHONE_RE.search(value), value
            assert "http" not in value and "ice-schedule-ops" not in value
            for f in forbidden:
                assert f not in value, f"{f!r} leaked into {value!r}"


# ---------------------------------------------------------------------------
# spec_sections
# ---------------------------------------------------------------------------

def _long_spec_text() -> str:
    parts = ["PART 1 - GENERAL", "1.1 SUMMARY",
             "A. This Section includes glass and glazing for interior applications. "
             "Questions go to the architect at arch@example.com or (212) 555-0100."]
    for art in range(2, 10):
        parts.append(f"1.{art} ARTICLE NUMBER {art}")
        for p in range(6):
            parts.append(f"{chr(65 + p)}. Requirement {art}.{p}: install safety glazing at hazardous "
                         f"locations per the building code, labeled and listed for the rating indicated.")
    parts.append("PART 2 - PRODUCTS")
    parts.append("2.1 MANUFACTURERS")
    parts.append("A. " + "Tempered glass from an approved maker. " * 80)  # one very long line
    return "\n".join(parts)


SPEC_SECTION = {
    "id": "5c2446ec-8f8e-4987-b77e-72f8b82ed291",
    "tenant_id": TENANT,
    "project_spec_id": SPEC,
    "division": "08",
    "section_code": "08 80 00",
    "section_title": "GLAZING",
    "parent_section_id": None,
    "hierarchy_path": "08/08 80 00",
    "heading_level": 2,
    "page_start": 362,
    "page_end": 377,
    "text_content": _long_spec_text(),
    "masterformat_year": "2020",
    "created_at": "2026-05-30T04:55:56.230117+00:00",
    "embedding": [0.1] * 50,
    "embedding_kind": "tfidf",
}


def test_spec_section_splits_into_ordered_pieces_with_header_and_metadata():
    res = run(dc.chunk_spec_section, dict(SPEC_SECTION))
    assert res.tenant_id == TENANT and res.project_id == PROJECT
    assert res.source_id == SPEC_SECTION["id"]
    assert len(res.chunks) > 3
    first = res.chunks[0]
    assert first.kind == "spec"
    assert first.header.startswith("NYCHA Interior Renovation | Spec 08 80 00 GLAZING | Division 08 Openings")
    assert "vol1 Rev 2 pages 362-377" in first.header
    assert first.date == "2026-05-30"
    assert first.trade_code == ["08"]
    assert first.parent_id == SPEC
    assert first.url == f"/specs?openId={SPEC}&sectionId={SPEC_SECTION['id']}"
    assert first.title.startswith("Spec 08 80 00 GLAZING")
    assert first.allowed_roles is None and first.private is False
    # Pieces are about 400 tokens (1,600 characters) and come in reading order.
    for c in res.chunks:
        assert len(c.body) <= dc.SPEC_PIECE_CHARS + 50
    joined = "\n".join(c.body for c in res.chunks)
    assert joined.index("1.2 ARTICLE NUMBER 2") < joined.index("1.9 ARTICLE NUMBER 9") < joined.index("PART 2 - PRODUCTS")
    # Later pieces name the article they are in.
    assert any("1.5 ARTICLE NUMBER 5" in c.header for c in res.chunks)
    assert any("2.1 MANUFACTURERS" in c.header for c in res.chunks)
    # The TF-IDF vector and contact details never reach the text.
    assert_clean(res, "embedding", "tfidf", "arch@example.com")


def test_spec_pieces_overlap_when_an_article_is_cut():
    text = "1.1 LONG ARTICLE\n" + "\n".join(f"Line {i} " + "word " * 30 for i in range(40))
    pieces = dc.split_spec_text(text)
    assert len(pieces) > 2
    for a, b in zip(pieces, pieces[1:]):
        last_line_of_a = a["text"].splitlines()[-1]
        assert b["text"].startswith(last_line_of_a) or last_line_of_a in b["text"]
        assert b["article"] == "1.1 LONG ARTICLE"
    # Overlap stays near 15%: the carried-over text is at most the overlap limit.
    shared = pieces[1]["text"].split("\n")
    carried = [ln for ln in shared if ln in pieces[0]["text"].split("\n")]
    assert sum(len(x) + 1 for x in carried) <= dc.SPEC_OVERLAP_CHARS + 1


def test_spec_pieces_break_at_headings():
    text = "\n".join(["PART 1 - GENERAL", "1.1 SUMMARY", "A. " + "x " * 400,
                      "1.2 SUBMITTALS", "A. Product data."])
    pieces = dc.split_spec_text(text)
    assert pieces[-1]["text"].startswith("1.2 SUBMITTALS")
    assert pieces[-1]["article"] == "1.2 SUBMITTALS"
    assert pieces[-1]["part"] == "PART 1 - GENERAL"


def test_short_spec_section_is_one_chunk():
    doc = dict(SPEC_SECTION, text_content="PART 1 - GENERAL\n1.1 SUMMARY\nA. Interior glazing.")
    res = run(dc.chunk_spec_section, doc)
    assert len(res.chunks) == 1
    assert "A. Interior glazing." in res.chunks[0].body


@pytest.mark.parametrize("change", [
    {"project_spec_id": "missing-spec"},   # parent spec gone
    {"tenant_id": "other-tenant"},         # spec belongs to another company
    {"text_content": "", "section_title": ""},  # nothing to say
    {"tenant_id": None},
])
def test_spec_section_orphans_and_empty_rows_give_no_chunks(change):
    res = run(dc.chunk_spec_section, dict(SPEC_SECTION, **change))
    assert isinstance(res, ChunkResult) and res.chunks == []


def test_spec_section_whose_project_was_deleted_gives_no_chunks():
    rows = dict(PARENTS, projects=[])
    res = run(dc.chunk_spec_section, dict(SPEC_SECTION), rows)
    assert res.chunks == [] and res.project_id == PROJECT


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

PROJECT_DOC = {
    "id": PROJECT, "tenant_id": TENANT, "name": "NYCHA Interior Renovation",
    "contract_number": "C-2026-114", "client_name": "NYCHA", "owner_name": "NYC Housing Authority",
    "contractor_name": "ICE", "agency": "HUD", "station_id": None, "project_type": "renovation",
    "contract_value_usd": 14743224.37, "address_line1": "100 Main St", "city": "Bronx", "state": "NY",
    "postal_code": "10451", "status": "active", "contract_type": "lump_sum",
    "ntp_date": "2026-06-15", "current_substantial_completion": "2027-09-30",
    "dr_template_file_url": "https://files.example.test/template.xlsx",
    "ms_project_file_url": "tenants/x/proj.mpp", "ms_project_file_sha": "abc123def456",
    "created_at": "2026-06-01T21:02:10.059807+00:00", "is_cui": False,
}


def test_project_chunk_header_body_and_requeue():
    res = run(dc.chunk_project, dict(PROJECT_DOC))
    assert res.tenant_id == TENANT and res.project_id == PROJECT and res.source_id == PROJECT
    (c,) = res.chunks
    assert c.header == "NYCHA Interior Renovation | Project contract C-2026-114 NTP 2026-06-15 | status active"
    assert "Client: NYCHA" in c.body and "Address: 100 Main St, Bronx, NY 10451" in c.body
    assert "Substantial completion: 2027-09-30" in c.body
    assert c.date == "2026-06-15" and c.status == "active"
    assert c.url == f"/projects?openId={PROJECT}"
    assert_clean(res, "14743224", "14,743,224", "template.xlsx", "proj.mpp", "abc123def456")
    assert ("locations", {"tenant_id": TENANT, "project_id": PROJECT}) in res.requeue
    assert ("crews", {"tenant_id": TENANT, "home_project_id": PROJECT}) in res.requeue


def test_project_with_only_a_name_still_indexes_and_falls_back_to_created_date():
    doc = {"id": "p2", "tenant_id": TENANT, "name": "VA 6B", "contract_number": "@3b4",
           "status": "planning", "created_at": "2026-06-01T21:02:10+00:00"}
    (c,) = run(dc.chunk_project, doc).chunks
    assert c.header == "VA 6B | Project contract @3b4 created 2026-06-01 | status planning"
    assert c.date == "2026-06-01"


@pytest.mark.parametrize("change", [{"name": "", "number": None}, {"status": "deleted"}, {"tenant_id": None}])
def test_project_without_name_or_deleted_gives_no_chunks(change):
    assert run(dc.chunk_project, dict(PROJECT_DOC, **change)).chunks == []


# ---------------------------------------------------------------------------
# subcontractors / suppliers
# ---------------------------------------------------------------------------

SUB = {
    "id": "2a39f3bd-297b-4227-8239-a8f89a605c53", "tenant_id": TENANT, "kind": "subcontractor",
    "name": "GKM Demolition Inc.", "trades": ["demolition", "abatement"],
    "contact_name": "Anthony Rizzo", "contact_email": "arizzo@gkmdemo.com",
    "contact_phone": "(908) 555-0142",
    "notes": "Licensed for NYC + NJ; pre-qual for VA jobs. Call 908-555-0142 or see https://gkm.example.test. "
             "After hours: cell 917 555 1234.",
    "created_at": dt.datetime(2026, 6, 1, 21, 2, 23, tzinfo=dt.timezone.utc),
}


def test_subcontractor_chunk_is_company_level_and_drops_contact_details():
    res = run(dc.chunk_subcontractor, dict(SUB))
    assert res.tenant_id == TENANT and res.project_id is None and res.source_id == SUB["id"]
    (c,) = res.chunks
    assert c.kind == "vendor"
    assert c.header == "Company directory | Subcontractor GKM Demolition Inc. | trades demolition, abatement"
    assert "Contact: Anthony Rizzo" in c.body
    assert "Licensed for NYC + NJ; pre-qual for VA jobs." in c.body
    assert c.trade_code == ["demolition", "abatement"]
    assert c.date == "2026-06-01"
    assert c.url == f"/vendors?tab=subs&openId={SUB['id']}"
    assert c.title == "Subcontractor GKM Demolition Inc."
    assert_clean(res, "arizzo", "555-0142", "555 1234", "gkm.example.test")


def test_supplier_chunk():
    doc = {"id": "s1", "tenant_id": TENANT, "kind": "supplier", "name": "Metro Steel Supply",
           "trades": ["structural-steel", "rebar"], "contact_name": "Mike Lee",
           "contact_email": "mike@metrosteel.com", "contact_phone": "(973) 555-0200",
           "notes": "Net-30; ships from Kearny NJ yard"}
    res = run(dc.chunk_supplier, doc)
    (c,) = res.chunks
    assert c.header.startswith("Company directory | Supplier Metro Steel Supply")
    assert c.url == "/vendors?tab=suppliers&openId=s1"
    assert_clean(res, "mike@", "555-0200")


def test_vendor_without_name_gives_no_chunks():
    assert run(dc.chunk_subcontractor, dict(SUB, name="  ")).chunks == []


# ---------------------------------------------------------------------------
# crews
# ---------------------------------------------------------------------------

CREW = {
    "id": "dcf7f215-967b-46e9-a5ec-d57602eead28", "tenant_id": TENANT, "crew_code": "abd",
    "name": "Electrical Crew", "trade": "electrical", "foreman_person_id": "person-1",
    "home_yard_address": "12 Yard Rd", "hourly_burdened_cost": 187.55, "target_size": 4,
    "notes": "Night shift capable", "default_headcount": 3, "company_name": "Empire Electrical",
    "home_project_id": PROJECT, "status": "active", "created_at": "2026-05-12T15:57:34.929870+00:00",
}


def test_crew_chunk_is_company_level_names_home_project_without_cost():
    res = run(dc.chunk_crew, dict(CREW))
    # Crews are listed to every company user, so the chunk is never scoped to
    # a project (a deleted home project would otherwise hide it for good).
    assert res.project_id is None and res.tenant_id == TENANT
    (c,) = res.chunks
    assert c.header == "NYCHA Interior Renovation | Crew Electrical Crew (abd) | trade electrical status active"
    assert "Night shift capable" in c.body and "Default headcount: 3" in c.body
    assert "Home project: NYCHA Interior Renovation" in c.body
    assert c.trade_code == ["electrical"] and c.status == "active"
    assert c.url == f"/resources/crews?openId={CREW['id']}"
    assert_clean(res, "187", "12 Yard Rd", "person-1")


def test_crew_with_deleted_home_project_stays_company_level():
    res = run(dc.chunk_crew, dict(CREW, home_project_id="gone"))
    assert res.project_id is None
    assert res.chunks[0].header.startswith("Company directory | Crew Electrical Crew")
    assert "Home project" not in res.chunks[0].body


def test_soft_deleted_crew_gives_no_chunks():
    assert run(dc.chunk_crew, dict(CREW, status="deleted")).chunks == []


# ---------------------------------------------------------------------------
# equipment
# ---------------------------------------------------------------------------

EQUIP = {"id": "eq1", "tenant_id": TENANT, "asset_tag": "EX-04", "type": "excavator",
         "manufacturer": "Bobcat", "model": "E35", "year": 2021, "ownership": "owned",
         "hourly_rate": 95.25, "daily_rate": 610.5, "monthly_rate": 9875.0,
         "status": "available", "created_at": "2026-05-12T15:57:34+00:00"}


def test_equipment_chunk_without_rates():
    res = run(dc.chunk_equipment, dict(EQUIP))
    assert res.project_id is None
    (c,) = res.chunks
    assert c.header == "Company directory | Equipment EX-04 excavator | status available"
    assert "Make and model: Bobcat E35 2021" in c.body
    assert c.url == "/resources/equipment?openId=eq1"
    assert_clean(res, "95.25", "610", "9875")


def test_deleted_equipment_gives_no_chunks():
    assert run(dc.chunk_equipment, dict(EQUIP, status="deleted")).chunks == []


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------

LOC = {"id": "loc1", "tenant_id": TENANT, "project_id": PROJECT, "floor_id": "floor-1",
       "name": "Lobby", "code": "L-101", "location_type": "room",
       "polygon_geojson": {"type": "Polygon", "coordinates": [[[40.8571937, -73.9182645]]]},
       "centroid_x": 40.8571937, "centroid_y": -73.9182645, "area_sqft": 900.0,
       "created_at": "2026-07-01T10:00:00+00:00"}


def test_location_chunk_with_floor_name_and_no_coordinates():
    res = run(dc.chunk_location, dict(LOC))
    assert res.project_id == PROJECT
    (c,) = res.chunks
    assert c.header == "NYCHA Interior Renovation | Location Lobby (L-101) | room floor Level 3"
    assert "Floor: Level 3" in c.body
    # No parent_id: deleting the floor must not wipe this location's chunk.
    assert c.parent_id is None
    assert c.url == "/locations?openId=loc1"
    assert_clean(res, "8571937", "9182645", "Polygon")


@pytest.mark.parametrize("change", [{"status": "deleted"}, {"project_id": "gone"}, {"name": "", "code": ""}])
def test_location_deleted_orphan_or_empty_gives_no_chunks(change):
    assert run(dc.chunk_location, dict(LOC, **change)).chunks == []


def test_floor_change_requeues_its_locations_and_has_no_chunks():
    floor = dict(PARENTS["floors"][0])
    res = run(dc.chunk_floor, floor)
    assert res.chunks == []
    assert res.tenant_id == TENANT and res.project_id == PROJECT and res.source_id == "floor-1"
    assert res.requeue == [("locations", {"tenant_id": TENANT, "floor_id": "floor-1"})]


def test_floor_without_company_requeues_nothing():
    res = run(dc.chunk_floor, {"id": "floor-x", "tenant_id": None})
    assert res.chunks == [] and res.requeue == []


def test_renamed_floor_shows_on_reindexed_location():
    rows = dict(PARENTS, floors=[dict(PARENTS["floors"][0], floor_name="Mezzanine")])
    (c,) = run(dc.chunk_location, dict(LOC), rows).chunks
    assert "Floor: Mezzanine" in c.body and "Level 3" not in c.text


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------

def test_entries_are_well_formed():
    names = [e.collection for e in dc.ENTRIES]
    assert len(names) == len(set(names)), "duplicate entries"
    for e in dc.ENTRIES:
        assert isinstance(e, Entry) and e.reason.strip()
        assert e.decision in ("index", "query_only", "exclude")
        if e.decision == "index":
            assert callable(e.chunker)
        else:
            assert e.chunker is None
    by = {e.collection: e.decision for e in dc.ENTRIES}
    assert by["spec_sections"] == "index" and by["project_specs"] == "query_only"
    assert by["floors"] == "index"
    for secret in ("users", "tenant_users", "people", "project_members"):
        assert by[secret] != "index"


def test_scrub_removes_contact_details_but_keeps_spec_numbers():
    s = dc.scrub("Call (212) 555-0100 or 212.555.0100, mail a@b.co, see www.x.test/y. "
                 "cell 908 555 0142, 9085550142, +1 908 555 0142, (908)555-0142. "
                 "Section 08 11 13, ASTM E 119-12, 16 CFR 1201.")
    assert "555" not in s and "@" not in s and "www" not in s
    assert "0142" not in s and "908" not in s
    assert "08 11 13" in s and "E 119-12" in s and "16 CFR 1201" in s
