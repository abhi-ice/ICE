/**
 * Tenant-wide offline warm-up (2026-07-06 — field-test round 3).
 *
 * While online, pre-download into the read-through GET cache (IndexedDB,
 * lib/offlineCache via the api interceptor) everything a super needs to work
 * a Daily Report with zero signal — for EVERY project in the tenant:
 *
 *   - the project's DR list
 *   - the /context bundle of reports dated yesterday / today / tomorrow
 *     (the ones a field crew actually opens) plus the 5 most recent
 *   - the project's schedule tasks (/projects/{id}/tasks) — REQUIRED to
 *     synthesize planned activities when a NEW report is created offline
 *     from the calendar (lib/drOfflineCreate)
 *   - the subcontractors list (crew add sheet)
 *
 * All fire-and-forget GETs through the authenticated api client; the
 * response interceptor mirrors them into IndexedDB. Failures are ignored —
 * a warm is opportunistic, never load-bearing. Throttled to one sweep per
 * 10 minutes per tenant session (sessionStorage stamp).
 */
import { localTodayISO, localISODate } from "./dateUtil";
import { getToken } from "./auth";

const THROTTLE_MS = 10 * 60 * 1000;
const STAMP_KEY = "ice-offline-warm-at";
const RECENT_PER_PROJECT = 5;
// Deep-warm only the most-recently-active projects (user spec, round 5):
// their schedule tasks + report bundles. Every project's report LIST is
// still cached (it powers the ranking and costs one small GET each).
const TOP_PROJECTS = 5;

function nearDates() {
  const today = localTodayISO();
  const base = new Date(`${today}T00:00:00`);
  const y = new Date(base); y.setDate(y.getDate() - 1);
  const t = new Date(base); t.setDate(t.getDate() + 1);
  return new Set([localISODate(y), today, localISODate(t)]);
}

export async function warmOfflineCache(api, { force = false } = {}) {
  try {
    if (typeof navigator !== "undefined" && !navigator.onLine) return;
    // Signed-out visitor: the 401 would bounce public pages to /login.
    if (!getToken()) return;
    if (!force) {
      const last = Number(sessionStorage.getItem(STAMP_KEY) || 0);
      if (Date.now() - last < THROTTLE_MS) return;
    }
  } catch { /* sessionStorage unavailable — warm anyway */ }

  try {
    const { data: projects } = await api.get("/projects");
    if (!Array.isArray(projects) || !projects.length) return;
    // Stamp AFTER the first fetch succeeds — a pre-login or transient
    // failure must not consume the 10-minute window (the 5s post-load warm
    // can fire before token hydration; the next trigger should retry).
    try { sessionStorage.setItem(STAMP_KEY, String(Date.now())); } catch { /* ok */ }
    const near = nearDates();
    api.get("/subcontractors").catch(() => {});
    // Rank projects by activity (newest report date) — fetching each DR
    // list also mirrors it into the cache, so ranking costs nothing extra.
    const ranked = [];
    for (const p of projects) {
      if (!p || !p.id) continue;
      // Schedule tasks warm for EVERY project (small JSON): a report created
      // offline from the calendar synthesizes its activity grid from these,
      // and that must work even on a project outside the deep-warm top 5.
      api.get(`/projects/${p.id}/tasks`).catch(() => {});
      try {
        const { data: reports } = await api.get(`/projects/${p.id}/daily-reports`);
        const rows = Array.isArray(reports) ? reports : [];
        // Guard the ranking against a null/missing report_date —
        // String(null) is "null", which compares GREATER than any ISO date
        // and would steal a deep-warm slot for an inactive project.
        const newest = rows.reduce(
          (m, r) => (r && r.report_date && String(r.report_date) > m
            ? String(r.report_date) : m), "",
        );
        ranked.push({ p, rows, newest });
      } catch { /* per-project best-effort */ }
    }
    ranked.sort((a, b) => b.newest.localeCompare(a.newest));
    for (const { p, rows } of ranked.slice(0, TOP_PROJECTS)) {
      const nearby = rows.filter(
        (r) => r && near.has(String(r.report_date).slice(0, 10)),
      );
      const recent = [...rows]
        .sort((a, b) => String(b.report_date).localeCompare(String(a.report_date)))
        .slice(0, RECENT_PER_PROJECT);
      const ids = [...new Set([...nearby, ...recent].map((r) => r.id).filter(Boolean))];
      for (const rid of ids) {
        api.get(`/daily-reports/${rid}/context`).catch(() => {});
      }
    }
  } catch { /* unauthenticated or transient — next trigger retries */ }
}

/** Wire the automatic warms: shortly after app load (token ready) and on
 *  every reconnect. Idempotent. */
let _wired = false;
export function wirePrefetch(api) {
  if (_wired) return;
  _wired = true;
  try {
    window.addEventListener("online", () => { warmOfflineCache(api); });
    // First warm ~5s after load — after login/token hydration, off the
    // critical rendering path.
    setTimeout(() => { warmOfflineCache(api); }, 5000);
  } catch { /* non-window env */ }
}
