"""Addition 4 -- tiered permission check for editing events on a
LOCKED daily report.

Three outcomes:

  "allowed"        -- the user can patch the event right now. Caller
                       proceeds with the write.
  "needs_request"  -- the user is the original submitter but the
                       24-hour self-edit window has closed and there's
                       no approved + non-expired edit_request. Caller
                       returns 403 with a "request edit access" hint.
  "forbidden"      -- the user is not privileged AND not the original
                       submitter. Caller returns 403.

Spec rules (see CLAUDE.md / Addition 4.1):

- Admin / PM: always allowed (any DR, any event, no approval flow).
- Within 24h of original DR submit, on YOUR OWN DR: allowed.
- After 24h, on YOUR OWN DR: needs an approved + non-expired
  edit_request for that user + DR (1-hour edit window from approve).
- Editing ANYONE ELSE'S DR (not original submitter): forbidden unless
  Admin/PM.

The check is async because it reads daily_reports.submitted_at +
edit_requests for the approval-window check.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from database import get_db, tenant_filter

EditDecision = Literal["allowed", "needs_request", "forbidden"]

# Privileged roles that bypass the request workflow entirely.
PRIVILEGED_ROLES = {"admin", "pm"}

# Self-edit window after the DR's submitted_at timestamp.
SELF_EDIT_WINDOW_HOURS = 24
# Edit-window granted by an approved request.
APPROVED_EDIT_WINDOW_HOURS = 1


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        # Mongo serializes datetimes to ISO strings; tolerate both with
        # and without trailing 'Z'.
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


async def check_edit_permission(
    *, tenant_id: str, user: dict, daily_report: dict,
    event_type: str, event_id: str,
) -> EditDecision:
    """Return one of "allowed" / "needs_request" / "forbidden".

    Caller is expected to short-circuit on 'forbidden' with HTTP 403,
    on 'needs_request' with HTTP 403 + a hint pointing at POST
    /edit-requests, and on 'allowed' to proceed with the patch.
    """
    role = (user.get("role") or "").lower()
    user_id = user.get("id")

    # Admin / PM -- always allowed.
    if role in PRIVILEGED_ROLES:
        return "allowed"

    # If the DR isn't locked yet, the regular PATCH path applies (no
    # edit_reason or audit boilerplate required). Caller is responsible
    # for not invoking this helper on draft DRs; we return "allowed"
    # here defensively but the helper is intended for locked-DR flows.
    if daily_report.get("status") not in ("submitted", "approved"):
        return "allowed"

    submitted_by = daily_report.get("submitted_by")
    submitted_at = _parse_iso(daily_report.get("submitted_at"))
    is_owner = bool(user_id) and submitted_by == user_id

    if not is_owner:
        # Editing someone else's DR is admin/PM-only per spec; no
        # request workflow for non-owners.
        return "forbidden"

    now = datetime.now(timezone.utc)
    if submitted_at is not None:
        if now - submitted_at <= timedelta(hours=SELF_EDIT_WINDOW_HOURS):
            return "allowed"

    # Past 24h: look for an approved + non-expired request for this
    # user + DR. event_type/event_id matching is permissive -- an
    # approved request on a DR opens the window for any event on that
    # DR (so the super doesn't have to file a request per event).
    db = get_db()
    request = await db.edit_requests.find_one(
        tenant_filter(tenant_id, {
            "daily_report_id": daily_report.get("id"),
            "requested_by": user_id,
            "status": "approved",
        }),
        sort=[("reviewed_at", -1)],
    )
    if request:
        expires_at = _parse_iso(request.get("edit_window_expires_at"))
        if expires_at and now <= expires_at:
            return "allowed"

    return "needs_request"


async def stamp_edit_metadata(
    *, db, collection: str, record_id: str, tenant_id: str,
    actor_id: str, edit_reason: str, before: dict, after: dict,
    request_id: Optional[str] = None,
) -> None:
    """Update last_edited_*/edit_count/post_submit_edited on the record
    AND write the EVENT_EDITED_POST_SUBMIT audit op with full before/after
    snapshots so legal can render a per-edit diff in TIA Section 12.

    Called from each event PATCH endpoint AFTER the underlying field
    write so 'after' reflects the post-merge state.
    """
    from audit import write_audit
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    new_count = int((before.get("edit_count") or 0)) + 1
    await db[collection].update_one(
        tenant_filter(tenant_id, {"id": record_id}),
        {"$set": {
            "last_edited_at": now,
            "last_edited_by": actor_id,
            "last_edit_reason": edit_reason,
            "edit_count": new_count,
            "post_submit_edited": True,
        }},
    )
    await write_audit(
        tenant_id=tenant_id, table_name=collection,
        op="EVENT_EDITED_POST_SUBMIT",
        record_id=record_id, before=before, after=after,
        actor_id=actor_id, request_id=request_id,
        # The audit_log row also captures the reason in the after
        # payload's metadata block; the diff is reconstructable from
        # before/after but legal prefers the reason inline.
    )


async def get_locked_dr_for_event(
    *, tenant_id: str, daily_report_id: Optional[str],
) -> Optional[dict]:
    """Resolve the DR the event lives on. Used by event PATCH handlers
    to decide whether the locked-edit code path applies.

    Returns None if no daily_report_id (orphan event -- shouldn't exist
    after migration 005 but defensive)."""
    if not daily_report_id:
        return None
    db = get_db()
    return await db.daily_reports.find_one(
        tenant_filter(tenant_id, {"id": daily_report_id}),
        {"_id": 0, "id": 1, "status": 1, "submitted_by": 1,
         "submitted_at": 1, "report_date": 1, "project_id": 1},
    )


async def enforce_locked_dr_edit_policy(
    *, ctx: dict, daily_report_id: Optional[str], edit_reason: Optional[str],
    event_type: str, event_id: str,
) -> tuple[Optional[dict], bool]:
    """Combined helper used by every event PATCH endpoint.

    Returns (parent_dr_doc, is_locked_edit) where is_locked_edit=True
    means the caller should call stamp_edit_metadata after writing.

    Raises HTTPException(422) if the parent DR is locked AND the
    payload didn't include an edit_reason (or it's too short).
    Raises HTTPException(403) if the user lacks edit permission
    (forbidden) or needs to file an edit_request first.
    """
    from fastapi import HTTPException
    from models import EDIT_REASON_MIN_LEN

    dr = await get_locked_dr_for_event(
        tenant_id=ctx["tenant_id"], daily_report_id=daily_report_id,
    )
    if not dr or dr.get("status") not in ("submitted", "approved"):
        # Draft DR (or orphan event) -- regular patch path, no
        # locked-edit ceremony.
        return dr, False

    # Locked path: require reason + permission.
    cleaned_reason = (edit_reason or "").strip()
    if len(cleaned_reason) < EDIT_REASON_MIN_LEN:
        raise HTTPException(
            422,
            f"edit_reason required (min {EDIT_REASON_MIN_LEN} chars) "
            f"when editing events on a locked daily report.",
        )

    decision = await check_edit_permission(
        tenant_id=ctx["tenant_id"], user=ctx["user"],
        daily_report=dr, event_type=event_type, event_id=event_id,
    )
    if decision == "forbidden":
        raise HTTPException(
            403,
            "Editing events on another user's locked daily report "
            "requires Admin or PM role.",
        )
    if decision == "needs_request":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "edit_request_required",
                "message": (
                    "Self-edit window (24h after submit) has closed. "
                    "File an edit request via POST /edit-requests so an "
                    "Admin or PM can approve a new 1-hour edit window."
                ),
                "daily_report_id": daily_report_id,
                "event_type": event_type,
                "event_id": event_id,
            },
        )
    return dr, True
