"""Bucket G Phase 1 (2026-05-11) -- Jigsaw Pre-fill UX service.

Owns the lifecycle of cross-entity field suggestions: the "light green"
state in Sid's V1 Product Flow. Producers (Photo AI, RFI drafter, QC
analyzer, schedule importer, calibration RAG) call create_suggestion;
authorized confirmers click Accept/Reject in the Jigsaw widget which
hits confirm_suggestion / reject_suggestion. AI re-runs supersede the
prior pending row instead of dropping a duplicate.

Design notes
------------
- Status is one-way: pending -> {confirmed, rejected, superseded,
  expired}. Once terminal, the row never moves back. Re-suggestions
  create a fresh row and supersede the old one (preserves the
  calibration trail).
- Confirmation is RBAC-gated. Each row carries authorized_confirmer_
  roles; the service rejects confirm calls from users outside that
  list. Admins are NOT auto-allowed via the service layer -- they're
  explicit members of the per-entity default list in models.py. This
  keeps the audit trail honest about who clicked Accept.
- Dedup: if a producer re-suggests the SAME field on the SAME entity
  and the prior suggestion is still pending OR confirmed with the
  same value, no new row is created. Otherwise the prior is
  superseded.
- Authorization-role fallback: if a producer passes an empty list,
  the service falls back to the per-entity defaults in
  FIELD_SUGGESTION_DEFAULT_ROLES; if still empty (entity_type not
  registered), falls back to ["admin"] so the row isn't unreachable.
- Audit: every create / confirm / reject / supersede / expire fires
  a FIELD_SUGGESTION_* op so compliance can replay the lifecycle.
- All read functions are tenant-scoped via tenant_filter(). Cross-
  tenant URL manipulation returns None / empty.

Phase 1 ships:
  - create_suggestion
  - confirm_suggestion
  - reject_suggestion
  - get_suggestions_for_entity
  - get_pending_suggestions_for_user
  - supersede_suggestion
  - expire_stale_pending (Phase 5 background job hook, defined now
    so the indexes + audit op are ready on first deploy)

Phase 2 layers the HTTP endpoints + the AI-producer wire-ups on top.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from database import get_db, tenant_filter
from models import (
    gen_id, utc_now, FIELD_SUGGESTION_DEFAULT_ROLES,
)
from audit import write_audit

log = logging.getLogger("field_suggestions")


# Bounded list of registered entity types. Keeps producers from
# stamping junk entity_type values that the JigsawField widget can't
# render. Mirrors the Literal in models.FieldSuggestionEntityType --
# duplicated as a set here so the runtime check is O(1).
REGISTERED_ENTITY_TYPES: set[str] = set(FIELD_SUGGESTION_DEFAULT_ROLES.keys())

# Registered producer names. Same rationale: prevents typos like
# "ai_copliot" silently landing rows that never get audited or rolled
# up in the Phase 5 acceptance-rate dashboard.
REGISTERED_SOURCES: set[str] = {
    "ai_copilot", "ai_photo_vision", "ai_rfi_drafter",
    "ai_qc_analyzer", "ai_safety_analyzer", "ai_schedule_analyzer",
    "system_inference", "calibration_rag",
}

# Expiration window: pending suggestions auto-rejected after this many
# days. Stops accumulating zombie rows in the user's review queue.
# Phase 5 wires this to a daily background job; Phase 1 ships the
# helper so the index + audit shape are stable from day 1.
EXPIRATION_DAYS = 30

# Bulk operation cap. Mirrors models.FieldSuggestionBulkConfirmPayload
# max_length so a request that gets past Pydantic can't sneak past
# the service either.
BULK_CAP = 50


def _normalize_authorized_roles(
    *,
    entity_type: str, supplied: Optional[list[str]],
) -> list[str]:
    """Resolve the final authorized_confirmer_roles list. Order:
       1. Caller-supplied non-empty list (verbatim).
       2. Per-entity default from FIELD_SUGGESTION_DEFAULT_ROLES.
       3. ["admin"] as last-resort fallback so the row isn't orphaned.
    """
    if supplied:
        # Dedupe + strip while preserving order.
        seen: dict[str, None] = {}
        for r in supplied:
            if r and r not in seen:
                seen[r] = None
        if seen:
            return list(seen.keys())
    default = FIELD_SUGGESTION_DEFAULT_ROLES.get(entity_type)
    if default:
        return list(default)
    log.warning(
        "field_suggestions: entity_type %r has no default role list; "
        "falling back to admin-only", entity_type,
    )
    return ["admin"]


def _serialize_value(value: Any) -> Any:
    """Suggestion values are stored as native JSON, but we coerce
    non-trivial objects (datetimes, decimals, sets) so Mongo's BSON
    encoder doesn't choke. dict / list / primitive pass through; the
    JSON roundtrip is the safety net."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, dict)):
        # Re-dump to coerce any nested non-trivial members.
        return json.loads(json.dumps(value, default=str))
    return json.loads(json.dumps(value, default=str))


