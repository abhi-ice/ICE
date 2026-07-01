"""MS Project Mode 2 — internal worker endpoints + frontend status. CPM Mode 2.

Two worker endpoints (authenticated by a dedicated worker bearer token, NOT a
user JWT) and one frontend status endpoint (normal tenant auth):

  POST /api/internal/ms-project/claim    worker long-polls for the next job
  POST /api/internal/ms-project/results  worker posts a job result (ingested here)
  GET  /api/projects/{project_id}/recompute-status/{job_id}   frontend polling

Safe by default: the worker endpoints require MS_PROJECT_WORKER_TOKEN to be set
(503 otherwise), and no jobs ever exist unless a project is on
schedule_mode="mode2" with FF_SCHEDULE_MODE_2 on. So merely deploying this router
to Emergent changes nothing for existing tenants.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response

from auth import require_tenant
from services import ms_project_sync as mps

router = APIRouter(tags=["ms-project-internal"])


async def require_worker(authorization: Optional[str] = Header(default=None)) -> dict:
    """Authenticate the MS Project worker by a shared bearer token (env
    MS_PROJECT_WORKER_TOKEN). Distinct from user auth: the worker is a machine
    agent, not a tenant user."""
    token = os.environ.get("MS_PROJECT_WORKER_TOKEN")
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
    """Heartbeat a long-running claimed job so the reaper doesn't requeue it."""
    job_id = payload.get("job_id")
    if not job_id:
        raise HTTPException(status_code=422, detail="job_id required")
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


@router.get("/projects/{project_id}/ms-project/sync-state")
async def sync_state(project_id: str, ctx: dict = Depends(require_tenant)):
    """Frontend banner data: standing conflict + last-known summary."""
    return await mps.get_sync_state(ctx["tenant_id"], project_id)
