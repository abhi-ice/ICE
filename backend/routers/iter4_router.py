"""Iter 4 router:
- voice annotation linkage PATCH
- submittals CRUD + auto-generated from spec ingestion
- drawings_spatial_metadata (face binding for Spatial Path 3)
- generic entity GET for pin deep-link

All endpoints respect tenant isolation via tenant_filter().
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, Literal
from pydantic import Field
from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel, ReadModel
from auth import require_tenant

router = APIRouter(tags=["iter4"])


# ============================================================
# VOICE ANNOTATION LINKAGE PATCH
# ============================================================
class VoiceLinkUpdate(StrictModel):
    linked_entity_type: Optional[str] = None
    linked_entity_id: Optional[str] = None


@router.patch("/voice-annotations/{annotation_id}")
async def patch_voice_annotation(
    annotation_id: str, payload: VoiceLinkUpdate, ctx: dict = Depends(require_tenant),
):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields")
    res = await db.voice_annotations.update_one(
        tenant_filter(ctx["tenant_id"], {"id": annotation_id}),
        {"$set": updates},
    )
    if not res.matched_count:
        raise HTTPException(404, "Voice annotation not found")
    row = await db.voice_annotations.find_one(
        tenant_filter(ctx["tenant_id"], {"id": annotation_id}), {"_id": 0},
    )
    return row


# ============================================================
# SUBMITTALS
# ============================================================
SubmittalStatus = Literal[
    "not_yet_submitted", "submitted", "in_review", "approved",
    "approved_as_noted", "rejected", "revise_resubmit", "closed",
]


class SubmittalCreate(StrictModel):
    project_id: str
    spec_section_id: Optional[str] = None
    spec_section_code: Optional[str] = None
    title: str
    type: str = "product_data"
    sub_responsible: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    drawings_affected: list[str] = Field(default_factory=list)


class Submittal(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    submittal_number: str
    spec_section_id: Optional[str] = None
    spec_section_code: Optional[str] = None
    title: str
    type: str = "product_data"
    status: SubmittalStatus = "not_yet_submitted"
    sub_responsible: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    drawings_affected: list[str] = Field(default_factory=list)
    submitted_at: Optional[str] = None
    approved_at: Optional[str] = None
    auto_generated: bool = False
    created_by: Optional[str] = None
    created_at: str


async def _next_submittal_number(db, tenant_id: str, project_id: str) -> str:
    n = await db.submittals.count_documents(tenant_filter(tenant_id, {"project_id": project_id}))
    return f"SUB-{n + 1:04d}"


@router.post("/submittals", response_model=Submittal)
async def create_submittal(payload: SubmittalCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    sub_no = await _next_submittal_number(db, ctx["tenant_id"], payload.project_id)
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "submittal_number": sub_no,
        "status": "not_yet_submitted",
        "submitted_at": None,
        "approved_at": None,
        "auto_generated": False,
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    })
    await db.submittals.insert_one(doc.copy())
    return Submittal(**doc)


@router.get("/projects/{project_id}/submittals", response_model=list[Submittal])
async def list_submittals(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.submittals.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("submittal_number", 1).to_list(2000)
    return [Submittal(**d) for d in docs]


class SubmittalUpdate(StrictModel):
    status: Optional[SubmittalStatus] = None
    sub_responsible: Optional[str] = None
    submitted_at: Optional[str] = None
    approved_at: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    spec_section_id: Optional[str] = None


@router.patch("/submittals/{submittal_id}", response_model=Submittal)
async def update_submittal(submittal_id: str, payload: SubmittalUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        row = await db.submittals.find_one(tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"_id": 0})
        if not row: raise HTTPException(404)
        return Submittal(**row)
    res = await db.submittals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"$set": updates},
    )
    if not res.matched_count:
        raise HTTPException(404, "Submittal not found")
    row = await db.submittals.find_one(tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"_id": 0})
    return Submittal(**row)


# ============================================================
# DRAWINGS SPATIAL METADATA — Path 3 face binding
# ============================================================
DrawingType = Literal["plan", "elevation_north", "elevation_south", "elevation_east", "elevation_west", "section", "rcp", "detail"]


class SpatialMetaUpsert(StrictModel):
    drawing_revision_id: str
    page_number: int = 1
    drawing_type: DrawingType
    floor_id: Optional[str] = None
    scale_ft_per_px: Optional[float] = None
    origin_offset_x_ft: float = 0
    origin_offset_y_ft: float = 0
    rotation_deg: float = 0
    calibrated: bool = False


class SpatialMeta(ReadModel):
    id: str
    tenant_id: str
    drawing_revision_id: str
    page_number: int
    drawing_type: str
    floor_id: Optional[str] = None
    scale_ft_per_px: Optional[float] = None
    origin_offset_x_ft: float = 0
    origin_offset_y_ft: float = 0
    rotation_deg: float = 0
    calibrated: bool = False
    calibrated_by: Optional[str] = None
    calibrated_at: Optional[str] = None
    created_at: str


@router.post("/drawings-spatial-metadata", response_model=SpatialMeta)
async def upsert_spatial_meta(payload: SpatialMetaUpsert, ctx: dict = Depends(require_tenant)):
    db = get_db()
    rev = await db.drawing_revisions.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.drawing_revision_id}), {"_id": 0, "id": 1, "drawing_id": 1},
    )
    if not rev:
        raise HTTPException(404, "Drawing revision not found")
    # find existing for (revision, page)
    existing = await db.drawings_spatial_metadata.find_one(
        tenant_filter(ctx["tenant_id"], {
            "drawing_revision_id": payload.drawing_revision_id,
            "page_number": payload.page_number,
        }), {"_id": 0},
    )
    base = payload.model_dump(mode="json")
    base["calibrated_by"] = ctx["user"]["id"] if payload.calibrated else None
    base["calibrated_at"] = utc_now().isoformat() if payload.calibrated else None
    base["drawing_id"] = rev["drawing_id"]
    if existing:
        await db.drawings_spatial_metadata.update_one(
            {"id": existing["id"]}, {"$set": base},
        )
        out = await db.drawings_spatial_metadata.find_one({"id": existing["id"]}, {"_id": 0})
        return SpatialMeta(**out)
    base.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "created_at": utc_now().isoformat(),
    })
    await db.drawings_spatial_metadata.insert_one(base.copy())
    return SpatialMeta(**base)


@router.get("/projects/{project_id}/spatial-metadata", response_model=list[SpatialMeta])
async def list_spatial_meta(project_id: str, ctx: dict = Depends(require_tenant)):
    """Return spatial metadata for all drawing revisions belonging to the project."""
    db = get_db()
    revs = await db.drawing_revisions.find(
        tenant_filter(ctx["tenant_id"]), {"_id": 0, "id": 1, "drawing_id": 1, "pdf_page_count": 1},
    ).to_list(5000)
    drawings = await db.drawings.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0, "id": 1},
    ).to_list(5000)
    drawing_ids = {d["id"] for d in drawings}
    rev_ids = [r["id"] for r in revs if r["drawing_id"] in drawing_ids]
    if not rev_ids:
        return []
    docs = await db.drawings_spatial_metadata.find(
        tenant_filter(ctx["tenant_id"], {"drawing_revision_id": {"$in": rev_ids}}), {"_id": 0},
    ).to_list(2000)
    return [SpatialMeta(**d) for d in docs]


# ============================================================
# GENERIC ENTITY FETCH — for pin deep-link
# ============================================================
ENTITY_COLLECTIONS = {
    "task": "tasks",
    "rfi": "rfis",
    "work_item": "work_items",
    "delay": "delays",
    "safety_event": "safety_events",
    "qc_issue": "qc_issues",
    "photo": "photos",
    "delivery": "deliveries",
    "change_order": "change_orders",
    "submittal": "submittals",
}


@router.get("/entities/{entity_type}/{entity_id}")
async def get_entity(entity_type: str, entity_id: str, ctx: dict = Depends(require_tenant)):
    coll = ENTITY_COLLECTIONS.get(entity_type)
    if not coll:
        raise HTTPException(400, f"Unknown entity type: {entity_type}")
    db = get_db()
    doc = await db[coll].find_one(tenant_filter(ctx["tenant_id"], {"id": entity_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Entity not found")
    return {"entity_type": entity_type, "entity": doc}


# ============================================================
# UPDATE LOCATION POLYGON (used by polygon editor)
# ============================================================
class LocationPolygonUpdate(StrictModel):
    polygon_geojson: dict


@router.patch("/locations/{location_id}/polygon")
async def update_location_polygon(location_id: str, payload: LocationPolygonUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    coords = payload.polygon_geojson.get("coordinates", [[]])[0]
    if len(coords) < 4:
        raise HTTPException(400, "Polygon needs at least 3 vertices (closed ring of 4 points)")
    cx = sum(c[0] for c in coords[:-1]) / max(1, len(coords) - 1)
    cy = sum(c[1] for c in coords[:-1]) / max(1, len(coords) - 1)
    res = await db.locations.update_one(
        tenant_filter(ctx["tenant_id"], {"id": location_id}),
        {"$set": {
            "polygon_geojson": payload.polygon_geojson,
            "centroid_x": cx,
            "centroid_y": cy,
        }},
    )
    if not res.matched_count:
        raise HTTPException(404, "Location not found")
    doc = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"_id": 0})
    return doc
