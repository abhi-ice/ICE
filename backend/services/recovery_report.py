"""Bucket I (2026-05-11) -- Recovery Report Generator service.

Unifies Weekly Rollup + AI Recovery Suggestions + Calibration RAG
into a single Friday-cadence report per Sid's diagram. Generators
are thin wrappers around existing services -- NO duplicate
aggregation logic. Each generator:

  1. Pulls source data from existing collections / endpoints.
  2. Freezes the data_snapshot at generation time (counsel-defensible).
  3. Calls AI producers (best-effort; report degrades gracefully if
     they fail).
  4. Computes variance_summary KPIs.
  5. Inserts a recovery_reports row + fires RECOVERY_REPORT_GENERATED
     audit op.

Render functions produce DOCX / PDF / JSON exhibit bytes; finalize +
archive flip status + audit. Generation is BG-task friendly (heavy
AI calls land in record_call_timing too via the existing photo_ai
hooks; the recovery service has its own thin wrappers).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import time as _time
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Any

from database import get_db, tenant_filter
from models import gen_id, utc_now
from audit import write_audit
from services.storage import put_object, get_object, make_path, record_file

log = logging.getLogger("recovery_report")


# Cache: render output cached for 1 hour, exhibit artifact 24 hours.
RENDER_CACHE_TTL_SECONDS = 3600
EXHIBIT_CACHE_TTL_SECONDS = 24 * 3600
_render_cache: dict[str, tuple[float, bytes]] = {}
_RENDER_CACHE_MAX = 32


def _cache_get(key: str) -> Optional[bytes]:
    cached = _render_cache.get(key)
    if cached and cached[0] > _time.monotonic():
        return cached[1]
    return None


def _cache_put(key: str, blob: bytes, ttl: int = RENDER_CACHE_TTL_SECONDS) -> None:
    _render_cache[key] = (_time.monotonic() + ttl, blob)
    if len(_render_cache) > _RENDER_CACHE_MAX:
        oldest = sorted(_render_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in oldest[: max(1, len(oldest) // 2)]:
            _render_cache.pop(k, None)


def _invalidate_cache(report_id: str) -> None:
    for fmt in ("pdf", "docx", "json"):
        _render_cache.pop(f"{report_id}|{fmt}", None)


# ---------------------------------------------------------------------
# Source-data helpers (wrap existing collections; NO duplicate logic).
# ---------------------------------------------------------------------

async def _gather_weekly_rollup(
    *, tenant_id: str, project_id: str,
    period_start: date, period_end: date,
) -> dict:
    """Mirrors the existing /projects/{id}/weekly-rollup endpoint
    but is callable from a service (which the endpoint isn't). We
    walk the same collections + math so the numbers agree with
    what the Weekly Reports page already shows."""
    db = get_db()
    drs = await db.daily_reports.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "report_date": {
                "$gte": period_start.isoformat(),
                "$lte": period_end.isoformat(),
            },
        }),
        {"_id": 0},
    ).to_list(length=None)
    dr_ids = [d["id"] for d in drs]
    rows: list[dict] = []
    if dr_ids:
        rows = await db.daily_report_activities.find(
            {"daily_report_id": {"$in": dr_ids}},
            {"_id": 0},
        ).to_list(length=None)

    total_hours = sum(float(r.get("hours_worked") or 0) for r in rows)
    delay_minutes = sum(int(r.get("delay_minutes") or 0) for r in rows)
    weather_minutes = sum(int(r.get("weather_impact_minutes") or 0) for r in rows)
    by_trade: dict[str, float] = {}
    for r in rows:
        tr = r.get("trade_code") or "UNK"
        by_trade[tr] = by_trade.get(tr, 0) + float(r.get("hours_worked") or 0)
    completed = sum(1 for r in rows
                    if (r.get("pct_complete_end_of_day") or 0) >= 100)
    total = len(rows)
    ppc = round((completed / total) * 100, 1) if total else 0.0

    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "daily_report_count": len(drs),
        "total_hours_worked": total_hours,
        "delay_minutes": delay_minutes,
        "weather_impact_minutes": weather_minutes,
        "hours_by_trade": by_trade,
        "ppc_pct": ppc,
        "activities_completed": completed,
        "total_planned_activities": total,
    }


async def _gather_delays(
    *, tenant_id: str, project_id: str,
    period_start: date, period_end: date,
) -> list[dict]:
    db = get_db()
    return await db.delays.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "logged_date": {
                "$gte": period_start.isoformat(),
                "$lte": period_end.isoformat(),
            },
            "status": {"$ne": "deleted"},
        }),
        {"_id": 0, "id": 1, "logged_date": 1, "severity": 1,
         "reason_category": 1, "description": 1, "delay_days": 1,
         "owner_attribution": 1, "linked_task_ids": 1,
         "cpm_impact_summary": 1, "status": 1},
    ).sort("logged_date", 1).to_list(length=2000)


async def _gather_resource_gaps(
    *, tenant_id: str, project_id: str,
    period_start: date, period_end: date,
) -> dict:
    """Wraps the resource_leveling dashboard to extract gap count
    over the window. Best-effort -- returns {gaps_count: 0} when the
    leveling service isn't available."""
    try:
        from services.resource_leveling import compute_dashboard
    except Exception:
        return {"gaps_count": 0, "by_severity": {}}
    try:
        dash = await compute_dashboard(
            tenant_id=tenant_id, project_id=project_id,
            date_range_start=period_start, date_range_end=period_end,
        )
        cells = dash.get("cells") or {}
        gaps_count = 0
        by_sev: dict[str, int] = {}
        for trade, by_date in cells.items():
            for d, cell in (by_date or {}).items():
                sev = cell.get("severity") or "none"
                if sev in ("minor", "moderate", "major"):
                    gaps_count += 1
                    by_sev[sev] = by_sev.get(sev, 0) + 1
        return {"gaps_count": gaps_count, "by_severity": by_sev}
    except Exception as exc:
        log.warning("resource gaps gather failed: %s", exc)
        return {"gaps_count": 0, "by_severity": {}}


async def _gather_gating_entities(
    *, tenant_id: str, project_id: str,
    period_start: date, period_end: date,
) -> list[dict]:
    db = get_db()
    return await db.gating_entities.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "$or": [
                {"created_at": {
                    "$gte": period_start.isoformat(),
                    "$lte": period_end.isoformat() + "T23:59:59Z",
                }},
                {"closed_at": {
                    "$gte": period_start.isoformat(),
                    "$lte": period_end.isoformat() + "T23:59:59Z",
                }},
            ],
        }),
        {"_id": 0, "id": 1, "entity_number": 1, "entity_type": 1,
         "status": 1, "is_over_sla": 1, "owner_attribution": 1,
         "submitted_at": 1, "closed_at": 1},
    ).to_list(length=2000)


