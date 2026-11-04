"""Feature Client DR Template Export (2026-05-20) -- AI-powered
semantic cell mapping for the client DR template export.

Replaces the prior keyword-scanning heuristic engine. The new pipeline:

  1. Use openpyxl to READ the structure of the uploaded xlsx template
     -- every non-empty cell on every worksheet, with its A1 address,
     row/column index, text, and whether the cell is a formula.
  2. Build a compact, structured snapshot of the Daily Report payload
     (project header, date, weather, signer, activities, crew
     presence, delays, notes).
  3. Send both to Anthropic's claude-sonnet-4-5-20250929 via the same
     emergentintegrations LlmChat shim used by every other AI feature
     in this codebase (services/ai_agents.py). The model returns a
     JSON object shaped:
         {"<sheet_name>": {"<cell_address>": <value>, ...}, ...}
     telling us exactly which cells should receive which values, based
     on SEMANTIC meaning (e.g. "GC" -> contractor_name) rather than
     literal label matching.
  4. Parse the JSON, then write the values back into the workbook with
     openpyxl. Cell-level guards:
        - Formula cells (data_type == 'f') are NEVER overwritten.
        - Formatting / font / borders / merges / column widths are
          left untouched (we only set cell.value), except that rows
          grow and long text wraps so it shows in full.
        - The workbook is round-tripped through BytesIO -- no disk I/O.

If the AI call fails the engine raises DrTemplateExportError. The
caller surfaces this as an HTTP 502 with a clear message; we never
fall back to the prior heuristic engine (per spec).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, date as date_cls, timezone
from io import BytesIO
from typing import Any, Optional

from emergentintegrations.llm.chat import LlmChat, UserMessage
from database import get_db
from models import gen_id, utc_now


MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"


class DrTemplateExportError(RuntimeError):
    """Raised when the AI mapping call fails. The router catches this
    and returns a 502 with the message attached -- we DO NOT fall back
    to the legacy heuristic engine."""


def _key() -> str:
    return os.environ.get("EMERGENT_LLM_KEY", "")


def _new_chat(session_id: str, system_message: str) -> LlmChat:
    """Same _new_chat shape as services/ai_agents.py so the export
    inherits the existing emergentintegrations -> Anthropic shim."""
    return LlmChat(
        api_key=_key(),
        session_id=session_id,
        system_message=system_message,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)


async def _track_usage(tenant_id: str, request_id: str, latency_ms: int, ok: bool):
    """Mirrors ai_agents._track_usage. Best-effort -- a write failure
    here must not block the export response."""
    try:
        db = get_db()
        await db.ai_usage_tracking.insert_one({
            "id": gen_id(),
            "tenant_id": tenant_id,
            "request_id": request_id,
            "service": MODEL_PROVIDER,
            "model": MODEL_NAME,
            "feature": "dr_template_export",
            "latency_ms": latency_ms,
            "ok": ok,
            "created_at": utc_now().isoformat(),
        })
    except Exception:
        pass


# ---- JSON-object parser (analogous to ai_agents._parse_json_array) ----
def _parse_json_object(text: str) -> dict:
    """Strip optional code fences, locate the outer { ... }, json.loads.
    Returns {} if parsing fails -- the caller treats that as a hard
    failure (raises DrTemplateExportError)."""
    if not text:
        return {}
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        text.strip(),
        flags=re.IGNORECASE | re.MULTILINE,
    )
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = json.loads(cleaned[start:end + 1])
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}


# ---- Date helpers (preserved from prior engine) -----------------------
def _format_date_mmddyyyy(d) -> str:
    if not d:
        return ""
    if isinstance(d, (datetime, date_cls)):
        return d.strftime("%m/%d/%Y")
    s = str(d)[:10]
    try:
        return date_cls.fromisoformat(s).strftime("%m/%d/%Y")
    except Exception:
        return s


def _day_of_week(d) -> str:
    if not d:
        return ""
    if isinstance(d, (datetime, date_cls)):
        return d.strftime("%A")
    try:
        return date_cls.fromisoformat(str(d)[:10]).strftime("%A")
    except Exception:
        return ""


def _is_writable(cell) -> bool:
    """Never overwrite a formula cell."""
    return getattr(cell, "data_type", None) != "f"


_ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _cell_safe(value):
    """A value Excel can hold: lists become lines, other objects text, and
    control characters (pasted or dictated notes) are dropped -- openpyxl
    refuses them and one bad cell must not fail the whole export."""
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(v) for v in value if v is not None)
    elif isinstance(value, dict):
        value = json.dumps(value, default=str)
    if isinstance(value, str):
        value = _ILLEGAL_XLSX_CHARS.sub("", value)
    return value


_EXCEL_MAX_ROW_HEIGHT = 409
_MIN_CHARS_PER_LINE = 6
_MAX_STACKED_LINES = 12


def _right_edge(ws) -> int:
    """The form's last column: its print area, else its last used column."""
    from openpyxl.utils.cell import range_boundaries
    try:
        ends = (range_boundaries(a.split("!")[-1].replace("$", ""))[2]
                for a in str(ws.print_area or "").split(","))
        return max(end for end in ends if end)
    except (TypeError, ValueError):
        return ws.max_column or 1


