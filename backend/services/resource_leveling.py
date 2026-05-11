"""Bucket H Phase 2 (2026-05-08) -- Resource Leveling service.

Pure-Python service layer for the cross-project supply/demand/gap
model. The Phase 2 router calls into these functions; recompute
triggers (Phase 4) re-use the recompute_* helpers.

Design notes
------------
- Gaps are NOT persisted in Phase 1/2. They're computed on read via
  a single aggregate per (tenant, trade, date) tuple over the
  crew_capacity (supply) and crew_demands (demand) collections.
  Phase 5 will materialize a crew_gaps collection if read latency
  becomes a bottleneck. For now the in-memory pipeline is small
  enough (capacity rows scale O(crews x window_days), demand rows
  scale O(tasks_with_required_trade x avg_task_duration)).

- All read functions are tenant-scoped via tenant_filter(). Cross-
  tenant URL manipulation returns empty results (4xx in the router).
  No data leaks through the service even if a caller forgets a
  guard.

- Date-range guard: 180-day cap enforced here (raises ValueError);
  router translates to HTTP 400. Some single-crew reads relax to
  90 days; the router decides.

- Severity ladder per spec Section 1.3:
    surplus  -> supply >= demand AND supply > demand (gap negative)
    none     -> supply == demand (gap == 0)
    minor    -> 0 < gap <= 20% of demand
    moderate -> 20% < gap <= 50% of demand
    major    -> gap > 50% of demand
  CP escalation: if demand on CP > 0 AND gap > 0, bump severity
  one rung (minor -> moderate, moderate -> major, major stays).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from collections import defaultdict

from database import get_db, tenant_filter

log = logging.getLogger("resource_leveling")

# Hard caps. Router validates against these; service raises ValueError
# on violation so anything that bypasses the router still fails safely.
MAX_RANGE_DAYS = 180
DEFAULT_RANGE_DAYS = 14
MAX_FUTURE_HORIZON_DAYS = 365


# =====================================================================
# Helpers
# =====================================================================

def _validate_range(start: date, end: date, *, max_days: int = MAX_RANGE_DAYS) -> None:
    if end < start:
        raise ValueError(f"date range invalid: end {end} before start {start}")
    span = (end - start).days + 1
    if span > max_days:
        raise ValueError(
            f"date range too wide ({span} days; max {max_days}). "
            f"Split the request or paginate."
        )


def _date_iter(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _severity_for_gap(*, supply: int, demand: int, on_cp: bool) -> str:
    """Returns one of GapSeverity literal values."""
    if demand <= 0:
        return "surplus" if supply > 0 else "none"
    gap = demand - supply
    if gap <= 0:
        return "none" if gap == 0 else "surplus"
    ratio = gap / float(demand)
    if ratio <= 0.20:
        sev = "minor"
    elif ratio <= 0.50:
        sev = "moderate"
    else:
        sev = "major"
    # CP escalation -- bump one rung if this gap sits on the critical path.
    if on_cp:
        sev = {"minor": "moderate", "moderate": "major", "major": "major"}[sev]
    return sev


def _gap_id(trade: str, d: date) -> str:
    """Synthetic gap id for the suggestions endpoint (gaps aren't
    persisted in Phase 2). Format: <trade>__<YYYY-MM-DD>. Trade is
    free-text so we double-underscore-separate to keep parsing
    unambiguous even when trade names contain hyphens or single
    underscores."""
    return f"{trade}__{d.isoformat()}"


def parse_gap_id(gap_id: str) -> tuple[str, date]:
    """Reverse of _gap_id. Raises ValueError on malformed input."""
    if not gap_id or "__" not in gap_id:
        raise ValueError(f"malformed gap_id: {gap_id!r}")
    trade, date_part = gap_id.rsplit("__", 1)
    return trade, date.fromisoformat(date_part)


# =====================================================================
# Compute primitives
# =====================================================================

async def compute_supply_for_trade(
    tenant_id: str, trade: str, start: date, end: date,
) -> dict[date, int]:
    """Returns {date: available_headcount} summed across all crews of
    the trade. Empty trades / empty ranges return {}."""
    _validate_range(start, end)
    db = get_db()
    rows = db.crew_capacity.find(
        tenant_filter(tenant_id, {
            "trade": trade,
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "date": 1, "available_headcount": 1},
    )
    out: dict[date, int] = defaultdict(int)
    async for r in rows:
        try:
            d = date.fromisoformat(str(r["date"])[:10])
        except Exception:
            continue
        out[d] += int(r.get("available_headcount") or 0)
    return dict(out)


async def compute_demand_for_trade(
    tenant_id: str, trade: str, start: date, end: date,
) -> dict[date, dict]:
    """Returns {date: {demanded: int, hours: float, on_cp: bool,
    project_ids: list[str]}}. Aggregates across all projects."""
    _validate_range(start, end)
    db = get_db()
    rows = db.crew_demands.find(
        tenant_filter(tenant_id, {
            "trade": trade,
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "date": 1, "project_id": 1, "demanded_headcount": 1,
         "hours_demanded": 1, "is_critical_path": 1},
    )
    out: dict[date, dict] = defaultdict(
        lambda: {"demanded": 0, "hours": 0.0, "on_cp": False,
                  "project_ids": []},
    )
    async for r in rows:
        try:
            d = date.fromisoformat(str(r["date"])[:10])
        except Exception:
            continue
        slot = out[d]
        slot["demanded"] += int(r.get("demanded_headcount") or 0)
        slot["hours"] += float(r.get("hours_demanded") or 0)
        slot["on_cp"] = slot["on_cp"] or bool(r.get("is_critical_path"))
        pid = r.get("project_id")
        if pid and pid not in slot["project_ids"]:
            slot["project_ids"].append(pid)
    return dict(out)


async def compute_gap_for_trade(
    tenant_id: str, trade: str, start: date, end: date,
) -> list[dict]:
    """Returns a list of gap-row dicts (matches GapRow read model)
    spanning the requested date range. Walks dates with EITHER supply
    OR demand; days with neither are omitted (no point rendering an
    all-zero cell)."""
    _validate_range(start, end)
    supply = await compute_supply_for_trade(tenant_id, trade, start, end)
    demand = await compute_demand_for_trade(tenant_id, trade, start, end)
    all_dates = set(supply.keys()) | set(demand.keys())
    rows: list[dict] = []
    for d in sorted(all_dates):
        s = int(supply.get(d, 0))
        dem_slot = demand.get(d) or {"demanded": 0, "on_cp": False, "project_ids": []}
        dem = int(dem_slot["demanded"])
        on_cp = bool(dem_slot["on_cp"])
        sev = _severity_for_gap(supply=s, demand=dem, on_cp=on_cp)
        rows.append({
            "id": _gap_id(trade, d),
            "trade": trade,
            "date": d.isoformat(),
            "supply": s,
            "demand": dem,
            "gap": dem - s,
            "severity": sev,
            "is_critical_path": on_cp,
            "contributing_project_ids": dem_slot["project_ids"],
        })
    return rows


async def _list_trades_in_scope(tenant_id: str, start: date, end: date) -> list[str]:
    """Union of trades that have either supply or demand in the window."""
    db = get_db()
    trades: set[str] = set()
    async for r in db.crew_capacity.find(
        tenant_filter(tenant_id, {
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "trade": 1},
    ):
        if r.get("trade"):
            trades.add(r["trade"])
    async for r in db.crew_demands.find(
        tenant_filter(tenant_id, {
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "trade": 1},
    ):
        if r.get("trade"):
            trades.add(r["trade"])
    return sorted(trades)


# =====================================================================
# Public read APIs (router-facing)
# =====================================================================

async def get_cross_project_view(
    *, tenant_id: str, start: date, end: date,
    trade_filter: Optional[list[str]] = None,
    project_filter: Optional[list[str]] = None,
) -> dict:
    """Returns the dashboard payload: {trades: [trade_str],
    dates: [iso_str], cells: {trade: {date: gap_row}}, summary: {...}}.
    Trade filter / project filter narrow the result.

    project_filter narrows which demand rows count toward the matrix
    (supply is tenant-wide -- crews don't belong to a single project
    by default, they get committed via reallocation flows). If
    project_filter is set AND no demand matches, the matrix renders
    supply-only days.
    """
    _validate_range(start, end)
    all_trades = await _list_trades_in_scope(tenant_id, start, end)
    if trade_filter:
        filter_set = {t.strip() for t in trade_filter if t.strip()}
        trades = [t for t in all_trades if t in filter_set]
    else:
        trades = all_trades

    cells: dict[str, dict[str, dict]] = {}
    summary = {
        "total_cells": 0,
        "shortage_cells": 0,
        "surplus_cells": 0,
        "balanced_cells": 0,
        "cp_shortage_cells": 0,
        "trades_in_scope": len(trades),
        "date_range_start": start.isoformat(),
        "date_range_end": end.isoformat(),
    }

    db = get_db()
    for trade in trades:
        supply = await compute_supply_for_trade(tenant_id, trade, start, end)
        # Per-trade demand WITH optional project filter applied.
        demand_q = {
            "trade": trade,
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }
        if project_filter:
            demand_q["project_id"] = {"$in": list(project_filter)}
        demand_rows = await db.crew_demands.find(
            tenant_filter(tenant_id, demand_q),
            {"_id": 0, "date": 1, "project_id": 1, "demanded_headcount": 1,
             "is_critical_path": 1, "source_task_ids": 1},
        ).to_list(length=None)
        per_date: dict[date, dict] = defaultdict(
            lambda: {"demanded": 0, "on_cp": False, "project_ids": [],
                      "task_ids": []},
        )
        for r in demand_rows:
            try:
                d = date.fromisoformat(str(r["date"])[:10])
            except Exception:
                continue
            slot = per_date[d]
            slot["demanded"] += int(r.get("demanded_headcount") or 0)
            slot["on_cp"] = slot["on_cp"] or bool(r.get("is_critical_path"))
            pid = r.get("project_id")
            if pid and pid not in slot["project_ids"]:
                slot["project_ids"].append(pid)
            for tid in (r.get("source_task_ids") or []):
                if tid not in slot["task_ids"]:
                    slot["task_ids"].append(tid)

        trade_cells: dict[str, dict] = {}
        all_dates = set(supply.keys()) | set(per_date.keys())
        for d in sorted(all_dates):
            s = int(supply.get(d, 0))
            dem_slot = per_date.get(d) or {"demanded": 0, "on_cp": False,
                                             "project_ids": [], "task_ids": []}
            dem = int(dem_slot["demanded"])
            on_cp = bool(dem_slot["on_cp"])
            sev = _severity_for_gap(supply=s, demand=dem, on_cp=on_cp)
            cell = {
                "id": _gap_id(trade, d),
                "supply": s, "demand": dem, "gap": dem - s,
                "severity": sev, "is_critical_path": on_cp,
                "contributing_project_ids": dem_slot["project_ids"],
                "contributing_task_ids": dem_slot["task_ids"],
            }
            trade_cells[d.isoformat()] = cell
            summary["total_cells"] += 1
            if sev in ("minor", "moderate", "major"):
                summary["shortage_cells"] += 1
                if on_cp:
                    summary["cp_shortage_cells"] += 1
            elif sev == "surplus":
                summary["surplus_cells"] += 1
            else:
                summary["balanced_cells"] += 1
        cells[trade] = trade_cells

    # Dates axis = union of every cell key, sorted.
    date_set: set[str] = set()
    for trade_cells in cells.values():
        date_set.update(trade_cells.keys())
    dates_axis = sorted(date_set)
    return {
        "trades": trades,
        "dates": dates_axis,
        "cells": cells,
        "summary": summary,
    }


async def get_project_demand_breakdown(
    *, tenant_id: str, project_id: str, start: date, end: date,
) -> dict:
    """Per-project demand profile: {trades: [...], days: [...],
    matrix: {trade: {date: {demanded, hours, on_cp, task_ids}}}}."""
    _validate_range(start, end)
    db = get_db()
    rows = await db.crew_demands.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "date": 1, "trade": 1, "demanded_headcount": 1,
         "hours_demanded": 1, "is_critical_path": 1, "source_task_ids": 1},
    ).to_list(length=None)
    matrix: dict[str, dict[str, dict]] = defaultdict(dict)
    trades: set[str] = set()
    days: set[str] = set()
    for r in rows:
        trades.add(r["trade"])
        days.add(str(r["date"])[:10])
        matrix[r["trade"]][str(r["date"])[:10]] = {
            "demanded": int(r.get("demanded_headcount") or 0),
            "hours": float(r.get("hours_demanded") or 0),
            "on_cp": bool(r.get("is_critical_path")),
            "task_ids": list(r.get("source_task_ids") or []),
        }
    return {
        "project_id": project_id,
        "trades": sorted(trades),
        "days": sorted(days),
        "matrix": {k: v for k, v in matrix.items()},
    }


async def get_crew_utilization_rollup(
    *, tenant_id: str, start: date, end: date,
    accessible_project_ids: Optional[list[str]] = None,
) -> list[dict]:
    """Bucket H Phase 4 fix -- per-crew aggregates over a date window.

    Returns one row per crew in the tenant:
      {crew: {id, name, trade, company_name, default_headcount,
              home_project_id, status},
       avg_utilization_pct: float (0-100; weekday-only baseline),
       over_committed_days: int,
       under_utilized_days: int,
       total_committed_hours: float,
       total_available_hours: float}

    Utilization = committed / total per weekday (holidays excluded
    from the denominator). avg_utilization_pct is the simple mean
    across working days. Over-committed = committed > total. Under-
    utilized = committed < 50% of total (per spec).

    accessible_project_ids: if supplied (non-admin caller), filters
    crews to ones whose home_project_id is in the set OR null
    (unassigned crews remain visible).
    """
    _validate_range(start, end)
    db = get_db()
    crew_q: dict = {"tenant_id": tenant_id, "status": {"$ne": "archived"}}
    if accessible_project_ids is not None:
        crew_q["$or"] = [
            {"home_project_id": {"$in": list(accessible_project_ids)}},
            {"home_project_id": None},
            {"home_project_id": {"$exists": False}},
        ]
    crews = await db.crews.find(
        crew_q,
        {"_id": 0, "id": 1, "name": 1, "trade": 1, "company_name": 1,
         "default_headcount": 1, "target_size": 1, "home_project_id": 1,
         "status": 1},
    ).to_list(length=None)

    out: list[dict] = []
    for c in crews:
        rows = await db.crew_capacity.find(
            {"tenant_id": tenant_id, "crew_id": c["id"],
             "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}},
            {"_id": 0, "total_headcount": 1, "committed_headcount": 1,
             "available_headcount": 1, "hours_per_person": 1, "is_holiday": 1},
        ).to_list(length=None)
        working_days = [r for r in rows if not r.get("is_holiday")]
        total_avail_h = 0.0
        total_committed_h = 0.0
        over = 0
        under = 0
        util_sum = 0.0
        util_n = 0
        for r in working_days:
            total = int(r.get("total_headcount") or 0)
            committed = int(r.get("committed_headcount") or 0)
            hours = float(r.get("hours_per_person") or 8.0)
            total_avail_h += total * hours
            total_committed_h += committed * hours
            if total > 0:
                ratio = committed / total
                util_sum += ratio
                util_n += 1
                if committed > total:
                    over += 1
                elif ratio < 0.5:
                    under += 1
        avg_util = (util_sum / util_n * 100.0) if util_n else 0.0
        out.append({
            "crew": {
                "id": c["id"], "name": c.get("name"), "trade": c.get("trade"),
                "company_name": c.get("company_name"),
                "default_headcount": c.get("default_headcount"),
                "target_size": c.get("target_size"),
                "home_project_id": c.get("home_project_id"),
                "status": c.get("status"),
            },
            "avg_utilization_pct": round(avg_util, 1),
            "over_committed_days": over,
            "under_utilized_days": under,
            "total_committed_hours": round(total_committed_h, 1),
            "total_available_hours": round(total_avail_h, 1),
            "working_days_in_window": util_n,
        })
    # Sort: over-committed first, then by under-utilized, then name.
    out.sort(key=lambda r: (-r["over_committed_days"],
                              -r["under_utilized_days"],
                              (r["crew"].get("name") or "").lower()))
    return out


async def get_crew_commitment_breakdown(
    *, tenant_id: str, crew_id: str, start: date, end: date,
) -> dict:
    """Per-crew calendar: per-day total / committed / available
    headcount, and (Phase 4) which projects are committing. In Phase
    2 the commitments_by_project map is empty until the reallocation
    flow writes per-project commit records."""
    _validate_range(start, end)
    db = get_db()
    crew = await db.crews.find_one(
        tenant_filter(tenant_id, {"id": crew_id}),
        {"_id": 0, "id": 1, "name": 1, "trade": 1, "company_name": 1,
         "default_headcount": 1, "target_size": 1, "status": 1,
         "home_project_id": 1},
    )
    if not crew:
        # Tenant-leak-resistant: 404 (router translates).
        raise LookupError(f"crew {crew_id} not found")
    rows = await db.crew_capacity.find(
        tenant_filter(tenant_id, {
            "crew_id": crew_id,
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }),
        {"_id": 0, "date": 1, "total_headcount": 1, "committed_headcount": 1,
         "available_headcount": 1, "is_holiday": 1, "notes": 1,
         "computed_from": 1, "hours_per_person": 1},
    ).sort("date", 1).to_list(length=None)
    days = []
    for r in rows:
        days.append({
            "date": str(r["date"])[:10],
            "total_headcount": int(r.get("total_headcount") or 0),
            "committed_headcount": int(r.get("committed_headcount") or 0),
            "available_headcount": int(r.get("available_headcount") or 0),
            "hours_per_person": float(r.get("hours_per_person") or 8.0),
            "is_holiday": bool(r.get("is_holiday")),
            "notes": r.get("notes"),
            "computed_from": r.get("computed_from"),
        })
    return {"crew": crew, "days": days}


async def list_gaps(
    *, tenant_id: str, start: date, end: date,
    severity_filter: Optional[list[str]] = None,
    trade_filter: Optional[list[str]] = None,
    limit: int = 100, offset: int = 0,
) -> dict:
    """Flat list of (trade, date) gap rows for the gap-list tab.
    Sorted by severity DESC (major first), then by date ASC.
    Pagination via offset/limit."""
    _validate_range(start, end)
    trades = await _list_trades_in_scope(tenant_id, start, end)
    if trade_filter:
        ts = {t.strip() for t in trade_filter if t.strip()}
        trades = [t for t in trades if t in ts]
    all_rows: list[dict] = []
    for trade in trades:
        for row in await compute_gap_for_trade(tenant_id, trade, start, end):
            if row["gap"] <= 0:
                # Only true shortages on the gap-list tab; surpluses
                # are visible on the heat map / breakdown.
                continue
            if severity_filter and row["severity"] not in severity_filter:
                continue
            all_rows.append(row)
    # Severity sort key (major first), then date asc, then trade asc.
    sev_rank = {"major": 0, "moderate": 1, "minor": 2}
    all_rows.sort(key=lambda r: (sev_rank.get(r["severity"], 99),
                                   r["date"], r["trade"]))
    total = len(all_rows)
    page = all_rows[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "rows": page}


# =====================================================================
# Resolution suggestions (rule-based; AI-augmented later)
# =====================================================================

async def find_gap_resolution_options(
    *, tenant_id: str, gap_id: str,
) -> dict:
    """Returns the gap row plus a list of resolution suggestions.
    Rules (per spec Section 2.1):
      - small gap (<20% shortfall): suggest overtime
      - surplus exists in another project same date: suggest realloc
      - persistent (>5 days): suggest sub-contract
      - CP demand: priority badge above non-CP gaps
    """
    trade, gap_date = parse_gap_id(gap_id)
    # Rebuild the gap row by computing the single (trade, date) tuple.
    gap_rows = await compute_gap_for_trade(tenant_id, trade, gap_date, gap_date)
    if not gap_rows:
        raise LookupError(f"no gap data for {trade}@{gap_date}")
    gap_row = gap_rows[0]
    suggestions: list[dict] = []

    db = get_db()

    # Rule 1: small gap -> overtime
    if 0 < gap_row["gap"] and gap_row["demand"] > 0:
        shortfall_ratio = gap_row["gap"] / float(gap_row["demand"])
        if shortfall_ratio < 0.20:
            # Estimate overtime hours = gap headcount * 2h OT.
            est_hours = gap_row["gap"] * 2.0
            suggestions.append({
                "action_type": "overtime",
                "title": f"Authorize {est_hours:.0f}h overtime across existing crew",
                "rationale": (
                    f"Shortfall is small ({shortfall_ratio*100:.0f}% of demand). "
                    f"Existing crew can cover with 2h OT each."
                ),
                "headcount": gap_row["gap"],
                "from_project_id": None,
                "to_project_id": gap_row["contributing_project_ids"][0] if gap_row["contributing_project_ids"] else None,
                "date_range_start": gap_date.isoformat(),
                "date_range_end": gap_date.isoformat(),
                "estimated_cost_usd": None,  # Phase 5 wires burdened-cost rollup
                "priority": "high" if gap_row["is_critical_path"] else "normal",
            })

    # Rule 2: surplus in another project same date -> reallocation
    # Find other projects with this trade NOT demanding fully on the
    # same date, AND crews of the trade with available headcount.
    if gap_row["gap"] > 0:
        avail_rows = await db.crew_capacity.find(
            tenant_filter(tenant_id, {
                "trade": trade,
                "date": gap_date.isoformat(),
                "available_headcount": {"$gt": 0},
            }),
            {"_id": 0, "crew_id": 1, "available_headcount": 1, "company_name": 1},
        ).to_list(length=None)
        total_available = sum(int(r.get("available_headcount") or 0) for r in avail_rows)
        if total_available > 0:
            # Identify a probable source project: a project with this
            # trade on the same date but lower headcount than its
            # max demand date in the window (i.e. it has slack today).
            # Simplification: pick the first project that ISN'T in
            # contributing_project_ids of this gap.
            source_pid = None
            async for r in db.crew_demands.find(
                tenant_filter(tenant_id, {"trade": trade, "date": gap_date.isoformat()}),
                {"_id": 0, "project_id": 1},
            ):
                if r["project_id"] not in gap_row["contributing_project_ids"]:
                    source_pid = r["project_id"]
                    break
            move_count = min(gap_row["gap"], total_available)
            suggestions.append({
                "action_type": "reallocation",
                "title": f"Reallocate {move_count} {trade} from available pool",
                "rationale": (
                    f"{total_available} {trade}(s) have available headcount this date. "
                    + (f"Source project {source_pid} appears to have slack." if source_pid else
                        "Pool consists of crews with no current commitment today.")
                ),
                "headcount": move_count,
                "from_project_id": source_pid,
                "to_project_id": gap_row["contributing_project_ids"][0] if gap_row["contributing_project_ids"] else None,
                "date_range_start": gap_date.isoformat(),
                "date_range_end": gap_date.isoformat(),
                "estimated_cost_usd": None,
                "priority": "high" if gap_row["is_critical_path"] else "normal",
            })

    # Rule 3: persistent gap (>5 days same trade same project) -> sub
    if gap_row["gap"] > 0:
        check_start = gap_date - timedelta(days=2)
        check_end = gap_date + timedelta(days=4)
        # Window the same trade and look for >= 5 days of shortage.
        check_rows = await compute_gap_for_trade(
            tenant_id, trade, check_start, check_end,
        )
        consec_shortage = sum(1 for r in check_rows if r["gap"] > 0)
        if consec_shortage >= 5:
            suggestions.append({
                "action_type": "sub_contract",
                "title": f"Sub-contract {trade} resource for the next week+",
                "rationale": (
                    f"Shortage persists ≥5 of 7 days around {gap_date}. "
                    f"Existing crew won't catch up; bring in a sub or hire."
                ),
                "headcount": gap_row["gap"],
                "from_project_id": None,
                "to_project_id": gap_row["contributing_project_ids"][0] if gap_row["contributing_project_ids"] else None,
                "date_range_start": check_start.isoformat(),
                "date_range_end": check_end.isoformat(),
                "estimated_cost_usd": None,
                "priority": "high" if gap_row["is_critical_path"] else "normal",
            })

    return {"gap": gap_row, "suggestions": suggestions}


# =====================================================================
# Recompute helpers (Phase 4 triggers will call these)
# =====================================================================

async def recompute_capacity_for_crew(
    *, tenant_id: str, crew_id: str, horizon_days: int = 90,
) -> dict:
    """Refresh the 90-day capacity window for one crew. Idempotent.
    Used by:
      - crew create (initial seed)
      - crew update (default_headcount changed)
      - crew delete (zero out future capacity, preserve historical)
    """
    db = get_db()
    crew = await db.crews.find_one(
        tenant_filter(tenant_id, {"id": crew_id}),
        {"_id": 0, "id": 1, "trade": 1, "company_name": 1, "status": 1,
         "default_headcount": 1, "target_size": 1, "home_project_id": 1},
    )
    today = date.today()
    horizon = today + timedelta(days=horizon_days - 1)
    # Bucket H Phase 4 -- soft-delete via /crud/crews/{id} sets
    # status="deleted"; treat as archived for capacity recompute.
    is_archived = (crew is None) or (crew.get("status") in ("archived", "deleted"))
    written = 0
    zeroed = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    if is_archived:
        # Zero out FUTURE rows. Past rows preserve historical truth.
        res = await db.crew_capacity.update_many(
            tenant_filter(tenant_id, {
                "crew_id": crew_id,
                "date": {"$gte": today.isoformat(), "$lte": horizon.isoformat()},
            }),
            {"$set": {
                "total_headcount": 0, "committed_headcount": 0,
                "available_headcount": 0,
                "is_holiday": True,           # treat as N/A on the dashboard
                "notes": "crew archived",
                "computed_at": now_iso,
                "computed_from": "manual_entry",
            }},
        )
        zeroed = int(res.modified_count or 0)
        return {"written": 0, "zeroed": zeroed, "archived": True}

    trade = crew.get("trade") or "(unknown)"
    company = crew.get("company_name")
    total = int(crew.get("default_headcount") or crew.get("target_size") or 0)
    for d in _date_iter(today, horizon):
        holiday = d.weekday() >= 5
        update_doc = {
            "tenant_id": tenant_id,
            "crew_id": crew_id,
            "trade": trade,
            "company_name": company,
            "date": d.isoformat(),
            "total_headcount": total if not holiday else 0,
            "available_headcount": total if not holiday else 0,
            "hours_per_person": 8.0,
            "is_holiday": holiday,
            "computed_at": now_iso,
            "computed_from": "schedule_derived",
        }
        # Upsert; if committed_headcount was set by a reallocation,
        # preserve it via setOnInsert + recompute available.
        existing = await db.crew_capacity.find_one(
            {"crew_id": crew_id, "date": d.isoformat()},
            {"_id": 0, "id": 1, "committed_headcount": 1, "notes": 1,
             "computed_from": 1, "presence_locked": 1},
        )
        if existing:
            committed = int(existing.get("committed_headcount") or 0)
            update_doc["committed_headcount"] = committed
            update_doc["available_headcount"] = max(0, update_doc["total_headcount"] - committed)
            # Preserve user-entered overrides.
            if existing.get("computed_from") == "manual_entry":
                # Keep manual-entry rows intact.
                continue
            # Bucket H Phase 5 -- presence-locked rows are historical
            # truth (DR submitted with actuals); never overwrite.
            if existing.get("presence_locked"):
                continue
            if existing.get("notes"):
                update_doc["notes"] = existing["notes"]
            await db.crew_capacity.update_one(
                {"id": existing["id"]},
                {"$set": update_doc},
            )
        else:
            update_doc["id"] = _gen_id()
            update_doc["committed_headcount"] = 0
            await db.crew_capacity.insert_one(update_doc.copy())
        written += 1
    return {"written": written, "zeroed": 0, "archived": False}


async def recompute_demand_for_project(
    *, tenant_id: str, project_id: str, horizon_days: int = 90,
) -> dict:
    """Wipe + rebuild crew_demands for a project's next-90-day window
    from current tasks. Used by:
      - schedule import (project's whole demand refreshed)
      - schedule activity create/update/delete (specific date range)
      - project archive (demand zeroes out)
    """
    db = get_db()
    today = date.today()
    horizon = today + timedelta(days=horizon_days - 1)
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "id": 1, "status": 1},
    )
    # Always clear the window first so re-imports don't double-count.
    deleted = await db.crew_demands.delete_many(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "date": {"$gte": today.isoformat(), "$lte": horizon.isoformat()},
        }),
    )
    if proj is None or proj.get("status") == "archived":
        return {"cleared": int(deleted.deleted_count or 0), "added": 0, "archived": True}
    # Walk active tasks for the project with required_trade set.
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "required_trade": {"$nin": [None, ""]},
            "status": {"$ne": "deleted"},
        }),
        {"_id": 0, "id": 1, "required_trade": 1, "required_headcount": 1,
         "required_hours": 1, "planned_start": 1, "planned_finish": 1,
         "is_critical_path": 1},
    ).to_list(length=None)
    added = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    per_tuple: dict[tuple[str, str], dict] = {}
    for t in tasks:
        rtrade = t.get("required_trade")
        rcount = int(t.get("required_headcount") or 0)
        rhours = float(t.get("required_hours") or 0)
        if not rtrade or rcount <= 0:
            continue
        try:
            ps = date.fromisoformat(str(t["planned_start"])[:10])
            pf = date.fromisoformat(str(t["planned_finish"])[:10])
        except Exception:
            continue
        if pf < ps:
            continue
        # Clamp to the recompute window.
        ws = max(ps, today)
        we = min(pf, horizon)
        if we < ws:
            continue
        span = (pf - ps).days + 1
        per_day_hours = rhours / span if span > 0 else rhours
        cp = bool(t.get("is_critical_path"))
        for d in _date_iter(ws, we):
            key = (rtrade, d.isoformat())
            slot = per_tuple.get(key)
            if slot is None:
                slot = {"demanded_headcount": 0, "hours_demanded": 0.0,
                        "source_task_ids": [], "is_critical_path": False}
                per_tuple[key] = slot
            slot["demanded_headcount"] += rcount
            slot["hours_demanded"] += per_day_hours
            slot["source_task_ids"].append(t["id"])
            slot["is_critical_path"] = slot["is_critical_path"] or cp
    docs = []
    for (rtrade, day_iso), slot in per_tuple.items():
        docs.append({
            "id": _gen_id(),
            "tenant_id": tenant_id,
            "project_id": project_id,
            "date": day_iso,
            "trade": rtrade,
            "demanded_headcount": slot["demanded_headcount"],
            "source_task_ids": slot["source_task_ids"],
            "hours_demanded": slot["hours_demanded"],
            "is_critical_path": slot["is_critical_path"],
            "computed_at": now_iso,
        })
    if docs:
        await db.crew_demands.insert_many(docs)
        added = len(docs)
    return {"cleared": int(deleted.deleted_count or 0), "added": added, "archived": False}


async def recompute_demand_for_dates(
    *, tenant_id: str, project_id: str, dates: Iterable[date],
) -> dict:
    """Narrower variant of recompute_demand_for_project: only affects
    the supplied date list. Used by schedule_activity create/update/
    delete hooks where the affected dates are a small subset of the
    project's 90-day horizon."""
    dates_list = sorted({d for d in dates if d is not None})
    if not dates_list:
        return {"cleared": 0, "added": 0}
    # Simplest correct strategy: nuke the affected dates and rebuild
    # from current tasks. Avoids drift bugs from incremental math.
    db = get_db()
    date_strings = [d.isoformat() for d in dates_list]
    deleted = await db.crew_demands.delete_many(
        tenant_filter(tenant_id, {
            "project_id": project_id, "date": {"$in": date_strings},
        }),
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "required_trade": {"$nin": [None, ""]},
            "status": {"$ne": "deleted"},
            "planned_start": {"$lte": dates_list[-1].isoformat()},
            "planned_finish": {"$gte": dates_list[0].isoformat()},
        }),
        {"_id": 0, "id": 1, "required_trade": 1, "required_headcount": 1,
         "required_hours": 1, "planned_start": 1, "planned_finish": 1,
         "is_critical_path": 1},
    ).to_list(length=None)
    per_tuple: dict[tuple[str, str], dict] = {}
    for t in tasks:
        rtrade = t.get("required_trade")
        rcount = int(t.get("required_headcount") or 0)
        rhours = float(t.get("required_hours") or 0)
        if not rtrade or rcount <= 0:
            continue
        try:
            ps = date.fromisoformat(str(t["planned_start"])[:10])
            pf = date.fromisoformat(str(t["planned_finish"])[:10])
        except Exception:
            continue
        span = (pf - ps).days + 1
        per_day_hours = rhours / span if span > 0 else rhours
        cp = bool(t.get("is_critical_path"))
        for d in dates_list:
            if d < ps or d > pf:
                continue
            key = (rtrade, d.isoformat())
            slot = per_tuple.setdefault(key, {
                "demanded_headcount": 0, "hours_demanded": 0.0,
                "source_task_ids": [], "is_critical_path": False,
            })
            slot["demanded_headcount"] += rcount
            slot["hours_demanded"] += per_day_hours
            slot["source_task_ids"].append(t["id"])
            slot["is_critical_path"] = slot["is_critical_path"] or cp
    docs = []
    for (rtrade, day_iso), slot in per_tuple.items():
        docs.append({
            "id": _gen_id(),
            "tenant_id": tenant_id,
            "project_id": project_id,
            "date": day_iso,
            "trade": rtrade,
            "demanded_headcount": slot["demanded_headcount"],
            "source_task_ids": slot["source_task_ids"],
            "hours_demanded": slot["hours_demanded"],
            "is_critical_path": slot["is_critical_path"],
            "computed_at": now_iso,
        })
    added = 0
    if docs:
        await db.crew_demands.insert_many(docs)
        added = len(docs)
    return {"cleared": int(deleted.deleted_count or 0), "added": added}


