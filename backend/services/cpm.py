"""Critical-path computation. Master prompt §21.

Walks the activity dependency graph. Pure-Python; runs synchronously inside
a FastAPI BackgroundTask (substitution for Celery).

CPM Part A (2026-05-15) -- tasks.predecessors changed from List[str]
to List[dict] {code, type, lag_days}.
CPM Part C (2026-05-15) -- forward pass now consumes type + lag.
CPM Part D (2026-05-15) -- backward pass now mirrors the forward
math: per-successor implied-late-finish derivation with MIN
convergence, preserving the +1 day FS asymmetry in reverse.
CPM Part E (2026-05-15) -- auto-recompute hook fires after every
delay / RFI / submittal-SLA-flip / mitigation event. Closes the
stale-float gap: the second delay logged in a request cycle now
sees fresh float instead of the values from before the first
delay's cascade ran. See auto_recompute_after_event() below.

CPM Part L (2026-05-16) -- WORKING-DAY MATH.
All forward + backward arithmetic moved off calendar days to
working days (Mon-Fri). Pre-Part-L:
  FS+0: B.ES = A.EF + timedelta(days=1)     # calendar
  EF:   ES + timedelta(days=dur-1)           # calendar
Post-Part-L:
  FS+0: B.ES = next_business_day(A.EF)       # skips weekends
  EF:   add_working_days(ES, dur-1)          # skips weekends
  lag:  treated as WORKING days (not calendar) -- matches MS
        Project / Primavera default for Schedule-from-Working-Days.
Module-level rationale + design decisions D1-D5 documented in
services/working_days.py. The MSU FS+0 schedule produces dates
that fall on business days already, so this is a refinement not
a regression for already-correct cases.

Per-predecessor implied-start derivation (forward pass) handles
all 4 PDM relationships:

    FS (Finish-to-Start, default):
        B.start = A.finish + 1 day + lag
        +1 day offset is the industry convention -- FS+0 means the
        successor starts the day AFTER the predecessor finishes
        (e.g., MSU: task 1.6 finishes 5/14, task 1.7 starts 5/15).
        Preserves backward compat for every FS+0 schedule.

    SS (Start-to-Start):
        B.start = A.start + lag
        NO +1 offset -- SS+0 means same start day (parallel start).
        SS+2 means successor starts 2 days after predecessor starts.

    FF (Finish-to-Finish):
        B.finish = A.finish + lag        (NO +1 offset; same-day finish)
        B.start  = B.finish - duration + 1
        FF+0 means both finish the same day.

    SF (Start-to-Finish, rare):
        B.finish = A.start + lag         (NO +1 offset)
        B.start  = B.finish - duration + 1

The +1 day asymmetry between FS and SS/FF/SF is the source of many
bugs in homegrown CPM engines -- documented explicitly here so the
next person reading this code doesn't "fix" it by adding +1 to SS.
FS is "successor starts after predecessor's last day"; SS/FF/SF are
"start-on-same-day" / "finish-on-same-day" / "start-against-other's-
start" semantics by industry convention.

For multiple predecessors: derive a candidate B.start from EACH
predecessor's constraint, then take MAX across candidates. The
latest binding constraint wins -- standard CPM convergence math.

PART D -- BACKWARD PASS (mirror math):

The backward pass walks the topo order in reverse. For each
predecessor A with successors S, derive a candidate A.LF (latest
finish) from EACH successor's constraint, then take MIN across
candidates. The earliest binding successor wins -- standard CPM
divergence math.

The +1 day FS asymmetry is mirrored as -1 in the backward direction.
The same sign rule applies to FF (no offset, same direction). SS
and SF constrain the predecessor's LATE START rather than LATE
FINISH; we convert via A.LF = A.LS + (dur - 1).

    FS (forward: B.ES = A.EF + 1 + lag):
        backward: A.LF = B.LS - 1 - lag    (mirrors the +1)

    SS (forward: B.ES = A.ES + lag):
        backward: A.LS = B.LS - lag        (no offset)
                  A.LF = A.LS + (dur - 1)

    FF (forward: B.EF = A.EF + lag):
        backward: A.LF = B.LF - lag        (no offset, same direction)

    SF (forward: B.EF = A.ES + lag):
        backward: A.LS = B.LF - lag        (no offset)
                  A.LF = A.LS + (dur - 1)

Float = working_days_between(ES, LS). is_critical_path = (float_days
<= 0). For MSU's pure FS+0 schedule, the new backward pass produces
byte-identical LS / LF / float / is_critical_path on every task.

CPM Bug 3.B (2026-05-16) -- structural float is a WORKING-DAY count
(was calendar days pre-Part-L). total_float_days now answers "how
many WORK days can this task slip" -- units-consistent with the
working-day Impacted Start/Finish dates (Part L) and with the
working-day delay/RFI/submittal impact entered by supers. The
"within float / pushes critical path" verdict (Part I/J/K) compares
working-day impact against this working-day float -- units-clean.
"""
from datetime import date, timedelta
from typing import Iterable, Optional
from collections import defaultdict, deque
from database import get_db, tenant_filter
from models import pred_code, pred_relationship
from services.working_days import (
    is_business_day, next_business_day, prev_business_day,
    add_working_days,
)