# =====================================================================
# Create
# =====================================================================

async def create_suggestion(
    *, tenant_id: str, project_id: str,
    entity_type: str, entity_id: str, field_path: str,
    suggested_value: Any, suggested_by: str,
    confidence_score: Optional[float] = None,
    authorized_confirmer_roles: Optional[list[str]] = None,
    source_context: Optional[str] = None,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Producer entrypoint. Returns the created row, OR -- if the
    suggestion is a duplicate of the existing pending / confirmed
    value -- the existing row with `_dedup=True`. Idempotent on
    (tenant, entity_type, entity_id, field_path) when the value
    matches.

    Side effects:
      - If a pending row exists for the same field with a DIFFERENT
        value, it's superseded (status -> superseded, superseded_by_
        suggestion_id pointed at the new row).
      - If a confirmed row exists with the same value, no new row
        is created (returns the confirmed row with _dedup=True so
        callers can short-circuit).
    """
    if entity_type not in REGISTERED_ENTITY_TYPES:
        return {"ok": False, "reason": f"unknown entity_type: {entity_type!r}"}
    if suggested_by not in REGISTERED_SOURCES:
        return {"ok": False, "reason": f"unknown suggested_by: {suggested_by!r}"}
    if not field_path or not field_path.strip():
        return {"ok": False, "reason": "field_path required"}
    if confidence_score is not None and not (0.0 <= confidence_score <= 1.0):
        return {"ok": False, "reason": "confidence_score must be in [0, 1]"}
    db = get_db()
    norm_value = _serialize_value(suggested_value)
    now = utc_now()

    # Dedup: existing pending / confirmed with the same value.
    existing = await db.field_suggestions.find_one(
        tenant_filter(tenant_id, {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "field_path": field_path,
            "status": {"$in": ["pending", "confirmed"]},
        }),
        {"_id": 0},
    )
    if existing and _values_equal(existing.get("suggested_value"), norm_value):
        return {"ok": True, "row": existing, "_dedup": True}

    # If there's a pending row with a DIFFERENT value, supersede it
    # before inserting the new one. Confirmed rows are left alone --
    # they represent a human decision and shouldn't be overwritten by
    # a fresh AI guess; a future re-suggest just sits as a pending
    # competitor (UI shows both, user can re-confirm or reject).
    if (existing
            and existing.get("status") == "pending"
            and not _values_equal(existing.get("suggested_value"), norm_value)):
        await _mark_superseded(
            tenant_id=tenant_id, old_id=existing["id"],
            actor_id=actor_id, request_id=request_id,
        )

    roles = _normalize_authorized_roles(
        entity_type=entity_type, supplied=authorized_confirmer_roles,
    )
    doc = {
        "id": gen_id(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "field_path": field_path.strip(),
        "suggested_value": norm_value,
        "suggested_by": suggested_by,
        "confidence_score": confidence_score,
        "status": "pending",
        "confirmed_by": None,
        "confirmed_at": None,
        "rejected_by": None,
        "rejected_at": None,
        "rejection_reason": None,
        "authorized_confirmer_roles": roles,
        "source_context": (source_context or None),
        "superseded_by_suggestion_id": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    await db.field_suggestions.insert_one(doc.copy())

    # Point the just-superseded row at the new id, if applicable.
    if (existing
            and existing.get("status") == "pending"
            and not _values_equal(existing.get("suggested_value"), norm_value)):
        await db.field_suggestions.update_one(
            {"id": existing["id"]},
            {"$set": {"superseded_by_suggestion_id": doc["id"]}},
        )

    try:
        await write_audit(
            tenant_id=tenant_id, table_name="field_suggestions",
            op="FIELD_SUGGESTION_CREATED",
            record_id=doc["id"], before=None,
            after={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "field_path": doc["field_path"],
                "suggested_by": suggested_by,
                "confidence_score": confidence_score,
                "authorized_confirmer_roles": roles,
                "superseded_prior": (existing or {}).get("id")
                    if existing and existing.get("status") == "pending" else None,
            },
            actor_id=actor_id,
            request_id=request_id or f"field_sugg_{doc['id']}",
        )
    except Exception as exc:
        log.warning("audit FIELD_SUGGESTION_CREATED failed (%s): %s",
                    doc["id"], exc)
    return {"ok": True, "row": doc}


def _values_equal(a: Any, b: Any) -> bool:
    """JSON-shape equality. We round-trip through json so dicts with
    the same keys in different orders compare equal."""
    try:
        return json.dumps(a, sort_keys=True, default=str) == \
               json.dumps(b, sort_keys=True, default=str)
    except Exception:
        return a == b


# =====================================================================
# Confirm / Reject / Supersede
# =====================================================================

async def confirm_suggestion(
    *, tenant_id: str, suggestion_id: str, user_id: str, user_role: str,
    request_id: Optional[str] = None,
) -> dict:
    """User clicks Accept. Validates role + status, flips to
    confirmed, audits. Returns {ok, row} or {ok: False, reason}.

    Race: if two users hit confirm concurrently, the first updates
    the row from pending -> confirmed; the second's update matches
    zero rows and returns {ok: False, reason: 'already_terminal'}."""
    db = get_db()
    row = await db.field_suggestions.find_one(
        tenant_filter(tenant_id, {"id": suggestion_id}),
        {"_id": 0},
    )
    if not row:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    if row.get("status") != "pending":
        return {"ok": False, "reason": "already_terminal",
                "current_status": row.get("status"),
                "http_status": 409}
    roles = row.get("authorized_confirmer_roles") or []
    if user_role not in roles:
        return {"ok": False, "reason": "role_not_authorized",
                "allowed_roles": roles, "http_status": 403}

    now = utc_now()
    res = await db.field_suggestions.update_one(
        {"id": suggestion_id, "status": "pending"},
        {"$set": {"status": "confirmed",
                   "confirmed_by": user_id,
                   "confirmed_at": now.isoformat(),
                   "updated_at": now.isoformat()}},
    )
    if not res.matched_count:
        # Race: another caller flipped it. Re-read so we return the
        # current state for the loser path.
        latest = await db.field_suggestions.find_one(
            {"id": suggestion_id}, {"_id": 0},
        )
        return {"ok": False, "reason": "already_terminal",
                "current_status": (latest or {}).get("status"),
                "http_status": 409}

    try:
        await write_audit(
            tenant_id=tenant_id, table_name="field_suggestions",
            op="FIELD_SUGGESTION_CONFIRMED",
            record_id=suggestion_id, before=None,
            after={"entity_type": row.get("entity_type"),
                    "entity_id": row.get("entity_id"),
                    "field_path": row.get("field_path"),
                    "suggested_value": row.get("suggested_value"),
                    "confirmed_by": user_id,
                    "user_role": user_role},
            actor_id=user_id,
            request_id=request_id or f"fs_confirm_{suggestion_id}",
        )
    except Exception as exc:
        log.warning("audit FIELD_SUGGESTION_CONFIRMED failed (%s): %s",
                    suggestion_id, exc)

    refreshed = await db.field_suggestions.find_one(
        {"id": suggestion_id}, {"_id": 0},
    )
    return {"ok": True, "row": refreshed}


async def reject_suggestion(
    *, tenant_id: str, suggestion_id: str, user_id: str, user_role: str,
    reason: str, request_id: Optional[str] = None,
) -> dict:
    """User clicks Reject. Reason min 10 chars enforced here as the
    last-line check (Pydantic also enforces at the router boundary)."""
    if not reason or len(reason.strip()) < 10:
        return {"ok": False, "reason": "reason_min_10_chars",
                "http_status": 422}
    db = get_db()
    row = await db.field_suggestions.find_one(
        tenant_filter(tenant_id, {"id": suggestion_id}),
        {"_id": 0},
    )
    if not row:
        return {"ok": False, "reason": "not_found", "http_status": 404}
    if row.get("status") != "pending":
        return {"ok": False, "reason": "already_terminal",
                "current_status": row.get("status"),
                "http_status": 409}
    roles = row.get("authorized_confirmer_roles") or []
    if user_role not in roles:
        return {"ok": False, "reason": "role_not_authorized",
                "allowed_roles": roles, "http_status": 403}

    now = utc_now()
    res = await db.field_suggestions.update_one(
        {"id": suggestion_id, "status": "pending"},
        {"$set": {"status": "rejected",
                   "rejected_by": user_id,
                   "rejected_at": now.isoformat(),
                   "rejection_reason": reason.strip(),
                   "updated_at": now.isoformat()}},
    )
    if not res.matched_count:
        latest = await db.field_suggestions.find_one(
            {"id": suggestion_id}, {"_id": 0},
        )
        return {"ok": False, "reason": "already_terminal",
                "current_status": (latest or {}).get("status"),
                "http_status": 409}
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="field_suggestions",
            op="FIELD_SUGGESTION_REJECTED",
            record_id=suggestion_id, before=None,
            after={"entity_type": row.get("entity_type"),
                    "entity_id": row.get("entity_id"),
                    "field_path": row.get("field_path"),
                    "rejected_by": user_id,
                    "user_role": user_role,
                    "reason": reason.strip()},
            actor_id=user_id,
            request_id=request_id or f"fs_reject_{suggestion_id}",
        )
    except Exception as exc:
        log.warning("audit FIELD_SUGGESTION_REJECTED failed (%s): %s",
                    suggestion_id, exc)
    refreshed = await db.field_suggestions.find_one(
        {"id": suggestion_id}, {"_id": 0},
    )
    return {"ok": True, "row": refreshed}


async def _mark_superseded(
    *, tenant_id: str, old_id: str,
    actor_id: Optional[str], request_id: Optional[str],
) -> None:
    """Internal helper -- called by create_suggestion when a pending
    row needs to step aside for a fresh suggestion. Audits the
    transition."""
    db = get_db()
    now = utc_now()
    res = await db.field_suggestions.update_one(
        tenant_filter(tenant_id, {"id": old_id, "status": "pending"}),
        {"$set": {"status": "superseded",
                   "updated_at": now.isoformat()}},
    )
    if not res.matched_count:
        return
    try:
        await write_audit(
            tenant_id=tenant_id, table_name="field_suggestions",
            op="FIELD_SUGGESTION_SUPERSEDED",
            record_id=old_id, before=None,
            after={"old_id": old_id,
                    "reason": "ai_re_suggested_different_value"},
            actor_id=actor_id,
            request_id=request_id or f"fs_supersede_{old_id}",
        )
    except Exception as exc:
        log.warning("audit FIELD_SUGGESTION_SUPERSEDED failed (%s): %s",
                    old_id, exc)


async def supersede_suggestion(
    *, tenant_id: str, old_id: str, new_id: str,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Public entrypoint -- callers (e.g. AI producers explicitly
    re-running on a different config) can mark an old row superseded
    and point it at a new one. Mostly used by tests + power tools;
    normal flow goes through create_suggestion which auto-supersedes."""
    db = get_db()
    await _mark_superseded(
        tenant_id=tenant_id, old_id=old_id,
        actor_id=actor_id, request_id=request_id,
    )
    if new_id:
        await db.field_suggestions.update_one(
            {"id": old_id},
            {"$set": {"superseded_by_suggestion_id": new_id}},
        )
    row = await db.field_suggestions.find_one(
        tenant_filter(tenant_id, {"id": old_id}),
        {"_id": 0},
    )
    return {"ok": True, "row": row}


