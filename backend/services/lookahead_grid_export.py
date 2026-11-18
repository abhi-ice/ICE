"""Two-week look-ahead in the owner's day-grid format (2026-11-18).

Owners and their construction managers (for example a VA medical center's
construction manager) ask for the look-ahead on a one-page form: the field
activities down the left, the 14 days of two Monday-to-Sunday weeks across
the top, and an X under every day the activity is worked. Below the grid sit
two boxes, "Notes" and "Items Delaying the Project". This module builds that
form from the project's own schedule, as an Excel file or a PDF.

Rules (general, every project):
  * Period: 14 days starting on a Monday. The default is the Monday of the
    project's current week (site-clock today); any Monday can be chosen.
  * Rows: field work only, not complete, whose schedule dates overlap the
    period, plus unfinished field work that is overdue (it is still to be
    done). Schedule dates, not the forecast, decide this. Work
    added from a daily report that a project manager has not reviewed yet is
    left out, so field notes never reach the owner as schedule activities.
    Paperwork and procurement are not field work; the ones running in the
    period are listed under Notes instead.
  * Days: an X on each working day of the project calendar (its workweek and
    holidays; federal holidays by default) between the activity's start and
    finish. The start is the actual start once work has begun, else the
    planned start; work that should have started already but has not runs
    from the first day of the period. The finish is the planned finish of
    the current schedule; overdue work runs to its forecast finish.
  * Items Delaying the Project: open delays and open blockers on the project.
"""
from __future__ import annotations

import html as _html
import io
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from database import get_db, tenant_filter

DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
PAPERWORK_CATEGORIES = {"preconstruction_paperwork", "construction_paperwork", "closeout_paperwork"}


