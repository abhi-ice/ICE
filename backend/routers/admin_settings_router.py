"""Admin runtime settings (2026-07-02) -- the UI home for what used to be
env-var-only switches. Powers two Settings cards:

  Scheduling features        GET/PATCH /api/admin/runtime-settings
  Microsoft Project connector  POST .../msp-token/rotate  (token shown ONCE)
                               GET  /api/admin/msp-worker/download (zip)

GET is admin-only (role_required). PATCH / rotate / download are limited to
PLATFORM_ADMIN_EMAILS when that env var is set (else admin-only), because the
settings are global across every company. The GET never returns the worker token value --
only whether one is set and when the worker last checked in.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from auth import require_tenant, role_required
from services import runtime_settings

router = APIRouter(tags=["admin-settings"])

# What each toggle means, in plain language -- served to the UI so the copy
# lives in one place.
FLAG_META = [
    {
        "key": "FF_SLIDER_GATING",
        "label": "Progress slider gating",
        "help": "Blocks moving an activity's progress slider past what its predecessor activities allow. Project managers and admins can override with a reason.",
    },
    {
        "key": "FF_PROGRESS_CASCADE",
        "label": "No-progress auto-slip",
        "help": "When an activity falls behind its plan, its finish date slides out automatically and the delay flows to the activities that depend on it.",
    },
    {
        "key": "FF_ENTERPRISE_CPM",
        "label": "ICE Enterprise scheduling engine",
        "help": "Allows projects to use the advanced scheduling engine (late dates, float, constraint types). Each project still chooses its engine in the project's Schedule engine setting.",
    },
    {
        "key": "FF_SCHEDULE_MODE_2",
        "label": "Microsoft Project connector",
        "help": "Allows projects to be scheduled by Microsoft Project through the ICE worker running on a Windows machine. Each project still opts in via its Schedule engine setting.",
    },
]

_ALLOWED = {m["key"] for m in FLAG_META}


def _platform_admin_emails() -> set[str]:
    raw = os.environ.get("PLATFORM_ADMIN_EMAILS") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def require_platform_admin(ctx: dict) -> None:
    """Day-1 fix (2026-10-25, audit findings 2/15). The runtime settings and
    the MS Project worker token are ONE global record shared by every
    company on this server, so being an admin of some company is not enough
    (anyone could sign up and become admin of their own company). When
    PLATFORM_ADMIN_EMAILS is set (comma-separated, any case), only those
    emails may change settings, rotate the worker token or download the
    worker. When it is not set, fall back to the company admin check."""
    allowed = _platform_admin_emails()
    if not allowed:
        role_required(ctx, ["admin"])
        return
    email = ((ctx.get("user") or {}).get("email") or "").strip().lower()
    if email not in allowed:
        raise HTTPException(
            status_code=403,
            detail="Only the platform owner can change these settings. Ask ICE support if something here needs to change.",
        )


def _state() -> dict:
    from services import feature_flags

    flags = []
    for meta in FLAG_META:
        key = meta["key"]
        ui_value = runtime_settings.flag_override(key)
        env_on = (os.environ.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}
        flags.append({
            **meta,
            "effective": feature_flags._enabled(key),
            "source": "ui" if ui_value is not None else ("env" if env_on else "default"),
        })
    return {"flags": flags, "ms_project": runtime_settings.worker_status()}


@router.get("/admin/runtime-settings")
async def get_settings(ctx: dict = Depends(require_tenant)):
    role_required(ctx, ["admin"])
    await runtime_settings.refresh_cache()  # admin page load: show the freshest truth
    return _state()


@router.patch("/admin/runtime-settings")
async def patch_settings(payload: dict = Body(...), ctx: dict = Depends(require_tenant)):
    require_platform_admin(ctx)
    flags = payload.get("flags") or {}
    bad = [k for k in flags if k not in _ALLOWED]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown flags: {bad}")
    updated_by = (ctx.get("user") or {}).get("email") or ""
    await runtime_settings.set_flags(flags, updated_by=updated_by)
    return _state()


@router.post("/admin/runtime-settings/msp-token/rotate")
async def rotate_msp_token(ctx: dict = Depends(require_tenant)):
    require_platform_admin(ctx)
    updated_by = (ctx.get("user") or {}).get("email") or ""
    token = await runtime_settings.rotate_worker_token(updated_by=updated_by)
    # The ONLY place the token value ever crosses the wire. The UI shows it
    # once with a copy button; a rotate invalidates the previous token.
    return {"token": token, "ms_project": runtime_settings.worker_status()}


@router.get("/admin/msp-worker/download")
async def download_worker(ctx: dict = Depends(require_tenant)):
    require_platform_admin(ctx)
    worker_dir = Path(__file__).resolve().parents[2] / "worker"
    if not worker_dir.is_dir():
        raise HTTPException(status_code=404, detail="Worker package not found on this deployment")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(worker_dir.iterdir()):
            # Ship the package, never a live config (worker.env holds the token).
            if p.is_file() and p.name != "worker.env" and not p.name.endswith(".log"):
                zf.write(p, arcname=f"ice-msproject-worker/{p.name}")
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="ice-msproject-worker.zip"'},
    )
