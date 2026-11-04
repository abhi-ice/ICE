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


def _sheet():
    wb = openpyxl.Workbook()
    return wb, wb.active


def _reloaded(wb):
    # Column widths only carry their min..max once read back from a file,
    # which is how every template reaches the export.
    buf = io.BytesIO()
    wb.save(buf)
    return openpyxl.load_workbook(io.BytesIO(buf.getvalue())).active


def _height(ws, row):
    return ws.row_dimensions[row].height if row in ws.row_dimensions else None


LONG = ("Final wall to be demoed when ICRA barrier is demoed. Men disposed of metal in "
        "early morning. Loaded up bins to dispose of remaining garbage. Proceeded with demo")


def test_long_text_in_a_wrapped_cell_grows_the_row():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws.column_dimensions["K"].width = 47.44
    ws["K14"] = LONG
    ws["K14"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[14].height = 30
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["K14"]])
    # Default 11pt font: about 52 characters a line, so four lines.
    assert 50 <= _height(ws, 14) <= 75
    assert list(ws.column_dimensions) == ["K"]  # no stray column entries saved


def test_long_text_in_an_unwrapped_box_wraps_on_any_form():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws.merge_cells("A5:C5")
    ws["A5"] = LONG * 2
    ws["A5"].alignment = Alignment(horizontal="left")
    dte._fit_rows(ws, [ws["A5"]])
    assert ws["A5"].alignment.wrap_text
    assert ws["A5"].alignment.horizontal == "left"
    assert _height(ws, 5) > 15


def test_long_text_cut_off_by_a_filled_neighbour_wraps():
    wb, ws = _sheet()
    ws["A5"], ws["B5"] = LONG, "6"
    ws.row_dimensions[5].height = 15
    dte._fit_rows(ws, [ws["A5"], ws["B5"]])
    assert ws["A5"].alignment.wrap_text and _height(ws, 5) > 15


def test_text_that_fits_the_form_stays_on_one_line():
    wb, ws = _sheet()
    for c, w in zip("ABCDEFGHIJK", (6, 14, 14, 10, 10, 10, 10, 10, 10, 10, 10)):
        ws.column_dimensions[c].width = w
    ws["K1"] = "edge"
    ws["A21"] = "x" * 120
    ws.row_dimensions[21].height = 15
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["A21"]])
    assert not ws["A21"].alignment.wrap_text and _height(ws, 21) == 15


def test_a_narrow_cell_with_free_space_is_not_stacked_into_a_tower():
    wb, ws = _sheet()
    ws.column_dimensions["A"].width = 10
    ws["K1"] = "edge"
    ws["A21"] = LONG * 2
    ws.row_dimensions[21].height = 15
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["A21"]])
    assert not ws["A21"].alignment.wrap_text and _height(ws, 21) == 15


def test_a_box_keeps_room_for_the_automatic_cell_beside_it():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws.column_dimensions["A"].width = 12
    ws.merge_cells("B5:F5")
    ws["A5"], ws["B5"] = "y" * 68, "z" * 92
    ws["A5"].alignment = ws["B5"].alignment = Alignment(wrap_text=True)
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["A5"], ws["B5"]])
    assert _height(ws, 5) >= 6 * 14.55


def test_thin_columns_are_left_alone():
    wb, ws = _sheet()
    ws["A5"], ws["B5"], ws["C5"] = "Weather:", "overcast, high 70 F, low 46 F", "Date:"
    ws["D5"], ws["E5"], ws["F5"] = "x", "Superintendent", "y"
    ws.row_dimensions[5].height = 15
    ws.column_dimensions["B"].width = 1.71
    ws.column_dimensions["E"].width = 5
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["B5"], ws["E5"]])
    assert _height(ws, 5) == 15


def test_right_aligned_text_cannot_run_on_to_the_right():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws["G1"] = "edge"
    ws["A5"], ws["B5"] = "x", "y" * 70
    ws["B5"].alignment = Alignment(horizontal="right")
    ws.row_dimensions[5].height = 15
    dte._fit_rows(ws, [ws["B5"]])
    assert ws["B5"].alignment.wrap_text and ws["B5"].alignment.horizontal == "right"
    assert _height(ws, 5) > 15


def test_automatic_rows_are_left_for_excel_to_fit():
    wb, ws = _sheet()
    ws["A5"], ws["B5"] = LONG, "6"
    dte._fit_rows(ws, [ws["A5"], ws["B5"]])
    assert ws["A5"].alignment.wrap_text and _height(ws, 5) is None


