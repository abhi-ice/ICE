"""Copy every stored file from the Emergent file store to the S3 bucket.

One-off tool for the move to Railway. Each file keeps the SAME key
(for example ice-schedule-ops/photos/<tenant>/<uuid>.jpg), so no database
row has to change.

Safe to run again and again:
  - a key that already exists in the bucket is skipped (not downloaded,
    not uploaded again);
  - nothing is ever deleted, from either store or the database;
  - the database is only read.

Every run writes a CSV manifest with one row per path:
  path, bytes, sha256, status, expected_bytes, expected_sha256, check, source
status is one of: copied, exists, would_copy, missing_in_source, error:<reason>
check compares what was copied with what the database recorded
(files.size, photos.sha256_hash, recovery_reports.exhibit_artifacts.sha256):
ok, size_mismatch, sha256_mismatch, or empty when there is nothing to compare.

Settings, all from environment variables (values are never printed):
  MONGO_URL, DB_NAME                    the database to read paths from
  EMERGENT_LLM_KEY                      the Emergent file store (source)
  S3_ENDPOINT_URL, S3_BUCKET,
  S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
  S3_REGION, S3_ADDRESSING_STYLE        the bucket (target)

Usage, from backend/:
  python -m scripts.copy_emergent_files --dry-run
  python -m scripts.copy_emergent_files --limit 20
  python -m scripts.copy_emergent_files --manifest copy_manifest.csv
  python -m scripts.copy_emergent_files --verify-existing   # also hash files already in the bucket

Where the paths come from (checked against the code on 2026-11-15):
  files.storage_path                          every upload recorded by record_file
  photos.storage_path, photos.thumbnail_path  site photos
  photo_tray.storage_key, .storage_path       photo tray
  drawings.storage_path, .pdf_url             drawings
  drawing_revisions.storage_path, .pdf_url    drawing revisions
  voice_annotations.audio_url, .storage_path  voice notes
  recovery_reports.exhibit_artifacts[].storage_key
  tia_generations (any stored path; its artifacts are also in files)
  project_specs.pdf_url                       spec PDFs (not in files)
  projects.dr_template_file_url               client daily report forms (not in files)
On top of the named fields, every string anywhere in these documents that
starts with "ice-schedule-ops/" is collected too, so a field added later is
not missed. --scan-all-collections widens that search to every collection.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import storage  # noqa: E402

PREFIX = storage.APP_NAME + "/"

# collection -> list of field paths. "a[].b" means field b of each item in
# list a.
FIELDS: dict[str, list[str]] = {
    "files": ["storage_path"],
    "photos": ["storage_path", "thumbnail_path"],
    "photo_tray": ["storage_key", "storage_path"],
    "drawings": ["storage_path", "pdf_url"],
    "drawing_revisions": ["storage_path", "pdf_url"],
    "voice_annotations": ["audio_url", "storage_path"],
    "recovery_reports": ["exhibit_artifacts[].storage_key"],
    "tia_generations": [],
    "project_specs": ["pdf_url"],
    "projects": ["dr_template_file_url"],
}

MANIFEST_COLUMNS = ["path", "bytes", "sha256", "status",
                    "expected_bytes", "expected_sha256", "check", "source"]


# ---------------------------------------------------------------------
# Collecting paths
# ---------------------------------------------------------------------

def _looks_like_key(value) -> bool:
    """A stored key: a non-empty relative path, not a web link or data URL."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or v != value:
        return False
    low = v.lower()
    if low.startswith(("http://", "https://", "data:", "blob:", "/")):
        return False
    return "/" in v


def _field_values(doc: dict, field: str):
    if "[]." in field:
        list_name, sub = field.split("[].", 1)
        for item in doc.get(list_name) or []:
            if isinstance(item, dict):
                yield item.get(sub), item
        return
    yield doc.get(field), doc


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


class PathSet:
    """Distinct paths, with where each was found and any size or SHA-256
    the database recorded for it."""

    def __init__(self):
        self.sources: dict[str, set] = {}
        self.expected_bytes: dict[str, int] = {}
        self.expected_sha: dict[str, str] = {}

    def add(self, path: str, source: str):
        self.sources.setdefault(path, set()).add(source)

    def paths(self) -> list[str]:
        return sorted(self.sources)


def collect_paths(db, scan_all: bool = False) -> PathSet:
    ps = PathSet()
    names = set(db.list_collection_names())
    collections = dict(FIELDS)
    if scan_all:
        for n in names:
            if not n.startswith("system."):
                collections.setdefault(n, [])
    for coll, fields in collections.items():
        if coll not in names:
            continue
        for doc in db[coll].find({}, {"_id": 0}):
            for field in fields:
                for value, holder in _field_values(doc, field):
                    if not _looks_like_key(value):
                        continue
                    ps.add(value, f"{coll}.{field}")
                    if coll == "files" and isinstance(doc.get("size"), int):
                        ps.expected_bytes.setdefault(value, doc["size"])
                    if coll == "photos" and field == "storage_path" and doc.get("sha256_hash"):
                        ps.expected_sha.setdefault(value, str(doc["sha256_hash"]).lower())
                    if coll == "recovery_reports" and holder.get("sha256"):
                        ps.expected_sha.setdefault(value, str(holder["sha256"]).lower())
            for s in _walk_strings(doc):
                if s.startswith(PREFIX) and _looks_like_key(s):
                    ps.add(s, f"{coll}.*")
    return ps


