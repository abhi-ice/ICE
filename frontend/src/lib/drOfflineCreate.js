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
 * Scope: the provisional report has an EMPTY activity grid (grid rows are
 * generated server-side from the schedule) — planned activities appear after
 * the first online sync. Weather/unplanned/voice notes work offline; photos
 * hang off activity rows and so need the synced grid.
 */
import { primeOfflineGet, readCachedGet } from "./api";
import { enqueueRequest } from "./drOfflineSync";

export function offlineDrId(projectId, reportDate) {
  return `offline-dr-${projectId}-${reportDate}`;
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

  // The detail page loads ONE bundle endpoint (GET /daily-reports/{id}/context)
  // — plant it (plus the small side endpoints) so the page renders offline.
  primeOfflineGet(`/daily-reports/${tempId}/context`, {
    report, project,
    grid: [], presence: [], photos: [], voice_annotations: [],
    delays: [], safety_events: [], qc_issues: [], rfis: [],
    deliveries: [], audit_log: [], user_labels: {},
  });
  primeOfflineGet(`/daily-reports/${tempId}`, report);
  primeOfflineGet(`/daily-reports/${tempId}/grid`, []);
  primeOfflineGet(`/daily-reports/${tempId}/presence`, []);
  primeOfflineGet(`/daily-reports/${tempId}/delay-reminders`, []);
  primeOfflineGet(`/daily-reports/${tempId}/photo-reviews`, []);

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