async def _gather_ai_recovery(
    *, tenant_id: str, project_id: str, delays: list[dict],
) -> tuple[list[dict], bool, Optional[str], float]:
    """Pulls AI-recovery suggestions for delays in the period (the
    existing ai_agents.recovery_suggestions function targets a
    single activity; we walk delay.linked_task_ids and call once
    per top-impact task). Returns (recovery_actions, degraded,
    reason, est_cost_usd).

    Degrades gracefully: if the agent fails, report still
    generates without ai_narrative + recovery_actions[]."""
    try:
        from services import ai_agents
    except Exception as exc:
        return [], True, f"ai_agents import failed: {exc}", 0.0
    actions: list[dict] = []
    est_cost = 0.0
    degraded = False
    reason = None
    # Cap to top 5 delays by impact to bound AI cost. Pick critical-
    # path delays first.
    sorted_delays = sorted(
        delays, key=lambda d: -float(d.get("delay_days") or 0),
    )[:5]
    for d in sorted_delays:
        tids = d.get("linked_task_ids") or []
        if not tids:
            continue
        # Call recovery_suggestions for the first task only -- the
        # per-task suggestions are similar for cascades.
        try:
            r = await ai_agents.recovery_suggestions(
                tenant_id=tenant_id, project_id=project_id,
                target_activity_id=tids[0],
                trigger=f"delay_{d.get('id', '')[:8]}_recovery_report",
            )
            for s in (r.get("suggestions") or []):
                actions.append({
                    "action": (s.get("action") or "")[:500],
                    "owner": s.get("owner"),
                    "due_date": s.get("due_date"),
                    "rationale": (s.get("rationale") or s.get("reasoning") or "")[:2000],
                    "confidence": s.get("confidence"),
                    "source_delay_id": d.get("id"),
                    "source_mitigation_id": s.get("mitigation_id"),
                })
            # Each call costs roughly $0.01 -- rough estimate without
            # parsing the usage payload.
            est_cost += 0.01
        except Exception as exc:
            degraded = True
            reason = f"ai_recovery_suggestions failed: {type(exc).__name__}: {exc}"
            log.warning("ai recovery gather failed for delay %s: %s",
                        d.get("id"), exc)
            break
    return actions, degraded, reason, est_cost


