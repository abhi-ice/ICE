"""Bucket C3b Phase 4 — backfill sha256 + server_received_timestamp +
photo_class on existing photos.

For every photo without a sha256_hash, pull the bytes from storage and
hash them. Set server_received_timestamp = photo.created_at (best
available proxy for "when the platform first saw it"). Default
photo_class = progress (the safe non-evidentiary default; user can
re-classify via PATCH /photos/{id}). Try to extract device_timestamp
from the existing pHash service's captured_at_exif if present.

Idempotent — per-record skip if sha256_hash already populated. Exits
non-zero on any failure (CI-safe).

Run from backend/ with the runtime env vars (MONGO_URL, DB_NAME):

  python -m migrations.003_photo_evidentiary_backfill
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python -m migrations.003_photo_evidentiary_backfill` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _to_dt(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


async def run():
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        sys.exit(2)
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    # Patch database globals so service.storage.get_object resolves the
    # right client when called outside a request context.
    import database as _database
    _database._client = client  # type: ignore
    _database._db = db          # type: ignore

    import hashlib
    try:
        from services.storage import get_object
    except Exception as e:
        print(f"ERROR importing storage service: {e}", file=sys.stderr)
        sys.exit(2)

    examined = 0
    updated = 0
    skipped = 0
    failures: list[dict] = []

    async for p in db.photos.find({}, {"_id": 0}):
        examined += 1
        try:
            if p.get("sha256_hash"):
                skipped += 1
                continue
            storage_path = p.get("storage_path")
            if not storage_path:
                # Photo with no storage_path — can't hash. Stamp the
                # other fields so the schema reads cleanly downstream.
                pass

            sha256 = None
            if storage_path:
                try:
                    data, _ct = get_object(storage_path)
                    sha256 = hashlib.sha256(data).hexdigest()
                except Exception as e:
                    failures.append({"id": p.get("id"), "error": f"storage hash failed: {e}"})
                    # Don't skip — still backfill the schema fields below.

            now_iso = datetime.now(timezone.utc).isoformat()
            created = _to_dt(p.get("created_at")) or datetime.now(timezone.utc)
            device_ts = _to_dt(p.get("captured_at_exif"))  # may be set by extract_photo_exif

            updates: dict = {
                "sha256_hash": sha256,
                "server_received_timestamp": (created or now_iso) if isinstance(created, str) else created.isoformat(),
                "device_timestamp": device_ts.isoformat() if device_ts else None,
                "timestamp_mismatch_flag": False,
                "exif_preserved": True,
                "photo_class": p.get("photo_class") or "progress",
                "evidentiary_flag": p.get("evidentiary_flag") or False,
            }
            await db.photos.update_one({"id": p["id"]}, {"$set": updates})
            updated += 1
        except Exception as e:
            failures.append({"id": p.get("id"), "error": str(e)})

    print(f"examined={examined}  updated={updated}  skipped={skipped}  failures={len(failures)}")
    for f in failures:
        print(f"  - {f['id']}: {f['error']}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