def _derive_implied_start(
    p_es: date, p_ef: date, rel_type: str, lag_days: int,
    dur_minus_one: int,
) -> date:
    """Given a predecessor's early_start (p_es) + early_finish (p_ef)
    + relationship type + lag (in WORKING days), return the EARLIEST
    start date the successor can have under this single predecessor's
    constraint.

    CPM Part L (2026-05-16) -- working-day math throughout:
      * FS +1 offset uses next_business_day(p_ef) instead of +timedelta(1)
      * Lag is WORKING days (add_working_days, not timedelta)
      * Duration accumulation for FF/SF uses add_working_days
      * Weekend predecessor finishes (legacy data) get rolled back to
        prev_business_day before applying +1 (D2 in working_days.py)

    For FF and SF, which constrain the successor's FINISH rather than
    its START, we derive the implied start by subtracting dur_minus_one
    WORKING days (a 3-day task finishing Wed started Mon).

    For unknown rel_type, defaults to FS (matches Predecessor.type
    default + handles legacy bare-string entries that came through
    pred_relationship().)
    """
    if rel_type == "SS":
        # Same-day start; no offset. Lag added in working days.
        # SS+0: B.ES = A.ES; SS+2: B.ES = 2 working days after A.ES.
        # add_working_days handles weekend anchoring on p_es defensively.
        return add_working_days(p_es, lag_days)
    if rel_type == "FF":
        # Same-day finish; no offset. Derive start from finish.
        # B.EF = A.EF + lag working days; then B.ES = B.EF - (dur-1) working.
        b_finish = add_working_days(p_ef, lag_days)
        return add_working_days(b_finish, -dur_minus_one)
    if rel_type == "SF":
        # B.finish constrained against A.start; no offset.
        b_finish = add_working_days(p_es, lag_days)
        return add_working_days(b_finish, -dur_minus_one)
    # FS (default) -- next-day start convention.
    # Step 1: D2 -- anchor p_ef onto a business day. Legacy schedules
    #   that imported with weekend planned_finish values get rolled
    #   back to the prior Friday so the +1 offset doesn't land on
    #   Sat+1=Sun.
    # Step 2: next_business_day applies the +1 offset (Fri -> next Mon).
    # Step 3: D1 -- add_working_days(..., lag) consumes the lag in
    #   working days. FS+0 => 0 working days after the next-day jump
    #   (so just the +1 offset). FS+7 => 7 working days after.
    p_ef_anchored = p_ef if is_business_day(p_ef) else prev_business_day(p_ef)
    next_day = next_business_day(p_ef_anchored)
    return add_working_days(next_day, lag_days)


