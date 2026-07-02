"""MS Project Mode 2 — internal worker endpoints + frontend status. CPM Mode 2.

Two worker endpoints (authenticated by a dedicated worker bearer token, NOT a
user JWT) and one frontend status endpoint (normal tenant auth):

  POST /api/internal/ms-project/claim    worker long-polls for the next job
  POST /api/internal/ms-project/results  worker posts a job result (ingested here)
  GET  /api/projects/{project_id}/recompute-status/{job_id}   frontend polling

File pipeline (2026-07-02): the project's .mpp is hosted in ICE object storage.
  POST /api/projects/{id}/ms-project/file            PM/Admin uploads a version
  GET  /api/projects/{id}/ms-project/file            any member downloads it
  POST /api/projects/{id}/ms-project/generate-file   build the file from ICE tasks
  GET  /api/internal/ms-project/file/{job_id}        worker downloads per job
  POST /api/internal/ms-project/file/{job_id}        worker uploads the new version

Safe by default: the worker endpoints require MS_PROJECT_WORKER_TOKEN to be set
(503 otherwise), and no jobs ever exist unless a project is on
schedule_mode="mode2" with FF_SCHEDULE_MODE_2 on. So merely deploying this router
to Emergent changes nothing for existing tenants.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Request, Response, UploadFile

from audit import write_audit
from auth import require_tenant
from database import get_db, tenant_filter
from models import utc_now
from services import ms_project_sync as mps
from services import runtime_settings
from services import storage

try:  # pymongo ships with motor in production; local tests stub the DB layer.
    from pymongo import ReturnDocument
    RETURN_AFTER = ReturnDocument.AFTER
except ModuleNotFoundError:  # pragma: no cover
    RETURN_AFTER = True  # pymongo defines ReturnDocument.AFTER == True

router = APIRouter(tags=["ms-project-internal"])

# File pipeline (2026-07-02) -- the project's .mpp is hosted in object storage;
# every store is a NEW object + NEW files row (kind='msp'), never an overwrite.
MSP_MAX_BYTES = 75 * 1024 * 1024
MSP_CONTENT_TYPE = "application/vnd.ms-project"
WORKER_ONLINE_WINDOW_SECONDS = 120

_POINTER_FIELDS = (
    "ms_project_file_id", "ms_project_file_name", "ms_project_file_rev",
    "ms_project_file_updated_at", "ms_project_file_source", "ms_project_file_sha",
)


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _declared_length(value) -> Optional[int]:
    """The declared Content-Length as an int, or None when absent. Through
    FastAPI the header arrives parsed; a direct (test) call leaves the Header
    default object in place -- anything non-numeric counts as absent."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _download_filename(name) -> str:
    """Content-Disposition-safe filename. Response headers are latin-1, so a
    non-ASCII stored name (or embedded quotes/control chars, which would break
    or inject into the header) must never reach the header raw. Strips quotes,
    backslashes, and control characters; falls back to plain 'schedule.mpp'
    when what's left isn't pure ASCII (or nothing is left)."""
    cleaned = "".join(c for c in str(name or "") if ord(c) >= 32 and c not in '"\\')
    cleaned = cleaned.strip()
    if not cleaned:
        return "schedule.mpp"
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError:
        return "schedule.mpp"
    return cleaned


