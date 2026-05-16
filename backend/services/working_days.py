"""CPM Part L-1 (2026-05-16) -- working-day date arithmetic helpers.

Federal construction CPM math operates in WORKING DAYS (Mon-Fri),
NOT calendar days. Pre-Part-L cpm.py used pure calendar arithmetic
throughout, which produced dates landing on Saturdays and Sundays
when delays / RFIs / submittals shifted tasks forward. Field
supers lost trust immediately ("Impacted Finish 05/17 Sunday? Our
crew doesn't work weekends.").

Part L moves ALL date arithmetic to working days. The forward
pass, backward pass, lag handling, duration accumulation, and
projected_finish_date / projected_start_date shifts all use the
helpers in this module. Only INPUT lag values can be calendar
(e.g., "concrete cure 7 days" is genuine calendar) -- but the
predecessor schema {code, type, lag_days} has no elapsed flag,
so this module treats ALL lag values as working days.

This matches MS Project's default behavior for project schedules
(Schedule From > Working Days), Primavera P6's day-count default,
and the federal Critical Path Method conventions documented in
FAR Part 36.

DESIGN DECISIONS (resolved during Part L implementation):

  D1. Lag is working days unless explicitly flagged otherwise.
      The predecessor dict {code, type, lag_days} has no elapsed
      flag, so EVERY lag is working days. If a future schema
      adds is_elapsed: bool, this module gains a calendar-mode
      branch -- but that's future work.

  D2. If a predecessor finishes on a weekend (legacy data from
      pre-Part-L imports), the forward pass rolls the pred_ef
      back to prev_business_day BEFORE applying the +1 offset.
      This prevents weekend successor starts on legacy schedules.

  D3. Duration = 0 (milestones) skip working-day arithmetic.
      Milestone date = pred-derived start. Anchoring to a weekend
      is still tolerated and rolled forward by next_business_day.

  D4. Empty / null / missing lag treated as 0 working days.

  D5. Backward pass mirrors forward: prev_business_day for FS
      offset (instead of next_business_day in reverse).

Holiday awareness is OUT OF SCOPE for Part L. Federal holidays
live in services/submittal_sla.py::FEDERAL_HOLIDAYS_2026 already
but are not yet wired into the CPM core. A future Part M can add
a project-level holidays_calendar field and thread it through
this module (every helper would take an optional holidays: set[date]
kwarg). The current Mon-Fri filter handles the dominant case --
field crews work Mon-Fri.

ALL helpers operate on datetime.date. They DO NOT touch datetime.
datetime (callers convert from ISO strings via date.fromisoformat).
"""
from __future__ import annotations

from datetime import date, timedelta


def is_business_day(d: date) -> bool:
    """True iff `d` is Mon-Fri.

    Used by every helper below. Future extension: a project-level
    holidays_calendar would also filter out specific federal /
    project no-work dates.
    """
    # datetime.date.weekday(): Mon=0, Tue=1, ..., Fri=4, Sat=5, Sun=6.
    return d.weekday() < 5


def next_business_day(d: date) -> date:
    """Returns the next business day strictly AFTER d.

    Examples:
        Mon -> Tue
        Fri -> next Mon (skipping Sat + Sun)
        Sat -> next Mon
        Sun -> next Mon
    """
    nd = d + timedelta(days=1)
    while not is_business_day(nd):
        nd += timedelta(days=1)
    return nd


def prev_business_day(d: date) -> date:
    """Returns the next business day strictly BEFORE d.

    Mirror of next_business_day for the backward pass. The CPM
    backward-pass FS offset uses this to produce a predecessor's
    LF candidate from a successor's LS on a weekend.

    Examples:
        Tue -> Mon
        Mon -> prev Fri (skipping Sun + Sat)
        Sun -> prev Fri
        Sat -> prev Fri
    """
    pd = d - timedelta(days=1)
    while not is_business_day(pd):
        pd -= timedelta(days=1)
    return pd


def add_working_days(start: date, n: int) -> date:
    """Add n working days. n can be 0, positive, or negative.

    Semantic:
      n = 0: returns start UNCHANGED if start is a business day;
             rolls forward to next business day if start is weekend
             (defensive against legacy weekend planned_start values).
      n > 0: rolls forward n business days. add_working_days(Mon, 1)
             returns Tue. add_working_days(Fri, 1) returns next Mon.
             add_working_days(Mon, 5) returns next Mon (skipping
             1 weekend).
      n < 0: rolls backward |n| business days. add_working_days(Mon, -1)
             returns prev Fri.

    Used by:
      * Forward pass: EF = add_working_days(ES, dur - 1)
      * Forward pass FS lag: add_working_days(ef_rolled_forward, lag)
      * Backward pass: LS = add_working_days(LF, -(dur - 1))
      * Projected-finish shift: projected = add_working_days(planned, shift)
    """
    # First, anchor start onto a business day if it isn't already
    # (D2: weekend predecessor finish gets rolled forward; D3: zero-
    # duration milestones land on a business day too).
    if not is_business_day(start):
        # next_business_day takes "strictly after"; we want "today or next"
        # so we step back one day and then call next_business_day.
        start = next_business_day(start - timedelta(days=1))
    if n == 0:
        return start
    d = start
    remaining = abs(n)
    direction = 1 if n > 0 else -1
    while remaining > 0:
        d = d + timedelta(days=direction)
        if is_business_day(d):
            remaining -= 1
    return d


def working_days_between(start: date, end: date) -> int:
    """Count of working days from `start` (inclusive) to `end`
    (exclusive). Negative when end < start.

    Used by:
      * Part L-3 actual_impact_days computation when a delay is
        resolved: actual = working_days_between(logged_date,
        resolved_date).
      * Future TIA narrative for "delay aged N working days before
        resolution."

    Semantic notes:
      working_days_between(Mon, Mon) == 0
      working_days_between(Mon, Tue) == 1   (Mon is the only WD in range)
      working_days_between(Fri, Mon) == 1   (Fri is the only WD in range)
      working_days_between(Mon, NextMon) == 5  (Mon..Fri)
    """
    if start > end:
        return -working_days_between(end, start)
    d = start
    count = 0
    while d < end:
        if is_business_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def calendar_days_between(start: date, end: date) -> int:
    """Plain calendar-day count between two dates.

    Provided for the (currently empty) case where a predecessor lag
    is genuinely calendar (e.g., concrete cure 7 calendar days
    regardless of weekends). Pre-Part-L cpm.py treated ALL lag as
    calendar; Part L treats ALL as working. This helper is here
    for when the schema adds an is_elapsed: bool flag and per-lag
    routing becomes possible.

    Equivalent to (end - start).days.
    """
    return (end - start).days
