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

### Iter 10 — Daily Report Phase 2 full rebuild (current)
The `/daily-reports` page is now the daily nerve-center for the field super,
not a thin grid. Single page surfaces every signal:

- **Header** — date, status (draft / submitted / approved + lock pill),
  Prepared/Submitted/Approved by names, 5 KPIs (PPC, headcount, crew hours,
  activity hours, delay min), pre-submit validation banner.
- **Weather panel** — auto-fill from Open-Meteo (geocodes project city/state),
  persists `weather_temp_high_f`/`_low_f`/`conditions`/`precip`/`wind_mph`/
  `weather_fetched_at` into the DR doc; manual override per field.
- **Crew presence panel** — per-trade rows (trade, company, headcount,
  hours/person, sub-total, notes); inline add/edit/delete; running totals.
- **Activity grid** — SOD%, EOD% (editable), Δ variance with red/green/grey
  pill, status select (`not_started`/`in_progress`/`blocked`/`complete`),
  hours, delay min, notes; "FIELD" badge on AF-prefixed unplanned rows.
- **Unplanned activity creator** — amber inline form right under the grid;
  creates a Task with `created_in_field=true`, `requires_pm_review=true`,
  `created_from_daily_report_id=<id>` and pushes a row to the grid.
- **Renovation panel** — only renders when `project.project_type` is in
  (`renovation`, `tenant_improvement`); occupancy_active, occupied_areas,
  dust_controls_in_place, after_hours_work, noise_complaints_count,
  dust_violations_count.
- **Inline event panels (5)** — Delays, Safety events, QC issues, RFIs,
  Deliveries — each with inline list + inline create form. Each created
  record carries `daily_report_id` so it surfaces in the bundle.
- **Photos panel** — multipart upload bound to `daily_report_id`,
  caption support, blob-fetched thumbnails (works under JWT auth).
- **Voice panel** — lists voice annotations with `linked_entity_type=daily_report`
  for this DR; the floating mic FAB still creates them and links via PATCH.
- **Audit log panel** — surfaces audit_log rows scoped to the DR + presence ids.
- **Footer** — Submit (blocks if any validation fails), Approve (admin/pm
  only, requires status=submitted), Export PDF.

Backend additions:
- New collection `daily_presence` (per-trade, per-DR rows).
- DailyReport: weather_wind_mph, weather_fetched_at, occupancy_active,
  occupied_areas, dust_controls_in_place, noise_complaints_count,
  dust_violations_count, after_hours_work.
- Task: created_in_field, requires_pm_review, created_from_daily_report_id.
- Delay / SafetyEvent / QcIssue / Rfi all gained `daily_report_id`.
- Endpoints: `PATCH /daily-reports/{id}`, `POST /daily-reports/{id}/weather-autofill`,
  `GET/POST /daily-reports/{id}/presence`, `PATCH/DELETE /daily-presence/{id}`,
  `POST /daily-reports/{id}/unplanned-activity`,
  `POST /daily-reports/{id}/approve`, `GET /daily-reports/{id}/context`
  (single bundle: report+project+grid+presence+photos+voice+delays+safety+qc+rfis+deliveries+audit_log+user_labels).
- Lock semantics: every DR-scoped endpoint refuses 409 once `status=submitted`.

Copilot scoping:
- `CopilotMessage` accepts `daily_report_id`. When present, `execute_copilot_turn`
  injects DR snapshot (date, status, weather, full grid, presence rows) so
  the LLM can reason about the day's plan vs actuals.
- 3 new tools: `record_presence`, `mark_activity_status`,
  `create_unplanned_activity` — all DR-scoped, all return the standardized
  envelope (`{created|updated: {resource, id, ...}}`). Strict-no-markdown
  rule still enforced (verified by tests).

Frontend wiring:
- `/lib/copilotContext.js` — pub/sub store. DailyReports publishes the open
  DR id; CopilotPanel subscribes and ships `daily_report_id` on every
  send + shows a green "Daily report scope" pill in its header.

**iter 10.json: 18/18 PASS.**

