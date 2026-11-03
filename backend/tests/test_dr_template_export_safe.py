"""Client-template export (2026-11-03): one odd value from the AI (a list,
an object, a control character from pasted or dictated notes) must not fail
the whole export, and failures must reach the user as a 4xx with a reason --
production's Cloudflare layer replaces any 5xx with its own HTML page."""
import asyncio
import io
import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

openpyxl = pytest.importorskip("openpyxl")
dte = pytest.importorskip("services.dr_template_export")


def _template() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A6"] = "Weather:"
    ws["A8"] = "Date"
    ws["K12"] = "LOCATION AND DESCRIPTION OF WORK"
    ws.merge_cells("C6:F6")
    ws["A30"] = "=1+1"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _run(answer: str) -> "openpyxl.Workbook":
    class _Chat:
        async def send_message(self, _m):
            return answer
    dte._new_chat = lambda sid, sysmsg: _Chat()
    dte._track_usage = lambda *a, **k: asyncio.sleep(0)
    out = asyncio.run(dte.inject_dr_into_template(
        _template(), dr={"report_date": "2026-11-03"}, project={}, activities=[],
        presence=[], delays=[], signer_name="", tenant_id="t", request_id="r"))
    return openpyxl.load_workbook(io.BytesIO(out)).active


def test_odd_values_are_written_safely_instead_of_failing():
    ws = _run('{"Sheet1": {"C6": "Sunny\\u000b", "A9": "11/03/2026", '
              '"K14": ["Demo duct work", "Trace VAV circuit"], "K15": {"x": 1}, '
              '"D6": "inside a merged cell", "A30": "overwrite formula?"}}')
    assert ws["C6"].value == "Sunny"
    assert ws["A9"].value == "11/03/2026"
    assert ws["K14"].value == "Demo duct work\nTrace VAV circuit"
    assert ws["K15"].value == '{"x": 1}'
    assert ws["A30"].value == "=1+1"


def test_no_usable_answer_is_a_clear_error():
    with pytest.raises(dte.DrTemplateExportError):
        _run("I could not map this template.")


def test_download_header_survives_non_latin1_project_names():
    # VA 6B's project name has an en dash; a raw header crashed with a bare 500.
    from starlette.responses import Response
    from services.http_download import attachment_disposition
    for name in ["DR_2026-11-04_Renovate 6B for Outpatient Mental Health – Building 100.xlsx",
                 r'DR_2026-11-04_Café "Main"\x.xlsx', "Ω", ""]:
        h = attachment_disposition(name, "DR.xlsx")
        Response(b"x", headers={"content-disposition": h})  # must not raise
        h.encode("latin-1")
        assert h.startswith('attachment; filename="')
    assert "%E2%80%93" in attachment_disposition("A – B.xlsx")
    assert attachment_disposition("plain.xlsx") == 'attachment; filename="plain.xlsx"'


