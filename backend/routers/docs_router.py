"""RFIs, change orders, submittals, work items, drawings, specs."""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from datetime import date
from database import get_db, tenant_filter
from models import (
    Rfi, RfiCreate, RfiUpdate,
    ChangeOrder, ChangeOrderCreate,
    WorkItem, WorkItemCreate, WorkItemLink, WorkItemLinkCreate,
    Drawing, DrawingCreate,
    ProjectSpec, SpecCreate, SpecSection,
    gen_id, utc_now,
)
from auth import require_tenant
from audit import write_audit

router = APIRouter(tags=["procurement-docs"])


async def _next_number(db, tenant_id: str, project_id: str, collection: str, prefix: str) -> str:
    count = await db[collection].count_documents(tenant_filter(tenant_id, {"project_id": project_id}))
    return f"{prefix}-{count + 1:04d}"


# --- RFIs ---
@router.post("/rfis", response_model=Rfi)
async def create_rfi(payload: RfiCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    rfi_number = await _next_number(db, ctx["tenant_id"], payload.project_id, "rfis", "RFI")
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "rfi_number": rfi_number,
        "date_initiated": utc_now().date().isoformat(),
        "date_responded": None,
        "response_text": None,
        "status": "draft",
        "cost_impact_estimated_usd": None,
        "time_impact_estimated_days": None,
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    })
    await db.rfis.insert_one(doc.copy())
    await write_audit(tenant_id=ctx["tenant_id"], table_name="rfis", op="INSERT",
                      record_id=doc["id"], before=None, after=doc,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return Rfi(**doc)


@router.get("/projects/{project_id}/rfis", response_model=list[Rfi])
async def list_rfis(project_id: str, status_filter: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"project_id": project_id})
    if status_filter:
        # Caller asked for a specific status — honour it as-is (might
        # legitimately want to inspect deleted records).
        f["status"] = status_filter
    else:
        # Default: hide soft-deleted RFIs. RFIs are still soft-delete via
        # /crud/{entity}/{id} (FK refs from work_item_links), so without
        # this filter deleted RFIs would keep appearing in the sidebar
        # list and the daily-report panel.
        f["status"] = {"$ne": "deleted"}
    docs = await db.rfis.find(f, {"_id": 0}).sort("date_initiated", -1).to_list(500)
    return [Rfi(**d) for d in docs]


@router.patch("/rfis/{rfi_id}", response_model=Rfi)
async def update_rfi(rfi_id: str, payload: RfiUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    before = await db.rfis.find_one(tenant_filter(ctx["tenant_id"], {"id": rfi_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "RFI not found")
    await db.rfis.update_one(tenant_filter(ctx["tenant_id"], {"id": rfi_id}), {"$set": updates})
    after = await db.rfis.find_one(tenant_filter(ctx["tenant_id"], {"id": rfi_id}), {"_id": 0})
    await write_audit(tenant_id=ctx["tenant_id"], table_name="rfis", op="UPDATE",
                      record_id=rfi_id, before=before, after=after,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    return Rfi(**after)


# --- Change orders ---
@router.post("/change-orders", response_model=ChangeOrder)
async def create_co(payload: ChangeOrderCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    co_number = await _next_number(db, ctx["tenant_id"], payload.project_id, "change_orders", "CO")
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "co_number": co_number, "status": "proposed",
        "proposed_at": utc_now().isoformat(), "approved_at": None,
        "created_at": utc_now().isoformat(),
    })
    await db.change_orders.insert_one(doc.copy())
    return ChangeOrder(**doc)


@router.get("/projects/{project_id}/change-orders", response_model=list[ChangeOrder])
async def list_co(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.change_orders.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("co_number", -1).to_list(500)
    return [ChangeOrder(**d) for d in docs]


# --- Work items ---
@router.post("/work-items", response_model=WorkItem)
async def create_work_item(payload: WorkItemCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "status": "open",
        "actual_cost_impact": None, "actual_time_impact_days": None,
        "opened_by": ctx["user"]["id"], "opened_at": utc_now().isoformat(),
        "owned_by": ctx["user"]["id"],
        "closed_at": None, "closure_summary": None,
        "created_at": utc_now().isoformat(),
    })
    await db.work_items.insert_one(doc.copy())
    return WorkItem(**doc)


@router.get("/projects/{project_id}/work-items", response_model=list[WorkItem])
async def list_work_items(project_id: str, status_filter: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"project_id": project_id})
    if status_filter:
        f["status"] = status_filter
    docs = await db.work_items.find(f, {"_id": 0}).sort("opened_at", -1).to_list(500)
    return [WorkItem(**d) for d in docs]


@router.post("/work-item-links", response_model=WorkItemLink)
async def create_work_item_link(payload: WorkItemLinkCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    wi = await db.work_items.find_one(tenant_filter(ctx["tenant_id"], {"id": payload.work_item_id}), {"_id": 0})
    if not wi:
        raise HTTPException(404, "Work item not found")
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "created_by": ctx["user"]["id"], "created_at": utc_now().isoformat(),
    })
    await db.work_item_links.insert_one(doc.copy())
    return WorkItemLink(**doc)


@router.get("/work-items/{work_item_id}/links", response_model=list[WorkItemLink])
async def list_work_item_links(work_item_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.work_item_links.find(
        tenant_filter(ctx["tenant_id"], {"work_item_id": work_item_id}), {"_id": 0},
    ).to_list(500)
    return [WorkItemLink(**d) for d in docs]


# --- Drawings (basic; markup canvas deferred) ---
@router.post("/drawings", response_model=Drawing)
async def create_drawing(payload: DrawingCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "revision_count": 1, "current_revision_id": None,
        "qr_code_url": None, "status": "current",
        "created_at": utc_now().isoformat(),
    })
    await db.drawings.insert_one(doc.copy())
    return Drawing(**doc)


@router.get("/projects/{project_id}/drawings", response_model=list[Drawing])
async def list_drawings(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.drawings.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("sheet_number", 1).to_list(500)
    return [Drawing(**d) for d in docs]


# --- Specs ---
@router.post("/specs", response_model=ProjectSpec)
async def create_spec(payload: SpecCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "pdf_url": None, "pdf_page_count": None,
        "total_sections": 0, "processing_status": "pending",
        "created_at": utc_now().isoformat(),
    })
    await db.project_specs.insert_one(doc.copy())
    return ProjectSpec(**doc)


@router.get("/projects/{project_id}/specs", response_model=list[ProjectSpec])
async def list_specs(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.project_specs.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    ).sort("created_at", -1).to_list(100)
    return [ProjectSpec(**d) for d in docs]


@router.get("/specs/{spec_id}/sections", response_model=list[SpecSection])
async def list_spec_sections(spec_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.spec_sections.find(
        tenant_filter(ctx["tenant_id"], {"project_spec_id": spec_id}), {"_id": 0},
    ).sort("section_code", 1).to_list(2000)
    return [SpecSection(**d) for d in docs]
