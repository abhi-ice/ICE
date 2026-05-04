# ICE Schedule Ops — PRD

## Original problem statement
Build a multi-tenant federal-construction project management platform replacing
Procore / Touchplan / Bluebeam / Smartsheet for GSA, USACE, NAVFAC,
and other federal agency builds. Original prompt requested PostgreSQL +
LangGraph + Celery + pgvector. The user explicitly authorised a substitution as
long as the stack remains Python and the functional intents are met. We
implemented the application using:

- **Backend**: FastAPI + Motor (async MongoDB driver) + Pydantic v2 + WeasyPrint
  + Pillow + imagehash + scikit-learn TF-IDF + emergentintegrations.LlmChat
  (universal Emergent LLM key) + Whisper (Emergent) + Open-Meteo
  (free public weather)
- **Frontend**: React 19 + TailwindCSS + shadcn/ui + Konva + raw three.js +
  react-pdf + react-router 7
- **Infra**: MongoDB collection-per-aggregate; supervisor-managed services;
  service-worker PWA; Emergent Object Storage for binary blobs

## Roles & personas
1. **Project Executive** (firm/tenant scope) — pipeline KPIs, P50/P80 finish
2. **Project Manager** (project scope) — daily ops, recovery, marketplace
3. **Field Superintendent** — daily reports, field issues, deliveries
4. **Foreman** — daily report row ownership, voice annotations, photos
5. **Voice User** — capture-only with PIN auth (planned)
6. **External / Read-only** — owners, sub-rep, GC PMs

## What's implemented (chronological)

### Iter 1 — MVP scaffold
- Multi-tenant auth (JWT + Emergent Google Auth optional), role-based
  permissions, audit-log hash chain
- Tenants, projects, locations (4-level: building → floor → zone → room),
  resources (crews, equipment, people, certifications)
- Master format trade taxonomy + cost-rate library + prevailing-wage support
- Activity-driven daily-report grid (date-filtered, polymorphic activity rows)
- Tasks CRUD + dependencies + lookahead query
- Approvals with rule engine + audit log
- 50 demo seed records

### Iter 2 — AI + Specs + Voice + Drawings
- Voice annotation pipeline (Whisper → Claude structured-field extractor)
- Spec PDF ingestion + section parsing + TF-IDF semantic search
  (substitution for pgvector)
- Drawing markup viewer (react-pdf + Konva overlay)
- 5 AI agents (copilot, RFI drafter, recovery, work-item assistant, change-order)
- 100% test pass

### Iter 3 — Persistence + Spatial 2.5D
- Drawing markups persisted (POST/PATCH/DELETE) with polymorphic
  pin → entity links
- Voice annotations CRUD + structured-field PATCH
- Spec embeddings (TF-IDF) cached per tenant
- Photo upload with Emergent Object Storage + thumbnail generation
- 2.5D mass-model viewer (raw three.js — R3F deferred due to React 19
  incompatibility)
- Collapsible sidebar
- 100% test pass

### Iter 4 — Spatial Path 3 + Submittals
- Spatial elevation projection (location.spatial_centroid + floor elevations
  → 3D coordinates)
- Drawing pin deep-link → entity side-panel (RFIs/work-items/tasks)
- Submittals CRUD + auto-generation from spec "1.4 SUBMITTALS" parsing
- Voice annotation PATCH (re-link / structured fields)
- Polygon editor (per location, per floor) with drawing-revision underlay
- 31/31 tests pass

### Iter 5 — Marketplace + Notifications + Productivity
- Marketplace cross-tenant postings (sub invitations, equipment, labor, materials)
- Notifications model + unread-count + mark-read + bell dropdown
- Global search (projects + tasks + RFIs + drawings + photos + submittals)
- Open-Meteo weather proxy (NOAA-equivalent)
- Productivity-rates aggregation (trade × location → hours-per-pct-point)
- Cost-rate library + cost-estimates with prevailing wage
- Work-item history/audit trail
- Approvals pending count → sidebar badge

### Iter 6 — Completing the master prompt (this iteration)
- **WeasyPrint PDF**: `/daily-reports/:id/pdf-rich` and
  `/projects/:id/weekly-rollup/pdf` stream true PDF bytes (replaces
  text-only stubs)
