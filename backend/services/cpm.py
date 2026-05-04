"""Critical-path computation. Master prompt §21.

Walks the activity dependency graph. Pure-Python; runs synchronously inside
a FastAPI BackgroundTask (substitution for Celery)."""
from datetime import date, timedelta
from typing import Iterable
from collections import defaultdict, deque
from database import get_db, tenant_filter


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
        for p in t.get("predecessors", []) or []:
            if p in by_id:
                successors[p].append(t["activity_code"])
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
        preds = [p for p in (t.get("predecessors") or []) if p in by_id]
        dur = float(t.get("duration_days") or 1.0)
        if not preds:
            ps = t.get("planned_start")
            start_d = _to_date(ps) if ps else date.today()
        else:
            start_d = max(ef[p] for p in preds) + timedelta(days=1)
        es[code] = start_d
        ef[code] = start_d + timedelta(days=max(0, int(round(dur)) - 1))
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
