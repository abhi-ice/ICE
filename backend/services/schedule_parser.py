"""Schedule file parser.

SUBSTITUTION: master prompt §20 specified MPXJ via JPype for .mpp/.xer.
Emergent runtime doesn't ship Java/JPype, so we support .xlsx and .csv only.
.mpp/.xer return a clear "use Railway parser" error. The activity_code,
duration units, and predecessor handling logic is preserved per §20.2-20.5.
"""
import csv
import io
import re
from datetime import datetime, date
from typing import List, Tuple
from openpyxl import load_workbook


def _parse_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                pass
    return None


def _parse_duration_days(v) -> float | None:
    """Per master prompt §20.3: MS Project duration is in WORK days, not
    calendar days. Handle '2 days', '8 hrs', '1 wk', plain numbers."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    m = re.match(r"^([\d.]+)\s*(days?|d|hrs?|hours?|h|wks?|weeks?|w|mins?|minutes?|m)?$", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    val = float(m.group(1))
    unit = (m.group(2) or "days").lower()
    if unit.startswith("h") and not unit.startswith("hours"):
        return val / 8.0
    if unit.startswith("hour") or unit == "hrs" or unit == "hr":
        return val / 8.0
    if unit.startswith("w"):
        return val * 5.0
    if unit.startswith("min"):
        return val / 480.0
    return val  # days


def _parse_predecessors(v) -> List[str]:
    if v is None or v == "":
        return []
    s = str(v)
    # Split on commas / semicolons; strip "FS+0", "SS-1" suffixes — keep the bare ID.
    parts = re.split(r"[;,]", s)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Take leading alphanumeric ID (before any FS/SS/FF/SF lag suffix)
        m = re.match(r"^([A-Za-z0-9._\-]+)", p)
        if m:
            out.append(m.group(1))
    return out


# Canonical column aliases — case-insensitive match
COL_ALIASES = {
    "activity_code": ["id", "activity id", "activity_code", "task id", "ref", "code", "uniqueid"],
    "description": ["name", "description", "task name", "activity", "task description"],
    "wbs": ["wbs", "wbs code"],
    "planned_start": ["start", "planned start", "scheduled start", "early start"],
    "planned_finish": ["finish", "planned finish", "end", "scheduled finish", "early finish"],
    "duration_days": ["duration", "duration_days", "dur"],
    "predecessors": ["predecessors", "preds", "depends on"],
    "trade_code": ["trade", "trade_code", "discipline"],
    "is_milestone": ["milestone", "is milestone"],
}


def _map_columns(header: list[str]) -> dict[str, int]:
    """Return mapping canonical_key -> column index."""
    norm = [str(h).strip().lower() if h is not None else "" for h in header]
    out: dict[str, int] = {}
    for canonical, aliases in COL_ALIASES.items():
        for a in aliases:
            if a in norm:
                out[canonical] = norm.index(a)
                break
    return out


def parse_xlsx(content: bytes) -> Tuple[List[dict], List[str]]:
    """Returns (parsed_rows, errors). Each row is a dict with keys matching
    schedule_proposal.parsed_to_task_create_payload's expected input."""
    errors: list[str] = []
    rows: list[dict] = []
    wb = load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active
    iterator = ws.iter_rows(values_only=True)
    try:
        header = next(iterator)
    except StopIteration:
        return [], ["Empty workbook"]
    cols = _map_columns(list(header))
    if "activity_code" not in cols or "description" not in cols:
        return [], [f"Missing required columns. Found header: {header}"]

    for line_no, row in enumerate(iterator, start=2):
        if row is None or all(c is None or c == "" for c in row):
            continue
        try:
            ac = row[cols["activity_code"]]
            desc = row[cols["description"]]
            if ac is None or desc is None:
                continue
            parsed = {
                "activity_code": str(ac).strip(),
                "description": str(desc).strip(),
                "wbs": str(row[cols["wbs"]]).strip() if "wbs" in cols and row[cols["wbs"]] else None,
                "planned_start": _parse_date(row[cols["planned_start"]]) if "planned_start" in cols else None,
                "planned_finish": _parse_date(row[cols["planned_finish"]]) if "planned_finish" in cols else None,
                "duration_days": _parse_duration_days(row[cols["duration_days"]]) if "duration_days" in cols else None,
                "predecessors": _parse_predecessors(row[cols["predecessors"]]) if "predecessors" in cols else [],
                "trade_code": str(row[cols["trade_code"]]).strip() if "trade_code" in cols and row[cols["trade_code"]] else None,
                "is_milestone": bool(row[cols["is_milestone"]]) if "is_milestone" in cols else False,
            }
            rows.append(parsed)
        except Exception as e:
            errors.append(f"Row {line_no}: {e}")
    return rows, errors


def parse_csv(content: bytes) -> Tuple[List[dict], List[str]]:
    errors: list[str] = []
    rows: list[dict] = []
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], ["Empty CSV"]
    cols = _map_columns(header)
    if "activity_code" not in cols or "description" not in cols:
        return [], [f"Missing required columns. Header: {header}"]

    for line_no, row in enumerate(reader, start=2):
        if not row or all((c or "") == "" for c in row):
            continue
        try:
            parsed = {
                "activity_code": str(row[cols["activity_code"]]).strip(),
                "description": str(row[cols["description"]]).strip(),
                "wbs": row[cols["wbs"]].strip() if "wbs" in cols and len(row) > cols["wbs"] and row[cols["wbs"]] else None,
                "planned_start": _parse_date(row[cols["planned_start"]]) if "planned_start" in cols and len(row) > cols["planned_start"] else None,
                "planned_finish": _parse_date(row[cols["planned_finish"]]) if "planned_finish" in cols and len(row) > cols["planned_finish"] else None,
                "duration_days": _parse_duration_days(row[cols["duration_days"]]) if "duration_days" in cols and len(row) > cols["duration_days"] else None,
                "predecessors": _parse_predecessors(row[cols["predecessors"]]) if "predecessors" in cols and len(row) > cols["predecessors"] else [],
                "trade_code": row[cols["trade_code"]].strip() if "trade_code" in cols and len(row) > cols["trade_code"] and row[cols["trade_code"]] else None,
                "is_milestone": str(row[cols["is_milestone"]]).strip().lower() in ("1", "true", "yes") if "is_milestone" in cols and len(row) > cols["is_milestone"] else False,
            }
            if not parsed["activity_code"] or not parsed["description"]:
                continue
            rows.append(parsed)
        except Exception as e:
            errors.append(f"Row {line_no}: {e}")
    return rows, errors


def parse_schedule_file(filename: str, content: bytes) -> Tuple[List[dict], List[str]]:
    fn = filename.lower()
    if fn.endswith(".xlsx"):
        return parse_xlsx(content)
    if fn.endswith(".csv"):
        return parse_csv(content)
    if fn.endswith(".mpp") or fn.endswith(".xer") or fn.endswith(".xml"):
        # SUBSTITUTION: Java MPXJ bridge not available on Emergent.
        return [], [
            f"{fn.rsplit('.',1)[1].upper()} parsing requires the MPXJ/JPype bridge which is not "
            "available on this Emergent runtime. Please re-export the schedule as XLSX or CSV, "
            "or use the Railway deployment which has the full parser."
        ]
    return [], [f"Unsupported schedule file type: {filename}"]