# =====================================================================
# Reallocation (commit a suggestion)
# =====================================================================

async def commit_reallocation(
    *, tenant_id: str, crew_id: str, from_project_id: Optional[str],
    to_project_id: str, start: date, end: date, headcount: int,
    reason: Optional[str], actor_id: str,
) -> dict:
    """Mark headcount as committed-to-project for the date range.
    Decrements available_headcount in crew_capacity rows.

    Phase 2 doesn't yet track per-project commit ledger (that's
    Phase 4 -- crew_capacity carries a commitments_by_project map).
    For now we just update the running committed_headcount totals;
    the audit log is the source of truth for the "who committed
    whom where" history.
    """
    _validate_range(start, end, max_days=180)
    if headcount <= 0:
        raise ValueError("headcount must be positive")
    db = get_db()
    crew = await db.crews.find_one(
        tenant_filter(tenant_id, {"id": crew_id}),
        {"_id": 0, "id": 1, "trade": 1, "default_headcount": 1, "target_size": 1},
    )
    if not crew:
        raise LookupError(f"crew {crew_id} not found")
    to_proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": to_project_id}),
        {"_id": 0, "id": 1},
    )
    if not to_proj:
        raise LookupError(f"to_project {to_project_id} not found")
    now_iso = datetime.now(timezone.utc).isoformat()
    affected_dates: list[str] = []
    over_commits: list[dict] = []
    for d in _date_iter(start, end):
        row = await db.crew_capacity.find_one(
            tenant_filter(tenant_id, {"crew_id": crew_id, "date": d.isoformat()}),
            {"_id": 0, "id": 1, "total_headcount": 1, "committed_headcount": 1,
             "is_holiday": 1},
        )
        if not row:
            # Holiday or out-of-window day; skip silently.
            continue
        if row.get("is_holiday"):
            continue
        new_committed = int(row.get("committed_headcount") or 0) + headcount
        total = int(row.get("total_headcount") or 0)
        if new_committed > total:
            over_commits.append({"date": d.isoformat(), "total": total,
                                  "would_be_committed": new_committed})
        new_available = max(0, total - new_committed)
        await db.crew_capacity.update_one(
            {"id": row["id"]},
            {"$set": {
                "committed_headcount": new_committed,
                "available_headcount": new_available,
                "computed_at": now_iso,
            }},
        )
        affected_dates.append(d.isoformat())
    return {
        "crew_id": crew_id,
        "to_project_id": to_project_id,
        "from_project_id": from_project_id,
        "headcount": headcount,
        "date_range_start": start.isoformat(),
        "date_range_end": end.isoformat(),
        "affected_dates": affected_dates,
        "over_commits": over_commits,
        "reason": reason,
        "committed_at": now_iso,
        "committed_by": actor_id,
    }


