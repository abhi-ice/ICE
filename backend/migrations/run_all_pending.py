"""Bucket C3b Phase 5 — unified migration runner. Final list confirmed
in Bucket C3c Phase 6.

Runs every shipped migration in ascending numeric order. Each individual
script is already idempotent (per-record skip checks), so re-running this
runner against an already-migrated DB is a no-op besides reading every
collection once.

Order matters only weakly today (none of the migrations write to the same
collection in conflicting ways), but stable order is defensive: the
delays restructure (001) seeds linked_task_ids referenced by the gating
backfill (002) calibration logic, and 003 is photos-only.

Bucket C3c added no new migration files: the new collections
(schedule_update_diffs, cascade_snapshots, tia_generations) are created
by Mongo on first insert with no historical backfill needed (diffs and
snapshots are forward-going by design; TIA generations have no prior
data to lift). The _discover() guard catches a future addition that
isn't registered here.

Usage from backend/:

  python -m migrations.run_all_pending

Exits non-zero if any migration's `run()` raises or calls sys.exit(>0).
"""
import asyncio
import importlib
import os
import sys
import traceback
from pathlib import Path

# Allow `python -m migrations.run_all_pending` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Migration filenames start with digits (001_*, 002_*, 003_*) which makes
# them illegal as direct `import` statements. Use importlib.
MIGRATIONS = [
    "migrations.001_delays_restructure",
    "migrations.002_gating_entities_backfill",
    "migrations.003_photo_evidentiary_backfill",
    # Followup-fix 1+3 (2026-05-07) -- DR cascade orphans + gating
    # entity orphans + photo evidentiary backfill (Fix 8 retroactive).
    "migrations.004_orphan_cleanup",
    # Bucket D Phase 1 (2026-05-08) -- DR-as-parent. Drops orphan
    # events, backfills linked_dr_activity_ids on every event with a
    # daily_report_id, creates photo_tray rows from existing photos.
    "migrations.005_dr_as_parent",
]

# Files matching the [0-9][0-9][0-9]_*.py pattern that are intentionally
# NOT auto-run. The discovery check below treats them like "registered"
# so an operator's `run_all_pending` invocation doesn't fail loudly,
# but the runner never imports them. These are operational, one-shot
# scripts (data wipes, repair tools) that must always be invoked
# manually with explicit flags.
MANUAL_ONLY = {
    # Bucket D follow-up (2026-05-08) -- test-data wipe. Refuses to run
    # without --confirm-wipe + --tenant-id + --project-id, prompts for
    # an interactive WIPE-CONFIRMED-<tenant> confirmation, and writes a
    # RESET_BOUNDARY audit entry preserving the SHA-256 chain.
    "006_test_data_reset",
}


def _discover() -> list[str]:
    """Verify the hard-coded list matches what's on disk. If a new
    migration file landed without being added here, fail loudly so the
    operator knows the runner is stale.

    Files in MANUAL_ONLY are recognized but skipped -- they exist on
    disk for manual invocation only and must NOT be auto-run."""
    on_disk = sorted(
        f.stem for f in Path(__file__).parent.glob("[0-9][0-9][0-9]_*.py")
    )
    declared = sorted(m.split(".", 1)[1] for m in MIGRATIONS)
    extra = set(on_disk) - set(declared) - MANUAL_ONLY
    if extra:
        print(
            f"ERROR: migration file(s) not registered in run_all_pending: {sorted(extra)}",
            file=sys.stderr,
        )
        sys.exit(2)
    return MIGRATIONS


async def _run_one(modname: str) -> None:
    print(f"\n=== {modname} ===")
    mod = importlib.import_module(modname)
    if not hasattr(mod, "run"):
        raise RuntimeError(f"{modname} has no async run() entrypoint")
    await mod.run()


async def main() -> int:
    if not os.environ.get("MONGO_URL") or not os.environ.get("DB_NAME"):
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        return 2

    failures: list[str] = []
    for modname in _discover():
        try:
            await _run_one(modname)
        except SystemExit as se:
            # Individual scripts exit non-zero on hard failure; capture
            # rather than letting the first failure stop the runner —
            # operators will want to see all migrations' status.
            if (se.code or 0) != 0:
                failures.append(f"{modname} -> SystemExit({se.code})")
        except Exception:
            traceback.print_exc()
            failures.append(f"{modname} -> exception")

    print("\n--- SUMMARY ---")
    if not failures:
        print("All migrations completed cleanly.")
        return 0
    print(f"FAILED ({len(failures)}):", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
