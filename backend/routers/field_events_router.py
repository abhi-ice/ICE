"""Field events: delays, safety events, QC issues, photos. Master prompt §4.7."""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from database import get_db, tenant_filter
from models import (
    Delay, DelayCreate, SafetyEvent, SafetyEventCreate,
    QcIssue, QcIssueCreate, Photo, PhotoCreate,
    gen_id, utc_now,
)
from auth import require_tenant
from audit import write_audit

router = APIRouter(tags=["field-events"])


# --- Delays ---
@router.post("/delays", response_model=Delay)
async def create_delay(payload: DelayCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "open",
        "reported_by": ctx["user"]["id"], "reported_at": utc_now().isoformat(),
        "created_at": utc_now().isoformat(),
    })
    await db.delays.insert_one(doc.copy())
    await write_audit(tenant_id=ctx["tenant_id"], table_name="delays", op="INSERT",
                      record_id=doc["id"], before=None, after=doc,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return Delay(**doc)


@router.get("/projects/{project_id}/delays", response_model=list[Delay])
async def list_delays(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.delays.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("date", -1).to_list(500)
    return [Delay(**d) for d in docs]


# --- Safety ---
@router.post("/safety-events", response_model=SafetyEvent)
async def create_safety(payload: SafetyEventCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "open",
        "reported_by": ctx["user"]["id"], "reported_at": utc_now().isoformat(),
        "created_at": utc_now().isoformat(),
    })
    await db.safety_events.insert_one(doc.copy())
    await write_audit(tenant_id=ctx["tenant_id"], table_name="safety_events", op="INSERT",
                      record_id=doc["id"], before=None, after=doc,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return SafetyEvent(**doc)


@router.get("/projects/{project_id}/safety-events", response_model=list[SafetyEvent])
async def list_safety(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.safety_events.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("date", -1).to_list(500)
    return [SafetyEvent(**d) for d in docs]


# --- QC ---
@router.post("/qc-issues", response_model=QcIssue)
async def create_qc(payload: QcIssueCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "open",
        "reported_by": ctx["user"]["id"], "reported_at": utc_now().isoformat(),
        "resolved_at": None, "resolution_notes": None,
        "created_at": utc_now().isoformat(),
    })
    await db.qc_issues.insert_one(doc.copy())
    return QcIssue(**doc)


@router.get("/projects/{project_id}/qc-issues", response_model=list[QcIssue])
async def list_qc(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.qc_issues.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("date", -1).to_list(500)
    return [QcIssue(**d) for d in docs]


# --- Photos ---
@router.post("/photos", response_model=Photo)
async def create_photo(payload: PhotoCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "thumbnail_path": None,
        "captured_at": utc_now().isoformat(),
        "captured_by": ctx["user"]["id"],
        "visual_fingerprint_match_id": None,
        "visual_fingerprint_match_confidence": None,
        "visual_fingerprint_overridden": False,
        "created_at": utc_now().isoformat(),
    })
    await db.photos.insert_one(doc.copy())
    return Photo(**doc)


@router.get("/projects/{project_id}/photos", response_model=list[Photo])
async def list_photos(project_id: str, location_id: Optional[str] = None,
                      schedule_activity_id: Optional[str] = None,
                      ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"project_id": project_id})
    if location_id:
        f["location_id"] = location_id
    if schedule_activity_id:
        f["schedule_activity_id"] = schedule_activity_id
    docs = await db.photos.find(f, {"_id": 0}).sort("captured_at", -1).to_list(500)
    return [Photo(**d) for d in docs]
