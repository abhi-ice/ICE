"""Detect summary / heading rows in a parsed schedule.

Strategy (no LLM — deterministic and free):
1. If MPXJ already set `is_summary=True` (only on .mpp/.xer paths) → trust it.
2. WBS-parent detection: if row's WBS code (e.g., "1") is a strict prefix of
   another row's WBS (e.g., "1.1", "1.2"), the row is a parent rollup.
3. Activity-code-pattern detection: same logic on dotted activity_codes.
4. "Whole-project" detection: if a row's planned_start equals the min start
   across all rows AND its planned_finish equals the max finish AND
   duration_days >= sum of children durations → it's the project banner.

Each row that fires any rule gets `excluded_as_summary=True` and a
`summary_reason` string. The user can flip it back on the UI.
"""
from __future__ import annotations
from typing import Iterable


def _is_strict_prefix(p: str, c: str) -> bool:
    """Return True iff p is a dotted prefix of c, e.g. '1' prefix of '1.1'."""
    if not p or not c or p == c:
        return False
    return c.startswith(p + ".")


def detect_summary_rows(rows: list[dict]) -> list[dict]:
    """Mutates rows in-place adding `excluded_as_summary` + `summary_reason`.
    Always returns the same list for chaining. NEVER mutates fields the
    diff or applier care about (planned_start/finish/duration_days remain
    untouched)."""
    # Default both flags
    for r in rows:
        if "excluded_as_summary" not in r:
            r["excluded_as_summary"] = False
        r.setdefault("summary_reason", None)

    # Rule 1 — trust MPXJ
    for r in rows:
        if r.get("is_summary"):
            r["excluded_as_summary"] = True
            r["summary_reason"] = "MPXJ flagged as summary"

    # Rule 2 + 3 — WBS / activity_code prefix
    wbs_codes = {r.get("wbs"): r for r in rows if r.get("wbs")}
    ac_codes = {r.get("activity_code"): r for r in rows if r.get("activity_code")}
    all_wbs = list(wbs_codes.keys())
    all_acs = list(ac_codes.keys())
    for r in rows:
        if r["excluded_as_summary"]:
            continue
        w = r.get("wbs")
        ac = r.get("activity_code")
        for other in all_wbs:
            if w and _is_strict_prefix(str(w), str(other)):
                r["excluded_as_summary"] = True
                r["summary_reason"] = f"WBS {w} is a parent of {other}"
                break
        if r["excluded_as_summary"]:
            continue
        for other in all_acs:
            if ac and _is_strict_prefix(str(ac), str(other)):
                r["excluded_as_summary"] = True
                r["summary_reason"] = f"activity_code {ac} is a parent of {other}"
                break

    # Rule 4 — whole-project banner (covers schedules with no WBS dots)
    real = [r for r in rows if not r["excluded_as_summary"]
            and r.get("planned_start") and r.get("planned_finish")]
    if len(real) > 1:
        starts = sorted([str(r["planned_start"])[:10] for r in real])
        finishes = sorted([str(r["planned_finish"])[:10] for r in real])
        min_start, max_finish = starts[0], finishes[-1]
        for r in rows:
            if r["excluded_as_summary"]:
                continue
            ps = str(r.get("planned_start") or "")[:10]
            pf = str(r.get("planned_finish") or "")[:10]
            if ps == min_start and pf == max_finish:
                # Only flag if there are MULTIPLE other tasks within this span
                others = [
                    o for o in real
                    if o is not r
                    and str(o.get("planned_start") or "")[:10] >= min_start
                    and str(o.get("planned_finish") or "")[:10] <= max_finish
                ]
                if len(others) >= 5:  # safety threshold to avoid false positives on short schedules
                    r["excluded_as_summary"] = True
                    r["summary_reason"] = "spans the entire project window"
    return rows


def visible_rows(rows: Iterable[dict]) -> list[dict]:
    """Return only the rows that are NOT marked as summaries — used by the
    diff and the applier."""
    return [r for r in rows if not r.get("excluded_as_summary")]
