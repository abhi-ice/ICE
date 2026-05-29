"""Drawing markups + Photos + Spec search routers.

drawing_markups follow master prompt §4.8 schema:
  id, tenant_id, drawing_revision_id, page_number, markup_type,
  geometry JSONB, style JSONB, text_content, pin_type,
  linked_entity_type, linked_entity_id, visibility, created_by

Photos with upload to Emergent Object Storage.
Spec semantic search via TF-IDF (services.spec_search).
"""
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Response, BackgroundTasks
import io
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
    # Bucket C3b Phase 4 — required class tag (default progress for
    # back-compat with old upload UIs); optional drawing pin at upload.
    photo_class: str = Form("progress"),
    drawing_pin_id: Optional[str] = Form(None),
    pin_drawing_id: Optional[str] = Form(None),
    pin_x: Optional[float] = Form(None),
    pin_y: Optional[float] = Form(None),
    file: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    body = await file.read()
    if len(body) > 25 * 1024 * 1024:  # pt.68: raised 10MB -> 25MB (matches photo-tray + vision cap)
        raise HTTPException(413, "Photo exceeds 25MB limit")
    if not body:
        raise HTTPException(400, "Empty file")

    # Bucket C3b Phase 4 — SHA-256 of body bytes BEFORE storage write so
    # the hash is the canonical fingerprint of what the user uploaded.
    import hashlib
    sha256 = hashlib.sha256(body).hexdigest()

    # Collision check — same project, same hash → 409 with the existing
    # photo's id so the client can confirm or skip.
    existing = await db.photos.find_one(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "sha256_hash": sha256,
        }),
        {"_id": 0, "id": 1},
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_photo",
                "message": "A photo with the same SHA-256 already exists in this project.",
                "existing_photo_id": existing["id"],
            },
        )

    # Class validation. Falls back to progress on garbage values rather
    # than 422 — friendly default for legacy clients.
    valid_classes = {"progress", "discovery", "damage", "quality", "safety", "coordination", "closeout"}
    if photo_class not in valid_classes:
        photo_class = "progress"

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
    server_now = utc_now()

    # Evidentiary flag: discovery / damage / quality OR has a pin.
    # Followup-fix 8 (2026-05-07): the OR semantics match the C3b spec
    # and the field_events_router._compute_evidentiary helper. The
    # earlier AND meant a 'discovery' photo was non-evidentiary until
    # the user also dropped a pin — federal evidence trail demands the
    # class itself be enough.
    EVIDENTIARY_CLASSES = ("discovery", "damage", "quality")
    has_pin = (pin_x is not None and pin_y is not None) or bool(pin_drawing_id) or bool(drawing_pin_id)
    evidentiary_flag = (photo_class in EVIDENTIARY_CLASSES) or has_pin

    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": project_id,
        "location_id": location_id,
        "schedule_activity_id": schedule_activity_id,
        "daily_report_id": daily_report_id,
        "storage_path": storage_path,
        "thumbnail_path": None,
        "captured_at": server_now.isoformat(),
        "captured_by": ctx["user"]["id"],
        "caption": caption,
        "tags": [],
        "visual_fingerprint_match_id": None,
        "visual_fingerprint_match_confidence": None,
        "visual_fingerprint_overridden": False,
        "created_at": server_now.isoformat(),
        # Bucket C3b Phase 4 evidentiary fields
        "sha256_hash": sha256,
        "device_timestamp": None,  # set below from EXIF if present
        "server_received_timestamp": server_now.isoformat(),
        "timestamp_mismatch_flag": False,
        "exif_preserved": True,
        "photo_class": photo_class,
        "evidentiary_flag": evidentiary_flag,
        "drawing_pin_id": drawing_pin_id,
        "pin_drawing_id": pin_drawing_id,
        "pin_x": pin_x,
        "pin_y": pin_y,
    }
    await db.photos.insert_one(doc.copy())
    # Best-effort EXIF + perceptual hash (master prompt §13)
    try:
        from PIL import Image, ExifTags
        import imagehash
        img = Image.open(io.BytesIO(body))
        exif_raw = img._getexif() or {}
        exif: dict = {}
        for k, v in exif_raw.items():
            tag = ExifTags.TAGS.get(k, str(k))
            exif[tag] = v
        update: dict = {"phash": str(imagehash.phash(img))}
        if "DateTimeOriginal" in exif:
            update["captured_at_exif"] = str(exif["DateTimeOriginal"])
            # Bucket C3b Phase 4 — also stamp device_timestamp + check
            # for clock skew vs server-receive. EXIF datetime format:
            # "YYYY:MM:DD HH:MM:SS" (note ':' separators in date).
            try:
                from datetime import datetime as _dt
                raw = str(exif["DateTimeOriginal"])
                # Convert "YYYY:MM:DD HH:MM:SS" → ISO
                if len(raw) >= 19 and raw[4] == ":":
                    iso = raw[:4] + "-" + raw[5:7] + "-" + raw[8:10] + "T" + raw[11:19]
                else:
                    iso = raw
                dt = _dt.fromisoformat(iso)
                update["device_timestamp"] = dt.isoformat()
                # Compare to server_now without tzinfo on the device side
                # (EXIF carries no offset). Mismatch threshold = 60 sec.
                skew = abs((server_now.replace(tzinfo=None) - dt).total_seconds())
                update["timestamp_mismatch_flag"] = skew > 60
            except Exception:
                pass
        gps_raw = exif.get("GPSInfo")
        if isinstance(gps_raw, dict):
            gps: dict = {}
            for k, v in gps_raw.items():
                tag = ExifTags.GPSTAGS.get(k, str(k))
                gps[tag] = v
            if "GPSLatitude" in gps and "GPSLongitude" in gps:
                from routers.iter6_router import _dms_to_dd
                lat = _dms_to_dd(gps["GPSLatitude"], gps.get("GPSLatitudeRef") or "N")
                lng = _dms_to_dd(gps["GPSLongitude"], gps.get("GPSLongitudeRef") or "E")
                update["gps_lat"] = lat
                update["gps_lng"] = lng
        if "Make" in exif:
            update["camera_make"] = str(exif["Make"]).strip()
        if "Model" in exif:
            update["camera_model"] = str(exif["Model"]).strip()
        await db.photos.update_one({"id": doc["id"]}, {"$set": update})
        doc.update(update)
    except Exception:
        pass
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