def _d(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _mdy(d: Optional[date]) -> str:
    return f"{d.month}/{d.day}/{d.strftime('%y')}" if d else ""


async def _calendar(db, tenant_id: str, project: dict):
    """(workweek set of weekday ints, holidays frozenset) for the project."""
    from services.working_days import FEDERAL_HOLIDAYS
    default = (frozenset({0, 1, 2, 3, 4}), FEDERAL_HOLIDAYS)
    if not project.get("calendar_id"):
        return default  # no project calendar: Monday-Friday, federal holidays
    cal = await db.calendars.find_one(tenant_filter(tenant_id, {"id": project["calendar_id"]}), {"_id": 0})
    if not cal:
        return default
    raw_hol = cal.get("extra_holidays") or cal.get("holidays") or []
    extra = frozenset(x for x in (_d(h) for h in raw_hol) if x)
    hol = (FEDERAL_HOLIDAYS | extra) if cal.get("use_federal", True) else extra
    if cal.get("workweek"):
        ww = frozenset(int(x) for x in cal["workweek"])
    elif cal.get("work_days_per_week"):
        ww = frozenset(range(max(1, min(7, int(cal["work_days_per_week"])))))
    else:
        ww = default[0]
    return ww, hol


def _is_field_work(t: dict) -> bool:
    return (t.get("work_category") or "field_work") not in PAPERWORK_CATEGORIES


def _done(t: dict) -> bool:
    return t.get("status") in ("complete", "deleted") or float(t.get("pct_complete") or 0) >= 100


def task_span(t: dict, start: date, end: date) -> tuple[Optional[date], Optional[date]]:
    """The dates an activity is worked inside [start, end], or (None, None)."""
    started = bool(t.get("actual_start")) or float(t.get("pct_complete") or 0) > 0
    s = _d(t.get("actual_start")) if t.get("actual_start") else _d(t.get("planned_start"))
    if s is None:
        return None, None
    if not started and s < start:
        s = start  # should have started already: it runs from the first day
    f = _d(t.get("planned_finish"))
    if f is None or f < start:
        # Overdue (or no finish on record) and not done: still to be worked.
        # Use the forecast when it reaches the period, else the whole period.
        pf = _d(t.get("projected_finish_date"))
        f = pf if pf is not None and pf >= start else end
    s, f = max(s, start), min(f, end)
    return (s, f) if s <= f else (None, None)


async def gather_lookahead_grid(*, tenant_id: str, project_id: str,
                                start_date: Optional[date] = None) -> dict:
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(tenant_id, {"id": project_id}), {"_id": 0})
    if not proj:
        return {"project": None}
    from services.site_time import project_today
    today = project_today(proj.get("timezone"))
    start = monday_of(start_date or today)
    end = start + timedelta(days=13)
    days = [start + timedelta(days=i) for i in range(14)]
    workweek, holidays = await _calendar(db, tenant_id, proj)
    working = [d.weekday() in workweek and d not in holidays for d in days]

    # Every unfinished activity, judged on the schedule's own dates (not the
    # forecast): a stretched forecast on one activity must not push the next
    # ones off the owner's form.
    # Unplanned field work is not part of the schedule (2026-11-18), reviewed or not.
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id,
                                  "status": {"$nin": ["deleted", "complete"]},
                                  "created_in_field": {"$ne": True},
                                  "planned_start": {"$lte": end.isoformat()}}),
        {"_id": 0, "activity_code": 1, "description": 1, "planned_start": 1, "planned_finish": 1,
         "actual_start": 1, "pct_complete": 1, "status": 1, "work_category": 1,
         "projected_finish_date": 1},
    ).sort("planned_start", 1).to_list(20000)

    rows, notes = [], []
    for t in tasks:
        if _done(t):
            continue
        s, f = task_span(t, start, end)
        if s is None:
            continue
        started = bool(t.get("actual_start")) or float(t.get("pct_complete") or 0) > 0
        if not _is_field_work(t):
            ps, pf = _d(t.get("planned_start")), _d(t.get("planned_finish"))
            planned_in = bool(ps and pf and ps <= end and pf >= start)
            if not (started or planned_in):
                continue  # old paperwork never started is tracked on its own pages
            pct = float(t.get("pct_complete") or 0)
            fin = _d(t.get("planned_finish"))
            notes.append(f"{t.get('description') or t.get('activity_code')}: "
                         f"{'%g%% complete' % pct if pct else 'in progress' if t.get('actual_start') else 'starts ' + _mdy(_d(t.get('planned_start')))}"
                         f"{', finish ' + _mdy(fin) if fin else ''}")
            continue
        marks = [working[i] and s <= d <= f for i, d in enumerate(days)]
        if not any(marks):
            continue
        rows.append({"name": t.get("description") or t.get("activity_code") or "",
                     "code": t.get("activity_code"), "marks": marks,
                     "pct": float(t.get("pct_complete") or 0),
                     "planned_start": t.get("planned_start"), "planned_finish": t.get("planned_finish"),
                     "actual_start": t.get("actual_start"),
                     "sort": (s, t.get("planned_start") or "")})
    rows.sort(key=lambda r: (r["sort"][0], str(r["sort"][1]), r["name"]))

    delaying = []
    async for dl in db.delays.find(
            tenant_filter(tenant_id, {"project_id": project_id, "status": {"$in": ["open", "in_progress"]}}),
            {"_id": 0, "description": 1}).sort("reported_at", 1).limit(20):
        if dl.get("description"):
            delaying.append(str(dl["description"]).strip())
    async for t in db.tasks.find(
            tenant_filter(tenant_id, {"project_id": project_id, "has_open_blocker": True,
                                      "created_in_field": {"$ne": True},
                                      "status": {"$nin": ["complete", "deleted"]}}),
            {"_id": 0, "description": 1, "open_blocker_summary": 1}).limit(20):
        summ = t.get("open_blocker_summary")
        if summ:
            delaying.append(f"{t.get('description')}: {summ}")

    addr = ", ".join(x for x in [proj.get("address_line1"), proj.get("city"),
                                 " ".join(y for y in [proj.get("state"), proj.get("postal_code")] if y)] if x)
    # Header block, top left: the project's own facts. A client that wants
    # its own branding uploads its form (services/lookahead_template_export).
    place = ", ".join(y for y in [proj.get("client_name"), addr] if y)
    header = []  # (text, kind) kind: title | line
    if proj.get("name"):
        header.append((proj["name"], "title"))
    if place:
        header.append((place, "line"))
    if proj.get("contractor_name"):
        header.append((f"Prepared by: {proj['contractor_name']}", "line"))
    return {
        "project": proj,
        "header": header,
        "footer": "",
        "title_lines": [x for x in [proj.get("name"), place] if x],
        "contract_number": proj.get("contract_number") or "",
        "project_number": proj.get("station_id") or proj.get("project_number") or "",
        "contractor": proj.get("contractor_name") or "",
        "start": start, "end": end, "days": days, "working": working,
        "holidays_in_period": [d for d in days if d in holidays],
        "date_issued": today,
        "rows": rows, "notes": notes, "delaying": delaying,
    }


