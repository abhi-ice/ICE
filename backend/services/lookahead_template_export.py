"""Client look-ahead template export (2026-11-18).

Same idea as the client daily report export (services/dr_template_export.py):
a project uploads its client's own two-week look-ahead form (.xlsx), and the
export fills THAT form -- the client's branding, layout and wording stay
exactly as they are -- with the project's look-ahead.

Pipeline:
  1. Read every non-empty cell and box (merged range) of the uploaded form
     (shared with the daily report export).
  2. Build the look-ahead data: the period (14 days from a Monday, each day
     with its date, weekday and whether it is a working day), the field
     activities with the days they are worked, paperwork and procurement
     under way, open delays, and the project's header facts. The data comes
     from services/lookahead_grid_export.gather_lookahead_grid, so the client
     form and the built-in grid always agree.
  3. The AI maps that data onto the form by meaning (the same model and
     client as the daily report export) and returns {sheet: {cell: value}}.
  4. The values are written into the client's workbook: formula cells are
     never touched, layout is kept, long text wraps; a value written as a
     date ("YYYY-MM-DD") becomes a real Excel date so the form's own date
     formulas (day+1, the period title) keep working.

Nothing here is specific to one client or form.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from services.dr_template_export import (
    DrTemplateExportError, _cell_safe, _extract_template_snapshot, _fit_rows,
    _is_writable, _parse_json_object, _track_usage,
)
from services.anthropic_compat import UserMessage
from database import get_db, tenant_filter

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A newer model than the daily report export: a look-ahead form needs the
# grid located exactly and notes kept out of the delay boxes.
MODEL_NAME = "claude-sonnet-5-5"
_MAX_TEMPLATE_CHARS = 300_000  # about 75k tokens of form; a look-ahead form is far smaller


def _lookahead_chat(session_id: str, system_message: str):
    from services.anthropic_compat import LlmChat
    from services.dr_template_export import _key
    return LlmChat(api_key=_key(), session_id=session_id,
                   system_message=system_message).with_model("anthropic", MODEL_NAME)


def _mdy(d) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year}" if d else ""


def build_lookahead_snapshot(g: dict) -> dict:
    """The look-ahead as the AI sees it (from gather_lookahead_grid)."""
    proj = g["project"] or {}
    days = g["days"]
    activities = []
    for i, r in enumerate(g["rows"], start=1):
        worked = [d for d, m in zip(days, r["marks"]) if m]
        activities.append({
            "id": f"a{i}",
            "activity": r["name"],
            "activity_code": r.get("code"),
            "first_day_in_period": worked[0].isoformat() if worked else None,
            "last_day_in_period": worked[-1].isoformat() if worked else None,
            "worked_days": [d.isoformat() for d in worked],
            "percent_complete": r.get("pct"),
            "start": r.get("actual_start") or r.get("planned_start"),
            "finish": r.get("planned_finish"),
        })
    return {
        "project": {
            "project_name": proj.get("name"),
            "client_name": proj.get("client_name"),
            "contract_number": proj.get("contract_number"),
            "project_number": proj.get("station_id"),
            "contractor_name": proj.get("contractor_name"),
            "address_line1": proj.get("address_line1"),
            "city": proj.get("city"), "state": proj.get("state"), "postal_code": proj.get("postal_code"),
        },
        "period": {
            "start": g["start"].isoformat(), "end": g["end"].isoformat(),
            "start_mmddyyyy": _mdy(g["start"]), "end_mmddyyyy": _mdy(g["end"]),
            "date_issued": g["date_issued"].isoformat(),
            "date_issued_mmddyyyy": _mdy(g["date_issued"]),
        },
        "days": [{"date": d.isoformat(), "weekday": d.strftime("%A"), "working_day": w}
                 for d, w in zip(days, g["working"])],
        "holidays_in_period": [d.isoformat() for d in g["holidays_in_period"]],
        "activities": activities,
        "notes_for_the_notes_area": g["notes"]
        + [f"No work on {_mdy(d)} (holiday)." for d in g["holidays_in_period"]],
        "items_delaying_the_project": g["delaying"],
    }


SYSTEM_PROMPT = """You are a semantic data-mapping engine for construction two-week look-ahead
exports inside ICE Schedule Ops.

