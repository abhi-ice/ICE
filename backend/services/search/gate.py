"""Outside-provider gate: may this record's text be sent to Voyage?

Voyage is an outside AI provider, so the same rules as photo AI apply
(services/photo_ai.py check_cui_safety). Text is NOT sent when:
  * the company is missing or has ai_enabled = false,
  * the project is missing, is_cui = true, or has ai_enabled = false,
  * the project's master doc has a govt_security_level other than "public"
    (for a master_docs row, its own level counts).

Gated chunks are still stored, with embedding = None, so the keyword search
that runs inside Atlas can still find them.

When the answer for a project changes (for example a project is marked
CUI), note_gate_state() reports it so the worker re-queues the project's
existing chunks and their vectors are removed or added.
"""
from __future__ import annotations

from typing import Any, Optional

from services.search.store import SEARCH_STATE
from services.search.types import ChunkContext


async def project_allows_embedding(ctx: ChunkContext, tenant_id: Optional[str],
                                   project_id: Optional[str]) -> bool:
    if not tenant_id:
        return False
    tenant = await ctx.one("tenants", {"id": tenant_id}, {"id": 1, "ai_enabled": 1})
    if not tenant or tenant.get("ai_enabled") is False:
        return False
    if not project_id:
        return True  # company-level record: only the company switch applies
    project = await ctx.one("projects", {"tenant_id": tenant_id, "id": project_id},
                            {"id": 1, "is_cui": 1, "ai_enabled": 1})
    if not project:
        return False
    if project.get("is_cui") is True or project.get("ai_enabled") is False:
        return False
    md = await ctx.one("master_docs", {"tenant_id": tenant_id, "project_id": project_id},
                       {"govt_security_level": 1})
    level = (md or {}).get("govt_security_level")
    if level and level != "public":
        return False
    return True


async def should_embed(ctx: ChunkContext, tenant_id: Optional[str], project_id: Optional[str],
                       collection: str, doc: Optional[dict[str, Any]]) -> bool:
    if collection == "master_docs" and doc is not None:
        level = doc.get("govt_security_level")
        if level and level != "public":
            return False
    return await project_allows_embedding(ctx, tenant_id, project_id)


def _state_id(tenant_id: Optional[str], project_id: Optional[str]) -> str:
    return f"gate:{tenant_id or ''}:{project_id or ''}"


async def note_gate_state(db, tenant_id: Optional[str], project_id: Optional[str],
                          allowed: bool) -> bool:
    """Remember the gate answer for this project. Returns True when it
    differs from the last answer we stored (the first answer never counts
    as a change)."""
    prev = await db[SEARCH_STATE].find_one_and_update(
        {"_id": _state_id(tenant_id, project_id)},
        {"$set": {"allowed": bool(allowed), "tenant_id": tenant_id, "project_id": project_id}},
        upsert=True,
    )
    return prev is not None and bool(prev.get("allowed")) != bool(allowed)
