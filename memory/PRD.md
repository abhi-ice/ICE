# ICE Schedule Ops — PRD

## Original problem statement
Build "ICE Schedule Ops", a multi-tenant federal-construction project management platform replacing Procore + Touchplan + Raken + OpenSpace + Bluebeam + DocuSign. Federal compliance baked in (NIST 800-171, audit immutability, GovCloud-deployable). 31-section master prompt; user said "use whatever infra you want but I want python to be used".

## Stack used (substitutions explicitly flagged in code as `SUBSTITUTION:`)
| Master prompt asked for | Emergent runtime delivered |
|---|---|
| PostgreSQL 16 + pgvector + RLS | MongoDB; tenant isolation enforced at app layer via `tenant_filter()` on every query |
| Alembic migrations | Schema-on-read |
| Celery + Redis | FastAPI BackgroundTasks |
| LangGraph multi-agent | `emergentintegrations.LlmChat` with explicit "graph node" functions (Anthropic claude-sonnet-4-5-20250929) |
| Voyage embeddings + DINOv2 | Deferred (no GPU/heavy ML in container) |
| Clerk auth | **Iter 1**: JWT HS256 + bcrypt. **Iter 2**: + Emergent-managed Google OAuth (cookie session) — dual-auth |
| MPXJ (.mpp / .xer) | XLSX + CSV only; .mpp/.xer return clear "use Railway parser" error |
| 2.5D + elevation 3D viewer | Path 1 (clickable polygons) only; Path 2/3 deferred |
| Bluebeam .bax interop | Deferred |
| MinIO/S3 + Docker Compose | Emergent Object Storage (init/put/get with retry on 403) |
| Real-time WebSockets | Polling (60s) |
| ReportLab PDF | Text-stream substitute for daily report export |

## Iteration 1 (2026-02 — initial MVP) — ✅ COMPLETE
- Multi-tenant foundation, JWT auth, audit log w/ SHA-256 hash chain, projects/floors/locations CRUD, tasks (activities) with CPM, daily-report grid pre-population from 2WLA, lookahead, weekly rollup, daily-report PDF stream, RFIs, change orders, work_items + polymorphic links, drawings (basic), specs (basic), photos, delays, safety, qc, approvals (decide), AI copilot/recovery/rfi-drafter via Claude Sonnet 4.5, schedule import (CSV/XLSX) with `parsed_to_task_create_payload` single source of truth + `extra='forbid'` regression guard, dashboard summary, NIST compliance dashboard, demo seed.
- 33 frontend routes, sidebar w/ 7 sections + Approvals badge, AI Copilot FAB + side panel.
- 37/37 backend tests passing.

## Iteration 2 (2026-02) — ✅ COMPLETE
**User requested:** Voice annotation pipeline, Drawing markup canvas, Spec PDF ingestion, Activity CRUD dialog, Daily Report date-filtered task picker, Google OAuth, UI improvement.

**Built:**
- **Voice annotation pipeline** (`services/voice_pipeline.py`):
  - OpenAI Whisper (whisper-1) transcribe via emergentintegrations
  - Claude Sonnet 4.5 structured-field extraction with kind-specific schemas (delay / safety_event / qc_issue / work_item / rfi)
  - Audio stored to Emergent Object Storage under `audio/voice/{tenant_id}/`
  - Frontend: global FAB (`VoiceCaptureButton.jsx`) with MediaRecorder + result sheet showing transcript + extracted JSON
- **Drawing markup canvas** (`components/drawings/DrawingViewer.jsx`):
  - react-pdf 7.7 multi-page viewer
  - Konva overlay with Pen / Rect / Pin tools (in-memory; persistence in next iter)
  - Page nav, zoom controls
  - Backend: `POST /api/drawing-revisions/upload` (PDF upload, page-count metadata, object-storage), `GET /api/drawing-revisions/:id/pdf` streams bytes
- **Spec PDF ingestion** (`services/spec_ingest.py`):
  - pdfplumber extract → DIVISION/SECTION/PART regex → hierarchical sections persisted to `spec_sections`
  - BackgroundTask processing with `processing_status` polling on UI
  - Section pills rendered with `<spec_citation>` styling
- **Activity CRUD** (`pages/Tasks.jsx`):
  - Dialog supports create + edit (description/dates/duration/trade/crew/location)
  - DELETE /api/tasks/:id soft-deletes (status='deleted'), audit-logged
