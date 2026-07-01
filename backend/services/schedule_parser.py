"""Schedule file parser.

SUBSTITUTION: master prompt §20 specified MPXJ via JPype for .mpp/.xer.
Emergent runtime doesn't ship Java/JPype, so we support .xlsx and .csv only.
.mpp/.xer return a clear "use Railway parser" error. The activity_code,
duration units, and predecessor handling logic is preserved per §20.2-20.5.

CPM Part B (2026-05-15) -- predecessor extraction now captures
relationship type (FS|SS|FF|SF) and integer-day lag. Output shape
is the post-Part-A canonical dict {code, type, lag_days} for every
parser branch (XLSX, CSV, MPXJ).

  Bare "1.5"           -> {"code": "1.5", "type": "FS", "lag_days": 0}
  "1.5FS+0"            -> {"code": "1.5", "type": "FS", "lag_days": 0}
  "1.5FS+7d"           -> {"code": "1.5", "type": "FS", "lag_days": 7}
  "1.5SS"              -> {"code": "1.5", "type": "SS", "lag_days": 0}
  "1.5FF+5d"           -> {"code": "1.5", "type": "FF", "lag_days": 5}
  "1.5FS-3"            -> {"code": "1.5", "type": "FS", "lag_days": -3}
  "1.5FS+8h"           -> {"code": "1.5", "type": "FS", "lag_days": 1}  (8 working hours -> 1d)
  "1.5SS+2w"           -> {"code": "1.5", "type": "SS", "lag_days": 14} (2 calendar weeks)

Lag-unit note (corrected 2026-06-29): the parser stores the lag MAGNITUDE
that the source expressed (e.g. "+2w" -> 14). BOTH downstream CPM engines --
the impact-days engine (services/cpm.py) and the enterprise engine
(services/enterprise_cpm.py) -- consume Predecessor.lag_days as WORKING days
via working_days.add_working_days. So the stored integer is treated as working
days end-to-end, and the two engines never disagree on lag. The original
"calendar days" wording above the examples is therefore the SOURCE magnitude,
not the consumption unit. True calendar-day -> working-day lag canonicalization
("Diagram 2" backlog item B1) is a deliberate, separable follow-up; it is
DEFERRED so it cannot disturb Mode-1's existing stored lags.
"""
import csv
import io
import math
import re
from datetime import datetime, date
from typing import List, Optional, Tuple
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


# CPM Part B (2026-05-15) -- predecessor text-form parser.
#
# Parsing strategy (two-pass):
#
#   Pass 1: search for an FS/SS/FF/SF type indicator followed by either
#           a signed lag (`+N` / `-N` / `Nd` / `Nw` / `Nh`) OR end-of-
#           entry. If found, split there: everything to the LEFT is the
#           code, the type group is the relationship, everything to the
#           RIGHT is the lag-with-unit. Trailing whitespace + delimiters
#           tolerated. Case-insensitive on the type letters.
#
#   Pass 2: if no type indicator found, the whole entry (after trim) is
#           the code with FS+0 default. This is the MSU case: a bare
#           "1.5" comes through cleanly without the regex engine
#           mistakenly slicing it into code="1." + lag="5".
#
# Why the two-pass split rather than one greedy regex: codes can
# contain letters AND end in 'F' or 'S' coincidentally (e.g. "FS-Item-1"
# is a perfectly valid activity_code). A single greedy regex that
# extracts a code-then-type doesn't disambiguate "code ends in F" from
# "code+FS suffix". The two-pass approach uses the lag-portion lookahead
# as the disambiguator: a real FS/SS/FF/SF type suffix is ALWAYS
# followed by a lag pattern or end-of-string, never by additional code
# characters.
#
# This means: code "T100" without any suffix correctly parses to
# {code: "T100", type: "FS", lag_days: 0} -- not the broken
# {code: "T", type: "FS", lag_days: 100} we'd get from a naive
# non-greedy or one-pass regex (caught in Part B round-trip pass 1).

