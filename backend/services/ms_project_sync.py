"""MS Project Mode 2 — job queue + result ingest (cloud/backend side).

CPM Mode 2 (2026-06-29). This is the cloud half of the bridge: the backend
enqueues schedule jobs into Mongo; a Windows "MS Project worker" (running on a
box that has MS Project + the Project_mcp COM library) long-polls /claim, drives
Project, and posts results to /results, which call record_result() here.

EVERYTHING here is inert unless a project is on schedule_mode="mode2" AND
FF_SCHEDULE_MODE_2 is on (the only caller, MsProjectEngine.recompute, is reached
only on that path). No existing collection is touched; three new collections are
used:
  schedule_jobs           queue: pending -> claimed -> done|error|parked
  schedule_job_watermark  per-project last_ingested_seq (stale-result rejection)
  schedule_job_seq        per-project monotonic counter

Design contract (from the Mode-2 spec):
  * per-project monotonic `seq`; ingest rejects a result whose seq <= watermark
    (out-of-order / replayed results can't regress the schedule).
  * claim is atomic (find_one_and_update pending->claimed) with a lease; a reaper
    returns expired-lease jobs to pending (worker crash recovery).
  * ingest writes MS Project's computed dates into our EXISTING fields
    (projected_*/is_critical_path/total_float_days/...) using the same
    null-means-unmoved rule as Mode 1, so every downstream surface renders
    unchanged. Matching is our activity_code <-> Project Text1, with the Project
    UniqueID captured as external_task_id for deterministic future matching.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from database import get_db, tenant_filter
from models import gen_id
from services.working_days import working_days_between

# Job lifecycle.
PENDING, CLAIMED, DONE, ERROR, PARKED = "pending", "claimed", "done", "error", "parked"
LEASE_SECONDS = 300          # a claimed job must heartbeat/finish within this
MAX_ATTEMPTS = 3             # after this many reclaim cycles -> parked + alert


def _now() -> datetime:
    return datetime.utcnow()


def _parse(v) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


# --------------------------------------------------------------------------
# Per-project monotonic sequence.
# --------------------------------------------------------------------------
async def _next_seq(db, tenant_id: str, project_id: str) -> int:
    doc = await db.schedule_job_seq.find_one_and_update(
        {"tenant_id": tenant_id, "project_id": project_id},
        {"$inc": {"seq": 1}},
        upsert=True, return_document=True,
    )
    # return_document=True (ReturnDocument.AFTER) returns the incremented doc.
    return int((doc or {}).get("seq", 1))


# --------------------------------------------------------------------------
# PUSH change-set: the field actuals our app owns, for this project's tasks.
# --------------------------------------------------------------------------
async def _build_push_change_set(db, tenant_id: str, project_id: str) -> dict:
    """Snapshot the progress/actuals ICE owns into a worker change-set. We push
    activity_code (stamped into Text1 so the worker can match), pct_complete,
    and actual_start/finish. Dependency edits are NOT pushed here — those only
    flow through an approved schedule proposal (the network is Project-owned).
    Idempotent: pushing the same values is a no-op for Project."""
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "activity_code": 1, "external_task_id": 1,
         "pct_complete": 1, "actual_start": 1, "actual_finish": 1},
    ).to_list(5000)
    updates = []
    for t in tasks:
        code = t.get("activity_code")
        if not code:
            continue
        upd = {
            "external_task_id": t.get("external_task_id"),
            "activity_code": code,
            "percent_complete": t.get("pct_complete"),
            "actual_start": (t.get("actual_start") or None),
            "actual_finish": (t.get("actual_finish") or None),
            "set_text_field": {"Text1": code},
        }
        updates.append(upd)
    return {"task_updates": updates, "dependency_ops": [], "recalc": True,
            "read_back": "full_network"}


async def _project_file_descriptor(db, tenant_id: str, project_id: str) -> dict:
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "id": 1, "ms_project_file": 1},
    ) or {}
    # ms_project_file (optional) lets an admin pin the expected .mpp FullName the
    # worker must have open; absent -> worker uses its configured project file.
    return {"logical_id": project_id, "expected_fullname": proj.get("ms_project_file")}


# --------------------------------------------------------------------------
# Enqueue.
# --------------------------------------------------------------------------
async def _get_sync_state(db, tenant_id: str, project_id: str) -> dict:
    return await db.ms_project_sync_state.find_one(
        {"tenant_id": tenant_id, "project_id": project_id}, {"_id": 0}) or {}


async def _set_sync_state(db, tenant_id: str, project_id: str, fields: dict) -> None:
    await db.ms_project_sync_state.update_one(
        {"tenant_id": tenant_id, "project_id": project_id},
        {"$set": {**fields, "updated_at": _now().isoformat()}}, upsert=True)


async def get_sync_state(tenant_id: str, project_id: str) -> dict:
    """Public read for the frontend banner: {conflict, conflict_at,
    last_rowversion, last_summary}."""
    db = get_db()
    st = await _get_sync_state(db, tenant_id, project_id)
    return {
        "conflict": bool(st.get("conflict")),
        "conflict_at": st.get("conflict_at"),
        "last_rowversion": st.get("last_rowversion"),
        "last_summary": await last_known_summary(tenant_id, project_id),
    }


async def enqueue_push_job(tenant_id, project_id, *, actor_id=None, request_id=None) -> dict:
    db = get_db()
    seq = await _next_seq(db, tenant_id, project_id)
    project_file = await _project_file_descriptor(db, tenant_id, project_id)
    # Conflict check basis: the rowversion we last ingested. The worker compares
    # the live project's rowversion to this; a mismatch = edited outside ICE.
    st = await _get_sync_state(db, tenant_id, project_id)
    project_file["expected_rowversion"] = st.get("last_rowversion")
    job = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "seq": seq,
        "op": "push",
        "status": PENDING,
        "change_set": await _build_push_change_set(db, tenant_id, project_id),
        "project_file": project_file,
        "actor_id": actor_id,
        "request_id": request_id or gen_id(),
        "attempt": 0,
        "issued_at": _now().isoformat(),
        "worker_id": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "result_status": None,
        "error": None,
    }
    await db.schedule_jobs.insert_one(job.copy())
    return {"job_id": job["id"], "seq": seq}


async def enqueue_pull_job(tenant_id, project_id, *, actor_id=None, request_id=None) -> dict:
    """Enqueue a PULL: the worker serializes Project's full network so the
    backend can ingest external edits as a reviewable schedule proposal (Phase 2
    consumes this via the existing proposal/diff flow)."""
    db = get_db()
    seq = await _next_seq(db, tenant_id, project_id)
    job = {
        "id": gen_id(), "tenant_id": tenant_id, "project_id": project_id, "seq": seq,
        "op": "pull", "status": PENDING, "change_set": None,
        "project_file": await _project_file_descriptor(db, tenant_id, project_id),
        "actor_id": actor_id, "request_id": request_id or gen_id(), "attempt": 0,
        "issued_at": _now().isoformat(), "worker_id": None, "claimed_at": None,
        "lease_expires_at": None, "result_status": None, "error": None,
    }
    await db.schedule_jobs.insert_one(job.copy())
    return {"job_id": job["id"], "seq": seq}


# --------------------------------------------------------------------------
# Claim (atomic) + lease renew.
# --------------------------------------------------------------------------
async def claim_next_job(worker_id: str, capabilities: list) -> Optional[dict]:
    """Atomically claim the oldest pending job whose op is in `capabilities`.
    Sets status=claimed, worker_id, lease. Returns the job (sans _id) or None."""
    db = get_db()
    await reap_expired_leases()  # opportunistic recovery before claiming
    now = _now()
    q = {"status": PENDING}
    if capabilities:
        q["op"] = {"$in": list(capabilities)}
    job = await db.schedule_jobs.find_one_and_update(
        q,
        {"$set": {"status": CLAIMED, "worker_id": worker_id,
                  "claimed_at": now.isoformat(),
                  "lease_expires_at": (now + timedelta(seconds=LEASE_SECONDS)).isoformat()},
         "$inc": {"attempt": 1}},
        sort=[("seq", 1)], return_document=True,
    )
    if not job:
        return None
    job.pop("_id", None)
    return job


async def renew_lease(job_id: str) -> bool:
    db = get_db()
    now = _now()
    res = await db.schedule_jobs.update_one(
        {"id": job_id, "status": CLAIMED},
        {"$set": {"lease_expires_at": (now + timedelta(seconds=LEASE_SECONDS)).isoformat()}},
    )
    return res.modified_count == 1


async def reap_expired_leases() -> int:
    """Return claimed jobs whose lease expired to pending (worker crash/offline),
    or park them after MAX_ATTEMPTS. Returns count requeued+parked."""
    db = get_db()
    now_iso = _now().isoformat()
    stuck = await db.schedule_jobs.find(
        {"status": CLAIMED, "lease_expires_at": {"$lt": now_iso}},
        {"_id": 0, "id": 1, "attempt": 1},
    ).to_list(1000)
    n = 0
    for j in stuck:
        if int(j.get("attempt", 0)) >= MAX_ATTEMPTS:
            await db.schedule_jobs.update_one(
                {"id": j["id"]}, {"$set": {"status": PARKED, "error": "lease_expired_max_attempts"}})
        else:
            await db.schedule_jobs.update_one(
                {"id": j["id"]},
                {"$set": {"status": PENDING, "worker_id": None,
                          "claimed_at": None, "lease_expires_at": None}})
        n += 1
    return n


# --------------------------------------------------------------------------
# Result ingest (the correctness core).
# --------------------------------------------------------------------------
async def record_result(payload: dict) -> dict:
    """Handle a worker /results postback. Idempotent + watermark-guarded."""
    db = get_db()
    job_id = payload.get("job_id")
    job = await db.schedule_jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        return {"ok": False, "reason": "unknown_job"}
    if job.get("status") in (DONE, ERROR, PARKED, "conflict"):
        # "conflict" is terminal too — a replayed conflict postback must be a
        # no-op (don't re-jitter conflict_at / re-stamp sync state).
        return {"ok": True, "reason": "already_finalized", "idempotent": True}

    status = payload.get("status")
    tenant_id, project_id, seq = job["tenant_id"], job["project_id"], int(job.get("seq", 0))

    if status == "conflict":
        # Project was edited outside ICE since our last sync (rowversion moved).
        # Do NOT overwrite Project. Flag a project-level conflict; the PM runs a
        # PULL to reconcile via the proposal flow, then re-pushes.
        await _set_sync_state(db, tenant_id, project_id, {
            "conflict": True, "conflict_at": _now().isoformat(),
            "conflict_live_rowversion": payload.get("project_rowversion")})
        await db.schedule_jobs.update_one(
            {"id": job_id},
            {"$set": {"status": "conflict", "result_status": "conflict",
                      "finished_at": _now().isoformat()}})
        return {"ok": True, "reason": "conflict_recorded"}

    if status != "ok":
        await db.schedule_jobs.update_one(
            {"id": job_id},
            {"$set": {"status": ERROR, "result_status": status,
                      "error": payload.get("error"), "finished_at": _now().isoformat()}})
        return {"ok": True, "reason": "worker_error_recorded"}

    # Watermark: reject a stale/out-of-order result (a newer push already landed).
    wm = await db.schedule_job_watermark.find_one(
        {"tenant_id": tenant_id, "project_id": project_id}, {"_id": 0, "last_ingested_seq": 1}) or {}
    if seq <= int(wm.get("last_ingested_seq", 0)):
        await db.schedule_jobs.update_one(
            {"id": job_id}, {"$set": {"status": DONE, "result_status": "stale_skipped",
                                       "finished_at": _now().isoformat()}})
        return {"ok": True, "reason": "stale_skipped", "seq": seq,
                "watermark": wm.get("last_ingested_seq")}

    ingested = 0
    proposal_id = None
    tasks_payload = payload.get("tasks") or []
    if job.get("op") in ("push", "pull"):
        # Both push read-back and pull carry Project's COMPUTED dates; ingest
        # them into our existing tasks (ICE-owned actuals are never touched).
        ingested = await _ingest_push_tasks(
            db, tenant_id, project_id, tasks_payload,
            project_rowversion=payload.get("project_rowversion"),
        )
    if job.get("op") == "pull":
        # Additionally, surface STRUCTURAL changes (added/dropped/dep/duration)
        # as a reviewable proposal via the existing Mode-1 flow.
        try:
            from services.ms_project_ingest import build_pull_proposal
            proposal_id = await build_pull_proposal(
                db, tenant_id, project_id, tasks_payload,
                payload.get("dependencies") or [], actor_id=job.get("actor_id"))
        except Exception:
            proposal_id = None

    # A successful sync clears any standing conflict and records the rowversion
    # we just ingested (the conflict-detection basis for the next push).
    await _set_sync_state(db, tenant_id, project_id, {
        "conflict": False, "conflict_at": None,
        "last_rowversion": payload.get("project_rowversion")})

    # Advance watermark + finalize + summary.
    await db.schedule_job_watermark.update_one(
        {"tenant_id": tenant_id, "project_id": project_id},
        {"$set": {"last_ingested_seq": seq, "updated_at": _now().isoformat()}}, upsert=True)
    summary = {
        "project_finish": payload.get("project_finish"),
        "critical_count": payload.get("critical_count"),
        "ingested_tasks": ingested,
        "ingested_at": _now().isoformat(),
        "seq": seq,
        "op": job.get("op"),
        "proposal_id": proposal_id,
    }
    await db.schedule_jobs.update_one(
        {"id": job_id},
        {"$set": {"status": DONE, "result_status": "ok",
                  "finished_at": _now().isoformat(), "summary": summary}})
    # Single audit for the ingest (mirrors CPM_RECOMPUTE shape).
    try:
        from audit import write_audit
        await write_audit(
            tenant_id=tenant_id, table_name="tasks", op="MS_PROJECT_INGEST",
            record_id=project_id, before=None,
            after={"ingested": ingested, "seq": seq, "job_id": job_id},
            actor_id=job.get("actor_id"), request_id=job.get("request_id") or gen_id())
    except Exception:
        pass
    return {"ok": True, "ingested": ingested, "seq": seq}


async def _ingest_push_tasks(db, tenant_id, project_id, tasks, *, project_rowversion=None) -> int:
    """Write MS Project's computed schedule into our existing Task fields.

    Matching: our activity_code == Project Text1 (worker stamped it); we also
    capture Project UniqueID as external_task_id for deterministic future runs.
    projected_* follow the Mode-1 null-means-unmoved rule (write only when the
    computed date differs from planned), so slip/remaining-float/effective-window
    keep their meaning and every surface renders unchanged. is_critical_path
    comes straight from Project's `critical`; float from Project's total_slack.
    """
    changed = 0
    for mt in tasks:
        code = (mt.get("text1") or "").strip()
        if not code:
            continue
        ours = await db.tasks.find_one(
            tenant_filter(tenant_id, {"project_id": project_id, "activity_code": code}),
            {"_id": 0, "id": 1, "planned_start": 1, "planned_finish": 1})
        if not ours:
            continue
        planned_finish = _parse(ours.get("planned_finish"))
        planned_start = _parse(ours.get("planned_start"))
        comp_start = _parse(mt.get("start"))
        comp_finish = _parse(mt.get("finish"))

        # Conditional projected_* (B3): only when moved vs planned.
        moved = bool(
            (comp_finish and planned_finish and comp_finish != planned_finish)
            or (comp_start and planned_start and comp_start != planned_start))
        projected_start = comp_start.isoformat() if (moved and comp_start) else None
        projected_finish = comp_finish.isoformat() if (moved and comp_finish) else None

        slip_days = None
        if projected_finish and planned_finish and comp_finish:
            slip_days = working_days_between(planned_finish, comp_finish)

        slack = mt.get("total_slack_days")
        total_float = int(round(slack)) if isinstance(slack, (int, float)) else None
        is_currently_critical = (slack <= 0) if isinstance(slack, (int, float)) else None

        # Defensive: external_task_id is typed Optional[int]; serialize_task
        # returns COM UniqueID (an int) but coerce so a non-int can't make a
        # later GET /tasks (response_model=Task) 422.
        _uid = mt.get("unique_id")
        try:
            _uid = int(_uid) if _uid is not None else None
        except (TypeError, ValueError):
            _uid = None

        await db.tasks.update_one(
            tenant_filter(tenant_id, {"id": ours["id"]}),
            {"$set": {
                "external_system": "ms_project",
                "external_task_id": _uid,
                "external_rowversion": project_rowversion,
                "external_last_modified_at": _now().isoformat(),
                "projected_start_date": projected_start,
                "projected_finish_date": projected_finish,
                "slip_days": slip_days,
                "is_critical_path": bool(mt.get("critical")),
                "total_float_days": total_float,
                "remaining_float_days": total_float,
                "is_currently_critical": is_currently_critical,
                "updated_at": _now().isoformat(),
            }})
        changed += 1
    return changed


async def last_known_summary(tenant_id: str, project_id: str) -> Optional[dict]:
    """The most recent ingested summary for the {stale} field on the recompute
    ack, so the UI can show 'as of <time>' while a fresh job is in flight."""
    db = get_db()
    j = await db.schedule_jobs.find_one(
        {"tenant_id": tenant_id, "project_id": project_id,
         "status": DONE, "result_status": "ok"},
        {"_id": 0, "summary": 1}, sort=[("seq", -1)])
    return (j or {}).get("summary")


async def get_job_status(tenant_id: str, project_id: str, job_id: str) -> Optional[dict]:
    db = get_db()
    j = await db.schedule_jobs.find_one(
        {"id": job_id, "tenant_id": tenant_id, "project_id": project_id},
        {"_id": 0, "id": 1, "status": 1, "result_status": 1, "seq": 1,
         "error": 1, "summary": 1, "issued_at": 1, "finished_at": 1})
    return j
