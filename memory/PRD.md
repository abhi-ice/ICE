# ICE Schedule Ops — PRD

## Original problem statement
Multi-tenant federal-construction project management platform replacing Procore + Touchplan + Raken + OpenSpace + Bluebeam + DocuSign. NIST 800-171 audit-immutable. 31-section master prompt; user said "use whatever infra you want but I want python".

## Stack & substitutions (flagged in code as `SUBSTITUTION:`)
| Master prompt | Delivered |
|---|---|
| PostgreSQL + pgvector + RLS | MongoDB; `tenant_filter()` on every query |
| Celery + Redis | FastAPI BackgroundTasks |
| LangGraph multi-agent | `emergentintegrations.LlmChat` (Claude Sonnet 4.5) with explicit graph-node functions |
| Voyage embeddings | scikit-learn TF-IDF (drop-in replacement) |
| DINOv2 | Deferred |
| Clerk | JWT + bcrypt + Emergent Google OAuth (dual-auth) |
| MPXJ .mpp/.xer | XLSX + CSV only |
| Spatial Path 2 (2.5D) | Raw three.js (R3F v9 + React 19 incompatible) |
| Spatial Path 3 (elevation) | THREE.CanvasTexture rendered via pdf.js |
| Bluebeam .bax | Deferred |
| MinIO/S3 | Emergent Object Storage |
| Real-time WebSockets | Polling (60s for badges) |
| ReportLab PDF | Text-stream substitute |

## Iter 1 (MVP) — ✅
Multi-tenant + JWT, audit log w/ SHA-256 chain, projects/floors/locations CRUD, tasks + CPM, daily-report grid, lookahead, RFIs, change orders, work_items + polymorphic links, drawings/specs (basic), photos/delays/safety/QC, approvals, AI copilot/recovery/rfi-drafter, schedule import w/ SSoT, NIST compliance dashboard, demo seed. **37/37 tests.**

## Iter 2 — ✅
Voice annotation (Whisper + Claude), DrawingViewer w/ markup overlay (in-memory), spec PDF ingestion, Activity CRUD, Daily Report date-filtered task picker, Google OAuth, UI redesign. **12/12 + 32/34 regression.**

## Iter 3 — ✅
Voice → entity pre-fill, drawing markup persistence + polymorphic linking, TF-IDF spec embeddings + AI copilot citations, Photo upload via Object Storage, collapsible sidebar, **2.5D Mass Model viewer (raw three.js)**. **15/15 + regression.**

## Iter 4 (this iteration) — ✅

### 1. Voice annotation linkage backend (`PATCH /api/voice-annotations/:id`)
Records `linked_entity_type` + `linked_entity_id` after the user creates an entity from the voice result. Frontend was already calling this; backend now serves it.

### 2. Submittals auto-generation (`services/spec_ingest.py::_autogenerate_submittals`)
- Detects `1.4 SUBMITTALS` paragraph in any spec section (regex: `^\s*1\.\s*4\s+SUBMITTALS\b`)
- Parses bulleted/lettered/numbered items underneath until next `PART 2`
- Creates Submittal rows with `auto_generated=true`, linked to the spec section via `spec_section_id` + `spec_section_code`
- Idempotent: re-ingestion does NOT duplicate (gate via `count_documents` on `auto_generated=true` for that section)
- **Submittals page rebuilt** (`pages/Submittals.jsx`) — auto-generated badge, status dropdown, manual create dialog
- **Run BEFORE `processing_status='complete'`** so consumers polling on complete see the autogen rows immediately (race fix from iter4 testing)

### 3. Drawings spatial metadata + Spatial Path 3 (elevation projection)
- `drawings_spatial_metadata` collection with face binding (drawing_type: plan / elevation_north/south/east/west / section / rcp / detail)
- `POST /api/drawings-spatial-metadata` upsert keyed on `(drawing_revision_id, page_number)` — no duplicates
- `GET /api/projects/:id/spatial-metadata` returns project-scoped bindings
- **Spatial3D.jsx** now renders elevation PDFs via:
  - `pdf.js` rendering the bound revision page to a HTML canvas
  - `THREE.CanvasTexture` mapped to a rectangular plane positioned at the corresponding building face (north/south/east/west)
  - Plane sized to building bounding box (XZ extent + Y stack height)
  - Toggle on/off via `[data-testid='toggle-elev']`
  - **BindElevationDialog** UI to assign drawing revisions to faces