def _derive_implied_late_finish(
    s_ls: date, s_lf: date, rel_type: str, lag_days: int,
    dur_minus_one: int,
) -> date:
    """Mirror of _derive_implied_start() for the backward pass.

    Given a successor's late_start (s_ls) + late_finish (s_lf)
    + relationship type + lag (WORKING days), return the LATEST
    finish date the predecessor can have under this single
    successor's constraint.

    CPM Part L (2026-05-16) -- working-day math throughout. The
    +1 day FS asymmetry in the forward pass becomes -1 in reverse,
    implemented via prev_business_day. SS / SF have no offset in
    either direction. FF carries no offset.

    SS and SF constrain the predecessor's LATE START rather than
    LATE FINISH, so we convert via A.LF = A.LS + dur_minus_one
    working days.

    For unknown rel_type, defaults to FS (matches Predecessor.type
    default + handles legacy bare-string entries via
    pred_relationship().)
    """
    if rel_type == "SS":
        # Mirror of forward SS: B.ES = A.ES + lag -> A.LS = B.LS - lag.
        # No offset; lag in working days.
        a_ls = add_working_days(s_ls, -lag_days)
        return add_working_days(a_ls, dur_minus_one)
    if rel_type == "FF":
        # Mirror of forward FF: B.EF = A.EF + lag -> A.LF = B.LF - lag.
        # No offset; lag in working days.
        return add_working_days(s_lf, -lag_days)
    if rel_type == "SF":
        # Mirror of forward SF: B.EF = A.ES + lag -> A.LS = B.LF - lag.
        # No offset.
        a_ls = add_working_days(s_lf, -lag_days)
        return add_working_days(a_ls, dur_minus_one)
    # FS (default) -- mirror of forward FS: B.ES = next_biz(A.EF) + lag
    # -> A.LF = prev_biz(B.LS) - lag. The prev_business_day mirrors the
    # forward next_business_day. For FS+0 on a Monday successor LS,
    # A.LF = prev_business_day(Mon) = prev Fri. Lag consumed in working
    # days going backward.
    s_ls_anchored = s_ls if is_business_day(s_ls) else next_business_day(s_ls)
    prev_day = prev_business_day(s_ls_anchored)
    return add_working_days(prev_day, -lag_days)