# ---------------------------------------------------------------------
# Generators.
# ---------------------------------------------------------------------

async def _persist_report(
    *, tenant_id: str, project_id: str, report_type: str,
    period_start: Optional[date], period_end: Optional[date],
    data_snapshot: dict, variance_summary: dict,
    recovery_actions: list[dict], ai_narrative: Optional[str],
    is_degraded: bool, degraded_reason: Optional[str],
    est_cost_usd: float,
    actor_id: Optional[str], request_id: Optional[str],
    idempotency_key: Optional[str] = None,
    audit_op_label: Optional[str] = None,
) -> dict:
    db = get_db()
    now = utc_now()
    # Idempotency: same (tenant, project, type, period, key) returns
    # the existing report.
    if idempotency_key:
        existing = await db.recovery_reports.find_one(
            tenant_filter(tenant_id, {
                "project_id": project_id,
                "report_type": report_type,
                "idempotency_key": idempotency_key,
            }),
            {"_id": 0},
        )
        if existing:
            return existing
    doc = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "report_type": report_type,
        "generated_at": now.isoformat(),
        "generated_by": actor_id,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "data_snapshot": data_snapshot,
        "ai_narrative": ai_narrative,
        "recovery_actions": recovery_actions,
        "variance_summary": variance_summary,
        "exhibit_artifacts": [],
        "status": "draft",
        "finalized_by": None, "finalized_at": None,
        "archived_by": None, "archived_at": None,
        "audit_op_label": audit_op_label,
        "is_degraded": is_degraded,
        "degraded_reason": degraded_reason,
        "ai_cost_estimate_usd": round(est_cost_usd, 4),
        "idempotency_key": idempotency_key,
    }
    await db.recovery_reports.insert_one(doc.copy())
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="recovery_reports",
            op="RECOVERY_REPORT_GENERATED",
            record_id=doc["id"], before=None,
            after={
                "report_type": report_type,
                "period_start": doc["period_start"],
                "period_end": doc["period_end"],
                "is_degraded": is_degraded,
                "variance_summary": variance_summary,
                "recovery_action_count": len(recovery_actions),
                "ai_cost_estimate_usd": doc["ai_cost_estimate_usd"],
            },
            actor_id=actor_id,
            request_id=request_id or f"recovery_gen_{doc['id']}",
        )
    except Exception as exc:
        log.warning("audit RECOVERY_REPORT_GENERATED failed: %s", exc)
    return doc


def _compute_variance_summary(
    *, weekly: dict, delays: list[dict], gating: list[dict],
    gaps: dict,
) -> dict:
    """Pulls a compact set of variance KPIs out of the source data.
    The Reports Generator UI surfaces these as the top-of-report
    KPI tiles."""
    delay_days = sum(float(d.get("delay_days") or 0) for d in delays)
    open_gates = sum(1 for g in gating if g.get("status") == "open")
    over_sla = sum(1 for g in gating if g.get("is_over_sla"))
    return {
        "schedule_slip_days": round(delay_days, 1),
        "delay_count": len(delays),
        "open_gating_count": open_gates,
        "gating_over_sla_count": over_sla,
        "ppc_pct": weekly.get("ppc_pct", 0.0),
        "total_hours_worked": weekly.get("total_hours_worked", 0.0),
        "delay_minutes": weekly.get("delay_minutes", 0),
        "weather_minutes": weekly.get("weather_impact_minutes", 0),
        "gaps_count": gaps.get("gaps_count", 0),
        "activities_completed": weekly.get("activities_completed", 0),
        "total_planned_activities": weekly.get("total_planned_activities", 0),
    }


