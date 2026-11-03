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
          left untouched (we only set cell.value).
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
    chat = LlmChat(
        api_key=_key(),
        session_id=session_id,
        system_message=system_message,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    # Same report, same placement every time: a cell map is not creative work.
    if hasattr(chat, "with_temperature"):
        chat = chat.with_temperature(0)
    return chat


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


# ---- Client header lines ---------------------------------------------
# Station, project title, contract number and contractor are the client's
# own wording for the project. A template that already fills one in keeps
# it -- the AI only fills a header item the template leaves empty. (VA 6B,
# 2026-11-03: the AI swapped "J&J Mechanical" for our tenant name and the
# station name for its number.)
_HEADER_WORD = (r"(?:(?:general|prime)\s+contractor|contractor|contract|station|facility|"
                r"project|client|job)(?:'s)?")
_HEADER_SEP = r"\s*(?:(?:[:#]|-|–|—)\s*|\s{2,}|$)"
_HEADER_LABEL = re.compile(
    rf"^\s*(?:{_HEADER_WORD}(?:\s*/\s*{_HEADER_WORD})*(?:\s+(?:name|title|no\.?|number|#|id))?"
    rf"|name\s+of\s+(?:the\s+)?(?:contractor|project|client)){_HEADER_SEP}(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
# "Contract No. 36C24218C0080", "Project Title Renovate 6B": an explicit
# name/number suffix may be followed by a single space.
_HEADER_LABEL_SUFFIXED = re.compile(
    rf"^\s*{_HEADER_WORD}(?:\s*/\s*{_HEADER_WORD})*\s+(?:name|title|no\.?|number|#|id)"
    r"\s+(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _header_match(text):
    if not isinstance(text, str):
        return None
    return _HEADER_LABEL.match(text) or _HEADER_LABEL_SUFFIXED.match(text)


# A line that also carries a per-report item ("Project No. 526   Date 11/02",
# "Contractor: J&J    Workers: 6", "Project: VA 6B Temp: 45F") is never locked:
# its per-report part must stay writable, and a lock is only a backstop.
_PER_REPORT_TAIL = re.compile(
    r"\s{2,}|[:#]|\b(?:date|day|report|workers?|men|hours?|temp|weather|shift|time|page)\b",
    re.IGNORECASE,
)
_CREW_COLUMNS = {
    "trade", "trades", "company", "subcontractor", "crew", "men", "no. of men", "# of men",
    "workers", "no. of workers", "# of workers", "number of workers", "headcount", "manpower",
    "hours", "description", "description of work", "work performed", "activity", "location",
    "skilled", "unskilled", "skilled workers", "equipment",
}
# Header lines live at the top of a form. Further down, "Owner - COR walkthrough"
# or "Station: 12+50 curb placed" are the day's remarks and must stay writable.
_HEADER_BAND_MAX_ROW = 10
_SCAN_MAX_ROW, _SCAN_MAX_COL = 200, 60
# Linear-time patterns only: this runs on every cell of the template.
_PLACEHOLDER = re.compile(r"\[[^\]]*\]|<[^>]*>|\([^)]*\)|tbd|tba|n/?a", re.IGNORECASE)
_FILLER_CHARS = re.compile(r"[xX_\-./\s]")
_COLUMN_WORDS = {
    "trade", "trades", "company", "subcontractor", "crew", "men", "no. of men", "# of men",
    "workers", "no. of workers", "# of workers", "headcount", "manpower", "hours",
    "description", "description of work", "work performed", "qty", "quantity",
    "location", "activity", "high", "low", "total", "date", "day", "time", "prec",
    "skilled", "unskilled", "equipment", "force", "remarks", "notes", "no", "#",
}


def _has_content(text) -> bool:
    """Real content -- not a blank, a '____' line or a placeholder like
    '[name]', '(Insert Project Name)', 'XXX-XX-XXX' or 'TBD'."""
    if text is None:
        return False
    s = str(text).strip()
    if not s or not _FILLER_CHARS.sub("", s) or _PLACEHOLDER.fullmatch(s):
        return False
    return bool(re.search(r"[A-Za-z0-9]", s))


def _is_bare_header_label(text) -> bool:
    m = _header_match(text)
    return bool(m and not _has_content(m.group("rest")))


def _is_column_word(text) -> bool:
    return isinstance(text, str) and text.strip().rstrip(":.").strip().lower() in _COLUMN_WORDS


def _is_field_label(text) -> bool:
    """A label by its wording alone ("Weather:", "Trade", "NAME OF
    CONTRACTOR") -- never by case, so a value typed in capitals ("J&J
    MECHANICAL") still reads as a value."""
    if not isinstance(text, str) or not _has_content(text) or len(text.strip()) > 40:
        return False
    return text.strip().endswith(":") or _is_column_word(text) or _is_bare_header_label(text)


def _looks_like_label(text) -> bool:
    """Field labels plus all-caps headings ("SKILLED WORKERS"), for spotting
    the cell a styled entry cell belongs to."""
    if _is_field_label(text):
        return True
    if not isinstance(text, str) or not _has_content(text) or len(text.strip()) > 40:
        return False
    s = text.strip()
    return s.isupper() and not re.search(r"\d", s)


def _existing_cells(ws, max_row: int = _SCAN_MAX_ROW, max_col: int = _SCAN_MAX_COL):
    """Cells the sheet actually stores, in reading order. Walking
    ws.iter_rows() instead creates every cell up to max_column -- seconds
    of work on a template with one stray formatted cell in column XFD."""
    cells = [c for (r, col), c in list(ws._cells.items()) if r <= max_row and col <= max_col]
    return sorted(cells, key=lambda c: (c.row, c.column))


def _box_of(ws, cell):
    """The merged box holding this cell, from a per-sheet index built once
    (a linear scan per write gets slow on forms with hundreds of boxes)."""
    index = getattr(ws, "_ice_box_index", None)
    if index is None:
        index = {}
        for rng in ws.merged_cells.ranges:
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    index[(r, c)] = rng
        ws._ice_box_index = index
    return index.get((cell.row, cell.column))


def _header_band_end(ws) -> int:
    """Last row of the form's header block: above the first row that reads
    like a table's column headings, never past row 10. Column headings are
    three or more column-style cells ("Trade | Men | Hours", "SKILLED
    WORKERS | UNSKILLED | ...") with no "Label:" fields among them -- a row of
    "Date: | Weather: | Temp:" fields, or of header items ("Project Name |
    Contract No. | Contractor"), is still the header."""
    last = min(ws.max_row or 0, _HEADER_BAND_MAX_ROW)
    by_row: dict[int, list] = {}
    for c in _existing_cells(ws, max_row=last):
        if _has_content(c.value):
            by_row.setdefault(c.row, []).append(c.value)
    for r in range(1, last + 1):
        values = by_row.get(r, [])
        if any(isinstance(v, str) and v.strip().endswith(":") for v in values):
            continue
        header_items = sum(1 for v in values if _is_bare_header_label(v))
        columns = sum(1 for v in values if _is_column_word(v) or (
            _looks_like_label(v) and not _is_bare_header_label(v)))
        if columns >= 3 and header_items < 2:
            return r - 1
    return last


def _protected_header_cells(ws) -> set[str]:
    """Cells holding the client's own header wording, which the export never
    overwrites. Deliberately narrow -- it is a backstop behind the prompt, and
    it must never block a per-day value: only a cell that is a header line
    and nothing else ("STATION -  VA Medical center, Bronx NY"), or the value
    beside a header label that stands alone in its row ("NAME OF CONTRACTOR"
    over "J&J Mechanical")."""
    protected: set[str] = set()
    band_end = _header_band_end(ws)
    if band_end < 1:
        return protected
    band = [c for c in _existing_cells(ws, max_row=band_end) if isinstance(c.value, str)]
    # Rows that read as a crew table's headings: a crew column ("Trade",
    # "Workers:", "No. of Men"), or a heading without a colon that is not
    # simply the value typed after a "Label:" ("DESCRIPTION OF WORK").
    table_rows: set[int] = set()
    prev: dict[int, str] = {}
    for c in band:
        v = c.value.strip()
        after_field = prev.get(c.row, "").endswith(":")
        prev[c.row] = v
        if not _has_content(v) or _is_bare_header_label(v):
            continue
        if v.rstrip(":.").strip().lower() in _CREW_COLUMNS:
            table_rows.add(c.row)
        elif not v.endswith(":") and not after_field and _looks_like_label(v)                 and not _is_column_word(v):
            table_rows.add(c.row)
    for cell in band:
        m = _header_match(cell.value)
        if not m:
            continue
        rest = m.group("rest")
        if _has_content(rest):
            if not _PER_REPORT_TAIL.search(rest.strip()):
                protected.add(cell.coordinate)
            continue
        # Bare label: lock its value -- but never in a crew table's heading
        # row ("Contractor | Trade | Workers", "DESCRIPTION OF WORK |
        # CONTRACTOR"), whose rows are the day's data.
        if cell.row in table_rows:
            continue
        box = _box_of(ws, cell)
        bottom = box.max_row if box else cell.row
        right = box.max_col if box else cell.column
        for r, c in ((cell.row, right + 1), (bottom + 1, cell.column)):
            if r > band_end or c > (ws.max_column or 0):
                continue
            v = ws.cell(row=r, column=c).value
            if not _has_content(v) or _is_field_label(v):
                continue
            if isinstance(v, str) and (len(v) > 80 or _PER_REPORT_TAIL.search(v.strip())):
                break  # reads like a per-day entry, not a name or number
            protected.add(ws.cell(row=r, column=c).coordinate)
            break
    return protected


def _percent_cells(ws, limit: int = 40) -> list[str]:
    """Blank cells formatted as a percentage ("Prec." on the VA form)."""
    out = []
    for cell in _existing_cells(ws):
        if type(cell).__name__ == "MergedCell" or _has_content(cell.value):
            continue
        if "%" in (cell.number_format or ""):
            out.append(cell.coordinate)
            if len(out) >= limit:
                break
    return out


# ---- Styled entry cells ----------------------------------------------
def _font_colour(cell) -> Optional[str]:
    """The cell's own font colour ("#00B050", "theme 4"), or None when it is
    the default text colour or white-ish (a styled band, not an entry)."""
    color = getattr(cell.font, "color", None)
    if color is None:
        return None
    kind = getattr(color, "type", None)
    if kind == "theme":
        theme = getattr(color, "theme", None)
        return None if theme in (None, 0, 1) else f"theme {theme}"
    if kind == "indexed":
        idx = getattr(color, "indexed", None)
        return None if idx in (None, 8, 9, 64, 65) else f"indexed {idx}"
    rgb = getattr(color, "rgb", None)
    if not isinstance(rgb, str) or len(rgb) < 6 or rgb.upper() in ("FF000000", "00000000"):
        return None
    try:
        channels = [int(rgb[-6:][i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return None
    return None if min(channels) >= 0xD0 else "#" + rgb[-6:]


def _styled_empty_cells(ws, limit: int = 40) -> list[dict]:
    """Blank cells the client pre-formatted with their own font colour --
    where their supervisor types the day's values (VA 6B: the date under
    "Date" in green bold). Without this the model only sees text cells
    and picks the nearest empty box instead. Cells right below or right of
    a label come first; white text and whole-row bands are skipped."""
    by_row: dict[int, list] = {}
    for cell in _existing_cells(ws):
        if type(cell).__name__ == "MergedCell" or _has_content(cell.value):
            continue
        shown = _font_colour(cell)
        if shown:
            by_row.setdefault(cell.row, []).append((cell, shown))
    near_label, other = [], []
    for r, cells in by_row.items():
        if len(cells) >= 6:
            continue  # a coloured band across the row
        for cell, shown in cells:
            entry = {"address": cell.coordinate, "font_color": shown}
            if cell.font.b:
                entry["bold"] = True
            if cell.number_format and cell.number_format != "General":
                entry["number_format"] = cell.number_format
            above = ws.cell(row=cell.row - 1, column=cell.column).value if cell.row > 1 else None
            left = ws.cell(row=cell.row, column=cell.column - 1).value if cell.column > 1 else None
            (near_label if _looks_like_label(above) or _looks_like_label(left) else other).append(entry)
    return (near_label + other)[:limit]


def _top_left(ws, cell):
    """A write aimed inside a box lands on the box's top-left cell -- unless
    that cell holds the template's own text (a label or table header the AI
    aimed one row too high at), which must survive. Returns None to skip."""
    box = _box_of(ws, cell)
    if not box:
        return cell
    top = ws.cell(row=box.min_row, column=box.min_col)
    if top.coordinate != cell.coordinate and _has_content(top.value):
        return None
    return top


_DATE_STR = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


_RAIN_LABEL = re.compile(r"(?<![a-z])(?:prec|precip|precipitation|rain|rainfall|snow)(?![a-z])", re.IGNORECASE)


def _labelled_as_rain(cell) -> bool:
    """Rain in inches is never a percentage, whatever the box's format."""
    ws = getattr(cell, "parent", None)
    if ws is None:
        return False
    for c in range(cell.column - 1, max(0, cell.column - 4), -1):
        v = ws.cell(row=cell.row, column=c).value
        if isinstance(v, str) and v.strip():
            return bool(_RAIN_LABEL.search(v))
    return False


def _typed_for_cell(cell, value):
    """Match the cell's own format: a date cell gets a real date (shows and
    aligns like the supervisor's own entry); a number aimed at a cell
    formatted as a percentage that isn't a fraction (precipitation 0 in a
    "0%" box) gets a plain number format instead of showing "0%"."""
    from openpyxl.styles.numbers import is_date_format
    fmt = cell.number_format or "General"
    if isinstance(value, str) and is_date_format(fmt):
        m = _DATE_STR.match(value)
        if m:
            try:
                return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "%" in fmt:
        if value > 1 or value < 0 or _labelled_as_rain(cell):
            cell.number_format = "General"
    return value


def _fit_rows(ws, written) -> None:
    """Grow row heights so wrapped text the export wrote is not cut off
    (the template's rows are sized for one short line). Estimates lines
    from the box width; only ever grows a row, never shrinks one."""
    import math
    default_w = ws.sheet_format.defaultColWidth or 8.43
    default_h = ws.sheet_format.defaultRowHeight or 15
    # Read widths without touching column_dimensions[...] -- indexing it
    # creates entries, which would be saved into the client's file. One
    # entry can cover several columns (min..max).
    widths: dict[int, Optional[float]] = {}
    hidden: set[int] = set()
    for dim in list(ws.column_dimensions.values()):
        lo, hi = dim.min or 0, dim.max or 0
        for c in range(lo, hi + 1):
            widths[c] = dim.width if dim.customWidth else None
            if dim.hidden:
                hidden.add(c)
    need_by_row: dict[int, float] = {}
    for cell in written:
        text = cell.value
        if not isinstance(text, str) or not text.strip():
            continue
        if not (cell.alignment and cell.alignment.wrap_text):
            continue
        box = _box_of(ws, cell)
        c0, c1 = (box.min_col, box.max_col) if box else (cell.column, cell.column)
        r0, r1 = (box.min_row, box.max_row) if box else (cell.row, cell.row)
        width = sum((widths.get(c) or default_w) for c in range(c0, c1 + 1) if c not in hidden)
        size = float(cell.font.sz or 11)
        # Column width counts '0' digits at 11pt; average text runs ~10% narrower.
        per_line = max(1, int(width * 11 / size * 1.1))
        lines = sum(max(1, math.ceil(len(part) * 1.08 / per_line)) for part in text.split("\n"))
        if lines <= 1:
            continue  # one line already fits the template's own row
        lines = min(lines, 12)
        needed = lines * size * 1.25
        heights = {r: (ws.row_dimensions[r].height if r in ws.row_dimensions else None)
                   for r in range(r0, r1 + 1)}
        have = sum((h or default_h) for h in heights.values())
        if needed > have:
            last = heights[r1] or default_h
            need_by_row[r1] = max(need_by_row.get(r1, 0), last + (needed - have))
    for r, h in need_by_row.items():
        ws.row_dimensions[r].height = round(h, 1)


# ---- Template snapshot -----------------------------------------------
def _safe_kept(ws) -> set:
    # A guard, not the export: an odd template must never fail the export here.
    try:
        return _protected_header_cells(ws)
    except Exception:
        return set()


def _hints(ws) -> dict:
    kept = _safe_kept(ws)
    out = {"kept_header_cells": sorted(kept)}
    for key, fn in (("styled_empty_cells", _styled_empty_cells), ("percent_cells", _percent_cells)):
        try:
            out[key] = fn(ws)
        except Exception:
            out[key] = []
    return out


def _extract_template_snapshot(wb) -> dict:
    """Walk every worksheet and capture every non-empty cell as
    {address, row, col, value, is_formula}. The AI uses this list to
    locate label cells and infer where the empty value cell sits."""
    sheets: dict[str, dict] = {}
    for ws in wb.worksheets:
        cells: list[dict] = []
        for cell in _existing_cells(ws, max_row=10 ** 6, max_col=10 ** 6):
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
            **_hints(ws),
        }
    return sheets


# ---- DR data snapshot ------------------------------------------------
def _whole(v):
    """Supervisors write whole degrees (47, not 46.4)."""
    try:
        return int(round(float(v))) if v is not None and v != "" else v
    except (TypeError, ValueError):
        return v


def _sentence_case(v):
    return v[:1].upper() + v[1:] if isinstance(v, str) and v else v


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
            "notes": p_row.get("notes") or None,
        }

    log = site_log or {}

    def _event(e: dict) -> dict:
        # A visit, meeting or call: "COR-William Anis onsite-view work in progress."
        # Voice intake stores with_party / description instead.
        return {
            "who": e.get("other_party_name") or e.get("with_party"),
            "organization": e.get("other_party_org"),
            "role": e.get("other_party_role"),
            "how": e.get("channel"),
            "summary": e.get("summary") or e.get("description"),
            "outcome": e.get("outcome_notes"),
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
            "conditions": _sentence_case(dr.get("weather_conditions")),
            "temp_high_f": _whole(dr.get("weather_temp_high_f")),
            "temp_low_f": _whole(dr.get("weather_temp_low_f")),
            "precipitation_in": dr.get("weather_precipitation_in"),
            "wind_speed_mph": dr.get("weather_wind_speed_mph"),
        },
        "activities": [_act(a) for a in (activities or [])],
        "crew_presence": [_pres(pr) for pr in (presence or [])],
        "delays": [_delay(d) for d in (delays or [])],
        "site_events": [_event(e) for e in log.get("site_events") or []],
        "people_on_site": [{"name": a.get("name"), "role": a.get("role"), "notes": a.get("notes")}
                           for a in log.get("people_on_site") or [] if a.get("name")],
        "deliveries": [{"vendor": d.get("vendor") or d.get("supplier_name"),
                        "description": d.get("description"),
                        "po_number": d.get("po_number")} for d in log.get("deliveries") or []],
        "safety_events": [{"type": e.get("event_type"), "severity": e.get("severity"),
                           "description": e.get("description")} for e in log.get("safety_events") or []],
        "quality_issues": [{"severity": q.get("severity"), "status": q.get("status"),
                            "description": q.get("description")} for q in log.get("quality_issues") or []],
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
   (visits, meetings, inspections), people on site, deliveries, safety events, quality
   issues, notes).

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
                  combined "Label: value" string for a value that changes with each
                  report (e.g. the template cell reads "SIGNATURE OF SUPERVISOR :
                  Camil Cherbaka" -- return the whole string with our signer, like
                  "SIGNATURE OF SUPERVISOR : " + our signer_name).

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
                    - every cell listed in kept_header_cells (see below)

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
     (These project header items -- contractor, contract number, client, station,
      project name -- are filled only where the template leaves them empty.)
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
     "Status" / "Report Status"
        -> our status

CELL-ADDRESS PATTERNS
---------------------
- LABEL adjacent to a separate VALUE cell: target the value cell (cell to the right
  of, or directly below, the label). LEAVE the label cell alone.
- "Label: value" in ONE cell for a per-report value (e.g. "SIGNATURE OF SUPERVISOR :
  Camil Cherbaka", "Weather: Rain"): REWRITE that single cell preserving the label
  prefix and substituting our value.
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
  number, contractor name, client) are the client's own wording for this project,
  even when our project data words them differently: LEAVE them exactly as they
  are. Each worksheet lists them in kept_header_cells; never target those cells.
  Fill a header item only where the template leaves it empty.
- A one-cell line holding a header item AND a per-report item ("Project No.:
  526-19-122      Date:") may be rewritten: keep the header part exactly as the
  template words it and fill only the per-report part
  ("Project No.: 526-19-122      Date: 11/03/2026").

STYLED ENTRY CELLS (styled_empty_cells)
---------------------------------------
Each worksheet also lists styled_empty_cells: blank cells the client pre-formatted
with their own font colour (and sometimes a date format). That is where the
client's supervisor types the day's values. When one sits directly below or right
of a label, that label's value goes THERE -- even if an empty box is also nearby
("Date" in A8 with a green, date-formatted A9 -> the date goes in A9, not in a box
to the right; "Day of Week:" in A10 with a green A11 -> A11).
- A table header box that spans several rows (e.g. "SKILLED WORKERS" in C12:E13)
  ends at its bottom row; the data rows start on the next row (14). Never write data
  into a header's rows.
- Identical boxes repeated on consecutive rows (C14:E14, C15:E15, ... C18:E18) are
  the table's data rows. Use them top-down, one record per row; the row labelled
  "TOTAL" after them is the totals row.
- Unlabeled full-width boxes below the main tables (e.g. A22:K22, A23:K23,
  A24:K24) are remarks lines: put our notes there, one sentence or item per line.

VA DAILY REPORT (headers SKILLED WORKERS / UNSKILLED / LOCATION AND DESCRIPTION OF WORK)
- One row per company on site (from crew_presence, else from activities).
- SKILLED WORKERS cell: "<company> - <N> men" (e.g. "J&J Mechanical - 6 men"; for
  one worker "Dua Electrical - 1 man"). When the crew row's notes name the workers,
  add them: "Dua Electric - 1 man - Jorge".
- UNSKILLED cell: that company's unskilled headcount when we have it, else leave it.
- LOCATION AND DESCRIPTION OF WORK cell (same row): what that company worked on --
  the activity description and its notes, in plain words, without activity codes.
- TOTAL row: total skilled headcount under SKILLED WORKERS; under UNSKILLED the
  unskilled total, or 0 when none of our crew rows are unskilled.
- Pair each company with the work it did (match by trade: an electrical company
  with the electrical activity). Activities no company matches get their own rows.
- Remarks lines (the full-width boxes below the tables), in this order: who visited
  or met on site (site_events, written like "COR-William Anis onsite-view work in
  progress."), then issues, delays and blockers logged that day (a title line, then
  its detail), then safety events and quality issues, then the day's notes. One item
  per line; when there are more items than lines, put two short items on a line.
- DELIVERY OF MATERIALS box: one line per delivery, "<vendor> - <description>". Leave
  it empty when there were no deliveries.
- The large unlabeled box beside the weather and date block (K6 on this form): ONE
  short line naming the contractor's and subcontractors' supervisors on site, from
  people_on_site or our notes, e.g. "Joe Lachina from J&J & Efriam Fakhry onsite".
  Visitors, the COR and inspectors go in the remarks lines, not here; never repeat
  the same people in both. Leave it empty when we have no such names.
- Prec.: leave it blank on a dry day (precipitation 0); fill it only when it rained.

PERCENT-FORMATTED CELLS (percent_cells)
- Each worksheet lists percent_cells: blank cells the client formatted as a
  percentage. Excel shows a number there as a percent (0.25 -> "25%"). For a value
  that is not a percentage (rain in inches), write text with its unit: "0.25 in".
  For a real percentage write the fraction: 0.55 for 55%.

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
    # even if the AI mistakenly targets one. Formatting / fonts /
    # borders / merges / column widths / images are untouched (we
    # only mutate cell.value).
    sheet_index = {ws.title: ws for ws in wb.worksheets}
    for sheet_name, cells in mapping.items():
        ws = sheet_index.get(sheet_name)
        if ws is None or not isinstance(cells, dict):
            continue
        kept = _safe_kept(ws)
        written = []
        for addr, value in cells.items():
            try:
                cell = _top_left(ws, ws[addr])
            except Exception:
                continue
            if cell is None or not _is_writable(cell):
                continue
            if cell.coordinate in kept:
                continue  # the client's own header wording stays
            if cell.coordinate != addr and cell.coordinate in cells:
                continue  # the AI also wrote the box itself; that one wins
            try:
                # None is a legitimate blank intent
                cell.value = _typed_for_cell(cell, _cell_safe(value))
            except (AttributeError, TypeError, ValueError):
                # Read-only cell subclasses reject .value -- skip, don't abort.
                continue
            written.append(cell)
        try:
            _fit_rows(ws, written)
        except Exception:
            pass  # row sizing is cosmetic; never fail the export over it

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
