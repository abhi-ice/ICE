# ICE Schedule Ops — PRD

## Original problem statement
Multi-tenant federal-construction PM platform replacing Procore / Touchplan /
Bluebeam / Smartsheet for GSA, USACE, NAVFAC, and other agencies. Original
prompt requested PostgreSQL + LangGraph + Celery + pgvector. The user
explicitly authorised substitution as long as the stack remains Python and
the functional intents are met. Stack actually shipped:

- **Backend**: FastAPI + Motor (async MongoDB) + Pydantic v2 + WeasyPrint +
  Pillow + imagehash + scikit-learn TF-IDF + emergentintegrations.LlmChat
  (universal Emergent LLM key) + Whisper + pdf2image + pytesseract + Stripe
  (via emergentintegrations.payments)
- **Frontend**: React 19 + TailwindCSS + shadcn/ui + Konva + raw three.js +
  react-pdf + react-router 7 + WebSocket-based real-time channel
- **Infra**: MongoDB collection-per-aggregate; supervisor-managed services;
  service-worker PWA; Emergent Object Storage for binary blobs

## Roles & personas
1. **Project Executive** — pipeline KPIs, P50/P80 finish (admin)
2. **Project Manager** — daily ops, recovery, marketplace, master doc
3. **Field Superintendent** — daily reports, field issues, deliveries
4. **Foreman** — daily report row ownership, voice annotations, photos
5. **Voice User (PIN auth)** — capture-only mobile foreman view
6. **External / Read-only** — owners, sub-rep, GC PMs

## What's implemented (chronological)

### Iter 1-4 — MVP through Spatial Path 3
Auth + tenants + projects + tasks + locations + resources + cost rates +
daily-report grid + voice annotations (Whisper + Claude) + spec PDF
ingestion + TF-IDF spec search + drawing markup viewer (Konva) + photo
uploads + 2.5D mass model + spatial elevation projection + drawing pin
deep-link → entity side-panel + auto-generated submittals + polygon editor.
**31/31 tests PASS in iter 4.**

### Iter 5 — Marketplace + Notifications + Productivity
Cross-tenant marketplace postings + notifications + global search +
weather (Open-Meteo) + productivity-rates + cost-estimates +
work-item history + approvals badge.

### Iter 6 — Master prompt completion (a-g)
- WeasyPrint daily-report + weekly-rollup PDFs
- Photo EXIF + perceptual hash (Pillow + imagehash) — substitution for DINOv2
- AI Recovery apply + eval-harness
- Aging dashboard (`/aging`) with smart sidebar badges
- Submittal ↔ Activity linkage (manual + auto-link)
- Spatial section view (cut-line → SVG vertical slice)
- Lookahead Gantt + drag-to-reschedule
- Drawing markup expansion (9 tools incl. ellipse, arrow, line, cloud, text, calibrate)
- Marketplace UI (full)
- Productivity dashboard UI
- Global search dropdown
- NotificationsBell on TopBar
- PWA manifest + service worker
**iter 5.json: 50/50 functional tests PASS.**

### Iter 7 — Stripe + RAG + project gating + compliance + real-time
- **Stripe Checkout escrow**: GC accepts a marketplace bid → funds escrow
  via Stripe → escrow holds in `payment_transactions` ledger → activity
  reaches `milestone_pct` → automatic release-to-sub via background task
  triggered from `tasks PATCH pct_complete`. Tracked end-to-end with WS
  broadcasts.
- **Master doc with project-type gating**: `/projects/:id/master-doc` — 4
  construction types (new / renovation / TI / infrastructure), delivery
  method, sketch-review style, govt security level, site conditions
  (occupied / historic / hazmat / shifts), site-constraint chips,
  notes. AI agents (`copilot_answer` + `recovery_suggestions`) prepend
  this as context, materially changing recommendations for renovation
  vs new-construction.
- **Knowledge Base / RAG**: `/kb` — TF-IDF over closed RFIs + resolved
  work items + manual lessons. `_kb_block` injected into AI prompts so
  every recovery / RFI-drafter / copilot response can cite past
  resolutions. Idempotent `ingest-historical` endpoint.
- **NIST 800-171 control mapping**: 16 controls across 8 families with
  implementation+evidence_endpoint each (`/compliance`).
- **Audit-log integrity verifier**: `GET /audit-log/verify-chain` walks
  the SHA-256 hash chain forward; surfaces first divergence index.
- **Polygon suggester (Claude vision)**: `POST /spatial/suggest-polygon`
  crops PDF page → Claude Sonnet 4.5 vision returns polygon points;
  PolygonEditor "AI suggest" button consumes it.
