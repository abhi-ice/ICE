"""Load an ICE full backup (.zip from Settings > Full backup) into a database.

Usage:
    RESTORE_TARGET_MONGO_URL=... python backend/scripts/restore_backup.py BACKUP.zip TARGET_DB_NAME
        [--drop --confirm TARGET_DB_NAME]

* The target comes ONLY from RESTORE_TARGET_MONGO_URL (never the app's own
  MONGO_URL), so the connection string stays off the command line and a
  shell that is set up for the app cannot be restored over by accident.
  The script prints the host and database before writing.
* Refuses to write into a database that already has data, unless --drop is
  given together with --confirm repeating the database name (then each
  collection in the backup is dropped first).
* Refuses to restore over the database the backup was taken from.
* Restores every document exactly (canonical Extended JSON), then the
  indexes, then checks every collection's count against the manifest.
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile

from bson import json_util
from pymongo import MongoClient

BATCH = 1000


def read_manifest(zip_path: str) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        manifest = json_util.loads(zf.read("manifest.json").decode("utf-8"))
    if manifest.get("format") != "ice-backup-v1":
        raise SystemExit(f"Not an ICE backup file (format={manifest.get('format')!r})")
    return manifest


def restore(zip_path: str, db, drop: bool = False, log=print) -> dict:
    manifest = read_manifest(zip_path)
    existing = [n for n in db.list_collection_names() if not n.startswith("system.")]
    if existing and not drop:
        raise SystemExit(
            f"Target database '{db.name}' already has {len(existing)} collections. "
            "Use --drop --confirm <name> to replace them."
        )
    result = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name, meta in manifest["collections"].items():
            coll = db[name]
            if drop:
                coll.drop()
            batch, n = [], 0
            with zf.open(f"collections/{name}.jsonl") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    batch.append(json_util.loads(raw.decode("utf-8")))
                    if len(batch) >= BATCH:
                        coll.insert_many(batch, ordered=True)
                        n += len(batch)
                        batch = []
            if batch:
                coll.insert_many(batch, ordered=True)
                n += len(batch)
            if n == 0 and name not in db.list_collection_names():
                db.create_collection(name)
            for ix in meta.get("indexes", []):
                if ix["name"] == "_id_":
                    continue
                opts = {k: v for k, v in ix.items() if k not in ("name", "key")}
                keys = [(k, int(v) if isinstance(v, float) and v.is_integer() else v) for k, v in ix["key"]]
                try:
                    coll.create_index(keys, name=ix["name"], **opts)
                except Exception as exc:  # keep going; report it
                    log(f"  index {name}.{ix['name']} skipped: {exc}")
            actual = coll.count_documents({})
            result[name] = (meta["count"], actual)
            flag = "OK" if actual == meta["count"] else "MISMATCH"
            log(f"{flag:8} {name:40} expected {meta['count']:>7}  restored {actual:>7}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("db_name")
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--confirm", default="")
    a = ap.parse_args()
    uri = os.environ.get("RESTORE_TARGET_MONGO_URL")
    if not uri:
        print("Set RESTORE_TARGET_MONGO_URL to the database you want to fill.")
        return 2
    if a.drop and a.confirm != a.db_name:
        print(f"--drop replaces data. Repeat the database name: --confirm {a.db_name}")
        return 2
    manifest = read_manifest(a.zip_path)
    source_db = manifest.get("database")
    app_uri = os.environ.get("MONGO_URL") or ""
    if app_uri and app_uri == uri and source_db == a.db_name:
        print("Refusing: the target is the same database this backup was taken from.")
        return 2
    client = MongoClient(uri)
    client.admin.command("ping")
    host = ", ".join(f"{h}:{p}" for h, p in client.nodes) or "?"
    print(f"Restoring {a.zip_path} (taken from '{source_db}', {manifest.get('total_documents')} documents)\n"
          f"  into database '{a.db_name}' at {host}"
          f"{'  -- DROPPING existing collections first' if a.drop else ''}")
    res = restore(a.zip_path, client[a.db_name], drop=a.drop)
    bad = {k: v for k, v in res.items() if v[0] != v[1]}
    print(f"\n{len(res)} collections, {sum(v[1] for v in res.values())} documents restored; "
          f"{len(bad)} mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
