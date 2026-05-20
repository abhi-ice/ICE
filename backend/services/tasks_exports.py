"""Bug #42 + #43 (2026-05-12) -- Tasks + Look Ahead operational exports.

Two exports, both operational (NOT litigation evidence -- no privilege
watermark):

  - Tasks Export: every task in the master schedule for the project.
    XLSX (frozen-header spreadsheet) or PDF (landscape table). Audit
    op TASKS_EXPORTED.

  - Look Ahead Export: 14-day forward-looking window (configurable).
    XLSX or PDF. At-risk rows highlighted. Audit op LOOK_AHEAD_EXPORTED.

Follows the Bucket K exhibit-exports pattern (services/exhibit_exports.py):
  - Service module gathers + renders
  - Tenant-isolated via tenant_filter at every query
  - Cross-tenant calls return None / [] -- router turns into 404
  - 5-minute byte-level cache keyed by (project, fmt, filter_hash)
  - Audit fires at the router layer BEFORE returning the file bytes

PDF renderer uses WeasyPrint to match render_photo_pack_pdf in
exhibit_exports.py. HTML+CSS approach handles landscape orientation
and column wrapping cleanly without the reportlab table API gymnastics.

XLSX renderer uses openpyxl (already in requirements.txt; used by
services/schedule_parser.py for the import side of the same data).
"""
from __future__ import annotations

import hashlib
import html as _html
import io
import json
import logging
import time as _time
from datetime import date, datetime, timezone, timedelta
from typing import Optional, Any

from database import get_db, tenant_filter

log = logging.getLogger("tasks_exports")


# 5-minute byte cache per (project, fmt, filter_hash). Mirrors Bucket K
# pattern. Repeat exports with the same filters reuse the bytes.
EXPORT_CACHE_TTL_SECONDS = 5 * 60
_export_cache: dict[str, tuple[float, bytes, str]] = {}
_EXPORT_CACHE_MAX = 32


def _cache_get(key: str) -> Optional[tuple[bytes, str]]:
    v = _export_cache.get(key)
    if v and v[0] > _time.monotonic():
        return v[1], v[2]
    return None