_TYPE_WITH_LAG_RE = re.compile(
    r"([Ff][Ss]|[Ss][Ss]|[Ff][Ff]|[Ss][Ff])"           # the type letters
    r"(?=\s*(?:[+-]?\s*\d|$))"                          # MUST be followed by a sign+digit OR end
)

_LAG_TAIL_RE = re.compile(
    r"^"
    r"\s*"
    r"([+-]?\s*\d+(?:\.\d+)?)?"                         # group 1: optional signed lag value
    r"\s*"
    r"(d|day|days|h|hr|hrs|hour|hours|w|wk|wks|week|weeks|m|min|mins|minute|minutes)?"
    r"\s*$"
)

_CODE_ONLY_RE = re.compile(r"^([A-Za-z0-9._\-]+)\s*$")


def _text_lag_to_calendar_days(raw_value: str, unit: Optional[str]) -> int:
    """Convert a parsed-text lag (e.g. '7', '-3.5', '+8h', '2w') to
    integer CALENDAR days, sign preserved, half-up rounded.

    Unit handling (per build spec: ALL lag stored as calendar days):
      d / day / days        -> 1:1 (treat as calendar days)
      w / wk / week / weeks -> *7  (wall-clock calendar weeks)
      h / hr / hour / hours -> /8  (working-hours convention; 1 working
                                    day = 1 calendar day for lag purposes)
      m / min / minutes     -> /480 (1 working day = 480 minutes)
      (unit omitted)        -> days (matches MS Project convention)

    Sign preserved via signed rounding. Half-up: 0.5 -> 1, -0.5 -> -1.
    """
    s = (raw_value or "").replace(" ", "").strip()
    if not s:
        return 0
    try:
        val = float(s)
    except ValueError:
        return 0
    u = (unit or "d").lower()
    if u.startswith("d"):
        days = val
    elif u.startswith("w"):
        days = val * 7.0
    elif u.startswith("h"):
        # Working-hours convention: 8 hrs = 1 day. Construction lag in
        # hours is rare; usually an MPXJ export remnant. Approximating
        # as calendar days via the working-hours factor is the most
        # defensible default.
        days = val / 8.0
    elif u.startswith("m"):
        days = val / 480.0
    else:
        days = val
    # Half-up signed rounding. math.floor + 0.5 works on both signs.
    if days >= 0:
        return int(math.floor(days + 0.5))
    return -int(math.floor(abs(days) + 0.5))


def _parse_one_predecessor(p: str) -> Optional[dict]:
    """Parse one predecessor entry text (single token, no commas).
    Returns the canonical dict {code, type, lag_days} or None.

    Two-pass strategy (see _TYPE_WITH_LAG_RE docstring above):
      1. Look for an FS/SS/FF/SF type indicator followed by a lag
         pattern (or end-of-entry). If found, split there.
      2. If no type indicator found, treat the whole entry as the
         code with FS+0 default. This is the MSU case -- bare "1.5"
         parses to {code: "1.5", type: "FS", lag_days: 0} without
         the regex mistakenly chewing into the code.
    """
    p = (p or "").strip()
    if not p:
        return None

    # Pass 1: find a type indicator with lag-tail or end-of-entry.
    # search() (not match()) so we find the SUFFIX position regardless
    # of where it falls inside the entry. The lookahead in
    # _TYPE_WITH_LAG_RE requires the matched type to be followed by
    # a sign-digit or end-of-string -- this is what distinguishes a
    # real suffix from a "code ends in F" coincidence.
    m = _TYPE_WITH_LAG_RE.search(p)
    if m:
        code = p[:m.start()].rstrip()
        rtype = m.group(1).upper()
        tail = p[m.end():]
        lag_days = 0
        tm = _LAG_TAIL_RE.match(tail)
        if tm:
            lag_raw = tm.group(1)
            unit = tm.group(2)
            if lag_raw:
                lag_days = _text_lag_to_calendar_days(lag_raw, unit)
        if code:
            return {"code": code, "type": rtype, "lag_days": lag_days}
        # Edge case: the entry was "FS+5" with NO code -- malformed.
        # Drop it rather than fabricate a code.
        return None

    # Pass 2: no FS/SS/FF/SF suffix found. The whole entry is the
    # code, defaulting to FS+0 (MSU's canonical bare-code case).
    cm = _CODE_ONLY_RE.match(p)
    if not cm:
        return None
    return {"code": cm.group(1), "type": "FS", "lag_days": 0}