# =====================================================================
# Reads
# =====================================================================

async def get_suggestions_for_entity(
    *, tenant_id: str, entity_type: str, entity_id: str,
    include_terminal: bool = False,
) -> list[dict]:
    """All suggestions on this entity. Default returns only pending
    rows (what the form widget needs); include_terminal=True returns
    the full ledger for the entity detail view."""
    db = get_db()
    q: dict = {"entity_type": entity_type, "entity_id": entity_id}
    if not include_terminal:
        q["status"] = "pending"
    rows = await db.field_suggestions.find(
        tenant_filter(tenant_id, q), {"_id": 0},
    ).sort("created_at", -1).to_list(length=500)
    return rows


async def get_pending_suggestions_for_user(
    *, tenant_id: str, user_role: str,
    project_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 100, cursor: Optional[str] = None,
) -> dict:
    """Review-queue feed. Returns pending suggestions where the
    caller's role is in authorized_confirmer_roles. Cursor-paginated
    on created_at desc.
    """
    db = get_db()
    q: dict = {
        "tenant_id": tenant_id,
        "status": "pending",
        "authorized_confirmer_roles": user_role,
    }
    if project_id:
        q["project_id"] = project_id
    if entity_type:
        q["entity_type"] = entity_type
    if cursor:
        q["created_at"] = {"$lt": cursor}
    limit = max(1, min(int(limit), 500))
    rows = await db.field_suggestions.find(
        q, {"_id": 0},
    ).sort("created_at", -1).limit(limit).to_list(length=limit)
    next_cursor = rows[-1]["created_at"] if rows and len(rows) == limit else None
    # Lightweight by-entity counts for the badge UI.
    counts_by_entity_type: dict[str, int] = {}
    for r in rows:
        et = r.get("entity_type") or "unknown"
        counts_by_entity_type[et] = counts_by_entity_type.get(et, 0) + 1
    return {
        "rows": rows,
        "next_cursor": next_cursor,
        "count_in_page": len(rows),
        "counts_by_entity_type": counts_by_entity_type,
    }


