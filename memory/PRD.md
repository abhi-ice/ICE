# ICE Schedule Ops — PRD

## Original problem statement (verbatim summary)
Build "ICE Schedule Ops", a multi-tenant federal-construction project management platform replacing Procore + Touchplan + Raken + OpenSpace + Bluebeam + DocuSign. Federal compliance baked in (NIST 800-171, audit immutability, GovCloud-deployable). 31 sections of master prompt covering data model, API, agents, sidebar IA, drawings, spatial views, daily reports, voice annotation, visual fingerprinting, work-item threading, spec ingestion, recovery engine, productivity learning, cost rate library, approvals, marketplace, and full test coverage.

User said: *"use whatever infra you want but i want python to be used and all other infra u can choose as you want but should do and all the features to be as intended as we want in a prompt."*

## Stack used (with substitutions explicitly flagged in code)
| Master prompt asked for | Emergent runtime delivered |
|---|---|
| PostgreSQL 16 + pgvector + RLS | MongoDB; tenant isolation enforced at app layer via `tenant_filter()` on every query |
| Alembic migrations | Schema-on-read |
| Celery + Redis | FastAPI BackgroundTasks |
| LangGraph multi-agent | `emergentintegrations.LlmChat` with explicit "graph node" functions (Anthropic claude-sonnet-4-5-20250929) |
| Voyage embeddings + DINOv2 | Deferred (no GPU/heavy ML in container) |
| Clerk auth | JWT HS256 + bcrypt; `X-Tenant-ID` header for tenant switch |
| MPXJ (.mpp / .xer) | XLSX + CSV only; .mpp/.xer return clear "use Railway parser" error |
| 2.5D + elevation 3D viewer | Path 1 (clickable polygons) only — Path 2/3 deferred |
| Bluebeam .bax interop | Deferred |
| MinIO/S3 + Docker Compose | Not used (supervisor-managed) |
| Real-time WebSockets | Polling (60s) |

## What's been implemented (2026-02 / iteration 1)

### Backend (24 endpoints + 8 routers)
- **Auth**: register, login, me, list-tenants. JWT HS256 + bcrypt(12).
- **Tenancy**: every business doc has `tenant_id`; `tenant_filter()` helper applied to every query; `X-Tenant-ID` header overrides JWT default if user is member.
- **Audit log**: append-only with SHA-256 hash chain; `GET /audit-log/verify` walks the chain.
- **Projects + floors + locations**: full CRUD; tenant-scoped.
- **Tasks (activities)**: CRUD, bulk-delete, lookahead query (date-bounded over `tasks`, marks `lookahead_status`), CPM forward+backward pass triggered as BackgroundTask on date changes.
- **Daily reports**: pre-populates grid from in-window activities at create time; pct cascade back to task; `prev_pct_complete` snapshotted by applier (NOT by TaskCreate); PDF stream (text-format substitute for ReportLab); weekly rollup query.
- **Resources**: crews, equipment, people, certifications + cross-project availability.
- **Field events**: delays, safety_events, qc_issues, photos.
- **Procurement & docs**: RFIs (auto-numbered), change orders, work_items + polymorphic work_item_links, drawings, project_specs, spec_sections.
- **Approvals**: create, list, decide (approve/reject), pending-count with badge.
- **Imports**: schedule via `parse_schedule_file` → `parsed_to_task_create_payload` (single source of truth shared with future agent path) → `validate_payload` (regression guard).
- **AI agents**: copilot (project-context Q&A), recovery (5 suggestions w/ category/confidence/cost/time), rfi_drafter — all via Claude Sonnet 4.5 via Emergent universal key. Token usage tracked in `ai_usage_tracking`.
- **Dashboard summary**: 6 KPI tiles aggregated via single query each.

### Frontend (33 routes)
- **Login** with pre-filled demo creds
- **Dashboard** with 6 KPI tiles + recent activities + recent RFIs
- **Sidebar**: 7 sections, 30 items, Approvals badge polled every 60s, all matching master prompt §7
- **Top bar**: tenant switcher, project switcher, search, notifications, user menu
- **Tasks**, **Lookahead** (7/14/21/28-day window switcher), **Daily reports** (list + grid editor with inline edits, submit, export download), **Weekly reports rollup**
- **Project-scoped lists**: Photos, Delays, Safety, QC issues, RFIs, Deliveries, Drawings, Specifications, Submittals, Change orders, Work items, Locations
- **Resources**: Crews, Equipment, People, Certifications, Availability
- **Approvals** queue with Approve/Reject
- **Recovery** (Claude Sonnet 4.5 suggestion cards)
- **Imports** wizard with template download
- **Audit log** with hash-chain verify badge
- **Compliance** (NIST 800-171 control evidence)
- **AI agents** roster
- **Marketplace, Settings, Copilot FAB + side panel**

