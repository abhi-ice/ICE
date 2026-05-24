"""ICE Schedule Ops — FastAPI entrypoint.

SUBSTITUTION NOTES (master prompt §3 / §29 honored where possible on
Emergent runtime):
- PostgreSQL/pgvector → MongoDB; tenant isolation enforced at app layer
  via tenant_filter() in every query.
- Celery + Redis → FastAPI BackgroundTasks (heavy jobs deferred).
- LangGraph → emergentintegrations.LlmChat with multi-step prompt chains
  (see services/ai_agents.py).
- Clerk → JWT (HS256) with bcrypt; X-Tenant-ID header for tenant switch.
- DOCKER COMPOSE & MinIO/S3 → not used; supervisor manages backend on
  0.0.0.0:8001 per Emergent platform constraints.
"""
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# === Anthropic direct-SDK shim (2026-05-18) =========================
# Customer uses their own ANTHROPIC_API_KEY (set in .env) rather than
# routing Claude through Emergent's gateway. We register a compat
# module BEFORE any router imports emergentintegrations.llm.chat, so
# all existing `from emergentintegrations.llm.chat import ...` lines
# resolve to our shim and call api.anthropic.com directly.
# Whisper STT (.llm.openai) and Stripe (.payments.stripe) keep using
# EMERGENT_LLM_KEY -- they are NOT intercepted here.
import sys  # noqa: E402
if os.environ.get("ANTHROPIC_API_KEY"):
    try:
        from services import anthropic_compat as _anthropic_compat  # noqa: E402
        sys.modules["emergentintegrations.llm.chat"] = _anthropic_compat
        logging.getLogger("ice_schedule_ops").info(
            "Anthropic direct-SDK shim active: emergentintegrations.llm.chat "
            "-> services.anthropic_compat (ANTHROPIC_API_KEY)."
        )
    except Exception as _exc:  # pragma: no cover -- non-fatal boot
        logging.getLogger("ice_schedule_ops").warning(
            "Anthropic direct-SDK shim FAILED to load (%s); falling back "
            "to Emergent gateway for Claude calls.", _exc,
        )
# ====================================================================

from database import get_db, close_db  # noqa: E402
from auth import require_tenant  # noqa: E402
from routers.auth_router import router as auth_router, team_router  # noqa: E402
from routers.projects_router import router as projects_router  # noqa: E402
from routers.tasks_router import router as tasks_router  # noqa: E402
from routers.daily_reports_router import router as daily_reports_router  # noqa: E402
from routers.dr_voice_router import router as dr_voice_router  # noqa: E402
from routers.resources_router import router as resources_router  # noqa: E402
from routers.field_events_router import router as field_events_router  # noqa: E402
from routers.docs_router import router as docs_router  # noqa: E402
from routers.system_router import router as system_router  # noqa: E402
from routers.voice_router import router as voice_router  # noqa: E402
from routers.upload_router import router as upload_router  # noqa: E402
from routers.iter3_router import router as iter3_router  # noqa: E402
from routers.iter4_router import router as iter4_router  # noqa: E402
from routers.iter5_router import router as iter5_router  # noqa: E402
from routers.iter6_router import router as iter6_router  # noqa: E402
from routers.iter7_router import router as iter7_router  # noqa: E402
from routers.iter8_router import router as iter8_router  # noqa: E402
from routers.schedule_proposals_router import router as schedule_proposals_router  # noqa: E402
from routers.schedule_diffs_router import router as schedule_diffs_router  # noqa: E402
from routers.calendar_reconciliation_router import router as calendar_reconciliation_router  # noqa: E402
from routers.tia_router import router as tia_router  # noqa: E402
from routers.photo_tray_router import router as photo_tray_router  # noqa: E402
from routers.mitigations_router import router as mitigations_router  # noqa: E402
from routers.gating_entities_router import router as gating_entities_router  # noqa: E402
from routers.coordination_router import router as coordination_router  # noqa: E402
from routers.edit_requests_router import router as edit_requests_router  # noqa: E402
from routers.resource_leveling_router import router as resource_leveling_router  # noqa: E402
from routers.project_members_router import router as project_members_router  # noqa: E402
from routers.field_suggestions_router import router as field_suggestions_router  # noqa: E402
from routers.submittal_sla_router import router as submittal_sla_router  # noqa: E402
from routers.recovery_reports_router import router as recovery_reports_router  # noqa: E402
from routers.exhibit_exports_router import router as exhibit_exports_router  # noqa: E402
from routers.ws_router import router as ws_router  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ice_schedule_ops")


