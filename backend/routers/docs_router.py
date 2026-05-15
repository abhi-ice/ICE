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


# =====================================================================
# CPM Part J (2026-05-15) -- RFI per_task_impacts verdict resolver.
#
# Mirror of routers.field_events_router._resolve_task_impacts (Part I
# delay flow) but for RFI rows. Stamps the same 5 server-only verdict
# keys on each per_task_impacts entry:
#
#     float_at_logging_days
#     prior_open_impacts_at_logging_days
#     remaining_float_at_logging_days
#     consumes_float_only
#     pushes_critical_path
#
# PRIOR-OPEN SCOPE (J-2 STUB):
#   Currently calls routers.field_events_router._sum_prior_open_delay_
#   impacts_on_task -- which walks db.delays ONLY. RFI prior impacts
#   on the same task are NOT yet included in the sum. Part J-5 extends
#   the helper to also walk db.rfis (with status in [draft, sent,
#   in_review]) and accept an exclude_rfi_id kwarg. After J-5 ships,
#   the call below picks up cross-collection prior-sum without any
#   change here.
#
#   Until J-5, RFIs do not contribute to each other's prior-sum:
#     - First RFI on a task: verdict identical to Part I delay flow
#     - Subsequent RFIs on same task: each reads structural - prior_delays
#       (does not subtract prior RFIs from the same task)
#   The most common case -- first RFI on a critical-path task with no
#   prior delays -- is fully correct under J-2.
#
# exclude_rfi_id is plumbed through the signature but unused in J-2.
# J-5 will thread it into _sum_prior_open_delay_impacts_on_task once
# that helper grows an exclude_rfi_id kwarg.
# =====================================================================
async def _resolve_rfi_task_impacts(
    db, tenant_id: str, rfi_doc: dict, *,
    project_id: Optional[str] = None,
    exclude_rfi_id: Optional[str] = None,
) -> tuple[bool, list[dict]]:
    """Compute + stamp the 5 server-only verdict keys on each
    per_task_impacts entry in `rfi_doc`. Returns (changed, enriched).

    `changed` is True iff any item in the returned list differs from
    the input in any of the 5 stamped keys -- callers can short-
    circuit the DB write when nothing moved.

    Args:
        db: motor database handle.
        tenant_id: required.
        rfi_doc: dict with per_task_impacts + project_id. The id
            field is read for exclude_rfi_id fallback (defaults to
            rfi_doc.id when the kwarg is omitted).
        project_id: kwarg override (defaults to rfi_doc.project_id).
            Required because the prior-sum query is project-scoped.
        exclude_rfi_id: kwarg override (defaults to rfi_doc.id).
            Plumbed for J-5 (RFI walk in prior-sum). Unused in J-2.

    Returns:
        (changed: bool, enriched: list[dict])
    """
    impacts_in = list(rfi_doc.get("per_task_impacts") or [])
    if not impacts_in:
        # No impacts on this RFI -- nothing to stamp. Returning
        # (False, []) lets the caller short-circuit the DB write.
        return (False, [])

    proj_id = project_id or rfi_doc.get("project_id")
    # CPM Part J-5 (2026-05-15) -- exclude_rfi_id is now CONSUMED.
    # When re-resolving an existing RFI's snapshot (e.g., the post-
    # auto_recompute re-stamp in update_rfi or the post-insert
    # re-stamp in create_rfi), we must exclude THIS RFI from the
    # prior-sum so its own impact_days don't double-count.
    excl_rfi = exclude_rfi_id if exclude_rfi_id is not None else rfi_doc.get("id")

    task_ids = [i["task_id"] for i in impacts_in if i.get("task_id")]
    if not task_ids:
        return (False, [])

    tasks = await db.tasks.find(
        tenant_filter(tenant_id, {"id": {"$in": task_ids}}),
        {"_id": 0, "id": 1, "total_float_days": 1, "is_critical_path": 1},
    ).to_list(len(task_ids))
    by_id = {t["id"]: t for t in tasks}

    # CPM Part J-5 (2026-05-15) -- helper renamed +
    # cross-collection walk (db.delays + db.rfis). The J-2 stub
    # ("delay-only prior-sum") is now closed: prior RFIs on the same
    # task contribute alongside prior delays, modulo the
    # exclude_rfi_id self-exclusion.
    from routers.field_events_router import _sum_prior_open_blocker_impacts_on_task

    out: list[dict] = []
    changed = False
    for item in impacts_in:
        tid = item.get("task_id")
        if not tid:
            # The Part J-1 validator drops items without task_id, but
            # defensive guard here too -- legacy backfilled rows
            # shouldn't reach this path, but if they do we skip
            # silently rather than 422.
            continue
        t = by_id.get(tid) or {}
        impact = float(item.get("projected_impact_days") or 0)
        f = t.get("total_float_days")
        f_val = float(f) if f is not None else None
        is_cp = bool(t.get("is_critical_path"))

        if proj_id is not None and f_val is not None:
            # CPM Part J-5 (2026-05-15) -- helper now walks both
            # collections. Pass exclude_rfi_id so THIS RFI's own
            # impacts don't count as prior to itself. exclude_delay_id
            # stays None -- an RFI is not a delay, so nothing to
            # exclude from the delay side of the walk.
            prior = await _sum_prior_open_blocker_impacts_on_task(
                db, tenant_id, proj_id, tid,
                exclude_delay_id=None,
                exclude_rfi_id=excl_rfi,
            )
            # Clamp at 0 -- can't have negative float in the verdict.
            remaining = max(0.0, f_val - prior)
            consumes = impact <= remaining
            pushes_cp = impact > remaining
        else:
            # Fallback path: structural float unknown (compute_cpm
            # has never run on this project) OR project_id resolution
            # failed. Mirror Part I delay-flow fallback so the
            # snapshot isn't broken in transient states.
            prior = 0.0
            remaining = None
            consumes = False
            pushes_cp = is_cp

        merged = dict(item)
        merged["task_id"] = tid
        # Keep impact_days as int per the RFI integer convention
        # (Part J-1 validator already enforced this at the API layer;
        # the int() here is defense-in-depth for legacy rows that
        # may have stored 3.0 instead of 3).
        merged["projected_impact_days"] = int(impact)
        merged["float_at_logging_days"] = f_val
        merged["prior_open_impacts_at_logging_days"] = prior
        merged["remaining_float_at_logging_days"] = remaining
        merged["consumes_float_only"] = consumes
        merged["pushes_critical_path"] = pushes_cp
        out.append(merged)

        # Change detector -- True iff any of the 5 stamped keys
        # differs from what the input carried. Empty/missing keys on
        # input (typical for fresh user-supplied items) always count
        # as changed since None != f_val for any populated float.
        if (
            item.get("float_at_logging_days") != f_val
            or item.get("prior_open_impacts_at_logging_days") != prior
            or item.get("remaining_float_at_logging_days") != remaining
            or item.get("consumes_float_only") != consumes
            or item.get("pushes_critical_path") != pushes_cp
        ):
            changed = True

    return (changed, out)


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

    # CPM Part J-2 (2026-05-15) -- auto-recompute fires BEFORE the
    # audit so _resolve_rfi_task_impacts below reads fresh
    # total_float_days, and the INSERT audit captures the stamped
    # per_task_impacts. Mirrors the Part E/I delay-flow ordering.
    # Fail-safe: a CPM bug cannot block the RFI from being saved
    # (already in DB above).
    from services.cpm import auto_recompute_after_event
    await auto_recompute_after_event(
        ctx["tenant_id"], doc["project_id"],
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    # CPM Part J-2 -- stamp server-only verdict keys on
    # per_task_impacts. Idempotent on RFIs with empty
    # per_task_impacts (returns (False, []) and skips the DB write).
    # The 5 stamp fields ride into the INSERT audit via doc.
    if doc.get("per_task_impacts"):
        impacts_changed, stamped = await _resolve_rfi_task_impacts(
            db, ctx["tenant_id"], doc,
        )
        if impacts_changed:
            doc["per_task_impacts"] = stamped
            await db.rfis.update_one(
                tenant_filter(ctx["tenant_id"], {"id": doc["id"]}),
                {"$set": {"per_task_impacts": stamped}},
            )

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
    # blocker on the task. Mirrors the delay create pattern. NO float
    # dependency, so its position relative to the audit is unchanged.
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
    # CPM Part J-2 (2026-05-15) -- the trailing auto_recompute_after_event
    # that used to live here was folded into the block above the audit
    # (Part E/I ordering). Until Part J-4 ships, compute_projected_finish_dates
    # still walks delays only -- this RFI's impact doesn't push Slip on
    # downstream tasks yet. After J-4, the helper extension picks up RFI
    # impacts and the cascade math fully reflects them on the next event.
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
    # Bug #49 (2026-05-13): pass updates + before so the policy can
    # detect RFI status-only transitions (draft -> sent -> answered
    # -> closed with response_text) initiated from /rfis and skip
    # the edit_reason 422.
    _parent_dr, is_locked_edit, effective_edit_reason = await enforce_locked_dr_edit_policy(
        ctx=ctx, daily_report_id=before.get("daily_report_id"),
        edit_reason=edit_reason, event_type="rfi", event_id=rfi_id,
        updates=updates, before=before,
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

    # CPM Part J-2 (2026-05-15) -- gate the cascade + verdict-resolve
    # block on three independently-flipping conditions:
    #   1. status crossed in/out of {draft, sent, in_review}
    #      (blocker-aggregation surface changes; matches Bug #50
    #      pattern already in production)
    #   2. linked_task_ids OR schedule_activity_id changed
    #      (the set of tasks this RFI gates moved)
    #   3. per_task_impacts in the patch  (the impact estimates
    #      themselves moved -- needs re-stamp even if status +
    #      linkage didn't change)
    # When none of the three fired, this was a routine edit
    # (subject text, recipient change, etc.) and we skip the cascade
    # entirely -- preserving pre-J behavior for cosmetic patches.
    BLOCKING_RFI_STATUSES = {"draft", "sent", "in_review"}
    status_blocking_changed = (
        (before.get("status") in BLOCKING_RFI_STATUSES)
        != (after.get("status") in BLOCKING_RFI_STATUSES)
    )
    linkage_changed = (
        set(before.get("linked_task_ids") or []) != set(after.get("linked_task_ids") or [])
        or before.get("schedule_activity_id") != after.get("schedule_activity_id")
    )
    impacts_changed = "per_task_impacts" in updates
    needs_recompute_resolve = (
        status_blocking_changed or linkage_changed or impacts_changed
    )

    if needs_recompute_resolve:
        # CPM Part J-2 -- auto-recompute + verdict re-resolve fires
        # BEFORE the audit so the audit row captures the stamped
        # per_task_impacts. Fail-safe inside auto_recompute_after_event.
        from services.cpm import auto_recompute_after_event
        await auto_recompute_after_event(
            ctx["tenant_id"], after["project_id"],
            actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
        )
        if after.get("per_task_impacts"):
            impacts_actually_changed, stamped = await _resolve_rfi_task_impacts(
                db, ctx["tenant_id"], after,
                project_id=after["project_id"],
                exclude_rfi_id=rfi_id,
            )
            if impacts_actually_changed:
                after["per_task_impacts"] = stamped
                await db.rfis.update_one(
                    tenant_filter(ctx["tenant_id"], {"id": rfi_id}),
                    {"$set": {"per_task_impacts": stamped}},
                )

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
            # Bug #49: on status-only transitions this carries
            # "status-only transition open -> closed; response_text: ...".
            edit_reason=effective_edit_reason, before=before, after=after,
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
    #
    # CPM Part J-2 (2026-05-15) -- compute_and_persist_status still
    # gated ONLY on status + linkage change (NOT on impacts_changed).
    # Blocker count depends on COUNT of blocking events, not on
    # impact-days values. A pure per_task_impacts edit doesn't shift
    # the blocker badge. The Part E auto_recompute_after_event was
    # folded into the needs_recompute_resolve block above, so its
    # trailing call here is gone.
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