### 4. Pin click → deep-link side panel (`components/drawings/EntitySidePanel.jsx`)
- Pin in DrawingViewer is `onClick`-able when `linked_entity_type` + `linked_entity_id` exist
- Backend: `GET /api/entities/{type}/{id}` generic fetch endpoint with type→collection mapping (task/rfi/work_item/delay/safety_event/qc_issue/photo/delivery/change_order/submittal); 400 on unknown type, 404 on unknown id, tenant-isolated
- Side panel renders kind-specific fields (status/priority/dates/cost/etc) and a deep-link button to the entity's list page
- Markup list panel link badges are also clickable to open the same panel

### 5. Polygon Editor — Spatial Path 1 (`pages/PolygonEditor.jsx`)
- Pick floor → pick plan revision → pick location → click PDF to add vertices
- Konva overlay renders blue polygon fill + numbered vertices
- Undo / Clear / Save buttons; auto-loads existing polygon for the picked location
- `PATCH /api/locations/:id/polygon` persists the new ring + recomputes centroid
- Closes the 2D-spatial loop: vertices land in `location.polygon_geojson` which feeds the 2.5D Mass Model directly

**Tests: 16/16 iter4 backend PASS + 15/15 iter3 regression PASS = 31/31. Frontend critical paths verified end-to-end. No outstanding issues.**

## Architectural principles preserved
1. ✅ Multi-tenant from day one
2. ✅ Activity-as-join-key
3. ✅ Polymorphic links pattern (extended to drawing_markups + drawings_spatial_metadata in iter3/4)
4. ✅ Single approvals table — no bypass paths
5. ✅ Append-only audit log w/ SHA-256 hash chain
6. ✅ Strict Pydantic schemas (`extra='forbid'`)
7. ✅ Single source of truth `parsed_to_task_create_payload`
8. ✅ Lookahead is a date-bounded query
9. ✅ Daily-report PDF streams bytes directly
10. ✅ Configurable per tenant
11. ✅ Tenant isolation enforced on every iter3/4 endpoint
12. ✅ Dual-auth (cookie + Bearer)
13. ✅ NEW iter4: spatial metadata + auto-generated submittals + generic entity fetch all preserve tenant isolation

## Demo seed
- Tenant: ICE Construction Inc. (slug `ice`, CMMC 2)
- Admin: `admin@iceops.com` / `Demo123!` (or Google sign-in → auto-provisions into ICE)
- 1 GSA project ($18.75M), 8 floors, 12 locations w/ polygons, 5 crews, 5 equipment, 8 people
- 25 schedule activities (15 on critical path), 3 RFIs, 2 work items, 1 pending approval

## Backlog
### P0 — next
- **Calibration UI** for Path 1 (2-point scale → ft per px) so polygon coords map to real-world feet
- **Drawing markup type expansion**: ellipse, arrow, line, cloud, text — schema supports them; FE only has pen/rect/pin
- **Submittal log → schedule activity linkage** (when activity ties to a spec section, auto-relate to its submittals)
- **Voice → spec citation** in the structured field extractor (pull spec_section_id from TF-IDF top hit)
- **Procurement aging dashboard** (RFIs > N days, submittals > N days)

### P1
- MPP/XER parser via JPype + MPXJ
- DINOv2 visual fingerprinting
- Real-time WebSockets
- WeasyPrint daily-report PDF
- Photo EXIF extraction
- Section views in Spatial 3D (cut-line on plan → vertical slice rendered)

### P2
- Marketplace cross-tenant flow
- Productivity rates materialized view
- NOAA weather auto-fill
- PWA + offline mode
- Eval harness for AI recovery
- Aging-based smart sidebar badges
- Bluebeam .bax
