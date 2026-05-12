"""Addition 4 (edit) + Addition 5 (add) -- tiered permission check for
EDITING existing events on a LOCKED daily report AND for ADDING new
events to a LOCKED daily report.

Test 2D follow-up (2026-05-12): added observability log lines so the
operator can trace what role / DR-status / decision the policy made
on every locked-DR add. Grep server logs for "locked_dr_add_policy"
to see every decision in real time.

Three outcomes for both flows:

  "allowed"        -- the user can patch/insert right now. Caller
                       proceeds with the write.
  "needs_request"  -- the user is the original submitter but the
                       24-hour self-edit window has closed and there's
                       no approved + non-expired edit_request. Caller
                       returns 403 with a "request edit access" hint.
  "forbidden"      -- the user is not privileged AND not the original
                       submitter. Caller returns 403.

Spec rules (see CLAUDE.md / Addition 4.1 + Addition 5):

- Admin / PM: always allowed (any DR, any event, no approval flow,
  no reason required).
- Within 24h of original DR submit, on YOUR OWN DR: allowed (reason
  required, min EDIT_REASON_MIN_LEN chars).
- After 24h, on YOUR OWN DR: needs an approved + non-expired
  edit_request for that user + DR (1-hour edit window from approve).
- Editing/adding on ANYONE ELSE'S DR (not original submitter):
  forbidden unless Admin/PM.

The edit_request workflow approves BOTH editing existing events AND
adding new events on the same DR -- the 1-hour window opens both
paths uniformly so the super doesn't have to file two requests when
they realize they need to both fix a row and add a missed delay.

The check is async because it reads daily_reports.submitted_at +
edit_requests for the approval-window check.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from database import get_db, tenant_filter

log = logging.getLogger("edit_permissions")

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
    audit_op: str = "EVENT_EDITED_POST_SUBMIT",
) -> None:
    """Update last_edited_*/edit_count/post_submit_edited on the record
    AND write the {audit_op} audit op with full before/after snapshots
    so legal can render a per-edit diff in TIA Section 12.

    Default op is EVENT_EDITED_POST_SUBMIT. The activity-grid PATCH
    endpoint passes audit_op="ACTIVITY_GRID_EDITED_POST_SUBMIT" so
    counsel can filter grid edits separately from event edits when
    reading the chain.

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
        op=audit_op,
        record_id=record_id, before=before, after=after,
        actor_id=actor_id, request_id=request_id,
        # The audit_log row also captures the reason in the after
        # payload's metadata block; the diff is reconstructable from
        # before/after but legal prefers the reason inline.
    )


