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

from database import get_db, close_db  # noqa: E402
from auth import require_tenant  # noqa: E402
from routers.auth_router import router as auth_router, team_router  # noqa: E402
from routers.projects_router import router as projects_router  # noqa: E402
from routers.tasks_router import router as tasks_router  # noqa: E402
from routers.daily_reports_router import router as daily_reports_router  # noqa: E402
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
    yield
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
