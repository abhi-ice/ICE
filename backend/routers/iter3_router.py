"""Drawing markups + Photos + Spec search routers.

drawing_markups follow master prompt §4.8 schema:
  id, tenant_id, drawing_revision_id, page_number, markup_type,
  geometry JSONB, style JSONB, text_content, pin_type,
  linked_entity_type, linked_entity_id, visibility, created_by

Photos with upload to Emergent Object Storage.
Spec semantic search via TF-IDF (services.spec_search).
"""
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Response, BackgroundTasks
from typing import Optional, List, Literal
from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel, ReadModel, Photo, PhotoCreate
from auth import require_tenant
from services.storage import put_object, get_object, make_path, record_file
from services.spec_search import (
    fit_spec_index, search_spec, search_all_specs_in_project,
)
from pydantic import Field

router = APIRouter(tags=["iter3"])


# ============================================================
# DRAWING MARKUPS
# ============================================================
class DrawingMarkupCreate(StrictModel):
    drawing_revision_id: str
    page_number: int = 1
    markup_type: Literal["pen", "rect", "ellipse", "arrow", "line", "text", "pin", "cloud"]
    geometry: dict
    style: dict = Field(default_factory=dict)
    text_content: Optional[str] = None
    pin_type: Optional[str] = None
    linked_entity_type: Optional[Literal["task", "rfi", "work_item", "delay", "safety_event", "qc_issue", "photo", "delivery", "change_order", "submittal"]] = None
    linked_entity_id: Optional[str] = None
    visibility: Literal["personal", "published"] = "personal"


class DrawingMarkup(ReadModel):
    id: str
    tenant_id: str
    drawing_revision_id: str
    page_number: int
    markup_type: str
    geometry: dict
    style: dict
    text_content: Optional[str] = None
    pin_type: Optional[str] = None
    linked_entity_type: Optional[str] = None
    linked_entity_id: Optional[str] = None
    visibility: str = "personal"
    created_by: Optional[str] = None
    created_at: str


@router.post("/drawing-markups", response_model=DrawingMarkup)
async def create_markup(payload: DrawingMarkupCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    rev = await db.drawing_revisions.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.drawing_revision_id}), {"_id": 0, "id": 1}
    )
    if not rev:
        raise HTTPException(404, "Drawing revision not found")
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    })
    await db.drawing_markups.insert_one(doc.copy())
    return DrawingMarkup(**doc)


@router.get("/drawing-revisions/{rev_id}/markups", response_model=list[DrawingMarkup])
async def list_markups(rev_id: str, page_number: Optional[int] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"drawing_revision_id": rev_id})
    if page_number is not None:
        f["page_number"] = page_number
    docs = await db.drawing_markups.find(f, {"_id": 0}).sort("created_at", 1).to_list(2000)
    return [DrawingMarkup(**d) for d in docs]


@router.delete("/drawing-markups/{markup_id}")
async def delete_markup(markup_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    res = await db.drawing_markups.delete_one(tenant_filter(ctx["tenant_id"], {"id": markup_id}))
    if not res.deleted_count:
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ============================================================
# PHOTOS UPLOAD
# ============================================================
@router.post("/photos/upload", response_model=Photo)
async def upload_photo(
    project_id: str = Form(...),
    location_id: Optional[str] = Form(None),
    schedule_activity_id: Optional[str] = Form(None),
    daily_report_id: Optional[str] = Form(None),
    caption: Optional[str] = Form(None),
    file: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    body = await file.read()
    if len(body) > 10 * 1024 * 1024:
        raise HTTPException(413, "Photo > 10MB")
    if not body:
        raise HTTPException(400, "Empty file")
    ct = file.content_type or "image/jpeg"
    ext = (file.filename.rsplit(".", 1)[-1] if "." in (file.filename or "") else "jpg").lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "heic"):
        ext = "jpg"
    storage_path = make_path(ctx["tenant_id"], "photos", ext)
    try:
        put_object(storage_path, body, ct)
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    await record_file(
        tenant_id=ctx["tenant_id"], storage_path=storage_path,
        original_filename=file.filename or f"photo.{ext}",
        content_type=ct, size=len(body), kind="photo",
        actor_id=ctx["user"]["id"],
    )
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "location_id": location_id,
        "schedule_activity_id": schedule_activity_id,
        "daily_report_id": daily_report_id,
        "storage_path": storage_path,
        "thumbnail_path": None,
        "captured_at": utc_now().isoformat(),
        "captured_by": ctx["user"]["id"],
        "caption": caption,
        "tags": [],
        "visual_fingerprint_match_id": None,
        "visual_fingerprint_match_confidence": None,
        "visual_fingerprint_overridden": False,
        "created_at": utc_now().isoformat(),
    }
    await db.photos.insert_one(doc.copy())
    return Photo(**doc)


@router.get("/photos/{photo_id}/file")
async def stream_photo(photo_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    p = await db.photos.find_one(tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0})
    if not p or not p.get("storage_path"):
        raise HTTPException(404, "Photo not found")
    try:
        data, ct = get_object(p["storage_path"])
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")
    return Response(content=data, media_type=ct)


# ============================================================
# SPEC SEMANTIC SEARCH
# ============================================================
class SpecSearchRequest(StrictModel):
    project_id: Optional[str] = None
    project_spec_id: Optional[str] = None
    query: str
    top_k: int = 5


@router.post("/specs/search")
async def specs_search(payload: SpecSearchRequest, ctx: dict = Depends(require_tenant)):
    if not payload.query.strip():
        raise HTTPException(400, "Empty query")
    if payload.project_spec_id:
        return await search_spec(
            tenant_id=ctx["tenant_id"],
            project_spec_id=payload.project_spec_id,
            query=payload.query,
            top_k=payload.top_k,
        )
    if payload.project_id:
        return await search_all_specs_in_project(
            tenant_id=ctx["tenant_id"],
            project_id=payload.project_id,
            query=payload.query,
            top_k=payload.top_k,
        )
    raise HTTPException(400, "Provide project_id or project_spec_id")


@router.post("/specs/{spec_id}/reindex")
async def specs_reindex(spec_id: str, bg: BackgroundTasks, ctx: dict = Depends(require_tenant)):
    bg.add_task(fit_spec_index, ctx["tenant_id"], spec_id)
    return {"ok": True, "queued": True}