async def generate_weekly_variance(
    *, tenant_id: str, project_id: str,
    period_start: date, period_end: date,
    actor_id: Optional[str] = None, request_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Mon-Sun variance vs. plan. Wraps existing weekly_rollup +
    delays + gating + resource gaps."""
    weekly = await _gather_weekly_rollup(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    delays = await _gather_delays(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    gating = await _gather_gating_entities(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    gaps = await _gather_resource_gaps(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    variance = _compute_variance_summary(
        weekly=weekly, delays=delays, gating=gating, gaps=gaps,
    )
    snapshot = {
        "weekly_rollup": weekly, "delays": delays,
        "gating_entities": gating, "resource_gaps": gaps,
    }
    label = (
        f"Weekly Variance Report "
        f"{period_start.isoformat()}/{period_end.isoformat()}"
    )
    # Weekly variance is mostly numeric -- no AI call needed (AI is
    # for friday_recovery + project_health). is_degraded=False.
    return await _persist_report(
        tenant_id=tenant_id, project_id=project_id,
        report_type="weekly_variance",
        period_start=period_start, period_end=period_end,
        data_snapshot=snapshot, variance_summary=variance,
        recovery_actions=[], ai_narrative=None,
        is_degraded=False, degraded_reason=None, est_cost_usd=0.0,
        actor_id=actor_id, request_id=request_id,
        idempotency_key=idempotency_key, audit_op_label=label,
    )


async def generate_friday_recovery(
    *, tenant_id: str, project_id: str,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    actor_id: Optional[str] = None, request_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Mon-Fri snapshot with AI recovery + calibration RAG."""
    if not period_start or not period_end:
        today = datetime.now(timezone.utc).date()
        # Find current Monday (weekday 0).
        monday = today - timedelta(days=today.weekday())
        friday = monday + timedelta(days=4)
        period_start = period_start or monday
        period_end = period_end or friday
    weekly = await _gather_weekly_rollup(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    delays = await _gather_delays(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    gating = await _gather_gating_entities(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    gaps = await _gather_resource_gaps(
        tenant_id=tenant_id, project_id=project_id,
        period_start=period_start, period_end=period_end,
    )
    recovery_actions, degraded, reason, cost = await _gather_ai_recovery(
        tenant_id=tenant_id, project_id=project_id, delays=delays,
    )
    variance = _compute_variance_summary(
        weekly=weekly, delays=delays, gating=gating, gaps=gaps,
    )
    # Build a short narrative inline (we don't make another LLM call
    # for prose; the recovery_actions already include the AI's
    # rationale per item).
    narrative_lines: list[str] = []
    narrative_lines.append(
        f"Friday Recovery Report for week of {period_start.isoformat()}."
    )
    narrative_lines.append(
        f"PPC: {variance['ppc_pct']}% across "
        f"{weekly.get('daily_report_count', 0)} daily report(s)."
    )
    if variance["delay_count"]:
        narrative_lines.append(
            f"{variance['delay_count']} delay event(s) logged "
            f"totaling {variance['schedule_slip_days']} day(s) of slip."
        )
    if variance["open_gating_count"]:
        narrative_lines.append(
            f"{variance['open_gating_count']} gating entity(ies) open "
            f"({variance['gating_over_sla_count']} over SLA)."
        )
    if variance["gaps_count"]:
        narrative_lines.append(
            f"{variance['gaps_count']} resource gap(s) flagged this week."
        )
    if recovery_actions:
        narrative_lines.append(
            f"AI proposed {len(recovery_actions)} recovery action(s) -- "
            f"review and assign before EOD."
        )
    if degraded:
        narrative_lines.append(
            f"NOTE: AI recovery service was degraded -- "
            f"narrative is summary-only."
        )
    ai_narrative = " ".join(narrative_lines)
    snapshot = {
        "weekly_rollup": weekly, "delays": delays,
        "gating_entities": gating, "resource_gaps": gaps,
        "ai_recovery_call_status": (
            "degraded" if degraded else "ok"
        ),
    }
    label = (
        f"Friday Recovery Report week of {period_start.isoformat()}"
    )
    return await _persist_report(
        tenant_id=tenant_id, project_id=project_id,
        report_type="friday_recovery",
        period_start=period_start, period_end=period_end,
        data_snapshot=snapshot, variance_summary=variance,
        recovery_actions=recovery_actions,
        ai_narrative=ai_narrative,
        is_degraded=degraded, degraded_reason=reason,
        est_cost_usd=cost,
        actor_id=actor_id, request_id=request_id,
        idempotency_key=idempotency_key, audit_op_label=label,
    )


async def generate_daily_variance(
    *, tenant_id: str, project_id: str, target_date: date,
    actor_id: Optional[str] = None, request_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Single-day variance report. Same shape as weekly with period =
    (target_date, target_date)."""
    weekly = await _gather_weekly_rollup(
        tenant_id=tenant_id, project_id=project_id,
        period_start=target_date, period_end=target_date,
    )
    delays = await _gather_delays(
        tenant_id=tenant_id, project_id=project_id,
        period_start=target_date, period_end=target_date,
    )
    gating = await _gather_gating_entities(
        tenant_id=tenant_id, project_id=project_id,
        period_start=target_date, period_end=target_date,
    )
    gaps = await _gather_resource_gaps(
        tenant_id=tenant_id, project_id=project_id,
        period_start=target_date, period_end=target_date,
    )
    variance = _compute_variance_summary(
        weekly=weekly, delays=delays, gating=gating, gaps=gaps,
    )
    snapshot = {
        "weekly_rollup": weekly, "delays": delays,
        "gating_entities": gating, "resource_gaps": gaps,
    }
    label = f"Daily Variance Report {target_date.isoformat()}"
    return await _persist_report(
        tenant_id=tenant_id, project_id=project_id,
        report_type="daily_variance",
        period_start=target_date, period_end=target_date,
        data_snapshot=snapshot, variance_summary=variance,
        recovery_actions=[], ai_narrative=None,
        is_degraded=False, degraded_reason=None, est_cost_usd=0.0,
        actor_id=actor_id, request_id=request_id,
        idempotency_key=idempotency_key, audit_op_label=label,
    )


async def generate_project_health_summary(
    *, tenant_id: str, project_id: str,
    actor_id: Optional[str] = None, request_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Current-state snapshot, no period. Useful for owner-meeting
    one-pagers."""
    db = get_db()
    # Project context.
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0},
    ) or {}
    # Counts.
    open_delays = await db.delays.count_documents(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$nin": ["resolved", "deleted"]},
        }),
    )
    open_qc = await db.qc_issues.count_documents(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$ne": "closed"},
        }),
    )
    open_safety = await db.safety_events.count_documents(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$ne": "closed"},
        }),
    )
    open_rfis = await db.rfis.count_documents(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$nin": ["closed", "answered"]},
        }),
    )
    open_submittals = await db.submittals.count_documents(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$nin": ["approved", "approved_as_noted",
                                  "rejected", "closed"]},
        }),
    )
    sub_breached = await db.submittals.count_documents(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "sla_status": "breached",
        }),
    )
    variance = {
        "open_delays": open_delays, "open_qc_issues": open_qc,
        "open_safety_events": open_safety, "open_rfis": open_rfis,
        "open_submittals": open_submittals,
        "submittals_breached": sub_breached,
        "project_status": proj.get("status"),
        "current_substantial_completion":
            proj.get("current_substantial_completion"),
        "original_substantial_completion":
            proj.get("original_substantial_completion"),
    }
    snapshot = {
        "project": proj, "counts": variance,
        "generated_at": utc_now().isoformat(),
    }
    label = f"Project Health Summary - {proj.get('name') or project_id[:8]}"
    return await _persist_report(
        tenant_id=tenant_id, project_id=project_id,
        report_type="project_health_summary",
        period_start=None, period_end=None,
        data_snapshot=snapshot, variance_summary=variance,
        recovery_actions=[], ai_narrative=None,
        is_degraded=False, degraded_reason=None, est_cost_usd=0.0,
        actor_id=actor_id, request_id=request_id,
        idempotency_key=idempotency_key, audit_op_label=label,
    )