# ---------------------------------------------------------------- Excel
def render_grid_xlsx(g: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from services.xlsx_safe import set_text_cell

    wb = Workbook()
    ws = wb.active
    ws.title = "2-Week Look Ahead"
    thin = Side(style="thin", color="000000")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(name="Arial", bold=True, size=10)
    norm = Font(name="Arial", size=10)
    grey = PatternFill("solid", start_color="D9D9D9")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.column_dimensions["A"].width = 46
    for i in range(14):
        ws.column_dimensions[chr(ord("B") + i)].width = 6.2

    styles = {"title": Font(name="Arial", bold=True, size=11), "line": Font(name="Arial", size=10)}
    for i, (txt, kind) in enumerate(g["header"], start=1):
        c = ws.cell(row=i, column=1)
        set_text_cell(c, txt)
        c.font = styles[kind]
        ws.merge_cells(start_row=i, start_column=1, end_row=i, end_column=8)
    right = [
        ("2 WEEKS LOOK AHEAD", True),
        (f"Contract No.: {g['contract_number']}", False),
        (f"Project No.: {g['project_number']}", False),
        (f"Period: {_mdy(g['start'])} to {_mdy(g['end'])}", False),
        (f"Date Issued: {_mdy(g['date_issued'])}", False),
    ]
    for i, (txt, b) in enumerate(right, start=1):
        c = ws.cell(row=i, column=9)
        set_text_cell(c, txt)
        c.font = Font(name="Arial", bold=b, size=11 if b else 10)
        ws.merge_cells(start_row=i, start_column=9, end_row=i, end_column=15)
    r0 = max(len(g["header"]), len(right)) + 2
    ws.cell(row=r0, column=1, value="ACTIVITIES")
    ws.merge_cells(start_row=r0, start_column=1, end_row=r0 + 2, end_column=1)
    ws.cell(row=r0, column=2, value="WEEK 1")
    ws.merge_cells(start_row=r0, start_column=2, end_row=r0, end_column=8)
    ws.cell(row=r0, column=9, value="WEEK 2")
    ws.merge_cells(start_row=r0, start_column=9, end_row=r0, end_column=15)
    for i, d in enumerate(g["days"]):
        ws.cell(row=r0 + 1, column=2 + i, value=DAY_NAMES[d.weekday()])
        ws.cell(row=r0 + 2, column=2 + i, value=f"{d.month}/{d.day}")
    for rr in range(r0, r0 + 3):
        for cc in range(1, 16):
            c = ws.cell(row=rr, column=cc)
            c.font, c.alignment, c.border, c.fill = bold, center, box, grey

    r = r0 + 3
    data_rows = g["rows"] or [{"name": "(no field work scheduled in this period)", "marks": [False] * 14}]
    for row in data_rows:
        c = ws.cell(row=r, column=1)
        set_text_cell(c, row["name"])
        c.font, c.border = norm, box
        c.alignment = Alignment(vertical="center", wrap_text=True)
        for i, m in enumerate(row["marks"]):
            cell = ws.cell(row=r, column=2 + i, value="X" if m else None)
            cell.font, cell.alignment, cell.border = bold, center, box
            if not g["working"][i]:
                cell.fill = PatternFill("solid", start_color="F2F2F2")
        r += 1
    for _ in range(max(0, 12 - len(data_rows))):  # blank lines for hand additions
        for cc in range(1, 16):
            ws.cell(row=r, column=cc).border = box
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Notes:").font = bold
    ws.cell(row=r, column=9, value="Items Delaying the Project:").font = bold
    ws.merge_cells(start_row=r, start_column=9, end_row=r, end_column=15)
    notes = list(g["notes"])
    if g["holidays_in_period"]:
        notes.append("No work on " + ", ".join(_mdy(d) for d in g["holidays_in_period"]) + " (holiday).")
    n = max(len(notes), len(g["delaying"]), 4)
    for i in range(n):
        a = ws.cell(row=r + 1 + i, column=1)
        set_text_cell(a, notes[i] if i < len(notes) else "")
        b = ws.cell(row=r + 1 + i, column=9)
        set_text_cell(b, g["delaying"][i] if i < len(g["delaying"]) else "")
        ws.merge_cells(start_row=r + 1 + i, start_column=1, end_row=r + 1 + i, end_column=8)
        ws.merge_cells(start_row=r + 1 + i, start_column=9, end_row=r + 1 + i, end_column=15)
        for c in (a, b):
            c.font = norm
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for cc in range(1, 16):
            ws.cell(row=r + 1 + i, column=cc).border = Border(bottom=Side(style="hair", color="999999"))

    last = r + n
    if g["footer"]:
        last += 2
        c = ws.cell(row=last, column=1)
        set_text_cell(c, g["footer"])
        c.font = Font(name="Arial", bold=True, size=9)
        c.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=last, start_column=1, end_row=last, end_column=15)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_area = f"A1:O{last}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- PDF
