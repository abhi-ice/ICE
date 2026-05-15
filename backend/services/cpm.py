"""Critical-path computation. Master prompt §21.

Walks the activity dependency graph. Pure-Python; runs synchronously inside
a FastAPI BackgroundTask (substitution for Celery).

CPM Part A (2026-05-15) -- tasks.predecessors changed from List[str]
to List[dict] {code, type, lag_days}.
CPM Part C (2026-05-15) -- forward pass now consumes type + lag.
Per-predecessor implied-start derivation handles all 4 PDM
relationships:

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

    for t in tasks:
        # CPM Part A -- extract activity_code from each predecessor
        # for the topological-sort indegree map.
        for p in t.get("predecessors", []) or []:
            code = pred_code(p)
            if code and code in by_id:
                successors[code].append(t["activity_code"])
                indeg[t["activity_code"]] += 1

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

    # Backward pass (late start / late finish)
    ls: dict[str, date] = {}
    lf: dict[str, date] = {}
    for code in reversed(topo):
        t = by_id[code]
        succs = successors[code]
        dur = float(t.get("duration_days") or 1.0)
        if not succs:
            lf[code] = project_finish
        else:
            lf[code] = min(ls[s] for s in succs) - timedelta(days=1)
        ls[code] = lf[code] - timedelta(days=max(0, int(round(dur)) - 1))

    # Update tasks
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
