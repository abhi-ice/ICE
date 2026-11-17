# Deploying ICE Site Ops to Railway (staging)

This is the exact setup for the **staging** environment on Railway, built from the `railway-migration` branch. Production uses the same steps later, with its own database (MongoDB Atlas) and its own bucket. No secret values appear here; only variable names.

Checked against docs.railway.com on 2026-11-15. Items marked **CHECK IN STAGING** could not be confirmed from the docs.

---

## 1. What runs where

| Service | What it is | Source folder (Root Directory) | Build file | Listens on | Health check |
|---|---|---|---|---|---|
| `api` | FastAPI backend | *(empty: repo root)* | `backend/Dockerfile` (set with `RAILWAY_DOCKERFILE_PATH`) | fixed `PORT=8000` | `/api/health` |
| `web` | The React site, served by Caddy, which also forwards `/api` and `/api/ws` to `api` | `/frontend` | `frontend/Dockerfile` (found automatically) | `PORT=8080` | `/healthz` |
| `digest-cron` | Sends the weekly digest trigger, then exits | `/cron/digest` | `cron/digest/Dockerfile` (found automatically) | nothing | none |
| `MongoDB` | Staging database with a copy of the data | Railway MongoDB template | | private only | |
| bucket (e.g. `ice-files-staging`) | File storage (S3-compatible) | Railway Storage Bucket, region `iad` (US East) | | | |

The browser only talks to the `web` address. `web` reaches `api` at `api.railway.internal:8000` over Railway's private network, and so does `digest-cron`. The `api` service needs no public address.

### Why the api builds from the repo root
The admin "download worker" button zips the `worker/` folder (`backend/routers/admin_settings_router.py`, which looks two folders above `routers/`, so `/app/worker`). A Root Directory of `/backend` would leave `worker/` out of the build. So `api` builds from the repo root with `backend/Dockerfile`.

### Which ignore file each build uses
- `api` (context = repo root): `backend/Dockerfile.dockerignore`. The Docker build engine (BuildKit) uses an ignore file named after the Dockerfile, placed next to it, **instead of** the root `.dockerignore`. It is an allow-list: only `backend/` and `worker/` go in, minus `worker/worker.env`, logs, `cache/`, `projects/`, tests and any `.env` files.
- The root `.dockerignore` is left as it was (it still leaves `worker/` out), so an Emergent build is unchanged.
- **CHECK IN STAGING:** Railway's docs do not say whether they honor the Dockerfile-specific ignore file. If they do not, the root `.dockerignore` applies; the build still succeeds (the Dockerfile copies `worker/` with a pattern that may match nothing), but the download button then answers "not found". Test: Settings, Microsoft Project connector, download the worker. If it says not found, tell the developer; the fix is a root `.dockerignore` that brings back `worker/` minus `requirements.txt`.
- `web` (context = `frontend/`): `frontend/.dockerignore` (leaves out `node_modules/`, `build/`, `.env.local`).
- The worker's `requirements.txt` is copied as a plain file but never installed. It lists pywin32, which only exists on Windows; installing it broke the Linux build on 2026-07-05.

---

## 2. Settings for each service (dashboard, service Settings tab)

These cannot all be set from the command line, so set them in the dashboard after the services exist. Do not use `railway.json` (Railway is ending support for it on 2026-12-01).

### `api`
- **Source:** GitHub repo, branch `railway-migration`.
- **Root Directory:** leave empty.
- **Builder:** Dockerfile (picked automatically once `RAILWAY_DOCKERFILE_PATH` is set).
- **Watch Paths:** `/backend/**` and `/worker/**`
- **Healthcheck Path:** `/api/health` (it never touches the database). For an outside uptime monitor use `/api/health/deep`, which also checks the database.
- **Replicas:** 1. Several features assume one process (live-update list, Friday report duplicate check, background loops). The start command already runs one uvicorn process.
- **Memory:** about 2 GB (Java, the 256 MB file cache, PDF-to-image work and 75 MB .mpp uploads).
- **No volume.** Nothing permanent is written to disk.
- **Public networking:** none needed.

### `web`
- **Source:** GitHub repo, branch `railway-migration`.
- **Root Directory:** `/frontend`
- **Watch Paths:** `/frontend/**`
- **Healthcheck Path:** `/healthz`
- **Public networking:** generate a Railway domain (or add the staging custom domain) pointing at port `8080`.

### `digest-cron`
- **Source:** GitHub repo, branch `railway-migration`.
- **Root Directory:** `/cron/digest`
- **Watch Paths:** `/cron/digest/**`
- **Cron Schedule:** `0 * * * 4,5,6` (every hour Thursday to Saturday, UTC, the same as Emergent). The api only sends the email when it is Friday 7 to 11:59 AM in each project's own time zone, and ignores repeats of the same hour.
- **On staging, leave the Cron Schedule empty** until you test the digest (the copied data has real superintendent email addresses). Trigger it once by hand with `railway cron run` while email goes only to you.
- **Restart policy:** Never. The script exits after one request (exit 1 if the api did not answer 2xx).

