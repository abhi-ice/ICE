"""Subcontractor + Supplier central repo (2026-02-XX).

Tenant-wide CRUD for two collections: `subcontractors` and `suppliers`.
Used by:
  - Project create / edit forms (multi-select pickers)
  - Daily Report "Manpower log" Company field (datalist autocomplete)
  - DR Voice intake (passed into the AI prompt so "3 electricians"
    can be mapped to a known sub when one exists)

Records are simple: id, name, trade(s), contact, notes. Suppliers are
the same shape but live in a separate collection for clean filtering.
"""
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database import get_db
from auth import require_tenant
from audit import write_audit
from models import gen_id

router = APIRouter(prefix="", tags=["vendors"])


class VendorIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    trades: List[str] = Field(default_factory=list)
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None


class VendorOut(VendorIn):
    id: str
    tenant_id: str
    kind: str  # "subcontractor" | "supplier"
    created_at: datetime
    updated_at: datetime


def _tenant_filter(tenant_id: str, extra: dict | None = None) -> dict:
    q = {"tenant_id": tenant_id}
    if extra:
        q.update(extra)
    return q


async def _list_kind(kind: str, ctx: dict) -> list[dict]:
    db = get_db()
    coll = db.subcontractors if kind == "subcontractor" else db.suppliers
    return await coll.find(
        _tenant_filter(ctx["tenant_id"]),
        {"_id": 0},
    ).sort("name", 1).to_list(500)


async def _create_kind(kind: str, payload: VendorIn, ctx: dict) -> dict:
    db = get_db()
    coll = db.subcontractors if kind == "subcontractor" else db.suppliers
    now = datetime.now(timezone.utc)
    name_norm = payload.name.strip()
    # Dedupe by name within tenant — return existing row if it's there.
    existing = await coll.find_one(
        _tenant_filter(ctx["tenant_id"], {"name": name_norm}),
        {"_id": 0},
    )
    if existing:
        return existing
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "kind": kind,
        "name": name_norm,
        "trades": [t.strip() for t in (payload.trades or []) if t and t.strip()],
        "contact_name": payload.contact_name,
        "contact_email": payload.contact_email,
        "contact_phone": payload.contact_phone,
        "notes": payload.notes,
        "created_at": now,
        "updated_at": now,
        "created_by": ctx["user"]["id"],
    }
    await coll.insert_one(doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name=coll.name, op="INSERT",
        record_id=doc["id"], before={}, after=doc,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return doc


async def _patch_kind(kind: str, vid: str, payload: dict, ctx: dict) -> dict:
    db = get_db()
    coll = db.subcontractors if kind == "subcontractor" else db.suppliers
    before = await coll.find_one(
        _tenant_filter(ctx["tenant_id"], {"id": vid}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, f"{kind.capitalize()} not found")
    updates = {k: v for k, v in payload.items() if v is not None}
    updates["updated_at"] = datetime.now(timezone.utc)
    await coll.update_one(
        _tenant_filter(ctx["tenant_id"], {"id": vid}), {"$set": updates},
    )
    after = await coll.find_one(
        _tenant_filter(ctx["tenant_id"], {"id": vid}), {"_id": 0},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name=coll.name, op="UPDATE",
        record_id=vid, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return after


async def _delete_kind(kind: str, vid: str, ctx: dict) -> dict:
    db = get_db()
    coll = db.subcontractors if kind == "subcontractor" else db.suppliers
    role = (ctx["user"].get("role") or "").lower()
    if role not in {"admin", "pm"}:
        raise HTTPException(403, "Only admin / PM can delete vendors")
    before = await coll.find_one(
        _tenant_filter(ctx["tenant_id"], {"id": vid}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, f"{kind.capitalize()} not found")
    await coll.delete_one(_tenant_filter(ctx["tenant_id"], {"id": vid}))
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name=coll.name, op="DELETE",
        record_id=vid, before=before, after={},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True, "id": vid}


@router.get("/subcontractors")
async def list_subs(ctx: dict = Depends(require_tenant)):
    return await _list_kind("subcontractor", ctx)


@router.post("/subcontractors")
async def create_sub(payload: VendorIn, ctx: dict = Depends(require_tenant)):
    return await _create_kind("subcontractor", payload, ctx)


@router.patch("/subcontractors/{vid}")
async def patch_sub(vid: str, payload: dict, ctx: dict = Depends(require_tenant)):
    return await _patch_kind("subcontractor", vid, payload, ctx)


@router.delete("/subcontractors/{vid}")
async def delete_sub(vid: str, ctx: dict = Depends(require_tenant)):
    return await _delete_kind("subcontractor", vid, ctx)


@router.get("/suppliers")
async def list_suppliers(ctx: dict = Depends(require_tenant)):
    return await _list_kind("supplier", ctx)


@router.post("/suppliers")
async def create_supplier(payload: VendorIn, ctx: dict = Depends(require_tenant)):
    return await _create_kind("supplier", payload, ctx)


@router.patch("/suppliers/{vid}")
async def patch_supplier(vid: str, payload: dict, ctx: dict = Depends(require_tenant)):
    return await _patch_kind("supplier", vid, payload, ctx)


@router.delete("/suppliers/{vid}")
async def delete_supplier(vid: str, ctx: dict = Depends(require_tenant)):
    return await _delete_kind("supplier", vid, ctx)
