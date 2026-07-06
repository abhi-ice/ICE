// Bucket D Fix G -- shared helper to resolve "today's DR" for a project.
//
// Used by every gallery "+ New" button to route the user to today's
// DR with a createEvent param so the DR page scrolls to the right
// inline-create panel. Backend POST /daily-reports is idempotent on
// (project, date) -- returns the existing DR if today's already exists,
// or creates a fresh one. Locked DRs (status=submitted/approved) flip
// the caller into the supplement flow.
import api from "./api";
import { localTodayISO } from "./dateUtil";

// Local wall-clock date, NOT toISOString().slice (which is the UTC day and,
// at ~8 PM Pacific, is already tomorrow -> the "+ New" button would open
// tomorrow's DR). See lib/dateUtil.js.
function isoToday() { return localTodayISO(); }

export async function resolveTodaysDr(projectId) {
  if (!projectId) throw new Error("projectId required");
  const today = isoToday();
  const { data } = await api.post("/daily-reports", {
    project_id: projectId, report_date: today,
  });
  return data;  // {id, report_date, status, ...}
}

// Convenience wrapper: resolve today's DR + return the navigation URL
// the gallery should push the user to. If the DR is locked, the caller
// should open SupplementWizard instead (caller checks the returned
// `locked` flag).
export async function navigateToTodaysDrForCreate(projectId, createEvent) {
  const dr = await resolveTodaysDr(projectId);
  const locked = dr.status === "submitted" || dr.status === "approved";
  return {
    dr, locked,
    url: locked
      ? null   // caller should open SupplementWizard instead
      : `/daily-reports?openId=${dr.id}&createEvent=${createEvent}`,
  };
}
