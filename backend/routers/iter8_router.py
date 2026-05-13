"""Iter 8 router — comprehensive CRUD for every project entity.

Adds PATCH + DELETE endpoints for: crews, equipment, people, certifications,
delays, safety-events, qc-issues, rfis, deliveries, work-items, change-orders,
floors, locations, photos, daily-reports.

Audit-logged. Tenant-isolated. Idempotent on DELETE (soft delete via status='deleted').

Also exposes:
  - POST /spatial/auto-detect-rooms — Claude vision detects all rooms on a
    plan PDF page and creates Location records. Master prompt §22.
  - GET /onboarding/getting-started — markdown-free walkthrough text.
"""
# Bug #44 fix (2026-05-13): see photo_tray_router.py for rationale.
import io
import re
import json
import os
import base64
from datetime import datetime, timezone, date, timedelta
from typing import Optional, Any, Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel, ReadModel
from auth import require_tenant
from audit import write_audit


router = APIRouter(tags=["iter8"])


# ============================================================
# Generic CRUD helper
# ============================================================
# Per-entity behaviour for the generic /crud/{entity}/{id} routes.
#
# soft_delete=True keeps the row but marks status="deleted". This is
# safe for entities with foreign-key references elsewhere (a deleted
# crew is still referenced by daily_presence rows).
#
# soft_delete=False does an actual delete_one. Used for leaf entities
# (delays, qc-issues, safety-events, floors, locations, photos) where
# nothing else references them and the user expects the row to vanish.
#
# Bug fix 2026-05: delays/safety-events/qc-issues were soft-delete but
# the corresponding list endpoints don't filter status="deleted", so
# the rows kept showing up after the user clicked Delete (and no audit
# entry was written). Switched to hard delete to match the Phase A
# DELETE /delays/{id} behaviour and to match what the UI promises.
ENTITY_CONFIG = {
    "crews":           {"collection": "crews",           "tenant_scope": True,  "project_scope": False, "soft_delete": True},
    "equipment":       {"collection": "equipment",       "tenant_scope": True,  "project_scope": False, "soft_delete": True},
    "people":          {"collection": "people",          "tenant_scope": True,  "project_scope": False, "soft_delete": True},
    "certifications":  {"collection": "certifications",  "tenant_scope": True,  "project_scope": False, "soft_delete": True},
    "delays":          {"collection": "delays",          "tenant_scope": True,  "project_scope": True,  "soft_delete": False},
    "safety-events":   {"collection": "safety_events",   "tenant_scope": True,  "project_scope": True,  "soft_delete": False},
    "qc-issues":       {"collection": "qc_issues",       "tenant_scope": True,  "project_scope": True,  "soft_delete": False},
    "rfis":            {"collection": "rfis",            "tenant_scope": True,  "project_scope": True,  "soft_delete": True},
    "deliveries":      {"collection": "deliveries",      "tenant_scope": True,  "project_scope": True,  "soft_delete": True},
    "work-items":      {"collection": "work_items",      "tenant_scope": True,  "project_scope": True,  "soft_delete": True},
    "change-orders":   {"collection": "change_orders",   "tenant_scope": True,  "project_scope": True,  "soft_delete": True},
    "floors":          {"collection": "floors",          "tenant_scope": True,  "project_scope": True,  "soft_delete": False},
    "locations":       {"collection": "locations",       "tenant_scope": True,  "project_scope": True,  "soft_delete": False},
    "photos":          {"collection": "photos",          "tenant_scope": True,  "project_scope": True,  "soft_delete": False},
}


# Map ENTITY_CONFIG keys to audit table_name strings (mongo collection name
# with underscores). audit.write_audit hashes by table_name + record_id, so
# we want the same string the entity-specific routers use.
_AUDIT_TABLE = {k: v["collection"] for k, v in ENTITY_CONFIG.items()}

# Bucket C3a Phase 1 — entities whose generic CRUD PATCH should also
# sync to their gating_entities CPM-node row. Maps the URL entity key to
# the GatingEntity entity_type.
_GATING_ENTITY_TYPE = {
    "rfis": "rfi",
    "submittals": "submittal",
    "work-items": "field_issue",
}


