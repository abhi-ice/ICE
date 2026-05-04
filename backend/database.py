"""MongoDB connection + tenant-aware helpers.

SUBSTITUTION: master prompt asked for PostgreSQL with row-level security.
Emergent's runtime is hardcoded to MongoDB, so RLS is enforced at the
application layer: every query must include `tenant_id` via `tenant_filter()`
or `with_tenant()`. Cross-tenant leak tests live in /app/backend/tests.
"""
import os
from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional
from uuid import UUID

_client: Optional[AsyncIOMotorClient] = None
_db = None


def get_db():
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        _db = _client[os.environ["DB_NAME"]]
    return _db


def tenant_filter(tenant_id: str, extra: dict | None = None) -> dict:
    """Build a MongoDB filter that ALWAYS includes tenant_id.

    Use this in every query touching business collections.
    """
    f = {"tenant_id": str(tenant_id)}
    if extra:
        f.update(extra)
    return f


async def close_db():
    global _client
    if _client:
        _client.close()