def _fit_rows(ws, written) -> None:
    """Make long text the export wrote show in full, on any client's form
    (their rows are sized for one short line): wrap text that would otherwise
    be cut off, and grow the row. Estimates lines from the box width, erring a
    line over since clipped text is worse than a spare line; only ever grows a
    row, never shrinks one."""
    import math
    from copy import copy
    default_w = ws.sheet_format.defaultColWidth or 8.43
    default_h = ws.sheet_format.defaultRowHeight or 15
    # Read widths without touching column_dimensions[...] -- indexing it
    # creates entries, which would be saved into the client's file. One
    # entry can cover several columns (min..max).
    widths: dict[int, Optional[float]] = {}
    hidden: set[int] = set()
    for dim in list(ws.column_dimensions.values()):
        for c in range(dim.min or 0, (dim.max or 0) + 1):
            widths[c] = dim.width if dim.customWidth else None
            if dim.hidden:
                hidden.add(c)

    def visible_width(c0: int, c1: int) -> float:
        return sum((widths.get(c) or default_w) for c in range(c0, c1 + 1) if c not in hidden)

    boxes = {(rng.min_row, rng.min_col): rng for rng in ws.merged_cells.ranges}

    def takes_run_on(row: int, col: int) -> bool:
        # _cells: ws.cell() would save a stray cell into the client's file.
        other = ws._cells.get((row, col))
        return (row, col) not in boxes and (other is None or (
            other.value is None and type(other).__name__ != "MergedCell"))

    edge = _right_edge(ws)
    need_by_row: dict[int, float] = {}
    auto_need: dict[int, float] = {}
    for cell in written:
        text = cell.value.strip() if isinstance(cell.value, str) else ""
        if not text:
            continue
        box = boxes.get((cell.row, cell.column))
        c0, c1 = (box.min_col, box.max_col) if box else (cell.column, cell.column)
        r0, r1 = (box.min_row, box.max_row) if box else (cell.row, cell.row)
        width = visible_width(c0, c1)
        size = float(cell.font.sz or 11)
        # Column width counts '0' digits at 11pt; capitals run about 30% wider.
        parts = [sum(1.3 if ch.isupper() else 1 for ch in part) * 1.08 for part in text.split("\n")]
        per_line = max(1, int(width * 11 / size))
        if per_line < _MIN_CHARS_PER_LINE:
            continue  # a hidden column or thin spacer: no height makes it readable
        lines = sum(max(1, math.ceil(part / per_line)) for part in parts)
        if lines <= 1:
            continue  # one line already fits the template's own row
        if not cell.alignment.wrap_text:
            if not box:
                # Left-aligned text runs on over empty cells beside it, up to
                # the next filled cell, box or the form's edge; line breaks
                # only show when wrapped.
                stop = c1 + 1
                if cell.alignment.horizontal in (None, "general", "left"):
                    while stop <= edge and takes_run_on(cell.row, stop):
                        stop += 1
                room = visible_width(c0, stop - 1) * 11 / size
                # Near enough: parts runs wide for safety, and a short value a
                # little over must not turn one row into seven.
                if len(parts) == 1 and (parts[0] <= room * 1.3 or (
                        stop > c1 + 1 and lines > _MAX_STACKED_LINES)):
                    continue  # it fits, or it's a narrow cell with free space,
                    # where a tall thin stack of one long line is worse
            alignment = copy(cell.alignment)
            alignment.wrap_text = True
            cell.alignment = alignment
        rows = [r for r in range(r0, r1 + 1)
                if not (r in ws.row_dimensions and ws.row_dimensions[r].hidden)]
        heights = {r: (ws.row_dimensions[r].height if r in ws.row_dimensions else None)
                   for r in rows}
        # 1.33: Calibri 11 prints at 14.55pt a line, Arial 12 at 15.
        needed = lines * size * 1.33
        if not box and heights.get(cell.row, 0) is None:
            # Excel fits an automatic row to wrapped text itself, unless a box
            # on the same row gives it a set height below.
            auto_need[cell.row] = max(auto_need.get(cell.row, 0), needed)
            continue
        extra = needed - sum((h or default_h) for h in heights.values())
        for r in reversed(rows):  # the last row first, others past Excel's limit
            if extra <= 0:
                break
            have = heights[r] or default_h
            grow = min(extra, _EXCEL_MAX_ROW_HEIGHT - have)
            need_by_row[r] = max(need_by_row.get(r, 0), have + grow)
            extra -= grow
    for r, h in need_by_row.items():
        ws.row_dimensions[r].height = min(round(max(h, auto_need.get(r, 0)), 1),
                                          _EXCEL_MAX_ROW_HEIGHT)