async def stamp_add_metadata(
    *, db, collection: str, record_id: str, tenant_id: str,
    actor_id: str, add_reason: str, after: dict,
    request_id: Optional[str] = None,
) -> None:
    """Addition 5 (2026-05-12) -- counterpart to stamp_edit_metadata
    for the "add new event to a locked DR" flow. Sets post_submit_added=
    true + add_reason + added_at_post_submit + added_by_post_submit on
    the inserted record, then writes EVENT_ADDED_POST_SUBMIT audit op
    with the full after snapshot (no before -- this is a new row).

    Called from each event POST endpoint AFTER insert_one when the
    permission check (enforce_locked_dr_add_policy) returned True.
    """
    from audit import write_audit
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    await db[collection].update_one(
        tenant_filter(tenant_id, {"id": record_id}),
        {"$set": {
            "post_submit_added": True,
            "add_reason": add_reason,
            "added_at_post_submit": now,
            "added_by_post_submit": actor_id,
        }},
    )
    # Refresh after-snapshot so the audit captures the stamped flags.
    enriched = {**after,
                "post_submit_added": True,
                "add_reason": add_reason,
                "added_at_post_submit": now,
                "added_by_post_submit": actor_id}
    await write_audit(
        tenant_id=tenant_id, table_name=collection,
        op="EVENT_ADDED_POST_SUBMIT",
        record_id=record_id, before=None, after=enriched,
        actor_id=actor_id, request_id=request_id,
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


async def enforce_locked_dr_add_policy(
    *, ctx: dict, daily_report_id: Optional[str], add_reason: Optional[str],
    event_type: str,
) -> tuple[Optional[dict], bool]:
    """Addition 5 (2026-05-12) -- counterpart to enforce_locked_dr_edit_policy
    for the "add new event to a locked DR" flow.

    Returns (parent_dr_doc, is_locked_add) where is_locked_add=True
    means the caller should call stamp_add_metadata after insert.

    Raises HTTPException(422) if the parent DR is locked AND the
    payload didn't include an add_reason (or it's too short) AND the
    caller isn't admin/PM.
    Raises HTTPException(403) if the user lacks add permission
    (forbidden) or needs to file an edit_request first.

    The role + 24h + edit_request rules are identical to the edit
    flow -- an approved edit_request for the DR opens BOTH paths
    (edits AND adds) for the same 1-hour window.

    Test 2D follow-up (2026-05-12): emits structured log lines for
    every decision branch so the operator can trace bypass / 422 /
    403 outcomes without instrumenting the call sites.
    """
    from fastapi import HTTPException
    from models import EDIT_REASON_MIN_LEN

    role = (ctx["user"].get("role") or "").lower()
    actor_id = (ctx.get("user") or {}).get("id")
    reason_len = len((add_reason or "").strip())

    dr = await get_locked_dr_for_event(
        tenant_id=ctx["tenant_id"], daily_report_id=daily_report_id,
    )
    if not dr or dr.get("status") not in ("submitted", "approved"):
        # Draft DR (or orphan event) -- regular create path, no
        # locked-add ceremony.
        log.debug(
            "locked_dr_add_policy: draft_or_missing dr_id=%s event_type=%s "
            "actor=%s role=%s -> NOT_LOCKED (regular create)",
            daily_report_id, event_type, actor_id, role,
        )
        return dr, False

    # Admin / PM bypass the reason requirement (already privileged at
    # tenant level + the role is captured in the audit actor_id, so
    # legal can still trace).
    if role in PRIVILEGED_ROLES:
        log.info(
            "locked_dr_add_policy: privileged_bypass dr_id=%s event_type=%s "
            "actor=%s role=%s reason_len=%d -> ALLOWED (admin/PM bypass; "
            "reason captured if frontend supplied it)",
            daily_report_id, event_type, actor_id, role, reason_len,
        )
        return dr, True

    # Non-privileged: require reason + permission tier.
    cleaned_reason = (add_reason or "").strip()
    if len(cleaned_reason) < EDIT_REASON_MIN_LEN:
        log.warning(
            "locked_dr_add_policy: reason_too_short dr_id=%s event_type=%s "
            "actor=%s role=%s reason_len=%d min=%d -> 422",
            daily_report_id, event_type, actor_id, role,
            reason_len, EDIT_REASON_MIN_LEN,
        )
        raise HTTPException(
            422,
            f"add_reason required (min {EDIT_REASON_MIN_LEN} chars) "
            f"when adding events to a locked daily report. "
            f"Got {reason_len} chars.",
        )

    # event_id="<new>" -- the row doesn't exist yet; check_edit_permission
    # only looks at it for the audit metadata, not the lookup.
    decision = await check_edit_permission(
        tenant_id=ctx["tenant_id"], user=ctx["user"],
        daily_report=dr, event_type=event_type, event_id="<new>",
    )
    if decision == "forbidden":
        log.warning(
            "locked_dr_add_policy: forbidden dr_id=%s event_type=%s "
            "actor=%s role=%s -> 403 (not submitter, not admin/PM)",
            daily_report_id, event_type, actor_id, role,
        )
        raise HTTPException(
            403,
            "Adding events to another user's locked daily report "
            "requires Admin or PM role.",
        )
    if decision == "needs_request":
        log.warning(
            "locked_dr_add_policy: needs_request dr_id=%s event_type=%s "
            "actor=%s role=%s -> 403 (24h self-edit window closed)",
            daily_report_id, event_type, actor_id, role,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "edit_request_required",
                "message": (
                    "Self-edit window (24h after submit) has closed. "
                    "File an edit request via POST /edit-requests so an "
                    "Admin or PM can approve a new 1-hour edit/add window."
                ),
                "daily_report_id": daily_report_id,
                "event_type": event_type,
                "operation": "add",
            },
        )
    log.info(
        "locked_dr_add_policy: allowed dr_id=%s event_type=%s actor=%s "
        "role=%s reason_len=%d -> POST-SUBMIT-ADD",
        daily_report_id, event_type, actor_id, role, reason_len,
    )
    return dr, True
