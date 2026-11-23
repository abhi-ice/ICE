// Crew sign-in sheet + payroll hours workbook (Excel), built from the crew
// rows and worker names on the daily reports. "day" = this report's date;
// "week" = Monday to Sunday around it (a payroll week).
import api from "./api";
import { toast } from "../components/ui/sonner";

function iso(d) { return d.toISOString().slice(0, 10); }

export function signInRange(reportDate, kind) {
  const day = String(reportDate || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  if (kind !== "week") return [day, day];
  const d = new Date(`${day}T00:00:00Z`);
  const back = (d.getUTCDay() + 6) % 7; // days since Monday
  const mon = new Date(d.getTime() - back * 86400000);
  const sun = new Date(mon.getTime() + 6 * 86400000);
  return [iso(mon), iso(sun)];
}

export function signInUrl(projectId, reportDate, kind) {
  const r = signInRange(reportDate, kind);
  if (!projectId || !r) return null;
  return `/projects/${projectId}/crew-sign-in/export?start=${r[0]}&end=${r[1]}`;
}

export const SIGN_IN_LABELS = {
  signin_day: "Sign-in sheet (this day)",
  signin_week: "Payroll hours (this week)",
};

export async function downloadCrewSignIn(projectId, reportDate, kind) {
  const url = signInUrl(projectId, reportDate, kind);
  if (!url) { toast.error("This report has no project or date."); return; }
  try {
    const res = await api.get(url, { responseType: "blob" });
    const cd = res.headers?.["content-disposition"] || "";
    const m = cd.match(/filename="?([^";]+)"?/i);
    const name = (m && m[1]) || `crew_sign_in_${String(reportDate).slice(0, 10)}.xlsx`;
    const blobUrl = URL.createObjectURL(new Blob([res.data], { type: "application/octet-stream" }));
    const a = document.createElement("a");
    a.href = blobUrl; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(blobUrl);
    toast.success(`${kind === "week" ? SIGN_IN_LABELS.signin_week : SIGN_IN_LABELS.signin_day} downloaded`);
  } catch (e) {
    let body = e.response?.data;
    if (body instanceof Blob) { try { body = JSON.parse(await body.text()); } catch { body = null; } }
    toast.error(`Export failed -- ${typeof body?.detail === "string" ? body.detail : "try again"}`);
  }
}
