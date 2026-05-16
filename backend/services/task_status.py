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
  - submittals.schedule_activity_id == task_id, submittals.status in [submitted, in_review],
    submittals.sla_status == 'breached' (over-SLA submittals only)
  - work_item_links where (entity_type='task', entity_id=task_id),
    work_items.status NOT IN [closed, resolved]

Bug #50 (2026-05-13): RFIs (via linked_task_ids) + submittals added to
the aggregation. Bug #53 (2026-05-13): QC / safety briefly added by
Bug #50 are REMOVED -- they're internal quality / safety flags, not
schedule blockers (Sid's clarification after seeing them surface in
production). The linkage stays on the entity models so the gallery
filter and per-task lookups continue to work; only the blocker
aggregation drops them.

CPM walk-forward (Phase 4) — see compute_projected_finish_dates.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional

from database import tenant_filter
from models import gen_id, pred_code


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

    # Bug #53 (2026-05-13) -- QC issues and safety events were briefly
    # added as blocker sources by Bug #50 (commit 9a0170d). Sid
    # clarified after seeing it in production: those are internal
    # quality / safety flags, not schedule blockers. The linkage stays
    # (qc_issues.linked_task_ids and safety_events.linked_task_ids
    # remain canonical for the gallery + per-task filtering), but
    # neither surfaces in the Tasks page blocker count or tooltip.
    # See routers/field_events_router.py for the matching removal of
    # the compute_and_persist_status recompute hooks on QC/Safety
    # create + PATCH paths (Bug #50 added them, Bug #53 removes them).

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

