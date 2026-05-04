"""Resources: crews, equipment, people, certifications."""
from fastapi import APIRouter, Depends, HTTPException
from datetime import date, timedelta
from typing import Optional
from database import get_db, tenant_filter
from models import (
    Crew, CrewCreate, Equipment, EquipmentCreate,
    Person, PersonCreate, Certification, CertificationCreate,
    gen_id, utc_now,
)
from auth import require_tenant

router = APIRouter(tags=["resources"])


# --- Crews ---
@router.post("/crews", response_model=Crew)
async def create_crew(payload: CrewCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    if await db.crews.find_one(tenant_filter(ctx["tenant_id"], {"crew_code": payload.crew_code}), {"_id": 0}):
        raise HTTPException(409, "Crew code already exists")
    doc = payload.model_dump(mode="json")
    doc.update({"id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "active", "created_at": utc_now().isoformat()})
    await db.crews.insert_one(doc.copy())
    return Crew(**doc)


@router.get("/crews", response_model=list[Crew])
async def list_crews(ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.crews.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).sort("name", 1).to_list(500)
    return [Crew(**d) for d in docs]


# --- Equipment ---
@router.post("/equipment", response_model=Equipment)
async def create_equipment(payload: EquipmentCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    if await db.equipment.find_one(tenant_filter(ctx["tenant_id"], {"asset_tag": payload.asset_tag}), {"_id": 0}):
        raise HTTPException(409, "Asset tag already exists")
    doc = payload.model_dump(mode="json")
    doc.update({"id": gen_id(), "tenant_id": ctx["tenant_id"], "status": "available", "created_at": utc_now().isoformat()})
    await db.equipment.insert_one(doc.copy())
    return Equipment(**doc)


@router.get("/equipment", response_model=list[Equipment])
async def list_equipment(ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.equipment.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).sort("asset_tag", 1).to_list(500)
    return [Equipment(**d) for d in docs]


# --- People ---
@router.post("/people", response_model=Person)
async def create_person(payload: PersonCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "e_verify_status": "pending", "i9_on_file": False, "cleared_for_federal": False,
        "status": "active", "created_at": utc_now().isoformat(),
    })
    await db.people.insert_one(doc.copy())
    return Person(**doc)


@router.get("/people", response_model=list[Person])
async def list_people(ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.people.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).sort("last_name", 1).to_list(2000)
    return [Person(**d) for d in docs]


# --- Certifications ---
@router.post("/certifications", response_model=Certification)
async def create_cert(payload: CertificationCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "verified": False, "created_at": utc_now().isoformat(),
    })
    await db.certifications.insert_one(doc.copy())
    return Certification(**doc)


@router.get("/certifications", response_model=list[Certification])
async def list_certs(expiring_within_days: Optional[int] = None, ctx: dict = Depends(require_tenant)):
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    if expiring_within_days:
        cutoff = (date.today() + timedelta(days=expiring_within_days)).isoformat()
        f["expires_date"] = {"$lte": cutoff}
    docs = await db.certifications.find(f, {"_id": 0}).sort("expires_date", 1).to_list(2000)
    return [Certification(**d) for d in docs]


@router.get("/availability")
async def availability(start: date, end: date, ctx: dict = Depends(require_tenant)):
    """Cross-project crew + equipment availability dashboard."""
    db = get_db()
    crews = await db.crews.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).to_list(500)
    equip = await db.equipment.find(tenant_filter(ctx["tenant_id"]), {"_id": 0}).to_list(500)
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "crews": [{"id": c["id"], "name": c["name"], "trade": c["trade"], "status": c.get("status")} for c in crews],
        "equipment": [{"id": e["id"], "asset_tag": e["asset_tag"], "type": e["type"], "status": e.get("status")} for e in equip],
    }