def _grid_pdf_html(g: dict) -> tuple[str, str]:
    e = lambda s: _html.escape(str(s or ""))
    head_days = "".join(f"<th class='{'' if w else 'off'}'>{DAY_NAMES[d.weekday()]}<br>{d.month}/{d.day}</th>"
                        for d, w in zip(g["days"], g["working"]))
    body = ""
    data_rows = g["rows"] or [{"name": "(no field work scheduled in this period)", "marks": [False] * 14}]
    for row in data_rows:
        cells = "".join(f"<td class='x {'' if w else 'off'}'>{'X' if m else ''}</td>"
                        for m, w in zip(row["marks"], g["working"]))
        body += f"<tr><td class='act'>{e(row['name'])}</td>{cells}</tr>"
    for _ in range(max(0, 8 - len(data_rows))):
        body += "<tr><td class='act'>&nbsp;</td>" + "<td class='x'></td>" * 14 + "</tr>"
    notes = list(g["notes"])
    if g["holidays_in_period"]:
        notes.append("No work on " + ", ".join(_mdy(d) for d in g["holidays_in_period"]) + " (holiday).")
    li = lambda xs: "".join(f"<li>{e(x)}</li>" for x in xs) or "<li>&nbsp;</li>"
    title = "".join(f"<div class='{kind}'>{e(txt)}</div>" for txt, kind in g["header"])
    prep = ""
    foot = f"<div class='footer'>{e(g['footer'])}</div>" if g["footer"] else ""
    html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>2 Weeks Look Ahead</title></head><body>
    <table class='hdr'><tr><td>{title}{prep}</td>
      <td class='right'><div class='t1'>2 WEEKS LOOK AHEAD</div>
        <div>Contract No.: {e(g['contract_number'])}</div><div>Project No.: {e(g['project_number'])}</div>
        <div>Period: {_mdy(g['start'])} to {_mdy(g['end'])}</div><div>Date Issued: {_mdy(g['date_issued'])}</div></td></tr></table>
    <table class='grid'><thead>
      <tr><th rowspan='2' class='act'>ACTIVITIES</th><th colspan='7'>WEEK 1</th><th colspan='7'>WEEK 2</th></tr>
      <tr>{head_days}</tr></thead><tbody>{body}</tbody></table>
    <table class='boxes'><tr><td><b>Notes:</b><ul>{li(notes)}</ul></td>
      <td><b>Items Delaying the Project:</b><ul>{li(g['delaying'])}</ul></td></tr></table>
    {foot}
    </body></html>"""
    css = """
    @page { size: letter landscape; margin: 0.45in; }
    body { font-family: Arial, Helvetica, sans-serif; font-size: 9pt; color: #000; }
    table.hdr { width: 100%; margin-bottom: 10px; }
    table.hdr td { vertical-align: top; }
    td.right { width: 34%; }
    .t1 { font-weight: bold; font-size: 12pt; }
    .t2, .line { font-size: 9.5pt; }


    .title { font-weight: bold; font-size: 11pt; }
    .footer { text-align: center; font-weight: bold; font-size: 8.5pt; margin-top: 14px; }
    table.grid { width: 100%; border-collapse: collapse; table-layout: fixed; }
    table.grid th, table.grid td { border: 1px solid #000; padding: 3px 2px; }
    table.grid th { background: #d9d9d9; text-align: center; font-size: 8pt; }
    th.act, td.act { width: 34%; text-align: left; }
    td.x { text-align: center; font-weight: bold; }
    .off { background: #f2f2f2; }
    table.boxes { width: 100%; margin-top: 12px; border-collapse: collapse; }
    table.boxes td { width: 50%; vertical-align: top; border: 1px solid #000; padding: 6px; height: 1.1in; }
    ul { margin: 4px 0 0 16px; padding: 0; }
    """
    return html, css


def render_grid_pdf(g: dict) -> bytes:
    from services.pdf_safety import render_pdf, safe_css
    html, css = _grid_pdf_html(g)
    return render_pdf(html, stylesheets=[safe_css(css)])


async def render_lookahead_grid_export(*, tenant_id: str, project_id: str, fmt: str,
                                       start_date: Optional[date] = None):
    g = await gather_lookahead_grid(tenant_id=tenant_id, project_id=project_id, start_date=start_date)
    if not g.get("project"):
        return b"", "", "", {}
    if fmt == "xlsx":
        blob = render_grid_xlsx(g)
        ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif fmt == "pdf":
        from services.pdf_safety import arender_pdf, safe_css
        html, css = _grid_pdf_html(g)
        blob = await arender_pdf(html, stylesheets=[safe_css(css)])
        ct = "application/pdf"
    else:
        raise ValueError(f"unknown format: {fmt!r}")
    filename = f"two_week_look_ahead_{g['start'].strftime('%Y%m%d')}_to_{g['end'].strftime('%Y%m%d')}.{fmt}"
    meta = {"count": len(g["rows"]), "at_risk_count": 0, "window_start": g["start"].isoformat(),
            "window_end": g["end"].isoformat(), "days": 14, "layout": "grid"}
    return blob, ct, filename, meta