### Iter 11 — Auth rebuild + tenant/team flow + DR fixes (current)
User asked for (a) bug fix on inline records ("load failed"), (b) full
demo-data purge, (c) Google OAuth removed, (d) email+password signup with
organization name, (e) team-invite flow, (f) DR grid must include any
activity whose `[planned_start, planned_finish]` interval contains today,
(g) Delay schema in DAYS not minutes.

**Auth / tenancy**
- `UserRegister` now requires `organization_name`; `POST /api/auth/register`
  creates a fresh tenant on-the-fly + makes the registrant its admin +
  best-effort seeds the CSI taxonomy/cost-rate library for that tenant.
- Slug auto-generated and uniquified.
- Google login completely removed from FastAPI (`google_auth_router`
  unregistered) and from the React app (`AuthCallback` route + import dropped,
  no Google button on Login page).
- Demo seed (`seed_demo`, `/api/dev/seed`) removed from server startup.

**Team management**
- New `team_router` mounted at `/api/team`: `POST /invites` (admin),
  `GET /invites`, `DELETE /invites/{id}` (revoke), `GET /members`,
  `DELETE /members/{id}` (admin, can't remove self → 400).
- Public `POST /api/auth/accept-invite` swaps {code, email, password,
  full_name} for an account joined to the inviting tenant. Codes are
  12-char `secrets.token_urlsafe`, 14-day expiry, single-use.
- Frontend pages: `/signup`, `/accept-invite`, `/team` (admin invite +
  pending-invite copy-link + member roster + remove). Sidebar gets a
  "Team" entry.

**Daily Report fixes**
- `_prepopulate_grid` now uses strict interval containment
  (`planned_start <= report_date <= planned_finish`); the ±1-day buffer
  was wrong — only 2 activities surfacing was the bug.
- New `POST /api/daily-reports/{id}/refresh-grid` — idempotent re-run of
  prepopulate against the live schedule. Frontend exposes a
  "Refresh from schedule" button on the activity-grid panel.
- `Delay.delay_minutes` → `Delay.delay_days` (Optional[float]). Copilot
  catalogue, copilot tool, frontend form, and rendered list all migrated.
  StrictModel `extra='forbid'` cleanly rejects legacy `delay_minutes`
  payloads with 422.
- "Load failed" toasts replaced with a `formatErr` helper that surfaces
  HTTP status + Pydantic detail + non-string detail arrays.

**Database state**
- Both at the start of this iteration AND at the end of testing-agent
  validation, ALL Mongo collections were dropped. The platform is
  intentionally empty — Abhi's first signup at `/signup` becomes the
  first tenant ever.

**iter 11.json: 27/27 PASS — pytest suite at `/app/backend/tests/test_iteration11.py`.**

### Iter 12 — Schedule diff + AI review + PM approval (current)
Every schedule upload (MPP / XER / CSV / XLSX) is now a **proposal** that
must be reviewed and approved before its rows touch the `tasks` collection
— federal-contract baseline-change audit trail.

**Backend**
- `services/schedule_diff.py` — pure `compute_schedule_diff` returning
  `{added, dropped, date_shifts, duration_changes, predecessor_changes, summary}`.
  Threshold for date shifts is configurable; spec says > 3 days.
- `services/schedule_ai_review.py` — Claude (Sonnet 4.5) reviews the diff
  and returns `{risk_level, summary, flags, recommendation, justification_required, model}`.
  Falls back to a deterministic heuristic if EMERGENT_LLM_KEY is missing
  or the LLM call fails. Strict no-markdown.
- `routers/schedule_proposals_router.py` — moved `POST /api/imports/schedule`
  here. Now creates a proposal at `status=pending_review` and never touches
  tasks. New endpoints:
  - `GET /api/projects/{pid}/schedule-proposals` (optional `?status=`)
  - `GET /api/schedule-proposals/{id}`
  - `POST /api/schedule-proposals/{id}/approve` (admin/pm) — applies
    parsed_rows; soft-deletes dropped activities (`status='deleted',
    deleted_by_proposal_id=<id>` so audit linkage is preserved); recomputes
    CPM; writes audit log; logs an /imports history row linked back.
  - `POST /api/schedule-proposals/{id}/reject` (admin/pm)
  - `POST /api/schedule-proposals/{id}/refresh-ai-review`
- `models.py` — `ScheduleProposal` + `ScheduleProposalDecision`.
- BSON-safety: `_norm_row` + recursive `_scrub` convert `datetime.date`
  to ISO strings before `insert_one` (regression-tested).

**Frontend**
- `/app/frontend/src/pages/ScheduleProposals.jsx` — list + detail page.
  - Risk pill (high/medium/low) with Shield icons.
  - Claude summary + flags + recommendation + Re-run review button.
  - 6 KPI tiles (current, incoming, +, −, date shifts, duration changes).
  - Collapsible diff sections: Added, Dropped, Date shifts > 3 days,
    Duration changes, Predecessor changes.
  - Decision card with notes textarea + Reject / Approve & Apply buttons
    (PM/Admin only); awaiting-PM banner for non-reviewers.
  - Recap card after approval/rejection with applied counts +
    skipped-row reasons.
- `Imports.jsx` — toast says "Proposal created — N row(s) parsed",
  auto-redirects to `/schedule-changes?id=<id>` after 600 ms.
- Sidebar gets a "Schedule changes" entry under SYSTEM with
  GitPullRequest icon.

**iter 12.json: 18/18 PASS — pytest suite at `/app/backend/tests/test_iteration12.py`.**

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
| 8    | 34/35   | 100%     | Real MPP/XER + section view + audit |
| 9    | 34/35   | 100%     | CRUD everywhere + onboarding     |
| 10   | 18/18   | 100%     | Daily Report Phase 2 full rebuild |
| 11   | 27/27   | 100%     | Auth rebuild, Google removed, team invites, DR refresh, delay→days, demo wiped |
| 12   | 18/18   | 100%     | Schedule diff + AI review + PM approval workflow |
| 12.1 | smoke   | smoke    | Heading-row filter (FREE heuristic) + chat + manual toggles |
| 13   | 4/4     | smoke    | 5-Route DR Voice Intake (Phase A: Manual/Voice Memo/Live Voice mode selector + Claude extraction + offline IndexedDB queue + offline request interceptor) |

### Iter 13 — 5-Route DR Voice Intake (Phase A; 2026-05-25)
The "New Daily Report" flow now exposes three intake modes (per Abhi's
specification, standard short names):

- **Manual** — type into the form (default, offline-capable).
- **Voice Memo** — record on-device, server-side Gemini transcribes,
  Claude extracts structured fields, DR collections auto-populate.
- **Live Voice** — Gemini Live WebSocket directly from the browser;
  AI guides the super through weather → trades → activity-by-activity
  → events. Final transcript posted to /voice/extract for the same
  Claude write-through.

Offline behaviour: Manual and Voice Memo stay enabled (form drafts +
audio blobs queue locally). Live Voice is grayed-out (label `(offline)`)
when `navigator.onLine` is false OR when `GEMINI_API_KEY` is absent on
the backend (`/api/daily-reports/voice/config` probe).

**Backend**
- `routers/dr_voice_router.py` — `/voice/transcribe`,
  `/voice/extract`, `/voice/transcribe-and-extract`,
  `/voice/live-token`, `/voice/config`.
- `services/dr_voice_extract.py` — Claude Sonnet 4.5 with a strict
  JSON schema covering weather, notes, activity_updates,
  crew_presence, delays, safety, qc, deliveries, coordination.
- Fixed: extract writes now use `db.daily_presence` (not
  `crew_presence`) and `daily_report_id` (not
  `logged_on_daily_report_id`) so rows appear in `/context`.
- `tests/test_dr_voice_intake.py` — 4 regression tests; all PASS.

**Frontend**
- `pages/DailyReports.jsx` — three-mode dropdown next to date picker.
- `components/VoiceIntakePanel.jsx` — async recorder + offline queue
  UI + Gemini Live WebSocket client. Auto-mounted at the top of DR
  detail when intake mode != "manual".
- `lib/drVoiceQueue.js` — IndexedDB audio queue.
- `lib/drOfflineSync.js` (NEW) — axios interceptor that captures
  network-only failures on PATCH/POST against /daily-reports/* and
  friends, queues to localStorage, drains on `online` event.
- DR detail page shows an offline status banner (amber when offline,
  blue when draining a non-empty queue with "Sync now" button).

**Phase B (completed 2026-05-25 ~21:00 UTC)** — User provided
`GEMINI_API_KEY` and billing is enabled on Google AI Studio. Voice
endpoints are LIVE.

- Transcription model: **`gemini-3.5-flash`** (GA; replaces the
  deprecated `gemini-2.0-flash` and the soon-to-be deprecated
  `gemini-2.5-flash`).
- Live conversational model: **`gemini-3.1-flash-live-preview`**
  (current Live-API recommendation; replaces the deprecated
  `gemini-2.0-flash-exp`).
- Transcription prompt hardened against hallucination on non-speech
  audio (verified with a synthetic 440 Hz tone → returns `""`).
- **Files API path added**: audio uploads <18 MB use inline bytes;
  uploads 18 MB-100 MB are routed through Google's Files API
  (`client.aio.files.upload`) so 60-minute high-quality memos work.
  Best-effort cleanup deletes the uploaded file after extraction.
- New endpoint cap raised from 25 MB → 100 MB.
- **E2E test suite** (`tests/test_dr_voice_e2e.py`) uses gTTS to
  synthesize a real construction-site voice memo, posts it to
  `/voice/transcribe-and-extract`, and verifies the whole pipeline
  populates weather, presence, and safety_event rows. **4/4 PASS**
  (full run including audio synthesis + Gemini + Claude + DB
  round-trip: 23s).

### Iter 13.1 — Voice intake debugging UX + fixes (2026-05-25 evening)
User filed a real voice memo for May 22 DR (3 carpenters + electrical
rough-in 20% + 3-day electrical delay) and got "no fields applied".
Multiple bugs surfaced:

1. **MIME codec parameter broke Gemini**: browser MediaRecorder sends
   `audio/webm;codecs=opus` but Gemini only accepts `audio/webm`.
   Backend now strips the `;codecs=...` suffix before calling Part.from_bytes.
2. **Activity grid was invisible to Claude**: the router pulled grid
   rows with a `tenant_filter()` call, but `daily_report_activities`
   docs don't carry tenant_id (tenant scope is enforced via DR
   parent). Removed the bogus filter so Claude actually sees the grid.
3. **Grid projection field name wrong**: was asking for `name` /
   `description`; the actual field is `activity_description`. Fixed.
4. **Description-based activity matching**: Claude now sees
   `code + activity_description + trade` for every grid row and
   matches spoken hints like "electrical rough in" to the right
   activity code. Added a server-side fuzzy fallback (token-overlap
   score) for cases where Claude leaves activity_code null but
   provides a description hint.
5. **Delay impact days**: extraction prompt now captures
   `projected_impact_days`; router writes it into `delay.delay_days`
   and into each `per_task_impacts` entry.
6. **Frontend transparency**:
   - **Live transcript preview** while recording, using the browser's
     native Web Speech API (free, real-time, no Gemini cost).
   - **Final Gemini transcript** displayed prominently after stop so
     the super can compare what they said vs what AI heard.
   - Color-coded result card: green when fields extracted, amber when
     transcript came back but no structured fields, red when no
     speech was detected at all (with troubleshooting tips).
7. **"Add via voice" button** on DR detail header — users can now
   open the voice intake panel on any draft DR, not just at creation.
8. **Verified**: feeding user's exact transcript through `/voice/extract`
   correctly produces activity_updates=1 (grid 1.9 → 20%),
   crew_presence=1 (CARP × 3 × 8 hr from ICE), and delays=1
   (3-day impact, linked to schedule_activity_id of the electrical task).

### Iter 13.2 — Voice intake hardening + Live Voice fix (2026-05-25 night)
After the codec/mic-stream fix, the user's first real voice recording
worked at ~95% accuracy. Two follow-up issues:

1. **Delays page 500'd**: `/api/projects/{id}/delays` returned 500 with
   `Delay.date Field required` ValidationError. Legacy delay rows in
   MongoDB predate `date` + `reported_at` being on the Delay model;
   the FIRST legacy row in a list killed the whole endpoint. Added
   defensive `_enrich_delay_for_read`, `_enrich_safety_for_read`,
   `_enrich_qc_for_read` helpers that fall back to `created_at` /
   `logged_date` / today's date. Three list endpoints now 200 again.

2. **Duplicate event rows when voice memo runs twice**: A single
   Claude response with two paraphrased delays (or two recordings on
   the same DR) created duplicate delay rows. Added token-overlap
   (≥60%) dedup in `dr_voice_router.py` for delays / safety / qc /
   deliveries / coordination + (trade, company) dedup for presence.
   Response now includes `skipped_duplicates` so the frontend toast
   tells the user "skipped 2 duplicates already on this DR".

3. **Live Voice WebSocket closed immediately**: model name
   `gemini-3.1-flash-live-preview` was 404. Probed Google's
   `models?supportedGenerationMethods=bidiGenerateContent` endpoint
   and found the real live-capable models: `gemini-2.5-flash-native-
   audio-latest` and `gemini-2.5-flash-native-audio-preview-09-2025`.
   Updated `/voice/live-token` to return the correct model.

4. **Live Voice setup required AUDIO modality**: native-audio model
   rejects `response_modalities=["TEXT"]` with code 1007 "Cannot
   extract voices from a non-audio request." Switched frontend to
   `["AUDIO"]` + enabled `input_audio_transcription` and
   `output_audio_transcription` so the user sees captioned text of
   both AI and human speech. Added audio playback through Web Audio
   API (24 kHz PCM chunks decoded + scheduled onto an AudioContext
   queue so AI's voice plays back in real time).

5. **Wait for setupComplete before streaming mic**: previously fired
   mic samples immediately on ws.onopen, which Gemini drops. Now
   waits for the setupComplete server message, then starts
   ScriptProcessor mic loop.

6. **Better Live Voice error UX**: WebSocket close handler now
   distinguishes "closed during connect" (auth/model rejection) from
   "closed during live" (network/timeout); explicit toast per case.

7. **8/8 voice regression tests PASS** after all changes. E2E test
   was updated to delete pre-existing rows on its target DR so it
   doesn't trip the new dedup logic.

### Iter 13.3 — Live Voice polish + Q&A extraction (2026-05-25 late night)
User reported live voice was choppy and final extraction produced "no
fields" because the transcript was fragmented per-token. Root causes:

1. **Choppy audio playback**: scheduling was using `Math.max(currentTime, nextAt)`
   which falls back to currentTime on buffer underrun → gaps. Switched
   to a 250ms pre-buffer; when the queue is exhausted, the next chunk
   starts `currentTime + 0.25s` so network jitter doesn't cut the AI off.

2. **AI's internal thinking text was captured**: native-audio model
   streams chain-of-thought as `modelTurn.parts[].text` ("**Initiating
   Daily Report**", "**Acknowledging the Delay**"). These got mixed
   into the transcript. Frontend now ignores those entirely — only
   `outputTranscription.text` (what the AI actually SPOKE) is shown.

3. **Per-token transcript fragmentation**: input/output transcription
   chunks arrived 1-3 chars at a time, each rendered as its own
   "You:" or "AI:" line. Refactored into turn-based ref accumulators
   (`userTurnRef`, `aiTurnRef`) that flush only on `turnComplete`.
   Display now reads as a clean conversation.

4. **Extract at end-of-session was being fed the fragmented mess**:
   stopLive() now builds a clean `Q (AI): ... A (super): ...`
   formatted transcript from the accumulated turns and sends THAT to
   `/voice/extract`. Tested with user's actual conversation transcript:
   weather + crew + activity 1.7 + delay (3-day impact, moderate) all
   extracted correctly. Live Voice now works end-to-end.

5. **Claude system prompt updated** to recognize the Q&A format and
   handle partial answers like "yes", "skip", "no", "moderate" by
   pairing them with the preceding AI question for context. Said "no"
   to safety/QC questions now produces ZERO event records (no empty
   placeholders).

6. **Live Voice system instruction strengthened**: AI now asks ONE-
   sentence questions, acknowledges in ≤3 words, asks ONE follow-up
   for missing fields (severity, impact days), accepts "skip" to
   move on, never repeats itself, and never speaks its internal
   reasoning out loud.

7. **Interruption support**: when Gemini sends `serverContent.interrupted`,
   the audio playback queue is immediately stopped so the AI doesn't
   keep talking over the user.

8. **Voice live transcript UI** rewritten — Slack-style chat layout
   with colored You/AI prefix and proper line breaks instead of one
   line per token.

9. **9/9 voice regression tests PASS** after all changes (added
   `test_e2e_live_voice_qa_transcript_extraction` to guard the new
   conversation-format extraction).

### Iter 13.4 — DR Events tabbed layout (2026-02-XX)
Restructured the Daily Report detail view: the previous 6-card grid
(Delays / Safety / QC / RFIs / Deliveries / Notes) is replaced with a
single tabbed "Events" surface. Each panel keeps **all** of its
fields, dropdowns, task-link pickers, severity selectors, form
buttons, and audit behavior intact -- this is a structural change
only, no data was removed.

Implementation:
1. New `EventsTabsCard` wrapper inside `DailyReports.jsx` that holds
   the active tab + a `pendingDetail` state.
2. Listens for the Activity Grid Issues dropdown's
   `dr-open-event-form` window event. When the target tab is not
   currently active, switches to it then re-dispatches the event with
   `__replayed: true` so the freshly-mounted panel's existing
   `useIssueDropdownListener` picks it up after React commits.
3. A floating emerald "+" button on the tab strip dispatches the
   same event so users can add an event from the tab header itself.
   Notes tab special-cases this -- focuses the notes textarea.
4. Empty-state text on every event tab updated to:
   "Nothing logged today — say so by clicking + at right."
5. Open delay reminders banner (`ReminderBanner`) was kept and moved
   ABOVE the Events tabbed card per user request, so rolled-forward
   delays remain the first thing the super sees.

Verified via screenshot tool: tab switching, "+" button, and the
Activity Grid → cross-tab navigation all work; QC pre-link from grid
correctly fires across tab boundaries.

### Iter 13.5 — DR detail UX cleanup (2026-02-XX)
Tightened the DR detail surface in response to Abhi's "saves N px every
DR" review:

1. **Project / Contract header** -- collapsed by default to a single-
   line summary `Client · Station · #Contract · Contractor · Signature`
   with a chevron. Full 6-field grid only renders when expanded; the
   PDF/xlsx export pipeline is untouched (still renders the full
   block for client deliverables). ~150px saved per DR open.
2. **Activity Grid sections** -- sections with 0 scheduled rows for
   the report date collapse by default. Section header (with count
   chip + read-only label) stays visible so the super sees the
   category exists; click the header to expand the body.
3. **Weather auto-fill** -- "Auto-fill from forecast" button removed.
   A new effect in `DailyReportDetail` fires the autofill endpoint
   automatically the first time the DR is opened when every weather
   field is blank AND `weather_fetched_at` is null, so a super who
   manually edited values (without invoking autofill) never has them
   overwritten on reopen. Badge now reads "auto-filled HH:MM:SS".
4. **Hours column** removed from the Activity Grid (header + per-row
   cell + section colspans). Activity hours are tracked via the
   Manpower log → activity link instead; the locked-DR per-row edit
   modal still keeps `hours_worked` as a back-compat field.
5. **"Crew presence" → "Manpower log"** rename on the panel header.
6. **Date format consistency** -- Activity Grid header date now uses
   `formatDateDisplay` (MM/DD/YYYY) so it matches the DR list,
   reminder cards, and toasts. SupplementBadge + photo supplement
   tooltip also pass through `formatDateDisplay`.
7. **Audit trail** -- collapses to `"N audit entries — click to
   expand"` chip by default; expanded view shows the rolling 30-row
   list with a "Collapse" link to fold it back.

Verified via screenshot tool: all 7 changes render correctly on a
freshly-opened DR. No regressions to the Events tabs card (13.4).

### Iter 13.6 — DR polish + Voice AI fixes + Vendor repo (2026-02-XX)

**TIER A — UX polish**
- Manpower log: removed the always-visible auto-blank draft. Saved
  rows sit clean; an explicit "+ Add trade" emerald button below the
  table opens one editable draft, which switches to "Save" once a
  trade is typed. Delete (Trash2) icon on draft + persisted rows is
  always clickable now (the `isOnlyRowInPanel` guard was dropped).
- Weather card slimmed (px-4 py-3 vs surface p-5) and given its own
  inline header so it doesn't dominate the viewport given that it's
  auto-filled.
- Renames: "Activity grid" → "Activity log"; "Field Work" →
  "Today's planned activity".
- New section-flow UI: each PanelShell now accepts a `step` prop
  rendering a numbered emerald badge (1 Project · 2 Weather ·
  3 Manpower · 4 Activity Log · 5 Events · 6 Photos · 7 Audit).
  New CSS class `.dr-section` adds a soft hairline border + left
  accent strip so the page reads as a flowing sequence of cards.

**TIER B — Voice AI fixes**
- RFI extraction: the system prompt now has an explicit RFI-vs-Delay
  decision rule + an `rfis[]` array in the extraction schema. Router
  inserts into `db.rfis` with priority normalization and dedupe.
- Re-recording dedupe: voice extractor now receives `already_logged_
  crews` for the DR and the prompt's CRITICAL RULES section tells
  Claude NOT to re-emit known trade+company pairs as new
  crew_presence rows.
- Activity progress nuance: voice extractor receives SOD%,
  duration_days, scheduled_hours_today, and open_blocker_count per
  grid row. Prompt's ACTIVITY PROGRESS NUANCE block now explains
  how to compute pct_complete_end_of_day from a phrase like "half of
  today's planned work" rather than blindly writing 50.
- Live voice prompt updated to capture RFI mentions distinctly.

**TIER C — Photo UX**
- AiAnalysisCard + TagWizard dialogs gained `max-h-[90vh]
  overflow-y-auto` so the action buttons are always reachable on
  shorter laptop screens at 100% zoom.
- Photo thumbnails shrunk: Photos page + PhotoTray now render
  3-8 columns (was 2-5); DR detail Photos panel renders 4-8 columns
  (was 3). Click any thumb to open the larger preview/tag wizard.

**TIER D — Bigger features**
- New EodSlider component: replaces the EOD% numeric input in the
  Activity log with a range slider (0-100) + live percent readout.
- Voice memo playback: VoiceIntakePanel keeps a Blob URL of the most-
  recent recording and renders a native `<audio controls>` in the
  result panel. URL is revoked + replaced on the next recording start
  so it never persists across page reloads.
- Delete project (admin-only): new `DELETE /api/projects/:id` route
  cascades across 24 project-scoped collections; UI button on
  /projects with name-confirmation prompt.
- Close tenant account (admin-only): new
  `POST /api/auth/close-account` route wipes 31 tenant-scoped
  collections including the tenant + users + memberships, freeing
  the email to sign up a brand-new organization. UI button in the
  user dropdown (`/components/TopBar.jsx`) with DELETE
  confirmation.
- Subcontractor / Supplier central repo:
  - New `vendors_router.py` with CRUD for both collections
    (`/api/subcontractors`, `/api/suppliers`) — tenant-scoped, name
    dedupe on insert.
  - New `/vendors` page with Tabs (Subcontractors / Suppliers),
    add/edit/delete via a shared dialog.
  - Sidebar entry "Vendors" added under OVERVIEW.
  - DR Manpower log "Company" column auto-completes from the
    subcontractor list (HTML5 `<datalist>`); empty state shows a
    "+ Add to vendor repo" link.
  - Voice AI now receives the list of known subcontractor names +
    their trades; system prompt instructs Claude to use the EXACT
    stored name on `crew_presence.company_name` when a trade match
    is found (else leaves it null).
- Collections `subcontractors` and `suppliers` are listed in the
  tenant + project cascade-delete arrays so account-close + project-
  delete wipe them cleanly.

Smoke-tested via screenshot: Vendors page renders, DR detail still
mounts with all 4 numbered section badges visible, no console errors.

### Iter 13.7 — Dialog viewport fit + project vendor links (2026-02-XX)

**Dialog scroll fix** — `components/ui/dialog.jsx` DialogContent base
class went from `grid` to `flex flex-col` with `max-h-[90vh]
overflow-y-auto overscroll-contain`. Every dialog in the app
(AiAnalysisCard preview, TagWizard, project create/edit, vendor
create/edit) now respects the viewport: top X close button is always
reachable, scrolling happens inside the modal, bottom action buttons
never get clipped. Verified at 1280×720.

**Project ↔ Vendor linking** —
- Backend: `Project`, `ProjectCreate`, `ProjectUpdate` now carry
  `subcontractor_ids: list[str]` and `supplier_ids: list[str]`
  (default `[]`).
- Frontend: new `VendorPicker` chip-grid component on the project
  create + edit dialog. Subs and suppliers are loaded from
  `/api/subcontractors` and `/api/suppliers`; selecting a chip
  toggles the ID in `form.subcontractor_ids` / `form.supplier_ids`.
  Empty-state surfaces a "Manage repo ↗" link + an "Add some →"
  link straight to /vendors so the user can populate the repo
  without leaving the project flow.
- Voice AI now narrows its `known_subcontractors` list to the
  project's selected subs when populated; falls back to the full
  tenant list otherwise (so legacy projects without explicit
  vendor linkage continue to work).
- The submit() function preserves empty arrays in payload so
  un-selecting all subs / suppliers actually clears the linkage.

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


## 2026-02 — Paperwork: visible in-app, hidden in exported PDF
- **Reverted** the in-app filters from the prior commit (wrong scope).
  Paperwork tasks (preconstruction_paperwork / construction_paperwork /
  closeout_paperwork) DO need to render in the DR's read-only reference
  sections so the super can see what paperwork is scheduled today —
  they just don't act on it.
- **PDF-only filter retained + fixed**: `iter6_router.export_daily_report_pdf`
  now `$in`-looks-up each grid row's task to stamp `work_category`
  (grid rows in mongo don't persist this — it lives on the linked task),
  then filters to `_grid_section_for == 2` (Field Work). External +
  internal PDFs both drop paperwork rows from "Today's activity";
  the in-app DR keeps them.
- Verified via curl: 3 paperwork + 1 field task scheduled for the same
  day → `/context` returns all 4, `pdf-rich` shows `Today's activity (1)`
  with only the field row.

## 2026-02 — Paperwork rows must never enter the super's DR
- **Root-cause fix**: `_prepopulate_grid` + `/available-tasks` in
  `daily_reports_router.py` now exclude `work_category in
  {preconstruction_paperwork, construction_paperwork, closeout_paperwork}`.
  Paperwork tasks are reference-only in the schedule; the super doesn't
  execute them in the field, so they must never sit in the DR. The
  earlier PDF-side filter only hid them at export — the rows were still
  polluting the live grid + downstream consumers (voice intake,
  copilot, /context).
- **Self-healing purge**: `daily_report_context` now deletes any
  paperwork rows that landed in `daily_report_activities` *before* the
  filter shipped. Idempotent, draft-only (submitted/approved DRs are
  federal-contract records and stay frozen). Existing polluted DRs
  clean themselves the moment a user opens them — no migration needed.
- Verified via curl: 1 paperwork + 1 field-work task scheduled for the
  same day → refresh-grid inserts 1 row only (field), /context after
  manually injecting a legacy paperwork row removes it from both
  response and DB, PDF shows `Today's activity (1)`.

## 2026-02 — DR PDF export polish + Weather UX
- iter6_router (`pdf-rich`): renamed "Crew presence" → **Manpower log**,
  "Activity grid" → **Today's activity**, added **Task worked** column
  to the Manpower log (resolves `schedule_activity_id` → `wbs — desc`),
  dropped the obsolete **Crew** + **Hrs** columns from Today's activity
  (whose body rows had only 6 cells against an 8-col header — a latent
  alignment bug).
- Today's activity grid in both internal + external PDFs now filters to
  `work_category` section 2 (Field Work) only, dropping the read-only
  reference rows from Preconstruction / Closeout Paperwork that previously
  bloated client-facing exports.
- Weather autofill (`DailyReports.jsx`): silent `.catch` replaced with a
  toast that surfaces the backend `detail` ("Could not geocode project
  address (no city/state)", "Weather provider unreachable", etc.), and a
  manual **Refresh** button added to `WeatherPanel` so the super can
  re-trigger the autofill after fixing the project city/state without
  having to navigate away and back.
- Verified via curl: `GET /api/daily-reports/{id}/pdf-rich?mode=internal`
  and `?mode=external` both 200 with the new section labels +
  "Task worked" header rendered, INTERNAL/external audience badge
  behaviour preserved.