# ---- Template snapshot -----------------------------------------------
def _extract_template_snapshot(wb) -> dict:
    """Walk every worksheet and capture every non-empty cell as
    {address, row, col, value, is_formula}. The AI uses this list to
    locate label cells and infer where the empty value cell sits."""
    sheets: dict[str, dict] = {}
    for ws in wb.worksheets:
        cells: list[dict] = []
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                v = cell.value
                if isinstance(v, str) and not v.strip():
                    continue
                # Truncate huge values so we don't blow the context.
                if isinstance(v, str) and len(v) > 200:
                    v = v[:200] + "..."
                cells.append({
                    "address": cell.coordinate,
                    "row": cell.row,
                    "col": cell.column,
                    "value": v,
                    "is_formula": cell.data_type == "f",
                })
        sheets[ws.title] = {
            "max_row": ws.max_row or 0,
            "max_col": ws.max_column or 0,
            "non_empty_cells": cells,
            # The form's boxes. A blank template has no sample data, so
            # without these the model cannot tell a two-row header from
            # the first data row, or find an unlabeled value box.
            "merged_ranges": sorted(
                (str(r) for r in ws.merged_cells.ranges),
                key=lambda a: (int(re.sub(r"\D", " ", a).split()[0]), a))[:300],
        }
    return sheets


# ---- DR data snapshot ------------------------------------------------
# Coordination events are the whole project contact log. A client's form only
# gets real visits and meetings: never phone calls, emails, internal notes,
# money or talk with our own subs. Spoken ones carry none of these fields, so
# the prompt tells the AI to skip those by their wording.
_ON_SITE_CHANNELS = {None, "in_person", "meeting"}
_NOT_FOR_THE_CLIENT = {"payment", "sub_coordination"}


