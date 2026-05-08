"""Migration 006 -- Test Data Reset (operational, NOT auto-run).

One-shot migration to wipe all test event data for a given tenant +
project, while preserving tenants / projects / users / master schedule
/ audit log. Writes a single RESET_BOUNDARY audit entry at the end so
the SHA-256 hash chain stays intact across the wipe (Option B from the
spec discussion -- chain-of-custody continuity).

NOT REGISTERED in MIGRATIONS in run_all_pending.py. The on-disk
discovery check in run_all_pending.py allow-lists this file via
MANUAL_ONLY so the auto-runner doesn't fail on its presence, but the
runner never calls it.

Run manually only:

  # Preview without deleting anything (no DB writes, no storage deletes).
  python -m migrations.006_test_data_reset \\
    --tenant-id <tenant_id> \\
    --project-id <project_id> \\
    --dry-run

  # Real wipe (requires explicit confirmation prompt + flags).
  python -m migrations.006_test_data_reset \\
    --tenant-id <tenant_id> \\
    --project-id <project_id> \\
    --confirm-wipe \\
    --executed-by <user_id_or_email> \\
    --note "Bucket D testing complete. Resetting for production-shape demo."

Refuses to run without explicit --tenant-id, --project-id, and
either --dry-run or --confirm-wipe. With --confirm-wipe also requires
--executed-by (audit attribution) and a typed confirmation string.
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow `python -m migrations.006_test_data_reset` from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# motor + dotenv are imported lazily inside main() so `--help` and AST
# checks work in environments where backend deps aren't installed
# (e.g. a local dev box without the venv set up).

log = logging.getLogger("migration_006")


# ---------- collection-scoping rules ----------
# Each entry: (collection_name, scope_kind). scope_kind tells the
# counter / wiper how to build the mongo filter for "rows belonging
# to this tenant + project".
#
#   "direct"      -- filter by tenant_id + project_id directly.
#   "via_dr"      -- filter by daily_report_id IN <in-scope DR ids>
#                    (collections that don't carry tenant_id/project_id).
#   "coord"       -- coordination_events: filter by tenant_id + project_id
#                    (it does carry both even though scoping reads use
#                    linked_daily_report_id for the per-DR view).
#   "audit_only"  -- never wiped; listed for the preserved-collections
#                    audit (NOT used by WIPE_COLLECTIONS).
WIPE_COLLECTIONS: list[tuple[str, str]] = [
    # daily-report family
    ("daily_reports",            "direct"),
    ("daily_report_activities",  "via_dr"),
    ("daily_presence",           "via_dr"),
    # event collections
    ("delays",                   "direct"),
    ("rfis",                     "direct"),
    ("qc_issues",                "direct"),
    ("safety_events",            "direct"),
    ("coordination_events",      "coord"),
    ("deliveries",               "direct"),
    ("photos",                   "direct"),
    ("voice_annotations",        "direct"),
    # photo tray
    ("photo_tray",               "direct"),
    # cascade + gating
    ("gating_entities",          "direct"),
    ("cascade_snapshots",        "direct"),
    ("schedule_update_diffs",    "direct"),
    # mitigations
    ("mitigations",              "direct"),
    ("mitigation_outcomes",      "direct"),
    # TIA + edit
    ("tia_generations",          "direct"),
    ("edit_requests",            "direct"),
]

# Collections that the wipe MUST NOT touch. Surfaced in the summary
# so the operator can eyeball-verify nothing critical disappeared.
PRESERVED_COLLECTIONS: list[str] = [
    "tenants",
    "projects",
    "users",
    "schedule_activities",
    "schedule_imports",
    "task_predecessors",
    "audit_log",
    "tenant_settings",
    "project_settings",
    "roles",
    "permissions",
    "locations",
    "drawings",
    "drawing_revisions",
    "polygons",
    "files",
]


STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"


# =====================================================================
# CLI
# =====================================================================

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migrations.006_test_data_reset",
        description=(
            "Wipe test event data for ONE tenant + project. Preserves "
            "tenants, projects, users, master schedule, and the audit "
            "log (with a RESET_BOUNDARY entry appended)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Preview only\n"
            "  python -m migrations.006_test_data_reset --tenant-id ice "
            "--project-id <pid> --dry-run\n"
            "  # Real wipe (interactive confirmation required)\n"
            "  python -m migrations.006_test_data_reset --tenant-id ice "
            "--project-id <pid> --confirm-wipe --executed-by abhi@ice-aec.com "
            "--note 'reset for demo'\n"
        ),
    )
    p.add_argument("--tenant-id", required=True,
                    help="Tenant id to scope the wipe to. Required.")
    p.add_argument("--project-id", required=True,
                    help="Project id to scope the wipe to. Required.")
    p.add_argument("--dry-run", action="store_true",
                    help="Count what WOULD be deleted without deleting. "
                         "Mutually exclusive with --confirm-wipe.")
    p.add_argument("--confirm-wipe", action="store_true",
                    help="Required to perform an actual wipe. Triggers an "
                         "interactive confirmation prompt; type "
                         "'WIPE-CONFIRMED-<tenant_id>' to proceed.")
    p.add_argument("--executed-by", default=None,
                    help="User id or email to record on the RESET_BOUNDARY "
                         "audit entry. Required with --confirm-wipe.")
    p.add_argument("--note", default=None,
                    help="Optional human-readable note attached to the "
                         "RESET_BOUNDARY audit entry.")
    p.add_argument("--include-keys", action="store_true",
                    help="Include the full storage_keys list in the "
                         "RESET_BOUNDARY audit entry. Off by default to "
                         "keep the audit log readable.")
    p.add_argument("--skip-storage", action="store_true",
                    help="Skip the storage-byte deletion step. The DB "
                         "rows for photos and photo_tray are still "
                         "wiped; the storage objects are left as orphans "
                         "(can be GC'd separately).")
    return p.parse_args(argv)


def _validate_flags(args: argparse.Namespace) -> Optional[str]:
    """Returns an error message if the flag combo is invalid, else None."""
    if args.dry_run and args.confirm_wipe:
        return "--dry-run and --confirm-wipe are mutually exclusive."
    if not args.dry_run and not args.confirm_wipe:
        return ("Pass --dry-run to preview or --confirm-wipe to wipe. "
                "Refusing to run without an explicit mode flag.")
    if args.confirm_wipe and not args.executed_by:
        return "--executed-by is required with --confirm-wipe (audit attribution)."
    return None


# =====================================================================
# Storage-object deletion
# =====================================================================

def _delete_storage_object(path: str) -> tuple[bool, str]:
    """Best-effort delete of a single storage object. Returns
    (success, reason). 404 counts as success (already gone)."""
    try:
        import requests
        from services.storage import init_storage
        key = init_storage()
        resp = requests.delete(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key},
            timeout=30,
        )
        if resp.status_code in (200, 204):
            return True, "deleted"
        if resp.status_code == 404:
            return True, "already_gone"
        return False, f"http_{resp.status_code}"
    except Exception as e:
        return False, f"exception:{type(e).__name__}"


# =====================================================================
# Scoped count + delete helpers
# =====================================================================

async def _scoped_filter(db, collection: str, scope_kind: str,
                          tenant_id: str, project_id: str,
                          dr_ids: list[str]) -> dict:
    """Build the mongo filter for a given collection's wipe scope.

    Why a single helper: every collection lookup needs the same
    tenant + project guard, but daily_report_activities and
    daily_presence don't carry tenant_id/project_id directly so
    they need an in-scope DR id list (computed once and passed in).
    """
    if scope_kind in ("direct", "coord"):
        # `coord` scopes match "direct" -- coordination_events carries
        # tenant_id + project_id (the linked_daily_report_id field is
        # used for the per-DR view, not for project scoping).
        return {"tenant_id": tenant_id, "project_id": project_id}
    if scope_kind == "via_dr":
        return {"daily_report_id": {"$in": dr_ids}} if dr_ids else {"_no_match_": True}
    raise ValueError(f"Unknown scope_kind: {scope_kind!r}")


async def _collect_in_scope_dr_ids(db, tenant_id: str, project_id: str) -> list[str]:
    rows = await db.daily_reports.find(
        {"tenant_id": tenant_id, "project_id": project_id},
        {"_id": 0, "id": 1},
    ).to_list(length=None)
    return [r["id"] for r in rows if r.get("id")]


async def _collect_storage_keys(db, tenant_id: str, project_id: str,
                                  dr_ids: list[str]) -> tuple[list[str], list[str]]:
    """Return (photo_storage_paths, tray_storage_keys). Collected
    BEFORE row deletion so we don't lose the references."""
    photo_paths: list[str] = []
    p_cursor = db.photos.find(
        {"tenant_id": tenant_id, "project_id": project_id},
        {"_id": 0, "storage_path": 1},
    )
    async for p in p_cursor:
        sp = p.get("storage_path")
        if sp:
            photo_paths.append(sp)

    tray_keys: list[str] = []
    t_cursor = db.photo_tray.find(
        {"tenant_id": tenant_id, "project_id": project_id},
        {"_id": 0, "storage_key": 1},
    )
    async for t in t_cursor:
        sk = t.get("storage_key")
        if sk:
            tray_keys.append(sk)
    return photo_paths, tray_keys


