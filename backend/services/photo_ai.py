"""Bucket F Phase 1 (2026-05-08) -- Photo AI service module.

Owns every interaction with Claude Vision for photo analysis. Phase 1
ships the data model + service stubs so the migration + indexes have
something to plug into; the actual Vision call wiring + endpoints
land in Phase 2.

Design notes
------------
- CUI safety check fires BEFORE any image is read. Projects flagged
  is_cui=true never have their photos sent to Claude Vision (federal
  contract requirement). The check returns False for CUI projects so
  upstream callers can skip-stamp the tray row.
- Tenant + project AI kill switches. Tenants and projects each carry
  ai_enabled (default True). Either being False blocks analysis.
- pHash dedup: BEFORE the API call, query (tenant_id, phash,
  ai_analysis_status="complete") -- if a hit exists, copy the
  suggestion fields to the new tray row + mark ai_reused_from_phash
  = True. Zero API cost. This is the single biggest win for the
  reburn-photos-of-the-same-pour case on construction sites.
- Budget enforcement: every analyze call calls check_budget first.
  If over, the tray row is stamped budget_paused + an audit fires.
  Spend accumulates per tenant per month via increment_spend. Reset
  happens on the 1st (handled in check_budget via reset-on-read).
- Rate limit: batch_analyze processes at most 10 per call with a
  200ms inter-call throttle. Per-tenant; concurrent batches across
  tenants are independent.
- Retry policy: one retry after 5s on transient failure (network,
  rate-limited from Anthropic, malformed JSON). After two consecutive
  failures the row gets ai_analysis_status="failed" + audit op
  PHOTO_AI_ANALYSIS_FAILED with the specific failure reason.
- Image preprocessing: Pillow resize to 1568x1568 max (Claude Vision
  recommended), JPEG quality 85, RGB convert if HEIC/PNG/WebP. Done
  inline in analyze_photo before the API call.
- Pricing: Claude Sonnet 4.5 (claude-sonnet-4-5-20250929):
    input  $3.00 / 1M tokens
    output $15.00 / 1M tokens
  Image tokens vary by size; we charge actual usage reported by the
  Anthropic API.

Phase 1 ships these signatures with mock returns (DEV_MODE env var
or no Anthropic key triggers the stub path). Phase 2 wires real
Claude Vision + the endpoints that call this module.
"""
from __future__ import annotations

import logging
import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import get_db, tenant_filter

log = logging.getLogger("photo_ai")

# Pricing per the Anthropic API (Claude Sonnet 4.5). Update when the
# pricing card changes. Constants here so tests can verify cost math
# without monkey-patching.
COST_PER_INPUT_TOKEN_USD = 3.00 / 1_000_000.0
COST_PER_OUTPUT_TOKEN_USD = 15.00 / 1_000_000.0

# Default model. Tracked on each tray row's ai_model_version so a
# later model bump leaves prior analyses intact for audit comparison.
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

# Image processing constants.
MAX_DIMENSION_PX = 1568            # Claude Vision recommended max
JPEG_QUALITY = 85

# Rate limit constants.
BATCH_SIZE_LIMIT = 10
BATCH_THROTTLE_MS = 200
RETRY_DELAY_SECONDS = 5
MAX_RETRIES = 1                    # one retry, then mark failed


# =====================================================================
# CUI + budget gates
# =====================================================================

