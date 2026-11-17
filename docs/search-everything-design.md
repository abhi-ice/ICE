# Design: "Search everything" (whole-database retrieval search), phases 0 to 5

Status: design only. I edited no files, connected to no database and ran no tests. I spot-checked four things in this session: `audit.py:176-202` (the `write_audit` signature and fields), `database.py:20-33` (`get_db`, `tenant_filter`), `services/runtime_settings.py:40-47` (`ALLOWED_FLAGS`), and the helpers `is_full_admin` / `is_enforced` / `allowed_project_ids` / `project_scope_filter` / `filter_rows` at `services/project_access.py:81/101/126/395/422`. Every other file:line comes from the three research maps and the critic report, all of which have had their corrections applied. Paths are relative to `backend/` unless noted. Drawings, email and voice are out of scope (phase 4 is reserved for them).

---

## 1. What we are building, in one paragraph

Every searchable record (daily report, activity note, delay, request for information (RFI), submittal, spec section and so on) is turned into one or more text "chunks". Each chunk gets a short header that says what the record is, plus the record's own text. Chunks are stored in one MongoDB collection, `search_chunks`, along with a Voyage `voyage-4` vector (1,024 numbers) and filter fields. Every write to a source record adds a job to `search_queue`. A background worker takes jobs from the queue and rebuilds the record's chunks. Searches run a vector search and a keyword search, merge the two lists, rerank the top results and return them with citations. The assistant ("Mason") gets a `search_everything` tool. Haiku answers simple lookups and Sonnet handles harder questions. Everything sits behind the default-OFF flag `FF_SEARCH_EVERYTHING`.

---

## 2. Phase 0: setup

### 2.1 Driver upgrade
- Today: `requirements.txt:70` `motor==3.3.1`, `requirements.txt:103` `pymongo==4.5.0`.
- Upgrade **both together** to `motor>=3.6` (3.7.x) and `pymongo>=4.10`. Motor before 3.6 breaks against pymongo 4.9 and later because pymongo moved its internal modules. That is from general knowledge, not checked here, so the upgrade must be tested.
- What it unlocks: `SearchIndexModel(type="vectorSearch")` and `Binary.from_vector` for compact vector storage. `$vectorSearch`, `$search` and `$rankFusion` are ordinary aggregation stages. **`$rankFusion` needs MongoDB 8.1 or later**: check the live Atlas Flex cluster version (see Risks).
- Places that build a client: `database.py:20`, each `migrations/0NN_*.py` (for example `001:41`, `028:76`), `scripts/reset_password.py:84`, and `routers/backup_router.py:44,119` (sync `MongoClient`).
- Things that must keep working (BSON handling): `routers/backup_router.py:40-41,104,114` (`json_util` canonical/relaxed), `scripts/restore_backup.py:25,33,59`, `audit.py:234-235` (`bson.encode`), `routers/iter8_router.py:25`. Regression check: `tests/test_backup_export.py`, which round-trips Int64, Decimal128 and binary values.
- The grep for deprecated calls is clean (no `.count()`, `ensure_index`, `find_and_modify`, and so on).
- Moving to pymongo's own `AsyncMongoClient` is optional and is not part of this work.

### 2.2 Voyage key
- `services/llm_keys.py:24-42`: add `voyage_api_key()` / `has_voyage_key()`, in the same style as the existing functions (read the environment on each call, strip, return None when empty).
- `server.py:477-497` `_log_startup_config`: log `VOYAGE_API_KEY yes/no` only, never the value.
- New `scripts/set_voyage_key.ps1` (at the repo root), copied from `scripts/set_anthropic_key.ps1:10-23`. It uses a hidden `Read-Host -AsSecureString` prompt, checks the prefix (Voyage keys usually start with `pa-`; confirm this), runs `railway.cmd variable set -e live -s api VOYAGE_API_KEY --stdin`, and has an `-AlsoStaging` switch.
- Embedding client: a new `services/voyage_client.py` (embed with `input_type` "document" or "query", and rerank). It is separate from `services/embeddings.py:27-87`, which keeps serving the spec fallback built on litellm until phase 3 retires it.

### 2.3 Flag `FF_SEARCH_EVERYTHING` (default OFF)
1. Add it to `ALLOWED_FLAGS` (`services/runtime_settings.py:40-47`).
2. Add a `FLAG_META` entry with a plain-language label (`routers/admin_settings_router.py:30-61`).
3. Add a getter `search_everything_enabled()` in `services/feature_flags.py` (pattern at :43-91).

When the flag is OFF:
- the enqueue hook does nothing;
- the worker loop sleeps;
- the tool is not added to the catalogue;
- the old `search` and `search_specs` keep working.

Flags apply to the whole platform (`feature_flags.py:43-47`). If we need to roll out per company, add `tenants.search_enabled` later, following the per-project `schedule_engine` pattern (`feature_flags.py:94-100`).

### 2.4 Test database
- The local `mongod 8.0.4` in `~/ice-testenv` cannot run `$vectorSearch`. Use `mongodb/mongodb-atlas-local` in WSL Docker (Docker 29.1.3 is present; no image has been pulled yet).
- Database names must match the guard in `tests/conftest.py:24,45-48`: prefix `test_` / `ice_test` / `unit_` / `smoke`, for example `test_search_everything`.
- Tests read `MONGO_URL` / `DB_NAME` from the environment and are skipped when the database cannot be reached (pattern at `tests/test_backup_export.py:30-31,55-57`).
- Voyage calls in unit tests use a fake embedder that returns a deterministic vector derived from a hash. Only a separate eval suite (phase 5), marked and switched on by an environment variable, calls the real Voyage service.