# =====================================================================
# Audit entry
# =====================================================================

async def _write_reset_boundary(
    *, tenant_id: str, project_id: str, executed_by: str,
    note: Optional[str], per_collection: dict[str, dict],
    storage_summary: dict, include_keys: bool, all_keys: list[str],
    started_at: str, finished_at: str, run_id: str,
) -> dict:
    """Append the RESET_BOUNDARY audit entry. Uses audit.write_audit
    so the SHA-256 hash chain stays intact (prev_hash points at the
    last pre-wipe entry; subsequent writes will chain on top of this
    new entry's hash)."""
    from audit import write_audit
    after = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "executed_by": executed_by,
        "started_at": started_at,
        "finished_at": finished_at,
        "note": note,
        "collections_wiped": {
            c: {"deleted": v.get("deleted", 0), "failures": v.get("failures", 0)}
            for c, v in per_collection.items()
        },
        "files_deleted": storage_summary.get("deleted_count", 0),
        "files_failed": storage_summary.get("failed_count", 0),
        "files_skipped": storage_summary.get("skipped_count", 0),
        "hash_chain_continues": True,
    }
    if include_keys:
        after["storage_keys_deleted"] = all_keys
    return await write_audit(
        tenant_id=tenant_id, table_name="system",
        op="RESET_BOUNDARY",
        record_id=run_id, before=None, after=after,
        actor_id=executed_by, request_id=f"migration_006_{run_id}",
    )


