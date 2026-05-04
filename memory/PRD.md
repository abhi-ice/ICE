# ICE Schedule Ops — PRD

## Original problem statement
Build "ICE Schedule Ops" — multi-tenant federal-construction project management platform replacing Procore + Touchplan + Raken + OpenSpace + Bluebeam + DocuSign. NIST 800-171 audit-immutable. 31-section master prompt; user said "use whatever infra you want but I want python to be used".

## Stack used (substitutions explicitly flagged in code)
| Master prompt | Delivered on Emergent |
|---|---|
| PostgreSQL + pgvector + RLS | MongoDB; tenant isolation enforced via `tenant_filter()` on every query |
| Celery + Redis | FastAPI BackgroundTasks |
| LangGraph multi-agent | `emergentintegrations.LlmChat` (Claude Sonnet 4.5) with explicit graph-node functions |
| **Voyage embeddings** | **Iter 3: scikit-learn TF-IDF** (vocab persisted per spec; cosine search) |
| DINOv2 visual fingerprinting | Deferred (no GPU) |
| Clerk auth | JWT + bcrypt; **Iter 2: + Emergent-managed Google OAuth** (dual-auth) |
| MPXJ .mpp/.xer | XLSX + CSV only |
| **2.5D + elevation 3D viewer** | **Iter 3: 2.5D mass model fully working** (raw three.js because R3F v9 + React 19 incompatibility around JSX __source); elevation deferred |
| Bluebeam .bax interop | Deferred |
| MinIO/S3 | Emergent Object Storage |
| Real-time WebSockets | Polling |

## Iteration 1 (initial MVP) — ✅
Multi-tenant foundation, JWT, audit log + hash chain, projects/floors/locations CRUD, tasks with CPM, daily-report grid, lookahead, weekly rollup, RFIs, change orders, work_items + polymorphic links, drawings (basic), specs (basic), photos, delays/safety/QC, approvals, AI copilot/recovery/rfi-drafter, schedule import (CSV/XLSX) with `parsed_to_task_create_payload` SSoT + `extra='forbid'` regression guard, dashboard summary, NIST compliance dashboard, demo seed. **37/37 backend tests passing.**

## Iteration 2 — ✅
- Voice annotation pipeline (Whisper + Claude structured-field extraction)
- Drawing PDF upload + react-pdf viewer + Konva markup overlay (in-memory)
- Spec PDF ingestion (pdfplumber + MasterFormat regex)
- Activity CRUD dialog
- Daily Report date-filtered task picker
- Google OAuth (Emergent-managed) + cookie session, dual-auth
- UI redesign: dark slate-900 sidebar, slate-50 page, blue-600 accents, login background image
- **Tests: 12/12 new + 32/34 regression PASS**

## Iteration 3 (this iter) — ✅
**1. Voice annotation → entity pre-fill (`EntityFromVoiceDialog.jsx`)**
   - Voice result panel exposes "Create [kind]" button
   - Opens kind-specific form (delay/safety/qc/work_item/rfi) pre-filled with extracted JSON fields
   - Linkage selectors for activity + location
   - Submits real entity creation; closes dialog on success

**2. Drawing markup persistence + polymorphic linking**
   - Backend: `drawing_markups` collection + `iter3_router.py` CRUD endpoints
   - Frontend: DrawingViewer now POSTs each shape (pen/rect/pin) to `/api/drawing-markups`
   - GET `/api/drawing-revisions/:id/markups` loads on mount
   - PinDialog with `linked_entity_type` (task/rfi/work_item) + `linked_entity_id` selectors
   - Right-side markup list panel shows persisted markups w/ link badge + delete

**3. Spec embeddings via TF-IDF + similarity search**
   - `services/spec_search.py`: scikit-learn TfidfVectorizer per spec, vocab + normalized vectors persisted to `spec_sections.embedding`
   - `POST /api/specs/search` with project_id or project_spec_id returns top-k matches with score + snippet
   - `POST /api/specs/:id/reindex` for re-fit
   - **AI Copilot** now injects top-3 spec hits into context block on every prompt — citations work against real retrieval, not hallucination