def _parse_predecessors(v) -> List[dict]:
    """Parse a comma/semicolon-separated predecessor cell from XLSX/CSV.

    CPM Part B (2026-05-15): returns List[dict] in canonical
    post-Part-A shape. Pre-Part-B this returned List[str] and silently
    dropped type+lag suffixes -- federal-defensibility problem on any
    schedule with non-FS dependencies. Now type+lag are extracted
    and round-tripped through the cascade engine.

    Bare codes like "1.5" continue to default to FS+0, preserving
    behavior for MSU's pure-FS+0 import.
    """
    if v is None or v == "":
        return []
    s = str(v)
    parts = re.split(r"[;,]", s)
    out: List[dict] = []
    for p in parts:
        entry = _parse_one_predecessor(p)
        if entry is not None:
            out.append(entry)
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
    # pt.76 (2026-05-29) -- progress-aware import: recognize execution-state
    # columns so an already-in-progress project migrates with its real % /
    # actuals / status instead of being zeroed to a fresh baseline. All four
    # are optional; a baseline-only schedule (no such columns) is unaffected.
    "pct_complete": ["% complete", "pct complete", "pct_complete", "percent complete",
                     "percent_complete", "% comp", "complete %", "physical % complete"],
    "actual_start": ["actual start", "actual_start", "act start", "actual start date"],
    "actual_finish": ["actual finish", "actual_finish", "act finish", "actual end",
                      "actual finish date"],
    "status": ["status", "task_status", "task status", "activity status", "state"],
    # Enterprise CPM ("Diagram 2", 2026-06-29) -- optional scheduling-input
    # columns. A schedule WITHOUT these columns parses exactly as before
    # (keys stay absent -> TaskCreate defaults them to None); they are read
    # only by the enterprise engine for schedule_engine="enterprise" projects.
    "constraint_type": ["constraint type", "constraint_type", "constraint"],
    "constraint_date": ["constraint date", "constraint_date", "constraint dt"],
    "calendar_id": ["calendar", "calendar id", "calendar_id", "calendar name"],
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


# pt.76 -- execution-state cell parsers for progress-aware import.
def _parse_pct(v):
    """Percent-complete from a schedule cell. Accepts 0-100 numbers or strings
    like '75', '75%', '75.0'. Returns a float clamped to [0,100], or None when
    empty/unparseable. (P6 / MS Project export percent-complete as 0-100.)"""
    if v is None:
        return None
    s = str(v).strip().rstrip("%").strip()
    if not s:
        return None
    try:
        f = float(s)
    except Exception:
        return None
    return max(0.0, min(100.0, f))


_STATUS_ALIASES = {
    "not_started": ["not started", "not_started", "notstarted", "new", "planned", "to do", "todo"],
    "in_progress": ["in progress", "in_progress", "inprogress", "started", "active", "wip", "ongoing"],
    "blocked": ["blocked", "on hold", "on-hold", "hold", "stopped", "suspended", "halted"],
    "complete": ["complete", "completed", "done", "finished", "closed", "100%"],
}


