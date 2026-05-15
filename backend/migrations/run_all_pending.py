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
    # Bucket D Corrective (2026-05-08) -- replace per-DR-scope
    # linked_dr_activity_ids with master-schedule-scope linked_task_ids.
    # Translates ids via daily_report_activities.schedule_activity_id,
    # drops the old field everywhere, drops legacy indexes. Idempotent.
    "migrations.007_dr_activity_to_task",
    # Bucket H Phase 1 (2026-05-08) -- Resource Leveling setup. Adds
    # required_trade/headcount/hours to tasks, default_headcount/
    # company_name/home_project_id to crews, and backfills 90 days of
    # crew_capacity + crew_demands. Pass E smoke-verifies gap
    # computation walks supply + demand cleanly. Idempotent.
    "migrations.008_resource_leveling_setup",
    # Bucket H Phase 3 (2026-05-08) -- per-project membership backfill.
    # Adds project_members rows for every tenant_user x project tuple
    # so the RBAC scoping rules have data to filter against. Admins
    # skipped (they're tenant-wide). Idempotent via unique index.
    "migrations.009_project_members_backfill",
    # Bucket F Phase 1 (2026-05-08) -- Photo AI setup. Stamps
    # ai_analysis_status="skipped" on pre-existing photo_tray rows
    # (no billable backfill), adds AI lineage fields on photos,
    # is_cui + ai_enabled on projects, monthly budget on tenants.
    # Idempotent via $exists checks.
    "migrations.010_photo_ai_setup",
    # Bucket F Phase 5 (2026-05-11) -- pull-forward Risk #1. Snapshots
    # the tray's ai_user_overridden flag onto each Photo tag row as
    # user_overridden_at_tag_time so gallery + TIA Section 8 indicators
    # don't drift when the tray is later edited. Idempotent via $exists.
    "migrations.011_photo_override_snapshot",
    # Bucket G Phase 1 (2026-05-11) -- Jigsaw Pre-fill UX setup.
    # Creates the field_suggestions collection + 5 indexes + writes a
    # single FIELD_SUGGESTIONS_INITIALIZED audit op per tenant. No
    # data backfill (suggestions are runtime). Idempotent via per-
    # tenant audit existence check.
    "migrations.012_field_suggestions_setup",
    # Bucket E Phase 1 (2026-05-11) -- Submittals SLA + register
    # depth. 14-day default A/E review SLA on every project, full
    # register-depth defaults on every submittal, opportunistic
    # aging backfill, indexes on submittals + submittal_review_
    # events. Single SUBMITTAL_SLA_INITIALIZED audit op per affected
    # tenant. Idempotent via per-field $exists checks + per-tenant
    # audit existence check.
    "migrations.013_submittals_sla_setup",
    # Bucket I (2026-05-11) -- Recovery Report Generator setup.
    # Creates the recovery_reports collection + 4 indexes. No data
    # backfill. Single RECOVERY_REPORTS_INITIALIZED audit op per
    # tenant. Idempotent via per-tenant audit existence check.
    "migrations.014_recovery_reports_setup",
    # Bucket J Phase 1 (2026-05-11) -- role enum extension. Enables
    # submittal_tech + scheduling_engineer as valid tenant_users.role
    # + project_members.role_on_project values. No automatic role
    # assignment; existing users keep current role. ROLE_ENUM_EXTENDED
    # audit op per tenant; idempotent.
    "migrations.015_role_enum_extended",
    # Google-Auth purge (2026-05-11) -- federal-contractor data-
    # confidentiality policy. Drops the user_sessions collection
    # (cookie-auth path removed), $unsets google-sourced fields on
    # users (auth_provider, picture), writes GOOGLE_AUTH_REMOVED
    # audit op per tenant. Idempotent at every step.
    "migrations.016_google_auth_purge",
    # Bucket D Addition 5 (2026-05-12) -- post-submit ADD flow.
    # Backfills post_submit_added=False, add_reason=None,
    # added_at_post_submit=None, added_by_post_submit=None on every
    # event collection + activity grid. Single
    # POST_SUBMIT_ADD_FIELDS_INITIALIZED audit per tenant. Idempotent
    # via $exists field guards + per-tenant audit existence check.
    "migrations.017_post_submit_add_flow",
    # CPM Part A (2026-05-15) -- predecessor schema with relationship
    # type + lag. Converts tasks.predecessors from List[str] (legacy
    # activity_code only) to List[dict] {code, type, lag_days}.
    # Bare-string entries default to FS+0 preserving current MSU
    # cascade behavior. PREDECESSOR_SCHEMA_MIGRATED audit per tenant;
    # idempotent via canonical-shape check + per-tenant audit guard.
    "migrations.018_predecessor_relationship_lag",
    # CPM Part J (2026-05-15) -- RFI per_task_impacts schema. Adds a
    # per_task_impacts: List[Dict] field to every RFI, default []. Lets
    # supers capture estimated schedule impact per linked task on RFIs
    # (parallel to delays' per_task_impacts array). Path 1
    # (compute_projected_finish_dates) and Path 2 (cumulative
    # remaining-float verdict) will walk this array on Parts J-4 / J-5.
    # RFI_PER_TASK_IMPACTS_BACKFILLED audit per tenant; idempotent via
    # $exists filter + per-tenant audit existence check. Backward-compat
    # preserved -- empty array = "no impact captured" = current pre-J
    # blocker-badge-only behavior.
    "migrations.019_rfi_per_task_impacts_backfill",
    # CPM Part K-1 (2026-05-15) -- Submittal per_task_impacts schema.
    # Mirrors migration 019 (RFI backfill) but for submittals. Adds
    # per_task_impacts: List[dict] field, default []. Lets the
    # detect_breaches helper (Part K-2) auto-suggest aging-day impacts
    # at the SLA-breach moment with source="system_suggested", which
    # the super can later edit to flip source to "super_overridden".
    # Path 1 (K-4) and Path 2 (K-5) walk over-SLA submittals'
    # per_task_impacts. SUBMITTAL_PER_TASK_IMPACTS_BACKFILLED audit op
    # per tenant; idempotent via $exists filter + per-tenant audit
    # existence check.
    "migrations.020_submittal_per_task_impacts_backfill",
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
