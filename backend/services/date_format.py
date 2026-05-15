"""Single chokepoint for date display-format conversions in backend
PDF / DOCX exports.

CPM Part G (2026-05-15) -- introduced to mirror the frontend's
lib/dateUtil.formatDateDisplay chokepoint so all user-facing date
strings in PDF + DOCX exports render in the same MM/DD/YYYY format
the on-screen pages use.

STORAGE LAYER -- DB writes, API payloads, CSV exports, ISO interval
strings on TIA metadata -- stays in YYYY-MM-DD throughout. This
helper is invoked at the LEAF render points where bytes about to
hit the user's screen / printer get formatted. There is no
mid-layer date conversion; every PDF / DOCX path calls
format_date_mmddyyyy() once, near the f-string or DOCX paragraph
that emits the date.

Why a separate module from the existing _iso_date / _format_cell_html
helpers in tasks_exports? Because those helpers are used by BOTH
the CSV/XLSX path (which must stay ISO) AND the PDF path (which
should become MM/DD/YYYY). Splitting the format concern into its
own module avoids the ambiguity of "should this helper format dates
or pass them through" -- now ISO helpers stay ISO and format helpers
emit MM/DD/YYYY.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
import re

# Strict YYYY-MM-DD match. We anchor both ends so an ID like
# "2026-05-15T13:00:00Z" only matches via its date-portion slice
# below, not via a partial match.
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def format_date_mmddyyyy(v: Any, default: str = "—") -> str:
    """Render a date value in MM/DD/YYYY (US construction industry
    convention; matches the frontend's lib/dateUtil.formatDateDisplay
    output).

    Accepts:
        date object        -> formatted MM/DD/YYYY
        datetime object    -> date portion -> MM/DD/YYYY (time discarded)
        "YYYY-MM-DD"       -> MM/DD/YYYY
        "YYYY-MM-DDTHH..." -> date portion taken; MM/DD/YYYY
        None / "" / NaN    -> `default` (em-dash "—" by default)
        anything else      -> `default`

    Defensive on bad inputs: a malformed string returns `default`,
    never raises -- the PDF generation pipeline must not crash on a
    bad date field.

    Used by every PDF / DOCX export path in the backend:
        services/tasks_exports.py        -- Tasks + Lookahead PDF
        services/tia_generator.py        -- TIA PDF/DOCX
        services/recovery_report.py      -- Recovery PDF
        routers/field_events_router.py   -- Delay AI report PDF
        routers/daily_reports_router.py  -- Daily Report PDF

    CSV + XLSX paths keep ISO YYYY-MM-DD for data-interchange
    purposes (CSV is consumed by spreadsheets / the analytics
    pipeline, not read directly by humans; XLSX cells natively
    render as dates in Excel based on locale).
    """
    if v is None or v == "":
        return default
    if isinstance(v, datetime):
        return v.strftime("%m/%d/%Y")
    if isinstance(v, date):
        return v.strftime("%m/%d/%Y")
    s = str(v)
    if not s:
        return default
    # Take the first 10 chars so ISO datetime strings ("2026-05-15T...")
    # reduce to their date portion. Then strict YYYY-MM-DD match.
    head = s[:10]
    m = _ISO_DATE_RE.match(head)
    if not m:
        return default
    yyyy, mm, dd = m.groups()
    return f"{mm}/{dd}/{yyyy}"