# Internal helper -- local id gen to avoid a circular import with models.
def _gen_id() -> str:
    import uuid
    return uuid.uuid4().hex


# =====================================================================
# Bucket H Phase 5 -- presence-actualized capacity
# =====================================================================
# When the super logs `daily_presence` for a trade on a DR date, what
# actually showed up becomes the source of truth for that day's
# committed_headcount. This service writes the actualization across
# the matching crews and flips computed_from to presence_recorded so
# future recomputes know NOT to overwrite (locked when DR submitted).

async def actualize_capacity_from_presence(
    *, tenant_id: str, project_id: str, presence_date: date,
    trade: str, headcount: int, company_name: Optional[str] = None,
    locked: bool = False, actor_id: Optional[str] = None,
) -> dict:
    """Stamp the matching crew_capacity row for (trade, date) with the
    actual headcount that showed up. Returns {updated_count, picked_
    crew_id, before, after}.

    Pick rule:
      1. Crews with home_project_id == project_id + matching trade
         (+ matching company_name if supplied). Prefer this set.
      2. Crews with no home_project_id (general pool) + matching
         trade + company.
      3. First crew matching trade (+ company).
      4. If none: silently no-op (returns {updated_count: 0}).
    locked=True flips a `presence_locked` flag on the row so the
    Phase 4 recompute_capacity_for_crew skips overwriting it.
    """
    db = get_db()
    base_q = {"tenant_id": tenant_id, "trade": trade,
              "status": {"$ne": "deleted"}}
    if company_name:
        base_q["company_name"] = company_name
    # Try preferred (home project) first.
    crew = await db.crews.find_one(
        {**base_q, "home_project_id": project_id},
        {"_id": 0, "id": 1},
    )
    if not crew:
        crew = await db.crews.find_one(
            {**base_q, "$or": [
                {"home_project_id": None},
                {"home_project_id": {"$exists": False}},
            ]},
            {"_id": 0, "id": 1},
        )
    if not crew:
        crew = await db.crews.find_one(base_q, {"_id": 0, "id": 1})
    if not crew:
        return {"updated_count": 0, "picked_crew_id": None,
                 "reason": "no_matching_crew"}

    now_iso = datetime.now(timezone.utc).isoformat()
    row = await db.crew_capacity.find_one(
        {"tenant_id": tenant_id, "crew_id": crew["id"],
         "date": presence_date.isoformat()},
        {"_id": 0},
    )
    if not row:
        # No capacity row yet -- create one. This can happen if the
        # DR's date is past the 90-day backfill window or the crew
        # was created after the migration ran.
        # Use the crew's default_headcount as total; committed = actual.
        full_crew = await db.crews.find_one(
            {"id": crew["id"]},
            {"_id": 0, "trade": 1, "company_name": 1,
             "default_headcount": 1, "target_size": 1},
        ) or {}
        total = int(full_crew.get("default_headcount")
                     or full_crew.get("target_size") or 0)
        committed = min(max(int(headcount), 0), total) if total else int(headcount)
        new_doc = {
            "id": _gen_id(),
            "tenant_id": tenant_id,
            "crew_id": crew["id"],
            "trade": full_crew.get("trade") or trade,
            "company_name": full_crew.get("company_name"),
            "date": presence_date.isoformat(),
            "total_headcount": max(total, committed),
            "committed_headcount": committed,
            "available_headcount": max(0, max(total, committed) - committed),
            "hours_per_person": 8.0,
            "is_holiday": False,
            "notes": "actualized from presence",
            "computed_at": now_iso,
            "computed_from": "presence_recorded",
            "presence_locked": locked,
        }
        await db.crew_capacity.insert_one(new_doc.copy())
        return {"updated_count": 1, "picked_crew_id": crew["id"],
                "before": None, "after": new_doc}

    # Existing row: update committed + computed_from, recompute available.
    total = int(row.get("total_headcount") or 0)
    committed = min(max(int(headcount), 0), total) if total else int(headcount)
    after_doc = {
        "committed_headcount": committed,
        "available_headcount": max(0, total - committed),
        "computed_at": now_iso,
        "computed_from": "presence_recorded",
        "presence_locked": locked or bool(row.get("presence_locked")),
    }
    await db.crew_capacity.update_one(
        {"id": row["id"]}, {"$set": after_doc},
    )
    return {"updated_count": 1, "picked_crew_id": crew["id"],
            "before": {k: row.get(k) for k in
                        ("committed_headcount", "available_headcount",
                          "computed_from", "presence_locked")},
            "after": after_doc}


async def lock_capacity_for_dr_submit(
    *, tenant_id: str, project_id: str, dr_date: date,
) -> dict:
    """Phase 5 -- DR submit locks the actualized capacity rows for
    that date so subsequent recomputes don't overwrite truth with
    derived values. Returns {locked_count}."""
    db = get_db()
    res = await db.crew_capacity.update_many(
        {"tenant_id": tenant_id,
         "date": dr_date.isoformat(),
         "computed_from": "presence_recorded"},
        {"$set": {"presence_locked": True}},
    )
    return {"locked_count": int(res.modified_count or 0)}