async def check_cui_safety(*, tenant_id: str, project_id: str) -> dict:
    """Returns {safe_to_analyze: bool, reason: Optional[str]}.

    safe_to_analyze=False means do NOT send the photo to Claude
    Vision. Phase 2's upload flow stamps the tray row with
    ai_analysis_status="skipped" + ai_failure_reason indicating
    which gate fired so the UI can render the right "AI disabled"
    pill.

    Order of checks (any one False -> skip):
      1. Tenant ai_enabled
      2. Project ai_enabled
      3. Project is_cui (federal compliance)
    """
    db = get_db()
    tenant = await db.tenants.find_one(
        {"id": tenant_id},
        {"_id": 0, "ai_enabled": 1, "display_name": 1},
    )
    if not tenant:
        return {"safe_to_analyze": False, "reason": "tenant_not_found"}
    if tenant.get("ai_enabled") is False:
        return {"safe_to_analyze": False,
                "reason": "tenant_ai_disabled",
                "display": "AI disabled for this tenant"}

    project = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        {"_id": 0, "is_cui": 1, "ai_enabled": 1, "name": 1},
    )
    if not project:
        return {"safe_to_analyze": False, "reason": "project_not_found"}
    if project.get("is_cui") is True:
        return {"safe_to_analyze": False,
                "reason": "cui_blocked",
                "display": "AI disabled — CUI project"}
    if project.get("ai_enabled") is False:
        return {"safe_to_analyze": False,
                "reason": "project_ai_disabled",
                "display": "AI disabled for this project"}
    return {"safe_to_analyze": True, "reason": None}


def _first_of_next_month(now: datetime) -> datetime:
    # Roll to the first of next month at 00:00 UTC.
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)


