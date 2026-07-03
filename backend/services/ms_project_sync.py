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
  * PUSH carries ICE-logged schedule impacts (2026-07-03): every task_update
    includes `impact_days_total` — the working-day sum of the task's OPEN
    blockers (open/in_progress delays + draft/sent/in_review RFIs + over-SLA
    submittals, the exact total the internal impact-days engine computes via
    task_status._open_blocker_impact_for_task), and it is 0 when nothing is
    pushing. The worker applies the DELTA versus its in-file ledger, Microsoft
    Project cascades natively, and the read-back brings the recomputed
    dates/floats home — closing a delay flows a lower total on the next push
    and the schedule reverts.
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
from models import gen_id, pred_code, pred_relationship
from services.feature_flags import schedule_mode_2_enabled
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
    actual_start/finish, and impact_days_total (2026-07-03) — the working-day
    sum of the task's OPEN schedule blockers, straight from task_status.
    _open_blocker_impact_for_task so the pushed number is exactly what the
    internal impact-days engine would apply. The key is present on EVERY
    update (explicit 0 when nothing is pushing) because the worker reconciles
    against its in-file ledger: the 0 is what lets it REVERT a previously
    applied impact once the delay/RFI closes. Dependency edits are NOT pushed
    here — those only flow through an approved schedule proposal (the network
    is Project-owned). Idempotent: pushing the same values is a no-op for
    Project."""
    # Lazy import (this module's convention for cross-service deps): keeps the
    # ms_project_sync <-> task_status pairing free of a module-load cycle.
    from services.task_status import _open_blocker_impact_for_task
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
        impact = await _open_blocker_impact_for_task(db, tenant_id, t["id"])
        upd = {
            "external_task_id": t.get("external_task_id"),
            "activity_code": code,
            "percent_complete": t.get("pct_complete"),
            "actual_start": (t.get("actual_start") or None),
            "actual_finish": (t.get("actual_finish") or None),
            "impact_days_total": int(round(impact)),
            "set_text_field": {"Text1": code},
        }
        updates.append(upd)
    return {"task_updates": updates, "dependency_ops": [], "recalc": True,
            "read_back": "full_network"}


async def _project_file_descriptor(db, tenant_id: str, project_id: str) -> dict:
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "id": 1, "ms_project_file": 1, "ms_project_file_id": 1,
         "ms_project_file_rev": 1, "ms_project_file_sha": 1},
    ) or {}
    # ms_project_file (optional, legacy) lets an admin pin the expected .mpp
    # FullName the worker must have open; absent -> worker uses its configured
    # project file. File pipeline (2026-07-02): file_id/file_rev/file_sha
    # describe the CURRENT hosted version -- when file_id is set the worker
    # downloads it per job (verifying sha) instead of using a local file.
    return {
        "logical_id": project_id,
        "expected_fullname": proj.get("ms_project_file"),
        "file_id": proj.get("ms_project_file_id"),
        "file_rev": proj.get("ms_project_file_rev"),
        "file_sha": proj.get("ms_project_file_sha"),
    }


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


async def enqueue_create_job(tenant_id, project_id, *, actor_id=None, request_id=None) -> dict:
    """Enqueue a CREATE: the worker builds a brand-new .mpp from ICE's tasks
    (codes, dates, durations, links, milestones), uploads it as the project's
    hosted file, then posts a push-style read-back so ingest stamps external
    ids. Used when a mode2 project has tasks but no stored file yet."""
    db = get_db()
    seq = await _next_seq(db, tenant_id, project_id)
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "name": 1, "start_date": 1}) or {}
    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "activity_code": 1, "description": 1, "duration_days": 1,
         "planned_start": 1, "planned_finish": 1, "is_milestone": 1, "predecessors": 1},
    ).to_list(5000)
    rows = []
    for t in tasks:
        code = t.get("activity_code")
        if not code:
            # A task without an activity_code can't be matched back on ingest
            # (no Text1 key) -- leave it out of the generated file.
            continue
        preds = []
        for p in (t.get("predecessors") or []):
            # Same predecessor representation the rest of the codebase uses:
            # pred_code/pred_relationship normalize legacy bare strings and
            # partial dicts to (code, FS/SS/FF/SF, lag_days).
            pcode = pred_code(p)
            if not pcode:
                continue
            rel, lag = pred_relationship(p)
            preds.append({"code": pcode, "type": rel, "lag_days": lag})
        dur = t.get("duration_days")
        try:
            # int days for Project's Duration field; 0 stays 0 (milestones).
            dur = int(round(float(dur))) if dur is not None else 1
        except (TypeError, ValueError):
            dur = 1
        rows.append({
            "activity_code": code,
            "name": t.get("description") or code,
            "duration_days": dur,
            "planned_start": t.get("planned_start"),
            "planned_finish": t.get("planned_finish"),
            "is_milestone": bool(t.get("is_milestone")),
            "predecessors": preds,
        })
    rows.sort(key=lambda r: (r["planned_start"] is None, str(r["planned_start"] or "")))
    start = proj.get("start_date")
    if not start:
        starts = [r["planned_start"] for r in rows if r["planned_start"]]
        start = min(starts) if starts else None
    job = {
        "id": gen_id(), "tenant_id": tenant_id, "project_id": project_id, "seq": seq,
        "op": "create", "status": PENDING,
        "change_set": {
            "project": {"name": proj.get("name"),
                        "start_date": (str(start)[:10] if start else None)},
            "tasks": rows,
        },
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

    # ENGINE FENCE (file pipeline, 2026-07-02): if the project is no longer on
    # mode2 by the time the result lands (project deleted counts too), OR an
    # admin turned the Microsoft Project connector OFF in Settings (the global
    # FF_SCHEDULE_MODE_2 kill-switch), the result belongs to an engine that no
    # longer drives this project. Finalize quietly -- no ingest, no sync-state
    # writes -- so a late worker postback can't touch it.
    proj = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "schedule_mode": 1, "ms_project_file_id": 1, "ms_project_file_rev": 1},
    ) or {}
    if not (schedule_mode_2_enabled() and proj.get("schedule_mode") == "mode2"):
        await db.schedule_jobs.update_one(
            {"id": job_id},
            {"$set": {"status": DONE, "result_status": "skipped_engine_changed",
                      "finished_at": _now().isoformat()}})
        return {"ok": True, "skipped": "engine_changed"}

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
        # SELF-HEAL (file pipeline, 2026-07-02): 'file_upload_rejected' means
        # the worker's file POST hit a superseded-base fence (a user uploaded
        # a newer version mid-job). For a PUSH that just means our actuals
        # never landed in the NEW file -- re-enqueue a fresh push against the
        # current version (fresh descriptor). Create/pull results are moot
        # against the new file: no re-enqueue.
        err = payload.get("error") or {}
        if (job.get("op") == "push" and isinstance(err, dict)
                and err.get("code") == "file_upload_rejected"):
            requeued = await enqueue_push_job(tenant_id, project_id, actor_id=job.get("actor_id"))
            return {"ok": True, "reason": "worker_error_recorded",
                    "requeued_job_id": requeued["job_id"]}
        return {"ok": True, "reason": "worker_error_recorded"}

    # REV GUARD (file pipeline, 2026-07-02; hosted mode only): the result was
    # computed against the file version the job carried. If the stored file
    # moved on since (a user re-uploaded mid-job), those numbers describe a
    # stale file -- skip the ingest and immediately re-enqueue a push so the
    # sync self-heals against the NEW version. Allowance: the worker's own
    # file upload during THIS job bumps the rev too (files row stamped with
    # this job_id) -- that one is exactly what we want to ingest.
    pf = job.get("project_file") or {}
    if job.get("op") == "create":
        # CREATE REV GUARD (file pipeline, 2026-07-02): a create job's ok
        # result may only be ingested when the CURRENT files row was uploaded
        # by THIS job. Anything else means a real file landed some other way
        # (user upload won the race, or the worker never uploaded) -- the
        # generated numbers describe a file that isn't the project's file.
        # Finalize DONE without re-enqueue: a real file exists, generation is
        # moot (the upload path already queued its own first sync).
        cur_row = None
        if proj.get("ms_project_file_id"):
            cur_row = await db.files.find_one(
                tenant_filter(tenant_id, {"id": proj["ms_project_file_id"]}),
                {"_id": 0, "job_id": 1})
        if (cur_row or {}).get("job_id") != job_id:
            await db.schedule_jobs.update_one(
                {"id": job_id},
                {"$set": {"status": DONE, "result_status": "stale_file_rev",
                          "finished_at": _now().isoformat()}})
            return {"ok": True, "reason": "stale_file_rev"}
    elif pf.get("file_id"):
        cur_row = None
        if proj.get("ms_project_file_id"):
            cur_row = await db.files.find_one(
                tenant_filter(tenant_id, {"id": proj["ms_project_file_id"]}),
                {"_id": 0, "job_id": 1})
        allowed = (proj.get("ms_project_file_rev") == pf.get("file_rev")
                   or (cur_row or {}).get("job_id") == job_id)
        if not allowed:
            await db.schedule_jobs.update_one(
                {"id": job_id},
                {"$set": {"status": DONE, "result_status": "stale_file_rev",
                          "finished_at": _now().isoformat()}})
            requeued = await enqueue_push_job(tenant_id, project_id, actor_id=job.get("actor_id"))
            return {"ok": True, "reason": "stale_file_rev",
                    "requeued_job_id": requeued["job_id"]}

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
    if job.get("op") in ("push", "pull", "create"):
        # Push read-back, pull, and create read-back all carry Project's
        # COMPUTED dates; ingest them into our existing tasks (ICE-owned
        # actuals are never touched). A create's file upload already happened
        # via the worker file endpoint before this postback.
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
