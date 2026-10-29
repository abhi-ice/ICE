# RCA — SiteOps (field-ops-hub-21) deploy run b282b3fd

## Live URL
https://field-ops-hub-21.emergent.host

## TL;DR
Two separate things:
1. The ANTHROPIC_API_KEY secret-change redeploy **SUCCEEDED and is live** — the reported `trigger_indie: deploy_event_timeout` was a **false negative** (caller-side await timeout). No fix/resume needed.
2. The upstream merge **880f281 is NOT in the deployed image** — the secret-change deploy *reused* the pre-merge image. The `backfill-section-paths` startup job therefore never shipped and never ran.

## Evidence

### The reported "failed step" did not actually fail
`get_step_status(b282b3fd)` — all 6 steps green: wakeup_environment, build, mongodb_migrate, manage_secrets, deploy (19:51:29→19:52:26), health_check (19:52:26). Run status = success; it is the active/promoted run (`active_run_id = b282b3fd`). The `deploy_event_timeout` was the trigger await timing out on the caller side while the pipeline completed server-side and promoted.

### ANTHROPIC_API_KEY objective met
- `GET /api/health` → 200, body `{"status":"ok","service":"ice-schedule-ops"}`.
- Backend logs (filtered `401|schedule_feedback|AI call failed|anthropic`) after startup: **none**. Startup clean ("Application startup complete"). The refreshed live key is reaching the backend.

### Merge 880f281 NOT deployed
Run b282b3fd is `type=secret_reuse` / `trigger=secret_change` and reused image `sha256:24b46c2...`, which was built by run `fe12b529` (agentic redeploy, 19:29). Inspecting that build's source in an eph sandbox:
- `backend/services/one_time_migrations.py` contains ONLY `RESET_JOB_ID = "2026-10-29-reset-dr-and-name-activities"`.
- `grep backfill|section_path|SECTION_PATH_JOB` in that file → no match.
So the backfill startup job the merge is supposed to add is absent from the deployed image.

Confirming DB state (read-only, prod db `field-ops-hub-21-ice_schedule_ops`):
- `db.migrations` has only `2026-10-29-reset-dr-and-name-activities` (status `done`, ran once at 19:09 during run 6e880afb — run-once claim intact, did NOT rerun). ✅
- `2026-10-29-backfill-section-paths` doc is **absent**. ❌
- Project `6007bdf1-a09d-4de3-a55b-cb5117c64e36`: **231 tasks, 0 with non-empty `section_path`**. ❌
- No backfill/migration log line at pod startup.

## Root cause
The deploy that got committed was the ANTHROPIC_API_KEY secret change, which is an image-reuse deploy — it does not rebuild source. The image it reused predates merge 880f281, so the merged backfill migration was never built or run.

## Required actions
1. **E1 / main agent — redeploy from source (full rebuild).** Verify merge 880f281 is present in the current preview `/app` (`backend/services/one_time_migrations.py` should contain the `2026-10-29-backfill-section-paths` job), then trigger a normal redeploy that rebuilds the image. A secret-reuse / image-reuse deploy will NOT pick up the code.
2. **After that rebuild, re-run the verifications:** `db.migrations["2026-10-29-backfill-section-paths"]` present with `status=done` and `result.tasks_filled`; reset job unchanged (ran once); non-empty `section_path` count > 0 for project ...c64e36.

## Not an issue
The ANTHROPIC_API_KEY redeploy is live and healthy; the trigger timeout was cosmetic. The reset migration correctly did not rerun.
