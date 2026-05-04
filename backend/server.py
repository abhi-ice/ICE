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
from fastapi import FastAPI, APIRouter, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from database import get_db, close_db  # noqa: E402
from auth import require_tenant  # noqa: E402
from seed import seed_demo  # noqa: E402
from routers.auth_router import router as auth_router  # noqa: E402
from routers.google_auth_router import router as google_auth_router  # noqa: E402
from routers.projects_router import router as projects_router  # noqa: E402
from routers.tasks_router import router as tasks_router  # noqa: E402
from routers.daily_reports_router import router as daily_reports_router  # noqa: E402
from routers.resources_router import router as resources_router  # noqa: E402
from routers.field_events_router import router as field_events_router  # noqa: E402
from routers.docs_router import router as docs_router  # noqa: E402
from routers.system_router import router as system_router  # noqa: E402
from routers.voice_router import router as voice_router  # noqa: E402
from routers.upload_router import router as upload_router  # noqa: E402

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _ensure_indexes()
    # Seed on startup so the demo is always available
    try:
        await seed_demo()
        logger.info("Demo seed ensured.")
    except Exception as e:
        logger.exception("Seed failed: %s", e)
    # Init Emergent object storage (best-effort — voice/upload work even if it fails)
    try:
        from services.storage import init_storage
        init_storage()
        logger.info("Object storage initialized")
    except Exception as e:
        logger.warning("Object storage init failed (uploads will retry): %s", e)
    yield
    await close_db()


app = FastAPI(title="ICE Schedule Ops", lifespan=lifespan)

api_router = APIRouter(prefix="/api")


@api_router.get("/health")
async def health():
    return {"status": "ok", "service": "ice-schedule-ops"}


@api_router.post("/dev/seed")
async def dev_seed():
    return await seed_demo()


# Tenant info endpoint
@api_router.get("/tenants/current")
async def current_tenant(ctx: dict = Depends(require_tenant)):
    db = get_db()
    t = await db.tenants.find_one({"id": ctx["tenant_id"]}, {"_id": 0})
    return t or {}


api_router.include_router(auth_router)
api_router.include_router(google_auth_router)
api_router.include_router(projects_router)
api_router.include_router(tasks_router)
api_router.include_router(daily_reports_router)
api_router.include_router(resources_router)
api_router.include_router(field_events_router)
api_router.include_router(docs_router)
api_router.include_router(system_router)
api_router.include_router(voice_router)
api_router.include_router(upload_router)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["set-cookie"],
)
