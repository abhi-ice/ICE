# ICE Schedule Ops — PRD

## Original problem statement
Multi-tenant federal-construction PM platform replacing Procore / Touchplan /
Bluebeam / Smartsheet. Original prompt: PostgreSQL + LangGraph + Celery +
pgvector. User authorised substitution; final stack:

- **Backend**: FastAPI + Motor (async MongoDB) + Pydantic v2 + WeasyPrint
  + Pillow + imagehash + scikit-learn TF-IDF + emergentintegrations.LlmChat
  + emergentintegrations.payments.stripe + Whisper + pdf2image + pytesseract
  + **JPype + MPXJ (Java JVM)** for true MPP/XER/Primavera parsing
- **Frontend**: React 19 + TailwindCSS + shadcn/ui + Konva + raw three.js
  (incl. THREE clip planes for 3D section view) + react-pdf + react-router 7
  + WebSocket real-time channel
- **Infra**: MongoDB collection-per-aggregate; supervisor; service-worker
  PWA; Emergent Object Storage

## Roles & personas
1. **Project Executive** — pipeline KPIs (admin)
2. **Project Manager** — daily ops, recovery, marketplace, master doc
3. **Field Superintendent** — daily reports, field issues, deliveries
4. **Foreman** — daily report rows, voice annotations, photos
5. **Voice User (PIN auth)** — capture-only mobile foreman view (READ-only AI)
6. **External / Read-only** — owners, sub-rep

## What's implemented (chronological)

### Iter 1-4 — MVP + Spatial Path 3
Auth, tenants, projects, tasks, locations, resources, daily reports, voice
annotations (Whisper + Claude), spec PDF ingestion, TF-IDF spec search,
drawing markup viewer, photo uploads, 2.5D mass model, spatial elevation
projection, drawing pin deep-link, auto-generated submittals, polygon
editor. **31/31 tests PASS in iter 4.**

### Iter 5 — Marketplace + Notifications + Productivity
Cross-tenant marketplace, notifications, global search, weather
(Open-Meteo), productivity rates, cost estimates, work-item history,
approvals badge.

### Iter 6 — Master prompt completion (a-g)
WeasyPrint PDFs, photo EXIF + perceptual hash, AI Recovery apply +
eval-harness, aging dashboard with smart sidebar badges, submittal ↔
activity linkage (manual + auto-link), spatial section view, lookahead
Gantt + drag, drawing markup expansion (9 tools), marketplace UI,
productivity dashboard, global search dropdown, NotificationsBell, PWA.
**iter 5.json: 50/50 PASS.**

### Iter 7 — Stripe + RAG + project gating + compliance + real-time
- Stripe Checkout escrow + milestone-bound auto-release
- Master doc with project-type gating (new / renovation / TI / infrastructure)
  injected into AI prompts
- Knowledge Base / RAG (TF-IDF over closed RFIs + work items)
- NIST 800-171 control mapping (16 controls × 8 families)
- Audit-log integrity verifier (SHA-256 hash chain)
- Polygon suggester (Claude vision via emergentintegrations)
- Real-time WebSockets
- Drawing OCR (pytesseract)
- Voice-only PIN auth + mobile foreman view
- Re-seed dev endpoint
**iter 6.json: 21/22 PASS.**

### Iter 8 — Final wave (current)
- **True MPP/XER parser** via JPype + MPXJ — Java JRE installed
  (`default-jre-headless`), `mpxj` package provides bundled jars, JVM
  starts lazily on first call (~2s warm-up). Per-extension reader map
  (MPXReader / PrimaveraXERFileReader / MPPReader / etc.) ensures
  reliability vs UniversalProjectReader.
- **AI Copilot READ + WRITE tool-use** — initial 11 tools.
- **Strict no-markdown reply rule** — system-prompt + `_strip_markdown` regex.
- **3D section view inside Three.js scene** — `localClippingEnabled=true`,
  THREE.Plane with angle/offset sliders.
- **Audit trail** for every copilot turn → `copilot_messages` collection.
**iter 7.json: 38 PASS / 1 SKIP.**