def test_long_text_in_the_forms_last_column_wraps():
    wb, ws = _sheet()
    ws["A4"], ws["D4"] = "Company", "Description of Work"
    ws["D5"] = LONG
    dte._fit_rows(ws, [ws["D5"]])
    assert ws["D5"].alignment.wrap_text


def test_long_text_stopped_by_a_filled_cell_further_along_wraps():
    wb, ws = _sheet()
    ws.column_dimensions["A"].width = 22
    ws["A5"], ws["C5"] = LONG, 3
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["A5"], ws["C5"]])
    assert ws["A5"].alignment.wrap_text


def test_long_text_stopped_by_the_print_area_wraps():
    wb, ws = _sheet()
    ws.column_dimensions["B"].width = 22
    ws["F1"] = "beyond the form"
    ws["B5"] = LONG
    ws.print_area = "A1:C30"
    ws = _reloaded(wb)
    assert dte._right_edge(ws) == 3
    dte._fit_rows(ws, [ws["B5"]])
    assert ws["B5"].alignment.wrap_text


def test_long_text_stopped_by_an_empty_box_wraps():
    wb, ws = _sheet()
    ws.column_dimensions["A"].width = 22
    ws["L1"] = "edge"
    ws.merge_cells("C5:E5")
    ws["A5"] = LONG
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["A5"]])
    assert ws["A5"].alignment.wrap_text


def test_line_breaks_always_wrap_even_with_room_to_run_on():
    wb, ws = _sheet()
    ws["Z1"] = "edge"
    ws["B10"] = "COR walkthrough\nRain stopped work at 2pm"
    dte._fit_rows(ws, [ws["B10"]])
    assert ws["B10"].alignment.wrap_text


def test_a_short_value_just_over_the_free_space_stays_on_one_line():
    wb, ws = _sheet()
    ws["A3"], ws["G3"] = "Project:", "Date:"
    ws["B3"] = "Renovate 6B for Outpatient Mental Health Clinic"
    ws.row_dimensions[3].height = 15
    dte._fit_rows(ws, [ws["B3"]])
    assert not ws["B3"].alignment.wrap_text and _height(ws, 3) == 15


def test_several_items_in_a_narrow_cell_still_wrap():
    wb, ws = _sheet()
    ws["H1"] = "edge"
    ws["B6"] = ("ABC Supply - 40 sheets of 5/8 drywall\nXYZ Metals - 12 bundles of stud track\n"
                "Acme Electric - 2 panels (scheduled)")
    ws.row_dimensions[6].height = 18
    dte._fit_rows(ws, [ws["B6"]])
    assert ws["B6"].alignment.wrap_text and _height(ws, 6) > 18


def test_hidden_and_spacer_columns_never_blow_up_a_row():
    wb, ws = _sheet()
    ws["A5"], ws["B5"], ws["C5"], ws["C6"] = "x", "Sunny and very long text here", "y", "z"
    ws["B6"] = "Star tech"
    ws.row_dimensions[5].height = 15
    ws.row_dimensions[6].height = 15
    ws.column_dimensions["B"].width = 0.1
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["B5"], ws["B6"]])
    assert _height(ws, 5) == 15 and _height(ws, 6) == 15
    assert not ws["B5"].alignment.wrap_text


def test_capitals_get_their_extra_room():
    from openpyxl.styles import Alignment, Font
    wb, ws = _sheet()
    ws.column_dimensions["K"].width = 47.44
    for a, text in (("K1", "a" * 400), ("K2", "A" * 400)):
        ws[a] = text
        ws[a].font = Font(sz=12)
        ws[a].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[1].height = ws.row_dimensions[2].height = 30
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["K1"], ws["K2"]])
    assert _height(ws, 2) >= 1.25 * _height(ws, 1)


def test_rows_get_enough_height_for_calibri():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws.merge_cells("A5:F5")
    ws["A5"] = "\n".join(f"Item {i}" for i in range(8))
    ws["A5"].alignment = Alignment(wrap_text=True)
    dte._fit_rows(ws, [ws["A5"]])
    assert _height(ws, 5) >= 8 * 14.55  # Excel's measured line pitch for Calibri 11


def test_text_running_over_empty_cells_stays_on_one_line():
    wb, ws = _sheet()
    ws["Z1"] = "edge"
    ws["A5"] = LONG
    dte._fit_rows(ws, [ws["A5"]])
    assert not ws["A5"].alignment.wrap_text
    assert _height(ws, 5) is None
    assert (5, 2) not in ws._cells  # no stray cell saved into the client's file


