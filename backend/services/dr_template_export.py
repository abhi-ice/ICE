"""Feature Client DR Template Export (2026-05-20) -- inject Daily
Report + project data into a project's uploaded xlsx template via
openpyxl. Returns the filled workbook as in-memory bytes.

Three passes:
  1. Token replacement -- cells containing the documented {{tokens}}
     are replaced with the corresponding value.
  2. Heuristic label fill -- if no token matched a particular cell,
     cells whose value looks like a known label (e.g. "Client",
     "Station", "Contract Number") have their IMMEDIATE RIGHT
     neighbor populated, but only if that neighbor is currently
     empty and not a formula cell.
  3. Activities table -- the first row with >=2 adjacent non-empty
     string cells is treated as the activity-table header. Up to
     20 activity rows are written below it (trade / worker count /
     description). If a {{activities}} token was present in pass 1,
     that anchor wins over the heuristic header detection.

Strict guards:
  - Formula cells (data_type == 'f') are NEVER overwritten.
  - Cell formatting / font / border / fill / merged cells / column
    widths are left untouched (we only set cell.value).
  - Workbook is returned as in-memory bytes -- nothing is written to
    disk.
  - PDF templates are out of scope -- the caller (the export router)
    returns 400 BEFORE invoking this engine.
"""
from __future__ import annotations
from datetime import datetime, date as date_cls
from io import BytesIO


# Heuristic label -> field-key map. Case-insensitive substring match;
# trailing punctuation (":", "*") and whitespace stripped before matching.
_LABEL_MAP = [
    ("client", "client_name"),
    ("station", "station"),
    ("project title", "project_title"),
    ("project name", "project_title"),
    ("contract number", "contract_number"),
    ("contract no", "contract_number"),
    ("contract #", "contract_number"),
    ("contractor", "contractor_name"),
    ("date", "date"),
    ("day of week", "day_of_week"),
    ("day", "day_of_week"),
    ("weather", "weather"),
    ("conditions", "weather"),
    ("high temp", "temp_high"),
    ("temp high", "temp_high"),
    ("low temp", "temp_low"),
    ("temp low", "temp_low"),
    ("precip", "precipitation"),
    ("signature", "signature"),
    ("signed by", "signature"),
    ("prepared by", "signature"),
    ("notes", "notes"),
    ("remarks", "notes"),
]


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


def _build_value_map(*, dr: dict, project: dict, signer_name: str) -> dict:
    """One source of truth for both token replacement (pass 1) and
    heuristic label fill (pass 2). Keys match the documented token
    names (sans {{ }})."""
    p = project or {}
    return {
        "client_name": p.get("client_name") or "",
        "station": p.get("station_id") or "",
        "project_title": p.get("name") or "",
        "contract_number": p.get("contract_number") or "",
        "contractor_name": p.get("contractor_name") or "",
        "date": _format_date_mmddyyyy(dr.get("report_date")),
        "day_of_week": _day_of_week(dr.get("report_date")),
        "weather": dr.get("weather_conditions") or "",
        "temp_high": dr.get("weather_temp_high_f") or "",
        "temp_low": dr.get("weather_temp_low_f") or "",
        "precipitation": dr.get("weather_precipitation_in") or "",
        "signature": signer_name or "",
        "notes": dr.get("notes") or "",
    }


def _is_writable(cell) -> bool:
    """Never overwrite a formula cell."""
    return getattr(cell, "data_type", None) != "f"


def _label_lookup(text: str) -> str | None:
    """Returns the value-map key a label cell points at, or None."""
    s = (text or "").strip().lower().rstrip(":").rstrip("*").strip()
    if not s:
        return None
    for label, field in _LABEL_MAP:
        if label in s:
            return field
    return None


def inject_dr_into_template(
    template_bytes: bytes,
    *,
    dr: dict,
    project: dict,
    activities: list[dict],
    signer_name: str,
) -> bytes:
    """Fill `template_bytes` (an .xlsx) with DR + project data and
    return the filled workbook as bytes. Best-effort -- cells that
    can't be mapped are left as-is; the export never errors on a
    template-content mismatch."""
    from openpyxl import load_workbook

    values = _build_value_map(dr=dr, project=project, signer_name=signer_name)
    wb = load_workbook(BytesIO(template_bytes))

    for ws in wb.worksheets:
        # ---- Pass 1: token replacement ------------------------------
        # We also record the cell that held {{activities}} (if any)
        # so Pass 3 can target it as the activity-table anchor.
        activities_anchor: tuple[int, int] | None = None
        for row in ws.iter_rows():
            for cell in row:
                if not _is_writable(cell):
                    continue
                v = cell.value
                if not isinstance(v, str) or "{{" not in v:
                    continue
                if "{{activities}}" in v:
                    activities_anchor = (cell.row, cell.column)
                    v = v.replace("{{activities}}", "").strip()
                    cell.value = v if v else None
                    if not v:
                        continue
                new_v = v
                for key, val in values.items():
                    new_v = new_v.replace("{{" + key + "}}", str(val))
                if new_v != cell.value:
                    cell.value = new_v

        # ---- Pass 2: heuristic label fill ---------------------------
        # For each cell that LOOKS like a label, fill the cell to its
        # immediate right -- only if empty + writable. Runs after
        # token replacement so a label cell whose template already had
        # a token next to it isn't double-filled.
        max_col = ws.max_column or 1
        for row in ws.iter_rows():
            for cell in row:
                if cell.column >= max_col:
                    continue
                if not isinstance(cell.value, str):
                    continue
                field = _label_lookup(cell.value)
                if not field:
                    continue
                neighbor = ws.cell(row=cell.row, column=cell.column + 1)
                if not _is_writable(neighbor):
                    continue
                if neighbor.value not in (None, ""):
                    continue
                v = values.get(field, "")
                if v not in (None, ""):
                    neighbor.value = v

        # ---- Pass 3: activities table ------------------------------
        anchor_row: int | None = None
        anchor_col: int | None = None
        if activities_anchor:
            anchor_row, anchor_col = activities_anchor
        else:
            for row in ws.iter_rows():
                cells = list(row)
                for i in range(len(cells) - 1):
                    a, b = cells[i], cells[i + 1]
                    if (isinstance(a.value, str) and a.value
                            and isinstance(b.value, str) and b.value):
                        anchor_row = a.row + 1   # row BELOW header
                        anchor_col = a.column
                        break
                if anchor_row:
                    break

        if anchor_row and anchor_col and activities:
            for i, act in enumerate(activities[:20]):
                vals = [
                    act.get("trade_code") or "",
                    act.get("crew_count") or "",
                    act.get("activity_description") or "",
                ]
                for col_offset, v in enumerate(vals):
                    target = ws.cell(
                        row=anchor_row + i, column=anchor_col + col_offset,
                    )
                    if not _is_writable(target):
                        continue
                    # Don't overwrite a cell that already has content
                    # (e.g. an example / legend row from the template).
                    if target.value not in (None, ""):
                        continue
                    target.value = v

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