### Architectural principles preserved
1. ✅ Multi-tenant from day one (every business doc carries `tenant_id`)
2. ✅ Activity-as-join-key (every entity that represents construction work has `schedule_activity_id`)
3. ✅ Polymorphic links pattern (`{entity_type, entity_id}` on `work_item_links`)
4. ✅ Single approvals table — no bypass paths
5. ✅ Append-only audit log with hash chain (`audit.py::write_audit`, `verify_chain`)
6. ✅ Strict Pydantic schemas (`StrictModel` with `extra='forbid'`) — pinned by regression test in services/schedule_proposal.py::validate_payload
7. ✅ Single source of truth `parsed_to_task_create_payload` (master prompt §20.1)
8. ✅ Lookahead is a date-bounded query, NOT a separate table
9. ✅ Daily-report PDF streams bytes directly (NOT S3 redirect)
10. ✅ Configurable per tenant (`tenant.work_item_label`, `approval_rules` JSONB)

### Tests (37/37 backend passing on iteration 1)
- Auth + tenant isolation + cross-tenant guard
- Projects + tasks CRUD + CPM correctness
- Daily-report grid pre-populate + cascade + submit + PDF stream
- Schedule import: prev_pct rejection (extra='forbid'), .mpp substitution error, csv happy path
- Audit log immutability + hash chain growth
- AI copilot + recovery + rfi-draft integration with Anthropic
- Approvals approve/reject quorum logic
- Resources + work-item polymorphic links cross-tenant rejection

## User personas
- **PX (Project Executive)** — dashboards, approvals, marketplace
- **PM (Project Manager)** — schedules, RFIs, change orders, recovery, approvals
- **Super (Superintendent)** — daily reports, photos, field issues, lookahead
- **PE (Project Engineer)** — RFIs, submittals, drawings, specs
- **Foreman** — daily report grid (mobile-optimized)
- **Owner / Architect / Engineer** — collaborator roles (read-only RFIs/approvals)

## Backlog (P0/P1/P2)

### P0 — next iteration (high value, blocking)
- **Voice annotation**: capture → Claude transcribe + structured field extraction → entity creation pre-fill
- **Drawing markup canvas**: `react-pdf` + `konva` overlay; multi-page PDF upload with auto-split; revision tracking
- **Spec PDF ingestion**: pdfplumber → MasterFormat regex → embed via Voyage (or fallback to keyword search) → `<spec_citation>` rendering
- **Photo upload + EXIF**: real file storage via Emergent object storage integration
- **Activity CRUD UI**: create/edit task dialog (today only via API + import)

### P1 — substitution catch-up
- **MPP/XER parser**: integrate JPype + MPXJ when GovCloud Java runtime is available
- **DINOv2 visual fingerprinting**: ship `location_visual_fingerprints` with cosine similarity over numpy arrays
- **Polygon editor (Path 1)**: click-to-draw room boundaries on uploaded plan PDF
- **Real-time WebSockets**: replace 60s polling for approvals badge + daily-report grid presence
- **PDF export via WeasyPrint**: replace text-stream substitute

### P2 — feature depth
- **Spatial Path 2 (2.5D mass model)** — three.js + extruded room polygons stacked by floor
- **Spatial Path 3 (elevation projection)** — texture-mapped on exterior faces
- **Bluebeam .bax interop**
- **Marketplace cross-tenant flow with onboarding wizard**
- **Productivity rates materialized view** + nightly recompute (master prompt §16)
- **Cost rate library Excel bulk import**
- **NOAA weather auto-fill on daily reports**
- **PWA install + offline mode (Workbox)**
- **Eval harness for AI recovery** (20+ scenarios, CI gate)

## Demo seed (idempotent, runs on every backend startup)
- Tenant: ICE Construction Inc. (slug: ice, CMMC level 2)
- Admin: admin@iceops.com / Demo123!
- 1 project: GSA Federal Building Renovation — 8th Floor (contract GS-11P-24-MKC-0027, $18.75M)
- 8 floors, 12 locations on Floor 8
- 5 crews, 5 equipment, 8 people
- 25 schedule activities (full chain from Mobilization to Substantial Completion) with predecessors set so CPM resolves a real critical path
- 3 RFIs (1 in_review, 1 sent, 1 answered), 2 work items (one with concealed-condition example), 1 pending approval