def _parse_status(v):
    """Normalize a source status string to one of
    not_started|in_progress|blocked|complete, or None when empty/unrecognized.
    Never returns 'deleted' (import cannot delete tasks)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    for canon, aliases in _STATUS_ALIASES.items():
        if s in aliases:
            return canon
    return None


# Enterprise CPM ("Diagram 2", 2026-06-29) -- schedule-constraint cell parser.
# Maps the spreadsheet / MS-Project constraint label to one of the 9 canonical
# models.ScheduleConstraintType values. Returns None when empty / unrecognized
# so an un-constrained row (the overwhelming majority) stays None and the
# enterprise engine treats it as the implicit "as soon as possible".
_CONSTRAINT_TYPE_ALIASES = {
    "mso": ["must start on", "mso", "must_start_on", "must-start-on"],
    "mfo": ["must finish on", "mfo", "must_finish_on", "must-finish-on"],
    "start_on": ["start on", "start_on", "start-on"],
    "snet": ["start no earlier than", "snet", "start_no_earlier_than",
             "start-no-earlier-than"],
    "snlt": ["start no later than", "snlt", "start_no_later_than",
             "start-no-later-than"],
    "fnet": ["finish no earlier than", "fnet", "finish_no_earlier_than",
             "finish-no-earlier-than"],
    "fnlt": ["finish no later than", "fnlt", "finish_no_later_than",
             "finish-no-later-than", "deadline"],
    "asap": ["as soon as possible", "asap"],
    "alap": ["as late as possible", "alap"],
}


def _parse_constraint_type(v):
    """Normalize a source constraint label to a ScheduleConstraintType, or None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    for canon, aliases in _CONSTRAINT_TYPE_ALIASES.items():
        if s in aliases:
            return canon
    return None


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
                "pct_complete": _parse_pct(row[cols["pct_complete"]]) if "pct_complete" in cols else None,
                "actual_start": _parse_date(row[cols["actual_start"]]) if "actual_start" in cols else None,
                "actual_finish": _parse_date(row[cols["actual_finish"]]) if "actual_finish" in cols else None,
                "status": _parse_status(row[cols["status"]]) if "status" in cols else None,
                # Enterprise CPM ("Diagram 2") -- optional scheduling inputs.
                "constraint_type": _parse_constraint_type(row[cols["constraint_type"]]) if "constraint_type" in cols else None,
                "constraint_date": _parse_date(row[cols["constraint_date"]]) if "constraint_date" in cols else None,
                "calendar_id": (str(row[cols["calendar_id"]]).strip() or None) if "calendar_id" in cols and row[cols["calendar_id"]] else None,
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
                "pct_complete": _parse_pct(row[cols["pct_complete"]]) if "pct_complete" in cols and len(row) > cols["pct_complete"] else None,
                "actual_start": _parse_date(row[cols["actual_start"]]) if "actual_start" in cols and len(row) > cols["actual_start"] else None,
                "actual_finish": _parse_date(row[cols["actual_finish"]]) if "actual_finish" in cols and len(row) > cols["actual_finish"] else None,
                "status": _parse_status(row[cols["status"]]) if "status" in cols and len(row) > cols["status"] else None,
                # Enterprise CPM ("Diagram 2") -- optional scheduling inputs.
                "constraint_type": _parse_constraint_type(row[cols["constraint_type"]]) if "constraint_type" in cols and len(row) > cols["constraint_type"] else None,
                "constraint_date": _parse_date(row[cols["constraint_date"]]) if "constraint_date" in cols and len(row) > cols["constraint_date"] else None,
                "calendar_id": (str(row[cols["calendar_id"]]).strip() or None) if "calendar_id" in cols and len(row) > cols["calendar_id"] and row[cols["calendar_id"]] else None,
            }
            if not parsed["activity_code"] or not parsed["description"]:
                continue
            rows.append(parsed)
        except Exception as e:
            errors.append(f"Row {line_no}: {e}")
    return rows, errors


