# ICE Site Ops: Plan to Move from Emergent to Railway

Built from a read-only review of the code and official Railway and MongoDB pages (checked 2026-11-15), then corrected by two independent reviews: one checking every code claim, one checking risks and Railway/Atlas facts. Anything marked **ASSUMPTION** or **UNCONFIRMED** needs checking before you rely on it. No secret values appear here.

---

## 1. Summary

**Target setup:**
- **Your own address**, for example `app.<your-company-domain>`. DNS is pointed at Railway, with Cloudflare in front only if you want it.
  - **Railway "web" service** (frontend). It serves the built React site and forwards `/api` and `/api/ws` to the backend over Railway's private network, so the browser talks to one address only.
  - **Railway "api" service** (backend). FastAPI in a Docker image that includes Java, WeasyPrint libraries, Poppler, Tesseract and fonts. Health check at `/api/health` (backend/server.py:986-988). One copy only.
  - **Railway "digest-cron" service.** A tiny job that sends the weekly schedule digest call, replacing Emergent's cron (.emergent/crons.yml).
  - **Railway Storage Bucket** for files. It is S3-compatible and replaces Emergent's file store (backend/services/storage.py:17).
- **MongoDB Atlas** (your own account), M10 cluster in AWS us-east-1, with continuous backup.
- **MS Project worker** on this PC. Only `ICE_BASE_URL` in worker/worker.env changes.
- **Outside services, called directly:** Anthropic (Claude), OpenAI (speech-to-text), Google Gemini, SendGrid, Stripe, Open-Meteo.

**Total effort:** about 2 to 3 weeks of part-time work, roughly 8 to 12 working days:
- code changes: 3 to 5 days
- staging and testing: 3 to 4 days
- cutover: one evening
- watching it afterward: 1 week

**Rough monthly cost: about $95 to $130, plus AI usage** (plus about $10 to $20 extra while staging runs).

| Item | Monthly cost |
|---|---|
| Railway Pro ($20 plan with $20 usage included). Memory about $10/GB/month, CPU about $20/vCPU/month, $0.05/GB outbound — every photo and PDF goes out through the api | about $25 to $45 |
| Atlas M10 (about $57) plus continuous backup and data transfer to Railway | about $65 to $80 |
| Railway bucket | $0.015 per GB, a few dollars at most |
| Claude and speech-to-text | Billed to you directly now instead of through Emergent. Volume unknown (open question) |

Budget option: Atlas Flex ($8 to $30 a month) brings the total to about $45 to $80. Its only safety net is one daily snapshot. Use the free M0 or Flex tier for staging, never a paid cluster.

**Recommendation in one line:** move the app to Railway and the database to your own MongoDB Atlas M10. Give the app your own permanent domain. Copy every file out of Emergent storage before anything else is switched off.

**Why Atlas instead of Railway's own MongoDB:**
- Railway's MongoDB template is one database server with **no backups configured by default**. Its only backup is a volume snapshot, with no restore to a point in time. Its version and high-availability cost are UNCONFIRMED.
- Atlas M10 is fully managed and can restore to any minute.
- For daily reports that superintendents write every day, being able to undo to a point in time is worth the roughly $57 a month.
- Copy the data with a frozen `mongodump`/`mongorestore` (Phase 3). Atlas Live Migration is not worth it: it needs owner rights and firewall access on Emergent's side, Emergent's database is not confirmed to be Atlas, and users change address anyway, so near-zero downtime buys nothing.

---

## 2. What depends on Emergent today

