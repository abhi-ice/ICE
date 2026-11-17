"""Full database backup download (2026-11-16).

GET /api/admin/backup/download -> a .zip of every collection in this
server's database, for the platform owner only. It only READS the data.

Why it exists: the hosting provider's database cannot be reached from
outside and its database viewer has no export, so the app itself has to
hand out a copy (for backups and for moving to another host).

File layout (one zip):
  manifest.json            database name, time, and per-collection counts
                           and index definitions (relaxed Extended JSON, readable)
  collections/<name>.jsonl one document per line, in MongoDB "canonical
                           Extended JSON" so dates, ObjectIds and 64-bit
                           numbers come back exactly (bson.json_util.loads)

Safety:
  * Only emails listed in PLATFORM_ADMIN_EMAILS may download. Unlike the
    other platform settings there is NO fallback to company admins when
    that list is empty: the file holds every company's data. Company team
    tools cannot invite, remove or reset the password of a listed email
    (auth_router._is_platform_owner_email).
  * One backup at a time per server process, so repeated clicks cannot
    pile up work on the database.
  * The file is built in a worker thread with its own connection that
    prefers a secondary copy of the database, so live requests keep being
    served while it runs.
  * Each download is recorded in the `backup_runs` collection (who, when,
    counts) and logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone

from bson import json_util
from bson.json_util import CANONICAL_JSON_OPTIONS, RELAXED_JSON_OPTIONS
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pymongo import MongoClient

from auth import require_tenant
from database import get_db
from routers.admin_settings_router import _platform_admin_emails

log = logging.getLogger("ice_schedule_ops.backup")

router = APIRouter(tags=["admin-backup"])

_LOCK = asyncio.Lock()
_BATCH = 500
_CHUNK = 256 * 1024
_SKIP_INDEX_KEYS = {"v", "ns", "key", "background"}


def _require_platform_owner(ctx: dict) -> str:
    allowed = _platform_admin_emails()
    email = ((ctx.get("user") or {}).get("email") or "").strip().lower()
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="Backups are turned off until the platform owner's email is set "
                   "(PLATFORM_ADMIN_EMAILS) on this server.",
        )
    if email not in allowed:
        raise HTTPException(status_code=403, detail="Only the platform owner can download a full backup.")
    return email


def _index_specs(info: dict) -> list:
    """index_information() output -> list keeping every index option."""
    out = []
    for name, spec in info.items():
        item = {"name": name, "key": [[k, v] for k, v in spec.get("key", [])]}
        for opt, val in spec.items():
            if opt not in _SKIP_INDEX_KEYS:
                item[opt] = val
        out.append(item)
    return out


def build_backup(db, fileobj) -> dict:
    """Write the backup zip into `fileobj` from a synchronous pymongo `db`
    and return the manifest. Runs in a worker thread, never on the event
    loop."""
    started = datetime.now(timezone.utc)
    names = sorted(n for n in db.list_collection_names() if not n.startswith("system."))
    manifest = {
        "format": "ice-backup-v1",
        "database": db.name,
        "started_at": started.isoformat(),
        "collections": {},
    }
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            coll = db[name]
            count = 0
            with zf.open(f"collections/{name}.jsonl", "w", force_zip64=True) as out:
                for doc in coll.find({}, batch_size=_BATCH):
                    line = json_util.dumps(doc, json_options=CANONICAL_JSON_OPTIONS)
                    out.write(line.encode("utf-8") + b"\n")
                    count += 1
            try:
                indexes = _index_specs(coll.index_information())
            except Exception:  # pragma: no cover - index listing is best effort
                indexes = []
            manifest["collections"][name] = {"count": count, "indexes": indexes}
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["total_documents"] = sum(c["count"] for c in manifest["collections"].values())
        zf.writestr("manifest.json", json_util.dumps(manifest, json_options=RELAXED_JSON_OPTIONS, indent=2))
    return manifest


def _build_with_own_client(db_name: str, fileobj) -> dict:
    client = MongoClient(os.environ["MONGO_URL"], readPreference="secondaryPreferred")
    try:
        return build_backup(client[db_name], fileobj)
    finally:
        client.close()


# project-scope: tenant-level (platform owner only; a copy of the whole database)
@router.get("/admin/backup/download")
async def download_backup(ctx: dict = Depends(require_tenant)):
    email = _require_platform_owner(ctx)
    if _LOCK.locked():
        raise HTTPException(status_code=409, detail="A backup is already being prepared. Try again in a minute.")
    async with _LOCK:
        db = get_db()
        tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        try:
            manifest = await asyncio.to_thread(_build_with_own_client, db.name, tmp)
        except Exception:
            tmp.close()
            log.exception("Backup failed")
            raise HTTPException(status_code=500, detail="The backup could not be prepared. Please try again.")
        size = tmp.tell()
        tmp.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"ice-backup-{stamp}.zip"
    try:
        await db.backup_runs.insert_one({
            "by": email,
            "at": datetime.now(timezone.utc).isoformat(),
            "database": manifest["database"],
            "total_documents": manifest["total_documents"],
            "collections": {k: v["count"] for k, v in manifest["collections"].items()},
            "bytes": size,
        })
    except Exception:  # never block the download on the record
        log.warning("Could not record backup run", exc_info=True)
    log.warning("Full backup downloaded by %s: %s documents, %s bytes", email, manifest["total_documents"], size)

    def _stream():
        try:
            while True:
                chunk = tmp.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            tmp.close()

    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        },
    )
