"""Search everything: GET /api/search/everything (2026-11-17).

One search box over every indexed record the caller may see (see
services/search/query.py for the pipeline and permission rules).

The path is /search/everything, not /search: GET /api/search is already the
header search box (routers/iter5_router.py global_search, used by
frontend GlobalSearch.jsx and MobileSearch.jsx) and must keep working
exactly as today.

Behind FF_SEARCH_EVERYTHING (Settings page, default OFF). While it is off
the endpoint answers 503 "Search is turned off." after the usual sign-in
check, and nothing else changes.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_tenant
from database import get_db
from services import feature_flags
from services.project_scope import require_project_if_given
from services.search.query import MAX_LIMIT, NOTICE_OFF, search_everything

log = logging.getLogger("search_router")
router = APIRouter(tags=["search"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_date(name: str, value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    if not _DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be a date like 2026-11-17")
    return value


@router.get("/search/everything")
async def search_everything_endpoint(
    q: str = Query(..., min_length=1, max_length=500),
    project_id: Optional[str] = Query(None),
    kind: Optional[str] = Query(None, max_length=400,
                                description="One kind, or several separated by commas"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(8, ge=1, le=MAX_LIMIT),
    ctx: dict = Depends(require_tenant),
):
    if not feature_flags.search_everything_enabled():
        raise HTTPException(503, NOTICE_OFF)
    date_from = _check_date("date_from", date_from)
    date_to = _check_date("date_to", date_to)
    # Same project check as other routes: the project must be in the
    # caller's company, and per-project access applies (404 for a read).
    # search_everything checks the project again on its own (the assistant
    # calls it without this route); that second check runs alongside the
    # query's vector and reuses the assignments already read for this
    # request, so it adds no waiting.
    await require_project_if_given(ctx, project_id)
    kinds = [k.strip() for k in (kind or "").split(",") if k.strip()] or None
    return await search_everything(
        get_db(), ctx, q, project_id=project_id or None, kinds=kinds,
        date_from=date_from, date_to=date_to, limit=limit,
    )