async def compute_cpm(tenant_id: str, project_id: str) -> dict:
    db = get_db()
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0},
    ).to_list(10000)
    if not tasks:
        return {"updated": 0, "critical_count": 0}

    by_id = {t["activity_code"]: t for t in tasks}
    successors: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = defaultdict(int)
    # CPM Part D -- edge map keyed by (predecessor_code, successor_code),
    # value is the list of (rel_type, lag_days) tuples for that edge. A
    # list (not a single tuple) so parallel edges between the same pair
    # of activities -- e.g., A->B with both SS+2 and FF+1, rare but
    # legal -- are all preserved as separate backward-pass candidates.
    edges: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)

    for t in tasks:
        # CPM Part A -- extract activity_code from each predecessor
        # for the topological-sort indegree map.
        # CPM Part D -- also record the relationship type + lag for
        # each edge so the backward pass can derive per-successor
        # implied-LF candidates.
        for p in t.get("predecessors", []) or []:
            code = pred_code(p)
            if code and code in by_id:
                successors[code].append(t["activity_code"])
                indeg[t["activity_code"]] += 1
                rel_type, lag_days = pred_relationship(p)
                edges[(code, t["activity_code"])].append((rel_type, lag_days))

    # Forward pass (early start / early finish)
    es: dict[str, date] = {}
    ef: dict[str, date] = {}
    queue = deque(t["activity_code"] for t in tasks if indeg[t["activity_code"]] == 0)
    topo: list[str] = []
    indeg_copy = dict(indeg)
    while queue:
        code = queue.popleft()
        topo.append(code)
        t = by_id[code]
        dur = float(t.get("duration_days") or 1.0)
        dur_minus_one = max(0, int(round(dur)) - 1)

        # CPM Part C (2026-05-15) -- per-predecessor implied-start
        # derivation. For each predecessor, _derive_implied_start
        # produces a candidate B.start that satisfies THAT
        # predecessor's constraint (relationship type + lag). Take
        # MAX across candidates -- the latest binding constraint
        # wins (standard CPM convergence). Predecessors that don't
        # resolve to a known activity_code are skipped silently.
        candidates: list[date] = []
        for p in (t.get("predecessors") or []):
            pc = pred_code(p)
            if not pc or pc not in by_id:
                continue
            rel_type, lag_days = pred_relationship(p)
            candidate = _derive_implied_start(
                es[pc], ef[pc], rel_type, lag_days, dur_minus_one,
            )
            candidates.append(candidate)

        if not candidates:
            # Root task -- anchor to planned_start, falling back to
            # today's date for tasks with no planned_start set yet.
            ps = t.get("planned_start")
            start_d = _to_date(ps) if ps else date.today()
        else:
            start_d = max(candidates)
        # CPM Part L (2026-05-16) -- anchor onto a business day. If
        # the root task's planned_start (or any computed start) lands
        # on a weekend (legacy data), roll forward to next Mon.
        if not is_business_day(start_d):
            start_d = next_business_day(start_d - timedelta(days=1))
        es[code] = start_d
        # CPM Part L -- duration accumulates in WORKING days. A 5-day
        # task starting Mon finishes Fri (Mon=day1, Tue=2, Wed=3,
        # Thu=4, Fri=5). dur_minus_one is 4; add_working_days(Mon, 4)
        # = Fri. Identical to pre-Part-L for pure-weekday spans.
        ef[code] = add_working_days(start_d, dur_minus_one)
        for s in successors[code]:
            indeg_copy[s] -= 1
            if indeg_copy[s] == 0:
                queue.append(s)

    if len(topo) < len(tasks):
        # Cycle detected — leave is_critical_path unchanged
        return {"updated": 0, "critical_count": 0, "error": "cycle_detected"}

    project_finish = max(ef.values())

    # CPM Part D (2026-05-15) -- Backward pass (late start / late finish)
    # with per-successor implied-LF derivation and MIN convergence.
    # Mirror of the forward pass: instead of MAX over predecessor
    # candidates, take MIN over successor candidates. Earliest binding
    # successor wins.
    #
    # Tasks with no successors anchor at project_finish (the standard
    # "no later than the project completion milestone" rule).
    #
    # For FS+0 schedules (e.g., MSU), the candidate via every edge
    # reduces to `successor.LS - 1 day`, which is byte-identical to
    # the pre-Part-D math `min(ls[s] for s in succs) - 1 day`. No date
    # drift on any existing FS+0 project.
    ls: dict[str, date] = {}
    lf: dict[str, date] = {}
    for code in reversed(topo):
        t = by_id[code]
        succs = successors[code]
        dur = float(t.get("duration_days") or 1.0)
        dur_minus_one = max(0, int(round(dur)) - 1)

        if not succs:
            # Tail task -- anchor to project_finish.
            lf[code] = project_finish
        else:
            # Derive a candidate A.LF from EACH outgoing edge (one
            # successor may contribute multiple candidates if the
            # pair has parallel edges, e.g. SS+2 AND FF+1). Take MIN.
            # set(succs) deduplicates the topo-sort successor list
            # so each unique successor's edges contribute exactly
            # once; the inner loop walks edges[(code, s)] which
            # already holds every (rel_type, lag) for that pair.
            candidates: list[date] = []
            for s_code in set(succs):
                for rel_type, lag_days in edges[(code, s_code)]:
                    candidate = _derive_implied_late_finish(
                        ls[s_code], lf[s_code], rel_type, lag_days,
                        dur_minus_one,
                    )
                    candidates.append(candidate)
            # candidates is non-empty here: succs is non-empty AND
            # edges is populated in lock-step with successors in the
            # setup loop above, so every successor has at least one
            # (rel_type, lag) entry in edges.
            lf[code] = min(candidates)
        # CPM Part L (2026-05-16) -- LS = LF - (dur-1) in working days.
        # A 5-day task with LF on Friday has LS on Monday (Mon=1,
        # Tue=2, Wed=3, Thu=4, Fri=5). add_working_days(Fri, -4) = Mon.
        ls[code] = add_working_days(lf[code], -dur_minus_one)

    # Update tasks. Float = LS - ES in WORKING days (Part L: was
    # calendar). A task is on the critical path when float_days <= 0.
    # Working-day float makes the structural metric match field
    # crews' intuition: "3 days of float" = 3 work-days the task
    # could slip before pushing the project completion.
    #
    # Implementation note: we use working_days_between() instead of
    # (LS - ES).days. For tasks on the critical path, LS == ES so
    # the count is 0 regardless. For off-CP tasks, the working-day
    # count is <= the calendar-day count (weekends removed). On a
    # pure-weekday schedule with no weekend ES/LS dates, the two
    # are identical; on weekend-spanning slack the working-day
    # count is the more honest metric.
    from services.working_days import working_days_between
    critical = 0
    for code, t in by_id.items():
        float_days = working_days_between(es[code], ls[code])
        is_cp = float_days <= 0
        if is_cp:
            critical += 1
        await db.tasks.update_one(
            tenant_filter(tenant_id, {"id": t["id"]}),
            {"$set": {"is_critical_path": is_cp, "total_float_days": float_days}},
        )

    return {"updated": len(by_id), "critical_count": critical, "project_finish": project_finish.isoformat()}


