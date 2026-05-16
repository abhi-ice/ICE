"""CPM Part L-1 (2026-05-16) -- working-day date arithmetic helpers.

CPM Step 1 (2026-05-17, Issue 3.D) -- FEDERAL-HOLIDAY-AWARE.

Federal construction CPM math operates in WORKING DAYS, NOT calendar
days. Pre-Part-L cpm.py used pure calendar arithmetic, producing
dates that landed on Saturdays / Sundays. Part L fixed weekends.
Step 1 closes the remaining gap: working days also skip US FEDERAL
HOLIDAYS.

WHY (Issue 3.D root cause): MS Project schedules -- and therefore
the imported planned dates -- are built on a calendar that skips
federal holidays. Pre-Step-1 this module skipped only Mon-Fri, so:
  * compute_cpm re-derived dates treating July 4 / Memorial Day /
    Juneteenth as ordinary working days, packing the schedule
    tighter than the imported MS Project dates.
  * the cascade could land an Impacted Start ON a federal holiday.
Step 1 makes ALL working-day arithmetic holiday-aware so the
engine's day-counting matches the contract schedule.

DESIGN DECISION D1 (Step 1) -- the `holidays` parameter on every
helper defaults to None, and None means "use the built-in
FEDERAL_HOLIDAYS set" (OPTION A: federal-aware BY DEFAULT). This
guarantees no call site can accidentally stay holiday-blind -- a
caller must OPT OUT explicitly by passing `holidays=frozenset()`
(empty set) for the rare pure-weekend-only case. Each helper
resolves the effective set ONCE at the top and threads the concrete
set (never None) into its internal calls, so an explicit empty set
propagates correctly through the recursion.

SCOPE: this is a FIXED federal-holiday calendar (the 11 US federal
holidays, observed-date rules applied -- a Saturday holiday observes
on the preceding Friday, a Sunday holiday on the following Monday).
Project-specific calendars, state holidays, and per-project no-work
days are OUT OF SCOPE for Step 1. (services/submittal_sla.py layers
project no-work days on top of this set for SLA math; it imports
FEDERAL_HOLIDAYS from here -- single source of truth.)

DESIGN DECISIONS (Part L, unchanged):
  D-L1. Lag is working days unless explicitly flagged otherwise.
  D-L2. Weekend / holiday predecessor finishes get rolled back to
        the prior business day before the +1 FS offset.
  D-L3. Duration = 0 (milestones): no working-day accumulation.
  D-L4. Empty / null lag treated as 0 working days.
  D-L5. Backward pass mirrors forward: prev_business_day for the
        FS offset.

ALL helpers operate on datetime.date.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


# =====================================================================
# CPM Step 1 (2026-05-17) -- canonical US federal-holiday calendar.
#
# SINGLE SOURCE OF TRUTH. services/submittal_sla.py imports this set
# (it previously held its own FEDERAL_HOLIDAYS_2026 copy -- now
# consolidated here). The frontend mirror lives in
# frontend/src/lib/dateUtil.js and MUST be kept in sync with this set.
#
# Each entry is the OBSERVED date (the actual day off):
#   * a holiday on Saturday  -> observed the preceding Friday
#   * a holiday on Sunday    -> observed the following Monday
# Sourced from the US OPM federal-holiday calendars, 2025-2028.
# Schedules running outside 2025-2028 degrade gracefully -- weekends
# are still skipped, only the holiday filter is inert for those years.
# A future bucket can swap this for python-holidays or a per-project
# table; the helper signatures already accept an injected set.
# =====================================================================
FEDERAL_HOLIDAYS: frozenset[date] = frozenset({
    # ---- 2025 ----
    date(2025, 1, 1),     # New Year's Day (Wed)
    date(2025, 1, 20),    # Martin Luther King Jr. Day
    date(2025, 2, 17),    # Presidents' Day
    date(2025, 5, 26),    # Memorial Day
    date(2025, 6, 19),    # Juneteenth (Thu)
    date(2025, 7, 4),     # Independence Day (Fri)
    date(2025, 9, 1),     # Labor Day
    date(2025, 10, 13),   # Columbus Day
    date(2025, 11, 11),   # Veterans Day (Tue)
    date(2025, 11, 27),   # Thanksgiving
    date(2025, 12, 25),   # Christmas Day (Thu)
    # ---- 2026 ----
    date(2026, 1, 1),     # New Year's Day (Thu)
    date(2026, 1, 19),    # Martin Luther King Jr. Day
    date(2026, 2, 16),    # Presidents' Day
    date(2026, 5, 25),    # Memorial Day
    date(2026, 6, 19),    # Juneteenth (Fri)
    date(2026, 7, 3),     # Independence Day observed (July 4 is Sat)
    date(2026, 9, 7),     # Labor Day
    date(2026, 10, 12),   # Columbus Day
    date(2026, 11, 11),   # Veterans Day (Wed)
    date(2026, 11, 26),   # Thanksgiving
    date(2026, 12, 25),   # Christmas Day (Fri)
    # ---- 2027 ----
    date(2027, 1, 1),     # New Year's Day (Fri)
    date(2027, 1, 18),    # Martin Luther King Jr. Day
    date(2027, 2, 15),    # Presidents' Day
    date(2027, 5, 31),    # Memorial Day
    date(2027, 6, 18),    # Juneteenth observed (June 19 is Sat)
    date(2027, 7, 5),     # Independence Day observed (July 4 is Sun)
    date(2027, 9, 6),     # Labor Day
    date(2027, 10, 11),   # Columbus Day
    date(2027, 11, 11),   # Veterans Day (Thu)
    date(2027, 11, 25),   # Thanksgiving
    date(2027, 12, 24),   # Christmas Day observed (Dec 25 is Sat)
    date(2027, 12, 31),   # New Year's Day 2028 observed (Jan 1 is Sat)
    # ---- 2028 ----
    date(2028, 1, 17),    # Martin Luther King Jr. Day
    date(2028, 2, 21),    # Presidents' Day
    date(2028, 5, 29),    # Memorial Day
    date(2028, 6, 19),    # Juneteenth (Mon)
    date(2028, 7, 4),     # Independence Day (Tue)
    date(2028, 9, 4),     # Labor Day
    date(2028, 10, 9),    # Columbus Day
    date(2028, 11, 10),   # Veterans Day observed (Nov 11 is Sat)
    date(2028, 11, 23),   # Thanksgiving
    date(2028, 12, 25),   # Christmas Day (Mon)
})


def _resolve(holidays: Optional[frozenset]) -> frozenset:
    """D1 -- None means the built-in federal set; an explicit set
    (including an empty frozenset, to opt out) is used verbatim."""
    return FEDERAL_HOLIDAYS if holidays is None else holidays


def is_business_day(d: date, holidays: Optional[frozenset] = None) -> bool:
    """True iff `d` is Mon-Fri AND not a federal holiday.

    holidays=None -> built-in FEDERAL_HOLIDAYS (D1, Option A).
    Pass holidays=frozenset() for weekend-only (no holiday filter).
    """
    # datetime.date.weekday(): Mon=0, ..., Fri=4, Sat=5, Sun=6.
    return d.weekday() < 5 and d not in _resolve(holidays)


def next_business_day(d: date, holidays: Optional[frozenset] = None) -> date:
    """Next business day strictly AFTER d, skipping weekends + holidays.

    Examples (federal-aware default):
        Fri before Memorial Day -> the Tuesday after (Mon is the
        holiday, Sat/Sun the weekend).
    """
    hol = _resolve(holidays)
    nd = d + timedelta(days=1)
    while not is_business_day(nd, hol):
        nd += timedelta(days=1)
    return nd


def prev_business_day(d: date, holidays: Optional[frozenset] = None) -> date:
    """Next business day strictly BEFORE d, skipping weekends + holidays.

    Mirror of next_business_day for the backward pass.
    """
    hol = _resolve(holidays)
    pd = d - timedelta(days=1)
    while not is_business_day(pd, hol):
        pd -= timedelta(days=1)
    return pd


def add_working_days(
    start: date, n: int, holidays: Optional[frozenset] = None,
) -> date:
    """Add n working days, skipping weekends + federal holidays.

    n = 0: returns start unchanged if it is a business day; rolls
           forward to the next business day if start is a weekend
           OR a holiday (D-L2 / D-L3).
    n > 0 / n < 0: rolls forward / backward |n| business days.
    """
    hol = _resolve(holidays)
    # Anchor start onto a business day if it isn't already (weekend
    # OR holiday). next_business_day takes "strictly after", so step
    # back one calendar day first to get "today or next".
    if not is_business_day(start, hol):
        start = next_business_day(start - timedelta(days=1), hol)
    if n == 0:
        return start
    d = start
    remaining = abs(n)
    direction = 1 if n > 0 else -1
    while remaining > 0:
        d = d + timedelta(days=direction)
        if is_business_day(d, hol):
            remaining -= 1
    return d


def working_days_between(
    start: date, end: date, holidays: Optional[frozenset] = None,
) -> int:
    """Count of working days from `start` (inclusive) to `end`
    (exclusive), skipping weekends + federal holidays. Negative when
    end < start.

    Semantic notes (federal-aware default):
      working_days_between(Mon, Mon)      == 0
      working_days_between(Mon, Tue)      == 1
      a span that contains July 4 does NOT count July 4.
    """
    hol = _resolve(holidays)
    if start > end:
        return -working_days_between(end, start, hol)
    d = start
    count = 0
    while d < end:
        if is_business_day(d, hol):
            count += 1
        d += timedelta(days=1)
    return count


def calendar_days_between(start: date, end: date) -> int:
    """Plain calendar-day count between two dates. Unchanged by Step 1
    -- calendar days never skip anything. Here for the (still empty)
    case where a predecessor lag is genuinely elapsed/calendar."""
    return (end - start).days