async def check_budget(*, tenant_id: str) -> dict:
    """Returns {budget_ok, remaining_usd, current_spend, monthly_limit,
    reset_at, ai_enabled}.

    Reset-on-read: if ai_current_month_resets_at <= now, zero the
    spend and bump the reset to first-of-next-month. Avoids a cron
    requirement; the first request of the new month triggers it.
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    tenant = await db.tenants.find_one(
        {"id": tenant_id},
        {"_id": 0, "ai_enabled": 1, "ai_monthly_budget_usd": 1,
         "ai_current_month_spend_usd": 1,
         "ai_current_month_resets_at": 1},
    )
    if not tenant:
        return {"budget_ok": False, "reason": "tenant_not_found",
                "remaining_usd": 0.0, "current_spend": 0.0,
                "monthly_limit": 0.0, "ai_enabled": False}

    ai_enabled = bool(tenant.get("ai_enabled", True))
    monthly_limit = float(tenant.get("ai_monthly_budget_usd", 100.0))
    current_spend = float(tenant.get("ai_current_month_spend_usd", 0.0))
    reset_at_raw = tenant.get("ai_current_month_resets_at")
    reset_at = None
    if isinstance(reset_at_raw, str):
        try:
            reset_at = datetime.fromisoformat(reset_at_raw.replace("Z", "+00:00"))
        except Exception:
            reset_at = None
    elif isinstance(reset_at_raw, datetime):
        reset_at = reset_at_raw
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)

    # Reset-on-read: if the reset boundary has passed, zero the spend.
    if reset_at is None or reset_at <= now:
        next_reset = _first_of_next_month(now)
        await db.tenants.update_one(
            {"id": tenant_id},
            {"$set": {
                "ai_current_month_spend_usd": 0.0,
                "ai_current_month_resets_at": next_reset.isoformat(),
            }},
        )
        current_spend = 0.0
        reset_at = next_reset

    remaining = max(0.0, monthly_limit - current_spend)
    budget_ok = ai_enabled and current_spend < monthly_limit
    return {
        "budget_ok": budget_ok,
        "ai_enabled": ai_enabled,
        "remaining_usd": round(remaining, 4),
        "current_spend": round(current_spend, 4),
        "monthly_limit": round(monthly_limit, 4),
        "reset_at": reset_at.isoformat() if reset_at else None,
        "pct_consumed": round(
            (current_spend / monthly_limit * 100.0) if monthly_limit > 0 else 0.0,
            1,
        ),
        "warning_80pct": (current_spend / monthly_limit) >= 0.8 if monthly_limit > 0 else False,
        "reason": None if budget_ok else (
            "tenant_ai_disabled" if not ai_enabled else "budget_exceeded"
        ),
    }


async def increment_spend(
    *, tenant_id: str, input_tokens: int, output_tokens: int,
) -> dict:
    """Add the actual API cost for one analyze call to the tenant's
    running monthly tally. Returns the cost contribution + new totals.
    """
    cost = (input_tokens * COST_PER_INPUT_TOKEN_USD
            + output_tokens * COST_PER_OUTPUT_TOKEN_USD)
    db = get_db()
    res = await db.tenants.find_one_and_update(
        {"id": tenant_id},
        {"$inc": {"ai_current_month_spend_usd": cost}},
        return_document=True,
        projection={"_id": 0, "ai_current_month_spend_usd": 1,
                     "ai_monthly_budget_usd": 1},
    )
    new_spend = float((res or {}).get("ai_current_month_spend_usd", 0.0))
    limit = float((res or {}).get("ai_monthly_budget_usd", 100.0))
    return {
        "added_usd": round(cost, 6),
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "new_total_usd": round(new_spend, 4),
        "monthly_limit_usd": round(limit, 4),
        "pct_consumed": round((new_spend / limit * 100.0) if limit > 0 else 0.0, 1),
    }


# =====================================================================
# pHash dedup
# =====================================================================

async def find_dedup_source(
    *, tenant_id: str, phash: str,
) -> Optional[dict]:
    """Returns a completed tray row matching the same (tenant, phash),
    or None. Caller copies the AI fields + sets ai_reused_from_phash
    on the new row -- no API call needed."""
    if not phash:
        return None
    db = get_db()
    src = await db.photo_tray.find_one(
        {"tenant_id": tenant_id, "phash": phash,
         "ai_analysis_status": "complete"},
        {"_id": 0, "id": 1, "ai_suggested_class": 1,
         "ai_suggested_caption": 1, "ai_suggested_event_type": 1,
         "ai_suggested_trade": 1, "ai_confidence_score": 1,
         "ai_safety_concerns_detected": 1, "ai_model_version": 1,
         "ai_raw_response": 1},
        sort=[("ai_analysis_completed_at", -1)],
    )
    return src


# =====================================================================
# Core: analyze_photo
# =====================================================================

def _is_dev_mode() -> bool:
    """Phase 1 default: dev mode = no Anthropic key configured OR
    PHOTO_AI_DEV_MODE=1 env. Phase 2 ships the real Vision call;
    until then the stub path is what every caller exercises."""
    return (
        os.environ.get("PHOTO_AI_DEV_MODE") == "1"
        or not os.environ.get("EMERGENT_LLM_KEY")
    )


def _stub_analysis(tray_row: dict) -> dict:
    """Mock analysis used when the real API isn't reachable. Returns
    the same shape the real call will produce so downstream code can
    be tested without an Anthropic key. Deterministic from tray.id so
    smoke tests are repeatable."""
    seed_int = sum(ord(c) for c in (tray_row.get("id") or "x"))
    classes = ["progress", "discovery", "damage", "quality", "other"]
    event_types = ["delay", "rfi", "qc_issue", "safety_event",
                    "coordination_event", "delivery", "none"]
    cls = classes[seed_int % len(classes)]
    ev = event_types[seed_int % len(event_types)]
    confidence = 0.5 + (seed_int % 50) / 100.0  # 0.50-0.99
    return {
        "ai_suggested_class": cls,
        "ai_suggested_caption": f"[dev stub] {cls} photo from tray {tray_row.get('id', '?')[:8]}",
        "ai_suggested_event_type": ev,
        "ai_suggested_trade": "general",
        "ai_confidence_score": round(confidence, 2),
        "ai_safety_concerns_detected": [],
        "ai_raw_response": {"_dev_mode": True, "seed": seed_int},
        "ai_model_version": "dev-stub-v1",
        "ai_cost_tokens_input": 0,
        "ai_cost_tokens_output": 0,
    }


async def analyze_photo(
    *, tenant_id: str, tray_id: str,
) -> dict:
    """Phase 1 entrypoint with stub Vision path. Returns:
      {ok: bool, status: <new tray ai_analysis_status>,
       reason?: str, fields?: {ai_*: ...}}

    The real call path (Phase 2) replaces _stub_analysis() with the
    Anthropic Vision request + Pillow preprocessing + retry loop.
    This signature stays so endpoints written in Phase 2 don't have
    to change shape.
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    tray = await db.photo_tray.find_one(
        tenant_filter(tenant_id, {"id": tray_id}),
        {"_id": 0},
    )
    if not tray:
        return {"ok": False, "status": "skipped",
                "reason": "tray_not_found"}

    # Already complete -- idempotent no-op.
    if tray.get("ai_analysis_status") == "complete":
        return {"ok": True, "status": "complete",
                "reason": "already_complete"}

    # CUI / AI-disabled gate.
    safety = await check_cui_safety(
        tenant_id=tenant_id,
        project_id=tray.get("project_id"),
    )
    if not safety.get("safe_to_analyze"):
        update = {
            "ai_analysis_status": "skipped",
            "ai_analysis_started_at": now_iso,
            "ai_analysis_completed_at": now_iso,
            "ai_failure_reason": safety.get("reason"),
            "ai_model_version": DEFAULT_MODEL,
        }
        await db.photo_tray.update_one(
            {"id": tray_id}, {"$set": update},
        )
        return {"ok": False, "status": "skipped",
                "reason": safety.get("reason"), "fields": update}

    # Budget gate.
    budget = await check_budget(tenant_id=tenant_id)
    if not budget.get("budget_ok"):
        update = {
            "ai_analysis_status": "budget_paused",
            "ai_analysis_started_at": now_iso,
            "ai_failure_reason": budget.get("reason"),
            "ai_model_version": DEFAULT_MODEL,
        }
        await db.photo_tray.update_one(
            {"id": tray_id}, {"$set": update},
        )
        # Budget pause is audit-worthy -- the operator needs to see it.
        try:
            from audit import write_audit
            await write_audit(
                tenant_id=tenant_id, table_name="photo_tray",
                op="PHOTO_AI_BUDGET_PAUSED",
                record_id=tray_id, before=None,
                after={"reason": budget.get("reason"),
                        "current_spend_usd": budget.get("current_spend"),
                        "monthly_limit_usd": budget.get("monthly_limit"),
                        "tray_id": tray_id},
                actor_id=None,
                request_id=f"photo_ai_budget_{tray_id}",
            )
        except Exception:
            pass
        return {"ok": False, "status": "budget_paused",
                "reason": budget.get("reason"), "fields": update}

    # pHash dedup. Zero-cost win for repeat uploads of the same image.
    phash = tray.get("phash")
    if phash:
        src = await find_dedup_source(tenant_id=tenant_id, phash=phash)
        if src:
            update = {
                "ai_analysis_status": "complete",
                "ai_analysis_started_at": now_iso,
                "ai_analysis_completed_at": now_iso,
                "ai_suggested_class": src.get("ai_suggested_class"),
                "ai_suggested_caption": src.get("ai_suggested_caption"),
                "ai_suggested_event_type": src.get("ai_suggested_event_type"),
                "ai_suggested_trade": src.get("ai_suggested_trade"),
                "ai_confidence_score": src.get("ai_confidence_score"),
                "ai_safety_concerns_detected":
                    list(src.get("ai_safety_concerns_detected") or []),
                "ai_model_version": src.get("ai_model_version") or DEFAULT_MODEL,
                "ai_reused_from_phash": True,
                "ai_cost_tokens_input": 0,
                "ai_cost_tokens_output": 0,
                "ai_raw_response": {"_reused_from": src["id"]},
            }
            await db.photo_tray.update_one(
                {"id": tray_id}, {"$set": update},
            )
            return {"ok": True, "status": "complete",
                    "reason": "phash_reused",
                    "source_tray_id": src["id"],
                    "fields": update}

    # Mark analyzing so the UI can poll.
    await db.photo_tray.update_one(
        {"id": tray_id},
        {"$set": {"ai_analysis_status": "analyzing",
                   "ai_analysis_started_at": now_iso}},
    )

    # Real Vision call goes here in Phase 2. Phase 1 stub returns
    # deterministic mock fields when no API key is configured.
    if _is_dev_mode():
        result_fields = _stub_analysis(tray)
    else:
        # Phase 2 wires the real Vision call. Until then, fall back
        # to the stub even with a key set so we don't accidentally
        # bill against a partially configured environment.
        result_fields = _stub_analysis(tray)

    finish = datetime.now(timezone.utc).isoformat()
    update = {
        "ai_analysis_status": "complete",
        "ai_analysis_completed_at": finish,
        **result_fields,
    }
    await db.photo_tray.update_one(
        {"id": tray_id}, {"$set": update},
    )

    # Cost increment + audit. Stub mode reports 0 tokens so no cost.
    in_tok = int(result_fields.get("ai_cost_tokens_input") or 0)
    out_tok = int(result_fields.get("ai_cost_tokens_output") or 0)
    if in_tok or out_tok:
        await increment_spend(
            tenant_id=tenant_id, input_tokens=in_tok, output_tokens=out_tok,
        )

    return {"ok": True, "status": "complete", "fields": update}