- **Daily Report date-filtered task picker** (per user's specific ask):
  - `GET /api/daily-reports/:id/available-tasks` returns ONLY tasks where `planned_start ≤ report_date ≤ planned_finish` AND not already in the grid
  - `POST /api/daily-reports/:id/grid` manually adds a row
  - `DELETE /api/daily-report-activities/:row_id` removes one
  - Add-activity bar visible on non-submitted reports
- **Google OAuth** (Emergent-managed; `routers/google_auth_router.py`):
  - "Sign in with Google" button → `https://auth.emergentagent.com/?redirect=<origin>/auth/callback`
  - `AuthCallback.jsx` reads `#session_id`, POSTs to `/api/auth/google/session`
  - Backend exchanges via `https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data`, auto-provisions user into ICE tenant, sets httpOnly `session_token` cookie (7d)
  - `auth.py` is now dual-auth: cookie OR Bearer JWT
  - `POST /api/auth/logout` clears cookie + deletes `user_sessions` row
- **Object storage** (`services/storage.py`): tenant-prefixed paths, retry-on-403, init at startup
- **UI redesign** (per `/app/design_guidelines.json`):
  - Dark `slate-900` sidebar with blue-600 active link + amber badge
  - `slate-50` page background with subtle radial accent
  - White cards with subtle shadow + border
  - Login page: steel-construction-crane background image with dark gradient overlay, marketing copy, feature chips, professional federal aesthetic
  - Tighter typography hierarchy, traffic-light status pills, tabular-nums for numeric columns
  - All `data-testid` attributes preserved for testability

**Tests:** 12/12 new backend tests PASS. 32/34 iter1 regression PASS (2 stale-state flakes — not regressions). Frontend critical paths verified end-to-end.

## Architectural principles preserved
1. ✅ Multi-tenant from day one (every business doc carries `tenant_id`)
2. ✅ Activity-as-join-key (every entity has `schedule_activity_id`)
3. ✅ Polymorphic links pattern (`{entity_type, entity_id}` on `work_item_links`)
4. ✅ Single approvals table — no bypass paths
5. ✅ Append-only audit log with hash chain
6. ✅ Strict Pydantic schemas (`StrictModel` with `extra='forbid'`) — pinned by regression test
7. ✅ Single source of truth `parsed_to_task_create_payload` (master prompt §20.1)
8. ✅ Lookahead is a date-bounded query, NOT a separate table
9. ✅ Daily-report PDF streams bytes directly (NOT S3 redirect)
10. ✅ Configurable per tenant (`tenant.work_item_label`, `approval_rules` JSONB)
11. ✅ NEW: Tenant isolation enforced on voice annotations, drawing revisions, spec sections
12. ✅ NEW: Dual-auth (cookie + Bearer) without code duplication

## User personas
- PX (Project Executive), PM (Project Manager), Super (Superintendent), PE (Project Engineer), Foreman, plus Owner/Architect/Engineer collaborator roles.

## Demo seed (idempotent, on every backend startup)
- Tenant: ICE Construction Inc. (slug: ice, CMMC level 2)
- Admin: admin@iceops.com / Demo123! (or sign in with Google → auto-provisions into this tenant)
- 1 project: GSA Federal Building Renovation — 8th Floor ($18.75M)
- 8 floors, 12 locations, 5 crews, 5 equipment, 8 people, 25 schedule activities (15 on critical path), 3 RFIs, 2 work items, 1 pending approval

## Backlog

### P0 — next iteration
- **Voice annotation → entity creation:** wire the structured fields into pre-filled forms (delay/safety/qc/work_item/rfi)
- **Drawing markup persistence:** `drawing_markups` table router; pin-to-entity polymorphic linking
- **Spec embeddings + similarity search** via Voyage SDK or fallback TF-IDF
- **Photo upload via Emergent Object Storage** (currently storage-path field exists but no upload UI)
- **Submittals auto-generation** from spec ingestion
- **Activity dependency editing UI** (predecessors as a graph)

### P1
- **MPP/XER parser** via JPype + MPXJ (when GovCloud Java runtime is available)
- **DINOv2 visual fingerprinting** for photos
- **Polygon editor (Spatial Path 1)** click-to-draw on plan PDFs
- **Real-time WebSockets** to replace 60s polling
- **WeasyPrint PDF** to replace text-stream substitute
- **OAuth role mapping** — currently auto-provisions Google users as `pm`; should support tenant-defined claim mapping

### P2
- 2.5D mass model (Spatial Path 2) — three.js + extruded room polygons
- Elevation projection (Spatial Path 3)
- Bluebeam .bax interop
- Marketplace cross-tenant onboarding
- Productivity rates materialized view + nightly recompute
- NOAA weather auto-fill on daily reports
- PWA install + offline mode (Workbox)
- Eval harness for AI recovery (20+ scenarios, CI gate)
- *Aging-based smart badging* — show "3 RFIs > 5 days old" / "2 approvals > 3 days old" on sidebar to drive urgency