# ---------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------

def _check(row: dict) -> str:
    if row["bytes"] == "" and row["sha256"] == "":
        return ""
    if row["expected_bytes"] != "" and row["bytes"] != "" and int(row["bytes"]) != int(row["expected_bytes"]):
        return "size_mismatch"
    if row["expected_sha256"] and row["sha256"] and row["sha256"] != row["expected_sha256"]:
        return "sha256_mismatch"
    if row["expected_bytes"] != "" or row["expected_sha256"]:
        return "ok"
    return ""


def _is_missing(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 404


def copy_one(path: str, *, dry_run: bool, verify_existing: bool, have_target: bool) -> dict:
    row = {"path": path, "bytes": "", "sha256": "", "status": ""}
    try:
        head = storage.s3_head_object(path) if have_target else None
        if head is not None:
            row["status"] = "exists"
            if head.get("size") is not None:
                row["bytes"] = head["size"]
            if verify_existing and not dry_run:
                data, _ct = storage.s3_get_object(path)
                row["bytes"] = len(data)
                row["sha256"] = hashlib.sha256(data).hexdigest()
            return row
        if dry_run:
            row["status"] = "would_copy"
            return row
        try:
            data, content_type = storage.emergent_get_object(path)
        except Exception as exc:
            row["status"] = "missing_in_source" if _is_missing(exc) else f"error:read:{type(exc).__name__}"
            return row
        row["bytes"] = len(data)
        row["sha256"] = hashlib.sha256(data).hexdigest()
        storage.s3_put_object(path, data, content_type or "application/octet-stream")
        row["status"] = "copied"
    except Exception as exc:
        row["status"] = f"error:{type(exc).__name__}"
    return row


def run(db, *, dry_run: bool, limit: int | None, manifest: str, workers: int,
        verify_existing: bool, scan_all: bool, out=print) -> dict:
    ps = collect_paths(db, scan_all=scan_all)
    paths = ps.paths()
    out(f"Found {len(paths)} distinct stored path(s).")
    if limit is not None:
        paths = paths[:max(0, limit)]
        out(f"--limit {limit}: processing the first {len(paths)} (sorted by path).")
    have_target = bool(storage.s3_settings()["bucket"])
    if not have_target and not dry_run:
        raise SystemExit("S3_BUCKET is not set; nothing can be copied.")
    if not have_target:
        out("S3_BUCKET is not set: dry run lists paths without checking the bucket.")
    if not dry_run and not (os.environ.get("EMERGENT_LLM_KEY") or "").strip():
        raise SystemExit("EMERGENT_LLM_KEY is not set; the Emergent file store cannot be read.")

    rows: list[dict] = []
    lock = threading.Lock()
    counts: dict[str, int] = {}

    def _do(p):
        r = copy_one(p, dry_run=dry_run, verify_existing=verify_existing, have_target=have_target)
        r["expected_bytes"] = ps.expected_bytes.get(p, "")
        r["expected_sha256"] = ps.expected_sha.get(p, "")
        r["check"] = _check(r)
        r["source"] = ";".join(sorted(ps.sources.get(p, ())))
        with lock:
            rows.append(r)
            key = r["status"].split(":", 1)[0]
            counts[key] = counts.get(key, 0) + 1
            if len(rows) % 100 == 0:
                out(f"  {len(rows)}/{len(paths)} done")
        return r

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(_do, paths))

    rows.sort(key=lambda r: r["path"])
    with open(manifest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in MANIFEST_COLUMNS})

    mismatches = sum(1 for r in rows if r["check"] in ("size_mismatch", "sha256_mismatch"))
    out(f"Manifest written: {manifest}")
    out("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) +
        f"; mismatches={mismatches}")
    return {"total": len(rows), "counts": counts, "mismatches": mismatches, "rows": rows}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="scripts.copy_emergent_files",
        description="Copy stored files from the Emergent file store to the S3 bucket under the same keys. Never deletes anything.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be copied; read and write no file contents.")
    p.add_argument("--limit", type=int, default=None,
                   help="Only process the first N paths (sorted), for a trial run.")
    p.add_argument("--manifest", default=None,
                   help="CSV manifest path (default: copy_manifest_<UTC time>.csv in the current folder).")
    p.add_argument("--workers", type=int, default=4, help="Files copied at the same time (default 4).")
    p.add_argument("--verify-existing", action="store_true",
                   help="Download files already in the bucket to record their size and SHA-256.")
    p.add_argument("--scan-all-collections", action="store_true",
                   help="Also look for ice-schedule-ops/ paths in every other collection.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    mongo_url = (os.environ.get("MONGO_URL") or "").strip()
    db_name = (os.environ.get("DB_NAME") or "").strip()
    if not mongo_url or not db_name:
        print("MONGO_URL and DB_NAME must be set.", file=sys.stderr)
        return 2
    from pymongo import MongoClient
    client = MongoClient(mongo_url, serverSelectionTimeoutMS=15000)
    try:
        db = client[db_name]
        manifest = args.manifest or (
            "copy_manifest_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".csv")
        res = run(db, dry_run=args.dry_run, limit=args.limit, manifest=manifest,
                  workers=args.workers, verify_existing=args.verify_existing,
                  scan_all=args.scan_all_collections)
    finally:
        client.close()
    errors = sum(v for k, v in res["counts"].items() if k in ("error", "missing_in_source"))
    return 1 if (errors or res["mismatches"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