# =====================================================================
# Main
# =====================================================================

async def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    # ---- flag validation
    err = _validate_flags(args)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    mode = "DRY-RUN" if args.dry_run else "WIPE"
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()

    # Lazy imports so --help / AST work without backend deps installed.
    from motor.motor_asyncio import AsyncIOMotorClient
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass  # python-dotenv not installed -- env must come from shell

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        return 2

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    # Patch the database module globals so audit.write_audit (called
    # outside a request context) finds the same client/db.
    import database as _database
    _database._client = client  # type: ignore[attr-defined]
    _database._db = db          # type: ignore[attr-defined]

    # ---- scope validation
    tenant = await db.tenants.find_one(
        {"id": args.tenant_id}, {"_id": 0, "id": 1, "display_name": 1, "slug": 1},
    )
    if not tenant:
        print(f"ERROR: tenant {args.tenant_id!r} not found.", file=sys.stderr)
        return 2
    project = await db.projects.find_one(
        {"id": args.project_id, "tenant_id": args.tenant_id},
        {"_id": 0, "id": 1, "name": 1, "tenant_id": 1},
    )
    if not project:
        print(
            f"ERROR: project {args.project_id!r} not found in tenant "
            f"{args.tenant_id!r}.", file=sys.stderr,
        )
        return 2

    # ---- collect in-scope DR ids (used by via_dr collections)
    dr_ids = await _collect_in_scope_dr_ids(db, args.tenant_id, args.project_id)

    # ---- count phase: build per-collection examined counts
    per_collection: dict[str, dict] = {}
    for coll, scope_kind in WIPE_COLLECTIONS:
        f = await _scoped_filter(db, coll, scope_kind, args.tenant_id,
                                   args.project_id, dr_ids)
        if f.get("_no_match_"):
            n = 0
        else:
            n = await db[coll].count_documents(f)
        per_collection[coll] = {"examined": n, "deleted": 0, "failures": 0,
                                  "scope_kind": scope_kind, "filter": f}

    # ---- pre-collect storage keys (in case of wipe; in dry-run we
    # report counts only).
    photo_paths, tray_keys = await _collect_storage_keys(
        db, args.tenant_id, args.project_id, dr_ids,
    )
    all_keys = photo_paths + tray_keys

    # ---- print pre-wipe summary
    run_id = f"reset-{int(time.time())}"
    print()
    print("=== Migration 006 -- Test Data Reset ===")
    print(f"Mode: {mode}")
    print(f"Scope: tenant={args.tenant_id} ({tenant.get('display_name') or tenant.get('slug') or '?'})  "
          f"project={args.project_id} ({project.get('name') or '?'})")
    print(f"Executed by: {args.executed_by or '(not set -- dry-run)'}")
    print(f"Run id: {run_id}")
    print(f"Started: {started_at}")
    print()
    print("Per-collection counts (before wipe):")
    for coll, data in per_collection.items():
        print(f"  {coll:<28} examined={data['examined']:<6} "
              f"scope={data['scope_kind']}")
    print()
    print(f"Storage objects to delete:")
    print(f"  photo storage_path        count={len(photo_paths)}")
    print(f"  tray storage_key          count={len(tray_keys)}")
    print(f"  total                     count={len(all_keys)}")
    print()
    if dr_ids:
        print(f"In-scope DR ids: {len(dr_ids)} daily report(s) will be removed")
    print()

    # ---- DRY RUN: stop here.
    if args.dry_run:
        print("DRY-RUN complete. No rows or storage objects deleted. No audit entry written.")
        elapsed = time.perf_counter() - started_perf
        print(f"Duration: {elapsed:.2f}s")
        return 0

    # ---- CONFIRMATION PROMPT
    sentinel = f"WIPE-CONFIRMED-{args.tenant_id}"
    print(f"This will permanently delete the rows above for tenant "
          f"{args.tenant_id} / project {args.project_id}.")
    print(f"Audit log entries are PRESERVED; a single RESET_BOUNDARY entry will be appended.")
    print()
    print(f"Type the literal string  {sentinel}  to proceed:")
    try:
        typed = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("Confirmation aborted by EOF/interrupt.", file=sys.stderr)
        return 3
    if typed != sentinel:
        print("Confirmation mismatch. Aborted.", file=sys.stderr)
        return 3

    # ---- WIPE PHASE: per collection.
    print()
    print("Wiping collections...")
    for coll, data in per_collection.items():
        f = data["filter"]
        if f.get("_no_match_"):
            data["deleted"] = 0
            continue
        try:
            res = await db[coll].delete_many(f)
            data["deleted"] = int(res.deleted_count or 0)
        except Exception as e:
            data["failures"] += 1
            print(f"  ! {coll}: {type(e).__name__}: {e}")

    # ---- STORAGE PHASE: best-effort delete.
    storage_summary = {"deleted_count": 0, "failed_count": 0, "skipped_count": 0,
                        "per_path": []}
    if args.skip_storage:
        storage_summary["skipped_count"] = len(all_keys)
        print(f"\nStorage byte-deletion SKIPPED ({len(all_keys)} object(s) left as orphans).")
    else:
        print(f"\nDeleting {len(all_keys)} storage object(s)...")
        for path in all_keys:
            ok, reason = _delete_storage_object(path)
            storage_summary["per_path"].append({"path": path, "ok": ok, "reason": reason})
            if ok:
                storage_summary["deleted_count"] += 1
            else:
                storage_summary["failed_count"] += 1

    # ---- AUDIT PHASE
    finished_at = datetime.now(timezone.utc).isoformat()
    audit_entry = await _write_reset_boundary(
        tenant_id=args.tenant_id, project_id=args.project_id,
        executed_by=args.executed_by, note=args.note,
        per_collection=per_collection, storage_summary=storage_summary,
        include_keys=args.include_keys, all_keys=all_keys,
        started_at=started_at, finished_at=finished_at, run_id=run_id,
    )

    # ---- POST-WIPE SUMMARY
    print()
    print("=== Migration 006 -- Test Data Reset COMPLETE ===")
    print(f"Mode: WIPE")
    print(f"Scope: tenant={args.tenant_id}, project={args.project_id}")
    print(f"Executed by: {args.executed_by}")
    print(f"Started:  {started_at}")
    print(f"Finished: {finished_at}")
    print()
    print("Per-collection counts:")
    for coll, data in per_collection.items():
        print(f"  {coll:<28} examined={data['examined']:<6} "
              f"deleted={data['deleted']:<6} failures={data['failures']}")
    print()
    print("Storage files:")
    print(f"  deleted   = {storage_summary['deleted_count']}")
    print(f"  failed    = {storage_summary['failed_count']}")
    print(f"  skipped   = {storage_summary['skipped_count']}")
    print()
    print("Audit:")
    print(f"  RESET_BOUNDARY entry written  id={audit_entry.get('id')}")
    print(f"  prev_hash points at the last pre-wipe entry")
    print(f"  hash_chain_continues = True")
    print()
    print("Preserved (verified untouched):")
    for coll in PRESERVED_COLLECTIONS:
        try:
            n = await db[coll].count_documents({})
            print(f"  {coll:<28} {n} row(s)")
        except Exception as e:
            print(f"  {coll:<28} (count failed: {e})")
    elapsed = time.perf_counter() - started_perf
    print()
    print(f"Duration: {elapsed:.2f}s")
    return 0


# =====================================================================
# Entrypoint
# =====================================================================

async def run() -> None:
    """Sentinel async entrypoint matching the run_all_pending convention.
    The auto-runner does NOT call this -- 006 is allow-listed in
    MANUAL_ONLY there. If something does invoke run() directly we refuse
    rather than wipe by accident."""
    print(
        "ERROR: 006_test_data_reset is a manual operational migration. "
        "Run via:\n"
        "  python -m migrations.006_test_data_reset --tenant-id <id> "
        "--project-id <id> --dry-run\n"
        "Refusing to execute via the run_all_pending auto-runner.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