def _cache_put(key: str, blob: bytes, content_type: str) -> None:
    _export_cache[key] = (
        _time.monotonic() + EXPORT_CACHE_TTL_SECONDS, blob, content_type,
    )
    if len(_export_cache) > _EXPORT_CACHE_MAX:
        oldest = sorted(_export_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in oldest[: max(1, len(oldest) // 2)]:
            _export_cache.pop(k, None)


def _filter_hash(filters: dict) -> str:
    raw = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _today_iso() -> str:
    return date.today().isoformat()


def _project_short_id(project_id: str) -> str:
    """First 8 chars of the UUID, for filename use."""
    return (project_id or "")[:8] if project_id else "unknown"


# =====================================================================
# Tasks Export
# =====================================================================

# Column spec (ORDER MATTERS -- frontend XLSX/PDF render in this order).
# Each entry: (key_in_payload_row, header_label).
#
# 2026-05-18 export-parity: columns rewritten to mirror the Tasks page
# UI 1:1 (frontend/src/pages/Tasks.jsx). Removed task_id (UUID),
# linked_dr_count, linked_event_count, last_synced_at, raw has_blocker
# bool, and required_headcount -- none of those surface in the UI.
# Added: predecessors, work_category, projected_start (Impacted Start),
# slip_wd, duration_days, cp_marker, total_float_days, remaining_float_days,
# blockers (count + summary), gap_severity -- all of which the UI shows.
TASKS_COLUMNS: list[tuple[str, str]] = [
    ("description",          "Description"),
    ("activity_code",        "Code"),
    ("work_category",        "Category"),
    ("resource",             "Resource"),
    ("predecessors",         "Predecessors"),
    ("trade",                "Trade"),
    ("planned_start",        "Plan start"),
    ("planned_finish",       "Plan finish"),
    ("impacted_start",       "Impacted Start"),
    ("impacted_finish",      "Impacted Finish"),
    ("slip_wd",              "Slip (wd)"),
    ("duration_days",        "Dur (d)"),
    ("pct_complete",         "%"),
    ("status",               "Status"),
    ("blockers",             "Blockers"),
    ("cp",                   "CP"),
    ("total_float_days",     "Float (d)"),
    ("remaining_float_days", "Rem Float (d)"),
    ("gap",                  "Gap"),
]


async def gather_tasks_for_export(
    *, tenant_id: str, project_id: str,
) -> dict:
    """Pulls all non-deleted tasks for the project + computes derived
    columns (linked_dr_count, linked_event_count, last_synced_at).

    Returns a dict with:
      - project: {id, name, contract_number}
      - rows: list of dicts shaped per TASKS_COLUMNS keys
      - count, generated_at, filter_hash
      - tenant_id (echoed)

    Cross-tenant / missing project -> returns {"rows": [], ...} with
    empty project so router can 404 if name is None.
    """
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "id": 1, "name": 1, "contract_number": 1},
    )
    if not proj:
        return {
            "project": None, "rows": [], "count": 0,
            "tenant_id": tenant_id, "project_id": project_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filter_hash": _filter_hash({"p": project_id}),
        }

    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$ne": "deleted"},
        }),
        {"_id": 0},
    ).sort([("sequence", 1), ("planned_start", 1),
             ("activity_code", 1)]).to_list(length=10000)

    # 2026-05-18 export-parity: pre-fetch resource-leveling gaps so the
    # Tasks export's "Gap" column mirrors what TaskGapIcon shows in the
    # UI (one gap per task -- most severe wins, CP-tagged variants tie-
    # break above non-CP). Gap collection is keyed by task_id.
    task_ids = [t.get("id") for t in tasks if t.get("id")]
    gap_by_task: dict[str, dict] = {}
    if task_ids:
        # Resource gaps live in the resource_levelling_gaps collection
        # in the live schema. We only need the worst gap per task for
        # the export, matching the UI's single-icon-per-task behavior.
        gaps_cur = db.resource_levelling_gaps.find(
            tenant_filter(tenant_id, {
                "task_id": {"$in": task_ids},
                "status": {"$ne": "resolved"},
            }),
            {"_id": 0, "task_id": 1, "trade": 1, "date": 1,
             "gap": 1, "severity": 1, "is_critical_path": 1},
        )
        _sev_rank = {"major": 3, "moderate": 2, "minor": 1}
        async for g in gaps_cur:
            tid = g.get("task_id")
            if not tid:
                continue
            cur = gap_by_task.get(tid)
            new_rank = (
                (2 if g.get("is_critical_path") else 0)
                + _sev_rank.get(g.get("severity") or "", 0)
            )
            cur_rank = (
                (2 if (cur or {}).get("is_critical_path") else 0)
                + _sev_rank.get((cur or {}).get("severity") or "", 0)
            ) if cur else -1
            if new_rank > cur_rank:
                gap_by_task[tid] = g

    # Shape rows -- 1:1 mapping with Tasks.jsx columns.
    rows: list[dict] = []
    for t in tasks:
        tid = t.get("id")
        # Predecessors -- match frontend formatPredecessors (FS+0 -> bare
        # code, otherwise <code><type>+/-<lag>d). Both legacy bare-string
        # and post-018 {code,type,lag_days} shapes supported.
        preds_raw = t.get("predecessors") or []
        pred_parts: list[str] = []
        for p in (preds_raw if isinstance(preds_raw, list) else []):
            if isinstance(p, str):
                pred_parts.append(p)
                continue
            if not isinstance(p, dict):
                continue
            code = p.get("code") or p.get("activity_code") or ""
            ptype = (p.get("type") or "FS").upper()
            lag = p.get("lag_days") or 0
            if ptype == "FS" and (lag == 0 or lag is None):
                pred_parts.append(str(code))
            else:
                lag_str = (f"+{lag}d" if (lag or 0) > 0
                           else (f"{lag}d" if lag else ""))
                pred_parts.append(f"{code}{ptype}{lag_str}")
        # Slip in working days -- compute via the same helper the
        # backend exports already use via the working_days service so
        # the export number matches what the UI shows (slipDays clamps
        # negatives to 0 -- acceleration is not a slip).
        slip_wd = 0
        pf_iso = _iso_date(t.get("projected_finish_date"))
        plan_fin_iso = _iso_date(t.get("planned_finish"))
        if pf_iso and plan_fin_iso and pf_iso > plan_fin_iso:
            try:
                from services.working_days import working_days_between
                from datetime import date as _date
                slip_wd = working_days_between(
                    _date.fromisoformat(plan_fin_iso),
                    _date.fromisoformat(pf_iso),
                )
            except Exception:
                slip_wd = _date_diff_days(plan_fin_iso, pf_iso)
        # CP marker -- "CP" structural, "CP*" effectively-critical, "" otherwise
        cp_marker = (
            "CP" if t.get("is_critical_path")
            else ("CP*" if t.get("is_currently_critical") else "")
        )
        # Blockers cell -- count + short summary (matches BlockerCell).
        # open_blocker_summary may be a list (one entry per blocker) or
        # a pre-joined string depending on schema age; handle both.
        blocker_count = int(t.get("open_blocker_count") or 0)
        _summary_raw = t.get("open_blocker_summary")
        if isinstance(_summary_raw, list):
            blocker_summary = "; ".join(
                str(s).strip() for s in _summary_raw if s
            )
        else:
            blocker_summary = (str(_summary_raw) if _summary_raw else "").strip()
        if blocker_count and blocker_summary:
            blockers_text = f"{blocker_count} ({blocker_summary})"
        elif blocker_count:
            blockers_text = str(blocker_count)
        else:
            blockers_text = ""
        # Gap cell -- severity + trade + date + gap size when present
        g = gap_by_task.get(tid)
        if g:
            gap_text = (
                f"{g.get('severity') or '?'} ({g.get('trade') or '?'} on "
                f"{_iso_date(g.get('date')) or '?'}, gap "
                f"{g.get('gap') or '?'}"
                f"{', on CP' if g.get('is_critical_path') else ''})"
            )
        else:
            gap_text = ""
        # Resource cell -- mirrors UI's t.resource_names.join(", ").
        resource_names = t.get("resource_names") or []
        resource_text = (
            ", ".join(str(r) for r in resource_names)
            if isinstance(resource_names, list) and resource_names else ""
        )

        rows.append({
            "description":          t.get("description"),
            "activity_code":        t.get("activity_code"),
            "work_category":        t.get("work_category") or "",
            "resource":             resource_text,
            "predecessors":         ", ".join(pred_parts),
            "trade":                t.get("trade_code") or t.get("required_trade") or "",
            "planned_start":        _iso_date(t.get("planned_start")),
            "planned_finish":       _iso_date(t.get("planned_finish")),
            "impacted_start":       _iso_date(t.get("projected_start_date")),
            "impacted_finish":      _iso_date(t.get("projected_finish_date")),
            "slip_wd":              slip_wd,
            "duration_days":        t.get("duration_days"),
            "pct_complete":         float(t.get("pct_complete") or 0),
            "status":               t.get("computed_status") or t.get("status") or "—",
            "blockers":             blockers_text,
            "cp":                   cp_marker,
            "total_float_days":     t.get("total_float_days"),
            "remaining_float_days": t.get("remaining_float_days"),
            "gap":                  gap_text,
        })

    return {
        "project": proj,
        "rows": rows,
        "count": len(rows),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filter_hash": _filter_hash({"p": project_id}),
    }


