"""Project scope check shared by create endpoints (2026-11-16).

Many create endpoints take a project_id in the request body. Before this
helper they saved whatever id they were given, so a user could attach a
change order, safety event, approval and so on to a project that does not
exist or that belongs to another company. require_project() looks the project
up under the caller's own tenant and answers 404 when it is not there.

It does NOT apply per-project membership rules (who on the team may touch
which project); that is a separate decision. It only stops records being
attached to projects outside the caller's company.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from database import get_db, tenant_filter


async def require_project_for_tenant(tenant_id: str, project_id,
                                     projection: Optional[dict] = None,
                                     db=None) -> dict:
    """Return the project doc for (tenant_id, project_id) or raise 404.

    A blank or non-string id is treated as "not found" too, so a malformed
    request never reaches the database query as an object (for example a
    {"$ne": ...} operator smuggled in through JSON). Callers that already
    hold a database handle may pass it as db."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise HTTPException(status_code=404, detail="Project not found")
    proj = await (db if db is not None else get_db()).projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}),
        projection or {"_id": 0, "id": 1, "tenant_id": 1, "name": 1},
    )
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


async def require_project(ctx: dict, project_id,
                          projection: Optional[dict] = None) -> dict:
    """Same check using the request context's active tenant."""
    return await require_project_for_tenant(ctx["tenant_id"], project_id, projection)


async def require_project_if_given(ctx: dict, project_id,
                                   projection: Optional[dict] = None) -> Optional[dict]:
    """For endpoints where project_id is optional: None or an empty string
    stays allowed (returns None); any other value must be a project in the
    caller's tenant."""
    if project_id is None or project_id == "":
        return None
    return await require_project(ctx, project_id, projection)