def test_one_line_values_do_not_grow_rows():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws["A1"] = "Sunny"
    ws["A1"].alignment = Alignment(wrap_text=True)
    dte._fit_rows(ws, [ws["A1"]])
    assert _height(ws, 1) is None


def test_very_long_text_is_not_capped_early_but_stays_within_excel():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws["A1"], ws["A2"] = "x " * 400, "x " * 4000
    for c in (ws["A1"], ws["A2"]):
        c.alignment = Alignment(wrap_text=True)
    ws.row_dimensions[1].height = ws.row_dimensions[2].height = 15
    dte._fit_rows(ws, [ws["A1"], ws["A2"]])
    assert 200 < _height(ws, 1) <= 409
    assert _height(ws, 2) == 409


def test_a_box_over_several_rows_counts_all_its_rows():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws.merge_cells("A5:A7")
    ws.column_dimensions["A"].width = 40
    ws["A5"] = LONG
    ws["A5"].alignment = Alignment(wrap_text=True)
    for r in (5, 6, 7):
        ws.row_dimensions[r].height = 30
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["A5"]])
    assert _height(ws, 7) == 30  # three 30pt rows already hold it


def test_a_tall_box_spreads_past_excels_row_limit():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws.merge_cells("A5:H12")
    ws["A5"] = "\n".join(f"Item {i}" for i in range(60))
    ws["A5"].alignment = Alignment(wrap_text=True)
    dte._fit_rows(ws, [ws["A5"]])
    heights = [_height(ws, r) or 15 for r in range(5, 13)]
    assert max(heights) <= 409 and sum(heights) >= 60 * 14.55


def test_hidden_rows_do_not_count_as_room():
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    ws.merge_cells("A5:D7")
    ws["A5"] = "\n".join(f"Item {i}" for i in range(6))
    ws["A5"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[6].height = 15
    ws.row_dimensions[7].height = 15
    ws.row_dimensions[7].hidden = True
    dte._fit_rows(ws, [ws["A5"]])
    assert _height(ws, 7) == 15
    assert (_height(ws, 5) or 15) + _height(ws, 6) >= 6 * 14.55


def test_a_column_width_covering_several_columns_is_read():
    from openpyxl.styles import Alignment
    from openpyxl.worksheet.dimensions import ColumnDimension
    wb, ws = _sheet()
    ws.merge_cells("B2:D2")
    ws["B2"] = LONG
    ws["B2"].alignment = Alignment(wrap_text=True)
    ws.column_dimensions["B"] = ColumnDimension(ws, index="B", min=2, max=4, width=30,
                                                customWidth=True)
    ws = _reloaded(wb)
    dte._fit_rows(ws, [ws["B2"]])
    assert _height(ws, 2) is None or _height(ws, 2) <= 30  # 90 wide: two lines at most


def test_many_merged_boxes_stay_fast():
    import time
    from openpyxl.styles import Alignment
    wb, ws = _sheet()
    for r in range(1, 1501):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)
    written = []
    for r in range(1, 151):
        ws.cell(row=r, column=1).value = "x" * 120
        written.append(ws.cell(row=r, column=1))
    t = time.time()
    dte._fit_rows(ws, written)
    assert time.time() - t < 1.5


def test_export_fits_rows_but_keeps_going_if_sizing_fails():
    answer = '{"Sheet1": {"K14": "' + LONG + '"}}'
    assert _run(answer)["K14"].value == LONG
    original = dte._fit_rows
    try:
        dte._fit_rows = lambda ws, written: 1 / 0
        assert _run(answer)["K14"].value == LONG
    finally:
        dte._fit_rows = original


def test_visits_and_deliveries_reach_the_form():
    snap = dte._build_dr_snapshot(
        dr={}, project={}, activities=[], presence=[], delays=[], signer_name="",
        site_log={"site_events": [{"other_party_name": "William Anis", "other_party_org": "VA",
                                   "other_party_role": "COR", "summary": "Viewed work in progress"}],
                  "deliveries": [{"vendor": "ABC Supply", "description": "40 sheets drywall",
                                  "po_number": "PO-7"}]})
    assert snap["site_events"][0]["who"] == "William Anis"
    assert snap["site_events"][0]["role"] == "COR"
    assert snap["deliveries"] == [{"vendor": "ABC Supply", "description": "40 sheets drywall",
                                   "po_number": "PO-7", "status": None}]