# =====================================================================
# Look Ahead Export
# =====================================================================

LOOKAHEAD_COLUMNS: list[tuple[str, str]] = [
    # 2026-05-18 export-parity: columns mirror Lookahead.jsx list view 1:1.
    # Removed task_id (UUID), days_until_start, at_risk_flag,
    # risk_reasons, required_headcount, assigned_crew, predecessor_status,
    # has_blocker -- none of those appear in the Lookahead UI list view.
    ("activity_code",        "Code"),
    ("description",          "Description"),
    ("trade",                "Trade"),
    ("planned_start",        "Plan start"),
    ("planned_finish",       "Plan finish"),
    ("impacted_start",       "Impacted Start"),
    ("impacted_finish",      "Impacted Finish"),
    ("slip_wd",              "Slip (wd)"),
    ("remaining_float_days", "Rem Float"),
    ("pct_complete",         "%"),
    ("status",               "Status"),
]


async def gather_lookahead_for_export(
    *, tenant_id: str, project_id: str, days: int = 14,
    start_date: Optional[date] = None,
) -> dict:
    """Pulls tasks in the look-ahead window + derives:
      - days_until_start
      - at_risk_flag (true if blocker / behind / projected slip)
      - risk_reasons (comma-joined)
      - assigned_crew (crew name lookup from current_crew_id then crew_id)
      - predecessor_status ("all_complete" / "X of Y complete" / "blocked_by_<code>")

    Window: [start_date, start_date + days]. Default start = today.
    """
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "id": 1, "name": 1, "contract_number": 1},
    )
    if not proj:
        return {
            "project": None, "rows": [], "count": 0, "at_risk_count": 0,
            "tenant_id": tenant_id, "project_id": project_id,
            "window_start": (start_date or date.today()).isoformat(),
            "window_end": ((start_date or date.today())
                            + timedelta(days=days)).isoformat(),
            "days": days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filter_hash": _filter_hash({"p": project_id, "d": days}),
        }

    today_d = start_date or date.today()
    end_d = today_d + timedelta(days=days)
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$ne": "deleted"},
            "$or": [
                {"planned_start": {"$gte": today_d.isoformat(),
                                    "$lte": end_d.isoformat()}},
                {"planned_finish": {"$gte": today_d.isoformat(),
                                     "$lte": end_d.isoformat()}},
                {"$and": [
                    {"planned_start": {"$lte": today_d.isoformat()}},
                    {"planned_finish": {"$gte": end_d.isoformat()}},
                ]},
            ],
        }),
        {"_id": 0},
    ).sort("planned_start", 1).to_list(length=500)

    # Build the activity_code -> status map (used for predecessor
    # status lookups). Pull all project tasks once.
    all_project_tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id}),
        {"_id": 0, "activity_code": 1, "computed_status": 1, "status": 1,
         "has_open_blocker": 1, "pct_complete": 1},
    ).to_list(length=10000)
    status_by_code: dict[str, dict] = {}
    for t in all_project_tasks:
        code = t.get("activity_code")
        if code:
            status_by_code[code] = {
                "computed_status": t.get("computed_status"),
                "status": t.get("status"),
                "has_blocker": bool(t.get("has_open_blocker")),
                "pct_complete": float(t.get("pct_complete") or 0),
            }

    # Crew name lookup. Resolve task.current_crew_id (most recent
    # daily_presence pick) before falling back to task.crew_id (the
    # baseline assignment from the schedule).
    crew_ids = set()
    for t in tasks:
        if t.get("current_crew_id"):
            crew_ids.add(t["current_crew_id"])
        if t.get("crew_id"):
            crew_ids.add(t["crew_id"])
    crew_by_id: dict[str, str] = {}
    if crew_ids:
        crews_cur = db.crews.find(
            tenant_filter(tenant_id, {"id": {"$in": list(crew_ids)}}),
            {"_id": 0, "id": 1, "name": 1, "crew_code": 1, "trade": 1},
        )
        async for c in crews_cur:
            label = c.get("name") or c.get("crew_code") or c.get("id")
            if c.get("trade"):
                label = f"{label} ({c['trade']})"
            crew_by_id[c["id"]] = label

    rows: list[dict] = []
    at_risk_count = 0
    for t in tasks:
        ps_iso = _iso_date(t.get("planned_start"))
        ps_d = _parse_iso_date(ps_iso)
        days_until_start = (
            (ps_d - today_d).days if ps_d else None
        )
        # Risk inference. Same vocabulary as the Lookahead frontend
        # (Lookahead.jsx atRisk computation): blocker, behind status,
        # or projected finish past planned finish.
        risk_reasons: list[str] = []
        cs = t.get("computed_status") or "on_track"
        blocker = bool(t.get("has_open_blocker"))
        if blocker:
            risk_reasons.append("has open blocker")
        if cs == "blocked":
            risk_reasons.append("computed_status=blocked")
        elif cs == "behind":
            risk_reasons.append("computed_status=behind")
        pf = t.get("projected_finish_date")
        pf_iso = _iso_date(pf)
        plan_fin = _iso_date(t.get("planned_finish"))
        if pf_iso and plan_fin and pf_iso > plan_fin:
            slip = _date_diff_days(plan_fin, pf_iso)
            risk_reasons.append(f"projected slip +{slip}d")
        # Predecessor status -- "all_complete" if every predecessor is
        # complete; else "X of Y complete"; else "blocked_by_<code>"
        # if a predecessor is itself blocked.
        preds = t.get("predecessors") or []
        if not isinstance(preds, list):
            preds = []
        if not preds:
            pred_status = "no_predecessors"
        else:
            blocked_preds: list[str] = []
            complete_count = 0
            for p in preds:
                # Predecessors may be strings (activity_code) or dicts
                # (legacy structured form).
                pcode = p if isinstance(p, str) else (
                    p.get("activity_code") if isinstance(p, dict) else None
                )
                if not pcode:
                    continue
                ps = status_by_code.get(pcode)
                if not ps:
                    continue
                if (ps.get("computed_status") == "blocked"
                        or ps.get("has_blocker")):
                    blocked_preds.append(pcode)
                if (ps.get("pct_complete") >= 100
                        or ps.get("status") == "complete"
                        or ps.get("computed_status") == "complete"):
                    complete_count += 1
            if blocked_preds:
                pred_status = f"blocked_by_{blocked_preds[0]}"
                risk_reasons.append(f"predecessor {blocked_preds[0]} blocked")
            elif complete_count == len(preds):
                pred_status = "all_complete"
            else:
                pred_status = f"{complete_count} of {len(preds)} complete"
                if complete_count < len(preds):
                    risk_reasons.append(
                        f"{len(preds) - complete_count} predecessor(s) incomplete",
                    )

        # Days-until-start gate -- if the task starts in <3 days but
        # predecessors aren't all complete, flag it.
        if (days_until_start is not None and 0 <= days_until_start <= 3
                and pred_status != "all_complete"
                and pred_status != "no_predecessors"):
            risk_reasons.append(
                f"starts in {days_until_start}d but {pred_status}",
            )

        at_risk = bool(risk_reasons)
        if at_risk:
            at_risk_count += 1

        # 2026-05-18 export-parity: surface the same status pill the
        # Lookahead.jsx list view shows -- precedence blocked > behind
        # > at-risk-from-projection > computed_status (default).
        if blocker or cs == "blocked":
            status_label = "blocked"
        elif cs == "behind":
            status_label = "behind"
        elif cs == "complete":
            status_label = "complete"
        elif pf_iso and plan_fin and pf_iso > plan_fin:
            status_label = "at risk"
        else:
            status_label = (cs or "on_track").replace("_", " ")

        # Slip in working days -- matches the UI's slipDays(planned_finish,
        # projected_finish_date) so the exported value tracks the on-screen number.
        slip_wd = 0
        if pf_iso and plan_fin and pf_iso > plan_fin:
            try:
                from services.working_days import working_days_between
                from datetime import date as _date
                slip_wd = working_days_between(
                    _date.fromisoformat(plan_fin),
                    _date.fromisoformat(pf_iso),
                )
            except Exception:
                slip_wd = _date_diff_days(plan_fin, pf_iso)

        rows.append({
            "activity_code":        t.get("activity_code"),
            "description":          t.get("description"),
            "trade":                t.get("trade_code") or t.get("required_trade") or "",
            "planned_start":        ps_iso,
            "planned_finish":       plan_fin,
            "impacted_start":       _iso_date(t.get("projected_start_date")),
            "impacted_finish":      pf_iso,
            "slip_wd":              slip_wd,
            "remaining_float_days": t.get("remaining_float_days"),
            "pct_complete":         float(t.get("pct_complete") or 0),
            "status":               status_label,
            # at_risk_flag stays on the dict (not in LOOKAHEAD_COLUMNS)
            # so _render_xlsx / _render_pdf can still tint at-risk rows
            # via the at_risk_key="at_risk_flag" pass-through.
            "at_risk_flag":         at_risk,
        })

    return {
        "project": proj,
        "rows": rows,
        "count": len(rows),
        "at_risk_count": at_risk_count,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "window_start": today_d.isoformat(),
        "window_end": end_d.isoformat(),
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filter_hash": _filter_hash({
            "p": project_id, "d": days, "s": today_d.isoformat(),
        }),
    }