You receive two structured payloads:

1. TEMPLATE -- a CLIENT'S own look-ahead form (.xlsx), per worksheet: every non-empty
   cell (address, row, col, value, is_formula) plus merged_ranges (the form's boxes).
   Cells usually hold LABELS, headings, branding, day names, or formulas.

2. LOOK-AHEAD DATA -- our project header facts, the period (14 days with date,
   weekday and working_day), the field activities with the exact days each is worked
   (worked_days), paperwork and procurement under way, items delaying the project,
   and holidays in the period.

YOUR JOB
--------
Return a JSON object mapping cell addresses to values so the client's form shows
this look-ahead in the client's own format. Map by MEANING, not literal text. The
client's branding, titles, logos, instructions and footers stay as they are.

For each cell decide:
    FILL   -> "<address>": <value>
    BLANK  -> "<address>": null   (stale sample data left in the form: old activity
                                   names, old X marks, old dates typed as values)
    LEAVE  -> omit the address    (labels, headings, branding, day-name headers,
                                   instructions, footers, any is_formula cell)

HOW TO READ THE FORM
--------------------
- Header items (project name, contract / project number, contractor, period, date
  issued, phase, location): fill the value cell next to or below the label, or
  rewrite a single "Label: value" cell keeping the label. Header text the form
  already shows for this project (its own title, client name, contract number) is
  the client's wording: LEAVE it. A header item we have no data for (for example
  "Phase", "Area", "Superintendent" when not in the data) stays EMPTY: never guess
  one.
- Period: if the form has ONE start-date input cell that the other date cells or the
  period title compute from by formula, fill ONLY that cell with period.start as
  "YYYY-MM-DD" and leave every formula alone. If the date cells are plain values,
  fill each with its date as "YYYY-MM-DD". A text "Period: __ to __" cell gets
  "<start_mmddyyyy> to <end_mmddyyyy>" after its label. "Date Issued" gets
  date_issued_mmddyyyy.
- Day grid (a column for each day, often MON..SUN for two weeks): put one activity
  per row, top-down, in the activity-name column (fill the names in the mapping as
  usual). Do NOT write the day marks yourself. Instead add one extra top-level key
  to your JSON:
      "__day_grid__": {
        "sheet": "<sheet name>",
        "mark": "X",                       (the form's own mark if it shows one)
        "date_columns": {"<YYYY-MM-DD>": "<column letter>", ...},   (all days)
        "activity_rows": {"<activity id from the data, e.g. a1>": <row number>, ...},
        "first_row": <first activity row>, "last_row": <last activity row of the grid>
      }
  Match each day column to its date by the form's date row, or, if it has none, by
  position (the first day column is period.start, the next the following day...).
  The program writes the marks from worked_days and clears stale marks.
  A form whose weeks are separate tables (Week 2 below Week 1 in the same columns,
  or one sheet per week): make "__day_grid__" a LIST with one such object per table,
  each listing only that table's dates and its own rows for the activities.
- Table layout (columns like Activity / Start / Finish / Duration / % / Responsible):
  one activity per row; Start = the activity's start, Finish = its finish (MM/DD/YYYY
  text unless the column clearly holds real dates); % = percent_complete.
- Week columns (WEEK 1 / WEEK 2 without days): mark the week(s) the activity is
  worked in.
- More activities than rows: fill every row, then list the rest in the notes area as
  "Also: <activity> (<first day> to <last day>)".
- Activity text: the activity name in plain words, without activity codes unless the
  form has a code column.

NOTES AND DELAYS
----------------
- A "Notes" / "Remarks" / "Comments" area gets notes_for_the_notes_area, one item per
  line, and nothing else from us.
