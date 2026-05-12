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

import base64
import io
import json
import logging
import os
import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import get_db, tenant_filter

log = logging.getLogger("photo_ai")

# Bucket F Phase 3 (2026-05-08) -- HEIF/HEIC support for iPhone uploads.
# pillow-heif registers itself as a Pillow plugin on import. ICE supers
# use iPhones; without this, every iOS Photos upload fails with
# NotAnImageError at preprocess time. Module-load registration so the
# first image upload doesn't pay the cost.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    log.info("pillow-heif registered: HEIC/HEIF uploads supported")
except ImportError:
    log.warning("pillow-heif not installed; HEIC uploads will fail "
                "with NotAnImageError")
except Exception as _heif_exc:
    log.warning("pillow-heif registration failed: %s", _heif_exc)

# Test 4A/B follow-up (2026-05-12) -- explicit dev-stub fallback signal.
# Operators sometimes don't realize the Emergent env never received the
# ANTHROPIC_API_KEY (vs. having it set to a stale value) and assume the
# stub path is broken. Loud WARN at module load so the boot log makes
# the posture obvious. Anthropic SDK is also import-probed here so any
# version mismatch (e.g. v0.40 breaking changes vs. our pinned 0.39)
# surfaces before the first photo upload rather than after.
# Federal-contractor compliance follow-up (2026-05-12): ALL Claude Vision
# traffic now flows through Emergent's LLM gateway via EMERGENT_LLM_KEY,
# NOT api.anthropic.com directly. No separate Anthropic Console account
# is maintained. The boot-time probe verifies emergentintegrations is
# importable AND a gateway key is configured before the first analyze.
_HAS_EMERGENT_LLM_KEY = bool(os.environ.get("EMERGENT_LLM_KEY"))
_PHOTO_AI_DEV_OVERRIDE = os.environ.get("PHOTO_AI_DEV_MODE") == "1"
if not _HAS_EMERGENT_LLM_KEY:
    log.warning(
        "Photo AI: EMERGENT_LLM_KEY not set -- falling back to "
        "deterministic dev stub. Real Claude Vision will NOT be "
        "called. Set EMERGENT_LLM_KEY to enable.",
    )
elif _PHOTO_AI_DEV_OVERRIDE:
    log.warning(
        "Photo AI: PHOTO_AI_DEV_MODE=1 set; using dev stub even "
        "though an API key is configured.",
    )
else:
    log.info(
        "Photo AI: Emergent LLM gateway path enabled "
        "(EMERGENT_LLM_KEY present).",
    )

# Probe the emergentintegrations SDK at module load. If the import
# fails we want to know now, not on the first analyze call. The
# gateway client (LlmChat + ImageContent + UserMessage) lives in
# emergentintegrations.llm.chat.
_GATEWAY_SDK_OK = False
try:
    from emergentintegrations.llm.chat import (  # noqa: F401
        LlmChat as _ProbeLlmChat,
        UserMessage as _ProbeUserMessage,
        ImageContent as _ProbeImageContent,
    )
    _GATEWAY_SDK_OK = True
    log.info("Photo AI: emergentintegrations gateway SDK import OK")
except ImportError as _gw_exc:
    log.warning(
        "Photo AI: gateway client init failed: emergentintegrations "
        "not installed (%s) -- dev stub will be used.",
        _gw_exc,
    )
except Exception as _gw_exc:
    log.warning(
        "Photo AI: gateway client init failed: %s -- dev stub will be used.",
        _gw_exc,
    )

# Back-compat alias so older audit messages / failure_reason codes
# that referenced the anthropic SDK probe name still resolve.
_ANTHROPIC_SDK_OK = _GATEWAY_SDK_OK

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

# Bucket F Phase 5 (2026-05-11) -- performance + observability knobs.
# All values are constants so smoke tests can verify caching behavior
# without runtime config plumbing.
IMAGE_CACHE_TTL_SECONDS = 60       # storage blob cache for repeat reads
STATS_CACHE_TTL_SECONDS = 300      # 5min cache on get_analysis_stats
TIMING_RETENTION_DAYS = 90         # ai_call_timings TTL window

# In-process LRU-ish caches. Single-worker assumption; multi-worker
# deploys lose some hits but never serve stale data because every entry
# carries an absolute expiry timestamp.
_image_cache: dict[str, tuple[float, bytes, str]] = {}
_image_cache_max = 64              # bounded to prevent RSS bloat
_stats_cache: dict[str, tuple[float, dict]] = {}
_stats_cache_max = 32

# Verbose failure-reason labels. Service code stamps the short reason
# code on the tray row (image_too_large, vision_timeout, etc.); the
# verbose label is returned in API responses so the UI can render
# specific user-facing copy without hard-coding the mapping.
FAILURE_REASON_VERBOSE = {
    "image_too_large":
        "The image exceeds the 20 MB upload limit. Resize or compress before retrying.",
    "not_an_image":
        "The file isn't a recognized image format. HEIC requires iOS support to be enabled.",
    "image_conversion_failed":
        "Pillow couldn't decode the image bytes (corrupted file or unsupported codec).",
    "storage_unavailable":
        "Couldn't read the photo bytes from storage. Check S3 / object-store connectivity.",
    "vision_api_error":
        "The Anthropic Vision API returned an error. The retry queue will pick this up.",
    "vision_timeout":
        "The Vision API call exceeded the 30-second timeout. Site network may be unstable.",
    "vision_malformed_json":
        "The AI response wasn't valid JSON. Likely a prompt regression -- file a bug.",
    "persist_failed":
        "Vision call succeeded but the database write failed. Retry will re-persist without re-billing the API.",
    "tray_deleted_mid_flight":
        "Photo was deleted from the tray during analysis. Nothing to retry.",
    "tray_not_found":
        "Tray row not found.",
    "tenant_not_found":
        "Tenant not found.",
    "tenant_ai_disabled":
        "AI is disabled for this tenant. An admin can re-enable in Settings > AI Usage.",
    "project_ai_disabled":
        "AI is disabled for this project. Toggle via the project settings page.",
    "cui_blocked":
        "Project is flagged CUI; photos are never sent to AI per federal compliance.",
    "budget_exceeded":
        "Monthly AI budget hit. New analyses will resume on the 1st or when an admin raises the cap.",
    "pre_ai_upload":
        "This photo was uploaded before AI shipped. No analysis was attempted.",
    "another_caller_in_flight":
        "Another worker is analyzing this photo right now.",
}

# Test 4A/B follow-up (2026-05-12) -- subtype-specific copy for the
# vision_api_error family. Keyed as "vision_api_error:<subtype>" so
# verbose_failure_reason can look up the more specific text first
# before falling back to the generic vision_api_error copy.
FAILURE_REASON_VERBOSE.update({
    "vision_api_error:no_api_key":
        "Photo AI is configured but the ANTHROPIC_API_KEY environment "
        "variable wasn't set on the server. Ask an admin to set it.",
    "vision_api_error:sdk_unavailable":
        "The anthropic Python SDK isn't installed on the server. "
        "Re-deploy with `pip install anthropic==0.39.0`.",
    "vision_api_error:client_init_failed":
        "The Anthropic client couldn't be constructed -- usually an "
        "SDK version mismatch with the pinned anthropic==0.39.0. "
        "Run `pip show anthropic` on the server to verify.",
    "vision_api_error:payload_encode_failed":
        "Couldn't base64-encode the JPEG for the Vision call (memory "
        "or encoding issue).",
    "vision_api_error:auth":
        "Anthropic rejected the API key (401). The key is missing, "
        "expired, or revoked -- ask an admin to rotate it.",
    "vision_api_error:permission_denied":
        "Anthropic denied access (403). The key may not have Vision "
        "scope or the org is blocked.",
    "vision_api_error:rate_limit":
        "Hit the Anthropic rate limit (429). The retry queue will "
        "pick this up shortly; consider lowering BATCH_THROTTLE_MS "
        "if it persists.",
    "vision_api_error:bad_request":
        "Anthropic returned 400 -- payload shape issue (oversized blob, "
        "unsupported media type, etc.). File a bug.",
    "vision_api_error:api_connection":
        "Couldn't reach the Anthropic API (network / DNS / TLS).",
    "vision_api_error:server_error":
        "Anthropic returned a 5xx -- their service is unhealthy. "
        "Retry will pick this up; if it persists check the Anthropic "
        "status page.",
    "vision_api_error:response_parse":
        "Couldn't extract text from the Anthropic response object. "
        "Likely a v0.40+ SDK shape drift; check the pinned version.",
    "vision_api_error:usage_parse":
        "Couldn't read the token-usage block from the response (cost "
        "tracking degraded). Non-fatal; analysis still completed.",
    "vision_api_error:unexpected_api":
        "Unhandled error from the anthropic SDK. Check server logs "
        "for the full traceback.",
    "vision_timeout:api_timeout":
        "The Vision API call exceeded the 30-second timeout (site "
        "network or Anthropic latency).",
})


