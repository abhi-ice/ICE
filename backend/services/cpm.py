"""Critical-path computation. Master prompt §21.

Walks the activity dependency graph. Pure-Python; runs synchronously inside
a FastAPI BackgroundTask (substitution for Celery).

CPM Part A (2026-05-15) -- tasks.predecessors changed from List[str]
to List[dict] {code, type, lag_days}.
CPM Part C (2026-05-15) -- forward pass now consumes type + lag.
CPM Part D (2026-05-15) -- backward pass now mirrors the forward
math: per-successor implied-late-finish derivation with MIN
convergence, preserving the +1 day FS asymmetry in reverse.

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

Float = LS - ES (calendar days). is_critical_path = (float_days <= 0).
For MSU's pure FS+0 schedule, the new backward pass produces
byte-identical LS / LF / float / is_critical_path on every task.
"""
from datetime import date, timedelta
from typing import Iterable, Optional
from collections import defaultdict, deque
from database import get_db, tenant_filter
from models import pred_code, pred_relationship


def _derive_implied_start(
    p_es: date, p_ef: date, rel_type: str, lag_days: int,
    dur_minus_one: int,
) -> date:
    """Given a predecessor's early_start (p_es) + early_finish (p_ef)
    + relationship type + lag (in calendar days), return the EARLIEST
    start date the successor can have under this single predecessor's
    constraint.

    For FF and SF, which constrain the successor's FINISH rather than
    its START, we derive the implied start by subtracting dur_minus_one
    (caller passes successor.duration - 1, since dates are inclusive
    in this CPM model: a 3-day task starting Mon finishes Wed, span
    of 2 days).

    See module docstring for the +1 day FS asymmetry rationale.

    For unknown rel_type, defaults to FS (matches Predecessor.type
    default + handles legacy bare-string entries that came through
    pred_relationship().)
    """
    lag = timedelta(days=lag_days)
    one_day = timedelta(days=1)
    if rel_type == "SS":
        # Same-day start; no offset. SS+0 = same day, SS+2 = 2 days after start.
        return p_es + lag
    if rel_type == "FF":
        # Same-day finish; no offset. Derive start from finish.
        b_finish = p_ef + lag
        return b_finish - timedelta(days=dur_minus_one)
    if rel_type == "SF":
        # B.finish constrained against A.start. No offset.
        b_finish = p_es + lag
        return b_finish - timedelta(days=dur_minus_one)
    # FS (default) -- next-day start convention: +1 offset, then lag.
    return p_ef + one_day + lag


def _derive_implied_late_finish(
    s_ls: date, s_lf: date, rel_type: str, lag_days: int,
    dur_minus_one: int,
) -> date:
    """Mirror of _derive_implied_start() for the backward pass.

    Given a successor's late_start (s_ls) + late_finish (s_lf)
    + relationship type + lag (calendar days), return the LATEST
    finish date the predecessor can have under this single
    successor's constraint.

    The +1 day FS asymmetry in the forward pass becomes -1 in
    reverse. SS / SF have no offset in either direction. FF carries
    no offset (same-finish semantics; direction flips but the +1 is
    not involved).

    SS and SF constrain the predecessor's LATE START rather than
    LATE FINISH, so we convert via A.LF = A.LS + dur_minus_one
    (dates inclusive: a 3-day task with LS=Mon has LF=Wed, span 2).

    For unknown rel_type, defaults to FS (matches Predecessor.type
    default + handles legacy bare-string entries via
    pred_relationship().)
    """
    lag = timedelta(days=lag_days)
    one_day = timedelta(days=1)
    if rel_type == "SS":
        # Mirror of forward SS: B.ES = A.ES + lag -> A.LS = B.LS - lag.
        # No offset. Convert LS to LF via duration-minus-one.
        a_ls = s_ls - lag
        return a_ls + timedelta(days=dur_minus_one)
    if rel_type == "FF":
        # Mirror of forward FF: B.EF = A.EF + lag -> A.LF = B.LF - lag.
        # No offset; same direction (predecessor constrains successor's
        # finish forward, successor constrains predecessor's finish back).
        return s_lf - lag
    if rel_type == "SF":
        # Mirror of forward SF: B.EF = A.ES + lag -> A.LS = B.LF - lag.
        # No offset. Convert LS to LF via duration-minus-one.
        a_ls = s_lf - lag
        return a_ls + timedelta(days=dur_minus_one)
    # FS (default) -- mirror of forward FS: B.ES = A.EF + 1 + lag
    # -> A.LF = B.LS - 1 - lag. The -1 mirrors the forward +1 day
    # convention. FS+0 backward: A.LF = B.LS - 1 (byte-identical to
    # the pre-Part-D `min(ls[s]) - 1` math for FS+0 schedules).
    return s_ls - one_day - lag


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
        es[code] = start_d
        ef[code] = start_d + timedelta(days=dur_minus_one)
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
        ls[code] = lf[code] - timedelta(days=dur_minus_one)

    # Update tasks. Float = LS - ES (calendar days, signed); a task is
    # on the critical path when float_days <= 0. Preserve the existing
    # threshold/comparison -- DO NOT change this without coordinating
    # with the Tasks UI (Part F adds Impacted/Slip columns) and the
    # auto-recompute trigger (Part E).
    critical = 0
    for code, t in by_id.items():
        float_days = (ls[code] - es[code]).days
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
