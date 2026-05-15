// Date helpers — keep "today" in the user's local timezone.
//
// `new Date().toISOString().slice(0, 10)` returns the UTC calendar date.
// At ~8 PM Eastern that's already tomorrow's UTC date, so resolved_date
// fields filled from this default ended up one day ahead of what the
// user typed. Use `localTodayISO()` instead — it formats the local date
// from the user's wall clock.
export function localTodayISO() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

// Same idea for an arbitrary Date object — local YYYY-MM-DD, not UTC.
export function localISODate(d) {
  if (!(d instanceof Date)) return "";
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

// CPM Part F (2026-05-15) -- single display-format point for date
// columns app-wide. Every visible date string (Tasks, Lookahead,
// Daily Reports, RFIs, Deliveries, Delays, QC, Safety, Photos,
// Weekly Reports, Approvals, Drawings, Dashboard) routes through
// this one helper so the format can be flipped in one place.
//
// CPM Part G (2026-05-15) -- format flipped from ISO pass-through
// to MM/DD/YYYY (US construction industry convention; matches the
// hand-written display format on field forms, RFI letterhead, and
// daily-report PDFs). STORAGE LAYER unchanged: every Mongo write,
// API payload, CSV export, and <input type="date"> still uses
// ISO YYYY-MM-DD; this helper is display-only.
//
// Input contract:
//     YYYY-MM-DD             -> MM/DD/YYYY
//     YYYY-MM-DDTHH:MM:SS    -> MM/DD/YYYY (time portion discarded)
//     null / "" / undefined  -> "—" (em-dash)
//     unrecognized format    -> pass-through (defense-in-depth so
//                                a future date column with an
//                                already-formatted string doesn't
//                                render as "—" or break)
//
// Output is intentionally a fixed-length 10-char string so it
// aligns in tabular-num columns (Tasks page Impacted Start /
// Finish, Lookahead Plan start / Plan finish, etc.). 4-digit year
// per the updated Part G spec (not 2-digit "YY") -- a 10-char
// MM/DD/YYYY string still fits the existing column widths and
// avoids the 19xx/20xx ambiguity that 2-digit years introduce.
export function formatDateDisplay(iso) {
  if (!iso) return "—";
  // Take the first 10 chars in case the input is an ISO datetime
  // string with a time portion (e.g., "2026-05-15T13:00:00Z").
  const s = String(iso).slice(0, 10);
  // Strict YYYY-MM-DD shape. Anything else falls through untouched
  // (defensive -- a caller that already passes "05/15/2026" shouldn't
  // get "—").
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return s;
  const [, yyyy, mm, dd] = m;
  return `${mm}/${dd}/${yyyy}`;
}

// CPM Part F (2026-05-15) -- slip between planned_finish and
// projected_finish_date in whole calendar days. Returns 0 when
// either input is missing OR when the projection equals planned
// (no slip) OR when the projection is BEFORE planned (acceleration
// -- not a slip; we keep the slip semantic to "days lost"). Callers
// render "—" when this returns 0 and "+N" when > 0.
//
// Defensive: any parse error returns 0 rather than NaN/"?", so the
// Slip column never renders garbage on malformed dates.
export function slipDays(plannedIso, projectedIso) {
  if (!plannedIso || !projectedIso) return 0;
  try {
    const p = new Date(String(plannedIso).slice(0, 10) + "T00:00:00");
    const pr = new Date(String(projectedIso).slice(0, 10) + "T00:00:00");
    const diff = Math.round((pr - p) / 86400000);
    return diff > 0 ? diff : 0;
  } catch {
    return 0;
  }
}