# =====================================================================
# Batch + reject + override + stats
# =====================================================================

async def batch_analyze_pending(
    *, tenant_id: str, project_id: Optional[str] = None,
    limit: int = BATCH_SIZE_LIMIT,
) -> dict:
    """Walks tray rows with ai_analysis_status="pending" (optionally
    scoped to project_id) and calls analyze_photo on up to `limit`
    rows. Throttles between calls. Returns aggregate counts."""
    limit = max(1, min(int(limit), BATCH_SIZE_LIMIT))
    db = get_db()
    q = {"tenant_id": tenant_id, "ai_analysis_status": "pending"}
    if project_id:
        q["project_id"] = project_id
    rows = await db.photo_tray.find(q, {"_id": 0, "id": 1}).limit(limit).to_list(limit)
    results = {"attempted": len(rows), "complete": 0, "skipped": 0,
                "budget_paused": 0, "failed": 0, "phash_reused": 0}
    for r in rows:
        out = await analyze_photo(tenant_id=tenant_id, tray_id=r["id"])
        status = out.get("status")
        if status == "complete":
            results["complete"] += 1
            if out.get("reason") == "phash_reused":
                results["phash_reused"] += 1
        elif status == "skipped":
            results["skipped"] += 1
        elif status == "budget_paused":
            results["budget_paused"] += 1
            # Stop the batch -- subsequent rows would also pause.
            results["stopped_at_budget"] = True
            break
        elif status == "failed":
            results["failed"] += 1
        # Throttle between analyze calls.
        await asyncio.sleep(BATCH_THROTTLE_MS / 1000.0)
    return results