# CPM Part B (2026-05-15) -- MPXJ enum + Duration adapter helpers.
#
# These run inside parse_mpp_xer where the JVM is started and MPXJ
# classes are loaded. They take a Java object (the MPXJ enum value
# or Duration) and produce our Python representation.
#
# Implementation choice: pattern-match against the enum's name() /
# toString() string output instead of importing the Java constants
# directly. JClass("org.mpxj.RelationType.FINISH_START") would fail
# loudly if the MPXJ version on the runtime spells the enum slightly
# differently (some versions use "Finish-Start" instead of
# "FINISH_START"). String-matching tolerates both.


def _mpxj_relation_type_to_code(rt) -> str:
    """Map an MPXJ RelationType enum value to our 2-letter code.

    Recognized name patterns (case-insensitive after normalization):
      FINISH_START, FINISH-START, FS  -> "FS"
      START_START,  START-START,  SS  -> "SS"
      FINISH_FINISH, FINISH-FINISH, FF -> "FF"
      START_FINISH, START-FINISH, SF  -> "SF"

    Default on unknown / None / exception: "FS" (matches the implicit
    default in every construction schedule; safe fallback). This is
    consistent with how a bare-string predecessor "1.5" coerces to
    FS+0 via models._normalize_predecessors_list.
    """
    if rt is None:
        return "FS"
    try:
        # Java enum's name() returns the canonical declared name (e.g.
        # "FINISH_START"). Some MPXJ versions also expose .toString()
        # returning "Finish-Start". Try both, prefer name() when
        # present.
        if hasattr(rt, "name"):
            name = str(rt.name())
        else:
            name = str(rt)
    except Exception:
        name = str(rt) if rt is not None else ""
    norm = name.upper().replace("-", "_").replace(" ", "_").strip()
    if norm in ("FINISH_START", "FS"):
        return "FS"
    if norm in ("START_START", "SS"):
        return "SS"
    if norm in ("FINISH_FINISH", "FF"):
        return "FF"
    if norm in ("START_FINISH", "SF"):
        return "SF"
    return "FS"


def _mpxj_lag_to_calendar_days(lag) -> int:
    """Convert an MPXJ Duration to integer CALENDAR days, signed,
    half-up rounded.

    MPXJ Duration exposes:
      .getDuration() -> double (the numeric value, sign-preserved
                                for leads)
      .getUnits()    -> TimeUnit enum (DAYS, ELAPSED_DAYS, HOURS,
                                       ELAPSED_HOURS, WEEKS,
                                       ELAPSED_WEEKS, MINUTES,
                                       ELAPSED_MINUTES, etc.)

    Conversion (per build spec: all lag stored as calendar days):
      DAYS / ELAPSED_DAYS              -> 1:1
      WEEKS / ELAPSED_WEEKS            -> *7 (wall-clock weeks)
      HOURS (working)                  -> /8  (8h working day)
      ELAPSED_HOURS                    -> /24 (24h calendar day)
      MINUTES (working)                -> /480
      ELAPSED_MINUTES                  -> /1440
      MONTHS / ELAPSED_MONTHS          -> *30 (defensive; rare in
                                                schedule files)
      unknown                          -> treat as days

    Half-up signed rounding so 0.5 -> 1 and -0.5 -> -1. Sign
    preservation is critical: negative duration is a LEAD (overlap),
    must round toward negative infinity correctly to keep the
    intended early-start semantics.

    Returns 0 on any extraction failure (safe default; downstream
    cascade treats as no lag).
    """
    if lag is None:
        return 0
    try:
        val = float(lag.getDuration())
    except Exception:
        return 0
    units_obj = None
    try:
        units_obj = lag.getUnits()
    except Exception:
        units_obj = None
    units_name = ""
    try:
        if units_obj is not None:
            if hasattr(units_obj, "name"):
                units_name = str(units_obj.name())
            else:
                units_name = str(units_obj)
    except Exception:
        units_name = ""
    u = units_name.upper().replace("-", "_").replace(" ", "_").strip()
    is_elapsed = "ELAPSED" in u
    if "WEEK" in u:
        days = val * 7.0
    elif "MONTH" in u:
        days = val * 30.0
    elif "DAY" in u:
        days = val * 1.0
    elif "HOUR" in u:
        days = val / (24.0 if is_elapsed else 8.0)
    elif "MINUTE" in u:
        days = val / (1440.0 if is_elapsed else 480.0)
    elif "YEAR" in u:
        days = val * 365.0
    else:
        # Unknown unit -- treat the numeric value as days (defensive).
        days = val
    if days >= 0:
        return int(math.floor(days + 0.5))
    return -int(math.floor(abs(days) + 0.5))