- **Drawing OCR**: `POST /drawings/ocr` runs pytesseract on the
  bottom-right title-block → heuristic extraction of sheet number, scale,
  date, title. DrawingViewer toolbar "OCR" button.
- **Real-time WebSockets**: `/api/ws?token=<jwt>` tenant-scoped fanout.
  `emit_notification()` and tasks PATCH both push events. NotificationsBell
  refreshes on incoming events.
- **Voice-only PIN auth**: admin issues a 6-digit PIN scoped to a
  project + foreman name; PIN exchange returns a `role='voice'` JWT
  that opens `/m/foreman` (mobile-first capture-only view: voice
  notes + photo upload + per-task percentage chips).
- **Mobile foreman view**: `/m/foreman` — phone-friendly layout with
  voice-record button, camera-capture photo input, and 7-day lookahead
  with one-tap pct-complete chips.
- **Re-seed unapplied recovery**: `POST /dev/seed/recovery` for
  regression coverage of the apply flow.

**iter 6.json: 21/22 functional tests PASS** (1 conditional skip on a
gating path that requires specific seed state).

## Test results by iteration
| Iter | Backend | Frontend |
|------|---------|----------|
| 1    | 100%    | 100%     |
| 2    | 100%    | 100%     |
| 3    | 100%    | 100%     |
| 4    | 31/31   | 100%     |
| 5    | 50/50   | 100% (after testid fixes) |
| 6    | 21/22   | 100%     |

## Known deferred / substituted
- **DINOv2** → imagehash pHash (deterministic, free)
- **MPP/XER parser** → CSV/XLSX import works; .mpp/.xer return helpful
  error (deferred — needs JPype + MPXJ JVM)
- **Bluebeam .bax** → deferred (proprietary)
- **Stripe Connect Express** → Stripe Checkout (escrow held in our
  ledger; payouts recorded as ledger rows + notifications, not actual
  Stripe Transfers — would require platform onboarding)
- **LangGraph** → multi-step LlmChat sequences cover the same intent
- **pgvector** → TF-IDF (lexical) + LlmChat for re-ranking
- **Celery** → FastAPI BackgroundTasks (sufficient for ingest + auto-release)

## Backlog (P2)
- Stripe Connect Express full onboarding for actual payouts to subs
- True XER/MPP parsing via MPXJ + JPype (requires JVM in container)
- Active probe of NIST evidence_endpoints (currently flag is hard-coded
  True from mapping)
- Section-view 3D rendering in `Spatial3D` (currently SVG slice)

## Architecture notes
- All MongoDB queries exclude `_id` in projections; Pydantic serialisation
  strict
- All endpoints respect `tenant_filter()` + `Depends(require_tenant)`
- LLM calls use `emergentintegrations.LlmChat` with universal Emergent key
- Routers split by concern: `iter3` (persistence) → `iter4` (submittals) →
  `iter5` (marketplace + notifications) → `iter6` (PDF + EXIF + section)
  → `iter7` (Stripe + RAG + master doc + compliance + real-time + voice
  PIN + OCR + polygon vision) + `ws_router` (WebSocket fanout)

## Test credentials
See `/app/memory/test_credentials.md`
- admin@iceops.com / Demo123!
- Tenant slug: `ice` (for voice-PIN exchange)
- Project: GSA Federal Building Renovation — 8th Floor
- Stripe: STRIPE_API_KEY=sk_test_emergent (Emergent test sandbox)

## File map
**Backend routers**: `auth_router` · `google_auth_router` · `projects_router` ·
`tasks_router` · `daily_reports_router` · `resources_router` ·
`field_events_router` · `docs_router` · `upload_router` · `iter3_router` ·
`iter4_router` · `iter5_router` · `iter6_router` · `iter7_router` · `ws_router`

**Backend services**: `spec_search` · `kb_search` · `cpm` · `ai_agents` ·
`spec_ingest` · `storage` · `voice_pipeline` · `schedule_parser`

**Frontend pages**: Dashboard · Projects · MasterDoc · DailyReports · Photos ·
Tasks · Lookahead (list+Gantt) · WeeklyReports · Locations · PolygonEditor ·
Spatial3D · Productivity · RFIs · Drawings · Specs · Submittals · Aging ·
Approvals · Recovery · Resources · Imports · AiAgentsPage · KnowledgeBase ·
MarketplaceFull · VoicePinAdmin · ComplianceFull · AuditLog · Settings ·
VoiceLogin · MobileForeman