# =====================================================================
# XLSX renderers (openpyxl)
# =====================================================================

def render_tasks_xlsx(payload: dict) -> bytes:
    return _render_xlsx(
        rows=payload.get("rows") or [],
        columns=TASKS_COLUMNS,
        sheet_title="Tasks",
        header_subtitle=(
            f"Project: {(payload.get('project') or {}).get('name') or '—'} | "
            f"Generated: {payload.get('generated_at', '')} | "
            f"Tasks: {payload.get('count', 0)}"
        ),
    )


def render_lookahead_xlsx(payload: dict) -> bytes:
    return _render_xlsx(
        rows=payload.get("rows") or [],
        columns=LOOKAHEAD_COLUMNS,
        sheet_title="Look-ahead",
        header_subtitle=(
            f"Project: {(payload.get('project') or {}).get('name') or '—'} | "
            f"Look-ahead: {payload.get('window_start')} to {payload.get('window_end')} | "
            f"Tasks: {payload.get('count', 0)} | "
            f"At-risk: {payload.get('at_risk_count', 0)}"
        ),
        at_risk_key="at_risk_flag",
    )


def _render_xlsx(
    *, rows: list[dict], columns: list[tuple[str, str]],
    sheet_title: str, header_subtitle: str,
    at_risk_key: Optional[str] = None,
) -> bytes:
    """Shared XLSX renderer. Frozen header, bold header row, auto-fit
    columns. When at_risk_key is set, rows with truthy values in that
    column are tinted light red (consistent with the lookahead Gantt
    at-risk styling on the frontend)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]   # XLSX sheet title 31-char cap

    # Subtitle row (merged across all columns).
    ws.cell(row=1, column=1, value=header_subtitle)
    ws.merge_cells(start_row=1, start_column=1,
                    end_row=1, end_column=len(columns))
    ws.cell(row=1, column=1).font = Font(italic=True, color="555555")
    ws.cell(row=1, column=1).alignment = Alignment(
        horizontal="left", vertical="center",
    )

    # Header row at row 2.
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(
        "solid", start_color="1A2845", end_color="1A2845",
    )
    for ci, (_, label) in enumerate(columns, start=1):
        cell = ws.cell(row=2, column=ci, value=label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left", vertical="center")

    # Data rows from row 3.
    at_risk_fill = PatternFill(
        "solid", start_color="FEE2E2", end_color="FEE2E2",
    )
    for ri, row in enumerate(rows, start=3):
        flagged = (
            at_risk_key and bool(row.get(at_risk_key))
        )
        for ci, (key, _) in enumerate(columns, start=1):
            val = row.get(key)
            # Coerce None to empty string for cell display; booleans
            # render as TRUE / FALSE which is fine.
            if val is None:
                val = ""
            cell = ws.cell(row=ri, column=ci, value=val)
            if flagged:
                cell.fill = at_risk_fill

    # Freeze header: row 1 (subtitle) + row 2 (header) frozen.
    ws.freeze_panes = "A3"

    # Auto-fit-ish column widths: cap at 60. openpyxl doesn't support
    # true auto-fit; we compute max string length per column from
    # header + sampled rows and clamp.
    for ci, (key, label) in enumerate(columns, start=1):
        longest = len(str(label))
        # Sample up to 200 rows to avoid O(N) over huge exports.
        for row in rows[:200]:
            v = row.get(key)
            if v is None:
                continue
            longest = max(longest, min(80, len(str(v))))
        ws.column_dimensions[get_column_letter(ci)].width = (
            min(60, max(10, longest + 2))
        )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =====================================================================
# PDF renderers (WeasyPrint)
# =====================================================================

def render_tasks_pdf(payload: dict) -> bytes:
    proj = payload.get("project") or {}
    cover = {
        "title":       "Tasks — Master Schedule",
        "subtitle":    "Generated for operational planning",
        "project_name": proj.get("name") or "—",
        "contract":    proj.get("contract_number") or "—",
        "generated_at": payload.get("generated_at", ""),
        "count_label": f"Total tasks: {payload.get('count', 0)}",
        "extra_label": None,
    }
    return _render_pdf(
        rows=payload.get("rows") or [],
        columns=TASKS_COLUMNS, cover=cover,
        project_name=cover["project_name"],
    )


def render_lookahead_pdf(payload: dict) -> bytes:
    # CPM Part G (2026-05-15) -- window_start / window_end on the
    # cover render MM/DD/YYYY via the shared backend chokepoint.
    # generated_at is left as the existing ISO datetime; the cover
    # label semantic ("Generated: <full timestamp>") expects time.
    from services.date_format import format_date_mmddyyyy
    proj = payload.get("project") or {}
    cover = {
        "title":       "Look-ahead — Forward-looking window",
        "subtitle":    "Generated for operational planning",
        "project_name": proj.get("name") or "—",
        "contract":    proj.get("contract_number") or "—",
        "generated_at": payload.get("generated_at", ""),
        "count_label": f"Tasks in window: {payload.get('count', 0)}",
        "extra_label": (
            f"Look-ahead: {format_date_mmddyyyy(payload.get('window_start'))} to "
            f"{format_date_mmddyyyy(payload.get('window_end'))} | At-risk: "
            f"{payload.get('at_risk_count', 0)}"
        ),
    }
    return _render_pdf(
        rows=payload.get("rows") or [],
        columns=LOOKAHEAD_COLUMNS, cover=cover,
        project_name=cover["project_name"],
        at_risk_key="at_risk_flag",
    )


def _render_pdf(
    *, rows: list[dict], columns: list[tuple[str, str]],
    cover: dict, project_name: str,
    at_risk_key: Optional[str] = None,
) -> bytes:
    """Shared PDF renderer. Landscape orientation; cover page +
    paginated table; page footer with N of M + project name.
    Falls back to a minimal text bytes blob if WeasyPrint is
    unavailable so the endpoint still returns something usable."""
    try:
        from weasyprint import HTML, CSS
    except Exception as exc:
        log.warning(
            "weasyprint unavailable; PDF export falls back to plain text: %s",
            exc,
        )
        # Plain-text fallback so the route still returns a valid
        # downloadable file rather than 500ing.
        lines: list[str] = [
            cover["title"], cover["subtitle"], "",
            f"Project: {project_name}",
            f"Contract: {cover['contract']}",
            f"Generated: {cover['generated_at']}",
            cover["count_label"],
        ]
        if cover.get("extra_label"):
            lines.append(cover["extra_label"])
        lines.append("")
        lines.append(" | ".join(label for _, label in columns))
        for r in rows[:1000]:
            lines.append(" | ".join(
                str(r.get(key) or "") for key, _ in columns
            ))
        return ("\n".join(lines)).encode("utf-8")

    # Build the HTML.
    head_cells = "".join(
        f"<th>{_html.escape(str(label))}</th>"
        for _, label in columns
    )
    body_rows = ""
    for r in rows:
        flagged = (
            at_risk_key and bool(r.get(at_risk_key))
        )
        cls = " class='at-risk'" if flagged else ""
        # CPM Part G -- pass the column key so date cells render
        # MM/DD/YYYY (see _PDF_DATE_COLUMN_KEYS).
        cells = "".join(
            f"<td>{_format_cell_html(r.get(key), key)}</td>"
            for key, _ in columns
        )
        body_rows += f"<tr{cls}>{cells}</tr>"

    extra_html = ""
    if cover.get("extra_label"):
        extra_html = (
            f"<div class='cover-extra'>"
            f"{_html.escape(str(cover['extra_label']))}</div>"
        )
    cover_html = f"""
      <section class='cover'>
        <h1>{_html.escape(cover['title'])}</h1>
        <div class='cover-sub'>{_html.escape(cover['subtitle'])}</div>
        <table class='cover-meta'>
          <tr><th>Project</th><td>{_html.escape(project_name)}</td></tr>
          <tr><th>Contract</th><td>{_html.escape(str(cover['contract']))}</td></tr>
          <tr><th>Generated</th><td>{_html.escape(str(cover['generated_at']))}</td></tr>
          <tr><th>Total</th><td>{_html.escape(cover['count_label'])}</td></tr>
        </table>
        {extra_html}
      </section>
    """

    full_html = f"""
    <!DOCTYPE html>
    <html><head><meta charset='utf-8'><title>Tasks Export</title></head>
    <body>
      {cover_html}
      <section class='content'>
        <table class='data-table'>
          <thead><tr>{head_cells}</tr></thead>
          <tbody>{body_rows}</tbody>
        </table>
      </section>
    </body></html>
    """

    css = f"""
      @page {{
        size: A4 landscape;
        margin: 0.4in 0.4in 0.6in 0.4in;
        @bottom-left {{
          content: "{_css_escape(project_name)}";
          font-size: 8pt;
          color: #555;
        }}
        @bottom-right {{
          content: "Page " counter(page) " of " counter(pages);
          font-size: 8pt;
          color: #555;
        }}
      }}
      body {{
        font-family: Helvetica, Arial, sans-serif;
        color: #1a2845;
        font-size: 8pt;
      }}
      section.cover {{
        page-break-after: always;
        padding: 2in 0.5in 0 0.5in;
      }}
      section.cover h1 {{
        font-size: 24pt;
        margin: 0 0 0.4em 0;
      }}
      .cover-sub {{
        font-size: 11pt;
        color: #555;
        margin-bottom: 1.5em;
        font-style: italic;
      }}
      table.cover-meta {{
        border-collapse: collapse;
        margin-top: 1em;
      }}
      table.cover-meta th {{
        text-align: left;
        padding: 4px 16px 4px 0;
        color: #1a2845;
        font-weight: 600;
        vertical-align: top;
      }}
      table.cover-meta td {{
        padding: 4px 0;
      }}
      .cover-extra {{
        margin-top: 1em;
        padding: 8px 12px;
        background: #f5f1ea;
        border-left: 3px solid #d97757;
        font-size: 10pt;
      }}
      section.content table.data-table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 7.5pt;
      }}
      table.data-table thead {{
        display: table-header-group;
      }}
      table.data-table thead th {{
        background: #1a2845;
        color: white;
        padding: 4px 6px;
        text-align: left;
        font-weight: 600;
        font-size: 7.5pt;
      }}
      table.data-table tbody td {{
        padding: 3px 6px;
        border-bottom: 1px solid #e5e7eb;
        vertical-align: top;
        word-break: break-word;
      }}
      table.data-table tbody tr {{
        page-break-inside: avoid;
      }}
      table.data-table tbody tr.at-risk td {{
        background: #fee2e2;
      }}
    """

    return HTML(string=full_html).write_pdf(stylesheets=[CSS(string=css)])


# =====================================================================
# Top-level entry points
# =====================================================================

async def render_tasks_export(
    *, tenant_id: str, project_id: str, fmt: str,
) -> tuple[bytes, str, str, int]:
    """Returns (bytes, content_type, filename, task_count). Caches
    5min per (project, fmt). Raises ValueError on unknown format.
    Returns (b'', '', '', 0) when project is missing/cross-tenant."""
    if fmt not in ("xlsx", "pdf"):
        raise ValueError(f"unknown format: {fmt!r}")
    payload = await gather_tasks_for_export(
        tenant_id=tenant_id, project_id=project_id,
    )
    if not payload.get("project"):
        return b"", "", "", 0
    key = f"tasks|{project_id}|{payload['filter_hash']}|{fmt}"
    cached = _cache_get(key)
    short = _project_short_id(project_id)
    ymd = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"tasks_{short}_{ymd}.{fmt}"
    if cached:
        return cached[0], cached[1], filename, int(payload.get("count", 0))
    if fmt == "xlsx":
        blob = render_tasks_xlsx(payload)
        ct = ("application/vnd.openxmlformats-officedocument."
              "spreadsheetml.sheet")
    else:
        blob = render_tasks_pdf(payload)
        ct = "application/pdf"
    _cache_put(key, blob, ct)
    return blob, ct, filename, int(payload.get("count", 0))


async def render_lookahead_export(
    *, tenant_id: str, project_id: str, fmt: str, days: int = 14,
) -> tuple[bytes, str, str, dict]:
    """Returns (bytes, content_type, filename, meta) where meta has
    {count, at_risk_count, window_start, window_end}. Caches 5min per
    (project, fmt, days). (b'', '', '', {}) when project is missing/
    cross-tenant."""
    if fmt not in ("xlsx", "pdf"):
        raise ValueError(f"unknown format: {fmt!r}")
    payload = await gather_lookahead_for_export(
        tenant_id=tenant_id, project_id=project_id, days=days,
    )
    if not payload.get("project"):
        return b"", "", "", {}
    key = f"lookahead|{project_id}|{payload['filter_hash']}|{fmt}"
    cached = _cache_get(key)
    short = _project_short_id(project_id)
    ymd = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"lookahead_{short}_{ymd}.{fmt}"
    meta = {
        "count": int(payload.get("count", 0)),
        "at_risk_count": int(payload.get("at_risk_count", 0)),
        "window_start": payload.get("window_start"),
        "window_end": payload.get("window_end"),
        "days": int(payload.get("days", days)),
    }
    if cached:
        return cached[0], cached[1], filename, meta
    if fmt == "xlsx":
        blob = render_lookahead_xlsx(payload)
        ct = ("application/vnd.openxmlformats-officedocument."
              "spreadsheetml.sheet")
    else:
        blob = render_lookahead_pdf(payload)
        ct = "application/pdf"
    _cache_put(key, blob, ct)
    return blob, ct, filename, meta


# =====================================================================
# Helpers
# =====================================================================

def _iso_date(v: Any) -> Optional[str]:
    """Coerce date/datetime/string to ISO YYYY-MM-DD or None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v)
    return s[:10] if s else None


