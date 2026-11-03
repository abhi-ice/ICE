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