async def get_suggestion(
    *, tenant_id: str, suggestion_id: str,
) -> Optional[dict]:
    """Single-row lookup. Tenant-scoped; returns None for cross-tenant
    URL manipulation so the router can 404."""
    db = get_db()
    return await db.field_suggestions.find_one(
        tenant_filter(tenant_id, {"id": suggestion_id}),
        {"_id": 0},
    )


# =====================================================================
# Expiration (Phase 5 background job hook)
# =====================================================================

async def expire_stale_pending(
    *, tenant_id: Optional[str] = None, older_than_days: int = EXPIRATION_DAYS,
    limit: int = 1000,
) -> dict:
    """Auto-reject pending rows older than `older_than_days`. Phase 5
    wires this to a daily job; Phase 1 exposes the function so the
    shape + audit op are stable from day 1.

    Returns counts. Best-effort -- individual row failures are logged
    but don't abort the sweep."""
    db = get_db()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, older_than_days))).isoformat()
    q: dict = {"status": "pending", "created_at": {"$lt": cutoff}}
    if tenant_id:
        q["tenant_id"] = tenant_id
    rows = await db.field_suggestions.find(
        q, {"_id": 0, "id": 1, "tenant_id": 1,
             "entity_type": 1, "entity_id": 1, "field_path": 1},
    ).limit(max(1, min(int(limit), 5000))).to_list(length=None)
    out = {"swept": len(rows), "expired": 0, "failures": 0,
           "tenants_touched": 0}
    seen_tenants: set = set()
    now = utc_now()
    for r in rows:
        try:
            tid = r.get("tenant_id")
            if not tid:
                continue
            seen_tenants.add(tid)
            res = await db.field_suggestions.update_one(
                {"id": r["id"], "status": "pending"},
                {"$set": {"status": "expired",
                           "rejection_reason": f"expired_after_{older_than_days}_days",
                           "rejected_at": now.isoformat(),
                           "updated_at": now.isoformat()}},
            )
            if res.matched_count:
                out["expired"] += 1
                try:
                    await write_audit(
                        tenant_id=tid, table_name="field_suggestions",
                        op="FIELD_SUGGESTION_EXPIRED",
                        record_id=r["id"], before=None,
                        after={"entity_type": r.get("entity_type"),
                                "entity_id": r.get("entity_id"),
                                "field_path": r.get("field_path"),
                                "expired_after_days": older_than_days},
                        actor_id=None,
                        request_id=f"fs_expired_{r['id']}",
                    )
                except Exception:
                    pass
        except Exception as exc:
            out["failures"] += 1
            log.warning("expire_stale_pending failed for %s: %s",
                        r.get("id"), exc)
    out["tenants_touched"] = len(seen_tenants)
    return out