def _iso_datetime(v: Any) -> Optional[str]:
    """Pass through ISO datetime strings; coerce datetime objects."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _parse_iso_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _date_diff_days(a_iso: str, b_iso: str) -> int:
    a = _parse_iso_date(a_iso)
    b = _parse_iso_date(b_iso)
    if not a or not b:
        return 0
    return (b - a).days


# CPM Part G (2026-05-15) -- column keys whose cell values are pure
# ISO YYYY-MM-DD dates. The PDF cell renderer converts these to
# MM/DD/YYYY via services.date_format.format_date_mmddyyyy; CSV / XLSX
# cells stay ISO (data-interchange semantic). Datetime columns like
# last_synced_at are intentionally NOT in this set -- they need full
# date+time and the existing _iso_datetime() rendering handles them.
_PDF_DATE_COLUMN_KEYS = frozenset({
    "planned_start",
    "planned_finish",
    "projected_finish",   # legacy key, kept for backward compat
    "impacted_start",
    "impacted_finish",
})


def _format_cell_html(v: Any, key: str | None = None) -> str:
    """Render a single cell value for the PDF table. Booleans
    render as ✓ / —, None as —, lists as comma-joined.

    CPM Part G (2026-05-15) -- when `key` is one of
    _PDF_DATE_COLUMN_KEYS, ISO date string values are converted to
    MM/DD/YYYY via the shared backend chokepoint
    services.date_format.format_date_mmddyyyy. Callers pre-Part-G
    invoked this helper with one argument (the value only); the
    `key` kwarg is optional so the older call-shape still works.
    """
    if v is None or v == "":
        return "<span style='color:#aaa'>—</span>"
    if isinstance(v, bool):
        return ("<span style='color:#b91c1c;font-weight:600'>✓</span>"
                if v else "<span style='color:#aaa'>—</span>")
    if isinstance(v, list):
        return _html.escape(", ".join(str(x) for x in v[:10]))
    # Part G -- per-column date formatting.
    if key and key in _PDF_DATE_COLUMN_KEYS:
        from services.date_format import format_date_mmddyyyy
        return _html.escape(format_date_mmddyyyy(v))
    return _html.escape(str(v))


def _css_escape(s: str) -> str:
    """Escape characters that would break a CSS content: string."""
    return (s or "").replace("\\", "\\\\").replace('"', "'")
