/**
 * Offline Daily Report creation (2026-07-06 — field-test round 2).
 *
 * Opening a NEW report from the calendar (or the gallery "+ New") while
 * offline used to die with "Network Error". Now the create falls back to a
 * PROVISIONAL report:
 *
 *   1. A deterministic temp id — "offline-dr-<projectId>-<date>" — so a
 *      double-tap reuses the same provisional report instead of forking two.
 *   2. GET-cache entries are planted (via api.primeOfflineGet, same
 *      tenant-scoped keys as the read-through cache) for the /context bundle
 *      the detail page loads, so the report OPENS offline immediately.
 *   3. The real POST /daily-reports is queued (lib/drOfflineSync) with an
 *      offlineCreate marker. On reconnect the drain replays it, learns the
 *      server's real id, and REWRITES every queued child mutation
 *      (crew / issues / unplanned / weather) from the temp id to the real
 *      one before replaying them. The backend create is idempotent on
 *      (project_id, report_date), so replays can never duplicate a report.
 *
 * PLANNED ACTIVITIES (round 3, same day): the grid is SYNTHESIZED offline
 * from the cached project schedule (/projects/{id}/tasks — prefetched by
 * lib/drPrefetch): every task whose effective window (projected dates
 * falling back to planned) covers the report date becomes a provisional
 * row with id "offline-row-<tempDrId>__<taskId>". Everything logged against
 * those rows — EOD%, photos, crew — carries either the STABLE
 * schedule_activity_id (crew/issues) or the provisional row id, which the
 * drain resolves to the real server row (matched by schedule_activity_id
 * on the synced report's grid) before replaying. If no tasks are cached,
 * the grid is empty and activities appear after the first online sync.
 */
import { primeOfflineGet, readCachedGet } from "./api";
import { enqueueRequest } from "./drOfflineSync";

export function offlineDrId(projectId, reportDate) {
  return `offline-dr-${projectId}-${reportDate}`;
}

export function offlineRowId(tempDrId, taskId) {
  // "__" separator: neither uuids nor the temp DR id contain it, so the
  // drain can split on the LAST "__" unambiguously.
  return `offline-row-${tempDrId}__${taskId}`;
}

/** Synthesize the report-date's planned Field Work rows from the cached
 *  schedule. Mirrors the server's slip-aware window: projected dates win
 *  over planned; a task overlaps when start <= date <= finish (ISO string
 *  compare — all dates are YYYY-MM-DD). Best-effort: no cached tasks -> []. */
async function synthesizeGridRows(projectId, reportDate, tempId) {
  let tasks = null;
  try { tasks = await readCachedGet(`/projects/${projectId}/tasks`); } catch { /* none */ }
  if (!Array.isArray(tasks)) return [];
  const day = String(reportDate).slice(0, 10);
  const rows = [];
  for (const t of tasks) {
    if (!t || !t.id) continue;
    // Match the server's grid prepopulation: complete/deleted tasks never
    // become grid rows (edits on them would be dropped at sync).
    if (t.status === "complete" || t.status === "deleted") continue;
    // Slip-aware effective window — the API serializes projected dates as
    // projected_start_date / projected_finish_date (mirrors
    // services/schedule_filters.py effective_start/effective_finish).
    const start = String(t.projected_start_date || t.planned_start || "").slice(0, 10);
    const finish = String(t.projected_finish_date || t.planned_finish || "").slice(0, 10);
    if (!start || !finish || start > day || finish < day) continue;
    const pct = Number(t.pct_complete || 0);
    rows.push({
      id: offlineRowId(tempId, t.id),
      daily_report_id: tempId,
      schedule_activity_id: t.id,
      activity_code: t.activity_code || null,
      activity_description: t.description || t.activity_code || "Activity",
      work_category: t.work_category || "field_work",
      trade_code: t.trade_code || null,
      pct_complete_start_of_day: pct,
      pct_complete_end_of_day: pct,
      hours_worked: null,
      status_at_end_of_day: null,
      auto_status: null,
      notes: null,
      _offline: true,
    });
  }
  return rows;
}

export async function createDrOffline(projectId, reportDate) {
  if (!projectId || !reportDate) throw new Error("projectId + reportDate required");
  const tempId = offlineDrId(projectId, reportDate);

  // Project doc for the /context bundle — best-effort from the cached
  // projects list (the read-through cache holds it after any online visit).
  let project = { id: projectId, name: "(project — offline)" };
  try {
    const cachedProjects = await readCachedGet("/projects");
    const hit = Array.isArray(cachedProjects)
      ? cachedProjects.find((p) => p && p.id === projectId)
      : null;
    if (hit) project = hit;
  } catch { /* keep the minimal stub */ }

  const report = {
    id: tempId,
    project_id: projectId,
    report_date: reportDate,
    status: "draft",
    _offline: true,
    weather_conditions: null,
    weather_temp_high_f: null,
    weather_temp_low_f: null,
    weather_precipitation_in: null,
    weather_wind_mph: null,
    weather_impact_minutes: 0,
    weather_fetched_at: null,
    notes: null,
    created_at: new Date().toISOString(),
  };

  // Planned activities (round 3): build the report-date's Field Work rows
  // from the cached schedule so the guided flow walks real activities
  // offline — EOD%, crew, issues, and photos all log against them.
  const grid = await synthesizeGridRows(projectId, reportDate, tempId);

  // The detail page loads ONE bundle endpoint (GET /daily-reports/{id}/context)
  // — plant it (plus the small side endpoints) so the page renders offline.
  primeOfflineGet(`/daily-reports/${tempId}/context`, {
    report, project,
    grid, presence: [], photos: [], voice_annotations: [],
    delays: [], safety_events: [], qc_issues: [], rfis: [],
    deliveries: [], audit_log: [], user_labels: {},
  });
  primeOfflineGet(`/daily-reports/${tempId}`, report);
  primeOfflineGet(`/daily-reports/${tempId}/grid`, grid);
  primeOfflineGet(`/daily-reports/${tempId}/presence`, []);
  primeOfflineGet(`/daily-reports/${tempId}/delay-reminders`, []);
  primeOfflineGet(`/daily-reports/${tempId}/photo-reviews`, []);

  // Show the provisional report on the cached DR list too, so the list page
  // reflects it offline (best-effort — the real list replaces this on sync).
  try {
    const listUrl = `/projects/${projectId}/daily-reports`;
    const cachedList = await readCachedGet(listUrl);
    if (Array.isArray(cachedList) && !cachedList.some((r) => r && r.id === tempId)) {
      primeOfflineGet(listUrl, [report, ...cachedList]);
    }
  } catch { /* best-effort */ }

  // Queue the real create. FIFO ordering + the drain's id remap take it from
  // here; dedupe inside enqueueRequest keeps double-taps to one entry.
  enqueueRequest({
    method: "POST",
    url: "/daily-reports",
    data: { project_id: projectId, report_date: reportDate },
    offlineCreate: { tempId },
  });

  return report;
}