def _to_date(v):
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return date.today()
    return date.today()


# ---------------------------------------------------------------------------
# CPM Part E (2026-05-15) -- auto-recompute hook
# ---------------------------------------------------------------------------
async def auto_recompute_after_event(
    tenant_id: str,
    project_id: str,
    *,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """Fail-safe CPM auto-recompute hook fired immediately after a
    field event (delay / RFI / submittal SLA flip / mitigation
    choose-unchoose) has been persisted.

    Run order is the one the Part E spec mandates:

      1. compute_cpm() -- refreshes total_float_days +
         is_critical_path on every task in the project. Silent
         recompute (writes per-task field updates only; NO audit
         op). This is the missing link before Part E -- without it,
         the per_task_impacts snapshot on the second delay in a
         request cycle reads stale float and mis-classifies
         consumes_float_only / pushes_critical_path.

      2. compute_projected_finish_dates() -- walks open-delay
         impacts forward to refresh projected_finish_date /
         projected_start_date on each task. Writes the EXISTING
         tasks.CPM_RECOMPUTE audit when the projected dates change.
         This audit op pre-dates Part E (Bucket C1 Phase 4) -- we
         did NOT add a new audit for the compute_cpm step. The
         audit chain looks identical to before Part E, just with
         correct float behind it.

    BOTH calls are wrapped in try/except + warning log. A CPM bug
    -- cycle detection, malformed predecessor, missing field, even
    a transient DB hiccup -- MUST NOT block the field event from
    being saved. The event row is already in the DB by the time
    this hook runs; the hook only refreshes derived columns
    (total_float_days, is_critical_path, projected_finish_date).
    On failure, the manual 'Recompute CPM' button remains the
    fallback path and the affected fields keep their previous
    values until the next event triggers another recompute.

    Callers from routers:
      - field_events_router.create_delay / update_delay / delete_delay
      - docs_router.create_rfi / update_rfi (status flip)
      - mitigations_router.choose_mitigation / unchoose_mitigation
      - submittal_sla service (detect_breaches + recompute_aging
        when sla_status crosses the breached boundary)
      - qc_rework_cascade.create_synthetic_delay_for_qc /
        record_rework_outcome (synthetic delay flow)

    The audit op the calling endpoint writes (DELAY_CREATE, RFI_UPDATE,
    MITIGATION_CHOOSE, etc.) fires AFTER this hook completes so that
    -- for delays specifically -- the re-resolved per_task_impacts
    snapshot is captured in the audit with the fresh float values.
    """
    import logging
    log = logging.getLogger(__name__)

    db = get_db()

    # Step 1 -- compute_cpm (silent, no audit).
    try:
        await compute_cpm(tenant_id, project_id)
    except Exception as exc:
        log.warning(
            "Part E auto-recompute: compute_cpm failed "
            "(tenant=%s, project=%s): %s",
            tenant_id, project_id, exc,
        )

    # Step 2 -- compute_projected_finish_dates (writes the EXISTING
    # CPM_RECOMPUTE audit on change; pre-Part-E behavior preserved).
    try:
        # Lazy import to avoid the services.cpm <-> services.task_status
        # circular at module load.
        from services.task_status import compute_projected_finish_dates
        await compute_projected_finish_dates(
            db, tenant_id, project_id,
            actor_id=actor_id, request_id=request_id,
        )
    except Exception as exc:
        log.warning(
            "Part E auto-recompute: compute_projected_finish_dates "
            "failed (tenant=%s, project=%s): %s",
            tenant_id, project_id, exc,
        )

    # CPM Part L (2026-05-16) -- Step 3: live remaining-float refresh.
    # Walks all 3 open-blocker collections (delays + RFIs + over-SLA
    # submittals) and stamps remaining_float_days = max(0,
    # structural - sum_open_impacts) on every task. Independent
    # try/except so a remaining-float bug doesn't break the existing
    # CPM + projected-finish chain. Idempotent: a no-op when the
    # value hasn't changed.
    try:
        from services.task_status import compute_remaining_float_for_tasks
        await compute_remaining_float_for_tasks(
            db, tenant_id, project_id,
        )
    except Exception as exc:
        log.warning(
            "Part L auto-recompute: compute_remaining_float_for_tasks "
            "failed (tenant=%s, project=%s): %s",
            tenant_id, project_id, exc,
        )