# =====================================================================
# Render functions.
# =====================================================================

def _render_json_bytes(report: dict) -> bytes:
    return json.dumps(report, default=str, indent=2).encode("utf-8")


def _render_docx_bytes(report: dict) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor
    doc = Document()
    doc.add_heading(
        report.get("audit_op_label") or f"Recovery Report {report['id'][:8]}",
        level=0,
    )
    p = doc.add_paragraph()
    p.add_run(
        f"Generated: {report.get('generated_at', '')}"
    ).italic = True

    # Variance KPIs.
    doc.add_heading("Variance Summary", level=1)
    vs = report.get("variance_summary") or {}
    if not vs:
        para = doc.add_paragraph()
        r = para.add_run("No variance data for this period.")
        r.italic = True
        r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    else:
        for k, v in vs.items():
            line = doc.add_paragraph()
            r1 = line.add_run(f"{k.replace('_', ' ').title()}: ")
            r1.bold = True
            line.add_run(f"{v}")

    # AI narrative.
    if report.get("ai_narrative"):
        doc.add_heading("AI Narrative", level=1)
        doc.add_paragraph(report["ai_narrative"])

    # Recovery actions table.
    doc.add_heading("Recovery Actions", level=1)
    actions = report.get("recovery_actions") or []
    if not actions:
        para = doc.add_paragraph()
        r = para.add_run("No recovery actions proposed.")
        r.italic = True
    else:
        t = doc.add_table(rows=1 + len(actions), cols=5)
        t.style = "Light Grid Accent 1"
        hdr = t.rows[0].cells
        hdr[0].text = "Action"
        hdr[1].text = "Owner"
        hdr[2].text = "Due"
        hdr[3].text = "Confidence"
        hdr[4].text = "Rationale"
        # CPM Part G (2026-05-15) -- recovery action due_date rendered
        # MM/DD/YYYY via the shared backend chokepoint.
        from services.date_format import format_date_mmddyyyy
        for i, a in enumerate(actions, start=1):
            cells = t.rows[i].cells
            cells[0].text = str(a.get("action") or "")[:200]
            cells[1].text = str(a.get("owner") or "—")
            cells[2].text = format_date_mmddyyyy(a.get("due_date"))
            cells[3].text = (
                f"{int((a.get('confidence') or 0) * 100)}%"
                if a.get("confidence") is not None else "—"
            )
            cells[4].text = str(a.get("rationale") or "")[:240]

    # Degraded marker.
    if report.get("is_degraded"):
        doc.add_heading("Note", level=2)
        para = doc.add_paragraph()
        r = para.add_run(
            "AI recovery service was degraded during generation. "
            f"Reason: {report.get('degraded_reason') or '(none recorded)'}",
        )
        r.italic = True
        r.font.color.rgb = RGBColor(0xB8, 0x80, 0x00)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _render_pdf_bytes(report: dict) -> bytes:
    """WeasyPrint render. Minimal styling consistent with TIA."""
    import html as _html
    try:
        from weasyprint import HTML
    except Exception as exc:
        log.warning("weasyprint unavailable; falling back to text PDF: %s", exc)
        # Fallback: text bytes wrapped as application/pdf MIME; not a
        # real PDF but downloadable for emergency use.
        text = _render_json_bytes(report).decode("utf-8", errors="replace")
        return text.encode("utf-8")

    vs = report.get("variance_summary") or {}
    actions = report.get("recovery_actions") or []

    rows_kpi = "".join(
        f"<tr><td><b>{_html.escape(str(k).replace('_', ' ').title())}</b></td>"
        f"<td>{_html.escape(str(v))}</td></tr>"
        for k, v in vs.items()
    )

    # CPM Part G (2026-05-15) -- recovery action due_date rendered
    # MM/DD/YYYY in the PDF. period_start / period_end on the cover
    # also go through the same helper below.
    from services.date_format import format_date_mmddyyyy
    rows_actions = "".join(
        "<tr>"
        f"<td>{_html.escape(str(a.get('action') or '')[:200])}</td>"
        f"<td>{_html.escape(str(a.get('owner') or '—'))}</td>"
        f"<td>{_html.escape(format_date_mmddyyyy(a.get('due_date')))}</td>"
        f"<td>{int((a.get('confidence') or 0) * 100)}%</td>"
        f"<td>{_html.escape(str(a.get('rationale') or '')[:240])}</td>"
        "</tr>"
        for a in actions
    )

    degraded_html = ""
    if report.get("is_degraded"):
        degraded_html = (
            "<div class='degraded'><b>AI service degraded.</b> "
            f"Reason: {_html.escape(str(report.get('degraded_reason') or ''))}</div>"
        )

    css = """
      body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #1a2845; }
      h1 { font-size: 18pt; margin-bottom: 4pt; }
      h2 { font-size: 12pt; border-bottom: 1px solid #d97757; padding-bottom: 2pt; margin-top: 14pt; }
      .meta { color: #666; font-size: 9pt; margin-bottom: 12pt; }
      table { border-collapse: collapse; width: 100%; margin: 6pt 0; }
      th, td { border: 1px solid #ccc; padding: 4pt; text-align: left; font-size: 9pt; }
      th { background: #f5f1ea; }
      .degraded { background: #fff5e6; border: 1px solid #d97757; padding: 6pt 10pt; margin: 8pt 0; font-size: 9pt; }
    """
    label = report.get("audit_op_label") or f"Recovery Report {report['id'][:8]}"
    body = f"""
    <html><head><meta charset='utf-8'><style>{css}</style></head>
    <body>
      <h1>{_html.escape(label)}</h1>
      <div class='meta'>
        Generated: {_html.escape(str(report.get('generated_at') or ''))}<br>
        Period: {_html.escape(format_date_mmddyyyy(report.get('period_start')))} to {_html.escape(format_date_mmddyyyy(report.get('period_end')))}<br>
        Status: {_html.escape(str(report.get('status') or ''))}
      </div>
      {degraded_html}
      <h2>Variance Summary</h2>
      <table><tbody>{rows_kpi or '<tr><td colspan=2>No data.</td></tr>'}</tbody></table>
      <h2>AI Narrative</h2>
      <p>{_html.escape(report.get('ai_narrative') or '(no narrative)')}</p>
      <h2>Recovery Actions ({len(actions)})</h2>
      <table>
        <thead><tr><th>Action</th><th>Owner</th><th>Due</th><th>Conf.</th><th>Rationale</th></tr></thead>
        <tbody>{rows_actions or '<tr><td colspan=5>No actions proposed.</td></tr>'}</tbody>
      </table>
    </body></html>
    """
    return HTML(string=body).write_pdf()