def test_spoken_visits_and_deliveries_reach_the_form():
    snap = dte._build_dr_snapshot(
        dr={}, project={}, activities=[], presence=[], delays=[], signer_name="",
        site_log={"site_events": [{"with_party": "William Anis", "description": "COR onsite"}],
                  "deliveries": [{"supplier_name": "ABC Supply", "description": "40 sheets drywall"}]})
    assert snap["site_events"][0]["who"] == "William Anis"
    assert snap["site_events"][0]["summary"] == "COR onsite"
    assert snap["deliveries"][0]["vendor"] == "ABC Supply"


def _visit(**kw):
    return {"other_party_name": "A Person", "summary": "Walked the site", **kw}


def test_only_real_visits_reach_a_client_form():
    log = [_visit(channel="in_person", direction="inbound", topic="regulatory"),
           _visit(channel="meeting", direction="outbound", topic="owner_direction"),
           {"with_party": "Spoken", "description": "Came by"},
           _visit(channel="phone", direction="inbound"),
           _visit(channel="email", direction="inbound"),
           _visit(channel="text_message", direction="outbound"),
           _visit(channel="formal_letter", direction="inbound"),
           _visit(channel="system_notification", direction="inbound"),
           _visit(channel="in_person", direction="internal"),
           _visit(channel="meeting", direction="inbound", topic="payment"),
           _visit(channel="in_person", direction="inbound", topic="sub_coordination")]
    snap = dte._build_dr_snapshot(dr={}, project={}, activities=[], presence=[], delays=[],
                                  signer_name="", site_log={"site_events": log})
    assert len(snap["site_events"]) == 3
    assert snap["site_events"][2]["who"] == "Spoken"


def test_internal_outcome_notes_never_reach_the_ai():
    snap = dte._build_dr_snapshot(
        dr={}, project={}, activities=[], presence=[], delays=[], signer_name="",
        site_log={"site_events": [_visit(channel="in_person", outcome_notes="Preparing a claim")]})
    assert "Preparing a claim" not in str(snap)


def test_deliveries_carry_their_status_and_skip_what_never_came():
    day = "2026-11-03"
    rows = [{"vendor": "Today", "delivery_date": day, "status": "received"},
            {"vendor": "Planned", "delivery_date": day, "status": "scheduled"},
            {"supplier_name": "Spoken", "status": "rejected", "date": day},
            {"vendor": "Off", "delivery_date": day, "status": "cancelled"},
            {"vendor": "Moved", "delivery_date": "2026-11-04", "status": "scheduled"}]
    snap = dte._build_dr_snapshot(dr={"report_date": day}, project={}, activities=[], presence=[],
                                  delays=[], signer_name="", site_log={"deliveries": rows})
    assert [(d["vendor"], d["status"]) for d in snap["deliveries"]] == [
        ("Today", "received"), ("Planned", "scheduled"), ("Spoken", "rejected")]


def test_deliveries_typed_into_the_report_count_as_arrived():
    day = "2026-11-03"
    rows = [{"vendor": "Typed", "delivery_date": day, "daily_report_id": "dr-1", "status": "scheduled"},
            {"vendor": "Planned", "delivery_date": day, "status": "scheduled"},
            {"supplier_name": "Spoken", "daily_report_id": "dr-1", "status": "scheduled",
             "source": "voice_intake"},
            {"vendor": "Late", "delivery_date": day, "daily_report_id": "dr-1", "status": "delayed"}]
    snap = dte._build_dr_snapshot(dr={"id": "dr-1", "report_date": day}, project={}, activities=[],
                                  presence=[], delays=[], signer_name="", site_log={"deliveries": rows})
    assert [d["status"] for d in snap["deliveries"]] == ["received", "scheduled", "scheduled", "delayed"]


def test_no_site_log_still_exports():
    snap = dte._build_dr_snapshot(dr={}, project={}, activities=[], presence=[], delays=[],
                                  signer_name="")
    assert snap["site_events"] == [] and snap["deliveries"] == []


def test_prompt_has_no_project_specific_rules():
    assert "kept_header_cells" not in dte.SYSTEM_PROMPT
    assert "styled_empty_cells" not in dte.SYSTEM_PROMPT
    assert "percent_cells" not in dte.SYSTEM_PROMPT
    assert "William Anis" not in dte.SYSTEM_PROMPT and "Ivars" not in dte.SYSTEM_PROMPT
    assert "site_events" in dte.SYSTEM_PROMPT and "deliveries" in dte.SYSTEM_PROMPT