### `MongoDB` (staging only)
- Created from Railway's MongoDB template. **Set its image to `mongo:7.0`** in the service's Source settings: the backend's database driver (pymongo 4.5) supports MongoDB up to 7.0, not 8.
- Private only. To restore the staging copy, use the public proxy address Railway shows for the database (`MONGO_PUBLIC_URL`) from this PC with `mongorestore --gzip --archive`, then remove public access if you like.

### Bucket
- Created in region `iad` (US East). The region cannot be changed later.
- Its Credentials tab shows whether it uses virtual-hosted or path-style addresses. New buckets use virtual-hosted (`S3_ADDRESSING_STYLE=virtual`).

---

## 3. Variables (names only)

`${{...}}` are Railway reference variables: Railway fills them in from another service, so secrets are typed only once. Replace `ice-files-staging` with the bucket's actual name in Railway.

### `api`
| Variable | Value |
|---|---|
| `RAILWAY_DOCKERFILE_PATH` | `backend/Dockerfile` |
| `PORT` | `8000` |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `30` (lets saves in progress finish during a redeploy) |
| `TZ` | `UTC` |
| `LOG_LEVEL` | `INFO` |
| `MONGO_URL` | `${{MongoDB.MONGO_URL}}` |
| `DB_NAME` | the staging database name (for example the name used in the dump) |
| `STORAGE_BACKEND` | `s3` (always set it; without it, a stray `EMERGENT_LLM_KEY` would switch storage to Emergent) |
| `S3_ENDPOINT_URL` | `${{ice-files-staging.ENDPOINT}}` |
| `S3_BUCKET` | `${{ice-files-staging.BUCKET}}` (not `RAILWAY_BUCKET_NAME`, which is only the display name) |
| `S3_ACCESS_KEY_ID` | `${{ice-files-staging.ACCESS_KEY_ID}}` |
| `S3_SECRET_ACCESS_KEY` | `${{ice-files-staging.SECRET_ACCESS_KEY}}` |
| `S3_REGION` | `${{ice-files-staging.REGION}}` |
| `S3_ADDRESSING_STYLE` | `virtual` (or `path` if the bucket's Credentials tab says so) |
| `JWT_SECRET`, `JWT_ALG`, `JWT_EXPIRE_HOURS`, `BCRYPT_ROUNDS` | sign-in settings (staging may use its own `JWT_SECRET`; production must copy Emergent's exactly) |
| `PLATFORM_ADMIN_EMAILS` | your admin email(s) |
| `ALLOW_PUBLIC_SIGNUP` | leave unset (off) |
| `ANTHROPIC_API_KEY` | your own key (Claude) |
| `OPENAI_API_KEY` | your own key (speech-to-text) |
| `GEMINI_API_KEY` | your own key (voice memo) |
| `LIVE_VOICE_ENABLED` | as on Emergent |
| `EMAIL_PROVIDER` | `disabled` on staging (or a SendGrid sandbox) |
| `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`, `SENDGRID_FROM_NAME` | only if email is turned on |
| `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe **test** keys on staging |
| `WEBHOOK_CRON_SECRET` | a long random value (the cron service reads it by reference) |
| `MS_PROJECT_WORKER_TOKEN` | a staging-only token, different from production |
| `CORS_ORIGINS` | `https://${{web.RAILWAY_PUBLIC_DOMAIN}}` (always set it; unset means any site) |
| `INVITE_BASE_URL`, `PRODUCTION_URL`, `ALLOWED_FRONTEND_HOSTS` | `https://${{web.RAILWAY_PUBLIC_DOMAIN}}` (the staging site address, used in email links) |
| `FF_SLIDER_GATING`, `FF_PROGRESS_CASCADE`, `FF_ENTERPRISE_CPM`, `FF_SCHEDULE_MODE_2` | copy what Emergent has, or set them in Settings |

Never set on Railway: `EMERGENT_LLM_KEY`, `PHOTO_AI_DEV_MODE`, `ICE_DEMO_SEED`, `REACT_APP_BACKEND_URL` (if it is ever set on the api it must be the new site address, because it is an accepted email-link base). `JAVA_HOME` is already set inside the image (`/opt/java`); do not override it.

### `web`
| Variable | Value |
|---|---|
| `PORT` | `8080` |
| `REACT_APP_BACKEND_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` (the web service's own public address; the build stops with a clear message if it is empty). Changing it needs a rebuild. |
| `API_UPSTREAM` | `${{api.RAILWAY_PRIVATE_DOMAIN}}:${{api.PORT}}` (gives `api.railway.internal:8000`) |

### `digest-cron`
| Variable | Value |
|---|---|
| `API_URL` | `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:${{api.PORT}}` |
| `WEBHOOK_CRON_SECRET` | `${{api.WEBHOOK_CRON_SECRET}}` |

---

## 4. Order of creation with the Railway command line (CLI v4)

Run these from `C:\Users\admin\Desktop\ICE`. `railway login` opens the browser; you sign in yourself.

```sh
# 1. Sign in and create the project.
railway login
railway init --name ice-site-ops

# 2. Make the staging environment and switch to it.
railway environment new staging
railway environment staging

# 3. Staging database (then set its image to mongo:7.0 in the dashboard).
railway add --database mongo

# 4. Bucket in US East.
railway bucket create ice-files-staging --region iad

# 5. The three app services, empty for now (sources are attached in step 7).
railway add --service api
railway add --service web
railway add --service digest-cron

# 6. Variables. Plain values with "variable set"; secrets through --stdin so
#    they are not left in the shell history. --skip-deploys avoids a deploy
#    after every line.
railway variable set -s api RAILWAY_DOCKERFILE_PATH=backend/Dockerfile PORT=8000 \
  RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30 TZ=UTC LOG_LEVEL=INFO STORAGE_BACKEND=s3 \
  S3_ADDRESSING_STYLE=virtual EMAIL_PROVIDER=disabled --skip-deploys
railway variable set -s api 'MONGO_URL=${{MongoDB.MONGO_URL}}' \
  'S3_ENDPOINT_URL=${{ice-files-staging.ENDPOINT}}' 'S3_BUCKET=${{ice-files-staging.BUCKET}}' \
  'S3_ACCESS_KEY_ID=${{ice-files-staging.ACCESS_KEY_ID}}' \
  'S3_SECRET_ACCESS_KEY=${{ice-files-staging.SECRET_ACCESS_KEY}}' \
  'S3_REGION=${{ice-files-staging.REGION}}' \
  'CORS_ORIGINS=https://${{web.RAILWAY_PUBLIC_DOMAIN}}' \
  'INVITE_BASE_URL=https://${{web.RAILWAY_PUBLIC_DOMAIN}}' \
  'PRODUCTION_URL=https://${{web.RAILWAY_PUBLIC_DOMAIN}}' \
  'ALLOWED_FRONTEND_HOSTS=https://${{web.RAILWAY_PUBLIC_DOMAIN}}' --skip-deploys
#    One secret at a time, for example (repeat for each secret in section 3):
#    railway variable set -s api ANTHROPIC_API_KEY --stdin --skip-deploys
railway variable set -s web PORT=8080 'REACT_APP_BACKEND_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}' \
  'API_UPSTREAM=${{api.RAILWAY_PRIVATE_DOMAIN}}:${{api.PORT}}' --skip-deploys
railway variable set -s digest-cron 'API_URL=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:${{api.PORT}}' \
  'WEBHOOK_CRON_SECRET=${{api.WEBHOOK_CRON_SECRET}}' --skip-deploys

# 7. Public address for the web service (a free *.up.railway.app address).
railway domain -s web --port 8080
```

Single quotes keep the shell from touching `${{...}}`. In PowerShell, single quotes work the same way.

Then, in the dashboard, for each of `api`, `web` and `digest-cron`:
1. **Connect the source:** Settings, Source, connect the GitHub repo, branch `railway-migration`.
2. Set Root Directory, Watch Paths, Healthcheck Path, Cron Schedule and Restart policy as in section 2.
3. Deploy `api` first, then `web`, then `digest-cron` (only once you are ready to test the digest).

Before the first `api` boot, restore the staging data and confirm the `migrations` collection came across (see the migration plan, Phase 2).

**Deploying from this PC instead of GitHub** (for a quick test): `railway up -s api --detach` from the repo root, `railway up frontend -s web --path-as-root --detach`, `railway up cron/digest -s digest-cron --path-as-root --detach`. `railway up` respects `.gitignore`, so `worker/worker.env` and logs are not uploaded. Files uploaded from Windows have Windows line endings; the Dockerfiles cope with that (the cron script is cleaned during its build).

---

## 5. First checks after deploy

1. `api` logs: no "Index creation FAILED"; the startup lines name the storage backend (`s3`) and the AI key in use (names only).
2. `https://<web address>/healthz` answers `ok`; `https://<web address>/api/health` answers from the api through Caddy.
3. Sign in, open a daily report, and watch the browser's network tab: `/api/ws` connects as a WebSocket (status 101).
4. Admin: download the Microsoft Project worker zip (see "CHECK IN STAGING" in section 1).
5. Upload a schedule `.mpp` (starts Java), export a PDF (WeasyPrint and fonts), run drawing title-block reading (Tesseract and Poppler).
6. `railway cron run` on `digest-cron` (email going only to you) and check its log line shows `http=200`.

Then work through the full test list in the migration plan, Phase 2.