async def _store_msp_version(db, tenant_id: str, project: dict, data: bytes, *,
                             filename: str, source: str, actor_id=None, job_id=None) -> dict:
    """Store a NEW .mpp version and advance the project's pointer fields.

    The rev is allocated ATOMICALLY: one find_one_and_update both $inc's
    ms_project_file_rev and $set's the other pointer fields, so two concurrent
    stores can never compute the same rev from a stale read or move the pointer
    backwards ($inc on an absent field yields 1 -- correct for the first
    store). record_file doesn't take extra fields, so the version metadata
    (sha/rev/source/job_id) is stamped onto the files row right after.
    Returns the new pointer fields."""
    sha = _sha12(data)
    path = storage.make_path(tenant_id, "msp", "mpp")
    try:
        # storage.put_object is synchronous (requests) -- keep it off the loop.
        await asyncio.to_thread(storage.put_object, path, data, MSP_CONTENT_TYPE)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"storage: {e}")
    rec = await storage.record_file(
        tenant_id=tenant_id, storage_path=path, original_filename=filename,
        content_type=MSP_CONTENT_TYPE, size=len(data), kind="msp", actor_id=actor_id)
    updated_at = utc_now().isoformat()
    doc = await db.projects.find_one_and_update(
        tenant_filter(tenant_id, {"id": project["id"]}),
        {"$inc": {"ms_project_file_rev": 1},
         "$set": {
             "ms_project_file_id": rec["id"],
             "ms_project_file_name": filename,
             "ms_project_file_updated_at": updated_at,
             "ms_project_file_source": source,
             "ms_project_file_sha": sha,
         }},
        return_document=RETURN_AFTER)
    rev = int((doc or {}).get("ms_project_file_rev") or 1)
    extra = {"sha12": sha, "project_id": project["id"], "rev": rev, "source": source}
    if job_id:
        # Lets record_result's rev guard recognize the worker's own upload.
        extra["job_id"] = job_id
    await db.files.update_one({"id": rec["id"]}, {"$set": extra})
    return {
        "ms_project_file_id": rec["id"],
        "ms_project_file_name": filename,
        "ms_project_file_rev": rev,
        "ms_project_file_updated_at": updated_at,
        "ms_project_file_source": source,
        "ms_project_file_sha": sha,
    }