async def _ensure_indexes():
    db = get_db()
    # tenant-scoped indexes for hot paths
    await db.users.create_index("email", unique=True)
    await db.tenants.create_index("slug", unique=True)
    await db.tenant_users.create_index([("user_id", 1), ("tenant_id", 1)], unique=True)
    await db.projects.create_index([("tenant_id", 1), ("status", 1)])
    await db.tasks.create_index([("tenant_id", 1), ("project_id", 1), ("activity_code", 1)])
    await db.tasks.create_index([("tenant_id", 1), ("project_id", 1), ("planned_start", 1)])
    await db.daily_reports.create_index([("tenant_id", 1), ("project_id", 1), ("report_date", 1)], unique=True)
    await db.daily_report_activities.create_index([("daily_report_id", 1), ("position", 1)])
    await db.audit_log.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.approvals.create_index([("tenant_id", 1), ("status", 1)])
    await db.locations.create_index([("tenant_id", 1), ("project_id", 1), ("floor_id", 1)])
    await db.crews.create_index([("tenant_id", 1), ("crew_code", 1)], unique=True)
    await db.equipment.create_index([("tenant_id", 1), ("asset_tag", 1)], unique=True)
    await db.rfis.create_index([("tenant_id", 1), ("project_id", 1), ("rfi_number", 1)])
    await db.work_items.create_index([("tenant_id", 1), ("project_id", 1), ("status", 1)])
    await db.work_item_links.create_index([("entity_type", 1), ("entity_id", 1)])
    await db.copilot_messages.create_index([("tenant_id", 1), ("user_id", 1), ("session_id", 1), ("created_at", 1)])
    await db.team_invites.create_index([("code", 1)], unique=True)
    await db.team_invites.create_index([("tenant_id", 1), ("status", 1)])
    await db.daily_presence.create_index([("tenant_id", 1), ("daily_report_id", 1)])
    # Bucket C2 — mitigation calibration RAG queries by (tenant_id,
    # outcome_recorded_at desc); detail page queries by linked_delay_id.
    await db.mitigations.create_index([("tenant_id", 1), ("outcome_recorded_at", -1)])
    await db.mitigations.create_index([("tenant_id", 1), ("linked_delay_id", 1)])
    # Bucket C3a — gating entities (Phase 2 dropdown queries by gates_task_ids;
    # detail page by id; status/over-SLA dashboards by (tenant, project, status))
    # plus the entity-type back-pointer for sync_status lookups.
    await db.gating_entities.create_index([("tenant_id", 1), ("project_id", 1), ("status", 1)])
    await db.gating_entities.create_index([("tenant_id", 1), ("entity_type", 1), ("entity_id", 1)])
    await db.gating_entities.create_index([("tenant_id", 1), ("gates_task_ids", 1)])
    # Project working-day calendar — keyed by (tenant, project, date) for
    # working-day math walks.
    await db.project_calendar_days.create_index(
        [("tenant_id", 1), ("project_id", 1), ("calendar_date", 1)],
        unique=True,
    )
    # Bucket C3b — coordination event log. List page sorts by occurred_at;
    # filters frequently by topic; backlinks to a daily report.
    await db.coordination_events.create_index(
        [("tenant_id", 1), ("project_id", 1), ("occurred_at", -1)],
    )
    await db.coordination_events.create_index(
        [("tenant_id", 1), ("project_id", 1), ("topic", 1)],
    )
    await db.coordination_events.create_index(
        [("tenant_id", 1), ("project_id", 1), ("linked_daily_report_id", 1)],
    )
    # Bucket C3b Phase 3 — daily_presence granular layer.
    # Existing (tenant, daily_report_id) index is kept; these add the
    # project + zone/shift + trade indexes for cross-DR aggregations.
    await db.daily_presence.create_index([("tenant_id", 1), ("project_id", 1), ("daily_report_id", 1)])
    await db.daily_presence.create_index([("tenant_id", 1), ("project_id", 1), ("zone_id", 1), ("shift", 1)])
    await db.daily_presence.create_index([("tenant_id", 1), ("project_id", 1), ("trade", 1)])
    # Bucket C3b Phase 5 — photo evidentiary indexes.
    # sha256_hash uniqueness check on upload (sparse so legacy photos
    # without a hash don't collide with each other on null).
    await db.photos.create_index(
        [("tenant_id", 1), ("sha256_hash", 1)],
        partialFilterExpression={"sha256_hash": {"$type": "string"}},
    )
    # Evidentiary-only lookups + class filter on the Photos page.
    await db.photos.create_index([("tenant_id", 1), ("project_id", 1), ("evidentiary_flag", 1)])
    await db.photos.create_index([("tenant_id", 1), ("project_id", 1), ("photo_class", 1)])
    # Bucket C3c Phase 1 — schedule_update_diffs. List page sorts by
    # computed_at desc per project; idempotency check uses the version
    # pair index.
    await db.schedule_update_diffs.create_index(
        [("tenant_id", 1), ("project_id", 1), ("computed_at", -1)],
    )
    await db.schedule_update_diffs.create_index(
        [("tenant_id", 1), ("earlier_schedule_version_id", 1),
         ("later_schedule_version_id", 1)],
        unique=True,
    )
    # Bucket C3c Phase 2 — cascade_snapshots. Dashboard query is "latest
    # snapshot per gating_entity in this project"; history endpoint
    # queries "all snapshots for one entity sorted by computed_at desc".
    await db.cascade_snapshots.create_index(
        [("tenant_id", 1), ("project_id", 1), ("gating_entity_id", 1),
         ("computed_at", -1)],
    )
    await db.cascade_snapshots.create_index(
        [("tenant_id", 1), ("gating_entity_id", 1), ("computed_at", -1)],
    )
    # Bucket C3c Phase 4 — tia_generations. List page query is by
    # (tenant, project, generated_at desc); detail by (tenant, id).
    await db.tia_generations.create_index(
        [("tenant_id", 1), ("project_id", 1), ("generated_at", -1)],
    )
    # Bucket D Phase 1 — photo_tray (staging area). Tray page filters
    # by status; the dedup check on upload looks up phash within a
    # project; tagging-wizard reads by id.
    await db.photo_tray.create_index(
        [("tenant_id", 1), ("project_id", 1), ("status", 1), ("uploaded_at", -1)],
    )
    await db.photo_tray.create_index(
        [("tenant_id", 1), ("project_id", 1), ("phash", 1)],
        partialFilterExpression={"phash": {"$type": "string"}},
    )
    # Photos (now tagging junction) — gallery filter by event-ref FKs.
    # Each is partial so legacy untagged rows don't pollute the index.
    for ref in ("daily_report_activity_id", "delay_id", "rfi_id",
                 "qc_issue_id", "safety_event_id",
                 "coordination_event_id", "delivery_id"):
        await db.photos.create_index(
            [("tenant_id", 1), ("project_id", 1), (ref, 1)],
            partialFilterExpression={ref: {"$type": "string"}},
        )
    await db.photos.create_index(
        [("tenant_id", 1), ("tray_photo_id", 1)],
        partialFilterExpression={"tray_photo_id": {"$type": "string"}},
    )
    # Supplement queries on each event collection -- lets the parent
    # DR detail view efficiently load its supplements.
    for coll in ("delays", "rfis", "qc_issues", "safety_events",
                  "coordination_events", "deliveries", "photos"):
        await db[coll].create_index(
            [("tenant_id", 1), ("parent_daily_report_id", 1)],
            partialFilterExpression={"parent_daily_report_id": {"$type": "string"}},
        )
    # daily_report_activities rows don't carry tenant_id (they're
    # scoped via daily_report_id). Index by parent_daily_report_id only.
    await db.daily_report_activities.create_index(
        [("parent_daily_report_id", 1)],
        partialFilterExpression={"parent_daily_report_id": {"$type": "string"}},
    )
    # Bucket D Corrective (2026-05-08) -- linked_task_ids gallery filter.
    # Each event collection gets the master-schedule-scoped index for
    # "what events affect task X?" lookups. Old per-DR-scope indexes on
    # linked_dr_activity_ids are dropped by migration 007.
    for coll in ("delays", "rfis", "qc_issues", "safety_events",
                  "coordination_events", "deliveries"):
        await db[coll].create_index(
            [("tenant_id", 1), ("project_id", 1), ("linked_task_ids", 1)],
        )

    # Bucket H Phase 5 (2026-05-08) -- resource-leveling read indexes.
    # Per spec Section 5.2:
    #   crew_capacity (tenant_id, trade, date)        -> per-trade supply rollup
    #   crew_capacity (tenant_id, crew_id, date)      -> per-crew calendar
    #   crew_demands  (tenant_id, trade, date)        -> per-trade demand rollup
    #   crew_demands  (tenant_id, project_id, date)   -> per-project demand profile
    # All four cover the read paths in services/resource_leveling.py
    # without forcing a collection scan. createIndex is idempotent so
    # re-running this on a deploy where indexes already exist is a
    # no-op. project_members indexes are created by migration 009
    # (kept there to keep "schema setup" with the table backfill).
    await db.crew_capacity.create_index(
        [("tenant_id", 1), ("trade", 1), ("date", 1)],
        name="cc_tenant_trade_date",
    )
    await db.crew_capacity.create_index(
        [("tenant_id", 1), ("crew_id", 1), ("date", 1)],
        name="cc_tenant_crew_date",
    )
    await db.crew_demands.create_index(
        [("tenant_id", 1), ("trade", 1), ("date", 1)],
        name="cd_tenant_trade_date",
    )
    await db.crew_demands.create_index(
        [("tenant_id", 1), ("project_id", 1), ("date", 1)],
        name="cd_tenant_project_date",
    )

    # Bucket F Phase 1 (2026-05-08) -- Photo AI read indexes.
    # Per spec:
    #   photo_tray (tenant_id, ai_analysis_status, uploaded_at)
    #     -> batch_analyze_pending walks pending rows in upload order
    #   photo_tray (tenant_id, ai_suggested_class)
    #     -> Photos page filter "show only AI-classified discovery"
    #   photo_tray (tenant_id, phash, ai_analysis_status)
    #     -> find_dedup_source: O(1) lookup of completed rows with
    #        matching pHash. Avoids a full-tray scan on every upload.
    #   photos (tenant_id, created_via_ai_suggestion)
    #     -> tag-row queries "which tags came from AI suggestions"
    #        for TIA Section 8 + Calibration RAG.
    await db.photo_tray.create_index(
        [("tenant_id", 1), ("ai_analysis_status", 1), ("uploaded_at", 1)],
        name="pt_tenant_aistatus_uploadedat",
    )
    await db.photo_tray.create_index(
        [("tenant_id", 1), ("ai_suggested_class", 1)],
        name="pt_tenant_aisuggestedclass",
    )
    await db.photo_tray.create_index(
        [("tenant_id", 1), ("phash", 1), ("ai_analysis_status", 1)],
        name="pt_tenant_phash_aistatus",
    )
    await db.photos.create_index(
        [("tenant_id", 1), ("created_via_ai_suggestion", 1)],
        name="ph_tenant_createdviaai",
    )

    # Bucket F Phase 2 (2026-05-08) -- photo_ai_locks unique index +
    # TTL. The $setOnInsert dedup-race lock relies on the unique
    # (tenant_id, phash) constraint to atomically pick a winner; the
    # TTL sweeps stale locks after 60s so a crashed analyze can never
    # permanently block a pHash. expireAfterSeconds is applied to
    # locked_at via a separate single-field index per Mongo's rules
    # (TTL must be a single field).
    await db.photo_ai_locks.create_index(
        [("tenant_id", 1), ("phash", 1)],
        unique=True, name="pal_tenant_phash_unique",
    )
    await db.photo_ai_locks.create_index(
        [("locked_at", 1)],
        expireAfterSeconds=60,
        name="pal_locked_at_ttl",
    )

    # Bucket F Phase 5 (2026-05-11) -- ai_call_timings TTL + lookup
    # indexes. Timing rows feed the P50/P95/P99 dashboard. TTL retains
    # 90 days; that's enough for trend lines but not so much that the
    # collection bloats.
    await db.ai_call_timings.create_index(
        [("tenant_id", 1), ("recorded_at", -1)],
        name="act_tenant_recorded",
    )
    await db.ai_call_timings.create_index(
        [("recorded_at", 1)],
        expireAfterSeconds=90 * 24 * 3600,  # 90 days
        name="act_recorded_ttl",
    )

    # Bucket G Phase 1 (2026-05-11) -- field_suggestions indexes.
    # Drives the Jigsaw widget (per-entity lookup), the review queue
    # (per-user role + status), and the Phase 5 per-source stats
    # roll-up. NO TTL -- expiration is a status flip + audit op
    # (services.field_suggestions.expire_stale_pending), not a row
    # drop, so the audit chain has evidence of every suggestion.
    await db.field_suggestions.create_index(
        [("tenant_id", 1), ("entity_type", 1),
         ("entity_id", 1), ("status", 1)],
        name="fs_tenant_entity_status",
    )
    await db.field_suggestions.create_index(
        [("tenant_id", 1), ("status", 1), ("created_at", -1)],
        name="fs_tenant_status_created",
    )
    await db.field_suggestions.create_index(
        [("tenant_id", 1), ("suggested_by", 1)],
        name="fs_tenant_source",
    )
    await db.field_suggestions.create_index(
        [("tenant_id", 1), ("authorized_confirmer_roles", 1)],
        name="fs_tenant_roles",
    )
    await db.field_suggestions.create_index(
        [("created_at", -1)],
        name="fs_created_at",
    )

    # Bucket E Phase 1 (2026-05-11) -- submittals SLA + chain-of-
    # custody indexes. Same set as migration 013; declared here too
    # so fresh deploys (never run migrations) get them on lifespan
    # startup.
    await db.submittals.create_index(
        [("tenant_id", 1), ("project_id", 1), ("status", 1)],
        name="sub_tenant_project_status",
    )
    await db.submittals.create_index(
        [("tenant_id", 1), ("a_e_review_due_at", 1)],
        name="sub_tenant_sla_due",
    )
    await db.submittals.create_index(
        [("tenant_id", 1), ("is_overdue", 1), ("project_id", 1)],
        name="sub_tenant_overdue",
    )
    await db.submittals.create_index(
        [("tenant_id", 1), ("submitted_to_ae_at", -1)],
        name="sub_tenant_submitted_to_ae",
    )
    await db.submittals.create_index(
        [("tenant_id", 1), ("project_id", 1), ("discipline", 1)],
        name="sub_tenant_discipline",
    )
    # Chain-of-custody event collection.
    await db.submittal_review_events.create_index(
        [("tenant_id", 1), ("submittal_id", 1), ("performed_at", 1)],
        name="sre_tenant_submittal_performed",
    )
    await db.submittal_review_events.create_index(
        [("tenant_id", 1), ("event_type", 1), ("performed_at", -1)],
        name="sre_tenant_event_performed",
    )
    await db.submittal_review_events.create_index(
        [("tenant_id", 1), ("project_id", 1), ("event_type", 1)],
        name="sre_tenant_project_event",
    )

    # Bucket I (2026-05-11) -- recovery_reports indexes. Mirror
    # migration 014 so fresh deploys get them on lifespan startup.
    await db.recovery_reports.create_index(
        [("tenant_id", 1), ("project_id", 1), ("generated_at", -1)],
        name="rr_tenant_project_generated",
    )
    await db.recovery_reports.create_index(
        [("tenant_id", 1), ("report_type", 1), ("status", 1)],
        name="rr_tenant_type_status",
    )
    await db.recovery_reports.create_index(
        [("tenant_id", 1), ("project_id", 1),
         ("report_type", 1), ("idempotency_key", 1)],
        name="rr_tenant_project_type_idem",
    )
    await db.recovery_reports.create_index(
        [("tenant_id", 1), ("status", 1), ("generated_at", -1)],
        name="rr_tenant_status_generated",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _ensure_indexes()
    # Followup-fix (2026-05-08): bcrypt runtime self-test. Hashes + verifies
    # a known string at boot. If this fails, EVERY user is about to be
    # locked out -- the deploy log will scream about it instead of the
    # operator finding out via "Invalid email or password" reports the
    # next morning. We log + proceed (don't crash) so the operator can
    # still hit the CLI reset path; the loud WARN is what we want.
    try:
        from auth import validate_bcrypt_runtime
        result = validate_bcrypt_runtime()
        if result.get("ok"):
            logger.info("bcrypt runtime self-test: PASS (version=%s)", result.get("version"))
        else:
            logger.warning(
                "bcrypt runtime self-test: FAIL (version=%s, error=%s). "
                "Existing password hashes may not validate. Investigate before "
                "trusting login attempts.",
                result.get("version"), result.get("error"),
            )
    except Exception as e:
        logger.warning("bcrypt runtime self-test: errored out: %s", e)
    # Init Emergent object storage (best-effort — voice/upload work even if it fails)
    try:
        from services.storage import init_storage
        init_storage()
        logger.info("Object storage initialized")
    except Exception as e:
        logger.warning("Object storage init failed (uploads will retry): %s", e)
    # Bucket F Phase 3 -- pending-row sweeper on startup. Catches photos
    # that were uploaded during a worker outage and stuck in `pending`
    # because the in-process BackgroundTask never ran. Tenant-wide
    # sweep (no tenant filter) so a multi-tenant deploy heals everyone
    # on the same boot. Cap at 100 rows so startup isn't blocked by
    # a big backlog; the admin-only /photo-tray/sweep-stuck-pending
    # endpoint can drain the rest.
    try:
        from services.photo_ai import sweep_stuck_pending
        sweep_result = await sweep_stuck_pending(
            tenant_id=None, older_than_minutes=5, limit=100,
        )
        if sweep_result.get("swept"):
            logger.info(
                "Photo AI startup sweep: %s rows touched (complete=%s, "
                "skipped=%s, failed=%s, budget_paused=%s, tenants=%s)",
                sweep_result.get("swept"), sweep_result.get("complete"),
                sweep_result.get("skipped"), sweep_result.get("failed"),
                sweep_result.get("budget_paused"),
                sweep_result.get("tenants_touched"),
            )
    except Exception as e:
        logger.warning("Photo AI startup sweep failed (non-fatal): %s", e)

    # Bucket G Phase 5 (2026-05-11) -- field_suggestions expiration
    # sweep. Pending suggestions older than EXPIRATION_DAYS (30) get
    # flipped to "expired" with rejection_reason set. Audit fires
    # FIELD_SUGGESTION_EXPIRED. Stops zombie suggestions from
    # cluttering the Review Queue forever.
    #
    # Boot pass: cap at 1000 rows so a backlog can't slow startup.
    # Recurring pass: every 6 hours via asyncio task. Cancelled on
    # shutdown via the lifespan exit path below.
    try:
        from services.field_suggestions import expire_stale_pending
        boot_sweep = await expire_stale_pending(
            tenant_id=None, older_than_days=30, limit=1000,
        )
        if boot_sweep.get("swept"):
            logger.info(
                "field_suggestions boot expiration sweep: swept=%s "
                "expired=%s tenants_touched=%s failures=%s",
                boot_sweep.get("swept"), boot_sweep.get("expired"),
                boot_sweep.get("tenants_touched"),
                boot_sweep.get("failures"),
            )
    except Exception as e:
        logger.warning(
            "field_suggestions boot expiration sweep failed (non-fatal): %s",
            e,
        )

    # Recurring 6-hour expiration sweep. asyncio.create_task so it
    # runs alongside the API server without blocking request handling.
    # The task ref is stashed on app.state so the shutdown path can
    # cancel it cleanly.
    import asyncio as _asyncio

    async def _periodic_suggestion_expire():
        from services.field_suggestions import expire_stale_pending as _exp
        SLEEP_S = 6 * 3600
        while True:
            try:
                await _asyncio.sleep(SLEEP_S)
                res = await _exp(tenant_id=None, older_than_days=30, limit=5000)
                if res.get("expired"):
                    logger.info(
                        "field_suggestions periodic sweep: "
                        "swept=%s expired=%s tenants=%s",
                        res.get("swept"), res.get("expired"),
                        res.get("tenants_touched"),
                    )
            except _asyncio.CancelledError:
                logger.info("field_suggestions periodic sweep cancelled")
                raise
            except Exception as exc:
                logger.warning(
                    "field_suggestions periodic sweep tick failed "
                    "(non-fatal): %s", exc,
                )

    app.state.field_suggestions_expire_task = _asyncio.create_task(
        _periodic_suggestion_expire(),
    )

    # Bucket E Phase 4e (2026-05-11) -- recurring submittal SLA breach
    # detection. Scans all tenants every 6 hours. Each tenant's open
    # submittals get checked for now > due_date; breached rows flip
    # sla_status, fire SUBMITTAL_SLA_BREACHED audit, write a chain
    # event, and cascade-flag any linked task via gating_entities.
    # Boot pass also runs once on startup so a downtime window
    # doesn't leave breaches sitting silent.
    async def _periodic_submittal_breach_detect():
        from services.submittal_sla import detect_breaches
        SLEEP_S = 6 * 3600
        while True:
            try:
                await _asyncio.sleep(SLEEP_S)
                # Walk every active tenant.
                db = get_db()
                tenants = await db.tenants.find(
                    {"status": {"$ne": "archived"}},
                    {"_id": 0, "id": 1},
                ).to_list(length=None)
                total_new = 0
                for t in tenants:
                    try:
                        out = await detect_breaches(
                            tenant_id=t.get("id"),
                            actor_id=None,
                            request_id="submittal_sla_breach_periodic",
                        )
                        total_new += out.get("newly_breached", 0)
                    except Exception as exc:
                        logger.warning(
                            "submittal SLA breach detect failed for tenant %s: %s",
                            t.get("id"), exc,
                        )
                if total_new:
                    logger.info(
                        "submittal_sla periodic breach scan: "
                        "tenants=%s newly_breached=%s",
                        len(tenants), total_new,
                    )
            except _asyncio.CancelledError:
                logger.info("submittal_sla periodic scan cancelled")
                raise
            except Exception as exc:
                logger.warning(
                    "submittal_sla periodic scan tick failed "
                    "(non-fatal): %s", exc,
                )

    # Boot pass -- catch breaches accumulated during downtime.
    try:
        from services.submittal_sla import detect_breaches as _detect_breaches_boot
        db = get_db()
        tenants = await db.tenants.find(
            {"status": {"$ne": "archived"}},
            {"_id": 0, "id": 1},
        ).to_list(length=None)
        for t in tenants:
            try:
                out = await _detect_breaches_boot(
                    tenant_id=t.get("id"),
                    actor_id=None,
                    request_id="submittal_sla_boot",
                )
                if out.get("newly_breached"):
                    logger.info(
                        "submittal_sla boot breach scan: tenant=%s "
                        "newly_breached=%s cascade_flagged=%s",
                        t.get("id"), out.get("newly_breached"),
                        out.get("cascade_flagged_tasks"),
                    )
            except Exception as exc:
                logger.warning(
                    "submittal_sla boot breach detect failed for "
                    "tenant %s: %s", t.get("id"), exc,
                )
    except Exception as exc:
        logger.warning(
            "submittal_sla boot breach scan failed (non-fatal): %s", exc,
        )

    app.state.submittal_sla_breach_task = _asyncio.create_task(
        _periodic_submittal_breach_detect(),
    )

    # Bucket I (2026-05-11) -- Friday Recovery Report auto-generation.
    # Every Friday at 21:00 UTC (which is 4-5pm in CONUS time zones)
    # the BG task scans every active project (skipping CUI projects
    # per Bucket F gate) and drops a draft friday_recovery report.
    # PM sees the draft in the Reports Generator tab + can finalize.
    # Tenants can opt out via tenant.recovery_reports_auto_friday
    # = false (default True for non-CUI tenants).
    async def _periodic_friday_recovery():
        from services.recovery_report import generate_friday_recovery
        from datetime import datetime, timezone, timedelta
        # Sleep loop with a 30-min granularity. Each tick checks if
        # we just crossed Friday 21:00 UTC; if so, fire generation.
        # last_fire_ts prevents double-fire if the worker restarts
        # within a 30-min window.
        last_fire_ts: dict[str, str] = {}  # project_id -> week_key
        while True:
            try:
                await _asyncio.sleep(30 * 60)   # 30 min
                now = datetime.now(timezone.utc)
                if now.weekday() != 4:          # Mon=0 ... Fri=4
                    continue
                if now.hour < 21:               # Run from 21:00 UTC on
                    continue
                week_key = now.strftime("%Y-W%V")
                db = get_db()
                # All active tenants.
                tenants = await db.tenants.find(
                    {"status": {"$ne": "archived"}},
                    {"_id": 0, "id": 1,
                     "recovery_reports_auto_friday": 1},
                ).to_list(length=None)
                for t in tenants:
                    if t.get("recovery_reports_auto_friday") is False:
                        continue
                    tid = t.get("id")
                    if not tid:
                        continue
                    # Pull active non-CUI projects.
                    projects = await db.projects.find(
                        tenant_filter(tid, {
                            "status": {"$in": ["active", "mobilization"]},
                            "is_cui": {"$ne": True},
                        }),
                        {"_id": 0, "id": 1, "name": 1},
                    ).to_list(length=None)
                    for p in projects:
                        pid = p.get("id")
                        if not pid:
                            continue
                        key = f"{tid}|{pid}|{week_key}"
                        if last_fire_ts.get(key):
                            continue
                        try:
                            await generate_friday_recovery(
                                tenant_id=tid, project_id=pid,
                                actor_id=None,
                                request_id=f"friday_auto_{week_key}",
                                idempotency_key=f"friday_auto_{week_key}",
                            )
                            last_fire_ts[key] = week_key
                        except Exception as exc:
                            logger.warning(
                                "Friday auto-gen failed for project %s: %s",
                                pid, exc,
                            )
                logger.info(
                    "Friday recovery auto-gen tick: tenants=%s",
                    len(tenants),
                )
            except _asyncio.CancelledError:
                logger.info("Friday auto-gen task cancelled")
                raise
            except Exception as exc:
                logger.warning(
                    "Friday auto-gen tick failed (non-fatal): %s", exc,
                )

    app.state.friday_recovery_task = _asyncio.create_task(
        _periodic_friday_recovery(),
    )
    yield
    # Cancel the recurring tasks before closing the DB so the next
    # tick doesn't hit a closed connection.
    #
    # CancelledError is a BaseException (not Exception) in Py3.8+, so
    # `except Exception: pass` does NOT swallow it -- the prior code
    # leaked it out of the lifespan and triggered "Application
    # shutdown failed" log noise on every SIGTERM. The explicit
    # CancelledError branch absorbs the expected re-raise from the
    # task's own `except CancelledError: raise` cleanup hook.
    for attr in (
        "field_suggestions_expire_task",
        "submittal_sla_breach_task",
        "friday_recovery_task",
    ):
        task = getattr(app.state, attr, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except _asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "BG task %s shutdown raised (non-fatal): %s", attr, exc,
                )
    await close_db()


app = FastAPI(title="ICE Schedule Ops", lifespan=lifespan)


# Followup-fix (2026-05-07): friendlier 422 error responses. Default
# FastAPI returns a list of {loc, msg, type} dicts; the frontend toast
# (CrudList.jsx) reads detail[0].msg which is just "Field required" --
# the user has no way to know WHICH field. Convert to a human-readable
# string naming the offending field while keeping the structured list
# alongside under `errors` for clients that want it.
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    msgs: list[str] = []
    for e in errors:
        loc = e.get("loc") or ()
        # Strip the conventional 'body' prefix on body-validation errors.
        path_parts = [str(p) for p in loc if p not in ("body", "query", "path")]
        path = ".".join(path_parts) if path_parts else "(root)"
        msg = e.get("msg") or "invalid"
        msgs.append(f"{path}: {msg}")
    detail = "; ".join(msgs) if msgs else "Validation failed"
    return JSONResponse(
        status_code=422,
        content={"detail": detail, "errors": errors},
    )


api_router = APIRouter(prefix="/api")


@api_router.get("/health")
async def health():
    return {"status": "ok", "service": "ice-schedule-ops"}


# Tenant info endpoint
@api_router.get("/tenants/current")
async def current_tenant(ctx: dict = Depends(require_tenant)):
    db = get_db()
    t = await db.tenants.find_one({"id": ctx["tenant_id"]}, {"_id": 0})
    return t or {}


api_router.include_router(auth_router)
api_router.include_router(team_router)
api_router.include_router(projects_router)
api_router.include_router(tasks_router)
api_router.include_router(daily_reports_router)
# dr_voice_router declares its own /api prefix, mount on the app root.
app.include_router(dr_voice_router)
api_router.include_router(resources_router)
api_router.include_router(field_events_router)
api_router.include_router(docs_router)
api_router.include_router(system_router)
api_router.include_router(voice_router)
api_router.include_router(upload_router)
api_router.include_router(iter3_router)
api_router.include_router(iter4_router)
api_router.include_router(iter5_router)
api_router.include_router(iter6_router)
api_router.include_router(iter7_router)
api_router.include_router(iter8_router)
api_router.include_router(schedule_proposals_router)
api_router.include_router(schedule_diffs_router)
api_router.include_router(calendar_reconciliation_router)
api_router.include_router(tia_router)
api_router.include_router(photo_tray_router)
api_router.include_router(mitigations_router)
api_router.include_router(gating_entities_router)
api_router.include_router(coordination_router)
api_router.include_router(edit_requests_router)
api_router.include_router(resource_leveling_router)
api_router.include_router(project_members_router)
api_router.include_router(field_suggestions_router)
api_router.include_router(submittal_sla_router)
api_router.include_router(recovery_reports_router)
api_router.include_router(exhibit_exports_router)
api_router.include_router(ws_router)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["set-cookie"],
)
