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
        }
    return sheets


# ---- DR data snapshot ------------------------------------------------
def _build_dr_snapshot(
    *,
    dr: dict,
    project: dict,
    activities: list[dict],
    presence: list[dict],
    delays: list[dict],
    signer_name: str,
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
   list of work activities, crew presence rows, delays, notes).

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
        signer_name=signer_name,
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
        for addr, value in cells.items():
            try:
                cell = ws[addr]
            except Exception:
                continue
            if not _is_writable(cell):
                continue
            try:
                cell.value = value  # None is a legitimate blank intent
            except (AttributeError, TypeError):
                # Merged-cell secondaries / read-only cell subclasses
                # reject .value assignment -- skip without aborting.
                continue

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