- An "Items Delaying the Project" / "Delays" / "Issues" / "Constraints" / "Owner
  Action Needed" / "Hold Points" area gets ONLY items_delaying_the_project, one per
  line. When items_delaying_the_project is EMPTY, that area and the lines under it
  get NOTHING -- do not put notes_for_the_notes_area there, even if the form has no
  other place for them. If the form has no delay area, put delay items in the notes
  area prefixed "Delay: ".
- A form with no notes area: leave paperwork and holiday notes out (a per-row
  "Comments" column is for that row's activity only).
- An area with several unlabeled lines: one item per line. A single box: all items
  on separate lines in one value (use "\\n").
- Leave an area empty when there is nothing for it.

GENERAL RULES
-------------
- Never invent values; never write a mark on a day not in worked_days.
- Never target a cell whose is_formula is true.
- Write only to the top-left cell of a merged box.
- Dates for date cells: "YYYY-MM-DD" strings (they are written as real Excel dates).
  Other dates in text: MM/DD/YYYY. Numbers as JSON numbers.
- When in doubt whether a cell is a label or stale data, LEAVE it.

OUTPUT FORMAT (STRICT)
----------------------
One valid JSON object: {"<sheet_name>": {"<cell_address>": <value or null>, ...}, ...}
Only sheets with at least one FILL or BLANK. No markdown, no code fences, no prose.
If nothing can be mapped with reasonable confidence, return {}.
"""


def _to_cell_value(cell, value):
    """A "YYYY-MM-DD" string becomes a real date (so the form's own date
    formulas keep working); everything else goes through the same safety
    rules as the daily report export."""
    if isinstance(value, str) and _ISO_DATE.match(value.strip()):
        try:
            d = datetime.strptime(value.strip(), "%Y-%m-%d")
            if not cell.is_date:
                cell.number_format = "m/d/yy"
            return d
        except ValueError:
            pass
    return _cell_safe(value)


_MARKS_OK = {"X", "x", "\u2713", "\u2714", "\u25cf", "\u2022", "*", "1", "\u25a0"}
_MAX_COL = 16384
_MAX_ROW = 1048576


def _clean_mark(m) -> str:
    m = str(m).strip() if isinstance(m, (str, int)) else ""
    return m if m in _MARKS_OK else "X"


def apply_day_grid(wb, spec, grid_data: dict) -> int:
    """Write the day marks the program computed (never the AI's guess) into
    the grid(s) the AI located. spec is one block or a list of blocks (forms
    whose weeks are separate tables). Badly shaped parts are skipped."""
    if isinstance(spec, list):
        return sum(apply_day_grid(wb, b, grid_data) for b in spec[:12])
    if not isinstance(spec, dict) or not isinstance(spec.get("sheet"), str):
        return 0
    from openpyxl.utils import column_index_from_string
    ws = {w.title: w for w in wb.worksheets}.get(spec["sheet"])
    if ws is None:
        return 0
    mark = _clean_mark(spec.get("mark") or "X")
    cols = {}
    date_columns = spec.get("date_columns")
    if isinstance(date_columns, dict):
        for d, letter in list(date_columns.items())[:62]:
            try:
                c = column_index_from_string(str(letter).strip().upper())
            except (ValueError, AttributeError, TypeError):
                continue
            if 1 <= c <= _MAX_COL:
                cols[str(d)[:10]] = c
    if not cols:
        return 0
    by_id = {a.get("id"): set(a.get("worked_days") or []) for a in grid_data.get("activities", [])}
    by_name = {}
    for a in grid_data.get("activities", []):
        by_name.setdefault(" ".join(str(a.get("activity") or "").casefold().split()), set(a.get("worked_days") or []))
    rows = {}
    activity_rows = spec.get("activity_rows")
    if isinstance(activity_rows, dict):
        for key, r in list(activity_rows.items())[:500]:
            try:
                r = int(r)
            except (TypeError, ValueError):
                continue
            if 1 <= r <= _MAX_ROW:
                rows[str(key)] = r
    try:
        first, last = int(spec.get("first_row") or 0), int(spec.get("last_row") or 0)
    except (TypeError, ValueError):
        first = last = 0
    used = set(rows.values())
    clear_rows = set(range(first, last + 1)) if 0 < first <= last <= first + 300 else set(used)
    stale = {mark.upper(), "X", "\u2713", "\u2714", "\u25cf"}
    for r in clear_rows | used:
        for c in cols.values():
            cell = ws.cell(row=r, column=c)
            if _is_writable(cell) and type(cell).__name__ != "MergedCell" and cell.value is not None \
                    and str(cell.value).strip().upper() in stale:
                cell.value = None
    n = 0
    for key, r in rows.items():
        days = by_id.get(key)
        if days is None:  # the AI keyed by name after all
            days = by_name.get(" ".join(key.casefold().split()))
        if days is None:
            continue
        for d, c in cols.items():
            if d in days:
                cell = ws.cell(row=r, column=c)
                if _is_writable(cell) and type(cell).__name__ != "MergedCell":
                    cell.value = mark
                    n += 1
    return n


def apply_mapping(wb, mapping: dict) -> int:
    """Write the AI's {sheet: {cell: value}} into the workbook. Returns the
    number of cells written. Formula cells and the inner cells of merged
    boxes are skipped."""
    from services.xlsx_safe import set_text_cell
    sheet_index = {ws.title: ws for ws in wb.worksheets}
    count = 0
    for sheet_name, cells in (mapping or {}).items():
        if sheet_name.startswith("__"):
            continue  # directives such as __day_grid__, handled separately
        ws = sheet_index.get(sheet_name)
        if ws is None or not isinstance(cells, dict):
            continue
        written = []
        for addr, value in cells.items():
            if str(addr).startswith("__"):
                continue
            try:
                cell = ws[addr]
            except Exception:
                continue
            if not _is_writable(cell) or type(cell).__name__ == "MergedCell":
                continue
            try:
                v = _to_cell_value(cell, value)
                if isinstance(v, datetime):
                    cell.value = v
                else:
                    set_text_cell(cell, v)
            except (AttributeError, TypeError, ValueError):
                continue
            written.append(cell)
            count += 1
        try:
            _fit_rows(ws, written)
        except Exception:
            pass  # row sizing is cosmetic
    return count


async def fill_lookahead_template(template_bytes: bytes, *, grid: dict,
                                  tenant_id: str, request_id: str,
                                  chat_factory=None) -> bytes:
    """Fill the client's look-ahead form. Raises DrTemplateExportError when
    the AI is unavailable or maps nothing (the caller answers 422)."""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(BytesIO(template_bytes))
        template_snapshot = _extract_template_snapshot(wb)
    except Exception as e:
        raise DrTemplateExportError(
            "The uploaded client form could not be opened. Upload it again.") from e
    data = build_lookahead_snapshot(grid)
    template_json = json.dumps(template_snapshot, default=str, separators=(",", ":"))
    if len(template_json) > _MAX_TEMPLATE_CHARS:
        raise DrTemplateExportError(
            "This client form is too large to fill automatically. Remove extra sheets "
            "or sample text and upload it again.")
    user_text = (
        "TEMPLATE (per-worksheet, only non-empty cells listed):\n"
        f"{template_json}\n\n"
        "LOOK-AHEAD DATA:\n"
        f"{json.dumps(data, default=str, indent=2)}\n\n"
        "Return the JSON object now: { \"<sheet_name>\": { \"<cell_address>\": <value>, ... } }. "
        "No prose, no fences."
    )
    if not data["items_delaying_the_project"]:
        user_text += (
            "\n\nReminder: items_delaying_the_project is empty, so every area labelled for "
            "delays, constraints, issues, hold points or owner action stays EMPTY. Notes go "
            "only to an area labelled for notes, remarks or comments; with no such area, "
            "leave the notes out.")
    started = datetime.now(timezone.utc)
    try:
        chat = (chat_factory or _lookahead_chat)(f"lookahead-template-{request_id}", SYSTEM_PROMPT)
        resp = await chat.send_message(UserMessage(text=user_text))
    except Exception as e:
        ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        await _track_usage(tenant_id, request_id, ms, ok=False, feature="lookahead_template_export")
        import logging
        logging.getLogger(__name__).warning("look-ahead template AI call failed: %s", e)
        raise DrTemplateExportError(
            "The AI that fills client forms could not be reached. Try again in a minute.") from e
    ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    mapping = _parse_json_object(resp if isinstance(resp, str) else str(resp))
    await _track_usage(tenant_id, request_id, ms, ok=bool(mapping), feature="lookahead_template_export")
    if not mapping:
        raise DrTemplateExportError(
            "The AI could not map the look-ahead onto this form. Check that the "
            "uploaded template is a look-ahead form, or try again.")
    # The day-grid directive may come at the top level or inside a sheet.
    spec = mapping.get("__day_grid__")
    for sheet_name, cells in mapping.items():
        inner = cells.get("__day_grid__") if isinstance(cells, dict) else None
        if not spec and isinstance(inner, dict):
            spec = {"sheet": sheet_name, **inner}
        elif not spec and isinstance(inner, list):
            spec = [{"sheet": sheet_name, **b} if isinstance(b, dict) and "sheet" not in b else b for b in inner]
    try:
        apply_mapping(wb, mapping)
        if spec:
            apply_day_grid(wb, spec, data)
        out = BytesIO()
        wb.save(out)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("look-ahead template fill failed")
        raise DrTemplateExportError(
            f"The client form could not be filled ({type(e).__name__}). Try again, or upload "
            "the form again.") from e
    return out.getvalue()


async def render_lookahead_client_export(*, tenant_id: str, project_id: str,
                                         start_date=None, request_id: str = "") -> tuple:
    """(bytes, content_type, filename, meta) for the client-format export.
    (b'', '', '', {}) when the project is missing. Raises
    DrTemplateExportError (-> 422) for every user-facing failure."""
    from services.lookahead_grid_export import gather_lookahead_grid
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "id": 1, "lookahead_template_file_url": 1, "lookahead_template_file_name": 1})
    if not proj:
        return b"", "", "", {}
    if not proj.get("lookahead_template_file_url"):
        raise DrTemplateExportError(
            "No client look-ahead template uploaded for this project. Upload one first.")
    from services.ai_unavailable import ensure_ai_available, CLIENT_TEMPLATE_UNAVAILABLE_MESSAGE
    ensure_ai_available(CLIENT_TEMPLATE_UNAVAILABLE_MESSAGE)
    from starlette.concurrency import run_in_threadpool
    from services.storage import get_object
    try:
        template_bytes, _ct = await run_in_threadpool(get_object, proj["lookahead_template_file_url"])
    except Exception as e:
        raise DrTemplateExportError(
            f"Couldn't read the uploaded client template ({type(e).__name__}). "
            "Upload the template again and retry.") from e
    try:
        g = await gather_lookahead_grid(tenant_id=tenant_id, project_id=project_id, start_date=start_date)
    except Exception as e:
        raise DrTemplateExportError(f"The look-ahead could not be built ({type(e).__name__}).") from e
    blob = await fill_lookahead_template(template_bytes, grid=g, tenant_id=tenant_id, request_id=request_id)
    fn = f"look_ahead_{g['start'].strftime('%Y%m%d')}_to_{g['end'].strftime('%Y%m%d')}.xlsx"
    meta = {"count": len(g["rows"]), "at_risk_count": 0, "window_start": g["start"].isoformat(),
            "window_end": g["end"].isoformat(), "days": 14, "layout": "client"}
    return blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", fn, meta