# =====================================================================
# Bulk operations (Phase 2 wires the endpoint; Phase 1 ships helpers)
# =====================================================================

async def bulk_confirm(
    *, tenant_id: str, suggestion_ids: list[str],
    user_id: str, user_role: str,
    request_id: Optional[str] = None,
) -> dict:
    """Confirm up to BULK_CAP suggestions in one call. Each row is
    individually validated against the user's role; un-authorized
    rows are skipped + returned in the `skipped` list. Atomic per-row
    (not per-batch) so a partial failure doesn't abort the rest."""
    if not suggestion_ids:
        return {"ok": False, "reason": "no_ids"}
    if len(suggestion_ids) > BULK_CAP:
        return {"ok": False, "reason": f"too_many_ids_max_{BULK_CAP}"}
    out = {"ok": True, "confirmed": [], "skipped": []}
    for sid in suggestion_ids:
        res = await confirm_suggestion(
            tenant_id=tenant_id, suggestion_id=sid,
            user_id=user_id, user_role=user_role,
            request_id=request_id,
        )
        if res.get("ok"):
            out["confirmed"].append(sid)
        else:
            out["skipped"].append({"id": sid,
                                     "reason": res.get("reason"),
                                     "current_status": res.get("current_status")})
    return out


async def bulk_reject(
    *, tenant_id: str, suggestion_ids: list[str], reason: str,
    user_id: str, user_role: str,
    request_id: Optional[str] = None,
) -> dict:
    if not suggestion_ids:
        return {"ok": False, "reason": "no_ids"}
    if len(suggestion_ids) > BULK_CAP:
        return {"ok": False, "reason": f"too_many_ids_max_{BULK_CAP}"}
    if not reason or len(reason.strip()) < 10:
        return {"ok": False, "reason": "reason_min_10_chars"}
    out = {"ok": True, "rejected": [], "skipped": []}
    for sid in suggestion_ids:
        res = await reject_suggestion(
            tenant_id=tenant_id, suggestion_id=sid,
            user_id=user_id, user_role=user_role,
            reason=reason, request_id=request_id,
        )
        if res.get("ok"):
            out["rejected"].append(sid)
        else:
            out["skipped"].append({"id": sid,
                                     "reason": res.get("reason"),
                                     "current_status": res.get("current_status")})
    return out