def _is_site_visit(e: dict) -> bool:
    return (e.get("channel") in _ON_SITE_CHANNELS and e.get("direction") != "internal"
            and e.get("topic") not in _NOT_FOR_THE_CLIENT)


def _delivery_status(d: dict, daily_report_id) -> Optional[str]:
    """A delivery typed into a day's report ("What was delivered") has arrived,
    but stays "scheduled": nothing on those forms sets a status. Spoken and
    project-level ones really can be expected loads."""
    status = d.get("status")
    if (status == "scheduled" and daily_report_id and d.get("source") != "voice_intake"
            and d.get("daily_report_id") == daily_report_id):
        return "received"
    return status


def _delivered_on(d: dict, report_date) -> bool:
    """Not cancelled, and not moved to another day (a re-dated delivery keeps
    the daily_report_id of the report it was logged on)."""
    moved_to = d.get("delivery_date")
    return d.get("status") != "cancelled" and (
        not moved_to or str(moved_to)[:10] == str(report_date)[:10])


def _build_dr_snapshot(
    *,
    dr: dict,
    project: dict,
    activities: list[dict],
    presence: list[dict],
    delays: list[dict],
    signer_name: str,
    site_log: Optional[dict] = None,
) -> dict:
    """Compact structured payload the AI consumes. Strips internal-only
    fields (tenant_id, audit hashes, _id, etc.) and renames a few keys
    so the AI doesn't have to learn our internal vocabulary."""
    p = project or {}
    dr = dr or {}

    def _act(a: dict) -> dict:
        return {
            "trade_code": a.get("trade_code"),
            "activity_code": a.get("activity_code"),
            "description": a.get("activity_description"),
            "headcount": a.get("crew_count"),
            "hours_worked": a.get("hours_worked"),
            "pct_complete_start": a.get("pct_complete_start_of_day"),
            "pct_complete_end": a.get("pct_complete_end_of_day"),
            "delay_minutes": a.get("delay_minutes"),
            "notes": a.get("notes"),
        }

    def _pres(p_row: dict) -> dict:
        hpc = p_row.get("hours_per_person") or 0
        hc = p_row.get("headcount") or 0
        return {
            "trade": p_row.get("trade"),
            "company": p_row.get("company_name") or p_row.get("sub_name"),
            "headcount": hc,
            "hours_per_person": hpc,
            "total_hours": p_row.get("total_hours") or (hc * hpc),
        }

    log = site_log or {}

    def _event(e: dict) -> dict:
        # Voice intake stores with_party / description instead.
        return {
            "who": e.get("other_party_name") or e.get("with_party"),
            "organization": e.get("other_party_org"),
            "role": e.get("other_party_role"),
            "summary": e.get("summary") or e.get("description"),
        }

    def _delay(d_row: dict) -> dict:
        return {
            "reason": d_row.get("delay_reason") or d_row.get("category"),
            "minutes_lost": d_row.get("minutes_lost"),
            "description": d_row.get("description"),
            "linked_activity_codes": d_row.get("affected_activity_codes"),
        }

    return {
        # The five core formal-header fields are hoisted to top-level
        # so the AI can map them into a client template's header block
        # without traversing nested paths. The richer `project` block
        # below carries the secondary fields (address, owner, type).
        "project_name": p.get("name"),
        "client_name": p.get("client_name"),
        "station_id": p.get("station_id"),
        "contract_number": p.get("contract_number"),
        "contractor_name": p.get("contractor_name"),
        "signer_name": signer_name or "",
        "report_date": _format_date_mmddyyyy(dr.get("report_date")),
        "day_of_week": _day_of_week(dr.get("report_date")),
        "status": dr.get("status"),
        "is_no_work_day": bool(dr.get("is_no_work_day")),
        "no_work_reason": dr.get("no_work_reason"),
        "no_work_note": dr.get("no_work_note"),
        "project": {
            "owner_name": p.get("owner_name"),
            "address_line1": p.get("address_line1"),
            "city": p.get("city"),
            "state": p.get("state"),
            "postal_code": p.get("postal_code"),
            "project_type": p.get("project_type"),
        },
        "weather": {
            "conditions": dr.get("weather_conditions"),
            "temp_high_f": dr.get("weather_temp_high_f"),
            "temp_low_f": dr.get("weather_temp_low_f"),
            "precipitation_in": dr.get("weather_precipitation_in"),
            "wind_speed_mph": dr.get("weather_wind_speed_mph"),
        },
        "activities": [_act(a) for a in (activities or [])],
        "crew_presence": [_pres(pr) for pr in (presence or [])],
        "delays": [_delay(d) for d in (delays or [])],
        "site_events": [_event(e) for e in log.get("site_events") or [] if _is_site_visit(e)],
        "deliveries": [{"vendor": d.get("vendor") or d.get("supplier_name"),
                        "description": d.get("description"),
                        "po_number": d.get("po_number"),
                        "status": _delivery_status(d, dr.get("id"))}
                       for d in log.get("deliveries") or []
                       if _delivered_on(d, dr.get("report_date"))],
        "notes": dr.get("notes"),
    }