async def reject_analysis(
    *, tenant_id: str, tray_id: str, reason: str, actor_id: str,
) -> dict:
    """User explicitly rejects the AI suggestion. tray row stays in
    the tray with status="rejected_by_user" and ai_user_override_
    reason set. Phase 2 audits this op."""
    if not reason or len(reason.strip()) < 10:
        return {"ok": False, "reason": "reason_required_min_10_chars"}
    db = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db.photo_tray.update_one(
        tenant_filter(tenant_id, {"id": tray_id}),
        {"$set": {
            "ai_analysis_status": "rejected_by_user",
            "ai_user_override_reason": reason.strip(),
            "ai_analysis_completed_at": now_iso,
        }},
    )
    if not res.matched_count:
        return {"ok": False, "reason": "tray_not_found"}
    return {"ok": True, "tray_id": tray_id, "reason": reason.strip()}


async def override_analysis(
    *, tenant_id: str, tray_id: str, fields: dict, override_reason: str,
    actor_id: str,
) -> dict:
    """User overrides one or more AI-suggested fields. Original AI
    values stay on the row (ai_suggested_*) so Calibration RAG can
    learn from the diff. Override fields land on the corresponding
    user-authored placeholders + ai_user_overridden=True.

    Phase 2 wires this to a dedicated endpoint + audits the diff in
    the photo_ai_calibration collection."""
    allowed = {"photo_class", "caption", "event_type", "trade"}
    bad = set(fields.keys()) - allowed
    if bad:
        return {"ok": False, "reason": f"unknown fields: {sorted(bad)}"}
    if not override_reason or len(override_reason.strip()) < 5:
        return {"ok": False, "reason": "override_reason_required"}
    db = get_db()
    tray = await db.photo_tray.find_one(
        tenant_filter(tenant_id, {"id": tray_id}),
        {"_id": 0},
    )
    if not tray:
        return {"ok": False, "reason": "tray_not_found"}
    update = {
        "ai_user_overridden": True,
        "ai_user_override_reason": override_reason.strip(),
    }
    await db.photo_tray.update_one(
        {"id": tray_id}, {"$set": update},
    )
    return {"ok": True, "tray_id": tray_id,
            "user_overrides": fields,
            "ai_originals": {
                "photo_class": tray.get("ai_suggested_class"),
                "caption": tray.get("ai_suggested_caption"),
                "event_type": tray.get("ai_suggested_event_type"),
                "trade": tray.get("ai_suggested_trade"),
            }}


