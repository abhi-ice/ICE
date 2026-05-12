"""Migration 016 -- Google-Auth purge.

Federal-contractor data-confidentiality policy: no third-party auth
providers. The 2026-05-11 purge removed the Emergent-managed Google
OAuth router (backend/routers/google_auth_router.py), the cookie
session_token resolution path in auth.py, the AuthCallback page on
the frontend, and the orphan UI + comment references.

This migration cleans the data layer:

  1. Drops the `user_sessions` collection (only ever used by the
     cookie auth path; safe to drop after router removal).
  2. `$unset` `auth_provider` + `picture` on every users row (those
     fields were only ever populated by google_auth_router.py).
  3. Writes a `GOOGLE_AUTH_REMOVED` audit op per tenant for the
     SHA-256 chain so counsel can later prove the removal date.

Each step is idempotent:
  - drop_collection on a missing collection is a no-op
  - `$unset` is a no-op on documents that don't have the field
  - per-tenant audit is gated by existence check

Run from backend/:

  python -m migrations.016_google_auth_purge
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        sys.exit(2)

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    import database as _database
    _database._client = client
    _database._db = db

    from audit import write_audit

    now = datetime.now(timezone.utc)
    print("=== Migration 016: Google-Auth purge ===")

    # --- Step 1: drop user_sessions collection -----------------------
    existing = await db.list_collection_names()
    if "user_sessions" in existing:
        try:
            await db.drop_collection("user_sessions")
            print("Dropped collection: user_sessions")
            user_sessions_dropped = True
        except Exception as exc:
            print(f"  ! drop user_sessions failed (non-fatal): "
                  f"{type(exc).__name__}: {exc}")
            user_sessions_dropped = False
    else:
        print("Collection user_sessions not present -- nothing to drop.")
        user_sessions_dropped = False

    # --- Step 2: $unset google-sourced fields on users ---------------
    # These fields were only ever populated by google_auth_router.py.
    # After purge, they're vestigial -- $unset wipes them across all
    # tenants. Idempotent: $unset on missing fields is a no-op.
    unset_res = await db.users.update_many(
        {"$or": [
            {"auth_provider": {"$exists": True}},
            {"picture": {"$exists": True}},
        ]},
        {"$unset": {"auth_provider": "", "picture": ""}},
    )
    users_cleaned = int(unset_res.modified_count or 0)
    print(f"Cleaned google-sourced fields on {users_cleaned} user rows.")

    # --- Step 3: per-tenant GOOGLE_AUTH_REMOVED audit op -------------
    audits_written = 0
    audits_skipped = 0
    tenants = db.tenants.find({}, {"_id": 0, "id": 1})
    async for tenant in tenants:
        tid = tenant.get("id")
        if not tid:
            continue
        already = await db.audit_log.find_one(
            {"tenant_id": tid, "op": "GOOGLE_AUTH_REMOVED"},
            {"_id": 0, "id": 1},
        )
        if already:
            audits_skipped += 1
            continue
        try:
            await write_audit(
                tenant_id=tid, table_name="users",
                op="GOOGLE_AUTH_REMOVED",
                record_id=f"migration_016_{int(now.timestamp())}",
                before=None, after={
                    "removed_router": "backend/routers/google_auth_router.py",
                    "removed_frontend_page": "frontend/src/pages/AuthCallback.jsx",
                    "user_sessions_collection_dropped": user_sessions_dropped,
                    "users_fields_cleared": ["auth_provider", "picture"],
                    "policy_basis": (
                        "Federal-contractor data-confidentiality: no "
                        "third-party auth providers allowed. Bearer-JWT "
                        "(email/password) is the sole auth path."
                    ),
                    "purge_date_utc": now.isoformat(),
                },
                actor_id=None, request_id="migration_016",
            )
            audits_written += 1
        except Exception as exc:
            print(f"  ! audit {tid}: {type(exc).__name__}: {exc}")

    print(f"\n--- SUMMARY ---")
    print(f"user_sessions dropped:   {user_sessions_dropped}")
    print(f"User rows cleaned:       {users_cleaned}")
    print(f"Tenants audited:         {audits_written}")
    print(f"Tenants already audited: {audits_skipped}")


if __name__ == "__main__":
    asyncio.run(run())