async def render_format(
    *, tenant_id: str, report_id: str, fmt: str,
    actor_id: Optional[str] = None, request_id: Optional[str] = None,
) -> tuple[bytes, str, str]:
    """Render + persist exhibit + return bytes + content_type +
    filename. Caches the rendered bytes for 1 hour per format."""
    db = get_db()
    report = await db.recovery_reports.find_one(
        tenant_filter(tenant_id, {"id": report_id}),
        {"_id": 0},
    )
    if not report:
        raise ValueError("not_found")

    cache_key = f"{report_id}|{fmt}"
    cached = _cache_get(cache_key)
    if cached:
        blob = cached
    elif fmt == "pdf":
        blob = _render_pdf_bytes(report)
    elif fmt == "docx":
        blob = _render_docx_bytes(report)
    elif fmt == "json":
        blob = _render_json_bytes(report)
    else:
        raise ValueError(f"unknown_format: {fmt}")
    _cache_put(cache_key, blob)

    sha = hashlib.sha256(blob).hexdigest()
    content_type = {
        "pdf":  "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "json": "application/json",
    }[fmt]
    filename = f"recovery_report_{report_id[:8]}.{fmt}"
    # Persist artifact + update exhibit_artifacts row for this format.
    try:
        storage_path = make_path(tenant_id, "recovery", fmt)
        put_object(storage_path, blob, content_type)
        await record_file(
            tenant_id=tenant_id, storage_path=storage_path,
            original_filename=filename, content_type=content_type,
            size=len(blob), kind="recovery_report_artifact",
            actor_id=actor_id,
        )
        # Upsert into exhibit_artifacts list.
        existing_artifacts = list(report.get("exhibit_artifacts") or [])
        existing_artifacts = [
            a for a in existing_artifacts if a.get("type") != fmt
        ]
        existing_artifacts.append({
            "type": fmt, "storage_key": storage_path,
            "sha256": sha, "size_bytes": len(blob),
            "generated_at": utc_now().isoformat(),
        })
        await db.recovery_reports.update_one(
            {"id": report_id, "tenant_id": tenant_id},
            {"$set": {"exhibit_artifacts": existing_artifacts}},
        )
        try:
            await write_audit(
                tenant_id=tenant_id, table_name="recovery_reports",
                op="RECOVERY_REPORT_EXPORTED",
                record_id=report_id, before=None,
                after={"format": fmt, "size_bytes": len(blob),
                        "sha256": sha[:16]},
                actor_id=actor_id,
                request_id=request_id or f"recovery_export_{report_id}_{fmt}",
            )
        except Exception:
            pass
    except Exception as exc:
        log.warning("recovery exhibit persist failed (still returning bytes): %s", exc)
    return blob, content_type, filename


