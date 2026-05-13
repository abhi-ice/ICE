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
    # Addition 5 (2026-05-12) -- "add to locked DR" flow.
    add_reason = doc.pop("add_reason", None)
    # Bucket D Corrective -- master-schedule task linkage required min-1.
    # Coexists with the C3a gating_entities back-pointer (still set
    # below by create_for_entity).
    from services.dr_activity_linkage import validate_linked_task_ids
    doc["linked_task_ids"] = await validate_linked_task_ids(
        tenant_id=ctx["tenant_id"],
        project_id=doc["project_id"],
        task_ids=doc.get("linked_task_ids") or [],
        required=True,
    )
    # Followup-fix (2026-05-07): preserve user-supplied response_text
    # (frontend "answer" field) instead of wiping it. Status is always
    # forced to draft on create -- the state machine is unchanged
    # pending Bucket B.
    doc.update({
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "rfi_number": rfi_number,
        "date_initiated": utc_now().date().isoformat(),
        "date_responded": None,
        "status": "draft",
        "cost_impact_estimated_usd": None,
        "time_impact_estimated_days": None,
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    })
    # Addition 5 -- locked-DR add policy (skipped on supplement path).
    is_locked_add = False
    if not doc.get("is_supplement") and doc.get("daily_report_id"):
        from services.edit_permissions import enforce_locked_dr_add_policy
        _dr, is_locked_add = await enforce_locked_dr_add_policy(
            ctx=ctx, daily_report_id=doc["daily_report_id"],
            add_reason=add_reason, event_type="rfi",
        )
    # Bucket D Phase 4 -- supplement metadata + audit.
    from services.dr_activity_linkage import (
        stamp_supplement_metadata, validate_supplement_parent_locked,
    )
    if doc.get("is_supplement") and doc.get("parent_daily_report_id"):
        await validate_supplement_parent_locked(
            tenant_id=ctx["tenant_id"],
            parent_daily_report_id=doc["parent_daily_report_id"],
        )
    sup_audit = stamp_supplement_metadata(doc, actor_id=ctx["user"]["id"], fact_type="rfi")
    await db.rfis.insert_one(doc.copy())
    await write_audit(tenant_id=ctx["tenant_id"], table_name="rfis", op="INSERT",
                      record_id=doc["id"], before=None, after=doc,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    if sup_audit:
        await write_audit(
            tenant_id=ctx["tenant_id"], table_name="rfis", op="SUPPLEMENT_FILED",
            record_id=doc["id"], before=None, after=sup_audit,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    if is_locked_add:
        from services.edit_permissions import stamp_add_metadata
        await stamp_add_metadata(
            db=db, collection="rfis", record_id=doc["id"],
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            add_reason=add_reason or "", after=doc,
            request_id=ctx["request_id"],
        )
    # Bucket C3a Phase 1 — auto-create the gating_entities CPM-node row
    # and write its id back onto the RFI. Schedule_activity_id (if set)
    # populates gates_task_ids; a one-task RFI is the common case.
    from services.gating_entities import create_for_entity
    gates = [doc["schedule_activity_id"]] if doc.get("schedule_activity_id") else []
    ge = await create_for_entity(
        tenant_id=ctx["tenant_id"], project_id=doc["project_id"],
        entity_type="rfi", entity=doc, gates_task_ids=gates,
    )
    await db.rfis.update_one(
        tenant_filter(ctx["tenant_id"], {"id": doc["id"]}),
        {"$set": {"gating_entity_id": ge["id"]}},
    )
    doc["gating_entity_id"] = ge["id"]
    # Bug #50 (2026-05-13) -- recompute blocker count on each linked
    # task so a freshly-created draft RFI immediately surfaces as a
    # blocker on the task. Mirrors the delay create pattern.
    affected_task_ids: set = set(doc.get("linked_task_ids") or [])
    if doc.get("schedule_activity_id"):
        affected_task_ids.add(doc["schedule_activity_id"])
    if affected_task_ids:
        from services.task_status import compute_and_persist_status
        for tid in affected_task_ids:
            await compute_and_persist_status(
                db, ctx["tenant_id"], tid,
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
    return Rfi(**doc)


@router.get("/projects/{project_id}/rfis", response_model=list[Rfi])
async def list_rfis(project_id: str, status_filter: Optional[str] = None,
                     affected_activity_id: Optional[str] = None,
                     ctx: dict = Depends(require_tenant)):
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
    # Bucket D Corrective -- gallery filter on master-schedule tasks.
    if affected_activity_id:
        f["linked_task_ids"] = affected_activity_id
    docs = await db.rfis.find(f, {"_id": 0}).sort("date_initiated", -1).to_list(500)
    return [Rfi(**d) for d in docs]


@router.patch("/rfis/{rfi_id}", response_model=Rfi)
async def update_rfi(rfi_id: str, payload: RfiUpdate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    raw = payload.model_dump(mode="json", exclude_unset=True)
    edit_reason = raw.pop("edit_reason", None)
    updates = {k: v for k, v in raw.items() if v is not None}
    before = await db.rfis.find_one(tenant_filter(ctx["tenant_id"], {"id": rfi_id}), {"_id": 0})
    if not before:
        raise HTTPException(404, "RFI not found")
    # Addition 4 -- locked-DR edit policy.
    from services.edit_permissions import (
        enforce_locked_dr_edit_policy, stamp_edit_metadata,
    )
    _parent_dr, is_locked_edit = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before.get("daily_report_id"),
        edit_reason=edit_reason, event_type="rfi", event_id=rfi_id,
    )
    # Bucket D Corrective -- re-validate linked_task_ids on PATCH
    # against master schedule. Linkage is required min-1 on POST so
    # the patch keeps the invariant.
    if "linked_task_ids" in updates:
        from services.dr_activity_linkage import validate_linked_task_ids
        updates["linked_task_ids"] = await validate_linked_task_ids(
            tenant_id=ctx["tenant_id"],
            project_id=before["project_id"],
            task_ids=updates["linked_task_ids"],
            required=True,
        )
    await db.rfis.update_one(tenant_filter(ctx["tenant_id"], {"id": rfi_id}), {"$set": updates})
    after = await db.rfis.find_one(tenant_filter(ctx["tenant_id"], {"id": rfi_id}), {"_id": 0})
    await write_audit(tenant_id=ctx["tenant_id"], table_name="rfis", op="UPDATE",
                      record_id=rfi_id, before=before, after=after,
                      actor_id=ctx["user"]["id"], request_id=ctx["request_id"])
    # Bucket C3a Phase 1 — mirror status / response timestamps onto the
    # gating_entities CPM node. Subject change → label denorm refresh.
    if "status" in updates or "subject" in updates or "date_responded" in updates:
        from services.gating_entities import sync_status, build_display_label
        from datetime import datetime as _dt, time as _time
        responded_at = None
        if updates.get("date_responded"):
            try:
                responded_at = _dt.combine(date.fromisoformat(str(updates["date_responded"])[:10]), _time.min)
            except Exception:
                responded_at = None
        new_label = None
        if "subject" in updates:
            _, new_label = build_display_label("rfi", after)
        await sync_status(
            tenant_id=ctx["tenant_id"], entity_type="rfi", entity_id=rfi_id,
            underlying_status=after.get("status"),
            actual_responded_at=responded_at,
            new_display_label=new_label,
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
    # Addition 4 -- post-submit edit audit + metadata stamp.
    if is_locked_edit:
        after = await db.rfis.find_one(tenant_filter(ctx["tenant_id"], {"id": rfi_id}), {"_id": 0})
        await stamp_edit_metadata(
            db=db, collection="rfis", record_id=rfi_id,
            tenant_id=ctx["tenant_id"], actor_id=ctx["user"]["id"],
            edit_reason=edit_reason, before=before, after=after,
            request_id=ctx["request_id"],
        )

    # Bug #50 (2026-05-13) -- recompute blocker count on every linked
    # task when the RFI's blocking-status changes. Mirrors the delay
    # PATCH pattern: status transitions in [draft/sent/in_review] <->
    # [answered/closed/voided] flip whether this RFI counts as a
    # blocker (count_open_blockers in services/task_status.py). If
    # linkage changed we refresh OLD linked tasks too so they
    # un-blocker. Idempotent: compute_and_persist_status only writes
    # when fields actually change.
    BLOCKING_RFI_STATUSES = {"draft", "sent", "in_review"}
    status_blocking_changed = (
        (before.get("status") in BLOCKING_RFI_STATUSES)
        != (after.get("status") in BLOCKING_RFI_STATUSES)
    )
    linkage_changed = (
        set(before.get("linked_task_ids") or []) != set(after.get("linked_task_ids") or [])
        or before.get("schedule_activity_id") != after.get("schedule_activity_id")
    )
    if status_blocking_changed or linkage_changed:
        from services.task_status import compute_and_persist_status
        affected: set = set()
        for t in (before.get("linked_task_ids") or []):
            affected.add(t)
        for t in (after.get("linked_task_ids") or []):
            affected.add(t)
        if before.get("schedule_activity_id"):
            affected.add(before["schedule_activity_id"])
        if after.get("schedule_activity_id"):
            affected.add(after["schedule_activity_id"])
        for tid in affected:
            await compute_and_persist_status(
                db, ctx["tenant_id"], tid,
                actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
            )
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
    now_iso = utc_now().isoformat()
    doc = payload.model_dump(mode="json")
    doc.update({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "status": "open",
        "actual_cost_impact": None, "actual_time_impact_days": None,
        "opened_by": ctx["user"]["id"], "opened_at": now_iso,
        "owned_by": ctx["user"]["id"],
        "closed_at": None, "closure_summary": None,
        "created_at": now_iso,
    })
    await db.work_items.insert_one(doc.copy())
    # Bucket C3a Phase 1 — auto-create gating_entities row for the field
    # issue. WorkItems don't carry a single schedule_activity_id; gating
    # link comes from work_item_links (entity_type='task') created later.
    # Phase 1 leaves gates_task_ids empty here and lets a later sync /
    # link patch populate it.
    from services.gating_entities import create_for_entity
    ge = await create_for_entity(
        tenant_id=ctx["tenant_id"], project_id=doc["project_id"],
        entity_type="field_issue", entity=doc, gates_task_ids=[],
    )
    await db.work_items.update_one(
        tenant_filter(ctx["tenant_id"], {"id": doc["id"]}),
        {"$set": {"gating_entity_id": ge["id"]}},
    )
    doc["gating_entity_id"] = ge["id"]
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
