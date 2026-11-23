"""Crew sign-in sheet and payroll hours workbook.

Built from the crew rows supervisors log on each daily report (company,
trade, workers, hours each, and one name per worker). Two sheets:

  * "Sign-in sheet": one line per worker per day, with a blank signature
    column so it can be printed and signed on site.
  * "Payroll hours": one line per worker per week, hours by day of the
    week and a total, the starting point for certified payroll.

A crew row with fewer names than workers gets "Name not entered" lines
for the missing people, so the hours always add up to the report.
"""
from __future__ import annotations

import io
from collections import OrderedDict
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

MISSING_NAME = "Name not entered"
_HEAD_FILL = PatternFill("solid", start_color="1F2937")
_HEAD_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=10)
_BODY_FONT = Font(name="Arial", size=10)
_BOLD = Font(name="Arial", bold=True, size=10)
_MISSING_FONT = Font(name="Arial", size=10, italic=True, color="B45309")
_THIN = Side(style="thin", color="D1D5DB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def worker_lines(report_date: str, crew: dict, task_label: str = "") -> list[dict]:
    """One line per worker on a crew row. Names come first; any workers
    beyond the names typed get a "Name not entered" line."""
    names = [n for n in (crew.get("worker_names") or []) if str(n).strip()]
    headcount = int(crew.get("headcount") or 0)
    count = max(headcount, len(names))
    hours = float(crew.get("hours_per_person") or 0)
    company = crew.get("company_name") or crew.get("sub_name") or ""
    out = []
    for i in range(count):
        out.append({
            "date": report_date,
            "company": company,
            "trade": crew.get("trade") or "",
            "name": names[i] if i < len(names) else MISSING_NAME,
            "named": i < len(names),
            "hours": hours,
            "task": task_label,
        })
    return out


def _fmt(d: str) -> str:
    try:
        return date.fromisoformat(str(d)[:10]).strftime("%m/%d/%Y")
    except ValueError:
        return str(d or "")


def _header(ws, row: int, labels: list[str]) -> None:
    for c, label in enumerate(labels, 1):
        cell = ws.cell(row=row, column=c, value=label)
        cell.fill, cell.font, cell.border = _HEAD_FILL, _HEAD_FONT, _BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def build_workbook(project_name: str, start: date, end: date, lines: list[dict]) -> bytes:
    wb = Workbook()
    period = f"{start.strftime('%m/%d/%Y')} to {end.strftime('%m/%d/%Y')}"

    # ---- Sheet 1: sign-in sheet ----
    ws = wb.active
    ws.title = "Sign-in sheet"
    ws["A1"] = f"{project_name}: crew sign-in sheet"
    ws["A1"].font = Font(name="Arial", bold=True, size=13)
    ws["A2"] = f"Period: {period}. From the crews logged on the daily reports."
    ws["A2"].font = _BODY_FONT
    cols = ["Date", "Company", "Trade", "Worker name", "Hours", "Activity", "Time in", "Time out", "Signature"]
    _header(ws, 4, cols)
    r = 5
    for ln in lines:
        vals = [_fmt(ln["date"]), ln["company"], ln["trade"], ln["name"], ln["hours"], ln["task"], "", "", ""]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font, cell.border = _BODY_FONT, _BORDER
        if not ln["named"]:
            ws.cell(row=r, column=4).font = _MISSING_FONT
        ws.cell(row=r, column=5).number_format = "0.0"
        ws.row_dimensions[r].height = 22
        r += 1
    if not lines:
        ws.cell(row=5, column=1, value="No crews logged on daily reports in this period.").font = _BODY_FONT
    for c, w in enumerate([12, 26, 18, 26, 8, 34, 10, 10, 24], 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A5"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "4:4"

    # ---- Sheet 2: payroll hours by worker and week ----
    ps = wb.create_sheet("Payroll hours")
    ps["A1"] = f"{project_name}: hours by worker, by week"
    ps["A1"].font = Font(name="Arial", bold=True, size=13)
    ps["A2"] = (f"Period: {period}. Use with your payroll records for certified payroll; "
                "wage rates and classifications are not tracked here.")
    ps["A2"].font = _BODY_FONT
    pcols = ["Week starting", "Worker name", "Company", "Trade"] + _DAY_NAMES + ["Total hours"]
    _header(ps, 4, pcols)
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for ln in sorted(lines, key=lambda x: (x["date"], x["company"], x["trade"], x["name"])):
        try:
            d = date.fromisoformat(str(ln["date"])[:10])
        except ValueError:
            continue
        monday = d - timedelta(days=d.weekday())
        key = (monday, ln["name"], ln["company"], ln["trade"], ln["named"])
        if key not in groups:
            groups[key] = [0.0] * 7
        groups[key][d.weekday()] += float(ln["hours"] or 0)
    r = 5
    first = r
    for (monday, name, company, trade, named), days in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][3], not kv[0][4], kv[0][1])):
        vals = [monday.strftime("%m/%d/%Y"), name, company, trade] + [h or None for h in days]
        for c, v in enumerate(vals, 1):
            cell = ps.cell(row=r, column=c, value=v)
            cell.font, cell.border = _BODY_FONT, _BORDER
            if c >= 5:
                cell.number_format = "0.0"
        if not named:
            ps.cell(row=r, column=2).font = _MISSING_FONT
        tot = ps.cell(row=r, column=12, value=f"=SUM(E{r}:K{r})")
        tot.font, tot.border, tot.number_format = _BOLD, _BORDER, "0.0"
        r += 1
    if groups:
        ps.cell(row=r, column=11, value="Total").font = _BOLD
        t = ps.cell(row=r, column=12, value=f"=SUM(L{first}:L{r - 1})")
        t.font, t.number_format = _BOLD, "0.0"
        if any(not k[4] for k in groups):
            ps.cell(row=r + 2, column=1,
                    value=f'"{MISSING_NAME}" means the supervisor logged more workers than names. '
                          "Add the names on that day's report to complete the payroll.").font = _MISSING_FONT
    else:
        ps.cell(row=5, column=1, value="No crews logged on daily reports in this period.").font = _BODY_FONT
    for c, w in enumerate([13, 26, 26, 18] + [7] * 7 + [11], 1):
        ps.column_dimensions[get_column_letter(c)].width = w
    ps.freeze_panes = "A5"
    ps.page_setup.orientation = "landscape"
    ps.sheet_properties.pageSetUpPr.fitToPage = True
    ps.page_setup.fitToWidth = 1
    ps.page_setup.fitToHeight = 0

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
