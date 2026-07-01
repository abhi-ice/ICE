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
from models import gen_id, pred_code, pred_relationship
# CPM B2 (2026-06-12) -- the progress-cascade feature flag. Imported at
# module top (feature_flags has no service deps, so no import cycle). The
# progress_cascade module itself is imported LAZILY inside
# compute_projected_finish_dates to avoid the task_status <-> progress_cascade
# cycle (progress_cascade imports planned_pct_at from this module).
from services.feature_flags import progress_cascade_enabled


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


# pt.79: paperwork work-categories whose progress is driven manually from the
# Submittals & Paperwork page (until the full submittal SLA model lands in v2).
PAPERWORK_CATEGORIES = (
    "preconstruction_paperwork", "construction_paperwork", "closeout_paperwork",
)


def derive_status(*, pct: float, planned: float, has_blocker: bool,
                  work_category: Optional[str] = None,
                  task_status: Optional[str] = None) -> str:
    """Apply the Phase 2 status rule. Strict hierarchy: complete > blocked
    > behind > on_track. Complete wins over blocker so a 100%-done activity
    isn't shown as blocked even if a stale delay still references it.

    pt.79 SHORTCUT (paperwork work-categories ONLY): submittal / approval /
    closeout paperwork tasks have no per-day field progress, so at 0% they would
    read "behind" the moment their planned_start passes — a false alarm that
    misrepresents schedule health. For these categories only, the user drives
    status from the Submittals & Paperwork page: a manual task_status of
    "in_progress" (or any entered progress) reads on_track; a manual "blocked"
    reads blocked; "open"/not_started paperwork still flags behind once overdue
    so genuinely-late paperwork is never hidden. Non-paperwork tasks are
    completely unaffected (the branch is gated on work_category). The full
    submittal-SLA model replaces this shortcut in v2."""
    if pct >= 100:
        return "complete"
    if has_blocker:
        return "blocked"
    if work_category in PAPERWORK_CATEGORIES:
        if task_status == "blocked":
            return "blocked"
        if task_status == "in_progress" or pct > 0:
            return "on_track"
        # else fall through: an untouched ("open") paperwork task still goes
        # "behind" when overdue.
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
    new_status = derive_status(
        pct=pct, planned=planned, has_blocker=has_blocker,
        work_category=task.get("work_category"),   # pt.79 paperwork shortcut
        task_status=task.get("status"),
    )

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
    data_date: Optional[date] = None,
) -> dict:
    """Phase 4 — walk the dependency graph forward, computing each
    task's IMPACTED start / finish date when open blockers push it.

    CPM Slip-cascade fix (2026-05-16) -- DATE-BASED, FLOAT-AWARE
    cascade. The pre-fix algorithm was SHIFT-BASED:

        successor_shift = own_delay_impact + MAX(predecessor_shifts)

    which blindly carried any predecessor's shift forward EVEN WHEN
    the delayed predecessor still finishes long before the successor
    needs it (because the successor was waiting on a different,
    later predecessor, or had float / gap absorbing the delay). A
    2-day delay on a task with 22 days of float over-propagated to
    36 of 53 downstream tasks and moved the project finish.

    THE FIX -- per predecessor, only the OVERFLOW past the
    successor's planned start cascades:

      For each task (memoized topological recursion):
        own_impact   = sum of open blocker impact days directly
                       linked to THIS task (delays + RFIs +
                       over-SLA submittals; _open_blocker_impact_
                       for_task).
        inherited    = MAX over predecessors P of:
                         IP  = implied successor start from P's
                               PLANNED dates  (working-day math)
                         II  = implied successor start from P's
                               IMPACTED dates
                         shift_from_P  = wd_between(IP, II)   (>= 0;
                               how much P's delay shifts the gated
                               start)
                         push_past    = wd_between(planned_start, II)
                               (how far II lands past the
                               successor's own planned start)
                         overflow_P   = max(0, min(shift_from_P,
                                                   push_past))
        total_shift  = inherited + own_impact
        impacted_start  = add_working_days(planned_start, total_shift)
        impacted_finish = add_working_days(planned_finish, total_shift)
        slip = total_shift  (== wd_between(planned_finish,
                              impacted_finish))

      total_shift == 0  ->  projected_start_date /
                            projected_finish_date written as None
                            (Tasks page shows em-dash; the task
                            did NOT move -- float absorbed it).
      total_shift  > 0  ->  projected dates written.

    Why min(shift_from_P, push_past): a predecessor can never push a
    successor by MORE than the predecessor's own delay (shift_from_P
    caps it), and only the part landing past the successor's planned
    start counts (push_past gates it). When P is not delayed,
    shift_from_P == 0 so overflow is 0 regardless of any calendar
    drift between the imported planned dates and our working-day
    math -- robust against MS-Project import quirks.

    DECISION D1 (slip-cascade fix, documented): the directly-delayed
    task SLIDES -- both impacted_start AND impacted_finish move by
    total_shift. This matches the existing Part F / Part L behavior
    (test_rfi_impact_pushes_slip asserts BOTH dates move by the
    impact) and the GSA Office Test expected output (Task 5 with a
    2-day own delay shows impacted_start +2 AND impacted_finish +2).
    The task is modeled as sliding, not stretching.

    DECISION D2: a successor never starts EARLIER than planned from
    cascade math -- overflow is clamped at 0. Pure-downstream tasks
    only ever move later.

    Working-day math (Part L) used throughout: _derive_implied_start
    (FS/SS/FF/SF, working-day offsets + lag) and add_working_days /
    working_days_between.

    Returns {recomputed: int, changed: list[task_id]}.
    """
    # Enterprise CPM ("Diagram 2", 2026-06-29) -- double-application guard
    # (judge m4). When the project runs on the enterprise engine, the
    # progress-driven slip is EMERGENT in that engine's forward pass and the
    # engine writes the forecast columns itself; running this impact-days
    # overlay too would apply the shift twice and clobber the engine's
    # writeback. Skip it. The check is gated by enterprise_cpm_enabled() so
    # with the flag OFF there is NO extra query and this is byte-identical to
    # before.
    from services.feature_flags import enterprise_cpm_enabled, schedule_engine_for_project
    if enterprise_cpm_enabled():
        _proj = await db.projects.find_one(
            tenant_filter(tenant_id, {"id": project_id}),
            {"_id": 0, "schedule_engine": 1},
        ) or {}
        if schedule_engine_for_project(_proj) == "enterprise":
            return {"recomputed": 0, "changed": [], "skipped": "enterprise_engine"}
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "activity_code": 1, "predecessors": 1,
         "duration_days": 1,
         "planned_start": 1, "planned_finish": 1,
         # CPM Cruft 3.G -- slip_days pulled so the idempotent
         # skip-if-unchanged check below can compare it.
         "projected_start_date": 1, "projected_finish_date": 1,
         "slip_days": 1},
    ).to_list(5000)
    by_code = {t.get("activity_code"): t for t in tasks if t.get("activity_code")}

    # CPM B2 (2026-06-12) -- progress-driven slip map. FF_PROGRESS_CASCADE.
    # A task behind its planned percent at the data date grows its remaining
    # duration; that shift folds into the task's OWN shift below (via MAX with
    # the impact-days value) and then cascades to successors through the SAME
    # float-aware overflow machinery a logged delay uses. Flag OFF -> empty
    # map -> own shift is the impact-days value alone (byte-identical to
    # pre-B2).
    #
    # The map is built by services.progress_cascade (lazy import -- avoids the
    # task_status <-> progress_cascade module-load cycle). That module owns
    # the progress query + the paperwork-category skip, so the CPM engine
    # files stay free of DR view-metadata tokens: this function never reads
    # those view fields itself. It only consumes a {task_id -> working-day
    # shift} map.
    progress_shift: dict[str, int] = {}
    if progress_cascade_enabled():
        from services.progress_cascade import build_progress_shift_map_from_db
        # Data date: the project-global as-of date the forward pass measures
        # "behind-ness" against. We use date.today() (the default) rather than
        # any single Daily Report's report_date BY DESIGN -- a CPM data date
        # is one date for the WHOLE schedule walk; binding it to one row's
        # report date would mis-size every OTHER task's planned-percent. The
        # `data_date` param exists only so tests can pin a deterministic date.
        dd = data_date or date.today()
        progress_shift = await build_progress_shift_map_from_db(
            db, tenant_id, project_id, dd,
        )

    # Per-task own_impact (open-blocker sum across delays + RFIs +
    # over-SLA submittals). One walk per task.
    own_impact: dict[str, float] = {}
    for t in tasks:
        own_impact[t["id"]] = await _open_blocker_impact_for_task(db, tenant_id, t["id"])

    # Working-day helpers + the shared CPM per-edge implied-start
    # derivation. Local import keeps the services.cpm <-> services.
    # task_status pairing free of a module-load cycle (cpm.py imports
    # task_status only lazily inside auto_recompute_after_event).
    #
    # CPM Step 1 (2026-05-17) -- add_working_days / working_days_between
    # are FEDERAL-HOLIDAY-AWARE by default. The projected-date shifts
    # and slip computed below now skip federal holidays, so an
    # Impacted Start / Finish can no longer land on July 4 etc.
    from services.working_days import add_working_days, working_days_between
    from services.cpm import _derive_implied_start

    def _parse(v):
        if not v:
            return None
        try:
            return date.fromisoformat(str(v)[:10])
        except Exception:
            return None

    # Memoized recursion. computed[task_id] = (impacted_start: date|None,
    # impacted_finish: date|None, total_shift: int).
    computed: dict[str, tuple] = {}

    def compute(task: dict, depth: int = 0) -> tuple:
        tid = task["id"]
        cached = computed.get(tid)
        if cached is not None:
            return cached

        ps = _parse(task.get("planned_start"))
        pf = _parse(task.get("planned_finish"))
        dur = int(round(float(task.get("duration_days") or 1.0)))
        dur_minus_one = max(0, dur - 1)
        own_days = max(0, int(round(own_impact.get(tid, 0.0))))
        # CPM B2 (2026-06-12) -- combine the impact-days own shift with the
        # progress-driven shift via MAX (not sum): a logged delay's
        # impact_days and a visible progress shortfall are two views of the
        # SAME lateness, so taking the larger is conservative and never
        # double-counts. progress_shift is empty when FF_PROGRESS_CASCADE is
        # off, so own == own_days there (pre-B2 behaviour, byte-identical).
        own = max(own_days, progress_shift.get(tid, 0))

        if depth > 300:
            # Cycle guard -- treat as un-impacted (planned dates,
            # zero shift). Memoize so other paths short-circuit.
            result = (ps, pf, 0)
            computed[tid] = result
            return result

        # ---- inherited shift: max float-aware overflow over preds --
        inherited = 0
        if ps is not None:
            for pred_entry in (task.get("predecessors") or []):
                p_code = pred_code(pred_entry)
                pred_task = by_code.get(p_code) if p_code else None
                if not pred_task:
                    continue
                rel_type, lag = pred_relationship(pred_entry)
                p_ps = _parse(pred_task.get("planned_start"))
                p_pf = _parse(pred_task.get("planned_finish"))
                if p_ps is None or p_pf is None:
                    # Predecessor has no planned dates -- can't derive
                    # an implied start; skip it (mirrors pre-fix
                    # behavior of silently skipping unresolved preds).
                    continue
                # Recurse: predecessor's IMPACTED dates (memoized).
                p_imp_start, p_imp_finish, _p_shift = compute(
                    pred_task, depth + 1,
                )
                if p_imp_start is None:
                    p_imp_start = p_ps
                if p_imp_finish is None:
                    p_imp_finish = p_pf
                # IP -- implied successor start from pred PLANNED dates.
                ip = _derive_implied_start(
                    p_ps, p_pf, rel_type, lag, dur_minus_one,
                )
                # II -- implied successor start from pred IMPACTED dates.
                ii = _derive_implied_start(
                    p_imp_start, p_imp_finish, rel_type, lag,
                    dur_minus_one,
                )
                # shift_from_P >= 0: II is never earlier than IP since
                # impacted dates >= planned dates and _derive_implied_
                # start is monotonic. max(0, ...) is defensive.
                shift_from_p = max(0, working_days_between(ip, ii))
                if shift_from_p == 0:
                    # Predecessor not delayed -> contributes nothing,
                    # regardless of any calendar drift between II and
                    # this task's planned_start. This is the key
                    # robustness guard against the over-propagation bug.
                    continue
                # push_past: how far the IMPACTED implied start lands
                # past this task's own planned start. Negative when II
                # is still before planned_start (delay absorbed by the
                # gap / float, or a different predecessor is binding).
                push_past = working_days_between(ps, ii)
                overflow = max(0, min(shift_from_p, push_past))
                if overflow > inherited:
                    inherited = overflow

        total_shift = inherited + own
        if ps is not None and pf is not None and total_shift > 0:
            imp_start = add_working_days(ps, total_shift)
            imp_finish = add_working_days(pf, total_shift)
        else:
            # total_shift == 0 -> task did not move. Carry planned
            # dates forward in the cache so downstream preds derive
            # II == IP for this task (shift_from_P == 0).
            imp_start = ps
            imp_finish = pf

        result = (imp_start, imp_finish, total_shift)
        computed[tid] = result
        return result

    for t in tasks:
        compute(t)

    changed: list[str] = []
    for t in tasks:
        imp_start, imp_finish, total_shift = computed.get(
            t["id"], (None, None, 0),
        )
        # Only a positive total_shift means the task actually moved.
        # total_shift == 0 -> projected dates cleared to None so the
        # Tasks page Impacted Start / Impacted Finish / Slip columns
        # render em-dash. THIS is the bug fix: a task whose
        # predecessors' delays were absorbed by float shows nothing.
        if total_shift > 0 and imp_start is not None and imp_finish is not None:
            new_psd = imp_start.isoformat()
            new_pfd = imp_finish.isoformat()
            # CPM Cruft 3.G (2026-05-16) -- persist the working-day
            # slip alongside the projected dates. total_shift IS
            # working_days_between(planned_finish, projected_finish)
            # by construction (imp_finish = add_working_days(pf,
            # total_shift)), so we use it directly -- no recompute.
            new_slip = total_shift
        else:
            new_psd = None
            new_pfd = None
            # Unmoved task -- slip_days is None, mirroring how
            # projected_finish_date itself is cleared to None.
            new_slip = None
        cur_pfd = t.get("projected_finish_date")
        cur_psd = t.get("projected_start_date")
        cur_slip = t.get("slip_days")
        if cur_pfd != new_pfd or cur_psd != new_psd or cur_slip != new_slip:
            await db.tasks.update_one(
                tenant_filter(tenant_id, {"id": t["id"]}),
                {"$set": {
                    "projected_finish_date": new_pfd,
                    "projected_start_date": new_psd,
                    "slip_days": new_slip,
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
# Tasks page Float column shows STRUCTURAL float (the network's
# slack independent of any logged events). Field PMs expect a LIVE
# number: "Task A had 22 days float; after a delay it should read
# the remaining slack."
#
# CPM Bug 3.E (2026-05-16) -- remaining float is now derived from
# the CASCADE'S projected dates, NOT from a direct-blocker sum.
#
# THE PRE-3.E FAULT:
#   remaining_float_days = max(0, structural - sum_DIRECT_impacts)
#   where the sum walked only blockers DIRECTLY linked to the task.
#   Two faults:
#     (a) A task impacted purely by an UPSTREAM delay (inherited
#         cascade slip) had sum_direct = 0, so it showed
#         remaining = structural -- falsely reassuring. (Task 15
#         slipped +10 from Task 6's delay but showed Rem Float 5.)
#     (b) The max(0, ...) clamp hid how far underwater a task was.
#         A task 5 working days past its float read 0, not -5.
#
# THE 3.E FIX (option (c) -- both diagnostic reports concur):
#   remaining_float_days = structural_float_days
#                          - working_days_between(planned_finish,
#                                                  projected_finish_date)
#   * Counts INHERITED cascade slip (the projected date already
#     merges direct + inherited via the cascade's MAX convergence
#     -- no double-counting; option (a) structural-direct-inherited
#     WOULD double-count and is NOT used).
#   * NO max(0, ...) clamp -- negative is the honest value (D-3E).
#     A task with structural float 5 pushed 10 working days shows
#     -5: it has consumed all its float and is 5 days into the red,
#     driving the project finish. Primavera P6 surfaces negative
#     float the same way.
#   * For a DIRECTLY-delayed task cascade_slip == own_impact, so
#     this returns the same MAGNITUDE as the old direct-sum -- only
#     difference is negatives are now shown instead of clamped.
#   * Unmoved task (projected_finish_date is None) -> cascade_slip
#     is 0 -> remaining == structural.
#
# CPM Bug 3.F (2026-05-16) -- also stamps is_currently_critical =
# (remaining_float_days <= 0). True when the task has consumed all
# its float by direct delay OR inherited cascade -- it now drives
# the project finish. DISTINCT from is_critical_path (structural,
# unchanged). A structural-CP task (float 0, no delay) is trivially
# is_currently_critical (0 <= 0).
#
# ORDERING: this helper now DEPENDS on projected_finish_date being
# already computed. auto_recompute_after_event MUST call it AFTER
# compute_projected_finish_dates (Step 3, after Step 2). The bare
# compute_cpm path does not refresh projected dates -- the
# recompute_cpm endpoint (Bug 2.E) routes through
# auto_recompute_after_event so the full chain fires.
#
# CONTRACT (unchanged where not noted):
#   * structural total_float_days STAYS a pure network metric.
#   * is_critical_path stays STRUCTURAL (float <= 0). Bug 3.F adds
#     is_currently_critical as a SEPARATE field.
#   * Audit chain immutability: per-event verdict snapshots stay
#     frozen; remaining_float_days / is_currently_critical are
#     DERIVED VIEWS refreshed on every recompute.
# =========================================================================
async def compute_remaining_float_for_tasks(
    db, tenant_id: str, project_id: str,
) -> dict:
    """For each task, stamp remaining_float_days (SIGNED -- may be
    negative) + is_currently_critical, derived from the cascade's
    projected_finish_date.

      remaining_float_days = total_float_days
                             - working_days_between(planned_finish,
                                                     projected_finish_date)
      is_currently_critical = (remaining_float_days <= 0)

    Idempotent: skip write when neither value changed. Returns
    {scanned, changed}.

    DEPENDS ON projected_finish_date already being computed -- call
    AFTER compute_projected_finish_dates (Bug 3.E ordering).
    """
    from services.working_days import working_days_between

    # Enterprise CPM ("Diagram 2", 2026-06-29) -- double-application guard
    # (judge m4): the enterprise engine writes remaining_float_days /
    # is_currently_critical itself (from finish-float, signed), so this
    # impact-days overlay must not run for an enterprise project. Gated by
    # enterprise_cpm_enabled() -> no extra query, byte-identical, when OFF.
    from services.feature_flags import enterprise_cpm_enabled, schedule_engine_for_project
    if enterprise_cpm_enabled():
        _proj = await db.projects.find_one(
            tenant_filter(tenant_id, {"id": project_id}),
            {"_id": 0, "schedule_engine": 1},
        ) or {}
        if schedule_engine_for_project(_proj) == "enterprise":
            return {"scanned": 0, "changed": 0, "skipped": "enterprise_engine"}

    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {
            "project_id": project_id, "status": {"$ne": "deleted"},
        }),
        {"_id": 0, "id": 1, "total_float_days": 1,
         "remaining_float_days": 1, "is_currently_critical": 1,
         "planned_finish": 1, "projected_finish_date": 1},
    ).to_list(5000)

    scanned = 0
    changed = 0
    for t in tasks:
        scanned += 1
        structural = int(t.get("total_float_days") or 0)
        pf = t.get("planned_finish")
        proj = t.get("projected_finish_date")
        # cascade_slip = working days the task's finish moved. The
        # cascade (compute_projected_finish_dates) sets projected_
        # finish_date = add_working_days(planned_finish, total_shift)
        # where total_shift = inherited cascade + own direct impact
        # -- so the slip already merges both. None projected -> the
        # task did not move -> 0 slip.
        cascade_slip = 0
        if pf and proj:
            try:
                cascade_slip = working_days_between(
                    date.fromisoformat(str(pf)[:10]),
                    date.fromisoformat(str(proj)[:10]),
                )
            except Exception:
                cascade_slip = 0
        # Bug 3.E -- SIGNED, NOT clamped. Negative => task has
        # consumed all its float and is that many working days in
        # the red.
        remaining = structural - int(cascade_slip)
        # Bug 3.F -- effectively-critical when no float remains.
        is_currently_critical = remaining <= 0
        if (t.get("remaining_float_days") != remaining
                or t.get("is_currently_critical") != is_currently_critical):
            await db.tasks.update_one(
                tenant_filter(tenant_id, {"id": t["id"]}),
                {"$set": {
                    "remaining_float_days": remaining,
                    "is_currently_critical": is_currently_critical,
                }},
            )
            changed += 1
    return {"scanned": scanned, "changed": changed}