# ---- System prompt ---------------------------------------------------
SYSTEM_PROMPT = """You are a semantic data-mapping engine for federal-construction daily-report
exports inside ICE Schedule Ops.

You receive two structured payloads on every call:

1. TEMPLATE — a CLIENT'S blank daily-report xlsx template, given per worksheet as a list
   of every non-empty cell with:
      - address (Excel A1 notation, e.g. "B5")
      - row, col (1-indexed)
      - value (the text the cell currently holds — usually a LABEL like "Date:",
        "Contractor:", section headers, or table headers like "Trade", "Description",
        "Crew", "Hours")
      - is_formula (true means the cell holds a formula -- you MUST NOT target it)

2. DR DATA — our internal Daily Report payload (project header, date, weather, signer,
   list of work activities, crew presence rows, delays and logged issues, site events
   (visits, meetings, inspections), deliveries, notes).

YOUR JOB
--------
Produce a JSON object that maps Excel cell addresses to the values that should be
written there, so the filled template represents the same daily report in the
CLIENT'S own format. The key insight is that the client's template uses its own
labels and layout; you must map by MEANING, not by literal text.

You own the FULL end-to-end fill. The caller does NOT pre-clear the template; the
output xlsx is built by starting from the original template and then applying YOUR
mapping cell-by-cell. So for every cell you must decide one of three things:

    FILL       -- return { "<address>": <value> }
                  Write a new value into this cell. Use this for value cells where
                  you have new DR data. Also use this to REWRITE a cell that holds a
                  combined "Label: value" string (e.g. the template cell reads
                  "STATION - VA Medical center, Bronx NY" -- you must return the
                  whole string with the new station, like
                  "STATION - " + our station_id).

    BLANK      -- return { "<address>": null }
                  Clear this cell. Use this for value cells that hold STALE prefilled
                  data left over from the previous fill (e.g. last week's foreman
                  name, an old trade row in the manpower table, a "Sunny" weather
                  entry from a prior day). The user wants the template to look like
                  a fresh fill for THIS daily report.

    LEAVE ALONE -- omit the cell address entirely from your output
                  Do NOT include the cell. Use this for everything that should
                  survive verbatim from the original template:
                    - agency/department headings ("DEPARTMENT OF VETERANS AFFAIRS")
                    - boilerplate / Paperwork Reduction Act blocks
                    - section labels ("Weather:", "Temperature:", "NAME OF
                      CONTRACTOR", "Date", "Day of Week:")
                    - table header rows ("SKILLED WORKERS", "UNSKILLED",
                      "LOCATION AND DESCRIPTION OF WORK", "EQUIPMENT", "FORCE",
                      "DELIVERY OF MATERIALS:")
                    - summary labels ("TOTAL", "SATISFACTORY")
                    - any cell whose is_formula is true (formulas are PRESERVED)
                    - styling-only cells (logos, separators)

Output shape (only sheets where you have at least one FILL or BLANK):
    {
      "<sheet_name>": {
        "<cell_address>": <value-to-fill>   OR   null,
        ...
      },
      ...
    }

SEMANTIC MAPPING REFERENCE
--------------------------
Match cell labels to DR data by MEANING, not literal text. Common equivalents:
     "Owner" / "GC" / "Prime Contractor" / "General Contractor" / "Contractor" /
     "NAME OF CONTRACTOR"
        -> our contractor_name  (top-level)
     "Project No." / "Job No." / "Contract Number" / "Contract #" / "Project #"
        -> our contract_number  (top-level)
     "Client" / "Customer" / "End User" / "Agency"
        -> our client_name  (top-level)
     "Station" / "Facility" / "Site ID" / "Location Code" / "Bldg #"
        -> our station_id  (top-level)
     "Project Name" / "Project Title" / "Site"
        -> our project_name  (top-level)
     "Address" / "Site Address" / "Location"
        -> our project.address_line1 (+ project.city / project.state /
           project.postal_code as separate cells if the template lays
           them out individually)
     "Signed by" / "Submitted by" / "Foreman" / "Prepared by" / "Signature" /
     "SIGNATURE OF SUPERVISOR" / "Name"
        -> our signer_name
     "Date" / "Report Date" / "Day Of"
        -> our report_date (MM/DD/YYYY)
     "Day" / "Day of Week"
        -> our day_of_week (full weekday name like "Wednesday")
     "Weather" / "Conditions" / "Sky"
        -> our weather.conditions
     "Hi" / "High" / "Max Temp" / "Temp High"
        -> our weather.temp_high_f (number)
     "Lo" / "Low" / "Min Temp" / "Temp Low"
        -> our weather.temp_low_f (number)
     "Precip" / "Precipitation" / "Rainfall"
        -> our weather.precipitation_in (number)
     "Wind"
        -> our weather.wind_speed_mph (number)
     "Notes" / "Remarks" / "Comments" / "Description of Work" / "Daily Narrative"
        -> our notes (full string -- one cell, even if long)
     "Visitors" / "Site Visits" / "Inspections" / "Meetings"
        -> our site_events
     "Deliveries" / "Delivery of Materials" / "Materials Received"
        -> our deliveries
     "Status" / "Report Status"
        -> our status

CELL-ADDRESS PATTERNS
---------------------
- LABEL adjacent to a separate VALUE cell: target the value cell (cell to the right
  of, or directly below, the label). LEAVE the label cell alone.
- "Label: value" in ONE cell (e.g. "STATION - VA Medical center, Bronx NY",
  "Contract Number:  526-25-100", "SIGNATURE OF SUPERVISOR : Camil Cherbaka"):
  REWRITE that single cell preserving the label prefix and substituting our value.
- Activity / manpower / equipment / delays tables: detect the header row (e.g.
  "SKILLED WORKERS", "UNSKILLED", "LOCATION AND DESCRIPTION OF WORK"; or "Trade",
  "Description", "Crew", "Hours"; or "EQUIPMENT", "FORCE"), then FILL the rows
  below the header with our records, ONE record per row, mapped column-by-column
  by header label. BLANK any leftover prefilled rows in the table that our data
  doesn't reach (so an old row of last week's data is cleared rather than left
  behind). LEAVE the header row alone.
- If the template has a section we have NO data for (e.g. an equipment list and we
  have no equipment records), BLANK any stale prefilled data rows in that section
  but LEAVE the section header.

READING THE FORM'S BOXES (merged_ranges)
----------------------------------------
Each worksheet also lists merged_ranges -- the form's boxes. A template is often
completely blank, so use the boxes to find where values go:
- Write ONLY to the top-left cell of a merged box (e.g. "C14" for C14:E14); other
  cells inside a box are ignored.
- Where a value goes: if the cell directly BELOW a label is empty and not inside
  any box, the value goes there ("Date" in A8 -> A9, "Day of Week:" in A10 -> A11).
  Otherwise use the empty box or cell to the RIGHT on the same row ("Weather:" in
  A6 with box C6:F6 -> C6; "High" in C7 -> D7). Leave the label alone.
- Header lines the template already fills in (station, project title, contract
  number, contractor name) are the client's own wording for this project: LEAVE
  them exactly as they are. Fill a header item only where the template leaves it
  empty.
- A table header box that spans several rows (e.g. "SKILLED WORKERS" in C12:E13)
  ends at its bottom row; the data rows start on the next row (14). Never write data
  into a header's rows.
- Identical boxes repeated on consecutive rows (C14:E14, C15:E15, ... C18:E18) are
  the table's data rows. Use them top-down, one record per row; the row labelled
  "TOTAL" after them is the totals row.
- Unlabeled full-width boxes below the main tables (e.g. A22:K22, A23:K23,
  A24:K24) are remarks lines: fill them as described under VISITS, DELIVERIES AND
  REMARKS below, one sentence or item per line.

VA DAILY REPORT (headers SKILLED WORKERS / UNSKILLED / LOCATION AND DESCRIPTION OF WORK)
- One row per company on site (from crew_presence, else from activities).
- SKILLED WORKERS cell: "<company> - <N> men" (e.g. "J&J Mechanical - 6 men").
- UNSKILLED cell: that company's unskilled headcount when we have it, else leave it.
- LOCATION AND DESCRIPTION OF WORK cell (same row): what that company worked on --
  the activity description and its notes, in plain words, without activity codes.
- TOTAL row: total skilled headcount under SKILLED WORKERS; unskilled total under
  UNSKILLED only when known.

VISITS, DELIVERIES AND REMARKS (every form)
-------------------------------------------
- site_events are the day's visits, meetings and inspections by people from outside
  our crews (owner, inspectors, designers, insurers and the like). Write each as one
  short line from its own fields: "<who> (<role or organization>) - <summary>". Skip
  any that reads as a phone call, email or text, an internal matter, money (prices,
  payments, change order costs, back-charges, claims), or routine talk with our own
  crews and subcontractors: this form goes to the client.
- deliveries: one line each, "<vendor> - <description>". Unless its status is received
  or complete, add the status so nothing reads as arrived when it hasn't:
  "<vendor> - <description> (scheduled)".
- A form with its own box or table for visitors or deliveries takes them there.
  When it has none, they go in the remarks instead.
- A remarks or notes area (a box, or several remarks lines) holds, in this order:
  site_events, then delays and issues logged that day (a title line, then its
  detail), then our notes. One item per line; a single box holds them all on separate
  lines. When there are more items than lines, put two short items on a line.
- Leave a visitors or deliveries box empty when we have none that day.

GENERAL RULES
-------------
- Never invent values. If we have no DR data for a value cell, BLANK it (null) --
  don't echo the label, don't paraphrase, don't synthesize.
- Never overwrite a cell whose is_formula is true. Omit it from the mapping.
- Numbers: emit as JSON numbers, not strings (72, not "72"). Dates: MM/DD/YYYY
  string. Day-of-week: full weekday name string.
- A cell address present with value `null` MEANS BLANK -- it is NOT the same as
  omitting the cell. Omitting means "leave the original template value as-is."
- Do NOT BLANK headers, labels, boilerplate, section titles, or formula cells.
  When in doubt about whether a cell is a label vs. a stale value, LEAVE IT ALONE.

OUTPUT FORMAT (STRICT)
----------------------
- Return ONE valid JSON object.
- Top-level keys are sheet names. Each value is itself an object whose keys are
  cell addresses (A1 notation) and whose values are EITHER the value to FILL
  OR the JSON literal null to BLANK.
- NO markdown, NO code fences, NO prose, NO trailing commas, NO comments.

If you cannot map a single cell with reasonable semantic confidence, return an
empty object: {}
"""