---

## 3. Phase 1: chunkers, `search_chunks`, `search_queue`, worker, backfill, nightly reconcile, deletion hooks, classification test

### 3.1 Collection classification (the registry)
One module, `services/search/registry.py`, holds `COLLECTIONS = {name: Decision(...)}` for **every** collection the code touches. The decision is one of `index`, `query_only` or `exclude`. For `index`, the entry also names a chunker, a scope resolver, `allowed_roles` and the hash fields.

#### Indexed (phase 1 unless marked)

| Collection | Scope (tenant / project) | Text embedded | Filter metadata on chunk | Chunking and header | Notes |
|---|---|---|---|---|---|
| daily_reports | own tenant_id, project_id (models.py:1204-1205) | notes, no_work_note, no_work_reason, weather_conditions, occupied_areas | date=report_date, status, is_no_work_day | 1 per report; split above about 1,500 tokens on `[Voice intake` markers or paragraphs. Header `<project> \| Daily report <date> \| status <s> \| weather <cond> <hi>/<lo>F` | Project name and author come from a join |
| daily_report_activities | **none on row** (0/172 have tenant_id or project_id). Resolve through daily_report_id → daily_reports (project_access.py:250) | notes, activity_description, delay_reason, constraint_other_description | date (parent), activity_code, trade_code, status=status_at_end_of_day, pct start/end | 1 per row with non-empty notes (13 in the backup). Header `<project> \| Daily report <date> \| Activity <code> <desc> \| trade <t> \| <p0>%-><p1>% \| <status>` | Parent missing → delete chunk, do not index (142/172 orphans) |
| daily_presence + daily_report_attendance | own tenant/project (models.py:1476-1478). Attendance resolves through daily_report_id (project_access.py:252) | presence: trade, company_name, sub_name, zone_name, notes; attendance: **notes only** | date (parent), trade_code | 1 "manpower" roll-up per report. Header `<project> \| Daily report <date> \| Manpower`. One line per row: `<trade> - <company> - <headcount> x <hours>h - <notes>` | Strip the template note "Auto-created from activity grid crew dropdown" (daily_reports_router.py:1440). **Never embed attendance `name`** (personal data) |
| deliveries | own (iter5_router.py:952,1029-1033) | vendor, description, po_number | date=delivery_date, status, activity_code (linked_task_ids → tasks) | 1 per delivery. Header `<project> \| Delivery <date> \| <vendor> \| PO <po> \| <status> \| activities <codes>` | status `deleted` → remove. Leave received_by out of the text |
| safety_events | own (models.py:2263-2264) | description, event_type, reasons | date, severity, status, activity_code | 1 per event. Header `<project> \| Safety <type> \| <severity> \| <date> \| <status> \| activities <codes>` | Possible health information about named workers: same visibility as today |
| qc_issues | own (models.py:2359-2360) | description, resolution_notes, rework_outcome_notes | date, severity, status, activity_code | 1 per issue | status deleted → remove |
| delays | own (models.py:2134-2135) | description, resolved_note; ai_report as a second chunk kind `delay_ai_report` | date, severity, reason_category, cause_class, status, activity_code, `synthetic` (paperwork / quality rework) | 1 per delay. Header `[Delay] <date> \| <severity> \| <reason> \| status <s> \| tasks <codes> \| project <name>` | Down-weight synthetic rows |
| rfis | own (models.py:1939-1940) | subject, description, response_text, to/from party | date=date_initiated, status, priority, trade_code, activity_code, rfi_number | 1 per RFI; split into question and answer above 1,500 tokens. Header `[RFI <n>] <subject> \| status \| priority \| trade \| spec \| tasks \| initiated` | Keep cost_impact_estimated_usd out of the text |
| submittals | own (iter4_router.py:256-257) | title, type_other_description, sla_breach_reason, plus non-empty `submittal_review_events.notes` | date, status, sla_status, trade_code=discipline, spec_section_code, activity_code | 1 per submittal. Header `[Submittal <n> rev <r>] spec <code> \| <type> \| <discipline> \| sub <x> \| status/SLA \| activity` | Aging and SLA counters are kept **out of the hash** |
| coordination_events | own (models.py:3153-3154) | summary (fallback: description, then topic), detail_text, outcome_notes, party name/org/role | date=occurred_at, topic, channel, outcome, trade_code, activity_code | 1 per event; split above 1,500 tokens | **Never embed or store other_party_email or other_party_phone.** deleted_at set → remove |
| work_items | own (models.py:2674-2675) | title, description, root_cause, closure_summary | date=opened_at, status, priority, activity_code (through work_item_links with entity_type=task) | 1 per item | Keep actual_cost_impact out (iter7_router.py:553). work_item_links writes requeue the parent |
| change_orders (critic a1) | own; project_access.py:268 | title, description | date, status | 1 per change order | amount_usd is metadata only. Soft delete (iter8_router.py:73) → remove |
| kb_entries (critic a2) | own tenant; project_id optional (company-level allowed, iter7_router.py:504-505) | title, body, tags | source_kind, date | 1 per entry. **Index only source_kind in (manual, lesson)** to avoid copying RFI and work-item chunks | **Strip `IMPACT: cost=$…`** (iter7_router.py:~553-554) |
| master_docs (critic a4) | own tenant/project | notes, site_constraints, primary_subs | govt_security_level | 1 per project | Skip entirely unless `govt_security_level == "public"` (iter7_router.py:593) |
| mitigations | own (ai_agents.py:596) | strategy_name, assumption, proposed_action_description, ai_reasoning, prerequisites, decision_rationale, outcome_notes | date, decision_status, scenario_label, linked_delay_id, activity_code | 1 per row. Header `[Mitigation <label> for delay <short text> \| <decision_status> \| <date>]` | Rank chosen/executed above superseded/not_chosen. Not in any cascade list |
| recovery_reports | own (models.py:3708-3711) | ai_narrative, recovery_actions[].action/rationale/owner | date, report_type, status | narrative chunk(s) plus 1 per action | Only status draft/finalized. **allowed_roles = {admin, pm, super, px, pe, foreman, viewer}** (recovery_reports_router.py:38). Never embed storage_key |
| tasks | own (models.py:978-981) | activity_code, description, display_name, section_path, wbs, trade_code, resource_names | date (planned_start/finish), activity_code, trade_code, status, is_milestone, is_critical_path | 1 per task (longest description 108 characters). Header `[Activity <code>] <path> \| trade <t> \| <start>-<finish> \| status <s>` | status deleted → remove. Content hash is essential (see 3.4) |
| projects | the row is the project | name, contract_number, client/owner/contractor, agency, address, project_type | status, contract_type, dates | 1 per project | Never contract_value_usd or file pointers. Ignore writes that touch only the forecast or MS Project pointers |
| subcontractors, suppliers | tenant; company-level (project_id null) | name, trades, contact_name, notes | kind, trade_code | 1 per vendor | Never contact_email or contact_phone |
| crews | tenant; project_id=home_project_id or null | name, crew_code, trade, company_name, notes | trade_code, status | 1 per crew | status deleted → remove. Never hourly_burdened_cost |
| equipment | tenant; company-level | asset_tag, type, manufacturer, model, year, ownership | status | 1 per asset | Never the rates |
| locations | own (models.py:484-497) | name, code, location_type, floor name | location_type | 1 per location | Filter status deleted (copilot soft delete) |
| photo_reviews | tenant; project through daily_report_id (project_access.py:253) | label, text, finding lines | date, lens, severity | 1 per review | Apply `is_dev_stub_result` (photo_ai.py:649-663). Parent missing → skip (all 5 backup rows are orphans) |
| **Phase 3:** spec_sections | tenant; project through project_spec_id → project_specs (iter5_router.py:763-779) | section_title, text_content, section_code, division, hierarchy_path | division, section_code, pages, project_spec_id | Split by PART/article, about 800-1,500 tokens with overlap. Header `Spec <code> <title> \| Division <d> \| <project>/<spec> <rev> \| pages a-b` | Never copy the existing `embedding` / `embedding_kind` fields |
| **Phase 3:** photos | own (models.py:2453-2454) | caption, tag_caption, tags, photo_class, plus the joined photo_tray ai_suggested_caption / ai_safety_concerns_detected | date, photo_class, activity_code, linked event ids | 1 per photo that has any text | Dev-stub filter on joined tray data; no GPS in the text; status deleted → remove |

