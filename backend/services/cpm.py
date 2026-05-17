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
# CPM Step 1 (2026-05-17) -- these working-day helpers are now
# FEDERAL-HOLIDAY-AWARE by default (working_days.py D1 / Option A).
# Every call below passes no `holidays` argument, so each uses the
# built-in FEDERAL_HOLIDAYS set: the forward pass, backward pass,
# duration accumulation, and float = working_days_between(es, ls)
# all skip federal holidays. No call-site change was needed -- the
# helper defaults carry it. Step 1 does NOT change WHERE the forward
# pass anchors (still planned_start for roots, still re-derived for
# non-roots) -- that anchor question is Step 2.
from services.working_days import (
    is_business_day, next_business_day, prev_business_day,
    add_working_days, working_days_between,
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

    # Forward pass (early start / early finish).
    #
    # CPM Step 2 (2026-05-17, Issue 3.D) -- compute_cpm ANCHORS ON THE
    # IMPORTED MS PROJECT DATES: the imported .mpp is the federal
    # contract artifact, so the engine trusts planned_start /
    # planned_finish instead of re-deriving them from scratch.
    #
    # CPM Step 2.1 (2026-05-17) -- Step 2's forward pass set ES =
    # imported planned_start in ISOLATION, never consulting
    # predecessors, while the backward pass walked the full
    # predecessor/successor network. The two passes used DIFFERENT
    # networks, so float = working_days_between(ES, LS) manufactured
    # fake negatives -- including on root tasks, which have no
    # predecessors at all. PART A below RECONCILES the imported start
    # against predecessors:
    #
    #   predecessor_implied_start = MAX over every resolvable
    #       predecessor of _derive_implied_start (the SAME
    #       per-relationship FS/SS/FF/SF + working-day-lag logic the
    #       pre-Step-2 forward pass used).
    #   ES = max(imported_planned_start, predecessor_implied_start)
    #   EF = imported_planned_finish, BUT when ES is rolled forward
    #        the imported finish rolls forward by the SAME number of
    #        working days, so the task keeps its imported SPAN.
    #
    # The imported date stays the SOURCE OF TRUTH whenever it is
    # feasible (imported_start >= predecessor_implied_start -> ES is
    # the imported date, unchanged). It is overridden ONLY when the
    # import is genuinely impossible -- a task imported to start
    # before its predecessors finish. Root tasks have no predecessors
    # so ES = imported_planned_start, always (this alone fixes the
    # spurious -7 the Diagnostic found on Task 1).
    #
    # CPM Step 3A (2026-05-17) -- schedule_reshape_count must count
    # ONLY tasks whose own imported start is genuinely earlier than
    # their predecessors allow. Step 2.1 decided this against the
    # predecessors' RECONCILED dates, so one genuine reshape near a
    # root cascade-inflated the count down the whole chain (GSA
    # reported 44 reshapes when essentially nothing was wrong). The
    # reconcile of ES (the max() above) still uses the reconciled
    # predecessor dates and still cascades -- that is correct CPM and
    # is UNCHANGED. But the COUNT is now decided against the
    # predecessors' IMPORTED dates, so a task merely dragged forward
    # by an upstream reshape is not counted. Genuine reshapes are
    # recorded in schedule_reshapes -- the import-quality signal that
    # REPLACES Step 2's fake negative float.
    #
    # The topological sort built here is consumed by the backward
    # pass. Kahn's BFS guarantees every predecessor is processed
    # (es/ef populated) before any successor, so the per-predecessor
    # _derive_implied_start calls below always read resolved dates.
    es: dict[str, date] = {}
    ef: dict[str, date] = {}
    # CPM Step 3A (2026-05-17) -- the IMPORTED (business-day-anchored)
    # start/finish per task, captured BEFORE the PART A reconcile.
    # schedule_reshape_count is decided against THESE dates (not the
    # reconciled es/ef) so an upstream reshape does not cascade-
    # inflate the count.
    imported_es: dict[str, date] = {}
    imported_ef: dict[str, date] = {}
    queue = deque(t["activity_code"] for t in tasks if indeg[t["activity_code"]] == 0)
    topo: list[str] = []
    indeg_copy = dict(indeg)
    # D2 -- import-quality observations: tasks where the imported
    # planned_finish disagrees with add_working_days(planned_start,
    # dur-1). Not an error; surfaced in the return payload + logs.
    duration_mismatches: list[dict] = []
    # CPM Step 2.1 / 3A -- tasks whose imported planned_start is
    # GENUINELY earlier than their predecessors' IMPORTED dates allow
    # (an internally inconsistent import). Decided against imported
    # predecessor dates -- a task dragged forward only by an upstream
    # reshape is NOT recorded here (the Step-2.1 cascade over-count).
    schedule_reshapes: list[dict] = []
    while queue:
        code = queue.popleft()
        topo.append(code)
        t = by_id[code]
        dur = float(t.get("duration_days") or 1.0)
        dur_minus_one = max(0, int(round(dur)) - 1)

        # CPM Step 2 -- imported planned_start, anchored onto a
        # business day (a legacy import may have landed it on a
        # weekend / holiday).
        ps = t.get("planned_start")
        imported_start = _to_date(ps) if ps else date.today()
        if not is_business_day(imported_start):
            imported_start = next_business_day(
                imported_start - timedelta(days=1)
            )
        imported_es[code] = imported_start

        # CPM Step 2.1 PART A -- reconcile the imported start against
        # predecessors. _derive_implied_start is the SAME FS/SS/FF/SF
        # + working-day-lag helper the pre-Step-2 forward pass used.
        # Two predecessor-implied starts are derived per task; both
        # read predecessors that are already resolved (topo order):
        #   reconciled -- from es[pc]/ef[pc]. Drives the ES reconcile
        #       (start_d). CASCADES: a rolled-forward predecessor
        #       pushes this task's reconciled start forward too --
        #       correct CPM, UNCHANGED by Step 3A.
        #   imported   -- from imported_es[pc]/imported_ef[pc]. CPM
        #       Step 3A: schedule_reshape_count is decided against
        #       THIS, so a task counts ONLY when its own imported
        #       start is genuinely earlier than its IMPORTED
        #       predecessors allow -- a task dragged forward purely
        #       by an upstream reshape is NOT counted.
        pred_candidates_reconciled: list[date] = []
        pred_candidates_imported: list[date] = []
        for p in (t.get("predecessors") or []):
            pc = pred_code(p)
            if not pc or pc not in by_id:
                continue
            rel_type, lag_days = pred_relationship(p)
            pred_candidates_reconciled.append(
                _derive_implied_start(
                    es[pc], ef[pc], rel_type, lag_days, dur_minus_one,
                )
            )
            pred_candidates_imported.append(
                _derive_implied_start(
                    imported_es[pc], imported_ef[pc], rel_type,
                    lag_days, dur_minus_one,
                )
            )
        # ES reconcile (UNCHANGED): ES = max(imported_start,
        # predecessor-implied-from-reconciled-dates). Imported date
        # wins whenever feasible; overridden only when the
        # predecessors' reconciled dates push past it.
        start_d = imported_start
        if pred_candidates_reconciled:
            pred_implied = max(pred_candidates_reconciled)
            if pred_implied > imported_start:
                start_d = pred_implied
        es[code] = start_d
        # es_moved -- the reconcile rolled this task's ES forward, by
        # a genuine inconsistency OR an inherited upstream cascade.
        # Keys the EF span-shift below: every moved task keeps its
        # span, so float is byte-for-byte the Step-2.1 result.
        es_moved = start_d != imported_start
        # CPM Step 3A -- reshaped == GENUINE inconsistency: this
        # task's imported start is earlier than its IMPORTED
        # predecessors allow. A cascade-only move (es_moved True but
        # the imported start is fine vs imported predecessor dates)
        # is NOT a reshape -- that was the Step-2.1 over-count.
        reshaped = bool(pred_candidates_imported) and (
            max(pred_candidates_imported) > imported_start
        )

        # CPM Step 2 / D2 -- EF anchored on the imported planned_finish
        # (the contract truth). A legacy weekend / holiday finish is
        # rolled BACK to the prior business day.
        # CPM Step 2.1 PART A -- when the ES was rolled forward (for
        # ANY reason -- genuine reshape OR inherited cascade), the
        # imported finish rolls forward by the SAME number of working
        # days so the task keeps its imported span. When planned_
        # finish is missing, fall back to the duration-derived finish.
        pf = t.get("planned_finish")
        if pf:
            imported_finish = _to_date(pf)
            if not is_business_day(imported_finish):
                imported_finish = prev_business_day(
                    imported_finish + timedelta(days=1)
                )
            imported_ef[code] = imported_finish
            if es_moved:
                shift = working_days_between(imported_start, start_d)
                finish_d = add_working_days(imported_finish, shift)
            else:
                finish_d = imported_finish
            ef[code] = finish_d
            # D2 -- detect (don't correct) a duration/finish mismatch.
            expected_ef = add_working_days(start_d, dur_minus_one)
            if finish_d != expected_ef:
                duration_mismatches.append({
                    "activity_code": code,
                    "planned_start": start_d.isoformat(),
                    "planned_finish": finish_d.isoformat(),
                    "duration_days": int(round(dur)),
                    "duration_derived_finish": expected_ef.isoformat(),
                })
        else:
            imported_ef[code] = add_working_days(
                imported_start, dur_minus_one,
            )
            ef[code] = add_working_days(start_d, dur_minus_one)

        # CPM Step 2.1 / 3A -- record the GENUINE reshape (imported
        # start earlier than the IMPORTED predecessors allow) for the
        # schedule_reshape_count diagnostic returned below. A genuine
        # reshape always has es_moved (pred-implied-reconciled >=
        # pred-implied-imported), so reconciled_start is the moved ES.
        if reshaped:
            schedule_reshapes.append({
                "activity_code": code,
                "imported_start": imported_start.isoformat(),
                "reconciled_start": start_d.isoformat(),
                "reconciled_finish": ef[code].isoformat(),
            })

        for s in successors[code]:
            indeg_copy[s] -= 1
            if indeg_copy[s] == 0:
                queue.append(s)

    if len(topo) < len(tasks):
        # Cycle detected — leave is_critical_path unchanged
        return {"updated": 0, "critical_count": 0, "error": "cycle_detected"}

    if duration_mismatches:
        import logging
        logging.getLogger(__name__).info(
            "compute_cpm (tenant=%s project=%s): %d task(s) with "
            "imported planned_finish != duration-derived finish: %s",
            tenant_id, project_id, len(duration_mismatches),
            duration_mismatches[:20],
        )

    if schedule_reshapes:
        import logging
        logging.getLogger(__name__).info(
            "compute_cpm (tenant=%s project=%s): %d task(s) whose "
            "imported planned_start is genuinely earlier than their "
            "IMPORTED predecessors allow (Step 3A genuine reshape): %s",
            tenant_id, project_id, len(schedule_reshapes),
            schedule_reshapes[:20],
        )

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
        # CPM Part L (2026-05-16) -- LS = LF - span in working days.
        # CPM Step 2.1 PART B -- size the task by its IMPORTED SPAN,
        # working_days_between(ES, EF), NOT the duration_days field.
        # The Step-2 forward pass trusts the imported planned_finish
        # as EF; deriving LS from duration_days disagreed with that
        # EF on any duration-mismatched task and manufactured artifact
        # float. working_days_between(es, ef) == dur-1 for a clean
        # task (so this is a no-op there) and the schedule-faithful
        # span for a mismatch task -- the forward and backward passes
        # now agree on every task's footprint.
        ls[code] = add_working_days(
            lf[code], -working_days_between(es[code], ef[code]),
        )

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
    # CPM Step 2.1 (2026-05-17) -- the forward pass now RECONCILES
    # each ES against predecessors (PART A) and the backward pass
    # sizes each task by its imported span (PART B), so the forward
    # and backward passes walk the SAME network. Structural float is
    # therefore normally >= 0; an internally inconsistent import is
    # rolled forward and counted in schedule_reshape_count rather
    # than left to manufacture a fake negative. negative_float_count
    # + the no-clamp policy are KEPT as a backstop -- a genuinely
    # pathological schedule must still EXPOSE a negative, never hide
    # it. is_critical_path keeps the float <= 0 rule, so a
    # negative-float task is still correctly flagged critical.
    critical = 0
    negative_float_count = 0
    for code, t in by_id.items():
        float_days = working_days_between(es[code], ls[code])
        is_cp = float_days <= 0
        if is_cp:
            critical += 1
        if float_days < 0:
            negative_float_count += 1
        await db.tasks.update_one(
            tenant_filter(tenant_id, {"id": t["id"]}),
            {"$set": {"is_critical_path": is_cp, "total_float_days": float_days}},
        )

    return {
        "updated": len(by_id),
        "critical_count": critical,
        "project_finish": project_finish.isoformat(),
        # CPM Step 2 -- import-anchor diagnostics.
        "negative_float_count": negative_float_count,
        "duration_mismatch_count": len(duration_mismatches),
        "duration_mismatches": duration_mismatches[:50],
        # CPM Step 2.1 / 3A -- forward-pass reconcile diagnostic. A
        # task is counted here ONLY when its imported planned_start is
        # genuinely earlier than its IMPORTED predecessors allow (an
        # internally inconsistent import). Step 3A: a task dragged
        # forward purely by an upstream reshape is NOT counted, so the
        # count reflects genuine import problems, not cascade depth.
        # 0 on a fully consistent imported schedule.
        "schedule_reshape_count": len(schedule_reshapes),
        "schedule_reshapes": schedule_reshapes[:50],
    }


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
) -> Optional[dict]:
    """Fail-safe CPM auto-recompute hook fired immediately after a
    field event (delay / RFI / submittal SLA flip / mitigation
    choose-unchoose) has been persisted.

    CPM Bug 2.E (2026-05-16) -- this hook is ALSO the body of the
    manual "Recompute CPM" button. routers.tasks_router.recompute_cpm
    now routes through here (was bare compute_cpm) so one click
    refreshes Float + Slip + Impacted dates + Rem Float in lock-step.
    Returns the compute_cpm result dict ({updated, critical_count,
    project_finish} or {error: ...}) so the endpoint has a payload
    for the frontend. Event-flow callers ignore the return.

    Run order (the ordering is now load-bearing -- Step 3 depends
    on Step 2):

      1. compute_cpm() -- refreshes total_float_days +
         is_critical_path on every task in the project. Silent
         recompute (writes per-task field updates only; NO audit
         op). This is the missing link before Part E -- without it,
         the per_task_impacts snapshot on the second delay in a
         request cycle reads stale float and mis-classifies
         consumes_float_only / pushes_critical_path.

      2. compute_projected_finish_dates() -- walks open-blocker
         impacts forward to refresh projected_finish_date /
         projected_start_date / slip_days on each task. Writes the
         EXISTING tasks.CPM_RECOMPUTE audit when the projected
         dates change.

      3. compute_remaining_float_for_tasks() -- stamps
         remaining_float_days + is_currently_critical. MUST run
         AFTER Step 2: Bug 3.E derives remaining float from the
         projected_finish_date that Step 2 writes.

    All three calls are wrapped in try/except + warning log. A CPM bug
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

    # Step 1 -- compute_cpm (silent, no audit). Capture the result
    # so the recompute_cpm endpoint (Bug 2.E) has a payload to
    # return; event-flow callers ignore it.
    cpm_result: Optional[dict] = None
    try:
        cpm_result = await compute_cpm(tenant_id, project_id)
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

    # Step 3 -- live remaining-float refresh (CPM Part L + Bug 3.E/3.F).
    # Stamps remaining_float_days (signed; Bug 3.E -- derived from the
    # projected_finish_date Step 2 just wrote, NOT clamped at 0) +
    # is_currently_critical (Bug 3.F). MUST run after Step 2 -- the
    # remaining-float formula reads projected_finish_date. Independent
    # try/except so a remaining-float bug doesn't break the CPM +
    # projected-finish chain. Idempotent.
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

    # Bug 2.E -- return the compute_cpm result for the recompute_cpm
    # endpoint. None when compute_cpm raised (handled above).
    return cpm_result
