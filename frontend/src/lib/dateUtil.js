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

// CPM Part A frontend follow-up (2026-05-15) -- predecessor display
// formatter. Migration 018 (Part A) converted tasks.predecessors from
// a List[str] of bare activity codes ("1.5") to a List[Dict] with
// the shape {code, type, lag_days} so PDM relationships (FS / SS /
// FF / SF) and lag/lead can be stored. The frontend now receives
// EITHER shape:
//
//     bare string         -- legacy task that has not been touched
//                            since pre-018 OR a brand-new task whose
//                            edit-form save flow split a comma-
//                            separated input back into bare strings
//     dict { code, type, lag_days }
//                         -- post-018 canonical shape
//
// Without this helper, the Tasks page / ScheduleProposals diff
// rendered dict entries as "[object Object]" via the bare
// .join(", ") on a List[Dict]. This helper is DISPLAY-ONLY -- it
// does not parse back to dict on save, so any edit-form input that
// uses formatPredecessors to PRE-FILL the input would lose dict
// info on round-trip. Display-only call sites (table columns, diff
// renderers) are safe.
//
// Format contract (5 cases per Part-A frontend spec):
//
//     "1.5"                                   -> "1.5"
//     {code:"1.5", type:"FS", lag_days:0}     -> "1.5"
//     {code:"22",  type:"FS", lag_days:-3}    -> "22FS-3d"
//     {code:"13",  type:"SS", lag_days:2}     -> "13SS+2d"
//     {code:"5",   type:"FF", lag_days:7}     -> "5FF+7d"
//
// FS+0 entries collapse to bare code (matches the legacy display).
// Non-FS or non-zero-lag entries render the full {code}{type}{sign}{lag}d
// shorthand familiar from MS Project / Primavera P6.
export function formatPredecessor(p) {
  if (typeof p === "string") return p;        // legacy bare string
  if (!p || typeof p !== "object") return ""; // null / undefined / weird
  const code = p.code || "";
  const type = p.type || "FS";
  const lag = p.lag_days || 0;
  if (type === "FS" && lag === 0) return code; // common case
  const sign = lag >= 0 ? "+" : "";
  return `${code}${type}${sign}${lag}d`;
}

export function formatPredecessors(preds) {
  if (!Array.isArray(preds)) return "—";
  if (preds.length === 0) return "—";
  return preds.map(formatPredecessor).join(", ");
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
