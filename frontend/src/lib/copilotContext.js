/**
 * Tiny event-emitter store shared between the DailyReports page and the
 * floating Copilot panel. When a DR is open we publish its id here, the
 * Copilot reads it on every send so the LLM gets DR-scoped context + the
 * 3 DR-only tools (record_presence, mark_activity_status, create_unplanned_activity).
 */
let activeDailyReportId = null;
const subs = new Set();

export function setActiveDailyReportId(id) {
  activeDailyReportId = id || null;
  subs.forEach((fn) => {
    try { fn(activeDailyReportId); } catch (_) { /* ignore */ }
  });
}
export function getActiveDailyReportId() {
  return activeDailyReportId;
}
export function subscribeDailyReportId(fn) {
  subs.add(fn);
  fn(activeDailyReportId);
  return () => subs.delete(fn);
}