async def _open_blocker_impact_for_task(db, tenant_id: str, task_id: str) -> float:
    """Sum of projected_impact_days across all OPEN schedule blockers
    directly linked to this task. Used as the 'pushed' days for the
    CPM walk-forward (compute_projected_finish_dates).

    CPM Part J-4 (2026-05-15) -- renamed from _open_delay_impact_for_task
    and extended to walk the rfis collection alongside delays.

    CPM Part K-4 (2026-05-15) -- extended to also walk db.submittals
    (over-SLA filter). The K-1 schema added per_task_impacts to
    submittals; the K-2 auto-suggest stamps system_suggested entries
    on the SLA-breach moment; K-3 stamps the 5 verdict keys after
    recompute. This helper now sums Path 1 impacts from all three
    collections so a breached submittal pushes the same Slip-column
    days as a delay/RFI on the same task.

    All three walks use the SAME per_task_impacts shape; the per-task
    summation logic is identical across collections.

    Status filters (each matches the upstream count_open_blockers
    convention in this same module, line 89-153 -- the two helpers
    ARE the contract):

        Delays:      status IN ["open", "in_progress"]
        RFIs:        status IN ["draft", "sent", "in_review"]
        Submittals:  status IN ["submitted", "in_review"]
                     AND sla_status == "breached"

    Submittal linkage is via schedule_activity_id (singular) NOT
    linked_task_ids -- submittals carry one task link, mirroring
    count_open_blockers (line 147). Each over-SLA submittal can have
    at most one matching entry in per_task_impacts (the K-2 auto-
    suggest pass populates exactly one entry per linked task).

    Closed / resolved / approved / answered / voided events are
    excluded -- only events CURRENTLY pushing contribute. When a
    submittal flips from breached -> approved, this helper's next
    call drops its impact (status leaves the $in set AND sla_status
    leaves "breached"); the projected_finish_date on the affected
    task moves back automatically. No retroactive rewrite needed.

    QC issues do NOT contribute here: per Bug #53, QC is internal-
    quality only; the SYNTHETIC delay spawned by severity=rework_
    required carries the schedule impact and lives in db.delays.
    """
    # ---- Delays ------------------------------------------------------
    delay_docs = await db.delays.find(
        tenant_filter(tenant_id, {
            "linked_task_ids": task_id,
            "status": {"$in": ["open", "in_progress"]},
        }),
        {"_id": 0, "per_task_impacts": 1},
    ).to_list(50)
    # ---- RFIs (CPM Part J-4) ----------------------------------------
    rfi_docs = await db.rfis.find(
        tenant_filter(tenant_id, {
            "linked_task_ids": task_id,
            "status": {"$in": ["draft", "sent", "in_review"]},
        }),
        {"_id": 0, "per_task_impacts": 1},
    ).to_list(50)
    # ---- Submittals (CPM Part K-4) ----------------------------------
    # Same filter as count_open_blockers (line 145-150). The submittal
    # collection's linked task lives on schedule_activity_id (singular),
    # not linked_task_ids. Over-SLA gate (sla_status="breached") is
    # the contract -- within-SLA submittals are normal procurement
    # and do not contribute schedule-impact days.
    submittal_docs = await db.submittals.find(
        tenant_filter(tenant_id, {
            "schedule_activity_id": task_id,
            "status": {"$in": ["submitted", "in_review"]},
            "sla_status": "breached",
        }),
        {"_id": 0, "per_task_impacts": 1},
    ).to_list(50)

    total = 0.0
    for d in delay_docs:
        for imp in (d.get("per_task_impacts") or []):
            if imp.get("task_id") == task_id:
                try:
                    total += float(imp.get("projected_impact_days") or 0)
                except Exception:
                    pass
    for r in rfi_docs:
        for imp in (r.get("per_task_impacts") or []):
            if imp.get("task_id") == task_id:
                try:
                    total += float(imp.get("projected_impact_days") or 0)
                except Exception:
                    pass
    for s in submittal_docs:
        for imp in (s.get("per_task_impacts") or []):
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

    CPM Part F (2026-05-15) -- also computes + persists
    projected_start_date = planned_start + shift_days using the SAME
    shift_for recursion. Both fields are written in a single $set per
    changed task; the idempotent skip-if-unchanged check now compares
    BOTH derived dates. A task counts as "changed" when EITHER date
    moves, ensuring the Tasks UI's Impacted Start column refreshes
    even when only the start date shifts (rare edge case where own
    impact is zero but a predecessor's shift propagates through).

    Cycle-safe: bounded BFS by activity_code; tasks not reached keep their
    existing projected_finish_date + projected_start_date (cleared to
    None when no impact).

    Returns {recomputed: int, changed: list[task_id]}.
    """
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        # Part F -- pull planned_start + projected_start_date so the
        # idempotent skip-if-unchanged check can compare both derived
        # dates against the prior values.
        {"_id": 0, "id": 1, "activity_code": 1, "predecessors": 1,
         "planned_start": 1, "planned_finish": 1,
         "projected_start_date": 1, "projected_finish_date": 1},
    ).to_list(5000)
    by_code = {t.get("activity_code"): t for t in tasks if t.get("activity_code")}

    # Pre-compute per-task own_impact (open-BLOCKER sum: open delays
    # + open RFIs as of Part J-4). One pair of queries per task.
    own_impact: dict[str, float] = {}
    for t in tasks:
        own_impact[t["id"]] = await _open_blocker_impact_for_task(db, tenant_id, t["id"])

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
        # CPM Part A (2026-05-15) -- iterate predecessor entries and
        # extract the activity_code via the pred_code() adapter
        # (handles both legacy bare-string + post-migration dict).
        # The local variable was shadowing the imported helper name --
        # rename the loop var to `pred_entry` to keep the import
        # accessible inside the closure.
        for pred_entry in (task.get("predecessors") or []):
            p_code = pred_code(pred_entry)
            if not p_code:
                continue
            pred_task = by_code.get(p_code)
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
        ps = t.get("planned_start")
        new_pfd = None
        new_psd = None
        # CPM Part F (2026-05-15) -- both projected dates are produced
        # by the SAME shift_days. We round to int days to match the
        # existing projected_finish behavior (sub-day shifts get
        # rolled up to the nearest whole day). Failure modes are
        # isolated per-date so a bad planned_finish doesn't poison
        # planned_start's projection (or vice versa).
        if shift_days > 0:
            # CPM Part L (2026-05-16) -- shift is in WORKING days, not
            # calendar. A 2-day delay on Fri lands the projected_
            # finish on the next Tue (not Sat). Field crews see dates
            # that match real calendar pages they post on the trailer.
            from services.working_days import add_working_days
            shift_int = int(round(shift_days))
            if pf:
                try:
                    pf_d = date.fromisoformat(str(pf)[:10])
                    new_pfd = add_working_days(pf_d, shift_int).isoformat()
                except Exception:
                    new_pfd = None
            if ps:
                try:
                    ps_d = date.fromisoformat(str(ps)[:10])
                    new_psd = add_working_days(ps_d, shift_int).isoformat()
                except Exception:
                    new_psd = None
        # Idempotent: skip if BOTH derived dates are unchanged.
        cur_pfd = t.get("projected_finish_date")
        cur_psd = t.get("projected_start_date")
        if cur_pfd != new_pfd or cur_psd != new_psd:
            await db.tasks.update_one(
                tenant_filter(tenant_id, {"id": t["id"]}),
                {"$set": {
                    "projected_finish_date": new_pfd,
                    "projected_start_date": new_psd,
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


# =========================================================================
# CPM Part L-2 (2026-05-16) -- live remaining-float column.
#
# Tasks page Float column currently shows STRUCTURAL float (the
# network's slack independent of any logged events). Field PMs
# expect: "Task A has 22 days float. I log a 2-day delay. The
# float should now read 20 days remaining." That's LIVE remaining
# float -- structural minus the sum of all currently-open blocker
# impact days on that task.
#
# This helper walks all 3 collections (delays + RFIs + over-SLA
# submittals) per task, computes the sum, subtracts from structural
# float, clamps at 0, and persists to tasks.remaining_float_days.
# Fires after compute_projected_finish_dates in
# auto_recompute_after_event so every event refresh keeps this
# column live.
#
# CONTRACT:
#   * structural total_float_days STAYS UNCHANGED (pure network metric)
#   * remaining_float_days = max(0, structural - sum_open_impacts)
#   * is_critical_path still derives from structural <= 0 (not from
#     remaining_float)
#   * Resolved / closed / approved events do NOT contribute (the same
#     filters K-4 / K-5 / count_open_blockers use)
#   * remaining_float_days RECOVERS when a delay is resolved -- its
#     impact stops counting toward the open-sum on the next recompute
#
# Audit chain immutability: the per-event audit snapshots stay
# frozen at log-time. remaining_float_days is a DERIVED VIEW
# refreshed on every event, NOT a rewrite of audit history.
# =========================================================================
async def compute_remaining_float_for_tasks(
    db, tenant_id: str, project_id: str,
) -> dict:
    """For each task in the project, stamp remaining_float_days =
    max(0, structural_float_days - sum_open_blocker_impacts).

    Idempotent: skip write when the value didn't change. Returns
    {scanned, changed}.
    """
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id, "status": {"$ne": "deleted"},
        }),
        {"_id": 0, "id": 1, "total_float_days": 1,
         "remaining_float_days": 1},
    ).to_list(5000)

    scanned = 0
    changed = 0
    for t in tasks:
        scanned += 1
        structural = t.get("total_float_days") or 0
        # Walks all 3 collections via the same helper Path 1 uses.
        # _open_blocker_impact_for_task already gates on the K-4
        # filter (delays open/in_progress, RFIs draft/sent/in_review,
        # submittals in_review breached) -- so resolved/closed
        # events automatically drop out.
        total_open = await _open_blocker_impact_for_task(
            db, tenant_id, t["id"],
        )
        # Clamp at 0: remaining can't be negative (per Part I
        # cumulative-float verdict convention).
        remaining = max(0, int(structural) - int(round(total_open)))
        if t.get("remaining_float_days") != remaining:
            await db.tasks.update_one(
                tenant_filter(tenant_id, {"id": t["id"]}),
                {"$set": {"remaining_float_days": remaining}},
            )
            changed += 1
    return {"scanned": scanned, "changed": changed}