| # | Item | Where in code | What replaces it | Effort |
|---|---|---|---|---|
| 1 | **File storage** (photos, drawings, specs, .mpp files, client forms, voice recordings, reports) | backend/services/storage.py:17, 28-35, 42-80; set up at server.py:529-538; migrations/006_test_data_reset.py:117 | New storage.py built on boto3 (already pinned, requirements.txt:11) pointing at a Railway bucket. Copy every file under the same path. | **L** |
| 2 | `emergentintegrations==0.1.0` package. Not on the public Python package index, so the build fails. | requirements.txt:24; imported at load time at iter5_router.py:26, ai_agents.py:23, copilot_tools.py:32, dr_template_export.py:41, dr_voice_extract.py:35, schedule_ai_review.py:11 | Delete it from requirements. Point every import at services/anthropic_compat.py, the existing replacement built on the official `anthropic` software kit. Today it is only switched in when ANTHROPIC_API_KEY is set (server.py:36-48). | M |
| 3 | AI features that switch themselves off when `EMERGENT_LLM_KEY` is empty | voice_pipeline.py:32-33 (also used for the Claude extraction at :139-141); photo_ai.py:88-104 (decided at load time), :1486-1522; schedule_diffs_router.py:96-98; schedule_proposals_router.py:875-876; schedule_ai_review.py:66-67; schedule_work_category.py:391-392; schedule_feedback.py:116-117; photo_ai.py:621, 629, 849-852 (no key means fake "development mode" results); ai_agents.py:32; copilot_tools.py:1615; dr_template_export.py:56-57; iter7_router.py:629; iter8_router.py:375 | Change each check to look at ANTHROPIC_API_KEY | S |
| 4 | Speech-to-text (Whisper) | services/voice_pipeline.py:32-39 | Official `openai` package (pinned, requirements.txt:77) with a new OPENAI_API_KEY | S |
| 5 | Stripe marketplace checkout and webhook | routers/iter7_router.py:55, 71-78, 123, 133, 179-195 | Official `stripe` package (pinned, requirements.txt:136). Add a new webhook signing secret variable. | M |
| 6 | Embeddings fallback key | services/embeddings.py:46-56 (off unless SPEC_EMBEDDINGS=neural, :26-29) | Leave it off. If it is ever turned on, use EMBED_API_KEY. | S |
| 7 | Weekly digest scheduler (the Friday 7 AM email) | .emergent/crons.yml; .emergent/cron/dispatch_webhook.sh:9, 62-71; endpoint at schedule_review_router.py:321-342 | Railway cron service that sends the same POST with a Bearer WEBHOOK_CRON_SECRET and a unique X-Webhook-Id | S |
| 8 | How the server starts (Emergent's supervisor on port 8001) | server.py:11-12; .emergent/emergent.yml | Dockerfile with `uvicorn server:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`, run from backend/ | M |
| 9 | Secrets injected by Emergent | backend/.env removed in commit c902cbb; .gitignore:103-105 | Railway service variables. Every variable is listed in section 3. | S |
| 10 | Frontend Emergent script, meta text, PostHog snippet | frontend/public/index.html:7, 32, 47-117 | Remove them. Keep PostHog only if you own that PostHog project. | S |
| 11 | Emergent visual-edits build package | frontend package.json devDependency; craco.config.js:84-98 | Remove it (it is only used by the dev server) | S |
| 12 | Backend address baked into the frontend at build time | frontend/src/lib/api.js:4-5; ws.js:6-8 | Build with REACT_APP_BACKEND_URL set to the full new address (section 3). Unset gives "undefined/api". | S |
| 13 | Email-link base addresses | auth_router.py:211-250, 622-681, 739-742, 272-274; schedule_digest.py:93-94 | Set INVITE_BASE_URL, PRODUCTION_URL and ALLOWED_FRONTEND_HOSTS to the new domain | S |
| 14 | Worker download zip is empty in a Docker build | `.dockerignore:1-5` leaves out worker/; admin_settings_router.py:123-142 returns 404 | Build the api from the repo root (Dockerfile at backend/Dockerfile) and copy worker/ in as plain files, minus worker.env, logs and cache/. Never pip-install worker/requirements.txt: pywin32 broke the Linux build and caused the 2026-07-05 outage. | S |
| 15 | Worker points at Emergent | worker/worker.env (ICE_BASE_URL); worker.env.example:8 | New address in worker.env. Update the example file. | S |
| 16 | Production database owned by Emergent | database.py:20-21; database name not in the repo | Your own Atlas cluster. You need the connection string or a dump from Emergent. | M (mostly waiting on Emergent) |
| 17 | Emergent text users can see, and comments | SystemPages.jsx:397-398; comments in DrGuidedDesktop.jsx:117, DailyReports.jsx:4835, index.css:2241, anthropic_compat.py:43, schedule_parser.py:3-6 (stale "no Java" note) | Edit the text | S |
| 18 | Emergent-only frontend dev setup | craco.config.js:11-22, 54-75 (frontend/plugins/health-check, ENABLE_HEALTH_CHECK, WDS_SOCKET_PORT) | Remove | S |
| 19 | Emergent built on ARM (.emergent/emergent.yml:2); Railway builds x86 | requirements.txt:59, 75, 80, 152 (jpype1, numpy, pandas, weasyprint) | Confirm these install on x86 in the Dockerfile | S |

---

## 3. Target setup on Railway

**Project and region**
- One Railway project with two environments, **staging** and **production**. Each environment has its own private network.
- Region: US East (Virginia), the closest to New York and New Jersey.
- Plan: Pro, for 30-day logs, unlimited team seats and more domains.

**Services (all from the one GitHub repo, each with its own root folder and watch paths)**

1. **api**, built from the **repo root** with Dockerfile path `backend/Dockerfile` and watch paths `backend/**` and `worker/**` (a `/backend` root folder would leave worker/ outside the build). Give it a fixed PORT, for example 8000, and bind uvicorn to `::` so private networking works on IPv4 and IPv6.
   - **Build:** a Dockerfile, not Railway's automatic builder. A Dockerfile is the predictable way to install:
     - Python 3.12 (ASSUMPTION: numpy 2.4.4 and pandas 3.0.2 at requirements.txt:75, :80 need 3.11 or newer)
     - a Java 17 runtime for the .mpp and .xer parser (schedule_parser.py:595-604)
     - Pango, Cairo, HarfBuzz, fontconfig and gdk-pixbuf for WeasyPrint
     - fonts-liberation and DejaVu fonts
     - poppler-utils (iter7_router.py:593, 680; iter8_router.py:327)
     - tesseract-ocr (iter7_router.py:681, 700)
     - JAVA_HOME set
   - **Do not use railway.json.** Railway stops supporting it on 2026-12-01. Use the Dockerfile plus the settings screen.
   - **Size:** 2 GB of memory. The Java runtime, a file cache of up to 256 MB (storage.py:106), PDF-to-image work and .mpp uploads of up to 75 MB all add up.
   - **One copy, one uvicorn worker.** Several parts assume a single process:
     - the WebSocket subscriber list (ws_router.py:23)
     - the Friday report's duplicate check (server.py:730)
     - every background loop in server.py:491-897
   - **Health check:** `/api/health`. Note it returns fixed text and never touches the database (server.py:986-988), so a green check can hide a wrong MONGO_URL. Add `/api/health/deep` with a database ping for the outside uptime monitor. Set RAILWAY_DEPLOYMENT_DRAINING_SECONDS to about 30 so saves still in progress can finish during a redeploy.
   - **No volume.** Nothing permanent is written to disk (only temporary files at schedule_parser.py:607 and voice_pipeline.py:44). A volume would also block zero-downtime deploys.

2. **web**, root folder `/frontend`
   - Builds with `yarn build` into `build/`.
   - Serves the site through Caddy with a custom Caddyfile that:
     - sends unknown routes to index.html, so the single-page app works
     - forwards `/api/*`, including WebSocket upgrades, to `api.railway.internal:8000` (the api's fixed port, not `$PORT`, which would be the web service's own)
   - Build with REACT_APP_BACKEND_URL set explicitly to the full new address, for example `https://app.<domain>`. Left unset it becomes "undefined/api"; empty gives a relative WebSocket address that older phones may reject.
   - Force `yarn install --frozen-lockfile && yarn build`: frontend/ has both package-lock.json and yarn.lock, and Railway may otherwise pick npm. Remove `@emergentbase/visual-edits` first, or every build downloads from Emergent (package.json:90).
   - **ASSUMPTION:** Railway's Caddy setup accepts a custom reverse-proxy Caddyfile. Check this in staging.
   - The fallback is two public addresses (`app.` and `api.`) with CORS_ORIGINS set.
   - Why one address is better: no CORS problems, and a single place where the phones' offline data lives.

3. **digest-cron**
   - A Railway cron service running a small curl image.
   - Schedule `0 * * * 4,5,6` in UTC, exactly as Emergent does (.emergent/cron/dispatch_webhook.sh uses UTC). The endpoint itself only sends when it is Friday 7–11:59 AM in each project's own timezone (schedule_digest.py:108-119).
   - It POSTs to `http://api.railway.internal:<port>/api/cron/schedule-digest` with `Authorization: Bearer ${{api.WEBHOOK_CRON_SECRET}}` and a unique `X-Webhook-Id` such as the date and hour. Repeats are already filtered out through db.cron_runs (schedule_review_router.py:321-342).
   - The process must exit when it finishes.

**Database**
- MongoDB Atlas M10 in AWS us-east-1, version 7.0. pymongo 4.5 supports up to 7.0, so do not use 8.x. MongoDB 7.0 reaches end of life on Atlas on 2027-08-31, so upgrade pymongo and motor before mid-2027 as a separate change.
- Continuous backup turned on.
- A separate small cluster or database for staging.
- Network access limited to Railway's outgoing addresses if possible. Otherwise allow all addresses but use a strong password.
- The app uses no transactions, change streams, file-in-database storage or Atlas-only search, so a plain copy works.

**File storage**
- Railway Storage Bucket: private, S3-compatible, $0.015 per GB a month, no download charges. Files keep being served through the backend as they are today.
- Use the same key layout as now, `ice-schedule-ops/{kind}/{tenant}/{uuid}.{ext}` (storage.py:149-152), so no database rows change.
- Separate buckets for staging and production.
- Cloudflare R2 is an equally good alternative if you would rather keep files with Cloudflare.

**Domains**
- Your own domain on the web service: a CNAME plus the TXT check record. Railway issues the certificate automatically.
- If Cloudflare stays in front, set SSL to **Full** (not Strict, not Flexible). Use a domain only one level deep.
- Suggestion: start with Cloudflare in DNS-only mode (grey cloud) at cutover, so there is one less moving part. Switch the proxy on later if you want it.

**Secrets (Railway variables; names only)**

| Group | Variables |
|---|---|
| Database | MONGO_URL, DB_NAME |
| Sign-in | JWT_SECRET (copy the current value exactly, see section 6), JWT_ALG, JWT_EXPIRE_HOURS, BCRYPT_ROUNDS, ALLOW_PUBLIC_SIGNUP (leave off), PLATFORM_ADMIN_EMAILS (set it, server.py:431-447) |
| AI | ANTHROPIC_API_KEY, OPENAI_API_KEY (new), GEMINI_API_KEY, LIVE_VOICE_ENABLED |
| Email | EMAIL_PROVIDER, SENDGRID_API_KEY, SENDGRID_FROM_EMAIL, SENDGRID_FROM_NAME |
| Links | INVITE_BASE_URL, PRODUCTION_URL, ALLOWED_FRONTEND_HOSTS (all set to the new domain). CORS_ORIGINS **always** set to the new domain: unset means "*" with credentials allowed (server.py:1042-1047). Never copy an old REACT_APP_BACKEND_URL onto the api: if set there it must be the new domain, because it is an accepted email-link base (auth_router.py:650, 677). |
| Payments | STRIPE_API_KEY, plus a new Stripe webhook signing secret |
| Scheduled job | WEBHOOK_CRON_SECRET |
| File storage | Bucket endpoint, bucket name, access key, secret key (new names chosen in Phase 1) |
| Other | TZ=UTC, LOG_LEVEL, JAVA_HOME, MS_PROJECT_WORKER_TOKEN (see below), AWS_SES_REGION (only if EMAIL_PROVIDER=ses). Never set: PHOTO_AI_DEV_MODE (forces fake photo AI results), ICE_DEMO_SEED |

- Feature flags and the worker token use the database value **only if an admin changed them in Settings**; otherwise they fall back to environment variables (feature_flags.py:44-47, ms_project_internal_router.py:162). Copy whatever Emergent has for FF_SLIDER_GATING, FF_PROGRESS_CASCADE, FF_ENTERPRISE_CPM, FF_SCHEDULE_MODE_2 and MS_PROJECT_WORKER_TOKEN, or set each in Settings before cutover.
- Leave out EMERGENT_LLM_KEY once Phase 1 is done.

**Logs:** kept 30 days on Pro. Railway has no built-in way to forward logs elsewhere. That is fine to start with. Add an outside log service later if needed.

**Backups:**
- Atlas continuous backup for the database.
- For the bucket, a weekly copy to a second location such as R2 or your own S3 (a simple scheduled sync). This is optional but recommended, because the bucket has no version history (UNCONFIRMED).

---

## 4. Phases

### Phase 0: Prepare accounts, access and the repo (1 to 2 days, plus waiting on Emergent)

- [ ] Create a Railway Pro workspace and connect GitHub. Give Admin access only to yourself.
- [ ] Create a MongoDB Atlas account and project. Create M10 (production) and a small staging cluster in AWS us-east-1, MongoDB 7.0.
- [ ] Get your own Anthropic, OpenAI and Gemini API keys, with billing on and spending alerts set.
- [ ] Confirm the SendGrid and Stripe accounts are yours. If Emergent created them, they need to be moved to you.
- [ ] **From Emergent** (through their support or deployment settings, not by driving their chat):
  - the production MONGO_URL and DB_NAME, or a full `mongodump`
  - the current values of JWT_SECRET, WEBHOOK_CRON_SECRET, EMERGENT_LLM_KEY and the other injected variables
  - whether you can deploy two last builds on Emergent: a backend with a write-freeze switch and the old-address frontend (both needed, Phase 3)
  - whether they give a **writable** database connection. Without it, rolling back after go-live is not possible (section 5)
- [ ] Choose and register the new domain, for example `app.<company>.com`.
- [ ] Measure the size of the data, read-only, against production:
  - `db.stats()`
  - a count of documents per collection
  - `db.files.aggregate([{$group:{_id:"$kind",n:{$sum:1},bytes:{$sum:"$size"}}}])`
  - a separate count of spec PDFs (`project_specs.pdf_url`) and client forms (`projects.dr_template_file_url`), which are not listed in `files`
- [ ] Check whether migrations 027 and 028 ("must be run on Emergent", backend/migrations/027_*.py:38, 028_*.py:32) have already been run. If not, run them on Emergent before the move, or decide to drop them.
- [ ] Read the `migrations` collection and confirm the reset job's record is there (services/one_time_migrations.py:15-16, 81-86). **If that record is missing on the new database, the first boot deletes every daily report and delay on project 6007bdf1-....**
- [ ] Make a working branch, `railway-migration`.

### Phase 1: Replace the Emergent-only code, on the branch, with tests (3 to 5 days)

- [ ] **Storage:** rewrite services/storage.py on boto3, keeping the same `put_object` / `get_object` / `make_path` functions and the 256 MB cache. Read the bucket settings from new variables. Remove the Emergent `/init` step from startup (server.py:529-538). Update migrations/006_test_data_reset.py:117.
- [ ] **Write-freeze switch:** a small backend middleware, controlled by a flag in runtime_settings, that returns 503 for POST, PUT, PATCH and DELETE (sign-in excepted). Phones always retry a 5xx (drOfflineSync.js:142), so queued saves stay safely on the phones instead of landing on Emergent after the final copy and being lost. Deploy it to Emergent before cutover. **If Emergent won't allow this deploy, it is a no-go.**
- [ ] **Remove the old reset job** (one_time_migrations.py:15-40). Required, not optional: it deletes every daily report and delay on project 6007bdf1 if its record is missing on the new database.
- [ ] **File copy script**, a one-off at `backend/scripts/copy_emergent_files.py`. It must:
  - collect every distinct path from `files.storage_path`, `photos`, `photo_tray`, `drawings`, `drawing_revisions` (both `storage_path` and `pdf_url`), `voice_annotations`, `recovery_reports` (`exhibit_artifacts[].storage_key`), `tia_generations`, `project_specs.pdf_url` and `projects.dr_template_file_url`
  - download each file with the old Emergent client and upload it under the **same key**
  - skip keys that already exist in the target
  - write a manifest of path, bytes and SHA-256, and compare it against `files.size`, `photos.sha256_hash` (field_events_router.py:1576-1585) and `recovery_reports.exhibit_artifacts.sha256`
  - be safe to run again and again
- [ ] **AI:**
  - Remove `emergentintegrations` from requirements.txt:24.
  - Change all 15-plus imports to `services.anthropic_compat`.
  - Delete the module swap at server.py:36-48.
  - Change every EMERGENT_LLM_KEY check listed in section 2, row 3, to ANTHROPIC_API_KEY.
- [ ] **Speech-to-text:** voice_pipeline.py:32-60 changes to `openai` `audio.transcriptions.create(model="whisper-1")`.
- [ ] **Stripe:** iter7_router.py changes to `stripe.checkout.Session.create` and `stripe.Webhook.construct_event` with the signing secret.
- [ ] **Clean requirements:** move black, flake8, mypy, pytest and isort to a separate requirements-dev.txt. Drop s5cmd, jq and litellm, unless you want neural embeddings.
- [ ] **Dockerfile** at backend/Dockerfile, built from the repo root, as described in section 3, with its own ignore file that keeps worker/ but drops worker.env, logs and cache/.
- [ ] **Frontend:**
  - remove the emergent-main.js script, the meta text, `@emergentbase/visual-edits` and the health-check plugin (craco.config.js:11-22, 54-75)
  - decide on PostHog
  - add the Caddyfile
  - edit SystemPages.jsx:397-398
- [ ] Update worker/worker.env.example:8.
- [ ] Add a startup log line showing which storage backend and which AI key are active, using names only, never values.
- [ ] **Tests:**
  - unit tests for storage put and get against a local S3 stand-in (for example MinIO or moto)
  - tests that every AI feature is enabled when only ANTHROPIC_API_KEY is set
  - a test that the app imports with no emergentintegrations installed
  - Keep the guard at tests/test_day1_round2_tests_safety.py:195-199: no Emergent host names in tests.
  - Run tests only against `test_`-prefixed databases (tests/conftest.py guard). **Never against the production MONGO_URL.**
- [ ] Code review. Do not merge to main, or do not trigger an Emergent sync, until cutover: Emergent only pulls when you ping it, but the next sync would pick up migration code and could break the live app.

### Phase 2: Staging on Railway with a copy of the data (3 to 4 days)

**Setup**
- [ ] Create the staging environment with the api, web and digest-cron services from the branch, the staging Atlas cluster and the staging bucket.
- [ ] Settings that keep staging from touching real people or real money:
  - `EMAIL_PROVIDER=disabled`, or a SendGrid sandbox. **Real superintendent email addresses are in the copied data.**
  - Stripe test keys.
  - digest-cron paused.
  - A different worker token. Rotate it on staging only after the restore.
- [ ] Copy the data: `mongodump --gzip --archive` from production, then `mongorestore` into staging. Use binary dump only, never JSON or CSV, so dates and the audit hash chain stay exact (audit.py:1-65).
- [ ] Confirm the `migrations` collection came across **before** the first boot.
- [ ] Run the file copy script from Emergent into the staging bucket. Check the manifest is 100% complete and the SHA-256 values match.
- [ ] First boot checks:
  - look for "Index creation FAILED" in the logs (server.py:109)
  - the health check passes
  - the bcrypt self-test passes

**Test every main flow** on a desktop, an iPhone and an Android phone, with a test superintendent account

- [ ] Sign in, sign out, and password reset (the email link shows the new domain). Invite a new user and accept the invite.
- [ ] Daily report: create it, add activities, save, submit, and create a no-work day.
- [ ] Photos: upload from the phone, view old photos copied from Emergent, photo AI gives real results (not "development mode").
- [ ] Drawings, specs and old .mpp files open, and the drawing title-block OCR works.
- [ ] PDF exports in both modes, plus recovery report and TIA exports. Check the fonts look right.
- [ ] Client-form export, the AI-filled Excel form (dr_template_export.py). Time it.
- [ ] MS Project worker: on this PC, run a **second copy** with its own folder, a staging worker.env and the staging token, **only while the production task is stopped or at a quiet hour**. Both copies start MS Project with DispatchEx (msp_worker.py:486, 1520), and running them at the same moment is untested.
  - Check claim, file download and upload, and results.
  - Check that the % complete pull matches.
- [ ] Schedule imports: .xlsx, .mpp and .xer (the Java parser starts), the AI review and the summary of schedule changes.
- [ ] Weather autofill: Open-Meteo is reachable from Railway (production had trouble before, RCA_4ae23940).
- [ ] AI copilot, voice memo (Gemini) and speech-to-text (OpenAI).
- [ ] WebSocket live updates: two browsers see each other's changes.
- [ ] **Offline queue:** put a phone in airplane mode, edit a daily report, add a photo and a voice note, reconnect, and confirm all three sync.
- [ ] Weekly digest: trigger digest-cron once by hand with email going to your own address only.
- [ ] Stripe test checkout and the webhook, if the marketplace is in use (open question).
- [ ] Download the worker zip from the admin page (it should no longer give 404).
- [ ] **Timing checks:**
  - note the longest requests: client export, schedule parse, large PDF exports
  - upload a 75 MB .mpp **on a slow connection**. Railway gives a request up to 15 minutes while data flows, cuts it after 5 minutes with nothing sent, and needs an upload to finish within 5 minutes. A 75 MB file on a 2 Mbps site connection takes about 5 minutes, so this is the real risk. WebSockets are exempt; the worker's 25-second long-poll is safe.
- [ ] Check `GET /api/audit-log/verify` for each company. Do **not** use `/audit-log/verify-chain`, which reads the wrong collection (iter7_router.py:545-560).
- [ ] Rehearse the full cutover once on staging, with a stopwatch, using a fresh dump. That gives you the real downtime figure.

**Go or no-go:** every box above is ticked, or you have consciously accepted what is not.

### Phase 3: Production cutover runbook

**Window:** a Saturday evening, for example from 6 pm Eastern. Superintendents are not filing reports then. Allow 3 hours and expect about 1 to 1.5.

**About "switching DNS":** today's address `field-ops-hub-21.emergent.host` belongs to Emergent, so it cannot be pointed at Railway. Cutover therefore means **moving users to the new address**. The steps below handle the phones' offline data for that reason (also section 6).

**One week before (T-7):**
- [ ] Production services set up on Railway with variables set. Bucket made, Atlas M10 made. **Create the api service but do not deploy it (or scale it to 0) until step 8**: a running api runs one-time jobs and background loops that write.
- [ ] Attach the custom domain now, with web serving a holding page, so the certificate is already issued before cutover night.
- [ ] Bulk file copy run into the production bucket. It can run for days; it is safe to repeat.
- [ ] Email or text superintendents: the new address, the date, "Make sure everything has synced by Saturday 6 pm", "You will sign in again at the new address", and "Delete the old home-screen icon and add the new one."

**Three days before (T-3):**
- [ ] Deploy the Emergent build with the write-freeze switch (off) and a banner: "ICE is moving to <new address> on Saturday." Phones must load this build while online before the freeze, because the old address's offline cache keeps serving the old build without signal (service-worker.js:10-23).
- [ ] Re-run the file copy.

**Cutover evening, in this order:**
1. [ ] **Freeze:** announce it. Stop the MS Project worker task on this PC (End Task in Task Scheduler).
2. [ ] Turn on the write-freeze flag on Emergent and confirm a test save gets 503. Note the exact freeze time.
3. [ ] Final `mongodump` of production, with the time noted.
4. [ ] `mongorestore --drop` into Atlas M10.
5. [ ] Confirm the `migrations` collection, including the reset-job record, and `runtime_settings` are there.
6. [ ] **Check counts:** compare document counts for every collection on both sides. Also compare counts per company for tasks, daily_reports, photos and audit_log, and compare the indexes.
   - The photo_ai_locks and ai_call_timings counts will differ. That is normal, because old rows delete themselves.
   - Look at `schedule_jobs` rows stuck in "running".
7. [ ] Run the file copy one last time for files uploaded since the last run. The manifest must be 100% complete.
8. [ ] Deploy the Railway api with the production variables and the **same JWT_SECRET** as Emergent. Then check:
   - the health check passes, **and** the logs show a successful database connection and runtime-settings load, and you can sign in (the health check alone doesn't touch the database)
   - no "Index creation FAILED" in the logs
   - photo AI is not in "development mode"
9. [ ] Confirm the custom domain's certificate (attached at T-7) is still valid.
10. [ ] **Repoint the worker:**
    - edit `ICE_BASE_URL` in worker/worker.env
    - restart the "ICE MS Project Worker" task, because settings are only read at start (load_config, msp_worker.py:145-172)
    - the token stays the same if it was generated in Settings; if production uses the environment variable, copy MS_PROJECT_WORKER_TOKEN
    - check the worker log for a successful claim. A 401 means the token is wrong, a 503 means the server has no token (msp_worker.py:1434-1439).
11. [ ] **Smoke test, 20 minutes:**
    - sign in
    - open a real project
    - view old photos and drawings
    - create and delete a test daily report on a test project
    - one PDF export and one client-form export
    - weather autofill
    - send yourself a password-reset email
    - an MS Project sync round trip
    - WebSocket live update
12. [ ] **Go-live point.** From here, real writes happen on Railway and rollback becomes harder (section 5).
13. [ ] Deploy the old-address frontend aimed at the Railway backend (section 6), so queued items on phones drain to Railway. In the same Emergent deploy, set EMAIL_PROVIDER=disabled and the Emergent cron to `enabled: false`, so Emergent stops emailing superintendents (its Friday digest and recovery report would otherwise keep sending stale data, server.py:723-795).
14. [ ] Turn digest-cron on.
15. [ ] Send the "We're live at <new address>" message, including: "Before you go to site, sign in at the new address on Wi-Fi and open each of your projects once." The new address starts with no offline copy (service-worker.js:45-60).

**Keep Emergent's data untouched for 14 days.** Its emails and cron are off. Do not delete its database, files or account, and do not cancel billing. Do not let anyone write to it.

### Phase 4: After cutover

- [ ] **First week:** read the api logs every day, especially for 5xx errors, timeouts and memory. Watch Railway's memory graph and raise the limit if it nears 2 GB.
- [ ] Have someone check the first Monday morning of daily reports live with superintendents, and confirm each one opened the new address online before going to site.
- [ ] Check the first Friday-morning digest emails arrive, the first Friday recovery report runs, and the 6-hourly MS Project impact push.
- [ ] Set up an uptime check on `/api/health/deep` with any free outside monitor that texts or emails you. Railway's health check runs only during deploys.
- [ ] **Test a restore** within 2 weeks: restore Atlas to a point in time into a scratch cluster and open it. Restore a few bucket files from the second copy.
- [ ] **Day 14+:** cancel the Emergent production deployment, and only after the file manifest shows 100% and a full week of reports ran cleanly. Before that, take one final Emergent database dump and keep it cold for 90 days.
- [ ] **Rotate secrets.** Old secrets are in git history at 649babf (docs/day1-risk-audit-2026-10-25.md:192, :748-767). Put **new** SendGrid, Stripe and Gemini keys into Railway at step 8, then revoke the old ones once Emergent's email and cron are off — revoking earlier breaks the live Emergent app. Revoke EMERGENT_LLM_KEY and the old ANTHROPIC_API_KEY once Emergent is closed.
  - Rotate JWT_SECRET **after** the old Emergent address is retired. That signs everyone out once, on purpose.
  - Rotate the worker token from the admin page and update worker.env.
- [ ] **Cost check at day 30:** Railway usage, Atlas and AI bills compared with the estimate. Decide whether M10 is right-sized.
- [ ] Clean the repo in a separate commit: remove the `.emergent/` folder, comments that mention Emergent, and fix the stale comment at .gitignore:41-48.

---

## 5. Rollback plan

| When | What a rollback means | How |
|---|---|---|
| Before step 12 (no real writes on Railway yet) | Easy | Stop the Railway api. Set worker.env back to the old address and restart the task. Tell superintendents to keep using the old address. Emergent's data has not changed since the freeze. |
| Within 14 days after go-live | **Only if Emergent gave a writable database connection (Phase 0).** Otherwise the real rollback is fixing forward on Railway plus Atlas point-in-time restore. | Freeze again. `mongodump` from Atlas, then `mongorestore --drop` into the Emergent database (needs Emergent's database access). Copy files uploaded after cutover back to Emergent storage (the file script run in reverse; write it in Phase 1 for exactly this). Point the worker back. |
| After Emergent is turned off | No rollback to Emergent | Use Atlas point-in-time restore or the bucket second copy to recover from bad data, not a platform switch. |

**Rollback triggers (before step 12 only):** any of these that cannot be fixed within 2 hours:
- sign-in failures
- daily report save or submit failures
- missing files
- a count mismatch

---

## 6. Risks and how each is handled

| Risk | How it is handled |
|---|---|
| **Data loss in the copy** | Binary `mongodump`/`mongorestore` only. Compare counts per collection and per company. Check the audit hash chain with `/api/audit-log/verify`. Keep Emergent untouched for 14 days. Take a final cold dump. |
| **One-time job deletes real reports.** If `migrations` is not copied, the reset job wipes project 6007bdf1-... daily reports and delays (one_time_migrations.py:15-40). | Remove the job in Phase 1 (required), and still check `migrations` before the first boot. |
| **Files lost when Emergent closes.** No listing exists; the database is the only record (storage.py:4-5). | Copy every path found in the database. Compare the manifest with SHA-256. Do not cancel Emergent until it is 100%. Keep the EMERGENT_LLM_KEY value until then. |
| **Queued saves landing on Emergent after the final copy.** Phones drain queues whenever they have signal; a save that reaches Emergent after the dump gets a 200, is cleared from the phone, and is lost. | The write-freeze switch (Phase 1) returns 503, which phones keep retrying. Then the old-address build aimed at Railway drains them to Railway. |
| **Phones with unsent offline edits on the old address.** Queues and the sign-in token are stored per website address (drOfflineSync.js:36, 230; drPhotoQueue.js:41; drVoiceQueue.js:32; drNoteQueue.js:24; auth.js:5). The new address cannot see them. | 1) Tell superintendents to sync before the freeze. 2) **Best option, if Emergent allows one last deploy:** rebuild the old Emergent frontend with REACT_APP_BACKEND_URL set to the new Railway address and allow the old address in CORS_ORIGINS. Leftover queued items on old phones then send themselves to Railway. Because JWT_SECRET is copied unchanged, their old sign-in tokens still work. 3) Otherwise, put a "we moved" page on the old address. Write down any superintendent who reports missing work and recover it by hand. |
| **File links stored with the old host** | Paths are stored as keys (`ice-schedule-ops/...`), not full Emergent addresses (storage.py:149-152), and are served by the backend. Copied under the same key, they work unchanged. **Check:** query `project_specs.pdf_url` and `projects.dr_template_file_url` for any value starting with `http`. If any do, rewrite them in a small script. |
| **Email links point at the old host** | Set INVITE_BASE_URL, PRODUCTION_URL and ALLOWED_FRONTEND_HOSTS. Test the reset and invite links in staging and production. Reset links sent before cutover expire after 30 minutes anyway (auth_router.py:606). |
| **Time zones** | Everything is stored in UTC and the app does not rely on the server's timezone. Set TZ=UTC. Railway cron runs in UTC, same as Emergent's dispatcher, and the digest endpoint picks Friday 7 AM site time per project. The Friday recovery report fires after 21:00 UTC regardless of host (server.py:723-795). |
| **Secrets** | Every value goes into Railway variables, never into the repo. Only Admins can see variables (Railway's Member role can see them too, so give that role sparingly). Rotate after cutover as in Phase 4. The Gemini key is handed to the browser (dr_voice_router.py:920-935). That is a known, separate security item, not part of this move. |
| **Everyone signed out** | Copy JWT_SECRET exactly. Without it, a random secret is made on every restart (auth.py:52-67). Superintendents still sign in once at the new address, because the token is stored per address. Warn them ahead and make sure password reset works. |
| **Downtime for superintendents** | A Saturday-evening freeze of about 1 to 1.5 hours, measured in the staging rehearsal. On Railway later, redeploys switch over only after the health check passes, with 30 seconds of draining. |
| **Long requests cut off** | Railway allows 15 minutes while data flows, 5 minutes with nothing sent, and 5 minutes to finish an upload. Measure in staging; big .mpp uploads on slow site connections are the real risk. |
| **Missing system libraries** (PDFs, OCR, .mpp parsing) | A Dockerfile with explicit packages. Emergent ran on ARM and Railway is x86, so confirm jpype1, numpy, pandas and weasyprint install. Every PDF, OCR and .mpp flow is tested in staging. |
| **Emergent keeps emailing superintendents after cutover** | Its cron and email are switched off in the step-13 Emergent deploy. |
| **More than one copy of the backend breaks live updates and doubles background jobs** | Fixed at 1 copy. Scaling out later is a separate project: move the loops to cron services and the WebSocket fan-out to a shared message service. |
| **AI costs now billed directly** | Spending limits and alerts on the Anthropic and OpenAI accounts from day one. |
| **Staging sends real emails to superintendents** | EMAIL_PROVIDER=disabled on staging, digest-cron paused, Stripe test keys. |

---

## 7. Fixes to keep separate from the move

**The weather design fix and the no-work-day display bug** should each go out as their own change, **not** inside the migration branch.
- Weather: auto-fill only tries once when a report is opened, fails silently on phones, and can never fill after submission (Nov 5 and Nov 6 VA 6B reports are blank).
- No-work day: the reason and note are saved but shown nowhere, and Daily notes can't be added after submission.

Why keep them apart:
1. **Clear cause.** If something looks wrong on the Monday after cutover, you need to know whether the move caused it or the fix did. Mixing them makes every problem twice as hard to trace.
2. **Clean rollback.** Rolling back the move should not also undo a bug fix, and the reverse.
3. **Weather may look different on Railway.** Production on Emergent could not reach api.open-meteo.com (RCA_4ae23940). The Cloudflare-related 422 workarounds (daily_reports_router.py:2075-2162) were built around Emergent's setup. Test weather on Railway staging first. The right design may change once the network problem is gone, so designing the fix on Emergent now could solve the wrong problem.
4. **The no-work-day bug is a display bug, separate from hosting.** It can ship on Emergent before the move, or on Railway after the first stable week.

**Suggested order:**
- the no-work-day fix: on Emergent now, or in week 2 on Railway
- the weather fix: after the staging weather test, on Railway in week 2

**Also keep separate:** the second audit checker that reads the wrong collection (iter7_router.py:545-560), the Gemini key being sent to the browser, and splitting the backend into several copies.

---

## 8. Open questions for you

1. **Domain:** what permanent address do you want, for example `app.<company>.com`? Is the company domain's DNS already on Cloudflare? Do you already have a custom domain on Emergent? If so, cutover becomes a true DNS switch and the offline-data risk mostly goes away.
2. **Emergent handover:** can Emergent give you the production MONGO_URL and DB_NAME, or a dump, and the current secret values? Can you still deploy to Emergent after the move, for the old-address plan in section 6? How much notice does cancelling need?
3. **Who owns SendGrid, Stripe, PostHog and the Atlas cluster today:** you or Emergent?
4. **Atlas tier:** M10 (about $57 a month, restore to any minute) as recommended, or Flex (about $8 to $30, one daily snapshot)?
5. **Is the Stripe marketplace in real use?** If not, the Stripe rewrite can wait and the feature can stay off.
6. **AI volume:** roughly how much Claude, Whisper and Gemini use per month? This is for the budget, since the Emergent credits go away.
7. **Freeze window:** is Saturday evening clear of all active sites, including night or weekend shifts?
8. **Who should get Railway access**, and in which role (Admin, Member or Deployer)?
9. **Migrations 027 and 028:** have they been run on production?
10. **Phones:** does the app show superintendents a clear "everything synced" sign? (Not found in the inventory.) If not, should a simple indicator be added before cutover so they can confirm nothing is left unsent?

**Main evidence files:** C:\Users\admin\Desktop\ICE\backend\services\storage.py, C:\Users\admin\Desktop\ICE\backend\server.py, C:\Users\admin\Desktop\ICE\backend\services\one_time_migrations.py, C:\Users\admin\Desktop\ICE\backend\requirements.txt, C:\Users\admin\Desktop\ICE\backend\services\anthropic_compat.py, C:\Users\admin\Desktop\ICE\.emergent\crons.yml, C:\Users\admin\Desktop\ICE\frontend\public\index.html, C:\Users\admin\Desktop\ICE\worker\msp_worker.py, C:\Users\admin\Desktop\ICE\.dockerignore