def _va_like_template() -> bytes:
    """The parts of the VA 6B form that went wrong on 2026-11-03."""
    from openpyxl.styles import Alignment, Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A3"] = "STATION -  VA Medical center, Bronx NY"
    ws["A4"] = "PROJECT TITLE- Renovate 6B for Outpatient Mental Health"
    ws["K4"] = "NAME OF CONTRACTOR"
    ws["K5"] = "J&J Mechanical"
    ws["A5"] = "Contract Number:  36C24218C008"
    ws["A8"] = "Date"
    ws.merge_cells("B8:D9")
    ws["A9"].font = Font(color="FF00B050", bold=True)
    ws["A9"].number_format = "mm-dd-yy"
    ws["G7"] = "Prec."
    ws["H7"].number_format = "0%"
    ws["C12"], ws["F12"], ws["K12"] = "Contractor", "Trade", "Men"
    ws["C13"] = "Old Co"
    ws.column_dimensions["K"].width = 47.44
    ws["K14"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[14].height = 30
    ws["A25"] = "SIGNATURE OF SUPERVISOR :"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _run_va(answer: dict):
    class _Chat:
        async def send_message(self, _m):
            return __import__("json").dumps(answer)
    dte._new_chat = lambda sid, sysmsg: _Chat()
    dte._track_usage = lambda *a, **k: asyncio.sleep(0)
    out = asyncio.run(dte.inject_dr_into_template(
        _va_like_template(), dr={"report_date": "2026-11-03"}, project={}, activities=[],
        presence=[], delays=[], signer_name="", tenant_id="t", request_id="r"))
    return openpyxl.load_workbook(io.BytesIO(out)).active


def test_client_header_lines_are_kept():
    ws = _run_va({"Sheet1": {
        "A3": "STATION -  526-19-122", "A4": "PROJECT TITLE- Renovate 6B – Building 100",
        "A5": "Contract Number:  36C24218C0080", "K5": "ICE",
        "A25": "SIGNATURE OF SUPERVISOR : Juemil Alfaro", "C13": "Star tech - 3 men"}})
    assert ws["A3"].value == "STATION -  VA Medical center, Bronx NY"
    assert ws["A4"].value == "PROJECT TITLE- Renovate 6B for Outpatient Mental Health"
    assert ws["A5"].value == "Contract Number:  36C24218C008"
    assert ws["K5"].value == "J&J Mechanical"
    # per-report lines and table rows are still written
    assert ws["A25"].value == "SIGNATURE OF SUPERVISOR : Juemil Alfaro"
    assert ws["C13"].value == "Star tech - 3 men"


def test_snapshot_points_at_styled_entry_cells_and_kept_headers():
    wb = openpyxl.load_workbook(io.BytesIO(_va_like_template()))
    snap = dte._extract_template_snapshot(wb)["Sheet1"]
    styled = {c["address"]: c for c in snap["styled_empty_cells"]}
    assert styled["A9"]["number_format"] == "mm-dd-yy" and styled["A9"]["bold"]
    assert set(snap["kept_header_cells"]) == {"A3", "A4", "A5", "K5"}


def test_values_take_the_cells_own_format_and_long_text_gets_room():
    long = ("Final wall to be demoed when ICRA barrier is demoed. Men disposed of metal in "
            "early morning. Loaded up bins to dispose of remaining garbage. Proceeded with demo")
    ws = _run_va({"Sheet1": {"A9": "11/03/2026", "H7": 2, "C9": "inside the box", "K14": long}})
    assert ws["A9"].value.year == 2026 and ws["A9"].value.day == 3
    assert "%" not in ws["H7"].number_format
    assert ws["B8"].value == "inside the box"
    assert ws.row_dimensions[14].height > 30
    assert list(ws.column_dimensions) == ["K"]  # no stray column entries saved


def test_weather_numbers_read_like_a_supervisor_wrote_them():
    snap = dte._build_dr_snapshot(
        dr={"weather_temp_high_f": 70, "weather_temp_low_f": 46.4, "weather_conditions": "overcast"},
        project={}, activities=[], presence=[], delays=[], signer_name="")
    assert snap["weather"] == {**snap["weather"], "temp_high_f": 70, "temp_low_f": 46,
                               "conditions": "Overcast"}


def _sheet(cells: dict, merges=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    for a, v in cells.items():
        ws[a] = v
    for m in merges:
        ws.merge_cells(m)
    return ws


def test_header_lock_stays_in_the_header_block():
    # Review 2026-11-03: remarks lower down, two-column tables, per-day cells
    # under a header label and placeholders must all stay writable.
    ws = _sheet({"A2": "Contract No.", "B2": "36C24218C0080", "A3": "Report No. 152",
                 "A5": "Contractor", "B5": "No. of Men", "A6": "Old Plumbing Co", "B6": 4,
                 "A7": "Project Name: (Insert Project Name)", "A8": "Contractor: TBD",
                 "A9": "Contractor Name: J&J Mechanical",
                 "A29": "Owner - COR walkthrough", "A30": "Station: 12+50 curb placed"})
    kept = dte._protected_header_cells(ws)
    assert "B2" in kept and "A9" in kept
    for free in ("A3", "A6", "A7", "A8", "A29", "A30"):
        assert free not in kept, free


def test_label_and_value_on_one_row_is_still_protected():
    ws = _sheet({"A3": "Project:", "B3": "Renovate 6B for Outpatient Mental Health", "E3": "Date:"})
    assert "B3" in dte._protected_header_cells(ws)


def test_writes_aimed_inside_a_header_box_do_not_erase_the_header():
    ws = _sheet({"C12": "SKILLED WORKERS"}, merges=["C12:E13"])
    assert dte._top_left(ws, ws["C13"]) is None
    ws2 = _sheet({}, merges=["B8:D9"])
    assert dte._top_left(ws2, ws2["C9"]).coordinate == "B8"


def test_one_line_values_do_not_grow_rows():
    from openpyxl.styles import Alignment
    ws = _sheet({"A1": "Sunny"})
    ws["A1"].alignment = Alignment(wrap_text=True)
    dte._fit_rows(ws, [ws["A1"]])
    assert 1 not in ws.row_dimensions or ws.row_dimensions[1].height is None


def test_coloured_bands_do_not_crowd_out_entry_cells():
    from openpyxl.styles import Font
    ws = _sheet({"A8": "Date"})
    for r in range(1, 5):
        for c in range(1, 15):
            ws.cell(row=r, column=c).font = Font(color="FFFFFFFF", bold=True)
    for c in range(1, 15):
        ws.cell(row=6, column=c).font = Font(color="FF1F4E79")
    ws["A9"].font = Font(color="FF00B050", bold=True)
    got = [e["address"] for e in dte._styled_empty_cells(ws)]
    assert got == ["A9"]


def test_table_header_in_row_one_locks_nothing_below():
    ws = _sheet({"A1": "Project:", "D1": "Date:", "G1": "Report No.:",
                 "A29": "Owner - COR walkthrough", "A30": "Station: 12+50 curb placed"})
    assert dte._protected_header_cells(ws) == set()


def test_combined_lines_stay_writable():
    # A line that also carries the date, report number or a crew count is the
    # day's data too -- never locked (round-5 review).
    ws = _sheet({"A3": "Project No.: 526-19-122      Date: 11/02/2026",
                 "A4": "Contractor: ____________   Date: ____________",
                 "A5": "Project No. 526-19-122      Date 11/02/2026",
                 "A6": "Contract No: 36C24218C0080   Report # 45",
                 "A7": "Project: VA 6B Temp: 45F",
                 "A8": "Contractor: J&J Mechanical    Workers: 6"})
    assert dte._protected_header_cells(ws) == set()


def test_header_values_without_digits_and_other_wordings_are_kept():
    ws = _sheet({"A1": "DAILY CONSTRUCTION REPORT", "A3": "Project:", "B3": "Downtown Library Renovation",
                 "D3": "Date:", "A4": "Contractor:", "B4": "J&J Mechanical", "D4": "Weather:",
                 "A5": "Client Name: VA NY Harbor Healthcare", "A6": "Station No.: 526",
                 "A7": "Owner/Client: VA", "A8": "Contractor's Name: J&J Mechanical"})
    kept = dte._protected_header_cells(ws)
    assert "A7" not in kept  # "Owner" lines are usually the owner's visits
    for cell in ("B3", "B4", "A5", "A6", "A8"):
        assert cell in kept, cell


def test_label_row_of_header_items_keeps_the_values_below():
    ws = _sheet({"A2": "Project Name", "B2": "Contract No.", "C2": "Contractor",
                 "A3": "Bronx 6B", "B3": "36C24218C0080", "C3": "J&J Mechanical"})
    assert {"A3", "B3", "C3"} <= set(dte._protected_header_cells(ws))


def test_placeholder_check_is_fast_on_long_x_runs():
    import time
    t = time.time()
    assert not dte._has_content("XXX-XX-XXX")
    assert dte._has_content("X" * 40 + " (to be assigned)")
    assert time.time() - t < 0.5


def test_spoken_visits_and_deliveries_reach_the_form():
    snap = dte._build_dr_snapshot(
        dr={}, project={}, activities=[], presence=[], delays=[], signer_name="",
        site_log={"site_events": [{"with_party": "William Anis", "description": "COR onsite"}],
                  "deliveries": [{"supplier_name": "ABC Supply", "description": "40 sheets drywall"}]})
    assert snap["site_events"][0]["who"] == "William Anis"
    assert snap["site_events"][0]["summary"] == "COR onsite"
    assert snap["deliveries"][0]["vendor"] == "ABC Supply"


def test_percent_cells_are_pointed_out():
    wb = openpyxl.load_workbook(io.BytesIO(_va_like_template()))
    assert dte._extract_template_snapshot(wb)["Sheet1"]["percent_cells"] == ["H7"]


def test_header_values_in_capitals_are_still_kept():
    ws = _sheet({"K4": "NAME OF CONTRACTOR", "K5": "J&J MECHANICAL", "A3": "PROJECT:",
                 "B3": "RENOVATE BUILDING SIX", "A6": "Contractor:", "B6": "ICE", "E6": "Weather:"})
    assert {"K5", "B3", "B6"} <= set(dte._protected_header_cells(ws))


def test_field_rows_do_not_end_the_header_block():
    ws = _sheet({"A2": "Date:", "C2": "Weather:", "E2": "Temp:",
                 "A3": "Contractor: J&J Mechanical", "A4": "Station - VA Medical Center",
                 "A6": "Project:", "B6": "Downtown Library Renovation", "D6": "Date:", "F6": "Report No.:"})
    assert {"A3", "A4", "B6"} <= set(dte._protected_header_cells(ws))


def test_small_crew_table_near_the_top_stays_writable():
    ws = _sheet({"A1": "DAILY REPORT", "A2": "Project:", "B2": "Bronx 6B", "A5": "Contractor",
                 "B5": "Trade", "C5": "Workers", "D5": "Description of Work", "A6": "Old Plumbing Co"})
    kept = dte._protected_header_cells(ws)
    assert "B2" in kept and "A6" not in kept


def test_single_space_header_lines_are_kept():
    ws = _sheet({"A3": "Contract No. 36C24218C0080", "A4": "PROJECT NO 526-19-122",
                 "A5": "Project Title Renovate 6B"})
    assert {"A3", "A4", "A5"} <= set(dte._protected_header_cells(ws))


def test_theme_coloured_entry_cells_are_found():
    from openpyxl.styles import Font
    from openpyxl.styles.colors import Color
    ws = _sheet({"A8": "Date"})
    ws["A9"].font = Font(color=Color(theme=4))
    assert [e["address"] for e in dte._styled_empty_cells(ws)] == ["A9"]


def test_a_stray_far_right_cell_does_not_slow_the_snapshot():
    import time
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 61):
        for c in range(1, 20):
            ws.cell(row=r, column=c, value=f"v{r}-{c}")
    ws["XFD5"] = "x"
    t = time.time()
    dte._extract_template_snapshot(wb)
    assert time.time() - t < 2


def test_crew_rows_and_owner_visits_near_the_top_stay_writable():
    ws = _sheet({"A6": "DESCRIPTION OF WORK PERFORMED", "D6": "CONTRACTOR", "A7": "Demo duct work",
                 "D7": "J&J Mechanical", "A8": "Owner", "B8": "COR William Anis walkthrough",
                 "A3": "Contractor:", "B3": "Star Tech", "C3": "Workers:", "D3": 3})
    assert dte._protected_header_cells(ws) == set()


def test_a_real_zero_percent_stays_a_percent():
    wb = openpyxl.Workbook()
    c = wb.active["B3"]
    c.number_format = "0%"
    assert dte._typed_for_cell(c, 0) == 0 and c.number_format == "0%"
    dte._typed_for_cell(c, 55)
    assert c.number_format == "General"


def test_many_merged_boxes_stay_fast():
    import time
    from openpyxl.styles import Alignment
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 1501):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)
    t = time.time()
    written = []
    for r in range(1, 151):
        cell = dte._top_left(ws, ws.cell(row=r, column=2))
        cell.value = "x" * 120
        written.append(cell)
    dte._fit_rows(ws, written)
    assert time.time() - t < 1.5


def test_rain_is_never_shown_as_a_percent():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["G7"] = "Prec."
    ws["H7"].number_format = "0%"
    dte._typed_for_cell(ws["H7"], 0)
    assert ws["H7"].number_format == "General"
