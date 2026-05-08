"""Addition 4 -- edit_requests approval queue.

A super who submitted a DR > 24h ago and now needs to fix an event
on it files an edit_request via POST /edit-requests. Admin / PM
review the queue at GET /edit-requests?status=pending and either
approve (opens a 1-hour edit window for that user on that DR) or
deny (with optional review_note).

Endpoints:

  POST   /edit-requests              -- super files a request
  GET    /edit-requests              -- ?status= filter
  POST   /edit-requests/{id}/approve -- admin/PM approves (1h window)
  POST   /edit-requests/{id}/deny    -- admin/PM denies

All audited:
  EDIT_REQUEST_FILED
  EDIT_REQUEST_APPROVED
  EDIT_REQUEST_DENIED
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_tenant
from audit import write_audit
from database import get_db, tenant_filter
from models import (
    EditRequest, EditRequestCreate, EditRequestReview,
    EditRequestStatus, gen_id, utc_now,
)
from services.edit_permissions import (
    APPROVED_EDIT_WINDOW_HOURS, PRIVILEGED_ROLES,
)


router = APIRouter(tags=["edit-requests"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Map EditableEventType -> mongo collection. Same vocabulary as
# photo_tray_router._validate_event_ref so the request -> event lookup
# stays consistent.
_EVENT_COLLECTION = {
    "delay": "delays",
    "rfi": "rfis",
    "qc": "qc_issues",
    "safety": "safety_events",
    "photo": "photos",
    "coordination": "coordination_events",
    "delivery": "deliveries",
    "activity": "daily_report_activities",
}


@router.post("/edit-requests", response_model=EditRequest)
async def file_edit_request(
    payload: EditRequestCreate, ctx: dict = Depends(require_tenant),
):
    db = get_db()

    # Validate the DR exists in tenant and is locked (otherwise the
    # super doesn't need a request -- they can just edit directly).
    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.daily_report_id}),
        {"_id": 0, "id": 1, "status": 1, "project_id": 1, "submitted_by": 1},
    )
    if not dr:
        raise HTTPException(404, "Daily report not found")
    if dr.get("status") not in ("submitted", "approved"):
        raise HTTPException(
            422,
            "Edit requests are only meaningful on locked DRs. This DR "
            "is in draft state -- edit the event directly instead.",
        )

    # Validate the event exists on the DR.
    coll = _EVENT_COLLECTION[payload.event_type]
    if payload.event_type == "activity":
        ev = await db[coll].find_one(
            {"id": payload.event_id, "daily_report_id": payload.daily_report_id},
            {"_id": 0, "id": 1},
        )
    elif payload.event_type == "coordination":
        ev = await db[coll].find_one(
            tenant_filter(ctx["tenant_id"], {
                "id": payload.event_id,
                "linked_daily_report_id": payload.daily_report_id,
            }),
            {"_id": 0, "id": 1},
        )
    else:
        ev = await db[coll].find_one(
            tenant_filter(ctx["tenant_id"], {
                "id": payload.event_id,
                "daily_report_id": payload.daily_report_id,
            }),
            {"_id": 0, "id": 1},
        )
    if not ev:
        raise HTTPException(
            404,
            f"{payload.event_type} {payload.event_id} not found on DR "
            f"{payload.daily_report_id}",
        )

    # 4.7 -- one pending request per (user, DR) at a time. Caller needs
    # to wait for review on the existing one before filing another.
    existing = await db.edit_requests.find_one(
        tenant_filter(ctx["tenant_id"], {
            "daily_report_id": payload.daily_report_id,
            "requested_by": ctx["user"]["id"],
            "status": "pending",
        }),
        {"_id": 0},
    )
    if existing:
        raise HTTPException(
            409,
            "A pending edit request already exists for this DR. Wait "
            "for review before filing another.",
        )

    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": dr["project_id"],
        "daily_report_id": payload.daily_report_id,
        "event_type": payload.event_type,
        "event_id": payload.event_id,
        "requested_by": ctx["user"]["id"],
        "requested_at": _now_iso(),
        "reason": payload.reason.strip(),
        "status": "pending",
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": None,
        "edit_window_expires_at": None,
    }
    await db.edit_requests.insert_one(doc.copy())
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="edit_requests",
        op="EDIT_REQUEST_FILED",
        record_id=doc["id"], before=None, after={
            "daily_report_id": doc["daily_report_id"],
            "event_type": doc["event_type"],
            "event_id": doc["event_id"],
            "reason": doc["reason"],
        },
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return EditRequest(**doc)


@router.get("/edit-requests", response_model=list[EditRequest])
async def list_edit_requests(
    status: Optional[EditRequestStatus] = Query(None),
    daily_report_id: Optional[str] = Query(None),
    requested_by: Optional[str] = Query(None),
    ctx: dict = Depends(require_tenant),
):
    """Default list returns pending requests across the tenant. Non-
    admin/PM users only see their own requests; admin/PM see all."""
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {})
    if status:
        f["status"] = status
    if daily_report_id:
        f["daily_report_id"] = daily_report_id
    role = (ctx["user"].get("role") or "").lower()
    if role not in PRIVILEGED_ROLES:
        # Non-privileged users only see their own.
        f["requested_by"] = ctx["user"]["id"]
    elif requested_by:
        f["requested_by"] = requested_by
    docs = await db.edit_requests.find(f, {"_id": 0}).sort("requested_at", -1).to_list(500)
    return [EditRequest(**d) for d in docs]


def _require_reviewer(ctx: dict) -> None:
    role = (ctx["user"].get("role") or "").lower()
    if role not in PRIVILEGED_ROLES:
        raise HTTPException(
            403, "Only Admin or PM can approve or deny edit requests.",
        )


@router.post("/edit-requests/{request_id}/approve", response_model=EditRequest)
async def approve_edit_request(
    request_id: str, payload: EditRequestReview,
    ctx: dict = Depends(require_tenant),
):
    _require_reviewer(ctx)
    db = get_db()
    before = await db.edit_requests.find_one(
        tenant_filter(ctx["tenant_id"], {"id": request_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Edit request not found")
    if before.get("status") != "pending":
        raise HTTPException(
            409,
            f"Edit request is in status={before.get('status')!r}; only "
            f"'pending' requests can be approved.",
        )
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=APPROVED_EDIT_WINDOW_HOURS)
    updates = {
        "status": "approved",
        "reviewed_by": ctx["user"]["id"],
        "reviewed_at": now.isoformat(),
        "review_note": (payload.review_note or "").strip() or None,
        "edit_window_expires_at": expires.isoformat(),
    }
    await db.edit_requests.update_one(
        tenant_filter(ctx["tenant_id"], {"id": request_id}), {"$set": updates},
    )
    after = {**before, **updates}
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="edit_requests",
        op="EDIT_REQUEST_APPROVED",
        record_id=request_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return EditRequest(**after)


@router.post("/edit-requests/{request_id}/deny", response_model=EditRequest)
async def deny_edit_request(
    request_id: str, payload: EditRequestReview,
    ctx: dict = Depends(require_tenant),
):
    _require_reviewer(ctx)
    db = get_db()
    before = await db.edit_requests.find_one(
        tenant_filter(ctx["tenant_id"], {"id": request_id}), {"_id": 0},
    )
    if not before:
        raise HTTPException(404, "Edit request not found")
    if before.get("status") != "pending":
        raise HTTPException(
            409,
            f"Edit request is in status={before.get('status')!r}; only "
            f"'pending' requests can be denied.",
        )
    now = datetime.now(timezone.utc)
    updates = {
        "status": "denied",
        "reviewed_by": ctx["user"]["id"],
        "reviewed_at": now.isoformat(),
        "review_note": (payload.review_note or "").strip() or None,
    }
    await db.edit_requests.update_one(
        tenant_filter(ctx["tenant_id"], {"id": request_id}), {"$set": updates},
    )
    after = {**before, **updates}
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="edit_requests",
        op="EDIT_REQUEST_DENIED",
        record_id=request_id, before=before, after=after,
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return EditRequest(**after)


@router.get("/edit-requests/pending-count")
async def pending_count(ctx: dict = Depends(require_tenant)):
    """Sidebar badge data. Returns total pending across the tenant
    (admin/PM see all; everyone else sees their own pending count)."""
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"status": "pending"})
    role = (ctx["user"].get("role") or "").lower()
    if role not in PRIVILEGED_ROLES:
        f["requested_by"] = ctx["user"]["id"]
    n = await db.edit_requests.count_documents(f)
    return {"pending": int(n)}