async def get_analysis_stats(
    *, tenant_id: str, project_id: Optional[str] = None, days: int = 30,
) -> dict:
    """Per-project (or tenant-wide) rollup over a recent window.
    Returns the shape the Photos page stats card needs in Phase 3."""
    if days < 1:
        days = 1
    if days > 365:
        days = 365
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = {"tenant_id": tenant_id,
         "uploaded_at": {"$gte": cutoff}}
    if project_id:
        q["project_id"] = project_id
    rows = await db.photo_tray.find(q, {"_id": 0}).to_list(length=None)
    analyzed = 0
    confidence_sum = 0.0
    confidence_n = 0
    overridden = 0
    rejected = 0
    safety_counts: dict[str, int] = {}
    total_in_tokens = 0
    total_out_tokens = 0
    by_status: dict[str, int] = {}
    failure_reasons: dict[str, int] = {}
    for r in rows:
        s = r.get("ai_analysis_status") or "pending"
        by_status[s] = by_status.get(s, 0) + 1
        if s == "complete":
            analyzed += 1
            c = r.get("ai_confidence_score")
            if c is not None:
                confidence_sum += float(c)
                confidence_n += 1
            if r.get("ai_user_overridden"):
                overridden += 1
            for concern in (r.get("ai_safety_concerns_detected") or []):
                safety_counts[concern] = safety_counts.get(concern, 0) + 1
            total_in_tokens += int(r.get("ai_cost_tokens_input") or 0)
            total_out_tokens += int(r.get("ai_cost_tokens_output") or 0)
        elif s == "rejected_by_user":
            rejected += 1
        elif s == "failed":
            failure_reasons[r.get("ai_failure_reason") or "unknown"] = (
                failure_reasons.get(r.get("ai_failure_reason") or "unknown", 0) + 1
            )
    top_safety = sorted(
        safety_counts.items(), key=lambda kv: -kv[1],
    )[:5]
    estimated_cost_usd = (
        total_in_tokens * COST_PER_INPUT_TOKEN_USD
        + total_out_tokens * COST_PER_OUTPUT_TOKEN_USD
    )
    avg_conf = (confidence_sum / confidence_n) if confidence_n else 0.0
    override_rate = (overridden / analyzed) if analyzed else 0.0
    rejection_rate = (rejected / max(1, analyzed + rejected))
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "window_days": days,
        "uploaded_in_window": len(rows),
        "analyzed_in_window": analyzed,
        "avg_confidence_score": round(avg_conf, 3),
        "override_count": overridden,
        "override_rate": round(override_rate, 3),
        "rejection_count": rejected,
        "rejection_rate": round(rejection_rate, 3),
        "top_safety_concerns": [
            {"concern": k, "count": v} for k, v in top_safety
        ],
        "by_status": by_status,
        "failure_reasons": failure_reasons,
        "input_tokens_total": total_in_tokens,
        "output_tokens_total": total_out_tokens,
        "estimated_cost_usd": round(estimated_cost_usd, 4),
    }
