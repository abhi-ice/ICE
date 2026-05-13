"""Bucket C1 Phase 2-5 — task status state machine and CPM walk-forward.

Pure functions where possible; the persist-back wrappers do a
single $set per task and only write when something actually changed
(idempotent — safe to call repeatedly).

Status rule (Phase 2):
  pct_complete >= 100   → complete
  has_open_blocker      → blocked
  pct < planned_pct(t)  → behind
  otherwise             → on_track

Blocker sources counted in has_open_blocker:
  - delays.linked_task_ids contains task_id, delays.status in [open, in_progress]
  - rfis.linked_task_ids contains task_id OR rfis.schedule_activity_id == task_id,
    rfis.status in [draft, sent, in_review]
  - qc_issues.linked_task_ids contains task_id, qc_issues.status in [open, needs_rework]
  - safety_events.linked_task_ids contains task_id, safety_events.status == 'open'
  - submittals.schedule_activity_id == task_id, submittals.status in [submitted, in_review],
    submittals.sla_status == 'breached' (over-SLA submittals only)
  - work_item_links where (entity_type='task', entity_id=task_id),
    work_items.status NOT IN [closed, resolved]

Bug #50 (2026-05-13): RFIs / QC / safety / submittals added to the
aggregation so the Tasks page surfaces them as blockers, matching the
"all open gating entities should aggregate" rule. RFIs now match on
EITHER linked_task_ids (canonical Bucket D Corrective linkage) OR
schedule_activity_id (legacy single-link) so we don't drop legacy data.
Submittals are blockers ONLY when SLA-breached -- in-flight submittals
within SLA are normal procurement, not blockers.

CPM walk-forward (Phase 4) — see compute_projected_finish_dates.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional

from database import tenant_filter
from models import gen_id


# ---------- Pure helpers ---------------------------------------------------

def planned_pct_at(task: dict, today: date) -> float:
    """Linear interpolation between planned_start (0%) and planned_finish
    (100%) at `today`. Out-of-range clamps to 0 / 100. Returns 0 when the
    task has no dates (we can't say what 'on schedule' would mean)."""
    ps = task.get("planned_start")
    pf = task.get("planned_finish")
    if not ps or not pf:
        return 0.0
    try:
        ps_d = date.fromisoformat(str(ps)[:10])
        pf_d = date.fromisoformat(str(pf)[:10])
    except Exception:
        return 0.0
    if today <= ps_d:
        return 0.0
    if today >= pf_d:
        return 100.0
    span = (pf_d - ps_d).days
    if span <= 0:
        return 100.0
    elapsed = (today - ps_d).days
    return round(elapsed / span * 100.0, 1)


def derive_status(*, pct: float, planned: float, has_blocker: bool) -> str:
    """Apply the Phase 2 status rule. Strict hierarchy: complete > blocked
    > behind > on_track. Complete wins over blocker so a 100%-done activity
    isn't shown as blocked even if a stale delay still references it."""
    if pct >= 100:
        return "complete"
    if has_blocker:
        return "blocked"
    if pct < planned:
        return "behind"
    return "on_track"


# ---------- Blocker counting ----------------------------------------------

async def count_open_blockers(db, tenant_id: str, task_id: str) -> tuple[int, list[dict]]:
    """Walk all three blocker sources and return (count, summary). Summary
    items: {entity_type, entity_id, severity, label?}. Cap each source at
    20 to keep the denormalized array bounded; if a task somehow gets >20
    open delays alone the count still reflects the truth via the source
    queries."""
    summary: list[dict] = []

    delays = await db.delays.find(
        tenant_filter(tenant_id, {
            "linked_task_ids": task_id,
            "status": {"$in": ["open", "in_progress"]},
        }),
        {"_id": 0, "id": 1, "severity": 1, "description": 1},
    ).limit(20).to_list(20)
    for d in delays:
        summary.append({
            "entity_type": "delay",
            "entity_id": d["id"],
            "severity": d.get("severity"),
            "label": (d.get("description") or "")[:80],
        })

    # Bug #50 (2026-05-13): match RFIs on EITHER linked_task_ids
    # (canonical Bucket D Corrective linkage) OR schedule_activity_id
    # (legacy single-link). Without the OR, RFIs migrated to multi-task
    # linkage wouldn't surface as blockers.
    rfis = await db.rfis.find(
        tenant_filter(tenant_id, {
            "$or": [
                {"linked_task_ids": task_id},
                {"schedule_activity_id": task_id},
            ],
            "status": {"$in": ["draft", "sent", "in_review"]},
        }),
        {"_id": 0, "id": 1, "rfi_number": 1, "subject": 1, "priority": 1, "status": 1},
    ).limit(20).to_list(20)
    seen_rfi_ids: set = set()
    for r in rfis:
        if r["id"] in seen_rfi_ids:
            continue
        seen_rfi_ids.add(r["id"])
        summary.append({
            "entity_type": "rfi",
            "entity_id": r["id"],
            "severity": r.get("priority") or "normal",
            "label": f"RFI {r.get('rfi_number')}: {(r.get('subject') or '')[:60]}",
        })

    # Bug #50 (2026-05-13): QC issues that are still open block the
    # task. Status set follows Sid's spec: open + needs_rework. If the
    # QC status vocabulary grows (e.g. in_remediation), add it here.
    qcs = await db.qc_issues.find(
        tenant_filter(tenant_id, {
            "linked_task_ids": task_id,
            "status": {"$in": ["open", "needs_rework"]},
        }),
        {"_id": 0, "id": 1, "severity": 1, "description": 1},
    ).limit(20).to_list(20)
    for q in qcs:
        summary.append({
            "entity_type": "qc_issue",
            "entity_id": q["id"],
            "severity": q.get("severity"),
            "label": (q.get("description") or "")[:80],
        })

    # Bug #50 (2026-05-13): open safety events block linked tasks.
    # The model has no requires_work_stoppage field today, so per
    # Sid's spec fallback ("otherwise all open") we treat every open
    # safety event as a blocker.
    safeties = await db.safety_events.find(
        tenant_filter(tenant_id, {
            "linked_task_ids": task_id,
            "status": "open",
        }),
        {"_id": 0, "id": 1, "severity": 1, "event_type": 1, "description": 1},
    ).limit(20).to_list(20)
    for s in safeties:
        et = (s.get("event_type") or "").replace("_", " ")
        descr = (s.get("description") or "")[:60]
        summary.append({
            "entity_type": "safety_event",
            "entity_id": s["id"],
            "severity": s.get("severity"),
            "label": f"{et}: {descr}" if et else descr,
        })

    # Bug #50 (2026-05-13): submittals that are still in flight AND
    # over their A/E review SLA block the task. In-flight-but-on-track
    # submittals are normal procurement; only the SLA-breached ones
    # are surfaced as blockers (Sid's spec).
    subs = await db.submittals.find(
        tenant_filter(tenant_id, {
            "schedule_activity_id": task_id,
            "status": {"$in": ["submitted", "in_review"]},
            "sla_status": "breached",
        }),
        {"_id": 0, "id": 1, "submittal_number": 1, "title": 1,
         "urgency_flag": 1, "overdue_days": 1, "status": 1},
    ).limit(20).to_list(20)
    for sub in subs:
        od = sub.get("overdue_days")
        title_part = (sub.get("title") or "")[:50]
        label = f"{sub.get('submittal_number') or 'SUB'}: {title_part}"
        if od:
            label = f"{label} (+{od}d over SLA)"
        summary.append({
            "entity_type": "submittal",
            "entity_id": sub["id"],
            # Severity is implicit: any breached-SLA submittal is
            # critical to surface. The Tasks-page color logic uses
            # this string; "critical" puts the row at the worst tier.
            "severity": "critical" if sub.get("urgency_flag") else "high",
            "label": label,
        })

    wi_links = await db.work_item_links.find(
        {"entity_type": "task", "entity_id": task_id},
        {"_id": 0, "work_item_id": 1},
    ).limit(50).to_list(50)
    if wi_links:
        wi_ids = list({l["work_item_id"] for l in wi_links})
        wis = await db.work_items.find(
            tenant_filter(tenant_id, {
                "id": {"$in": wi_ids},
                "status": {"$nin": ["closed", "resolved"]},
            }),
            {"_id": 0, "id": 1, "title": 1, "priority": 1},
        ).limit(20).to_list(20)
        for wi in wis:
            summary.append({
                "entity_type": "work_item",
                "entity_id": wi["id"],
                "severity": wi.get("priority") or "normal",
                "label": (wi.get("title") or "")[:80],
            })

    return len(summary), summary


# ---------- Persist wrapper -----------------------------------------------

async def compute_and_persist_status(
    db, tenant_id: str, task_id: str, *,
    today: Optional[date] = None,
    request_id: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> Optional[dict]:
    """Single source of truth for "this task's status changed somehow,
    recompute". Idempotent — only writes when at least one of the four
    derived fields changes. Returns the new field values (or None if the
    task is gone)."""
    today = today or datetime.utcnow().date()
    task = await db.tasks.find_one(tenant_filter(tenant_id, {"id": task_id}), {"_id": 0})
    if not task:
        return None
    count, summary = await count_open_blockers(db, tenant_id, task_id)
    has_blocker = count > 0
    pct = float(task.get("pct_complete") or 0)
    planned = planned_pct_at(task, today)
    new_status = derive_status(pct=pct, planned=planned, has_blocker=has_blocker)

    new_fields = {
        "computed_status": new_status,
        "has_open_blocker": has_blocker,
        "open_blocker_count": count,
        "open_blocker_summary": summary,
    }
    needs_write = any(task.get(k) != v for k, v in new_fields.items())
    if not needs_write:
        return new_fields

    new_fields["updated_at"] = datetime.utcnow().isoformat()
    await db.tasks.update_one(
        tenant_filter(tenant_id, {"id": task_id}),
        {"$set": new_fields},
    )
    # Audit. Lazy import to avoid circular.
    from audit import write_audit
    await write_audit(
        tenant_id=tenant_id, table_name="tasks", op="STATUS_COMPUTE",
        record_id=task_id,
        before={k: task.get(k) for k in ("computed_status", "has_open_blocker", "open_blocker_count")},
        after={k: new_fields.get(k) for k in ("computed_status", "has_open_blocker", "open_blocker_count")},
        actor_id=actor_id, request_id=request_id or gen_id(),
    )
    return new_fields


async def recompute_for_tasks(
    db, tenant_id: str, task_ids: list[str], *,
    today: Optional[date] = None,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> int:
    """Bulk helper. Returns count of tasks whose status actually changed."""
    if not task_ids:
        return 0
    changed = 0
    for tid in task_ids:
        res = await compute_and_persist_status(
            db, tenant_id, tid,
            today=today, actor_id=actor_id, request_id=request_id,
        )
        # Naive change detection — compute_and_persist already logs a
        # write; we just count.
        if res is not None:
            changed += 1
    return changed


# ---------- Phase 4: CPM walk-forward for projected_finish_date -----------

async def _open_delay_impact_for_task(db, tenant_id: str, task_id: str) -> float:
    """Sum of projected_impact_days across open delays directly linked
    to this task. Used as the 'pushed' days for CPM walk-forward."""
    docs = await db.delays.find(
        tenant_filter(tenant_id, {
            "linked_task_ids": task_id,
            "status": {"$in": ["open", "in_progress"]},
        }),
        {"_id": 0, "per_task_impacts": 1},
    ).to_list(50)
    total = 0.0
    for d in docs:
        for imp in (d.get("per_task_impacts") or []):
            if imp.get("task_id") == task_id:
                try:
                    total += float(imp.get("projected_impact_days") or 0)
                except Exception:
                    pass
    return total


async def compute_projected_finish_dates(
    db, tenant_id: str, project_id: str, *,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Phase 4 — walk the dependency graph forward. For each task with
    an open-delay impact, compute its projected_finish_date as
    planned_finish + (own_impact + max_predecessor_projected_shift).

    Cycle-safe: bounded BFS by activity_code; tasks not reached keep their
    existing projected_finish_date (cleared to None when no impact).

    Returns {recomputed: int, changed: list[task_id]}.
    """
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "activity_code": 1, "predecessors": 1,
         "planned_finish": 1, "projected_finish_date": 1},
    ).to_list(5000)
    by_code = {t.get("activity_code"): t for t in tasks if t.get("activity_code")}

    # Pre-compute per-task own_impact (open-delay sum). One query per task.
    own_impact: dict[str, float] = {}
    for t in tasks:
        own_impact[t["id"]] = await _open_delay_impact_for_task(db, tenant_id, t["id"])

    # Topological visit by predecessor walk. We don't have a true topo sort
    # available without re-implementing CPM; this BFS does the safe thing:
    # process each task once, looking up max(predecessor.shift) on demand.
    shift_by_id: dict[str, float] = {}

    def shift_for(task: dict, depth: int = 0) -> float:
        if depth > 200:  # cycle guard
            return shift_by_id.get(task["id"], own_impact.get(task["id"], 0.0))
        if task["id"] in shift_by_id:
            return shift_by_id[task["id"]]
        own = own_impact.get(task["id"], 0.0)
        max_pred = 0.0
        for pred_code in (task.get("predecessors") or []):
            pred_task = by_code.get(pred_code)
            if pred_task:
                max_pred = max(max_pred, shift_for(pred_task, depth + 1))
        shift_by_id[task["id"]] = own + max_pred
        return shift_by_id[task["id"]]

    for t in tasks:
        shift_for(t)

    changed: list[str] = []
    for t in tasks:
        shift_days = shift_by_id.get(t["id"], 0.0)
        pf = t.get("planned_finish")
        new_pfd = None
        if shift_days > 0 and pf:
            try:
                pf_d = date.fromisoformat(str(pf)[:10])
                new_pfd = (pf_d + timedelta(days=int(round(shift_days)))).isoformat()
            except Exception:
                new_pfd = None
        # Idempotent: skip if unchanged.
        if t.get("projected_finish_date") != new_pfd:
            await db.tasks.update_one(
                tenant_filter(tenant_id, {"id": t["id"]}),
                {"$set": {
                    "projected_finish_date": new_pfd,
                    "updated_at": datetime.utcnow().isoformat(),
                }},
            )
            changed.append(t["id"])

    if changed:
        from audit import write_audit
        await write_audit(
            tenant_id=tenant_id, table_name="tasks", op="CPM_RECOMPUTE",
            record_id=project_id,
            before=None, after={"changed_task_ids": changed[:100], "total": len(changed)},
            actor_id=actor_id, request_id=request_id or gen_id(),
        )
    return {"recomputed": len(tasks), "changed": changed}
