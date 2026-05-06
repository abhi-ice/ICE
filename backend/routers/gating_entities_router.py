"""Bucket C3a — gating entities read API.

Phase 2: dropdown endpoint (open gates by task) for the daily-report
constraint reference picker.
Phase 4: detail page endpoint + affected-daily-reports endpoint.
Phase 5: cascade-nullification on underlying entity delete (driven from
crud_delete in iter8_router; this file only owns the read paths and the
service helpers it imports).

All endpoints are tenant-scoped.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List

from database import get_db, tenant_filter
from models import GatingEntity
from auth import require_tenant
from services.gating_entities import compute_age_fields


router = APIRouter(tags=["gating-entities"])


# Statuses that count as "still gating" — used by both the dropdown and
# the daily-report constraint pre-population.
OPEN_STATUSES = ("draft", "submitted", "open", "under_review", "awaiting_clarification")


async def _enrich(ge: dict) -> dict:
    """Merge computed age + over-SLA fields into a gating entity dict."""
    age = await compute_age_fields(ge)
    return {**ge, **age}


@router.get("/projects/{project_id}/gating-entities")
async def list_gating_entities(
    project_id: str,
    task_id: Optional[str] = Query(None, description="Filter to entities gating this task (in gates_task_ids)"),
    status: Optional[str] = Query(None, description="Comma-separated list, e.g. open,under_review"),
    open_only: bool = Query(False, description="Shortcut for status in OPEN_STATUSES"),
    ctx: dict = Depends(require_tenant),
):
    """Generic list — used by the daily-report constraint dropdown
    (filtered by task_id + open_only) and by future dashboards (filtered
    by status). Returns up to 200 rows; sorted most-recent submitted_at
    first so the dropdown shows the freshest gates."""
    db = get_db()
    f: dict = {"project_id": project_id}
    if task_id:
        f["gates_task_ids"] = task_id
    if open_only:
        f["status"] = {"$in": list(OPEN_STATUSES)}
    elif status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if wanted:
            f["status"] = {"$in": wanted}
    docs = await db.gating_entities.find(
        tenant_filter(ctx["tenant_id"], f), {"_id": 0},
    ).sort("submitted_at", -1).to_list(200)
    enriched = [await _enrich(d) for d in docs]
    return [GatingEntity(**d).model_dump(mode="json") for d in enriched]