#### Query-only (no chunks; the assistant's structured `query` tool or a join reads them)
photo_tray (a join source for photos), schedule_proposals (optional "import summary" chunk later: filename + ai_review.summary + review_notes), schedule_update_diffs, logic_proposals (REVIEW_ROLES only, schedule_review_router.py:33), ai_recovery_suggestions, project_calendar_days, submittal_review_events (notes folded into the submittal), approvals, gating_entities, project_specs (spec name and revision for headers; when it reaches `complete` at spec_ingest.py:138, requeue its sections), cost_rate_categories, trade_taxonomy (lookup table used to map trades), imports, field_suggestions, activity_name_suggestions.
- **Correction:** `confirm_suggestion` does NOT write the value onto the parent record (field_suggestions.py:318-387; router :235-246). If task chunks should show a confirmed required_trade, join the confirmed suggestions.

#### Excluded (with reason)
- **Personal data or secrets:** users, tenant_users, team_invites (redeemable code), runtime_settings (worker token), voice_pins (sign-in PIN, iter7_router.py:~985-1044), auth_attempts, people and certifications (names, phone, hourly_rate, e_verify_status; models.py:1750-1783), project_members (access source, not content), payment_transactions.
- **Privileged or system:** tia_generations (counsel-privileged, models.py:3266,3323), audit_log (whole copies of records plus old plaintext secrets), copilot_messages (the model's own output; private per user).
- **Logs, telemetry, computed numbers:** task_sync_events, cascade_snapshots, crew_capacity, crew_demands, productivity_observations, ai_call_timings, ai_usage_tracking, photo_ai_locks, cron_runs, digest_runs, backup_runs, migrations, files, notifications, work_item_history, mitigation_outcomes (duplicates qc rework notes), edit_requests, calendars, crew_assignments, equipment_assignments, floors (used only for location headers), schedule_jobs / schedule_job_seq / schedule_job_watermark / ms_project_sync_state, field_activity_code_counters, drawings_spatial_metadata, audit_chain_heads, project_access_watch, tenants (display_name only, read at header-build time), work_item_links (a join that requeues its parent).
- **Out of scope in this version:** voice_annotations (voice text that matters is already appended to daily_reports.notes, dr_voice_router.py:450-460); drawings, drawing_revisions, drawing_markups (deferred to phase 4; markups default to `visibility: "personal"`, iter3_router.py:53, so when they arrive they must be private=true with an owner); marketplace_postings / marketplace_responses (shared across companies by design, iter5_router.py:1180-1246; they do not fit a model partitioned by tenant_id).
- **photo_ai_calibration:** before excluding it for good, check that `photo_tray.ai_user_override_reason` really holds the human correction text (photo_tray_router.py:1525-1537). If it does not, fold the reason into the photo chunk.

### 3.2 `search_chunks` document shape
```
_id: "<collection>:<source_id>:<n>"
tenant_id, project_id (null = company-level), kind (e.g. "daily_report","dr_activity","manpower","delay","delay_ai_report","rfi","submittal","coordination","work_item","change_order","kb","master_doc","mitigation","recovery_report","task","project","vendor","crew","equipment","location","photo_review","spec","photo"),
source_collection, source_id, chunk_no, parent_id (e.g. daily_report_id),
date (ISO date string), activity_code [array], trade_code [array], status,
private (bool, default false), owner_user_id (null), allowed_roles (null = every role),
synthetic (bool), header, body, text (header+body that was embedded),
embedding (1024 floats, or BSON binary vector), model ("voyage-4"),
content_hash, indexed_at, source_updated_at
```

### 3.3 `search_queue`
```
_id: "<collection>:<source_id>"   (one pending job per record; upsert)
tenant_id, project_id, collection, source_id, op ("upsert"|"delete"|"purge_project"|"purge_tenant"),
status ("pending"|"claimed"|"done"|"failed"), attempts, available_at, lease_until, claimed_by,
enqueued_at, last_error
```
- Enqueueing is an upsert that resets `status=pending` and `available_at=now`. Repeated writes collapse into a single job.
- Purge jobs use `_id: "purge:<tenant>:<project>"`.

### 3.4 Hook points: how writes reach the queue
There are three layers. The reconcile (3.7) is the safety net that makes small misses tolerable.

**Layer A, the central hook in `write_audit` (`audit.py:176`).**
- Most user-facing saves already call it (158 call sites), and it carries `tenant_id`, `table_name`, `op`, `record_id` and `after` (`audit.py:192-202`).
- After the audit row is written, call `await search_enqueue_from_audit(table_name, record_id, tenant_id, op)`.
- This must never raise, and must do nothing when the flag is off.
- A mapping from `table_name` to collection lives in the registry. Unknown table names are logged once.
- This covers, among others, these create/update/delete routes:
  - daily_reports (daily_reports_router.py:316, 1758, 2158, 2215, 2601, 2971, 512)
  - delays (field_events_router.py:501, 744/789, 921, 984, 938)
  - rfis (docs_router.py:289, 443/513)
  - qc / safety (field_events_router.py:1194, 1298, 1391, 1547)
  - coordination (coordination_router.py:93, 276, 322)
  - deliveries (iter5_router.py:1034, 1117)
  - generic CRUD (iter8_router.py:192, 329, 378)
  - project create/update (projects_router.py:111, 240)
  - vendors (vendors_router.py:85, 113, 138)
  - copilot write tools: copilot_tools.py:851/876/919/947/971/992/1029/1062/1124/1180/1203/1225/1261/1334/1496/1558/1627/1650, plus **the generic `_tool_delete_entity` :1380 and `_tool_update_entity` :1433, which also write tasks, change_orders, kb_entries, people and floors through DELETABLE_ENTITIES (copilot_tools.py:1340-1347)**.
- Each route must be checked for whether it audits. Wherever it does not, add a Layer B call.

**Layer B, explicit `enqueue(collection, id, tenant_id)` calls at writes that skip the audit.** These are mostly service-layer writes and child rows:
- **tasks (high volume, numbers only):** cpm.py:582, enterprise_cpm.py:650, task_status.py:268/405/974/1132 (status refresh runs on every list read, tasks_router.py:163), schedule_feedback.py:378/405/413/443/592/784/879, ms_project_sync.py:895, daily_reports_router.py:403/426/891/1291/1815, schedule_proposals_router.py:414/436/446, schedule_review_router.py:227, iter6_router.py:679. Migrations 021-026 recompute through cpm.py:582.
  - **The worker computes `content_hash` over the text fields plus the filter fields and skips the embed when nothing changed.** Better still, the enqueue helper takes the post-write document (or projection) and compares hashes before it enqueues at all, so CPM and status churn never reaches the queue.
- **daily_report_activities:** 269, 819, 2921, 1196, 1810, 3074 (written on a GET), dr_voice_router.py:536, iter8_router.py:291, copilot_tools.py:1558/1650, schedule_feedback.py:307/498, delete 859 and 479.
  - **Correction:** `stamp_add_metadata` / `stamp_edit_metadata` (edit_permissions.py:459/420, called at daily_reports_router.py:822/1484) match **zero rows** here, because `tenant_filter` adds a tenant_id that these rows do not have. Do not treat them as content writes.
  - The enqueue takes tenant_id from the parent report.
- **daily_presence** (rolls up into the parent report's manpower chunk): 2668, 1440, 2778, 2807, 480, dr_voice_router.py:581, copilot_tools.py:1496. Attendance writes iter5_router.py:557 and :583 enqueue the same manpower chunk.
- **daily_reports outside routes:** dr_voice_router.py:457 (voice notes append) and no_work_day.py:104.
- **Inserts from voice apply:** dr_voice_router.py:708/740/781/808/832/857.
- **Other services and paths:**
  - photo_tray_router.py:1920 (safety event from a photo)
  - qc_rework_cascade.py:123/126/199/220
  - tasks_router.py:208/254/284 (paperwork blocker delays)
  - mitigations: ai_agents.py:543/596/615, mitigations_router.py:228/241/246/329/348/456, field_events_router.py:763
  - recovery: recovery_report.py:312/849/892/934/970, recovery_reports_router.py:192/235/247/253 (**written through `get_db()`, so a grep for `db.` misses them**)
  - submittals: submittal_sla.py:516/664/738/905/969/1048/1359/1622 (timers only; the hash absorbs them) and :825 (review event notes → parent submittal), spec_ingest.py:207, iter6_router.py:843/892, copilot_tools.py:1461
  - kb_entries: iter7_router.py:484/529/558
  - master_docs: iter7_router.py:613
  - change_orders: docs_router.py:603
  - work_item_links: docs_router.py:677 → parent work item
  - projects: forecast_refresh.py:70 and ms_project_internal_router.py:117/138 only change numbers (the hash skips them)
  - crews / equipment / locations: resources_router.py:54/77, projects_router.py:514/542, iter4_router.py:854, iter8_router.py:541
- **Phase 3:** spec_ingest.py:120 (insert_many) and project_specs completion at :138. **Do NOT enqueue** on spec_search.py:60/89, which only write the old embedding field. Photo AI writes on photo_tray (photo_ai.py:1209, 1222, 1297, 1949, 2057, 2152, 2254, 2327; photo_tray_router.py:782, 1058, 1174, 1401, 1979) requeue `photos` where `tray_photo_id` equals the tray row's id.
- Seed and migration writes (seed.py, migrations 001-028, scripts/restore_backup.py:61,65) are not hooked. The backfill and the reconcile cover them.

**Layer C, the reconcile (3.7).** It catches anything the first two layers missed.

### 3.5 Deletion and purge hooks
- **Row deletes** go through Layer A/B with `op=delete`: the worker removes all `search_chunks` with that `source_collection` and `source_id`. Soft deletes (status `deleted`, `deleted_at` set, recovery report archived) are treated as deletes by the chunker returning zero chunks.
- **Report delete** (daily_reports_router.py:441-513, 479-480, 493-505): enqueue a delete for the report, its activities and its manpower chunk. Also delete photo_review chunks by `parent_id`, because there is no cascade for that collection.
- **Project delete** (projects_router.py:284-324):
  - Just before the `delete_many` loop (:316), call `search_purge(tenant_id, project_id)`.
  - It deletes `search_chunks {tenant_id, project_id}` and `search_queue {tenant_id, project_id}` synchronously.
  - It also inserts a `purge_project` marker that the worker honours for 10 minutes, dropping any job for that project whose parent cannot be found, so a job already in flight cannot put chunks back.
- **Company close** (auth_router.py:926-934): call `search_purge(tenant_id)` the same way.
- **Do not rely on the cascade lists** (projects_router.py:274-281, auth_router.py:863-875). They miss mitigations, spec_sections, photo_reviews, work_items, change_orders, kb_entries, crews and more, and they contain ghost names that no code uses ("resources", "specs", "schedule_imports", "schedule_revisions", "marketplace_listings", "memberships").

### 3.6 Worker
- **Where it runs:** `app.state.search_queue_task` in `lifespan` (server.py:502), in the same shape as `_periodic_forecast_refresh` (server.py:965-979).
  - First sleep 60-120s so the flag cache is loaded.
  - Poll every 2-5s.
  - Re-raise `CancelledError`.
  - Add the name to the shutdown tuple (server.py:992-1009).
- **Claiming jobs:** use `find_one_and_update({status:"pending", available_at<=now} → claimed, lease_until=now+5min)`, copied from ms_project_sync.py:619. A reaper puts expired leases back to pending. This stays safe if Railway runs more than one replica.
- **Processing each job:**
  1. Load the source row.
  2. Resolve scope (parent joins as in 3.1).
  3. Apply the **outside-provider gate** (3.8).
  4. Build chunks and compute their hashes.
  5. Embed only the chunks whose hash changed (Voyage batches of up to 128).
  6. Upsert the changed chunks and delete chunk numbers that no longer exist.
  7. Mark the job done.
- **Failures:** back off (30s, 2m, 10m, 1h). After 6 attempts mark the job `failed` and log it.
- **Budget:** cap embed calls per minute. Log tokens to `ai_usage_tracking`, following ai_agents.py:45-57.

### 3.7 Backfill and nightly reconcile
- **Backfill:** a one-time job registered in `services/one_time_migrations.py` (pattern at :141-214). It **only enqueues**, page by page across every indexed collection, and the worker does the embedding, so the 2-hour stale-claim rule (:138) is never hit. It must be safe to run against a restored backup (warning at :15-20). It runs only when the flag is on.
- **Nightly reconcile:** a `while True` loop that runs once per night (around 02:00 Eastern).
  - (a) Every indexed source row whose hash or `updated_at` differs from its chunk → enqueue.
  - (b) Every chunk whose source row, parent report, project or tenant no longer exists → delete. This handles the orphans described above.
  - (c) Queue rows stuck as `failed` → report a count to the log and the admin page.
- **Restore** (scripts/restore_backup.py:39-65): it drops and reinserts everything but only rebuilds regular indexes. After a restore, recreate the search indexes and run a full reindex.
- **Backup** (backup_router.py:90-100): leave `search_chunks` and `search_queue` out of the backup, since both can be rebuilt and the vectors would inflate the zip.

### 3.8 Outside-provider compliance gate (a requirement, not a choice)
Voyage is an outside AI provider. Photo AI already refuses to send data for `is_cui` projects or for companies with `ai_enabled=false` (photo_ai.py:423-462 `check_cui_safety`; exhibit_exports.py:354-384). Reuse `check_cui_safety`. The worker **does not embed** a row when:
- its project has `is_cui=true`, or
- its company has `ai_enabled=false`, or
- its master_doc `govt_security_level != "public"`.

These records stay findable only through the keyword (`$search`) path, which runs inside Atlas. The same gate applies to the query text: a query that is scoped to a CUI project runs keyword-only. See section 9 for an offline-model option for such projects.

### 3.9 Test that fails when a collection is unclassified
`tests/test_search_registry.py`:
- (1) Statically scan `backend/**/*.py` with a regex for `db\.(\w+)`, `get_db\(\)\.(\w+)`, `db\[["'](\w+)["']\]` and the string lists in the cascade constants.
- (2) Assert that every name found is a key in `registry.COLLECTIONS`, apart from an explicit `IGNORED_NAMES` set for the ghost names.
- (3) Assert that every `index` entry has a chunker, a scope resolver and hash fields.
- (4) Assert that no `exclude` collection appears in any chunker.
- (5) Fixture tests per chunker, using backup-shaped sample documents. Check that the header is correct, that forbidden fields (email, phone, rates, cost, GPS, storage keys, `IMPACT: cost`) are absent from `text`, and that deleted, dev-stub and orphan rows produce zero chunks.

The test needs no database, so it always runs.

---

## 4. Phase 2: hybrid search, rerank, the assistant tool, model routing

### 4.1 Index definitions
**Vector index** `search_chunks_vec`:
```json
{ "fields": [
  {"type":"vector","path":"embedding","numDimensions":1024,"similarity":"dotProduct","quantization":"scalar"},
  {"type":"filter","path":"tenant_id"}, {"type":"filter","path":"project_id"},
  {"type":"filter","path":"kind"}, {"type":"filter","path":"date"},
  {"type":"filter","path":"activity_code"}, {"type":"filter","path":"trade_code"},
  {"type":"filter","path":"status"}, {"type":"filter","path":"private"},
  {"type":"filter","path":"owner_user_id"}, {"type":"filter","path":"allowed_roles"},
  {"type":"filter","path":"model"} ] }
```
dotProduct requires normalized vectors. Voyage vectors are normalized (general knowledge; check in phase 0 by measuring the vector lengths).

**Atlas Search index** `search_chunks_text`:
```json
{ "mappings": { "dynamic": false, "fields": {
  "header": {"type":"string","analyzer":"lucene.english"},
  "body":   {"type":"string","analyzer":"lucene.english"},
  "activity_code": [{"type":"token"},{"type":"string","analyzer":"lucene.keyword"}],
  "tenant_id":{"type":"token"}, "project_id":{"type":"token"}, "kind":{"type":"token"},
  "trade_code":{"type":"token"}, "status":{"type":"token"}, "private":{"type":"boolean"},
  "owner_user_id":{"type":"token"}, "allowed_roles":{"type":"token"},
  "date":{"type":"token"} } } }
```
The keyword analyzer on activity_code lets exact codes such as "A1240", "RFI-014" and "09 29 00" match.

**Regular indexes:**
- `search_chunks`: `{source_collection:1, source_id:1, chunk_no:1}` unique; `{tenant_id:1, project_id:1}`; `{tenant_id:1, parent_id:1}`; `{tenant_id:1, indexed_at:1}` (reconcile).
- `search_queue`: `{status:1, available_at:1}`; `{status:1, lease_until:1}` (reaper); `{tenant_id:1, project_id:1}` (purge).

Create them in `_ensure_indexes_bg` (server.py:519) using `SearchIndexModel`, with a failure that only logs (Atlas Flex limits; see Risks).

### 4.2 Query pipeline
1. **Build the permission filter** (4.3).
2. **Embed the query** with Voyage `voyage-4`, `input_type="query"`. Skip this step when the compliance gate applies.
3. **Merge with `$rankFusion`** of two pipelines:
   - `$vectorSearch` (limit 40, numCandidates 400, with the filter);
   - `$search` compound (`must` text on header^2 and body, plus a `should` exact match on activity_code, with `filter` clauses mirroring the permission filter; limit 40).
   - Weights start at vector 1.0, text 1.0, to be tuned in phase 5.
   - **Fallback when the cluster is older than 8.1:** run both pipelines and do reciprocal rank fusion (k=60) in Python.
4. **Rerank** the top 30 with Voyage `rerank-3-lite` (use the exact name from the task; confirm the model id in phase 0) and keep the top 8.
5. **Rehydrate and re-check** (4.3, step 4).
6. **Return** citations: `{"count","results":[{"cite":"S1","kind","source_collection","source_id","project_id","label","date","activity_code","rfi_number|section_code","excerpt"(≤400 chars),"score","url"}]}`. The total must stay well under the assistant's 12,000-character tool-result cap (copilot_tools.py:1979-1980).

Endpoint: `GET /search?q=&project_id=&kind=&date_from=&date_to=` in a new `routers/search_router.py`, behind the flag and `require_tenant`, with `project_id` checked by `require_project_if_given` (services/project_scope.py:48-58).

### 4.3 Permission model: results must equal what the user can see today
1. **Company:** always `tenant_id = ctx.tenant_id`. The model or the user never chooses it.
2. **Project:** `ids = await allowed_project_ids(ctx)` (project_access.py:126).
   - If `None` (not enforced: FF_PROJECT_ACCESS off and not a restricted admin, project_access.py:101-105), there is no project filter. This matches today, where every company member sees every project.
   - Otherwise `project_id ∈ ids ∪ {null}`. Company-level chunks (vendors, crews, equipment, company-level kb entries) stay visible, matching `include_company_level=True`.
   - If a `project_id` argument is given, check it first with `check_project_access` (project_access.py:188-224) and filter to that project only.
3. **Role and private:**
   - `allowed_roles` is null or contains `ctx.role`. Admin is always allowed, matching `role_required` (auth.py:427-436).
   - **Correction:** there is no "subcontractor" role. The roles shut out of recovery reports are `submittal_tech` and `scheduling_engineer` (models.py:74-77; recovery_reports_router.py:38). Also, recovery reports always check project membership unless the caller is an all-projects admin, whatever the flag says (recovery_reports_router.py:45-83). So recovery chunks get an extra rule: when not enforced, still require `has_project_access`.
   - `private=false`, or `owner_user_id = ctx.user_id`, or `is_full_admin` (project_access.py:81). Nothing in phase 1-3 is private. The field exists for drawing markups (phase 4) and chat history.
4. **Rehydrate (defence in depth):** for the final 8 hits, load the source rows with `tenant_filter` and drop any hit whose row:
   - is missing, deleted or archived;
   - is a dev-stub AI result (`is_dev_stub_result`, photo_ai.py:649-696);
   - or fails `filter_rows(ctx, rows)` (project_access.py:422). For rows without a project, use the resolved parent project.
   Then apply `redact_secrets` (auth.py:245-261) to the excerpt. A stale chunk therefore can never show something the list page would hide.
5. **Never in results:** any excluded collection. audit_log and copilot_messages are never indexed, so the assistant's admin-only rule (copilot_tools.py:463-468) cannot be bypassed through search.
6. **A separate fix:** `kb_search.py:31-33` scopes knowledge-base entries **by company only**, and `_kb_block` puts them into every turn (copilot_tools.py:1752). That leaks across projects once restricted access is on. When the flag is on, route `_kb_block` through `search_everything` with `kind="kb"`.

### 4.4 The assistant tool `search_everything`
- Add `_tool_search_everything(ctx, args)` after `_tool_search_specs` (copilot_tools.py:~336). It uses `ctx["_access"]` (copilot_tools.py:1885-1891) and `_guard(..., write=False)` for an explicit project.
- Register it in `TOOL_REGISTRY` (:1657-1688). Do **not** add it to `WRITE_TOOLS` (:241-250).
- Catalogue entry after :164: "Use for free-text or cross-record questions (what happened, who said, when did). Cite results as [S1], or with the native forms [A1240], RFI-014, 09 29 00 (rule at :238)."
- **Citations to the screen:** `system_router.py:224` sets `citations=[]`. Collect the `results` from `res["tool_results"]` where `tool=="search_everything"`. In `frontend/src/components/ai/CopilotPanel.jsx` near :176-213, render the citations as links that open the source record.
- While the flag is off or the index is still filling, keep `search` (iter5_router.py:731-760) and `search_specs` as fallbacks. When the flag is on, `search` delegates to the new pipeline.
- **Separate bugs** (not part of this work; list them): `is_critical` is used at copilot_tools.py:609/657/661/702/195, but tasks hold `is_critical_path`; `"trade"` is projected but tasks hold `trade_code`; `query` on daily_report_activities always returns zero rows (:529-534); `ALLOWED` lists `schedule_diffs`, but the real collection is `schedule_update_diffs`.

### 4.5 Model routing: Haiku for lookups, Sonnet for harder questions
- Replace `MODEL_NAME` (copilot_tools.py:146) with `MODEL_FAST` (Haiku) and `MODEL_DEEP` (Sonnet), stored as runtime settings so they can be changed without a deploy. **Confirm the exact model ids with one live call in phase 0.** A bad id comes back as "Anthropic 404 … likely bad model name" (anthropic_compat.py:~289-292).
- `_pick_model(message, daily_report_id, photos)`, called before :1874. It classifies only the text after `=== New message ===`, because CopilotPanel.jsx:157-162 prepends the conversation history.
  - **Sonnet** when: photos are attached (:1832-1858); the message contains write verbs (create, add, update, delete, mark, record); it asks for analysis (why, impact, compare, recommend, delay analysis, report, draft); it is longer than about 400 characters; or a daily report is in focus.
  - **Otherwise Haiku**: lookups such as "when did…", "who was on site…", "find the RFI about…".
- **Escalation in the middle of a turn:** after :1917-1921, switch with `chat.with_model("anthropic", MODEL_DEEP)` if Haiku's JSON fails to parse, a tool returns an error, or the plan sets `"escalate": true` (add this to the schema at :153). History survives the switch (anthropic_compat.py:176, 181-193).
- **Record the model used** in `copilot_messages` (:1999-2012) and in `ai_usage_tracking`.

---

## 5. Phase 3: specs and photo captions

- **Specs:**
  - Index `spec_sections` as described in 3.1.
  - The scope resolver joins project_specs and stores project_id on each chunk.
  - Re-ingest rule: there is no delete, and a re-upload adds a second set of sections (spec_ingest.py:118-120). When `processing_status` changes to `complete`, rebuild every chunk for that `project_spec_id` and delete the chunks of sections that are no longer the latest set. Chunk ids are stable: spec id + section_code + chunk number.
  - When the flag is on, `search_specs` (copilot_tools.py:299-336) and the top-3 spec block injected into context (:1773-1794) use the new search with `kind="spec"`.
  - Retire the TF-IDF fallback (spec_search.py, embeddings.py) one release later.
- **Photo captions:**
  - Index `photos` with joined tray AI captions and safety concerns.
  - Apply the dev-stub filter.
  - Skip photos with no text.
  - Keep GPS out of the text.
  - Photo AI writes on photo_tray requeue the linked photos (3.4).
  - Untagged tray photos stay unsearchable (decision: query_only).

## 6. Phase 5: evaluation, latency, load

- **Evaluation set:** about 150 questions written against a seeded copy of the test database. Each question has an expected source id (or ids).
  - Coverage: every indexed kind; exact codes; vague wording; date filters; and **permission cases**, where a user without project access, a scheduling_engineer asking about recovery reports, deleted rows, dev-stub reviews and orphaned activities must return nothing.
  - **Targets:**
    - recall@8 ≥ 0.90
    - mean reciprocal rank ≥ 0.70
    - leaks = **0**, a hard fail
    - citation accuracy (the cited source supports the answer) ≥ 0.95 on a 40-question assistant subset graded by Sonnet as judge plus human spot checks
    - routing: at least 80% of lookups stay on Haiku and no write goes through Haiku
- **Latency targets:**
  - `/search` p50 ≤ 600 ms, p95 ≤ 1.5 s (query embed about 150 ms, fusion about 200 ms, rerank about 200 ms)
  - assistant lookup turn on Haiku p50 ≤ 4 s
  - Sonnet turn p95 ≤ 20 s, under the 60-second client timeout in CopilotPanel.jsx:167
  - indexing lag from a write to a searchable chunk p95 ≤ 60 s
  - backfill throughput ≥ 50 records/s
- **Staging load test:**
  - Run on the Railway staging environment with its own Atlas cluster, never "live".
  - Replay the 200-superintendent baseline (a prior project load test) plus 20 searches/s.
  - Run a bulk CPM recompute at the same time to prove the hash keeps tasks off the queue.
  - Pass if p95 targets hold, queue depth returns to 0 within 5 minutes, and request latency does not regress more than 10%.

---

## 7. Rollout order
Phase 0 → phase 1 with the flag on in staging only → backfill → phase 2 eval → flag on in live for the internal company → phase 3 → phase 5 sign-off → everyone.

## 8. Risks

1. **Atlas Flex limits:** the live database is Atlas Flex (ice-prod). Check that Flex allows both search indexes and supports MongoDB 8.1 for `$rankFusion`. If not, use Python reciprocal rank fusion, or move to a dedicated tier.
2. **Write coverage:** some writes skip `write_audit`. The service layer and `get_db()` writes are easy to miss with a grep. Mitigation: Layer B, the nightly reconcile and the registry test.
3. **Churn from numeric writes:** tasks (CPM, status refresh on read) and submittals (SLA timers) could flood the queue and Voyage spend. Mitigation: compare hashes before enqueueing.
4. **Orphans and missing cascades** (daily_report_activities, photo_reviews, mitigations, spec_sections, kb_entries, change_orders, work_items): mitigated by purging by tenant and project, by parent checks in the worker, and by the reconcile.
5. **Outside-provider compliance:** CUI projects, `ai_enabled=false` companies and non-public master docs must never reach Voyage (3.8).
6. **Permission drift:** if an endpoint's rules change and the chunk filters do not, results could differ from the screens. Mitigation: rehydrate and re-check (4.3, step 4) plus the leak tests in the eval set.
7. **Personal and commercial data in text:** forbidden-field assertions in the chunker tests (emails, phones, rates, cost impact, GPS, storage keys, attendance names).
8. **Backup and restore:** search collections bloat the backup, and Atlas search indexes are not restored. Exclude them from the backup and reindex after a restore.
9. **Driver upgrade:** Motor and pymongo must move together, and BSON behaviour must be re-checked.
10. **Model ids:** Haiku, Sonnet, voyage-4 and rerank-3-lite ids must be confirmed live before shipping.
11. **Cost:** cap embeds per minute, log usage, and add a monthly ceiling like the photo AI budget (photo_ai.py:563).

---

## 9. The user's question: EmbeddingGemma offline, stored in the browser?

This is design advice, not checked against code.

- **Can it work?** Yes. EmbeddingGemma (about 300 million parameters; 768-dimension vectors that can be shortened to 512, 256 or 128) runs in the browser through Transformers.js / ONNX Runtime Web, preferably on WebGPU. The quantized model is roughly 150-200 MB. It downloads once and is cached in browser Cache Storage or IndexedDB, then works without a connection.
- **What the browser should store:** only the viewer's own offline subset. These are the records the offline daily-report layer already prefetches for that user's projects, with their vectors in IndexedDB, and only ones they are allowed to see today. Whole-database search, and its company, project, role and private filtering, must stay on the server. A browser copy of the whole company database would bypass those permission rules.
- **Vectors cannot be mixed:** EmbeddingGemma vectors (768 dims) and Voyage vectors (1,024 dims) cannot be compared with each other. The options are:
  - (a) Keep the server on Voyage, and let the browser embed its small offline cache itself with EmbeddingGemma (a separate local index; nothing to sync).
  - (b) Also run EmbeddingGemma on the server through the existing litellm hook (`services/embeddings.py`, which works with any OpenAI-compatible `EMBED_API_BASE`). Store a second vector field marked `model:"embeddinggemma"` and ship the precomputed vectors down with the prefetch.
- **Recommendation:** (a) for offline daily-report search, as a later phase.
- **Bonus:** a self-hosted EmbeddingGemma on the server is also the way to give **CUI projects** vector search without sending text to an outside provider. Index them with `model:"embeddinggemma"` in a second 768-dimension vector index.

Key files referenced: C:/Users/admin/Desktop/ICE/backend/audit.py, C:/Users/admin/Desktop/ICE/backend/database.py, C:/Users/admin/Desktop/ICE/backend/services/project_access.py, C:/Users/admin/Desktop/ICE/backend/services/copilot_tools.py, C:/Users/admin/Desktop/ICE/backend/services/runtime_settings.py, C:/Users/admin/Desktop/ICE/backend/server.py, C:/Users/admin/Desktop/ICE/backend/routers/projects_router.py, C:/Users/admin/Desktop/ICE/backend/routers/auth_router.py, C:/Users/admin/Desktop/ICE/frontend/src/components/ai/CopilotPanel.jsx.