def verbose_failure_reason(code: Optional[str]) -> str:
    """Returns the verbose label for a tray.ai_failure_reason code.
    Falls back to the base code if subtype-specific copy isn't
    defined, then to the code itself so the UI always has something
    to render.

    Test 4A/B follow-up: handles "code:subtype" lookups by stripping
    the subtype on miss and re-trying with the base code.
    """
    if not code:
        return "No reason recorded."
    if code in FAILURE_REASON_VERBOSE:
        return FAILURE_REASON_VERBOSE[code]
    # Strip ":subtype" and retry with base code.
    if ":" in code:
        base = code.split(":", 1)[0]
        if base in FAILURE_REASON_VERBOSE:
            return FAILURE_REASON_VERBOSE[base]
    # "unclassified:TypeError" etc. -- pass through with a helpful hint.
    if code.startswith("unclassified:"):
        return (
            f"Unclassified failure ({code}). The Photo AI service hit an "
            "exception type that isn't wrapped by a domain-specific class. "
            "Check server logs for the traceback and file a bug -- this "
            "should never reach the UI."
        )
    return f"Unknown failure code: {code}"


async def _evict_image_cache_if_needed() -> None:
    """Bounded eviction. Drops entries with the oldest expiry when over
    the size cap; cheap O(n) scan since the cap is tiny."""
    if len(_image_cache) <= _image_cache_max:
        return
    # Sort by expiry ascending, drop the oldest half.
    items = sorted(_image_cache.items(), key=lambda kv: kv[1][0])
    for key, _ in items[: max(1, len(items) // 2)]:
        _image_cache.pop(key, None)


async def cached_get_object(storage_key: str) -> tuple[bytes, str]:
    """Wrap services.storage.get_object with a 60s TTL cache. Repeat
    reads of the same blob (e.g. tray detail open + analyze + override
    flow on the same photo within a minute) avoid a network round-trip.

    Cache key = storage_key. Returns (blob_bytes, content_type)."""
    import time
    now = time.monotonic()
    cached = _image_cache.get(storage_key)
    if cached and cached[0] > now:
        return cached[1], cached[2]
    from services.storage import get_object
    blob, content_type = await asyncio.to_thread(get_object, storage_key)
    _image_cache[storage_key] = (
        now + IMAGE_CACHE_TTL_SECONDS, blob, content_type,
    )
    await _evict_image_cache_if_needed()
    return blob, content_type


async def record_call_timing(
    *, tenant_id: str, project_id: Optional[str], tray_id: str,
    elapsed_ms: int, model_version: str, status: str,
    input_tokens: int = 0, output_tokens: int = 0,
) -> None:
    """Append a single timing row to ai_call_timings. Used by
    analyze_photo to feed the P50/P95/P99 dashboard. Bounded retention
    via the TTL index declared in server.py _ensure_indexes()."""
    db = get_db()
    try:
        await db.ai_call_timings.insert_one({
            "tenant_id": tenant_id,
            "project_id": project_id,
            "tray_id": tray_id,
            "elapsed_ms": int(elapsed_ms),
            "model_version": model_version,
            "status": status,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "recorded_at": datetime.now(timezone.utc),
        })
    except Exception as exc:
        # Timing collection is best-effort; never block the analyze call.
        log.warning("ai timing record failed (%s): %s", tray_id, exc)


def _percentile(sorted_vals: list[int], pct: float) -> int:
    """Linear-interpolation percentile over a pre-sorted int list.
    Returns 0 on empty input so the dashboard renders a clean zero
    rather than blowing up."""
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return int(sorted_vals[0])
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return int(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


async def get_call_timing_percentiles(
    *, tenant_id: str, days: int = 7,
) -> dict:
    """Roll up ai_call_timings into P50 / P95 / P99 over the window
    and split by model_version + status. Used by the cost dashboard."""
    if days < 1:
        days = 1
    if days > 90:
        days = 90
    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = db.ai_call_timings.find(
        {"tenant_id": tenant_id, "recorded_at": {"$gte": cutoff}},
        {"_id": 0, "elapsed_ms": 1, "model_version": 1, "status": 1},
    )
    overall: list[int] = []
    by_model: dict[str, list[int]] = {}
    by_status: dict[str, list[int]] = {}
    async for row in cursor:
        ms = int(row.get("elapsed_ms") or 0)
        if ms < 0:
            continue
        overall.append(ms)
        m = row.get("model_version") or "(unknown)"
        s = row.get("status") or "(unknown)"
        by_model.setdefault(m, []).append(ms)
        by_status.setdefault(s, []).append(ms)
    overall.sort()
    summary = {
        "window_days": days,
        "sample_size": len(overall),
        "p50_ms": _percentile(overall, 0.50),
        "p95_ms": _percentile(overall, 0.95),
        "p99_ms": _percentile(overall, 0.99),
        "min_ms": overall[0] if overall else 0,
        "max_ms": overall[-1] if overall else 0,
        "by_model": {
            m: {
                "n": len(v),
                "p50_ms": _percentile(sorted(v), 0.50),
                "p95_ms": _percentile(sorted(v), 0.95),
                "p99_ms": _percentile(sorted(v), 0.99),
            }
            for m, v in by_model.items()
        },
        "by_status": {
            s: {"n": len(v), "p50_ms": _percentile(sorted(v), 0.50)}
            for s, v in by_status.items()
        },
    }
    return summary


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
    """Dev mode = explicit PHOTO_AI_DEV_MODE=1 OR no Emergent gateway
    key OR gateway SDK import failed. Real Vision requires
    EMERGENT_LLM_KEY (federal-contractor compliance: no direct
    Anthropic Console account). When dev mode is on, _stub_analysis
    returns deterministic mock data without any network call.
    """
    if os.environ.get("PHOTO_AI_DEV_MODE") == "1":
        return True
    if not _GATEWAY_SDK_OK:
        return True
    return not os.environ.get("EMERGENT_LLM_KEY")


def _anthropic_api_key() -> Optional[str]:
    """Returns the Emergent LLM gateway key. Name preserved for
    back-compat with older callers; all Vision traffic now routes
    through Emergent's gateway, not api.anthropic.com directly.
    """
    return os.environ.get("EMERGENT_LLM_KEY") or None


# =====================================================================
# Pillow preprocessing
# =====================================================================

def _preprocess_image_for_vision(blob: bytes) -> tuple[bytes, str, dict]:
    """Resize to MAX_DIMENSION_PX (1568) preserving aspect, convert
    to RGB JPEG q85. Handles HEIC / PNG / WebP via Pillow. Returns
    (jpeg_bytes, media_type, meta) or raises a domain-specific
    exception class:
      ImageTooLargeError      input >20MB before preprocessing
      NotAnImageError         file doesn't open as an image
      ImageConversionError    Pillow can't convert (e.g. unknown HEIC variant)
    """
    if len(blob) > 20 * 1024 * 1024:
        raise ImageTooLargeError(
            f"input {len(blob)} bytes exceeds 20MB limit",
        )
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise ImageConversionError("Pillow not installed")
    try:
        img = Image.open(io.BytesIO(blob))
        # HEIC may require pillow-heif. If not registered, Pillow
        # raises UnidentifiedImageError which we surface clearly.
        img.load()
    except Exception as exc:
        raise NotAnImageError(f"unrecognized image format: {exc}") from exc
    try:
        # Strip EXIF orientation -> rotate so the analysis sees the
        # photo right-side up (mobile cameras tag rotation in EXIF
        # rather than reorient pixels).
        img = ImageOps.exif_transpose(img)
        # Convert palette / RGBA / HEIC -> RGB for JPEG output.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        # Cap longest side at MAX_DIMENSION_PX preserving aspect.
        w, h = img.size
        if max(w, h) > MAX_DIMENSION_PX:
            img.thumbnail((MAX_DIMENSION_PX, MAX_DIMENSION_PX),
                          Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        jpeg_bytes = out.getvalue()
        new_w, new_h = img.size
        return jpeg_bytes, "image/jpeg", {
            "width": new_w, "height": new_h,
            "size_bytes": len(jpeg_bytes),
            "original_size_bytes": len(blob),
        }
    except Exception as exc:
        raise ImageConversionError(f"pillow conversion failed: {exc}") from exc


class _PhotoAIError(Exception):
    """Base class for photo-AI domain errors. Used as a tag class so
    the analyze loop can distinguish from generic exceptions.

    Test 4A/B follow-up (2026-05-12): subclasses can carry a `subtype`
    attribute that the retry loop joins into the persisted
    ai_failure_reason as `code:subtype` (e.g. "vision_api_error:auth").
    Subtypes are surfaced in audit + the Photo AI dashboard so the
    operator can triage by failure class without re-reading logs.
    """
    reason_code = "photo_ai_error"

    def __init__(self, message: str = "", *, subtype: Optional[str] = None):
        super().__init__(message)
        self.subtype = subtype


class ImageTooLargeError(_PhotoAIError):
    reason_code = "image_too_large"


class NotAnImageError(_PhotoAIError):
    reason_code = "not_an_image"


class ImageConversionError(_PhotoAIError):
    reason_code = "image_conversion_failed"


class VisionApiError(_PhotoAIError):
    """Test 4A/B follow-up -- accepts a `subtype` keyword to narrow the
    failure class:
      - client_init      AsyncAnthropic constructor raised (signature
                          drift, missing dep, etc.)
      - auth             401 / invalid API key
      - rate_limit       429
      - bad_request      400 (bad message shape, oversized blob, etc.)
      - api_connection   network / DNS / TLS error
      - response_parse   couldn't extract text from response object
      - usage_parse      couldn't extract token counts
      - unexpected_api   anything else from the SDK
      - sdk_unavailable  anthropic package not importable
    """
    reason_code = "vision_api_error"


class VisionTimeoutError(_PhotoAIError):
    reason_code = "vision_timeout"


class VisionMalformedJsonError(_PhotoAIError):
    reason_code = "vision_malformed_json"


class StorageUnavailableError(_PhotoAIError):
    reason_code = "storage_unavailable"


class PersistFailedError(_PhotoAIError):
    """Test 4A/B follow-up -- distinguishes "AI call succeeded but DB
    write failed" from "API itself failed". Operators can re-run the
    persist without re-billing the API call."""
    reason_code = "persist_failed"


# =====================================================================
# Anthropic Vision call
# =====================================================================

# Prompt asks Claude to return ONLY a JSON object with the fields the
# tray row needs. Conservative confidence guidance + safety-concerns
# list. Keep the prompt tight to keep token count (and cost) low.
_VISION_SYSTEM_PROMPT = """You are analyzing a construction site photo for a federal-contracting
project management platform. Return ONLY a JSON object (no preface,
no markdown fences) with these exact fields:

{
  "photo_class": one of "progress" | "discovery" | "damage" | "quality" | "other",
  "caption": <one-sentence description, <=200 chars, neutral tone>,
  "suggested_event_type": one of "delay" | "rfi" | "qc_issue" | "safety_event" | "coordination_event" | "delivery" | "none",
  "suggested_trade": <trade name in lowercase or null; e.g., "plumber", "electrician", "concrete", "framing", "drywall", "general">,
  "confidence": <float 0.0-1.0; how sure are you. Be conservative: <0.7 means uncertain>,
  "safety_concerns": <list of short strings; e.g., ["missing fall protection", "exposed rebar"]; empty list if none>
}

Definitions:
- progress: routine work-in-progress documentation
- discovery: previously unknown condition (concealed/differing site)
- damage: damage to existing work or property
- quality: defect, non-conforming work, or QC observation
- other: doesn't fit the above (e.g. mobilization, weather, deliveries)

Be conservative. If you can't tell, lower the confidence below 0.5.
Don't invent details you don't see in the image."""


_JSON_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_vision_json(text: str) -> dict:
    """Strip optional ```json fences, parse, validate shape. Raises
    VisionMalformedJsonError on any failure."""
    if not text:
        raise VisionMalformedJsonError("empty response")
    s = text.strip()
    m = _JSON_CODE_FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    try:
        data = json.loads(s)
    except Exception as exc:
        raise VisionMalformedJsonError(f"json.loads failed: {exc}") from exc
    if not isinstance(data, dict):
        raise VisionMalformedJsonError("response not a JSON object")
    # Clamp confidence + coerce types.
    try:
        conf = float(data.get("confidence") or 0.0)
    except Exception:
        conf = 0.0
    data["confidence"] = max(0.0, min(1.0, conf))
    # Allowed enums; map unknowns to safe defaults.
    cls = (data.get("photo_class") or "").strip().lower()
    if cls not in {"progress", "discovery", "damage", "quality", "other"}:
        cls = "other"
    data["photo_class"] = cls
    ev = (data.get("suggested_event_type") or "").strip().lower()
    if ev not in {"delay", "rfi", "qc_issue", "safety_event",
                   "coordination_event", "delivery", "none"}:
        ev = "none"
    data["suggested_event_type"] = ev
    # Strings must be strings; lists must be lists.
    cap = data.get("caption") or ""
    data["caption"] = str(cap)[:500]
    trade = data.get("suggested_trade")
    if trade is not None and not isinstance(trade, str):
        trade = None
    data["suggested_trade"] = trade
    concerns = data.get("safety_concerns") or []
    if not isinstance(concerns, list):
        concerns = []
    data["safety_concerns"] = [str(c)[:200] for c in concerns if c][:10]
    return data


async def _call_anthropic_vision(
    *, jpeg_bytes: bytes, media_type: str,
    model: str = DEFAULT_MODEL, timeout_seconds: float = 30.0,
) -> dict:
    """Single Vision call. Returns parsed dict + usage tokens. Raises
    VisionTimeoutError / VisionApiError / VisionMalformedJsonError so
    the retry loop can distinguish failure classes.

    Federal-contractor compliance refactor (2026-05-12): the underlying
    transport is now Emergent's LLM gateway (via emergentintegrations
    LlmChat) instead of direct api.anthropic.com. The 7-stage exception
    classification (sdk/client_init/payload_encode/api_error subtypes/
    response_parse/vision_malformed_json/usage_metadata) is preserved
    so audit-log triage flows are unchanged. Function name kept for
    back-compat with the retry loop and audit messages.
    """
    api_key = _anthropic_api_key()
    if not api_key:
        raise VisionApiError(
            "no EMERGENT_LLM_KEY configured", subtype="no_api_key",
        )

    # --- Stage 1: SDK import ----------------------------------------
    try:
        from emergentintegrations.llm.chat import (
            LlmChat, UserMessage, ImageContent,
        )
    except ImportError as exc:
        raise VisionApiError(
            f"emergentintegrations not installed: {exc}",
            subtype="sdk_unavailable",
        ) from exc
    except Exception as exc:
        raise VisionApiError(
            f"emergentintegrations import errored "
            f"({type(exc).__name__}): {exc}",
            subtype="sdk_unavailable",
        ) from exc

    # --- Stage 2: client init ---------------------------------------
    # LlmChat does not currently accept a timeout kwarg; we wrap the
    # send_message call in asyncio.wait_for below to enforce
    # timeout_seconds without dropping the existing retry contract.
    try:
        # A unique session_id per call keeps photo-AI analyses
        # stateless -- we don't want a previous photo's context
        # poisoning the next one's classification.
        session_id = f"photo-ai-{int(asyncio.get_event_loop().time()*1000)}"
        chat = LlmChat(
            api_key=api_key,
            session_id=session_id,
            system_message=_VISION_SYSTEM_PROMPT,
        ).with_model("anthropic", model)
    except TypeError as exc:
        raise VisionApiError(
            f"LlmChat constructor rejected kwargs "
            f"({type(exc).__name__}: {exc}). Likely SDK version drift "
            f"-- check `pip show emergentintegrations`.",
            subtype="client_init_failed",
        ) from exc
    except Exception as exc:
        raise VisionApiError(
            f"LlmChat init failed ({type(exc).__name__}): {exc}",
            subtype="client_init_failed",
        ) from exc

    # --- Stage 3: encode payload ------------------------------------
    try:
        b64 = base64.standard_b64encode(jpeg_bytes).decode("ascii")
        image_content = ImageContent(image_base64=b64)
        user_message = UserMessage(
            text="Analyze this photo. Return only the JSON object.",
            file_contents=[image_content],
        )
    except Exception as exc:
        raise VisionApiError(
            f"gateway payload encode failed ({type(exc).__name__}): {exc}",
            subtype="payload_encode_failed",
        ) from exc

    # --- Stage 4: API call ------------------------------------------
    try:
        resp_obj = await asyncio.wait_for(
            chat.send_message(user_message),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise VisionTimeoutError(
            f"timeout after {timeout_seconds}s", subtype="api_timeout",
        ) from exc
    except Exception as exc:
        # Classify by error string content. emergentintegrations passes
        # gateway HTTP errors through; we look for status-code hints
        # in the message to set a useful subtype.
        cls = type(exc).__name__
        msg_str = str(exc)[:200]
        msg = f"{cls}: {msg_str}"
        low = msg_str.lower()
        if "rate" in low and "limit" in low or "429" in msg_str:
            subtype = "rate_limit"
        elif ("auth" in low or "unauthorized" in low
              or "401" in msg_str or "api key" in low):
            subtype = "auth"
        elif "permission" in low or "403" in msg_str:
            subtype = "permission_denied"
        elif "bad request" in low or "400" in msg_str:
            subtype = "bad_request"
        elif "connection" in low or "network" in low:
            subtype = "api_connection"
        elif ("500" in msg_str or "502" in msg_str
              or "503" in msg_str or "504" in msg_str
              or "server error" in low or "internal" in low):
            subtype = "server_error"
        else:
            subtype = "unexpected_api"
        raise VisionApiError(msg, subtype=subtype) from exc

    # --- Stage 5: text extraction from gateway response -------------
    # emergentintegrations.send_message returns a string of the
    # assistant's text. Defensive cast in case a future SDK version
    # returns a richer object.
    try:
        if isinstance(resp_obj, str):
            text = resp_obj.strip()
        elif hasattr(resp_obj, "text"):
            text = str(getattr(resp_obj, "text") or "").strip()
        elif hasattr(resp_obj, "content"):
            text = str(getattr(resp_obj, "content") or "").strip()
        else:
            text = str(resp_obj).strip()
    except Exception as exc:
        raise VisionApiError(
            f"response text extraction failed "
            f"({type(exc).__name__}): {exc}",
            subtype="response_parse",
        ) from exc

    # --- Stage 6: JSON parse (raises VisionMalformedJsonError) ------
    data = _parse_vision_json(text)

    # --- Stage 7: usage / model metadata ----------------------------
    # emergentintegrations.send_message does not currently surface
    # per-call token usage as a structured object. Estimate from the
    # round-trip payload sizes so the cost dashboard at Settings ->
    # AI Usage still rolls up sensible numbers. (1 token ~= 4 chars
    # for prompt+response text; image tokens are baked into the
    # gateway's input billing and not separately reported.)
    try:
        # Approximate: prompt text + image is roughly fixed-overhead;
        # we use the SDK-reported model where available.
        resp_model = model
        if hasattr(resp_obj, "model"):
            resp_model = str(getattr(resp_obj, "model") or model)
        in_tok = 0
        out_tok = 0
        if hasattr(resp_obj, "usage"):
            usage = getattr(resp_obj, "usage")
            in_tok = int(getattr(usage, "input_tokens", 0) or 0)
            out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        # If gateway didn't surface usage, estimate from text lengths.
        if in_tok == 0 and out_tok == 0:
            # Image: ~1568x1568 JPEG ≈ 1600 image tokens for Sonnet
            # plus the system prompt + user text (~250 tokens).
            in_tok = 1850
            out_tok = max(1, len(text) // 4)
    except Exception as exc:
        log.warning(
            "photo_ai: usage parse failed (%s: %s); recording estimate",
            type(exc).__name__, exc,
        )
        in_tok = 1850
        out_tok = max(1, len(text) // 4) if text else 0
        resp_model = model

    return {
        "parsed": data, "raw_text": text,
        "input_tokens": in_tok, "output_tokens": out_tok,
        "model": str(resp_model),
    }


# =====================================================================
# Concurrent dedup lock ($setOnInsert pattern)
# =====================================================================
# To prevent two concurrent uploads with the same pHash from both
# making the API call, we attempt to claim a per-(tenant, phash)
# lock via insert into photo_ai_locks. Winner proceeds; loser gets
# duplicate-key error, falls back to "wait + dedup" path.
#
# TTL index on locked_at (60s) auto-cleans crashed analyses so the
# lock never permanently blocks a pHash. Created here so the service
# is self-contained; server.py _ensure_indexes() also declares it
# to bootstrap fresh deploys.

PHOTO_AI_LOCK_TTL_SECONDS = 60


async def _try_claim_phash_lock(
    *, tenant_id: str, phash: str, tray_id: str,
) -> bool:
    """Attempt to insert a lock row for (tenant_id, phash). Returns
    True on success (this caller is the winner), False on duplicate-
    key (another caller already locked). Uses upsert-via-insert so
    Mongo's unique index does the work atomically."""
    if not phash:
        return True   # no phash, no race to worry about
    db = get_db()
    now = datetime.now(timezone.utc)
    try:
        await db.photo_ai_locks.insert_one({
            "tenant_id": tenant_id,
            "phash": phash,
            "tray_id": tray_id,
            "locked_at": now,
        })
        return True
    except Exception as exc:
        # DuplicateKeyError signals the loser path. Other errors
        # (no connection, etc.) we treat as winner-by-default since
        # blocking the analysis would be worse than risking a double
        # call.
        if "DuplicateKey" in type(exc).__name__ or "duplicate key" in str(exc).lower():
            return False
        log.warning("phash lock claim error (proceeding as winner): %s", exc)
        return True


async def _release_phash_lock(
    *, tenant_id: str, phash: str,
) -> None:
    """Delete the lock row after analysis completes (or fails). Best-
    effort; if the delete fails the TTL index sweeps the row in 60s."""
    if not phash:
        return
    db = get_db()
    try:
        await db.photo_ai_locks.delete_one(
            {"tenant_id": tenant_id, "phash": phash},
        )
    except Exception:
        pass


async def _wait_and_dedup(
    *, tenant_id: str, phash: str, tray_id: str, max_wait_seconds: float = 15.0,
) -> Optional[dict]:
    """Loser path: poll find_dedup_source until the winner finishes
    or we time out. Returns the dedup source row or None."""
    if not phash:
        return None
    deadline = asyncio.get_event_loop().time() + max_wait_seconds
    while asyncio.get_event_loop().time() < deadline:
        src = await find_dedup_source(tenant_id=tenant_id, phash=phash)
        if src and src.get("id") != tray_id:
            return src
        await asyncio.sleep(1.0)
    return None


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


async def _persist_skipped(*, db, tray_id: str, reason: str,
                             now_iso: str) -> dict:
    """Helper -- stamp a tray row as skipped + return the update dict."""
    update = {
        "ai_analysis_status": "skipped",
        "ai_analysis_started_at": now_iso,
        "ai_analysis_completed_at": now_iso,
        "ai_failure_reason": reason,
        "ai_model_version": DEFAULT_MODEL,
    }
    await db.photo_tray.update_one({"id": tray_id}, {"$set": update})
    return update


async def _persist_budget_paused(*, db, tenant_id: str, tray_id: str,
                                    reason: str, now_iso: str,
                                    budget: dict) -> dict:
    update = {
        "ai_analysis_status": "budget_paused",
        "ai_analysis_started_at": now_iso,
        "ai_failure_reason": reason,
        "ai_model_version": DEFAULT_MODEL,
    }
    await db.photo_tray.update_one({"id": tray_id}, {"$set": update})
    # Once-per-month-per-tenant audit. The tenant row carries a
    # last-emitted-at marker so retries within the month don't spam
    # the audit log.
    try:
        tenant = await db.tenants.find_one(
            {"id": tenant_id},
            {"_id": 0, "ai_budget_audit_last_at": 1,
             "ai_current_month_resets_at": 1},
        )
        last_at = (tenant or {}).get("ai_budget_audit_last_at")
        reset_at = (tenant or {}).get("ai_current_month_resets_at")
        emit_audit = False
        if not last_at:
            emit_audit = True
        else:
            # Emit once per monthly window: if last_at < current month
            # reset boundary, we owe a fresh audit.
            try:
                last_dt = datetime.fromisoformat(
                    str(last_at).replace("Z", "+00:00"),
                )
                reset_dt = datetime.fromisoformat(
                    str(reset_at or "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"),
                )
                # reset_at is FIRST OF NEXT MONTH; "current month start"
                # is one month earlier. If last_at predates the prior
                # boundary we'd have emitted then, so just check vs
                # 30 days ago as a simple coarse window.
                if (datetime.now(timezone.utc) - last_dt).days > 25:
                    emit_audit = True
            except Exception:
                emit_audit = True
        if emit_audit:
            from audit import write_audit
            await write_audit(
                tenant_id=tenant_id, table_name="tenants",
                op="PHOTO_AI_BUDGET_REACHED",
                record_id=tenant_id, before=None,
                after={"current_spend_usd": budget.get("current_spend"),
                        "monthly_limit_usd": budget.get("monthly_limit"),
                        "first_paused_tray_id": tray_id,
                        "reason": reason},
                actor_id=None,
                request_id=f"photo_ai_budget_reached_{tenant_id}",
            )
            await db.tenants.update_one(
                {"id": tenant_id},
                {"$set": {"ai_budget_audit_last_at":
                            datetime.now(timezone.utc).isoformat()}},
            )
    except Exception:
        pass
    return update


async def _persist_dedup_reuse(*, db, tray_id: str, src: dict,
                                  now_iso: str) -> dict:
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
    await db.photo_tray.update_one({"id": tray_id}, {"$set": update})
    return update


async def analyze_photo(
    *, tenant_id: str, tray_id: str,
    actor_id: Optional[str] = None,
) -> dict:
    """Real Vision integration. Flow:
      1. Load tray row.
      2. Re-check CUI / AI gates (belt + suspenders -- upload also
         pre-checks before scheduling the BackgroundTask).
      3. Budget gate.
      4. pHash dedup -- if a completed row exists with the same
         phash, copy + zero cost.
      5. Concurrent-race lock via photo_ai_locks ($setOnInsert).
         Winner runs the Vision call; loser waits for the winner
         then dedups.
      6. Atomically mark status=analyzing (only if currently pending
         OR skipped -- guards against double-running a complete row).
      7. Read blob from storage -> Pillow preprocess -> base64.
      8. Call Anthropic Vision; retry once on transient failure.
      9. Persist complete + increment spend + audit
         PHOTO_AI_ANALYZED.

    Returns: {ok, status, reason?, fields?, source_tray_id?,
              error_class?}
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
    if tray.get("ai_analysis_status") == "complete":
        return {"ok": True, "status": "complete",
                "reason": "already_complete"}

    # 2. CUI / AI gate (re-checked at service entry).
    safety = await check_cui_safety(
        tenant_id=tenant_id, project_id=tray.get("project_id"),
    )
    if not safety.get("safe_to_analyze"):
        reason = safety.get("reason") or "ai_disabled"
        update = await _persist_skipped(
            db=db, tray_id=tray_id, reason=reason, now_iso=now_iso,
        )
        # CUI-specific audit so compliance reports can roll up blocked
        # attempts separately from generic "AI disabled" cases.
        if reason == "cui_blocked":
            try:
                from audit import write_audit
                await write_audit(
                    tenant_id=tenant_id, table_name="photo_tray",
                    op="PHOTO_AI_CUI_BLOCKED",
                    record_id=tray_id, before=None,
                    after={"project_id": tray.get("project_id"),
                            "tray_id": tray_id,
                            "actor_id": actor_id},
                    actor_id=actor_id,
                    request_id=f"photo_ai_cui_{tray_id}",
                )
            except Exception:
                pass
        return {"ok": False, "status": "skipped",
                "reason": reason, "fields": update}

    # 3. Budget gate.
    budget = await check_budget(tenant_id=tenant_id)
    if not budget.get("budget_ok"):
        update = await _persist_budget_paused(
            db=db, tenant_id=tenant_id, tray_id=tray_id,
            reason=budget.get("reason") or "budget_exceeded",
            now_iso=now_iso, budget=budget,
        )
        return {"ok": False, "status": "budget_paused",
                "reason": budget.get("reason"), "fields": update}

    # 4. pHash dedup pre-check (fast path; race-safe lock below).
    phash = tray.get("phash")
    if phash:
        src = await find_dedup_source(tenant_id=tenant_id, phash=phash)
        if src and src.get("id") != tray_id:
            update = await _persist_dedup_reuse(
                db=db, tray_id=tray_id, src=src, now_iso=now_iso,
            )
            return {"ok": True, "status": "complete",
                    "reason": "phash_reused",
                    "source_tray_id": src["id"], "fields": update}

    # 5. Concurrent-race lock. Loser path waits then dedups.
    won_lock = await _try_claim_phash_lock(
        tenant_id=tenant_id, phash=phash or "", tray_id=tray_id,
    )
    if not won_lock:
        # Loser: wait for winner.
        winner_src = await _wait_and_dedup(
            tenant_id=tenant_id, phash=phash, tray_id=tray_id,
        )
        if winner_src:
            update = await _persist_dedup_reuse(
                db=db, tray_id=tray_id, src=winner_src, now_iso=now_iso,
            )
            return {"ok": True, "status": "complete",
                    "reason": "phash_reused_after_race",
                    "source_tray_id": winner_src["id"], "fields": update}
        # Winner crashed or took too long -- claim the lock ourselves
        # (delete + re-insert).
        await _release_phash_lock(tenant_id=tenant_id, phash=phash)
        await _try_claim_phash_lock(
            tenant_id=tenant_id, phash=phash or "", tray_id=tray_id,
        )

    # 6. Atomically mark analyzing (only flip from pending / skipped /
    # failed / rejected_by_user; never overwrite complete or another
    # in-flight analyzing).
    res = await db.photo_tray.update_one(
        {"id": tray_id,
         "ai_analysis_status": {"$in": ["pending", "skipped", "failed",
                                          "rejected_by_user",
                                          "budget_paused"]}},
        {"$set": {"ai_analysis_status": "analyzing",
                   "ai_analysis_started_at": now_iso}},
    )
    # If matched_count == 0, another concurrent caller already flipped
    # to analyzing -- bail to avoid the double API call.
    if not res.matched_count:
        await _release_phash_lock(tenant_id=tenant_id, phash=phash)
        return {"ok": False, "status": tray.get("ai_analysis_status"),
                "reason": "another_caller_in_flight"}

    # 7-9. Vision call wrapped in retry. Returns the new tray state.
    try:
        result = await _run_vision_with_retry(
            tenant_id=tenant_id, tray=tray, actor_id=actor_id,
        )
    finally:
        await _release_phash_lock(tenant_id=tenant_id, phash=phash)
    return result


async def _run_vision_with_retry(
    *, tenant_id: str, tray: dict, actor_id: Optional[str],
) -> dict:
    """The actual Vision call + retry loop. Separated so analyze_
    photo can wrap it in the lock release try/finally."""
    import time
    db = get_db()
    tray_id = tray["id"]
    storage_key = tray.get("storage_key")
    # Bucket F Phase 5 -- measure end-to-end Vision-call wall time so
    # the dashboard can render P50/P95/P99. Start the clock at the
    # first storage read so storage + preprocess + API are all in the
    # number (which is what operators actually care about).
    t_start = time.monotonic()

    last_error: Optional[_PhotoAIError] = None
    last_reason: str = "unknown"

    for attempt in range(MAX_RETRIES + 1):
        try:
            # Re-check tray row exists (might have been deleted mid-flight).
            still_there = await db.photo_tray.find_one(
                {"id": tray_id, "tenant_id": tenant_id},
                {"_id": 0, "id": 1, "storage_key": 1},
            )
            if not still_there:
                return {"ok": False, "status": "skipped",
                        "reason": "tray_deleted_mid_flight"}

            # Storage read (Phase 5 -- routed through 60s cache).
            try:
                blob, _content_type = await cached_get_object(storage_key)
            except Exception as exc:
                raise StorageUnavailableError(
                    f"storage read failed: {exc}",
                ) from exc

            # Pillow preprocess.
            jpeg_bytes, media_type, _img_meta = _preprocess_image_for_vision(blob)

            # Dev mode: deterministic stub (no API key configured).
            if _is_dev_mode():
                stub = _stub_analysis(tray)
                in_tok = 0
                out_tok = 0
                parsed = {
                    "photo_class": stub["ai_suggested_class"],
                    "caption": stub["ai_suggested_caption"],
                    "suggested_event_type": stub["ai_suggested_event_type"],
                    "suggested_trade": stub["ai_suggested_trade"],
                    "confidence": stub["ai_confidence_score"],
                    "safety_concerns": stub["ai_safety_concerns_detected"],
                }
                model = stub["ai_model_version"]
                raw_text = json.dumps(parsed)
            else:
                v = await _call_anthropic_vision(
                    jpeg_bytes=jpeg_bytes, media_type=media_type,
                )
                parsed = v["parsed"]
                in_tok = v["input_tokens"]
                out_tok = v["output_tokens"]
                model = v["model"]
                raw_text = v["raw_text"]

            # Persist complete.
            finish_iso = datetime.now(timezone.utc).isoformat()
            update = {
                "ai_analysis_status": "complete",
                "ai_analysis_completed_at": finish_iso,
                "ai_suggested_class": parsed.get("photo_class"),
                "ai_suggested_caption": parsed.get("caption"),
                "ai_suggested_event_type": parsed.get("suggested_event_type"),
                "ai_suggested_trade": parsed.get("suggested_trade"),
                "ai_confidence_score": parsed.get("confidence"),
                "ai_safety_concerns_detected": parsed.get("safety_concerns") or [],
                "ai_raw_response": {"text": raw_text[:4000], "parsed": parsed},
                "ai_model_version": model,
                "ai_cost_tokens_input": in_tok,
                "ai_cost_tokens_output": out_tok,
                "ai_reused_from_phash": False,
                "ai_failure_reason": None,
            }
            await db.photo_tray.update_one(
                {"id": tray_id}, {"$set": update},
            )

            # Cost increment + audit.
            if in_tok or out_tok:
                await increment_spend(
                    tenant_id=tenant_id, input_tokens=in_tok, output_tokens=out_tok,
                )
            # Phase 5 -- record success timing for the P50/P95/P99
            # dashboard. Best-effort; never blocks the analyze path.
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            await record_call_timing(
                tenant_id=tenant_id,
                project_id=tray.get("project_id"),
                tray_id=tray_id, elapsed_ms=elapsed_ms,
                model_version=model, status="complete",
                input_tokens=in_tok, output_tokens=out_tok,
            )
            try:
                from audit import write_audit
                await write_audit(
                    tenant_id=tenant_id, table_name="photo_tray",
                    op="PHOTO_AI_ANALYZED",
                    record_id=tray_id, before=None,
                    after={"photo_class": parsed.get("photo_class"),
                            "confidence": parsed.get("confidence"),
                            "event_type": parsed.get("suggested_event_type"),
                            "trade": parsed.get("suggested_trade"),
                            "safety_concerns_count": len(parsed.get("safety_concerns") or []),
                            "model": model,
                            "input_tokens": in_tok,
                            "output_tokens": out_tok,
                            "elapsed_ms": elapsed_ms,
                            "attempt": attempt + 1},
                    actor_id=actor_id,
                    request_id=f"photo_ai_{tray_id}",
                )
            except Exception:
                pass
            return {"ok": True, "status": "complete", "fields": update,
                    "attempt": attempt + 1}

        except _PhotoAIError as exc:
            last_error = exc
            # Test 4A/B follow-up -- join the reason code with the
            # subtype (when present) so the persisted ai_failure_reason
            # reads "vision_api_error:auth" rather than just
            # "vision_api_error". Operators can triage from the audit
            # log without rebuilding context from server logs.
            sub = getattr(exc, "subtype", None)
            last_reason = f"{exc.reason_code}:{sub}" if sub else exc.reason_code
            log.warning("photo_ai attempt %d failed: %s reason=%s msg=%s",
                         attempt + 1, type(exc).__name__, last_reason, exc)
            # Hard failures don't retry.
            if isinstance(exc, (NotAnImageError, ImageTooLargeError,
                                 ImageConversionError)):
                break
            # Auth + permission failures don't retry either -- the key
            # isn't going to fix itself on the second attempt.
            if isinstance(exc, VisionApiError) and sub in (
                "auth", "permission_denied", "no_api_key",
                "sdk_unavailable", "client_init_failed",
            ):
                break
            # Storage / Vision / JSON failures retry once.
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            break
        except Exception as exc:
            # Test 4A/B follow-up -- this branch should now be
            # functionally unreachable because every stage in the loop
            # body raises a _PhotoAIError subclass. If we DO land here
            # it means a new code path was added without classifying
            # its failure -- the log.exception captures the full
            # traceback so the operator can wrap that path next.
            last_reason = f"unclassified:{type(exc).__name__}"
            log.exception(
                "photo_ai UNCLASSIFIED error (please wrap the responsible "
                "stage with a _PhotoAIError subclass): %s", exc,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            break

    # All retries exhausted -> mark failed.
    finish_iso = datetime.now(timezone.utc).isoformat()
    update = {
        "ai_analysis_status": "failed",
        "ai_analysis_completed_at": finish_iso,
        "ai_failure_reason": last_reason,
        "ai_model_version": DEFAULT_MODEL,
    }
    await db.photo_tray.update_one({"id": tray_id}, {"$set": update})
    # Phase 5 -- record failure timing too; the dashboard splits by
    # status so operators can see "complete vs. failed" latency
    # separately (failures usually finish fast, but timeouts pin to
    # the 30s ceiling).
    failure_elapsed_ms = int((time.monotonic() - t_start) * 1000)
    await record_call_timing(
        tenant_id=tenant_id,
        project_id=tray.get("project_id"),
        tray_id=tray_id, elapsed_ms=failure_elapsed_ms,
        model_version=DEFAULT_MODEL, status="failed",
    )
    try:
        from audit import write_audit
        await write_audit(
            tenant_id=tenant_id, table_name="photo_tray",
            op="PHOTO_AI_ANALYSIS_FAILED",
            record_id=tray_id, before=None,
            after={"reason": last_reason, "tray_id": tray_id,
                    "elapsed_ms": failure_elapsed_ms,
                    "attempts": MAX_RETRIES + 1},
            actor_id=actor_id,
            request_id=f"photo_ai_failed_{tray_id}",
        )
    except Exception:
        pass
    return {"ok": False, "status": "failed",
            "reason": last_reason, "fields": update,
            "verbose": verbose_failure_reason(last_reason)}


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
    reason set. Phase 2 audits this op.

    Bucket F Phase 4 -- rejections also feed the calibration corpus
    so future fine-tuning can learn from "AI said X, user rejected
    because Y" pairs. Same shape as the override write so the corpus
    is uniform: original_ai (the suggestion) + user_correction (None
    on reject) + reason.
    """
    if not reason or len(reason.strip()) < 10:
        return {"ok": False, "reason": "reason_required_min_10_chars"}
    db = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    # Snapshot AI fields BEFORE the status flip so the calibration
    # row captures what was actually rejected.
    tray = await db.photo_tray.find_one(
        tenant_filter(tenant_id, {"id": tray_id}),
        {"_id": 0, "id": 1, "project_id": 1,
         "ai_suggested_class": 1, "ai_suggested_caption": 1,
         "ai_suggested_event_type": 1, "ai_suggested_trade": 1,
         "ai_confidence_score": 1, "ai_safety_concerns_detected": 1},
    )
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
    # Calibration write -- mirror the override path. user_correction
    # is None to distinguish "rejected everything" from "overrode field X".
    try:
        await db.photo_ai_calibration.insert_one({
            "id": _gen_id(),
            "tenant_id": tenant_id,
            "tray_id": tray_id,
            "project_id": (tray or {}).get("project_id"),
            "kind": "reject",
            "original_ai": {
                "photo_class": (tray or {}).get("ai_suggested_class"),
                "caption": (tray or {}).get("ai_suggested_caption"),
                "event_type": (tray or {}).get("ai_suggested_event_type"),
                "trade": (tray or {}).get("ai_suggested_trade"),
                "confidence": (tray or {}).get("ai_confidence_score"),
                "safety_concerns":
                    list((tray or {}).get("ai_safety_concerns_detected") or []),
            },
            "user_correction": None,
            "reason": reason.strip(),
            "actor_id": actor_id,
            "created_at": now_iso,
        })
    except Exception:
        # Calibration is best-effort; reject still succeeds.
        pass
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


async def sweep_stuck_pending(
    *, tenant_id: Optional[str] = None,
    older_than_minutes: int = 5, limit: int = 100,
) -> dict:
    """Bucket F Phase 3 -- re-queue tray rows that have been stuck in
    `pending` longer than `older_than_minutes` (default 5).

    Without this, a worker hiccup or a BackgroundTask that exits
    before the analysis lands leaves photos invisibly stuck. Run this
    via the lifespan-task on startup + admin endpoint.

    Strategy: walk pending rows with uploaded_at < cutoff, reset
    ai_analysis_started_at to None + fire analyze_photo() for each.
    Limit caps the sweep size per call so a 1000+ tray backlog
    doesn't choke the worker. Returns counts per outcome.
    """
    db = get_db()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=max(1, older_than_minutes))).isoformat()
    q: dict = {"ai_analysis_status": "pending",
                "uploaded_at": {"$lt": cutoff}}
    if tenant_id:
        q["tenant_id"] = tenant_id
    rows = await db.photo_tray.find(
        q, {"_id": 0, "id": 1, "tenant_id": 1},
    ).limit(max(1, min(int(limit), 500))).to_list(length=None)
    out = {"swept": len(rows), "complete": 0, "skipped": 0,
            "failed": 0, "budget_paused": 0, "tenants_touched": 0}
    seen_tenants: set = set()
    for r in rows:
        tid = r.get("tenant_id")
        if not tid:
            continue
        seen_tenants.add(tid)
        try:
            res = await analyze_photo(
                tenant_id=tid, tray_id=r["id"], actor_id=None,
            )
            s = res.get("status")
            if s == "complete":
                out["complete"] += 1
            elif s == "skipped":
                out["skipped"] += 1
            elif s == "budget_paused":
                out["budget_paused"] += 1
            elif s == "failed":
                out["failed"] += 1
        except Exception as exc:
            log.warning("sweeper failed on %s: %s", r.get("id"), exc)
            out["failed"] += 1
        await asyncio.sleep(BATCH_THROTTLE_MS / 1000.0)
    out["tenants_touched"] = len(seen_tenants)
    return out


async def get_analysis_stats(
    *, tenant_id: str, project_id: Optional[str] = None, days: int = 30,
    bypass_cache: bool = False,
) -> dict:
    """Per-project (or tenant-wide) rollup over a recent window.
    Returns the shape the Photos page stats card needs in Phase 3.

    Bucket F Phase 5 -- 5min in-process cache. Stats are expensive on
    large tenants (full-tray scan per call); the Photos page card
    only changes meaningfully on the timescale of new uploads, so a
    5min TTL is the right trade-off. bypass_cache=True forces a fresh
    compute (used by the dashboard refresh button)."""
    import time
    if days < 1:
        days = 1
    if days > 365:
        days = 365
    cache_key = f"{tenant_id}|{project_id or ''}|{days}"
    now_mono = time.monotonic()
    if not bypass_cache:
        cached = _stats_cache.get(cache_key)
        if cached and cached[0] > now_mono:
            return {**cached[1], "_cache_hit": True}
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
    result = {
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
        "_cache_hit": False,
    }
    # Cache + bounded eviction.
    _stats_cache[cache_key] = (
        now_mono + STATS_CACHE_TTL_SECONDS, result,
    )
    if len(_stats_cache) > _stats_cache_max:
        oldest = sorted(_stats_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in oldest[: max(1, len(oldest) // 2)]:
            _stats_cache.pop(k, None)
    return result


# =====================================================================
# Bucket F Phase 5 -- cost monitoring + CUI compliance
# =====================================================================

async def get_cost_dashboard(
    *, tenant_id: str, days: int = 30,
) -> dict:
    """Tenant-wide cost rollup for Settings > AI Usage. Returns:
      - per-day spend buckets (date, input_tokens, output_tokens,
        usd, analyses)
      - top-10 most expensive trays (id, project_id, model, usd)
      - split by model_version (for migration windows when a model
        bumps mid-month)
      - current budget posture + warning/pause signals
    """
    if days < 1:
        days = 1
    if days > 365:
        days = 365
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await db.photo_tray.find(
        {"tenant_id": tenant_id,
         "uploaded_at": {"$gte": cutoff},
         "ai_analysis_status": "complete"},
        {"_id": 0, "id": 1, "project_id": 1,
         "ai_analysis_completed_at": 1,
         "ai_model_version": 1,
         "ai_cost_tokens_input": 1,
         "ai_cost_tokens_output": 1,
         "ai_reused_from_phash": 1},
    ).to_list(length=None)

    per_day: dict[str, dict] = {}
    per_model: dict[str, dict] = {}
    top_cost: list[dict] = []
    reused = 0
    total_in = 0
    total_out = 0
    for r in rows:
        in_tok = int(r.get("ai_cost_tokens_input") or 0)
        out_tok = int(r.get("ai_cost_tokens_output") or 0)
        if r.get("ai_reused_from_phash"):
            reused += 1
        usd = (in_tok * COST_PER_INPUT_TOKEN_USD
               + out_tok * COST_PER_OUTPUT_TOKEN_USD)
        total_in += in_tok
        total_out += out_tok
        day = (r.get("ai_analysis_completed_at") or "")[:10] or "unknown"
        b = per_day.setdefault(day, {
            "date": day, "input_tokens": 0, "output_tokens": 0,
            "usd": 0.0, "analyses": 0,
        })
        b["input_tokens"] += in_tok
        b["output_tokens"] += out_tok
        b["usd"] += usd
        b["analyses"] += 1
        model = r.get("ai_model_version") or "(unknown)"
        m = per_model.setdefault(model, {
            "model": model, "analyses": 0,
            "input_tokens": 0, "output_tokens": 0, "usd": 0.0,
        })
        m["analyses"] += 1
        m["input_tokens"] += in_tok
        m["output_tokens"] += out_tok
        m["usd"] += usd
        top_cost.append({
            "tray_id": r.get("id"),
            "project_id": r.get("project_id"),
            "model": model,
            "usd": round(usd, 6),
            "completed_at": r.get("ai_analysis_completed_at"),
        })

    # Round + sort.
    per_day_list = sorted(
        [{**v, "usd": round(v["usd"], 4)} for v in per_day.values()],
        key=lambda x: x["date"],
    )
    per_model_list = sorted(
        [{**v, "usd": round(v["usd"], 4)} for v in per_model.values()],
        key=lambda x: -x["usd"],
    )
    top_cost.sort(key=lambda x: -x["usd"])
    top_cost = top_cost[:10]

    # Live budget posture for the dashboard header.
    budget = await check_budget(tenant_id=tenant_id)
    total_usd = (total_in * COST_PER_INPUT_TOKEN_USD
                 + total_out * COST_PER_OUTPUT_TOKEN_USD)
    return {
        "tenant_id": tenant_id,
        "window_days": days,
        "analyses_complete": len(rows),
        "phash_reused_count": reused,
        "input_tokens_total": total_in,
        "output_tokens_total": total_out,
        "estimated_cost_usd": round(total_usd, 4),
        "per_day": per_day_list,
        "per_model": per_model_list,
        "top_cost_trays": top_cost,
        "budget": budget,
    }


async def get_cui_compliance_report(
    *, tenant_id: str, days: int = 90,
) -> dict:
    """CUI compliance audit roll-up. Counts photo-tray AI activity by
    project CUI status over the window + lists every PHOTO_AI_CUI_
    BLOCKED audit op for proof of enforcement. Federal auditors ask
    for this; the report is generated on-demand from
    audit_log + photo_tray data.

    Returns the shape an admin can export as evidence:
      - tenant info
      - projects flagged is_cui=true
      - tray counts per (project, ai_analysis_status) -- CUI projects
        should have everything in skipped(cui_blocked) or skipped(
        pre_ai_upload); a single complete row on a CUI project is a
        compliance flag.
      - audit_log entries for PHOTO_AI_CUI_BLOCKED in the window
      - generated_at timestamp + range
    """
    if days < 1:
        days = 1
    if days > 365:
        days = 365
    db = get_db()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()

    tenant = await db.tenants.find_one(
        {"id": tenant_id},
        {"_id": 0, "legal_name": 1, "display_name": 1,
         "cmmc_level": 1, "data_region": 1, "ai_enabled": 1},
    ) or {}

    cui_projects = await db.projects.find(
        tenant_filter(tenant_id, {"is_cui": True}),
        {"_id": 0, "id": 1, "name": 1, "agency": 1,
         "contract_number": 1, "ai_enabled": 1},
    ).to_list(length=None)
    cui_ids = [p["id"] for p in cui_projects]

    # Per-CUI-project tray status rollup. Anything that's NOT
    # skipped(cui_blocked) / skipped(pre_ai_upload) / pending is a
    # compliance flag (means the photo got past the CUI gate somehow).
    flags: list[dict] = []
    by_project_status: dict[str, dict[str, int]] = {}
    if cui_ids:
        rows = await db.photo_tray.find(
            {"tenant_id": tenant_id, "project_id": {"$in": cui_ids},
             "uploaded_at": {"$gte": cutoff}},
            {"_id": 0, "id": 1, "project_id": 1,
             "ai_analysis_status": 1, "ai_failure_reason": 1,
             "uploaded_at": 1},
        ).to_list(length=None)
        for r in rows:
            pid = r.get("project_id")
            s = r.get("ai_analysis_status") or "pending"
            by_project_status.setdefault(pid, {}).setdefault(s, 0)
            by_project_status[pid][s] += 1
            if s == "complete":
                # CUI photo that got analyzed -- compliance flag.
                flags.append({
                    "tray_id": r.get("id"),
                    "project_id": pid,
                    "status": s,
                    "uploaded_at": r.get("uploaded_at"),
                    "severity": "violation",
                })
            elif s in ("failed", "rejected_by_user", "budget_paused"):
                # Edge: gate fired AFTER status flipped to analyzing.
                # We can't always tell from the row alone; surface for
                # manual review.
                if r.get("ai_failure_reason") != "cui_blocked":
                    flags.append({
                        "tray_id": r.get("id"),
                        "project_id": pid,
                        "status": s,
                        "reason": r.get("ai_failure_reason"),
                        "uploaded_at": r.get("uploaded_at"),
                        "severity": "review",
                    })

    # CUI-block audit entries (proof of enforcement).
    blocked_entries = await db.audit_log.find(
        {"tenant_id": tenant_id,
         "op": "PHOTO_AI_CUI_BLOCKED",
         "created_at": {"$gte": cutoff}},
        {"_id": 0, "id": 1, "created_at": 1, "table_name": 1,
         "record_id": 1, "actor_id": 1, "hash": 1, "after": 1},
    ).sort("created_at", -1).limit(500).to_list(length=None)

    # Non-CUI project AI activity counts (denominator for the report).
    non_cui_count = await db.photo_tray.count_documents(
        {"tenant_id": tenant_id, "project_id": {"$nin": cui_ids},
         "uploaded_at": {"$gte": cutoff},
         "ai_analysis_status": "complete"},
    )

    return {
        "tenant_id": tenant_id,
        "tenant": tenant,
        "window_days": days,
        "generated_at": now.isoformat(),
        "cui_projects": cui_projects,
        "cui_project_count": len(cui_projects),
        "cui_tray_status_rollup": by_project_status,
        "compliance_flags": flags,
        "compliance_flag_count": len(flags),
        "cui_blocks_enforced_count": len(blocked_entries),
        "cui_block_audit_entries": blocked_entries[:50],
        "non_cui_completes_in_window": non_cui_count,
        # A clean report = zero violation-severity flags.
        "is_clean": all(f.get("severity") != "violation" for f in flags),
    }