async def require_worker(authorization: Optional[str] = Header(default=None)) -> dict:
    """Authenticate the MS Project worker by a shared bearer token. The token
    generated from Settings -> Microsoft Project connector (stored in runtime
    settings) wins; env MS_PROJECT_WORKER_TOKEN is the fallback so pre-UI
    deployments keep working. Distinct from user auth: the worker is a machine
    agent, not a tenant user."""
    token = runtime_settings.worker_token() or os.environ.get("MS_PROJECT_WORKER_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="MS Project worker integration not configured")
    if not authorization or authorization.strip() != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid MS Project worker token")
    return {"worker": True}


@router.post("/internal/ms-project/claim")
async def claim_job(payload: dict = Body(default={}), _w: dict = Depends(require_worker)):
    """Long-poll for the next job. Returns a job dict, or 204 when none within
    the wait window. The worker calls this in a loop."""
    worker_id = str(payload.get("worker_id") or "worker")
    capabilities = payload.get("capabilities") or ["push", "pull", "validate"]
    await runtime_settings.touch_worker(worker_id)
    wait_ms = max(0, min(int(payload.get("wait_ms") or 0), 25000))
    deadline = time.monotonic() + (wait_ms / 1000.0)
    while True:
        job = await mps.claim_next_job(worker_id, capabilities)
        if job is not None:
            return job
        if time.monotonic() >= deadline:
            return Response(status_code=204)
        await asyncio.sleep(2)


@router.post("/internal/ms-project/results")
async def post_result(payload: dict = Body(...), _w: dict = Depends(require_worker)):
    """Worker posts a job result. Ingest is idempotent + watermark-guarded."""
    if not payload.get("job_id"):
        raise HTTPException(status_code=422, detail="job_id required")
    return await mps.record_result(payload)


@router.post("/internal/ms-project/renew-lease")
async def renew(payload: dict = Body(...), _w: dict = Depends(require_worker)):
    """Heartbeat a long-running claimed job so the reaper doesn't requeue it.
    Also refreshes the worker-liveness stamp: during a long job the worker
    renews instead of claiming, and the Settings/banner 'worker online' light
    must stay green the whole time."""
    job_id = payload.get("job_id")
    if not job_id:
        raise HTTPException(status_code=422, detail="job_id required")
    await runtime_settings.touch_worker(str(payload.get("worker_id") or "worker"))
    return {"renewed": await mps.renew_lease(job_id)}


@router.get("/projects/{project_id}/recompute-status/{job_id}")
async def recompute_status(project_id: str, job_id: str, ctx: dict = Depends(require_tenant)):
    """Frontend polls this after a Mode-2 recompute returned {status:'queued'}."""
    j = await mps.get_job_status(ctx["tenant_id"], project_id, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/projects/{project_id}/ms-project/pull")
async def trigger_pull(project_id: str, ctx: dict = Depends(require_tenant)):
    """'Sync from MS Project' — enqueue a PULL so the worker brings Project's
    current schedule into ICE (computed dates ingested; structural changes
    surfaced as a reviewable proposal). PM/Admin only."""
    if (ctx["user"].get("role") or "").lower() not in ("admin", "pm"):
        raise HTTPException(status_code=403, detail="Only PM or Admin can sync from MS Project")
    return await mps.enqueue_pull_job(
        ctx["tenant_id"], project_id, actor_id=ctx["user"]["id"], request_id=ctx["request_id"])


@router.post("/projects/{project_id}/ms-project/file")
async def upload_ms_project_file(
    project_id: str,
    file: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
    content_length: Optional[int] = Header(default=None),
):
    """Upload the project's Microsoft Project file (.mpp). Every upload is a
    NEW version (rev+1) -- the previous object is never overwritten. After the
    store, the first sync is queued automatically: a project that already has
    tasks pushes ICE's actuals into the file; an empty project pulls the file's
    network in as its schedule. PM/Admin only."""
    if (ctx["user"].get("role") or "").lower() not in ("admin", "pm"):
        raise HTTPException(status_code=403, detail="Only PM or Admin can upload the Microsoft Project file")
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.get("schedule_mode") != "mode2":
        raise HTTPException(
            status_code=409,
            detail="This project is not on the Microsoft Project engine, so it has no file to manage.")
    filename = file.filename or ""
    if not filename.lower().endswith(".mpp"):
        raise HTTPException(status_code=400, detail="Only Microsoft Project files (.mpp) are accepted")
    # Cheap size pre-check on the declared Content-Length so an oversized
    # request is refused before the body is read (the post-read check below
    # still guards a lying/absent header).
    declared = _declared_length(content_length)
    if declared is not None and declared > MSP_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than the 75MB limit")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(data) > MSP_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than the 75MB limit")

    # An upload always wins over a queued (not-yet-claimed) generation: flip
    # any pending 'create' job to error NOW so an offline worker's stale queue
    # can never displace this file later. (Claimed/in-flight create jobs are
    # fenced at the worker file POST instead.)
    await db.schedule_jobs.update_many(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "op": "create", "status": "pending"},
        {"$set": {
            "status": "error",
            "result_status": "superseded_by_upload",
            "error": {"code": "superseded_by_upload",
                      "message": "A file was uploaded before this generation job ran — keeping the uploaded file."},
            "finished_at": utc_now().isoformat(),
        }})

    before = {k: proj.get(k) for k in _POINTER_FIELDS}
    pointer = await _store_msp_version(
        db, ctx["tenant_id"], proj, data,
        filename=filename, source="upload", actor_id=ctx["user"]["id"])

    # AUTO-FIRST-SYNC: queue the right first job so no manual step is needed.
    auto_job = None
    try:
        has_task = await db.tasks.find_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}}),
            {"_id": 0, "id": 1})
        if has_task:
            enq = await mps.enqueue_push_job(
                ctx["tenant_id"], project_id, actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
            auto_job = {"op": "push", "job_id": enq["job_id"]}
        else:
            enq = await mps.enqueue_pull_job(
                ctx["tenant_id"], project_id, actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
            auto_job = {"op": "pull", "job_id": enq["job_id"]}
    except Exception:
        auto_job = None  # upload succeeded; the user can sync manually

    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects", op="UPDATE",
        record_id=project_id, before=before, after=pointer,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return {
        "file": {
            "id": pointer["ms_project_file_id"],
            "name": pointer["ms_project_file_name"],
            "rev": pointer["ms_project_file_rev"],
            "updated_at": pointer["ms_project_file_updated_at"],
            "source": pointer["ms_project_file_source"],
        },
        "auto_job": auto_job,
    }


@router.get("/projects/{project_id}/ms-project/file")
async def download_ms_project_file(project_id: str, ctx: dict = Depends(require_tenant)):
    """Download the CURRENT stored version of the project's Microsoft Project
    file. Any project member can download."""
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "ms_project_file_id": 1, "ms_project_file_name": 1})
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if not proj.get("ms_project_file_id"):
        raise HTTPException(status_code=404, detail="No Microsoft Project file stored for this project yet")
    row = await db.files.find_one(
        tenant_filter(ctx["tenant_id"], {"id": proj["ms_project_file_id"]}), {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="No Microsoft Project file stored for this project yet")
    try:
        # storage.get_object is synchronous (requests) -- keep it off the loop.
        data, _ct = await asyncio.to_thread(storage.get_object, row["storage_path"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"storage: {e}")
    name = _download_filename(
        proj.get("ms_project_file_name") or row.get("original_filename") or "project.mpp")
    return Response(
        content=data, media_type=MSP_CONTENT_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/projects/{project_id}/ms-project/generate-file")
async def generate_ms_project_file(project_id: str, ctx: dict = Depends(require_tenant)):
    """Generate a Microsoft Project file FROM ICE's tasks (worker 'create' op)
    for a mode2 project that has a schedule but no stored file yet. PM/Admin
    only."""
    if (ctx["user"].get("role") or "").lower() not in ("admin", "pm"):
        raise HTTPException(status_code=403, detail="Only PM or Admin can generate the Microsoft Project file")
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "schedule_mode": 1, "ms_project_file_id": 1})
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.get("schedule_mode") != "mode2":
        raise HTTPException(
            status_code=409,
            detail="This project is not on the Microsoft Project engine, so there is nothing to generate.")
    if proj.get("ms_project_file_id"):
        raise HTTPException(
            status_code=409,
            detail="This project already has a Microsoft Project file. Upload a new version instead of generating one.")
    # One in-flight sync at a time: a second Generate click (or a queued
    # push/pull) must not stack a duplicate create job.
    active = await db.schedule_jobs.find_one(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "status": {"$in": ["pending", "claimed"]}},
        {"_id": 0, "id": 1})
    if active:
        raise HTTPException(
            status_code=409,
            detail="A sync is already in progress — wait for it to finish.")
    rows = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "activity_code": 1}).to_list(5000)
    if not any((r.get("activity_code") or "").strip() for r in rows):
        raise HTTPException(
            status_code=409,
            detail="Add at least one schedule task with an activity code before generating a file.")
    return await mps.enqueue_create_job(
        ctx["tenant_id"], project_id, actor_id=ctx["user"]["id"], request_id=ctx["request_id"])


