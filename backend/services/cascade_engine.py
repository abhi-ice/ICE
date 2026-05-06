"""Bucket C3c Phase 2 — RFI-to-CPM cascade engine.

For every open gating_entity, projects how the schedule slips if the
gate stays open for N additional working days. Walks the CPM successor
graph from each gated task to find the transitive cascade, then computes
slip and CP-impact at three horizons: N=5, N=10, N=20 working days.

Snapshots are append-only (one row per recompute, never overwrite) so
the dashboard can show last-computed AND a TIA can later cite cascade
state as of any past date.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime
from typing import Optional

from collections import defaultdict, deque

from database import get_db, tenant_filter
from models import gen_id, utc_now
from services.gating_entities import compute_age_fields, OPEN_STATUSES_PROXY


# Notification thresholds per the C3c spec.
THRESHOLDS = {
    "n10_cp_impact_pm": 5,        # CP impact at N10 > 5 days -> notify PM
    "n20_cp_impact_exec": 15,     # CP impact at N20 > 15 days -> notify PX + executive
}


def severity_for(snapshot: dict) -> str:
    """green | yellow | orange | red — drives table row coloring."""
    cp10 = float(snapshot.get("projected_cp_impact_at_n10_days") or 0)
    cp20 = float(snapshot.get("projected_cp_impact_at_n20_days") or 0)
    over_sla = (snapshot.get("sla_status") == "over_sla")
    if cp20 > THRESHOLDS["n20_cp_impact_exec"]:
        return "red"
    if cp10 > THRESHOLDS["n10_cp_impact_pm"] or over_sla:
        return "orange"
    if cp10 > 0:
        return "yellow"
    return "green"


async def _build_successor_map(tenant_id: str, project_id: str) -> tuple[dict, dict]:
    """Returns (id_to_task, successors_by_task_id). successors_by_task_id
    maps task_id -> list of task_ids that have this task as a predecessor.
    Predecessors on tasks are stored as activity_codes, so we resolve via
    a code->id lookup."""
    db = get_db()
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "activity_code": 1, "predecessors": 1,
         "is_critical_path": 1, "total_float_days": 1, "computed_status": 1},
    ).to_list(20000)
    by_id = {t["id"]: t for t in tasks}
    code_to_id = {t.get("activity_code"): t["id"] for t in tasks if t.get("activity_code")}

    successors: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for p_code in (t.get("predecessors") or []):
            p_id = code_to_id.get(str(p_code))
            if p_id:
                successors[p_id].append(t["id"])
    return by_id, successors


def _bfs_descendants(start_ids: list[str], successors: dict[str, list[str]]) -> set[str]:
    """Return the set of task ids reachable from start_ids via successor
    edges. Excludes the start_ids themselves."""
    seen: set[str] = set()
    queue: deque[str] = deque()
    for s in start_ids:
        for nxt in successors.get(s, []):
            if nxt not in seen and nxt not in start_ids:
                seen.add(nxt)
                queue.append(nxt)
    while queue:
        cur = queue.popleft()
        for nxt in successors.get(cur, []):
            if nxt not in seen and nxt not in start_ids:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _project_slip(by_id: dict, task_ids: set[str], horizon_days: int,
                   *, critical_only: bool) -> float:
    """Maximum effective slip across the given task ids at horizon N.
    Effective slip per task = max(0, horizon - total_float_days). If
    critical_only=True, restrict to tasks marked is_critical_path."""
    worst = 0.0
    for tid in task_ids:
        t = by_id.get(tid)
        if not t:
            continue
        if critical_only and not t.get("is_critical_path"):
            continue
        float_days = t.get("total_float_days")
        try:
            fd = float(float_days) if float_days is not None else 0.0
        except (TypeError, ValueError):
            fd = 0.0
        eff = max(0.0, float(horizon_days) - max(0.0, fd))
        if eff > worst:
            worst = eff
    return round(worst, 2)


async def _latest_approved_proposal_id(tenant_id: str, project_id: str) -> Optional[str]:
    """Pin the snapshot to the most recent approved schedule_proposal so
    a later TIA can reproduce the cascade against the schedule version
    that was live at compute time."""
    db = get_db()
    doc = await db.schedule_proposals.find_one(
        tenant_filter(tenant_id, {"project_id": project_id, "status": "approved"}),
        {"_id": 0, "id": 1},
        sort=[("reviewed_at", -1)],
    )
    return doc["id"] if doc else None


async def compute_cascade(tenant_id: str, gating_entity_id: str) -> Optional[dict]:
    """Compute one cascade_snapshots row for the given gating entity.
    Returns the inserted row, or None if the gating entity isn't found
    or has no gates_task_ids. Always inserts (never updates) so history
    is preserved."""
    db = get_db()
    ge = await db.gating_entities.find_one(
        tenant_filter(tenant_id, {"id": gating_entity_id}), {"_id": 0},
    )
    if not ge:
        return None

    direct = list(ge.get("gates_task_ids") or [])
    age = await compute_age_fields(ge)

    by_id, successors = await _build_successor_map(tenant_id, ge["project_id"])
    transitive = _bfs_descendants(direct, successors) if direct else set()
    full_set = set(direct) | transitive

    proposal_id = await _latest_approved_proposal_id(tenant_id, ge["project_id"])

    # SLA status
    sla_status = "under_sla"
    cur_age = age.get("current_age_working_days") or 0
    sla = int(ge.get("sla_working_days") or 0)
    if sla > 0:
        if cur_age > sla:
            sla_status = "over_sla"
        elif cur_age == sla:
            sla_status = "at_sla"

    snap = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": ge["project_id"],
        "computed_at": utc_now().isoformat(),
        "gating_entity_id": gating_entity_id,
        "directly_gated_activity_ids": direct,
        "transitively_gated_activity_ids": sorted(transitive),
        "projected_slip_at_n5_days": _project_slip(by_id, full_set, 5, critical_only=False),
        "projected_slip_at_n10_days": _project_slip(by_id, full_set, 10, critical_only=False),
        "projected_slip_at_n20_days": _project_slip(by_id, full_set, 20, critical_only=False),
        "projected_cp_impact_at_n5_days": _project_slip(by_id, full_set, 5, critical_only=True),
        "projected_cp_impact_at_n10_days": _project_slip(by_id, full_set, 10, critical_only=True),
        "projected_cp_impact_at_n20_days": _project_slip(by_id, full_set, 20, critical_only=True),
        "source_schedule_version_id": proposal_id,
        "open_age_working_days": cur_age,
        "sla_status": sla_status,
    }
    snap["severity_band"] = severity_for(snap)
    await db.cascade_snapshots.insert_one(snap.copy())
    return snap


def _state_signature(snap: dict) -> str:
    """Stable hash of the threshold-relevant fields. Used to dedupe
    notifications: if the signature matches the gating_entity's
    last_cascade_notification_state, no fresh notification fires."""
    body = json.dumps({
        "sla_status": snap.get("sla_status"),
        "severity_band": snap.get("severity_band"),
        # Bucket projected_cp_impact at integer days so 5.0 vs 5.01 don't
        # both trigger.
        "cp10": int(snap.get("projected_cp_impact_at_n10_days") or 0),
        "cp20": int(snap.get("projected_cp_impact_at_n20_days") or 0),
    }, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def maybe_notify(tenant_id: str, snap: dict) -> Optional[dict]:
    """Per the C3c notification spec, fire an audit-log notification when
    the gate's threshold-state changes. Notifications are stored as audit
    entries with op='CASCADE_NOTIFICATION' so they show up alongside
    other federal-record events. Returns the entry, or None if no
    state change."""
    db = get_db()
    ge = await db.gating_entities.find_one(
        tenant_filter(tenant_id, {"id": snap["gating_entity_id"]}),
        {"_id": 0, "id": 1, "last_cascade_notification_state": 1, "display_label": 1},
    )
    if not ge:
        return None
    new_sig = _state_signature(snap)
    if ge.get("last_cascade_notification_state") == new_sig:
        return None  # State unchanged — don't spam

    # Decide who to notify based on thresholds.
    targets: list[str] = []
    cp10 = float(snap.get("projected_cp_impact_at_n10_days") or 0)
    cp20 = float(snap.get("projected_cp_impact_at_n20_days") or 0)
    if cp10 > THRESHOLDS["n10_cp_impact_pm"]:
        targets.append("pm")
    if snap.get("sla_status") == "over_sla":
        targets.extend(["pm", "px"])
    if cp20 > THRESHOLDS["n20_cp_impact_exec"]:
        targets.extend(["px", "executive"])
    targets = sorted(set(targets))

    if not targets:
        # No threshold crossed — still record the state hash so future
        # comparisons work, but don't write a notification.
        await db.gating_entities.update_one(
            tenant_filter(tenant_id, {"id": ge["id"]}),
            {"$set": {"last_cascade_notification_state": new_sig}},
        )
        return None

    from audit import write_audit
    entry = await write_audit(
        tenant_id=tenant_id, table_name="cascade_snapshots",
        op="CASCADE_NOTIFICATION",
        record_id=snap["id"], before=None,
        after={
            "gating_entity_id": snap["gating_entity_id"],
            "display_label": ge.get("display_label"),
            "severity_band": snap.get("severity_band"),
            "sla_status": snap.get("sla_status"),
            "projected_cp_impact_at_n10_days": cp10,
            "projected_cp_impact_at_n20_days": cp20,
            "notification_targets": targets,
        },
        actor_id=None, request_id=None,
    )
    await db.gating_entities.update_one(
        tenant_filter(tenant_id, {"id": ge["id"]}),
        {"$set": {"last_cascade_notification_state": new_sig}},
    )
    return entry


async def recompute_all_cascades(tenant_id: str, project_id: str) -> dict:
    """For every open gating_entity in the project, compute a fresh
    cascade snapshot and (if state changed) fire the threshold
    notifications. Returns aggregate stats.

    Bucket C3c Phase 6 adds a batch-level CASCADE_RECOMPUTE_BATCH audit
    entry so cron-driven runs leave a trace even when no per-snapshot
    notification fires (per-snapshot audits are too chatty for nightly
    runs across hundreds of gates)."""
    db = get_db()
    ges = await db.gating_entities.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "status": {"$in": list(OPEN_STATUSES_PROXY)},
        }),
        {"_id": 0, "id": 1},
    ).to_list(2000)

    computed = 0
    notified = 0
    for ge in ges:
        snap = await compute_cascade(tenant_id, ge["id"])
        if not snap:
            continue
        computed += 1
        notif = await maybe_notify(tenant_id, snap)
        if notif:
            notified += 1

    # Batch-level audit -- single entry per recompute so the chain
    # records when the snapshot family was refreshed.
    if ges:
        from audit import write_audit
        try:
            await write_audit(
                tenant_id=tenant_id, table_name="cascade_snapshots",
                op="CASCADE_RECOMPUTE_BATCH",
                record_id=project_id, before=None,
                after={"open_gates": len(ges), "computed": computed,
                       "notified": notified},
                actor_id=None, request_id=None,
            )
        except Exception:
            # Audit failure must not crash the recompute.
            pass

    return {"open_gates": len(ges), "computed": computed, "notified": notified}