# =====================================================================
# Finalize / archive.
# =====================================================================

async def finalize_report(
    *, tenant_id: str, report_id: str, actor_id: str,
    request_id: Optional[str] = None,
) -> dict:
    db = get_db()
    row = await db.recovery_reports.find_one(
        tenant_filter(tenant_id, {"id": report_id}),
        {"_id": 0},
    )
    if not row:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    if row.get("status") == "finalized":
        return {"ok": False, "reason": "already_finalized",
                "http_status": 409}
    if row.get("status") == "archived":
        return {"ok": False, "reason": "archived",
                "http_status": 409}
    now = utc_now().isoformat()
    res = await db.recovery_reports.update_one(
        {"id": report_id, "tenant_id": tenant_id, "status": "draft"},
        {"$set": {"status": "finalized",
                   "finalized_by": actor_id,
                   "finalized_at": now}},
    )
    if not res.matched_count:
        return {"ok": False, "reason": "race_or_not_draft",
                "http_status": 409}
    _invalidate_cache(report_id)
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="recovery_reports",
            op="RECOVERY_REPORT_FINALIZED",
            record_id=report_id, before={"status": "draft"},
            after={"status": "finalized", "finalized_by": actor_id},
            actor_id=actor_id,
            request_id=request_id or f"recovery_final_{report_id}",
        )
    except Exception:
        pass
    refreshed = await db.recovery_reports.find_one(
        {"id": report_id, "tenant_id": tenant_id}, {"_id": 0},
    )
    return {"ok": True, "row": refreshed}