def _worker_online() -> bool:
    """True when the worker's last claim heartbeat is fresh enough that a
    queued job should start within seconds."""
    last = runtime_settings.worker_status().get("last_seen")
    if not last:
        return False
    try:
        seen = datetime.fromisoformat(str(last))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() <= WORKER_ONLINE_WINDOW_SECONDS


@router.get("/projects/{project_id}/ms-project/sync-state")
async def sync_state(project_id: str, ctx: dict = Depends(require_tenant)):
    """Frontend banner data: standing conflict + last-known summary, plus
    (file pipeline, 2026-07-02) the hosted file version, any in-flight job,
    and whether the worker is online."""
    st = await mps.get_sync_state(ctx["tenant_id"], project_id)
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"_id": 0, "ms_project_file_id": 1, "ms_project_file_name": 1,
         "ms_project_file_rev": 1, "ms_project_file_updated_at": 1,
         "ms_project_file_source": 1}) or {}
    st["file"] = ({
        "name": proj.get("ms_project_file_name"),
        "rev": proj.get("ms_project_file_rev"),
        "updated_at": proj.get("ms_project_file_updated_at"),
        "source": proj.get("ms_project_file_source"),
    } if proj.get("ms_project_file_id") else None)
    active = await db.schedule_jobs.find_one(
        {"tenant_id": ctx["tenant_id"], "project_id": project_id,
         "status": {"$in": ["pending", "claimed"]}},
        {"_id": 0, "id": 1, "op": 1, "status": 1, "issued_at": 1},
        sort=[("seq", -1)])
    st["active_job"] = ({
        "id": active["id"], "op": active.get("op"), "status": active.get("status"),
        "created_at": active.get("issued_at"),
    } if active else None)
    st["worker_online"] = _worker_online()
    return st


