"""Projects + floors + locations CRUD."""
from fastapi import APIRouter, Depends, HTTPException, status
from typing import Optional
from database import get_db, tenant_filter
from models import (
    Project, ProjectCreate, ProjectUpdate,
    Floor, FloorCreate, Location, LocationCreate,
    gen_id, utc_now,
)
from auth import require_tenant
from audit import write_audit

router = APIRouter(tags=["projects"])


# --- projects ---
@router.post("/projects", response_model=Project)
async def create_project(payload: ProjectCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "actual_completion_date": None,
        "created_at": utc_now().isoformat(),
        "created_by": ctx["user"]["id"],
    })
    await db.projects.insert_one(doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects", op="INSERT",
        record_id=doc["id"], before=None, after=doc, actor_id=ctx["user"]["id"],
        request_id=ctx["request_id"],
    )
    return Project(**doc)


@router.get("/projects", response_model=list[Project])
async def list_projects(
    status_filter: Optional[str] = None,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    if status_filter:
        f["status"] = status_filter
    docs = await db.projects.find(f, {"_id": 0}).sort("created_at", 1).to_list(500)
    return [Project(**d) for d in docs]


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    if not doc:
        raise HTTPException(404, "Project not found")
    return Project(**doc)


@router.patch("/projects/{project_id}", response_model=Project)
async def update_project(project_id: str, payload: ProjectUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        return await get_project(project_id, ctx)
    before = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Project not found")
    await db.projects.update_one(
        tenant_filter(ctx["tenant_id"], {"id": project_id}),
        {"$set": updates},
    )
    after = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="projects", op="UPDATE",
        record_id=project_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return Project(**after)


# --- floors ---
@router.post("/floors", response_model=Floor)
async def create_floor(payload: FloorCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    # ensure project belongs to tenant
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    doc = payload.model_dump(mode="json")
    doc.update({"id": gen_id(), "tenant_id": ctx["tenant_id"], "created_at": utc_now().isoformat()})
    await db.floors.insert_one(doc.copy())
    return Floor(**doc)


@router.get("/projects/{project_id}/floors", response_model=list[Floor])
async def list_floors(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.floors.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0}
    ).sort("floor_number", 1).to_list(200)
    return [Floor(**d) for d in docs]


# --- locations ---
@router.post("/locations", response_model=Location)
async def create_location(payload: LocationCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    doc = payload.model_dump(mode="json")
    doc.update({"id": gen_id(), "tenant_id": ctx["tenant_id"], "created_at": utc_now().isoformat()})
    await db.locations.insert_one(doc.copy())
    return Location(**doc)


@router.get("/projects/{project_id}/locations", response_model=list[Location])
async def list_locations(project_id: str, floor_id: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"project_id": project_id})
    if floor_id:
        f["floor_id"] = floor_id
    docs = await db.locations.find(f, {"_id": 0}).sort("name", 1).to_list(2000)
    return [Location(**d) for d in docs]


@router.patch("/locations/{location_id}", response_model=Location)
async def update_location(location_id: str, payload: LocationCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = payload.model_dump(mode="json")
    before = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "Location not found")
    await db.locations.update_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"$set": updates})
    after = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {"id": location_id}), {"_id": 0})
    return Location(**after)