async def archive_report(
    *, tenant_id: str, report_id: str, actor_id: str,
    request_id: Optional[str] = None,
) -> dict:
    db = get_db()
    row = await db.recovery_reports.find_one(
        tenant_filter(tenant_id, {"id": report_id}),
        {"_id": 0},
    )
    if not row:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    if row.get("status") == "archived":
        return {"ok": False, "reason": "already_archived",
                "http_status": 409}
    now = utc_now().isoformat()
    await db.recovery_reports.update_one(
        {"id": report_id, "tenant_id": tenant_id},
        {"$set": {"status": "archived",
                   "archived_by": actor_id,
                   "archived_at": now}},
    )
    _invalidate_cache(report_id)
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="recovery_reports",
            op="RECOVERY_REPORT_ARCHIVED",
            record_id=report_id,
            before={"status": row.get("status")},
            after={"status": "archived", "archived_by": actor_id},
            actor_id=actor_id,
            request_id=request_id or f"recovery_arch_{report_id}",
        )
    except Exception:
        pass
    return {"ok": True}


async def delete_report(
    *, tenant_id: str, report_id: str, actor_id: str,
    request_id: Optional[str] = None,
) -> dict:
    """Admin-only soft delete. Sets status='archived' + deleted_at;
    audit chain references the row via RECOVERY_REPORT_DELETED."""
    db = get_db()
    row = await db.recovery_reports.find_one(
        tenant_filter(tenant_id, {"id": report_id}),
        {"_id": 0},
    )
    if not row:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    now = utc_now().isoformat()
    await db.recovery_reports.update_one(
        {"id": report_id, "tenant_id": tenant_id},
        {"$set": {"status": "archived",
                   "deleted_by": actor_id,
                   "deleted_at": now}},
    )
    _invalidate_cache(report_id)
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="recovery_reports",
            op="RECOVERY_REPORT_DELETED",
            record_id=report_id,
            before={"status": row.get("status")},
            after={"deleted_by": actor_id},
            actor_id=actor_id,
            request_id=request_id or f"recovery_del_{report_id}",
        )
    except Exception:
        pass
    return {"ok": True}