# --------------------------------------------------------------------------
# Worker file transfer (file pipeline, 2026-07-02). Raw bytes both ways; the
# worker downloads the current version per claimed job and uploads the updated
# file back BEFORE posting results.
# --------------------------------------------------------------------------
async def _claimed_job_or_409(db, job_id: str) -> dict:
    job = await db.schedule_jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("status") != "claimed":
        raise HTTPException(status_code=409, detail="job is not claimed")
    return job


@router.get("/internal/ms-project/file/{job_id}")
async def worker_download_file(job_id: str, _w: dict = Depends(require_worker)):
    """Stream the CURRENT stored .mpp for a claimed job. X-File-Sha/X-File-Rev
    let the worker verify download integrity against the job's descriptor.
    404 when the project has no stored file (worker falls back to the legacy
    local path or errors 'project_file_not_found')."""
    db = get_db()
    job = await _claimed_job_or_409(db, job_id)
    proj = await db.projects.find_one(
        tenant_filter(job["tenant_id"], {"id": job["project_id"]}),
        {"_id": 0, "ms_project_file_id": 1, "ms_project_file_rev": 1,
         "ms_project_file_sha": 1})
    if not proj or not proj.get("ms_project_file_id"):
        raise HTTPException(status_code=404, detail="project has no stored file")
    row = await db.files.find_one(
        tenant_filter(job["tenant_id"], {"id": proj["ms_project_file_id"]}), {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="project has no stored file")
    try:
        # storage.get_object is synchronous (requests) -- keep it off the loop.
        data, _ct = await asyncio.to_thread(storage.get_object, row["storage_path"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"storage: {e}")
    return Response(content=data, media_type=MSP_CONTENT_TYPE, headers={
        "X-File-Sha": proj.get("ms_project_file_sha") or _sha12(data),
        "X-File-Rev": str(proj.get("ms_project_file_rev") or 0),
    })


@router.post("/internal/ms-project/file/{job_id}")
async def worker_upload_file(
    job_id: str,
    request: Request,
    x_file_sha: Optional[str] = Header(default=None),
    _w: dict = Depends(require_worker),
    content_length: Optional[int] = Header(default=None),
):
    """Store the worker's updated .mpp as a NEW version for a claimed job.
    Body is raw bytes; X-File-Sha is the worker-computed sha12, recomputed
    here (422 on mismatch) so a corrupted upload can never become the current
    version. The files row carries this job_id -- record_result's rev guard
    uses it to allow the worker's own rev bump.

    SUPERSEDED-BASE FENCES (409): the worker's file was built from the file
    base its job carried. If a user upload changed that base mid-job, the
    worker's bytes must NOT become the current version -- the worker treats
    409 (like 413/422) as permanent and posts an error result with code
    'file_upload_rejected'."""
    db = get_db()
    job = await _claimed_job_or_409(db, job_id)
    # Cheap size pre-check on the declared Content-Length so an oversized
    # upload is refused before the body is read (post-read check still runs).
    declared = _declared_length(content_length)
    if declared is not None and declared > MSP_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than the 75MB limit")
    proj = await db.projects.find_one(
        tenant_filter(job["tenant_id"], {"id": job["project_id"]}), {"_id": 0})
    if not proj:
        raise HTTPException(status_code=404, detail="project not found")
    if job.get("op") == "create" and proj.get("ms_project_file_id") is not None:
        # The create job was enqueued when NO file existed; one exists now.
        raise HTTPException(
            status_code=409,
            detail="A file was uploaded while this one was being generated — keeping the uploaded file.")
    base_file_id = (job.get("project_file") or {}).get("file_id")
    if job.get("op") != "create" and base_file_id and base_file_id != proj.get("ms_project_file_id"):
        raise HTTPException(
            status_code=409,
            detail="A newer file version was uploaded while this sync ran.")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(data) > MSP_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than the 75MB limit")
    if (x_file_sha or "").strip().lower() != _sha12(data):
        raise HTTPException(status_code=422, detail="X-File-Sha does not match the uploaded bytes")
    source = "generated" if job.get("op") == "create" else "worker_push"
    filename = proj.get("ms_project_file_name") or f"{(proj.get('name') or 'project')}.mpp"
    pointer = await _store_msp_version(
        db, job["tenant_id"], proj, data,
        filename=filename, source=source, actor_id=job.get("actor_id"), job_id=job_id)
    return {"rev": pointer["ms_project_file_rev"]}
