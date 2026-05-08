"""Bucket C3b — coordination event log.

Phase 1: full CRUD on coordination_events. Quick-log from daily report
goes through the same POST endpoint with linked_daily_report_id set.
Phase 2 (voice extractor) creates rows here too with voice_extracted=True.

All endpoints tenant-scoped. Soft-delete only — coordination history is
part of the federal-contract record.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List

from database import get_db, tenant_filter
from models import (
    CoordinationEvent, CoordinationEventCreate, CoordinationEventUpdate,
    gen_id, utc_now,
)
from auth import require_tenant
from audit import write_audit


router = APIRouter(tags=["coordination-events"])


@router.post("/coordination-events", response_model=CoordinationEvent)
async def create_coordination_event(
    payload: CoordinationEventCreate,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.project_id}),
        {"_id": 0, "id": 1},
    )
    if not proj:
        raise HTTPException(404, "Project not found")
    now_iso = utc_now().isoformat()
    doc = payload.model_dump(mode="json")
    # Bucket D Phase 1 — DR-activity linkage OPTIONAL for coordination
    # (some events are project-wide). Validation just confirms ids are
    # real if provided. linked_daily_report_id (existing field) is the
    # scope DR for the activity-id check.
    from services.dr_activity_linkage import validate_linked_dr_activity_ids
    doc["linked_dr_activity_ids"] = await validate_linked_dr_activity_ids(
        tenant_id=ctx["tenant_id"],
        daily_report_id=doc.get("linked_daily_report_id"),
        activity_ids=doc.get("linked_dr_activity_ids") or [],
        required=False,
        parent_daily_report_id=doc.get("parent_daily_report_id"),
    )
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "created_at": now_iso,
        "created_by_user_id": ctx["user"]["id"],
        "updated_at": now_iso,
        "deleted_at": None,
        "deleted_by_user_id": None,
    })
    # Bucket D Phase 4 -- supplement metadata + audit.
    from services.dr_activity_linkage import (
        stamp_supplement_metadata, validate_supplement_parent_locked,
    )
    if doc.get("is_supplement") and doc.get("parent_daily_report_id"):
        await validate_supplement_parent_locked(
            tenant_id=ctx["tenant_id"],
            parent_daily_report_id=doc["parent_daily_report_id"],
        )
    sup_audit = stamp_supplement_metadata(doc, actor_id=ctx["user"]["id"], fact_type="coordination_event")
    await db.coordination_events.insert_one(doc.copy())
    if sup_audit:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="coordination_events",
            op="SUPPLEMENT_FILED",
            record_id=doc["id"], before=None, after=sup_audit,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="coordination_events",
        op="COORDINATION_EVENT_CREATE", record_id=doc["id"],
        before=None, after=doc,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return CoordinationEvent(**doc)


@router.get("/projects/{project_id}/coordination-events")
async def list_coordination_events(
    project_id: str,
    topic: Optional[str] = Query(None, description="Single topic filter"),
    channel: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="ISO date inclusive"),
    date_to: Optional[str] = Query(None, description="ISO date inclusive"),
    linked_entity_id: Optional[str] = Query(
        None,
        description="Match if this id appears in any of the linked_* arrays "
                    "(rfi/field_issue/submittal/bulletin/activity/gating_entity) "
                    "or equals linked_daily_report_id.",
    ),
    include_deleted: bool = Query(False),
    affected_activity_id: Optional[str] = Query(None),
    ctx: dict = Depends(require_tenant),
):
    """Sidebar list — sortable filters per spec. Cap 500 rows; default
    sort newest first by occurred_at."""
    db = get_db()
    f: dict = {"project_id": project_id}
    if not include_deleted:
        f["deleted_at"] = None
    if topic:
        f["topic"] = topic
    if channel:
        f["channel"] = channel
    if direction:
        f["direction"] = direction
    # Bucket D Phase 3 -- gallery filter on linked_dr_activity_ids
    # (Phase 1 added the field; this is the matching list filter).
    if affected_activity_id:
        f["linked_dr_activity_ids"] = affected_activity_id
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            # Inclusive — bump to end of day so callers can pass a date.
            rng["$lte"] = date_to + "T23:59:59" if len(date_to) == 10 else date_to
        f["occurred_at"] = rng
    if linked_entity_id:
        f["$or"] = [
            {"linked_rfi_ids": linked_entity_id},
            {"linked_field_issue_ids": linked_entity_id},
            {"linked_submittal_ids": linked_entity_id},
            {"linked_bulletin_ids": linked_entity_id},
            {"linked_activity_ids": linked_entity_id},
            {"linked_gating_entity_ids": linked_entity_id},
            {"linked_daily_report_id": linked_entity_id},
        ]
    docs = await db.coordination_events.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("occurred_at", -1).to_list(500)
    return [CoordinationEvent(**d).model_dump(mode="json") for d in docs]


@router.get("/coordination-events/{event_id}", response_model=CoordinationEvent)
async def get_coordination_event(event_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.coordination_events.find_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "Coordination event not found")
    return CoordinationEvent(**doc)


def _can_edit(ctx: dict, doc: dict) -> bool:
    """Phase 1 ownership rule: creator OR admin role."""
    user = ctx.get("user") or {}
    if user.get("role") == "admin":
        return True
    return doc.get("created_by_user_id") == user.get("id")


@router.patch("/coordination-events/{event_id}", response_model=CoordinationEvent)
async def update_coordination_event(
    event_id: str, payload: CoordinationEventUpdate,
    ctx: dict = Depends(require_tenant),
):
    db = get_db()
    before = await db.coordination_events.find_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Coordination event not found")
    if before.get("deleted_at"):
        raise HTTPException(409, "Cannot edit a deleted coordination event.")
    if not _can_edit(ctx, before):
        raise HTTPException(403, "Only the event creator or admin can edit this event.")
    raw = payload.model_dump(mode="json", exclude_unset=True)
    edit_reason = raw.pop("edit_reason", None)
    updates = {k: v for k, v in raw.items() if v is not None}
    if not updates and not edit_reason:
        return CoordinationEvent(**before)
    # Addition 4 -- locked-DR edit policy. Coordination uses
    # linked_daily_report_id (different field name).
    from services.edit_permissions import (
        enforce_locked_dr_edit_policy, stamp_edit_metadata,
    )
    _parent_dr, is_locked_edit = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before.get("linked_daily_report_id"),
        edit_reason=edit_reason, event_type="coordination", event_id=event_id,
    )
    # Bucket D verification fix -- re-validate linked_dr_activity_ids
    # when the patch attempts to replace it. Linkage is OPTIONAL for
    # coordination per spec, so required=False (empty list is allowed
    # to clear the linkage), but anything that IS present must resolve
    # to real rows on the parent DR.
    if "linked_dr_activity_ids" in updates:
        from services.dr_activity_linkage import validate_linked_dr_activity_ids
        # Coordination uses linked_daily_report_id (different field name).
        scope_dr = updates.get("linked_daily_report_id") or before.get("linked_daily_report_id")
        updates["linked_dr_activity_ids"] = await validate_linked_dr_activity_ids(
            tenant_id=ctx["tenant_id"],
            daily_report_id=scope_dr,
            activity_ids=updates["linked_dr_activity_ids"],
            required=False,
            parent_daily_report_id=before.get("parent_daily_report_id"),
        )
    updates["updated_at"] = utc_now().isoformat()
    await db.coordination_events.update_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"$set": updates},
    )
    after = await db.coordination_events.find_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="coordination_events",
        op="COORDINATION_EVENT_UPDATE", record_id=event_id,
        before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # Addition 4 -- post-submit edit audit + metadata stamp.
    if is_locked_edit:
        after = await db.coordination_events.find_one(
            tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
        )
        await stamp_edit_metadata(
            db=db, collection="coordination_events", record_id=event_id,
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            edit_reason=edit_reason, before=before, after=after,
            request_id=ctx["request_id"],
        )
    return CoordinationEvent(**after)


@router.delete("/coordination-events/{event_id}")
async def delete_coordination_event(event_id: str, ctx: dict = Depends(require_tenant)):
    """Soft-delete: stamps deleted_at + deleted_by; list filter hides
    these by default. Restore would be a future endpoint if ever needed."""
    db = get_db()
    before = await db.coordination_events.find_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Coordination event not found")
    if before.get("deleted_at"):
        return {"ok": True, "already_deleted": True}
    if not _can_edit(ctx, before):
        raise HTTPException(403, "Only the event creator or admin can delete this event.")
    now_iso = utc_now().isoformat()
    await db.coordination_events.update_one(
        tenant_filter(ctx["tenant_id"], {"id": event_id}),
        {"$set": {"deleted_at": now_iso,
                   "deleted_by_user_id": ctx["user"]["id"],
                   "updated_at": now_iso}},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="coordination_events",
        op="COORDINATION_EVENT_DELETE", record_id=event_id,
        before=before, after={"deleted_at": now_iso,
                                "deleted_by_user_id": ctx["user"]["id"]},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ok": True}