def _cfg(entity: str) -> dict:
    cfg = ENTITY_CONFIG.get(entity)
    if not cfg:
        raise HTTPException(404, f"Unknown entity '{entity}'")
    return cfg


@router.patch("/crud/{entity}/{id}")
async def crud_patch(entity: str, id: str, payload: dict,
                      bg: BackgroundTasks,
                      ctx: dict = Depends(require_tenant)):
    cfg = _cfg(entity)
    db = get_db()
    coll = db[cfg["collection"]]
    # Bucket H Phase 4 -- snapshot the before-state when entity=crews so
    # we know if a supply-side field actually moved.
    before = None
    if entity == "crews":
        before = await coll.find_one(
            tenant_filter(ctx["tenant_id"], {"id": id}), {"_id": 0},
        )
    # Strip immutable fields
    for k in ("id", "tenant_id", "created_at", "created_by", "_id"):
        payload.pop(k, None)
    if not payload:
        raise HTTPException(400, "no fields to update")
    payload["updated_at"] = utc_now().isoformat()
    payload["updated_by"] = ctx["user"]["id"]
    res = await coll.update_one(
        tenant_filter(ctx["tenant_id"], {"id": id}), {"$set": payload},
    )
    if not res.matched_count:
        raise HTTPException(404, f"{entity} {id} not found")
    doc = await coll.find_one(tenant_filter(ctx["tenant_id"], {"id": id}), {"_id": 0})
    # Bucket H Phase 4 -- crew supply recompute on default_headcount /
    # trade / status change. Background so the PATCH response stays
    # snappy; recompute touches 90 capacity rows per crew.
    if entity == "crews" and before is not None:
        supply_keys = {"default_headcount", "target_size", "trade",
                        "company_name", "home_project_id", "status"}
        if any(k in payload for k in supply_keys):
            try:
                from routers.resources_router import _bg_recompute_capacity
                bg.add_task(_bg_recompute_capacity, ctx["tenant_id"], id)
            except Exception:
                pass
    # Bucket C3a Phase 1 — keep gating_entities row in sync when status
    # changes through this generic CRUD path. Only fires for entity types
    # that have a gating CPM-node mapping. Status field name varies by
    # entity; the status mapper in services.gating_entities handles each.
    ge_type = _GATING_ENTITY_TYPE.get(entity)
    if ge_type and "status" in payload:
        try:
            from services.gating_entities import sync_status
            await sync_status(
                tenant_id=ctx["tenant_id"], entity_type=ge_type, entity_id=id,
                underlying_status=doc.get("status"),
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
        except Exception:
            # Sync is best-effort here — don't fail the underlying patch
            # if the gating service is in an odd state.
            pass
    return doc


async def _cascade_gating_entity_on_delete(db, tenant_id: str, entity: str,
                                            entity_id: str, actor_id: str,
                                            request_id: str) -> None:
    """Bucket C3a Phase 5 — when an underlying entity (rfi / submittal /
    work_item) is soft-deleted via /crud/{entity}/{id} DELETE, cascade
    its gating_entities row away and clear any daily-report row that
    references it. We hard-delete the gating row (audit log captures
    it) rather than marking 'withdrawn' so the dropdown filter doesn't
    have to special-case soft state on top of soft state."""
    ge_type = _GATING_ENTITY_TYPE.get(entity)
    if not ge_type:
        return
    ge = await db.gating_entities.find_one(
        tenant_filter(tenant_id, {"entity_type": ge_type, "entity_id": entity_id}),
        {"_id": 0},
    )
    if not ge:
        return

    # 1. Find every DR row pointing at this gate. Tenant-guarded via the
    #    parent daily_report — we don't have a tenant_id directly on
    #    daily_report_activities (legacy). Use a $in over DRs we own.
    dr_ids = [d["id"] for d in await db.daily_reports.find(
        tenant_filter(tenant_id, {}), {"_id": 0, "id": 1},
    ).to_list(5000)]
    affected = await db.daily_report_activities.find(
        {"constraint_reference_entity_id": ge["id"], "daily_report_id": {"$in": dr_ids}},
        {"_id": 0},
    ).to_list(2000)

    # 2. Reset constraint fields on each affected row + per-row audit.
    for r in affected:
        new_state = {
            "constraint_type": None,
            "constraint_reference_entity_type": None,
            "constraint_reference_entity_id": None,
            "constraint_other_description": None,
            "constraint_auto_filled": False,
        }
        await db.daily_report_activities.update_one(
            {"id": r["id"]}, {"$set": new_state},
        )
        await write_audit(
            tenant_id=tenant_id, table_name="daily_report_activities", op="UPDATE",
            record_id=r["id"],
            before={k: r.get(k) for k in new_state},
            after={**new_state, "_cascade_reason": f"gating_entity {ge['id']} deleted"},
            actor_id=actor_id, request_id=request_id,
        )

    # 3. Hard-delete the gating entity + audit.
    await db.gating_entities.delete_one(
        tenant_filter(tenant_id, {"id": ge["id"]}),
    )
    await write_audit(
        tenant_id=tenant_id, table_name="gating_entities", op="GATING_ENTITY_DELETE",
        record_id=ge["id"],
        before=ge, after={"_cascade_reason": f"underlying {entity} {entity_id} deleted",
                            "rows_reset": len(affected)},
        actor_id=actor_id, request_id=request_id,
    )


@router.delete("/crud/{entity}/{id}")
async def crud_delete(entity: str, id: str, bg: BackgroundTasks,
                       ctx: dict = Depends(require_tenant)):
    cfg = _cfg(entity)
    db = get_db()
    coll = db[cfg["collection"]]
    table_name = _AUDIT_TABLE.get(entity, cfg["collection"])
    # Snapshot the row BEFORE we touch it so the audit entry has the full
    # before-state. Tenant-filtered so cross-tenant attempts return 404.
    before = await coll.find_one(tenant_filter(ctx["tenant_id"], {"id": id}), {"_id": 0})
    if not before:
        raise HTTPException(404, f"{entity} {id} not found")
    if cfg.get("soft_delete"):
        await coll.update_one(
            tenant_filter(ctx["tenant_id"], {"id": id}),
            {"$set": {"status": "deleted",
                      "deleted_at": utc_now().isoformat(),
                      "deleted_by": ctx["user"]["id"]}},
        )
        after = await coll.find_one(tenant_filter(ctx["tenant_id"], {"id": id}), {"_id": 0})
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name=table_name, op="UPDATE",
            record_id=id, before=before, after=after,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
        # Bucket H Phase 4 -- crew soft-delete: zero out future capacity,
        # preserve historical (recompute_capacity_for_crew checks status
        # and takes the archived branch). Background.
        if entity == "crews":
            try:
                from routers.resources_router import _bg_recompute_capacity
                bg.add_task(_bg_recompute_capacity, ctx["tenant_id"], id)
            except Exception:
                pass
        # Bucket C3a Phase 5 — cascade gating_entity + DR-row constraint
        # reset. Best-effort: failure here doesn't roll back the soft-
        # delete (the underlying row is already marked status=deleted
        # and a partial cascade is better than a 500).
        try:
            await _cascade_gating_entity_on_delete(
                db, ctx["tenant_id"], entity, id,
                ctx["user"]["id"], ctx["request_id"],
            )
        except Exception:
            pass
        return {"ok": True, "soft_deleted": True}
    else:
        await coll.delete_one(tenant_filter(ctx["tenant_id"], {"id": id}))
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name=table_name, op="DELETE",
            record_id=id, before=before, after=None,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
        # Same cascade for hard-delete entities (none in current config
        # are mapped to gating but kept for forward compat).
        try:
            await _cascade_gating_entity_on_delete(
                db, ctx["tenant_id"], entity, id,
                ctx["user"]["id"], ctx["request_id"],
            )
        except Exception:
            pass
        return {"ok": True, "hard_deleted": True}


# ============================================================
# Auto-detect rooms from a plan PDF (Claude vision)
# ============================================================
class AutoDetectRoomsRequest(StrictModel):
    drawing_revision_id: str
    floor_id: str
    page: int = 1
    create_locations: bool = True


@router.post("/spatial/auto-detect-rooms")
async def auto_detect_rooms(payload: AutoDetectRoomsRequest, ctx: dict = Depends(require_tenant)):
    """Render the plan PDF page, send to Claude vision, ask it to identify
    every room (label + polygon points). Optionally create a Location per
    detected room. Master prompt §22 — drawing_ingest agent."""
    from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    import pdf2image
    from PIL import Image

    db = get_db()
    rev = await db.drawing_revisions.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.drawing_revision_id}),
        {"_id": 0},
    )
    if not rev or not (rev.get("storage_path") or rev.get("pdf_url")):
        raise HTTPException(404, "Drawing revision not found")
    floor = await db.floors.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.floor_id}), {"_id": 0},
    )
    if not floor:
        raise HTTPException(404, "Floor not found")

    from services.storage import get_object
    pdf_bytes, _ = get_object(rev.get("storage_path") or rev.get("pdf_url"))
    try:
        images = pdf2image.convert_from_bytes(
            pdf_bytes, first_page=payload.page, last_page=payload.page, dpi=120,
        )
        img = images[0] if images else None
    except Exception as e:
        raise HTTPException(500, f"pdf2image: {e}")
    if img is None:
        raise HTTPException(500, "Could not render plan page")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()
    pdf_w_pt = float(rev.get("page_width") or rev.get("pdf_width_px") or img.size[0] * 72.0 / 120.0)
    pdf_h_pt = float(rev.get("page_height") or rev.get("pdf_height_px") or img.size[1] * 72.0 / 120.0)
    img_w_px, img_h_px = img.size

    sys = (
        "You are an architectural drafting assistant analysing a floor plan. "
        "Identify every distinct room or enclosed space (skip stairs, elevators, "
        "unlabeled corridors). Reply with a JSON object only, no prose, no "
        "markdown:\n\n"
        '{"rooms": [{"name": "Conference Room 305", "code": "305", '
        '"polygon": [[x1,y1],[x2,y2],...]}, ...]}\n\n'
        "polygon points are in image-pixel coordinates of the source image, in "
        "clockwise order, NOT closed (do not repeat the first point at end). "
        "Use the room number printed on the plan as 'code'; if the room has a "
        "labelled name (e.g. 'Conference Rm A') use that as 'name', otherwise "
        '"Room <code>". Skip rooms whose polygon would be smaller than 60 pixels.'
    )
    api_key = os.environ.get("EMERGENT_LLM_KEY", "")
    chat = LlmChat(
        api_key=api_key,
        session_id=f"rooms-{payload.drawing_revision_id}-{payload.floor_id}",
        system_message=sys,
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")
    try:
        msg = UserMessage(
            text="Identify every room on this floor plan. Return JSON only.",
            file_contents=[ImageContent(image_base64=img_b64)],
        )
        resp = await chat.send_message(msg)
    except Exception as e:
        raise HTTPException(502, f"LLM: {e}")

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(resp).strip(),
                     flags=re.IGNORECASE | re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except Exception:
        m = re.search(r"\{[\s\S]+\}", cleaned)
        data = json.loads(m.group(0)) if m else {"rooms": []}

    rooms = data.get("rooms", [])
    # Convert pixel coords back to PDF pt
    sx = pdf_w_pt / img_w_px
    sy = pdf_h_pt / img_h_px
    detected: list[dict] = []
    for r in rooms:
        try:
            polygon = [[round(p[0] * sx, 2), round(p[1] * sy, 2)] for p in r.get("polygon", [])]
            if len(polygon) < 3:
                continue
            detected.append({
                "name": r.get("name") or f"Room {r.get('code') or '?'}",
                "code": r.get("code") or "",
                "polygon": polygon,
            })
        except Exception:
            continue

    created: list[dict] = []
    if payload.create_locations:
        for r in detected:
            # Avoid duplicates by code
            existing = await db.locations.find_one(tenant_filter(ctx["tenant_id"], {
                "floor_id": payload.floor_id,
                "code": r["code"] or r["name"][:24],
            }), {"_id": 0, "id": 1})
            if existing:
                continue
            # Compute centroid for spatial_centroid
            cx = sum(p[0] for p in r["polygon"]) / len(r["polygon"])
            cy = sum(p[1] for p in r["polygon"]) / len(r["polygon"])
            doc = {
                "id": gen_id(),
                "tenant_id": ctx["tenant_id"],
                "project_id": floor["project_id"],
                "floor_id": payload.floor_id,
                "zone_id": None,
                "code": (r["code"] or r["name"][:24]) or "rm",
                "name": r["name"],
                "location_type": "room",
                "area_sqft": None,
                "spatial_centroid": {"x": round(cx, 2), "y": round(cy, 2)},
                "polygon_geojson": {
                    "type": "Polygon",
                    "coordinates": [r["polygon"] + [r["polygon"][0]]],
                },
                "created_at": utc_now().isoformat(),
                "created_by": ctx["user"]["id"],
            }
            await db.locations.insert_one(doc.copy())
            created.append({"id": doc["id"], "code": doc["code"], "name": doc["name"]})
    return {
        "detected_count": len(detected),
        "created_count": len(created),
        "detected": detected,
        "created": created,
    }


# ============================================================
# Onboarding / getting-started workflow
# ============================================================
ONBOARDING_STEPS = [
    {
        "id": "01_project",
        "title": "Confirm your project",
        "body": "ICE Schedule Ops is multi-tenant. Make sure the right project is selected in the top bar (next to the search). Every entity you create is scoped to that project.",
        "action": "Select a project in the top bar.",
        "route": "/projects",
    },
    {
        "id": "02_master_doc",
        "title": "Fill in the project master doc",
        "body": "Tell the platform what kind of build this is — new construction, renovation, tenant improvement, or infrastructure. Set occupancy, hazmat, and security flags. Every AI agent reads this on every call, so a renovation project gets phasing-aware suggestions while new construction gets foundation-out sequencing.",
        "action": "Open Master doc in the sidebar.",
        "route": "/master-doc",
    },
    {
        "id": "03_floors",
        "title": "Define the floors",
        "body": "Floors are the vertical organisation of your building. Each floor has a number, name, elevation in feet, and a height in feet. Locations (rooms, zones) hang off a floor.",
        "action": "Use Locations page or call the copilot: 'Add a floor: number 3, name Mezzanine, elevation 24 feet, height 12 feet.'",
        "route": "/locations",
    },
    {
        "id": "04_drawings",
        "title": "Upload your plan PDFs",
        "body": "Upload at least one floor plan PDF per floor. The drawing won't auto-create rooms by itself — see the next step. Keep one PDF per sheet (e.g. A-101 Floor 1, A-102 Floor 2).",
        "action": "Drawings page → Upload drawing.",
        "route": "/drawings",
    },
    {
        "id": "05_rooms_auto",
        "title": "Auto-detect rooms from a plan (Claude vision)",
        "body": "Open Polygon editor, pick a floor, pick the plan revision you uploaded for that floor, then click 'AI auto-detect rooms'. Claude reads the plan and creates a Location record for every room it finds, complete with the room polygon and centroid.",
        "action": "Polygon editor → choose floor + drawing → AI auto-detect rooms.",
        "route": "/polygon-editor",
    },
    {
        "id": "06_rooms_manual",
        "title": "Or draw rooms manually",
        "body": "If the auto-detect misses a room (irregular shape, faint walls), pick that room in the dropdown and click on the plan to draw the polygon point-by-point. Last point closes back to the first.",
        "action": "Polygon editor → click on the plan to draw points.",
        "route": "/polygon-editor",
    },
    {
        "id": "07_calibrate",
        "title": "Calibrate the plan scale",
        "body": "In the drawing viewer, pick the Calibrate tool, click the two ends of a known dimension on the plan (often a printed scale bar), and enter the real-world distance in feet/inches. This stores ft-per-px for that revision so polygon coordinates map to real square footage.",
        "action": "Drawings → open a drawing → Calibrate tool.",
        "route": "/drawings",
    },
    {
        "id": "08_specs",
        "title": "Upload specifications",
        "body": "Upload the spec book PDF (CSI MasterFormat). The platform extracts every section (e.g. 03 30 00 Cast-In-Place Concrete), builds a TF-IDF semantic search index, and auto-generates a Submittal log row for every '1.4 SUBMITTALS' paragraph.",
        "action": "Specs page → Upload spec PDF.",
        "route": "/specs",
    },
    {
        "id": "09_schedule",
        "title": "Import the schedule",
        "body": "Upload your existing Primavera P6 (.xer) or MS Project (.mpp/.mpx) file directly. The platform uses MPXJ (Java) to parse it — tasks come in with their planned start/finish, durations, predecessors, and milestone flags. Or upload CSV/XLSX if you don't have those.",
        "action": "Imports → Schedule import.",
        "route": "/imports",
    },
    {
        "id": "10_resources",
        "title": "Set up crews, equipment, and people",
        "body": "Resources are the actors: crews, equipment, and individuals (with certifications and prevailing-wage rates). Tasks consume resources for cost forecasting and conflict detection.",
        "action": "Resources → Crews / Equipment / People / Certifications.",
        "route": "/resources/crews",
    },
    {
        "id": "11_daily",
        "title": "File daily reports",
        "body": "Each day, the field super opens Daily reports for the date, fills out weather + activity rows (each task gets pct_complete_start_of_day, pct_complete_end_of_day, crew_count, hours_worked, delay_minutes), and submits. The platform auto-rolls into the weekly PDF.",
        "action": "Daily reports → New report for today.",
        "route": "/daily-reports",
    },
    {
        "id": "12_copilot",
        "title": "Ask the AI copilot to do work for you",
        "body": "Click the Bot icon in the bottom-right. Ask: 'Add a task: install conference-room AV, duration 4 days', 'Create RFI about ceiling height conflict in Room 305', 'Mark A1130 as 75 percent complete', 'Show me all open RFIs'. The copilot reads your master doc, your spec sections, and past resolved RFIs (knowledge base) before answering.",
        "action": "Click the blue Bot button bottom-right.",
        "route": None,
    },
]


@router.get("/onboarding/getting-started")
async def get_onboarding(ctx: dict = Depends(require_tenant)):
    """Plain-text walkthrough — no markdown anywhere."""
    db = get_db()
    state: dict = {}
    # progress hints — does the project have any of these yet?
    project_id = None
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"]), {"_id": 0, "id": 1})
    if proj:
        project_id = proj["id"]
        state["02_master_doc"] = bool(await db.master_docs.find_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0}))
        state["03_floors"] = bool(await db.floors.find_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0}))
        state["04_drawings"] = bool(await db.drawings.find_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0}))
        state["05_rooms_auto"] = (await db.locations.count_documents(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id}))) > 0
        state["08_specs"] = bool(await db.specs.find_one(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0}))
        state["09_schedule"] = (await db.tasks.count_documents(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id}))) > 0
        state["10_resources"] = (await db.crews.count_documents(
            tenant_filter(ctx["tenant_id"]))) > 0
        state["11_daily"] = (await db.daily_reports.count_documents(
            tenant_filter(ctx["tenant_id"], {"project_id": project_id}))) > 0
    return {
        "steps": [{**s, "complete": state.get(s["id"], False)} for s in ONBOARDING_STEPS],
        "completed_count": sum(1 for v in state.values() if v),
        "total": len(ONBOARDING_STEPS),
    }