- **Photo EXIF + perceptual hash**: extracted automatically on upload
  (Pillow + imagehash) — substitution for DINOv2 visual fingerprinting.
  Hamming-distance ≤ 8 marks duplicates with confidence.
- **AI Recovery — apply suggestion**: `POST /ai/recovery/:id/apply` updates
  target task `planned_finish` for split/resequence/change_method
  categories; creates escalation work item for category="escalate"
- **Eval harness**: `GET /ai/recovery/eval-harness` scores past applied
  recoveries (predicted vs realized days, accuracy_score)
- **Aging dashboard**: `/aging` page with 4 cards + per-category detail
  tables; aging-summary feeds smart sidebar badges
- **Submittal ↔ Activity linkage**: `PATCH /submittals/:id/links` +
  `POST /projects/:id/submittals/auto-link-activities` (idempotent
  spec-section → task spec_section_codes match)
- **Spatial section view**: `POST /spatial/section` computes vertical
  slice for a 2-point cut-line; rendered as SVG in
  `SectionViewDialog`
- **Lookahead Gantt**: drag-to-reschedule (PATCH /tasks) inline on the
  Gantt timeline
- **Drawing markup expansion**: 9 tools (pen, rect, ellipse, arrow, line,
  cloud, text, pin, calibrate); calibration writes
  `drawings_spatial_metadata.scale_ft_per_px`
- **Marketplace UI**: full CRUD UI replaces stub
- **Productivity dashboard UI**: trade × location rates with day-window
  filter
- **Global search UI**: TopBar dropdown
- **NotificationsBell UI**: TopBar dropdown with mark-read
- **Smart sidebar badges**: pulled from `/aging-summary` (RFIs, submittals,
  work items, total aging count)
- **PWA manifest + service worker**: offline-mode for static assets

### Test results
- iter1.json: 100% (basics)
- iter2.json: 100% (voice/specs/drawings)
- iter3.json: 100% (persistence + spatial)
- iter4.json: 31/31 PASS
- iter5.json: **50/50 functional** (1 conditional skip — apply-flow has no
  unapplied suggestion in seed) + 100% testid coverage after fixes

## Known deferred / substituted
- **DINOv2** → imagehash pHash (deterministic, free, sufficient for dup detection)
- **MPP/XER parser** → CSV/XLSX schedule import works; .mpp/.xer returns
  helpful error (deferred — needs JPype + MPXJ JVM)
- **Bluebeam .bax** → deferred (proprietary format, no open spec)
- **Real-time WebSockets** → 60s polling on notifications & badges
  (sufficient for current scale)
- **LangGraph** → multi-step LlmChat flows handle the same intent
- **pgvector** → TF-IDF (lexical) + LlmChat for re-ranking when needed
- **Celery** → FastAPI BackgroundTasks (sufficient for ingest jobs)

## Backlog (P2)
- NIST 800-171 control mapping UI + audit-log hash-chain integrity check
- Polygon-suggester AI agent (Claude vision)
- OCR sheet-metadata extraction on drawing upload
- True XER/MPP parsing via MPXJ + JPype
- Voice-only PIN auth flow
- Marketplace acceptance + cross-tenant notification routing

## Architecture notes
- All MongoDB collections exclude `_id` in projections (Pydantic
  serialisation strict)
- All endpoints respect `tenant_filter()`; `Depends(require_tenant)`
- LLM calls use `emergentintegrations.LlmChat` with universal Emergent key;
  models pinned per agent (gpt-4o for ingest, claude-sonnet-4.5 for
  recovery, whisper for voice)
- 3 routers: `iter3_router` (persistence), `iter4_router` (submittals/
  spatial), `iter5_router` (marketplace/notifs/etc.), `iter6_router`
  (PDF/EXIF/section/aging-detail/recovery-apply/eval-harness)

## Test credentials
See `/app/memory/test_credentials.md`
- admin@iceops.com / Demo123!
- Tenant: ICE Construction (auto-resolved on login)
- Project: GSA Federal Building Renovation — 8th Floor (8 floors,
  12 locations, 25 activities, 18 submittals, 11 drawings, 2 RFIs,
  2 deliveries, 5 schedule activities, 3 RFIs, 2 work items,
  1 pending approval)