### Iter 9 — CRUD everywhere + onboarding (current)
- **Generic CRUD** for 11 entities via `/api/crud/{entity}/{id}` PATCH +
  DELETE (soft-delete with status='deleted'). Tenant-isolated. Allow-list
  in `iter8_router.ENTITY_CONFIG`.
- **Frontend `CrudList` framework** — single configurable component
  drives Crews / People / Equipment / Certifications / RFIs / Deliveries
  / Work-items / Change-orders / Locations / Delays / Safety-events /
  QC-issues. Dialog forms with field-level validation, inline edit/delete.
- **Copilot tool registry expanded to 21 tools** — added create_crew,
  create_person, create_equipment, create_floor, create_location,
  create_delay, create_safety_event, create_qc_issue, create_delivery,
  delete_entity, update_entity. Models match canonical Pydantic schemas
  (first_name + last_name, asset_tag + type + manufacturer, etc).
- **Standardised tool-result envelope** — every write tool returns
  `{created|updated|deleted: {resource: <type>, id, ...}}`. Frontend
  badges parse the envelope uniformly.
- **AI auto-detect rooms** — `POST /api/spatial/auto-detect-rooms`
  renders the plan PDF, asks Claude vision to identify every enclosed
  room, returns polygon coordinates, and (optionally) creates a
  Location record per room.
- **Onboarding workflow** — `GET /api/onboarding/getting-started`
  returns 12-step guide for AI engineers new to construction. Each step
  reports complete=true based on actual DB state. Frontend
  `/getting-started` page renders the checklist with deep-links and a
  prominent "AI auto-detect rooms" CTA.
- **Markdown stripping verified** under load.
**iter 8.json: 34 PASS / 1 SKIP / 2 FIXED post-report (person + equipment envelope shapes).**

## Cumulative test results
| Iter | Backend | Frontend | Notes |
|------|---------|----------|-------|
| 1    | 100%    | 100%     | MVP                              |
| 2    | 100%    | 100%     | Voice / specs / drawings         |
| 3    | 100%    | 100%     | Persistence + spatial 2.5D       |
| 4    | 31/31   | 100%     | Spatial Path 3 + submittals      |
| 5    | 50/50   | 100%     | Marketplace + notifs + prod      |
| 6    | 21/22   | 100%     | Master-prompt scope a-g          |
| 7    | 38/39   | 100%     | Stripe + RAG + gating + 3D cut   |

## Known deferred / substituted
- **DINOv2** → imagehash pHash
- **Bluebeam .bax** → deferred (proprietary)
- **Stripe Connect Express** → Stripe Checkout (escrow ledgered, payouts
  recorded as ledger rows + notifications, not actual Stripe Transfers)
- **LangGraph** → multi-step LlmChat sequences
- **pgvector** → TF-IDF + LlmChat for re-ranking
- **Celery** → FastAPI BackgroundTasks
- **Sheet-classifier ML for atypical title-blocks** → multi-region
  pytesseract heuristic suffices for standard architectural sheets

## Backlog (P2)
- Stripe Connect Express full onboarding for actual sub payouts
- Active probe of NIST evidence_endpoints (currently mapping-only)
- Bluebeam .bax import (no public spec)
- Sheet-classifier ML for atypical title-blocks
- Standardise copilot tool-result envelope shape (cosmetic)

## Architecture notes
- Routers split: `auth/google_auth/projects/tasks/daily_reports/resources/
  field_events/docs/upload/iter3/iter4/iter5/iter6/iter7/ws/system`
- Services: `spec_search/kb_search/cpm/ai_agents/copilot_tools/spec_ingest/
  storage/voice_pipeline/schedule_parser`
- All MongoDB queries exclude `_id` in projections
- All endpoints use `Depends(require_tenant)` for tenant isolation
- LLM calls via `emergentintegrations.LlmChat` with universal Emergent key
- JVM starts lazily on first MPXJ call; one process per pod, persistent

## Test credentials
See `/app/memory/test_credentials.md`.
- `admin@iceops.com / Demo123!`
- Tenant slug `ice` (for voice-PIN exchange)
- Stripe sandbox: `STRIPE_API_KEY=sk_test_emergent`
