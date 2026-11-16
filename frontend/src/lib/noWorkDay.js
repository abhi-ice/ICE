// Heading for a daily report filed as a no-work day:
//   "No work day — Weather", "No work day — Holiday: Thanksgiving",
//   "No work day — <typed note>" for reason "other", or "No work day".
// Mirror of backend/services/no_work_day.py — keep the labels in step so
// the screens and the exports use the same words.

export const NO_WORK_DAY_TEXT = "No work day";

export const NO_WORK_REASON_LABELS = {
  weather: "Weather",
  holiday: "Holiday",
  scheduled_non_work: "Scheduled non-work day",
  crew_unavailable: "Crew unavailable",
  tenant_restriction: "Tenant or owner access restricted",
  other: "Other",
};

export function noWorkReasonText(report) {
  const reason = String(report?.no_work_reason || "").trim();
  const note = String(report?.no_work_note || "").split(/\s+/).filter(Boolean).join(" ");
  // "Other" is only a placeholder; the typed note is the reason.
  if (reason === "other" && note) return note;
  const label = NO_WORK_REASON_LABELS[reason]
    || (reason ? reason.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "");
  if (label && note) return `${label}: ${note}`;
  return label || note;
}

// "No work day — <reason>" for a no-work-day report, otherwise null.
export function noWorkHeading(report) {
  if (!report || !report.is_no_work_day) return null;
  const reason = noWorkReasonText(report);
  return reason ? `${NO_WORK_DAY_TEXT} — ${reason}` : NO_WORK_DAY_TEXT;
}
