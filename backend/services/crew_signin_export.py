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

from services.xlsx_safe import set_text_cell

MISSING_NAME = "Name not entered"
_HEAD_FILL = PatternFill("solid", start_color="1F2937")
_HEAD_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=10)
_BODY_FONT = Font(name="Arial", size=10)
_BOLD = Font(name="Arial", bold=True, size=10)
_MISSING_FONT = Font(name="Arial", size=10, italic=True, color="B45309")
_THIN = Side(style="thin", color="D1D5DB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


MAX_LINES_PER_CREW = 500


def _clean_names(crew: dict) -> list[str]:
    return [str(n).strip() for n in (crew.get("worker_names") or []) if str(n or "").strip()]


def _headcount(crew: dict) -> int:
    try:
        return max(0, int(float(crew.get("headcount") or 0)))
    except (TypeError, ValueError):
        return 0


def worker_line_count(crew: dict) -> int:
    """How many sign-in lines a crew row gives: its worker count, or the
    number of names when no count was entered, capped per crew."""
    hc = _headcount(crew)
    return min(hc if hc > 0 else len(_clean_names(crew)), MAX_LINES_PER_CREW)


def worker_lines(report_date: str, crew: dict, task_label: str = "", locked: bool = False) -> list[dict]:
    """One line per worker on a crew row. Names come first; any workers
    beyond the names typed get a "Name not entered" line of their own
    (numbered, so two unnamed workers never merge into one person). The
    worker count decides how many lines there are, so the hours always
    match the report; with no count, each name is one worker."""
    names = _clean_names(crew)
    headcount = _headcount(crew)
    count = worker_line_count(crew)
    try:
        hours = float(crew.get("hours_per_person") or 0)
    except (TypeError, ValueError):
        hours = 0.0
    if headcount == 0:
        # Names but no worker count: the report counts 0 hours for this
        # crew, so the sign-in lines carry 0 hours too (totals match).
        hours = 0.0
    # The same name twice on one crew is two people: number the repeats so
    # the payroll sheet keeps them apart.
    seen: dict[str, int] = {}
    labels = []
    for n in names[:count]:
        seen[n] = seen.get(n, 0) + 1
        labels.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    company = crew.get("company_name") or crew.get("sub_name") or ""
    out = []
    for i in range(count):
        named = i < len(labels)
        out.append({
            "date": report_date,
            "company": company,
            "trade": crew.get("trade") or "",
            "name": labels[i] if named else f"{MISSING_NAME} (worker {i + 1} of {count})",
            "named": named,
            "hours": hours,
            "task": task_label,
            "locked": locked,
            "crew_id": crew.get("id") or "",
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
    set_text_cell(ws["A1"], f"{project_name}: crew sign-in sheet")
    ws["A1"].font = Font(name="Arial", bold=True, size=13)
    ws["A2"] = f"Period: {period}. From the crews logged on the daily reports."
    ws["A2"].font = _BODY_FONT
    cols = ["Date", "Company", "Trade", "Worker name", "Hours", "Activity", "Time in", "Time out", "Signature"]
    _header(ws, 4, cols)
    r = 5
    for ln in lines:
        vals = [_fmt(ln["date"]), ln["company"], ln["trade"], ln["name"], ln["hours"], ln["task"], "", "", ""]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c)
            if isinstance(v, str):
                set_text_cell(cell, v)   # typed text never becomes a live formula
            else:
                cell.value = v
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
    set_text_cell(ps["A1"], f"{project_name}: hours by worker, by week")
    ps["A1"].font = Font(name="Arial", bold=True, size=13)
    ps["A2"] = (f"Period: {period}. Use with your payroll records for certified payroll; "
                "wage rates and classifications are not tracked here.")
    ps["A2"].font = _BODY_FONT
    pcols = ["Week starting", "Worker name", "Company", "Trade"] + _DAY_NAMES + ["Total hours"]
    _header(ps, 4, pcols)
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for ln in sorted(lines, key=lambda x: (x["date"], x["company"], x["trade"], not x["named"], x["name"] if x["named"] else "")):
        try:
            d = date.fromisoformat(str(ln["date"])[:10])
        except ValueError:
            continue
        monday = d - timedelta(days=d.weekday())
        # A named worker is one row for the week. An unnamed worker can't be
        # matched across days, so each one keeps a row of its own.
        who = ln["name"] if ln["named"] else (ln["name"], ln["date"], ln.get("crew_id"))
        key = (monday, who, ln["company"], ln["trade"], ln["named"])
        if key not in groups:
            groups[key] = {"days": [0.0] * 7, "name": ln["name"], "locked": False}
        groups[key]["days"][d.weekday()] += float(ln["hours"] or 0)
        groups[key]["locked"] = groups[key]["locked"] or bool(ln.get("locked"))
    r = 5
    first = r
    any_missing_locked = any_missing_open = False
    for (monday, _who, company, trade, named), g in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][3], not kv[0][4], kv[0][1] if kv[0][4] else "")):
        if not named:
            any_missing_locked = any_missing_locked or g["locked"]
            any_missing_open = any_missing_open or not g["locked"]
        vals = [monday.strftime("%m/%d/%Y"), g["name"], company, trade] + [h or None for h in g["days"]]
        for c, v in enumerate(vals, 1):
            cell = ps.cell(row=r, column=c)
            if isinstance(v, str):
                set_text_cell(cell, v)
            else:
                cell.value = v
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
        notes = []
        if any_missing_open:
            notes.append(f'"{MISSING_NAME}" means the supervisor logged more workers than names. '
                         "Add the names on that day's report (tap the crew) to complete the payroll.")
        if any_missing_locked:
            notes.append(f'Some "{MISSING_NAME}" lines are on reports that were already submitted, so the '
                         "names can't be added in the app. Write them on the printed sheet.")
        for i, text in enumerate(notes):
            ps.cell(row=r + 2 + i, column=1, value=text).font = _MISSING_FONT
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