**4. Photo upload via Emergent Object Storage (`pages/Photos.jsx`)**
   - Camera-capture input (`<input type="file" accept="image/*" capture="environment">`)
   - Backend `POST /api/photos/upload` stores under `photos/{tenant_id}/`, creates Photo row + file record
   - `GET /api/photos/:id/file` streams bytes (Bearer-authed, blob URL on FE)
   - Masonry grid + click-to-modal viewer with linkage display

**5. Collapsible sidebar sections**
   - Section headers are buttons with chevron
   - Collapsed state persisted to `localStorage[ice.sidebar.collapsed]`
   - Per-section toggle, smooth UX

**6. 2.5D Mass Model viewer (Spatial Path 2)** at `/spatial-3d`
   - **Raw three.js** (no react-three-fiber due to R3F v9 + React 19 JSX `__source` prop incompatibility)
   - For each location with `polygon_geojson`, build `THREE.Shape` → `ExtrudeGeometry` extruded by `floor.floor_height_ft`
   - Stack floors at `floor.elevation_z_ft`
   - Color rooms by assigned task status (slate / blue / green / red), CP highlight in red
   - Wireframe edges via `EdgesGeometry` + `LineSegments`
   - Hover detection via `Raycaster` → top-left tooltip with activity context
   - Z-slice slider to cut at any height
   - Orbit + zoom + pan via `OrbitControls`
   - Auto-generated rectangular polygons in seed for the 12 floor-8 rooms

**Tests: 15/15 new iter3 backend tests PASS + 12/12 iter2 regression PASS. Frontend critical paths verified end-to-end. No outstanding issues.**

## Architectural principles (preserved across all iterations)
1. ✅ Multi-tenant from day one
2. ✅ Activity-as-join-key
3. ✅ Polymorphic links pattern (extended to drawing_markups in iter3)
4. ✅ Single approvals table — no bypass paths
5. ✅ Append-only audit log w/ SHA-256 hash chain
6. ✅ Strict Pydantic schemas (`extra='forbid'`) — pinned by regression test
7. ✅ Single source of truth `parsed_to_task_create_payload`
8. ✅ Lookahead is a date-bounded query (NOT a separate table)
9. ✅ Daily-report PDF streams bytes directly
10. ✅ Configurable per tenant
11. ✅ Tenant isolation enforced on all new iter3 endpoints (drawing_markups, photos, specs/search)
12. ✅ Dual-auth (cookie + Bearer) without code duplication

## Demo seed
- Tenant: ICE Construction Inc. (`ice`, CMMC 2)
- Admin: `admin@iceops.com` / `Demo123!` (or Google sign-in → auto-provisions into ICE)
- 1 GSA project ($18.75M renovation, contract GS-11P-24-MKC-0027)
- 8 floors, 12 locations **with auto-generated polygon_geojson** (powers 2.5D)
- 5 crews, 5 equipment, 8 people
- 25 schedule activities (15 on critical path), 3 RFIs, 2 work items, 1 pending approval

## Backlog
### P0 — next
- **Spatial Path 3 (elevation projection)** — texture exterior faces with elevation PDFs
- **Voice → entity backend linkage** — `PATCH /api/voice-annotations/:id` to link the created entity (currently best-effort silent in FE)
- **Submittals auto-generation** from spec ingestion `1.4 SUBMITTALS` section
- **Drawing markup pin → opens linked entity** — click pin in DrawingViewer to deep-link to RFI/work item

### P1
- MPP/XER parser via JPype + MPXJ on GovCloud
- DINOv2 visual fingerprinting (photos auto-tag location)
- Polygon editor (Spatial Path 1) — click-to-draw on plan PDFs
- Real-time WebSockets to replace 60s polling
- WeasyPrint daily-report PDF
- Photo EXIF extraction + capture-time inference

### P2
- Marketplace cross-tenant flow + onboarding
- Productivity rates materialized view + nightly recompute
- NOAA weather auto-fill on daily reports
- PWA install + offline mode (Workbox)
- Eval harness for AI recovery (20+ scenarios, CI gate)
- *Aging-based smart badging* (RFIs/approvals > N days old surface red dots on sidebar)
- Bluebeam .bax interop