def parse_mpp_xer(content: bytes, filename: str) -> Tuple[List[dict], List[str]]:
    """Parse Microsoft Project (.mpp), Primavera (.xer / .xml), MPX, or
    Project XML using JPype + MPXJ. Master prompt §1 — primary scheduling
    formats.

    The JVM starts once at module import time (cached). Each call writes
    the bytes to a temp file because MPXJ readers consume java.io.File
    handles, not byte streams.
    """
    import tempfile
    import os as _os
    try:
        import mpxj  # type: ignore
        import jpype  # type: ignore
    except ImportError:
        return [], ["MPXJ bridge not available on this runtime."]

    if not jpype.isJVMStarted():
        try:
            mpxj.startJVM()
        except Exception as e:
            return [], [f"JVM start failed: {e}"]

    suffix = "." + filename.rsplit(".", 1)[-1].lower()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(content)
    tmp.flush()
    tmp.close()

    rows: List[dict] = []
    errors: List[str] = []
    try:
        ext = filename.rsplit(".", 1)[-1].lower()
        # Per-format reader map (UniversalProjectReader can be flaky on
        # synthetic files; explicit readers are more reliable)
        reader_map = {
            "mpx": "org.mpxj.mpx.MPXReader",
            "xer": "org.mpxj.primavera.PrimaveraXERFileReader",
            "xml": "org.mpxj.primavera.PrimaveraPMFileReader",
            "mpp": "org.mpxj.mpp.MPPReader",
            "pp":  "org.mpxj.asta.AstaFileReader",
            "pmxml": "org.mpxj.primavera.PrimaveraPMFileReader",
        }
        reader_cls = reader_map.get(ext)
        if reader_cls is None:
            UniversalProjectReader = jpype.JClass("org.mpxj.reader.UniversalProjectReader")
            reader = UniversalProjectReader()
        else:
            reader = jpype.JClass(reader_cls)()
        project = reader.read(tmp.name)
        if project is None:
            return [], [f"MPXJ could not read {filename} (corrupt or unsupported variant)"]
        for task in project.getTasks():
            if task is None or task.getNull():
                continue
            unique_id = task.getUniqueID()
            wbs = task.getWBS()
            name = task.getName() or (f"task_{unique_id}" if unique_id else None)
            outline = task.getOutlineNumber()
            if not name:
                continue
            start = task.getStart()
            finish = task.getFinish()
            duration = task.getDuration()
            duration_days = None
            if duration is not None:
                try:
                    duration_days = int(round(float(duration.getDuration()) / 8.0)) if str(duration.getUnits()).lower() == "h" else int(duration.getDuration())
                except Exception:
                    pass
            # CPM Part B (2026-05-15) -- predecessor extraction now
            # captures relationship type + lag, not just the code.
            # Each entry is the canonical dict {code, type, lag_days}.
            preds: list[dict] = []
            try:
                for r in task.getPredecessors():
                    if r is None:
                        continue
                    # MPXJ 16+ renamed getTargetTask() → getPredecessorTask().
                    # Older versions used getTargetTask(); support both.
                    tgt = None
                    if hasattr(r, "getPredecessorTask"):
                        tgt = r.getPredecessorTask()
                    elif hasattr(r, "getTargetTask"):
                        tgt = r.getTargetTask()
                    if tgt is None:
                        continue
                    # Prefer activity_id / wbs (matches our activity_code) so the UI
                    # can render predecessors as readable codes like "1.5", not raw IDs.
                    try:
                        ref = tgt.getActivityID() or tgt.getWBS()
                    except Exception:
                        ref = None
                    if not ref:
                        ref = tgt.getID()
                    if ref is None:
                        continue
                    # CPM Part B -- extract relationship type. Match
                    # against enum .name() / toString() rather than
                    # importing the Java RelationType constants
                    # directly: this is version-tolerant across MPXJ
                    # variants that may spell the enum as
                    # FINISH_START vs Finish-Start vs FS_TASK_DEPENDENCY.
                    rtype = "FS"
                    try:
                        rt_obj = r.getType() if hasattr(r, "getType") else None
                        if rt_obj is not None:
                            rtype = _mpxj_relation_type_to_code(rt_obj)
                    except Exception:
                        rtype = "FS"
                    # CPM Part B -- extract lag (signed Duration).
                    # MPXJ Relation.getLag() returns a Duration with
                    # getDuration() + getUnits(); convert to integer
                    # calendar days, sign-preserved, half-up rounded.
                    lag_days = 0
                    try:
                        lag_obj = r.getLag() if hasattr(r, "getLag") else None
                        if lag_obj is not None:
                            lag_days = _mpxj_lag_to_calendar_days(lag_obj)
                    except Exception:
                        lag_days = 0
                    preds.append({
                        "code": str(ref),
                        "type": rtype,
                        "lag_days": lag_days,
                    })
            except Exception:
                pass
            # Resource names — MPP "Resource Names" column equivalent.
            resource_names: list[str] = []
            try:
                for ra in task.getResourceAssignments():
                    if ra is None: continue
                    res = ra.getResource()
                    if res is None: continue
                    nm = res.getName()
                    if nm: resource_names.append(str(nm))
            except Exception:
                pass
            try:
                act_code = task.getActivityID() or wbs or outline or str(unique_id)
            except Exception:
                act_code = wbs or str(unique_id)
            # Capture summary/heading info — MPXJ exposes Task.getSummary()
            # which is True for parent rollup rows ("Kitchen Renovation",
            # "Completion & Close-Outs") that should NOT become activities.
            try:
                is_summary = bool(task.getSummary()) if hasattr(task, "getSummary") else False
            except Exception:
                is_summary = False
            try:
                outline_level = int(task.getOutlineLevel()) if hasattr(task, "getOutlineLevel") and task.getOutlineLevel() is not None else None
            except Exception:
                outline_level = None
            rows.append({
                "activity_code": str(act_code) if act_code else f"task_{unique_id}",
                "description": str(name).strip(),
                "wbs": str(wbs) if wbs else None,
                "planned_start": start.toString()[:10] if start else None,
                "planned_finish": finish.toString()[:10] if finish else None,
                "duration_days": duration_days,
                "predecessors": preds,
                "resource_names": resource_names,
                "trade_code": None,
                "is_milestone": bool(task.getMilestone()) if hasattr(task, "getMilestone") else False,
                "is_summary": is_summary,
                "outline_level": outline_level,
            })
    except Exception as e:
        errors.append(f"MPXJ read failed: {e}")
    finally:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass
    return rows, errors


def parse_schedule_file(filename: str, content: bytes) -> Tuple[List[dict], List[str]]:
    fn = filename.lower()
    if fn.endswith(".xlsx"):
        return parse_xlsx(content)
    if fn.endswith(".csv"):
        return parse_csv(content)
    if fn.endswith(".mpp") or fn.endswith(".xer") or fn.endswith(".xml") or fn.endswith(".mpx"):
        return parse_mpp_xer(content, filename)
    return [], [f"Unsupported schedule file type: {filename}"]
