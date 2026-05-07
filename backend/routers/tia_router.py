"""Bucket C3c Phase 4 — TIA generator router.

  POST /api/projects/{pid}/tia/generate          kick off a generation
  GET  /api/projects/{pid}/tia/generations       list this project's runs
  GET  /api/tia/generations/{id}                 generation record
  GET  /api/tia/generations/{id}/json|word|pdf   download artifact

Phase 5 will add the privilege-flag endpoints + access-control checks
on the download endpoints; Phase 4 stamps counsel_privileged_work_product
=true on every generation but does not yet gate downloads.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Response, BackgroundTasks
from datetime import date
from typing import Optional
import logging

from database import get_db, tenant_filter
from auth import require_tenant
from audit import write_audit
from models import TIAGenerationCreate, TIAUnflagPayload, utc_now, gen_id
from services.tia_generator import generate_tia, fetch_artifact_bytes


# Followup-fix 2 (2026-05-07): threshold for switching from inline
# generation to BackgroundTasks. Cheap count of "evidence items"
# (delays + gates + coordination + photos + diffs + presence rows
# in range) -- if the estimate exceeds this, run async so the request
# returns 202 immediately and the frontend polls.
TIA_BG_TASK_THRESHOLD = 200


def _is_admin(ctx: dict) -> bool:
    return (ctx.get("user", {}).get("role") or "").lower() == "admin"


async def _estimate_evidence_count(
    tenant_id: str, project_id: str,
    start: date, end: date, filters: dict,
) -> int:
    """Cheap pre-count of evidence items the TIA gather will pull. Used
    to decide between sync (small) and BackgroundTask (large) paths.
    Each query is an indexed count_documents -- single round-trip per
    collection, no doc fetch."""
    db = get_db()
    s_iso = start.isoformat()
    e_iso = end.isoformat()
    e_iso_end = e_iso + "T23:59:59Z"
    cause = filters.get("cause_class")

    delays_q: dict = {"project_id": project_id,
                      "logged_date": {"$gte": s_iso, "$lte": e_iso},
                      "status": {"$ne": "deleted"}}
    if cause:
        delays_q["cause_class"] = cause

    coord_q = {"project_id": project_id,
               "occurred_at": {"$gte": s_iso, "$lte": e_iso_end},
               "deleted_at": None}

    photos_q = {"project_id": project_id,
                "photo_class": {"$in": ["discovery", "damage", "quality"]}}

    gates_q: dict = {"project_id": project_id,
                     "$or": [
                         {"created_at": {"$gte": s_iso, "$lte": e_iso_end}},
                         {"closed_at": {"$gte": s_iso, "$lte": e_iso_end}},
                     ]}
    if cause:
        gates_q["cause_class"] = cause

    diffs_q = {"project_id": project_id,
               "computed_at": {"$gte": s_iso, "$lte": e_iso_end}}

    try:
        delays_n = await db.delays.count_documents(tenant_filter(tenant_id, delays_q))
        coord_n = await db.coordination_events.count_documents(tenant_filter(tenant_id, coord_q))
        photos_n = await db.photos.count_documents(tenant_filter(tenant_id, photos_q))
        gates_n = await db.gating_entities.count_documents(tenant_filter(tenant_id, gates_q))
        diffs_n = await db.schedule_update_diffs.count_documents(tenant_filter(tenant_id, diffs_q))
    except Exception:
        # If pre-count blows up (e.g. missing collection), fall back to
        # the small-job path -- if it really were huge we'd notice.
        return 0
    return delays_n + coord_n + photos_n + gates_n + diffs_n


async def _bg_run(tenant_id: str, project_id: str, generation_id: str,
                   start: date, end: date, filters: dict,
                   actor_id: Optional[str], request_id: Optional[str]):
    """BackgroundTask wrapper. generate_tia inserts its own placeholder
    row keyed by a fresh id; for the BG path we pass an existing
    generation_id so we don't double-insert. The generator's failure
    branch already flips status='failed' with the error."""
    db = get_db()
    try:
        # Mark running so the polling frontend transitions
        # pending -> running -> complete|failed.
        await db.tia_generations.update_one(
            tenant_filter(tenant_id, {"id": generation_id}),
            {"$set": {"status": "running"}},
        )
        await generate_tia(
            tenant_id=tenant_id, project_id=project_id,
            start=start, end=end, filters=filters,
            actor_id=actor_id, request_id=request_id,
            existing_generation_id=generation_id,
        )
    except Exception as e:
        logger.exception("TIA BG generation failed (id=%s)", generation_id)
        try:
            await db.tia_generations.update_one(
                tenant_filter(tenant_id, {"id": generation_id}),
                {"$set": {"status": "failed",
                          "error": str(e)[:500],
                          "failure_reason": str(e)[:500]}},
            )
        except Exception:
            pass


router = APIRouter(tags=["tia"])
logger = logging.getLogger(__name__)


def _parse_date(s: str, label: str) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        raise HTTPException(400, f"Invalid {label} (expected ISO yyyy-mm-dd)")


@router.post("/projects/{project_id}/tia/generate")
async def kick_off_generation(
    project_id: str,
    payload: TIAGenerationCreate,
    bg: BackgroundTasks,
    ctx: dict = Depends(require_tenant),
):
    """Followup-fix 2 (2026-05-07): split into sync (small) and async
    (large) paths.

    Cheap pre-count of evidence items in range. If under
    TIA_BG_TASK_THRESHOLD (200), generate inline -- typical 6-month
    test data finishes in seconds and the user wants the artifacts
    immediately. If at or above the threshold, insert a placeholder
    row with status='pending', schedule a BackgroundTask, and return
    202 Accepted with the row -- the frontend polls
    GET /tia/generations/{id} every 5s until status flips to
    complete/failed.

    The hard 5-minute timeout from C3c Phase 6 is gone -- BG task path
    has no request-thread limit, and the sync path's small-job
    estimate makes a runaway sync gather very unlikely."""
    if payload.project_id != project_id:
        raise HTTPException(400, "project_id mismatch")
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")

    filters = {
        k: v for k, v in {
            "cause_class": payload.cause_class,
            "zone_id": payload.zone_id,
            "gating_entity_id": payload.gating_entity_id,
        }.items() if v
    }

    estimate = await _estimate_evidence_count(
        ctx["tenant_id"], project_id,
        payload.date_range_start, payload.date_range_end, filters,
    )

    if estimate < TIA_BG_TASK_THRESHOLD:
        # Sync path -- inline generation, returns the complete row.
        try:
            row = await generate_tia(
                tenant_id=ctx["tenant_id"], project_id=project_id,
                start=payload.date_range_start, end=payload.date_range_end,
                filters=filters,
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
        except Exception as e:
            logger.exception("TIA inline generation failed for project %s", project_id)
            raise HTTPException(500, f"TIA generation failed: {str(e)[:300]}")
        return row

    # Async path -- pre-insert placeholder, hand off to BackgroundTask.
    generation_id = gen_id()
    placeholder = {
        "id": generation_id,
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "generated_at": utc_now().isoformat(),
        "generated_by_user_id": ctx["user"]["id"],
        "status": "pending",
        "error": None,
        "failure_reason": None,
        "estimated_evidence_count": estimate,
        "date_range_start": payload.date_range_start.isoformat(),
        "date_range_end": payload.date_range_end.isoformat(),
        "filters_applied": filters,
        "structured_json_artifact_id": None,
        "word_doc_artifact_id": None,
        "evidence_index_pdf_artifact_id": None,
        "delay_events_count": 0,
        "mitigations_count": 0,
        "coordination_events_count": 0,
        "photos_evidentiary_count": 0,
        "schedule_diffs_count": 0,
        "total_evidence_items": 0,
        "counsel_privileged_work_product": True,
        "privilege_unflagged_at": None,
        "privilege_unflagged_by": None,
        "privilege_unflag_reason": None,
        "download_log": [],
    }
    await db.tia_generations.insert_one(placeholder.copy())
    bg.add_task(
        _bg_run,
        ctx["tenant_id"], project_id, generation_id,
        payload.date_range_start, payload.date_range_end, filters,
        ctx["user"]["id"], ctx["request_id"],
    )
    return Response(
        content=__import__("json").dumps(placeholder, default=str),
        media_type="application/json", status_code=202,
    )


@router.get("/projects/{project_id}/tia/generations")
async def list_generations(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.tia_generations.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}),
        {"_id": 0},
    ).sort("generated_at", -1).to_list(200)
    return docs


@router.get("/tia/generations/{generation_id}")
async def get_generation(generation_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.tia_generations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": generation_id}), {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "TIA generation not found")
    return doc


# ============================================================
# Downloads (each appends to download_log)
# ============================================================

CONTENT_TYPES = {
    "json": "application/json",
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


async def _can_download(row: dict, ctx: dict) -> bool:
    """Bucket C3c Phase 5 -- privilege gating.

    If counsel_privileged_work_product is False (admin has unflagged):
      any project member can download.
    If True (default):
      only the original generator OR an admin can download.

    The check is intentionally narrow -- 'project member' is enforced by
    require_tenant; this helper only adds the privilege overlay. The
    rationale is to default-protect litigation-prep material and force
    a deliberate admin action (with reason) before broader sharing."""
    if not row.get("counsel_privileged_work_product", True):
        return True
    if _is_admin(ctx):
        return True
    return row.get("generated_by_user_id") == ctx["user"]["id"]


async def _stream_download(generation_id: str, kind: str, ctx: dict):
    db = get_db()
    row = await db.tia_generations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": generation_id}), {"_id": 0},
    )
    if not row:
        raise HTTPException(404, "TIA generation not found")
    if row.get("status") != "complete":
        raise HTTPException(409, f"TIA generation status is {row.get('status')}; not ready for download")
    if not await _can_download(row, ctx):
        raise HTTPException(403, "Privileged work product -- access restricted")

    fetched = await fetch_artifact_bytes(ctx["tenant_id"], generation_id, kind)
    if not fetched:
        raise HTTPException(404, f"Artifact ({kind}) not found")
    data, content_type, filename = fetched

    # Append to download_log
    log_entry = {
        "downloaded_at": utc_now().isoformat(),
        "downloaded_by_user_id": ctx["user"]["id"],
        "format": kind,
        "privilege_state_at_download": bool(row.get("counsel_privileged_work_product", True)),
    }
    await db.tia_generations.update_one(
        tenant_filter(ctx["tenant_id"], {"id": generation_id}),
        {"$push": {"download_log": log_entry}},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tia_generations",
        op="TIA_DOWNLOAD",
        record_id=generation_id, before=None,
        after=log_entry,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )

    return Response(
        content=data,
        media_type=content_type or CONTENT_TYPES[kind],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/tia/generations/{generation_id}/unflag-privilege")
async def unflag_privilege(
    generation_id: str,
    payload: TIAUnflagPayload,
    ctx: dict = Depends(require_tenant),
):
    """Admin-only. Removes the counsel-privileged flag so all project
    members can download. Captures reason + actor + timestamp on the
    row and writes a TIA_PRIVILEGE_UNFLAGGED audit entry.

    Confirmation copy on the frontend warns about distribution
    implications -- this is a deliberate one-way-ish action (reflag is
    available but the unflag-window is logged forever)."""
    if not _is_admin(ctx):
        raise HTTPException(403, "Admin required to unflag privileged TIA")
    db = get_db()
    row = await db.tia_generations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": generation_id}), {"_id": 0},
    )
    if not row:
        raise HTTPException(404, "TIA generation not found")
    if not row.get("counsel_privileged_work_product", True):
        return {"ok": True, "already_unflagged": True}
    now_iso = utc_now().isoformat()
    updates = {
        "counsel_privileged_work_product": False,
        "privilege_unflagged_at": now_iso,
        "privilege_unflagged_by": ctx["user"]["id"],
        "privilege_unflag_reason": payload.reason,
    }
    await db.tia_generations.update_one(
        tenant_filter(ctx["tenant_id"], {"id": generation_id}),
        {"$set": updates},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tia_generations",
        op="TIA_PRIVILEGE_UNFLAGGED",
        record_id=generation_id,
        before={"counsel_privileged_work_product": True},
        after={"counsel_privileged_work_product": False,
               "privilege_unflagged_by": ctx["user"]["id"],
               "privilege_unflag_reason": payload.reason},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True, **updates}


@router.post("/tia/generations/{generation_id}/reflag-privilege")
async def reflag_privilege(
    generation_id: str,
    ctx: dict = Depends(require_tenant),
):
    """Admin-only. Re-applies the counsel-privileged flag. Useful if
    the unflag was a mistake or the access purpose has lapsed.
    Previous unflag metadata is preserved (privilege_unflagged_at /
    by / reason) -- those stay readable in the row for the chain of
    custody, only the active flag flips back."""
    if not _is_admin(ctx):
        raise HTTPException(403, "Admin required to reflag privileged TIA")
    db = get_db()
    row = await db.tia_generations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": generation_id}), {"_id": 0},
    )
    if not row:
        raise HTTPException(404, "TIA generation not found")
    if row.get("counsel_privileged_work_product", True):
        return {"ok": True, "already_flagged": True}
    await db.tia_generations.update_one(
        tenant_filter(ctx["tenant_id"], {"id": generation_id}),
        {"$set": {"counsel_privileged_work_product": True}},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="tia_generations",
        op="TIA_PRIVILEGE_FLAGGED",
        record_id=generation_id,
        before={"counsel_privileged_work_product": False},
        after={"counsel_privileged_work_product": True},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True, "counsel_privileged_work_product": True}


@router.get("/tia/generations/{generation_id}/json")
async def download_json(generation_id: str, ctx: dict = Depends(require_tenant)):
    return await _stream_download(generation_id, "json", ctx)


@router.get("/tia/generations/{generation_id}/word")
async def download_word(generation_id: str, ctx: dict = Depends(require_tenant)):
    return await _stream_download(generation_id, "word", ctx)


@router.get("/tia/generations/{generation_id}/pdf")
async def download_pdf(generation_id: str, ctx: dict = Depends(require_tenant)):
    return await _stream_download(generation_id, "pdf", ctx)