# ---- Public API ------------------------------------------------------
async def inject_dr_into_template(
    template_bytes: bytes,
    *,
    dr: dict,
    project: dict,
    activities: list[dict],
    presence: Optional[list[dict]] = None,
    delays: Optional[list[dict]] = None,
    site_log: Optional[dict] = None,
    signer_name: str,
    tenant_id: str,
    request_id: str,
) -> bytes:
    """Fill `template_bytes` (an .xlsx) with DR + project data using the
    AI mapping engine and return the filled workbook as bytes.

    Raises DrTemplateExportError if the AI call fails or returns
    something we can't parse. Callers must surface the error to the
    user -- there is NO fallback to the heuristic engine.
    """
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(template_bytes))
    template_snapshot = _extract_template_snapshot(wb)
    dr_snapshot = _build_dr_snapshot(
        dr=dr, project=project, activities=activities,
        presence=presence or [], delays=delays or [],
        signer_name=signer_name, site_log=site_log,
    )

    user_text = (
        "TEMPLATE (per-worksheet, only non-empty cells listed):\n"
        f"{json.dumps(template_snapshot, default=str, indent=2)}\n\n"
        "DR DATA:\n"
        f"{json.dumps(dr_snapshot, default=str, indent=2)}\n\n"
        "Return the JSON object now. Remember: shape is "
        "{ \"<sheet_name>\": { \"<cell_address>\": <value>, ... }, ... }. "
        "No prose, no fences."
    )

    started = datetime.now(timezone.utc)
    try:
        chat = _new_chat(f"dr-template-{request_id}", SYSTEM_PROMPT)
        resp = await chat.send_message(UserMessage(text=user_text))
    except Exception as e:
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        await _track_usage(tenant_id, request_id, latency_ms, ok=False)
        raise DrTemplateExportError(
            f"AI mapping engine unavailable: {e}"
        ) from e

    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    mapping = _parse_json_object(resp if isinstance(resp, str) else str(resp))
    await _track_usage(tenant_id, request_id, latency_ms, ok=bool(mapping))

    if not mapping:
        raise DrTemplateExportError(
            "AI mapping engine returned no cells. The template may not "
            "match a recognizable daily-report layout, or the response "
            "could not be parsed."
        )

    # 2026-05-21 -- apply the AI's per-cell intent. The mapping now
    # carries a three-way decision PER CELL:
    #   - cell address present, value != null  -> FILL with that value
    #     (this includes rewriting label+value cells like
    #      "STATION - <new station>" or "SIGNATURE OF SUPERVISOR : Abhi")
    #   - cell address present, value == null  -> BLANK the cell
    #     (the AI saw a stale prefilled value and decided we have no
    #      DR data for it; clear the leftover)
    #   - cell address absent                  -> LEAVE ALONE
    #     (labels, boilerplate, section headers, formulas the AI
    #      wanted to preserve verbatim)
    # Formula cells (data_type == 'f') are still never overwritten,
    # even if the AI mistakenly targets one. Fonts / borders / merges /
    # column widths / images are untouched; _fit_rows below is the only
    # layout change.
    sheet_index = {ws.title: ws for ws in wb.worksheets}
    for sheet_name, cells in mapping.items():
        ws = sheet_index.get(sheet_name)
        if ws is None or not isinstance(cells, dict):
            continue
        written = []
        for addr, value in cells.items():
            try:
                cell = ws[addr]
            except Exception:
                continue
            if not _is_writable(cell):
                continue
            try:
                cell.value = _cell_safe(value)  # None is a legitimate blank intent
            except (AttributeError, TypeError, ValueError):
                # Merged-cell secondaries / read-only cell subclasses
                # reject .value assignment -- skip without aborting.
                continue
            written.append(cell)
        try:
            _fit_rows(ws, written)
        except Exception:
            pass  # row sizing is cosmetic; never fail the export over it

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
