"""Addition 4 (edit) + Addition 5 (add) -- tiered permission check for
EDITING existing events on a LOCKED daily report AND for ADDING new
events to a LOCKED daily report.

Bug #49 (2026-05-13): status-only transitions (open -> resolved,
draft -> answered -> closed, etc.) are exempted from the
edit_reason requirement when invoked from a standalone event page.
The status-specific note field (resolved_note / resolution_notes /
response_text / outcome_notes / received_by) replaces the generic
edit_reason for audit purposes. See STATUS_TRANSITION_CONFIGS +
is_status_only_transition() below; the exemption fires only when
the fields that actually CHANGED (vs the pre-image) are a subset of
the type's allowed status-only set AND include the trigger field.
Field edits to other attributes still gate on edit_reason.

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


# Bug #49 (2026-05-13) -- status-only transition configuration. Per
# event_type:
#   trigger:    the status-like field whose change defines a "transition"
#   allowed:    superset of fields a status-only PATCH may touch. Patches
#               whose set of CHANGED fields (post-diff against the
#               pre-image) is a subset of `allowed` AND includes
#               `trigger` are exempted from the edit_reason gate.
#   note_field: the field whose value supplies the audit-trail reason
#               when the exemption fires. May be None for delivery
#               (no narrative note; received_by carries the actor).
#
# Intentionally NOT included:
#   - "safety": SafetyEventUpdate has no status field, so the PATCH
#     path can't transition status; the locked-DR resolve flow for
#     safety doesn't exist today (would need a separate close endpoint).
#   - "photo" / "activity": photos and DR activity-grid rows have no
#     resolve/close semantics; their PATCH endpoints also call this
#     policy, but the exemption never fires for them.
#   - "submittal": submittals follow a separate SLA + status-machine
#     pipeline (services/submittal_sla.py) and don't enforce the
#     locked-DR edit_reason gate at all.
STATUS_TRANSITION_CONFIGS: dict[str, dict] = {
    "delay": {
        "trigger": "status",
        "allowed": {"status", "resolved_note", "resolved_date",
                    "resolved_by_user_id"},
        "note_field": "resolved_note",
    },
    "qc": {
        "trigger": "status",
        "allowed": {"status", "resolution_notes", "resolved_at"},
        "note_field": "resolution_notes",
    },
    "rfi": {
        "trigger": "status",
        "allowed": {"status", "response_text", "date_responded"},
        "note_field": "response_text",
    },
    "coordination": {
        "trigger": "outcome",
        "allowed": {"outcome", "outcome_notes"},
        "note_field": "outcome_notes",
    },
    "delivery": {
        "trigger": "status",
        "allowed": {"status", "received_by"},
        "note_field": "received_by",
    },
    # Bug #55 (2026-05-14) -- safety added after SafetyEventUpdate
    # gained status / closed_note / closed_date fields. Close-only
    # patches (status -> 'closed' with a closure narrative) skip the
    # edit_reason 422 via the same exemption as the other event types.
    # Field edits to severity / event_type / description still gate
    # on edit_reason. Note field is closed_note for the synthesized
    # audit-trail reason.
    "safety": {
        "trigger": "status",
        "allowed": {"status", "closed_note", "closed_date"},
        "note_field": "closed_note",
    },
}


def _projection_equal(before_v, updates_v) -> bool:
    """Bug #49 hotfix 2 (2026-05-13). True iff updates_v is consistent
    with before_v under "frontend echoes a thin projection of stored
    data" semantics.

    Root cause this addresses:
    backend/routers/field_events_router.py:_resolve_task_impacts stamps
    each per_task_impacts item with 5 keys on storage:
        task_id, projected_impact_days,
        float_at_logging_days, consumes_float_only, pushes_critical_path

    DelayForm.jsx echoes back only 2 keys:
        task_id, projected_impact_days

    Python dict equality requires identical key sets:
        {a:1, b:2, c:3, d:4, e:5}  !=  {a:1, b:2}
    so list element-wise == falsely flags per_task_impacts as changed on
    every patch, even when the user touched nothing on the impacts grid.
    That pulled per_task_impacts into changed_fields, which is NOT in
    delay's status-only allowed set, disqualifying the exemption and
    causing the 422 reported in preview.

    Rule:
      - Scalar / dict identity:  use Python ==.
      - List-of-dict, same length: each updates item must be a
        key-subset of the corresponding before item with matching
        values for the shared keys. Backend-only keys not echoed by
        the frontend are tolerated.
      - Anything else: fall back to ==.

    Asymmetry note: we project BEFORE -> UPDATES keys, never the other
    way. If the frontend sent extra keys not in before, Pydantic
    StrictModel (extra='forbid') would 422 before reaching the policy
    -- so the reverse case is unreachable.

    Detected as a real change (returns False), preserving the gate:
      - User changes projected_impact_days from 5 to 10:
          b_item.get('projected_impact_days') == 5.0 != u_item[..] == 10
      - User adds or removes a task: len(before) != len(updates).
      - User mutates a non-list-of-dict field (severity, etc.):
        falls through to ==, which catches real value changes.
    """
    if before_v == updates_v:
        return True
    if (isinstance(before_v, list) and isinstance(updates_v, list)
            and len(before_v) == len(updates_v)):
        for b_item, u_item in zip(before_v, updates_v):
            if isinstance(b_item, dict) and isinstance(u_item, dict):
                # u keys must be a subset of b keys-and-values.
                for k, v in u_item.items():
                    if b_item.get(k) != v:
                        return False
            elif b_item != u_item:
                return False
        return True
    return False


def _diff_fields_meaningful(
    event_type: str, updates: dict, before: dict,
) -> set:
    """Bug #49 hotfix (2026-05-13). Compute the set of fields a PATCH
    meaningfully changes from the pre-image, tolerant of the
    "frontend-default-vs-DB-None" phantom-change pattern.

    The original Bug #49 diff was a straight value-equality check
    (`before.get(k) != v`). That works for clean rows but breaks on
    legacy data:

        DelayForm.jsx:88
          owner_attribution: initial?.owner_attribution || "none",
          cause_class:       initial?.cause_class       || "other",

    The form applies a JS-side default ("none" / "other") when the
    loaded record's field is null. Legacy delays created before those
    fields existed (or never set) come back from Mongo as null. The
    user opens the form, sees the JS default, doesn't touch the field,
    and saves -- payload echoes back "none"/"other". Backend `before`
    has null, `updates` has "none"; equality fails, the field lands in
    changed_fields, and because owner_attribution / cause_class aren't
    in the delay's allowed status-only set, the exemption never fires.
    Result: every legacy-delay resolve from /delays returns 422.

    Rule applied here:
      - before.get(k) == v  -> not changed (regardless of value).
      - before.get(k) is None AND k NOT in the event type's allowed
        status-only set AND updates[k] is a non-None value
        -> TREAT AS NOT CHANGED. This tolerates the JS-default echo.
      - otherwise -> counted as changed.

    Why this is safe:
      - Trigger + note fields (status, resolved_note, etc.) are IN the
        allowed set, so the None -> meaningful-value transition is
        still detected (status changing from None to "resolved",
        resolved_note from None to "fixed at 0700").
      - Real mixed edits where a non-allowed field changes from a real
        value (not None) to another real value -- e.g. severity major
        -> critical -- still count (before is not None).
      - The only behavior lost is: "user genuinely sets a non-allowed
        field for the first time (from None to a real value) as part
        of a patch that is otherwise status-only." In that case we now
        treat the patch as status-only and let it through without
        edit_reason. Trade-off accepted: in practice the user supplies
        edit_reason for such intentional patches via the standalone
        form's edit-reason input (when present), and the cost of the
        false-422 was blocking every legacy-delay close.
    """
    cfg = STATUS_TRANSITION_CONFIGS.get(event_type) or {}
    allowed = cfg.get("allowed") or set()
    changed: set = set()
    for k, v in updates.items():
        before_v = before.get(k)
        # Bug #49 hotfix 2 (2026-05-13): swapped raw `==` for
        # _projection_equal so list-of-dict fields where the frontend
        # echoes a thin projection of DB-enriched data (per_task_impacts
        # is the canonical case) compare correctly.
        if _projection_equal(before_v, v):
            continue
        if before_v is None and k not in allowed and v is not None:
            # JS-default-vs-DB-None phantom change. Skip.
            continue
        changed.add(k)
    return changed


def is_status_only_transition(
    event_type: str, changed_fields: set,
) -> bool:
    """Bug #49 (2026-05-13). True iff every field that actually CHANGED
    in this PATCH is in the event-type's allowed status-transition set
    AND the trigger field (status/outcome) is among those changed.

    Used by enforce_locked_dr_edit_policy to skip the edit_reason gate
    on resolve/close flows initiated from standalone event pages, which
    today have no edit_reason capture (Bug #49 repro). Adding/changing
    a non-status field alongside the transition (e.g. severity, vendor)
    falls through to the existing gate — those still require edit_reason.

    `changed_fields` must be precomputed by the caller as
    `{k for k, v in updates.items() if before.get(k) != v}` so that
    forms which echo back unchanged fields (DelayForm sends the entire
    record on each save) don't accidentally fail the subset check.
    """
    cfg = STATUS_TRANSITION_CONFIGS.get(event_type)
    if not cfg or not changed_fields:
        return False
    if cfg["trigger"] not in changed_fields:
        return False
    return changed_fields.issubset(cfg["allowed"])


def derive_status_transition_reason(
    event_type: str, updates: dict, before: dict,
) -> str:
    """Bug #49 (2026-05-13). Synthesize an effective_edit_reason for
    stamp_edit_metadata when the status-only-transition exemption fires.

    Format: "status-only transition <before>-><after>; <note_field>: <note>".
    Falls back to "<no note supplied>" when the note field is empty;
    legal can still reconstruct intent from the before/after snapshot
    on the UPDATE audit row.
    """
    cfg = STATUS_TRANSITION_CONFIGS.get(event_type) or {}
    trigger = cfg.get("trigger", "status")
    note_field = cfg.get("note_field")
    before_val = before.get(trigger)
    after_val = updates.get(trigger, before_val)
    if note_field:
        note_val = (updates.get(note_field) or before.get(note_field) or "")
        note_text = note_val.strip() if isinstance(note_val, str) else str(note_val)
        if not note_text:
            note_text = "<no note supplied>"
        return (
            f"status-only transition {before_val} -> {after_val}; "
            f"{note_field}: {note_text}"
        )
    return f"status-only transition {before_val} -> {after_val}"


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
    updates: Optional[dict] = None,
    before: Optional[dict] = None,
) -> tuple[Optional[dict], bool, Optional[str]]:
    """Combined helper used by every event PATCH endpoint.

    Returns (parent_dr_doc, is_locked_edit, effective_edit_reason).
    is_locked_edit=True means the caller should call stamp_edit_metadata
    after writing; effective_edit_reason is the value the caller should
    pass for `edit_reason=` on that call (equals the input edit_reason
    in the normal case, or a synthesized note-derived reason on the
    Bug #49 status-only-transition exemption path).

    Bug #49 (2026-05-13): when `updates` and `before` are supplied AND
    the PATCH matches the event_type's status-only-transition pattern
    AND edit_reason is missing/too-short, skip the 422 and synthesize
    a reason from the note field. The UPDATE audit op caller already
    writes a full before/after snapshot so the chain captures the
    status change either way; on locked DRs the
    EVENT_EDITED_POST_SUBMIT op also fires, now carrying the synthetic
    note-derived reason so legal can see the intent inline. Hash chain
    unbroken — we add no audit writes and skip none.

    Legacy callers that don't pass `updates`/`before` fall through to
    the original behavior; the third tuple element equals the input
    edit_reason (which may be None on draft DRs, where it's unused).

    Raises HTTPException(422) if the parent DR is locked AND the
    payload didn't include an edit_reason (or it's too short) AND the
    PATCH is not exempt as a status-only transition.
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
        return dr, False, edit_reason

    # Bug #49 -- status-only transition exemption. Detect first; then
    # decide whether to require / accept / synthesize the reason.
    #
    # Bug #49 hotfix (2026-05-13): use _diff_fields_meaningful instead
    # of inline value-equality so JS-default-vs-DB-None phantom changes
    # on non-allowed fields don't falsely disqualify the patch.
    # Log at INFO so operators can grep "bug49_diff" to debug future
    # regressions (per Sid's diagnostic request).
    status_only = False
    if updates is not None and before is not None:
        changed_fields = _diff_fields_meaningful(event_type, updates, before)
        status_only = is_status_only_transition(event_type, changed_fields)
        log.info(
            "bug49_diff: dr_id=%s event_type=%s event_id=%s "
            "changed=%s allowed=%s status_only=%s",
            daily_report_id, event_type, event_id,
            sorted(changed_fields),
            sorted((STATUS_TRANSITION_CONFIGS.get(event_type) or {}).get("allowed") or []),
            status_only,
        )
        # Bug #49 hotfix 2 (2026-05-13): when status_only is False AND
        # there are changed_fields, emit a structural diagnostic for
        # each "offending" field so the next class-of regression is
        # one-glance debuggable. Captures types + (for list-of-dict)
        # the key sets on each side -- which is exactly the signal
        # that revealed the per_task_impacts mismatch.
        if not status_only and changed_fields:
            diag = {}
            for k in changed_fields:
                bv = before.get(k)
                uv = updates.get(k)
                entry: dict = {
                    "before_type": type(bv).__name__,
                    "updates_type": type(uv).__name__,
                }
                if isinstance(bv, list) and bv and isinstance(bv[0], dict):
                    entry["before_item_keys"] = sorted(bv[0].keys())
                if isinstance(uv, list) and uv and isinstance(uv[0], dict):
                    entry["updates_item_keys"] = sorted(uv[0].keys())
                if isinstance(bv, list) and isinstance(uv, list):
                    entry["before_len"] = len(bv)
                    entry["updates_len"] = len(uv)
                diag[k] = entry
            log.info(
                "bug49_diff_detail: dr_id=%s event_type=%s event_id=%s offenders=%s",
                daily_report_id, event_type, event_id, diag,
            )

    # Reason resolution:
    #   - If the user supplied a sufficient edit_reason, always use it
    #     (more specific than any synthesis).
    #   - Else if the patch is status-only (Bug #49), skip the 422 and
    #     synthesize the reason from the note field.
    #   - Else 422.
    cleaned_reason = (edit_reason or "").strip()
    if len(cleaned_reason) >= EDIT_REASON_MIN_LEN:
        effective_reason: Optional[str] = edit_reason
    elif status_only:
        effective_reason = derive_status_transition_reason(
            event_type, updates or {}, before or {},
        )
        log.info(
            "bug49_status_only_exemption: dr_id=%s event_type=%s "
            "event_id=%s -> reason synthesized from note",
            daily_report_id, event_type, event_id,
        )
    else:
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
    return dr, True, effective_reason


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
