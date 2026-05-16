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


// CPM Bug 3.B (2026-05-16) -- working-day helpers, JS mirror of
// backend/services/working_days.py. Slip / float scalars must be
// WORKING-DAY counts (Mon-Fri), consistent with the working-day
// Impacted dates (Part L) and the working-day impact values supers
// enter. A PM who logs a "5 day" delay must see Slip "+5", not the
// calendar "+7" that a Fri->Fri span produced pre-Bug-3.B.
//
// JS Date.getDay(): Sun=0, Mon=1, ..., Fri=5, Sat=6.
export function isBusinessDay(d) {
  const wd = d.getDay();
  return wd >= 1 && wd <= 5;
}

// Count of working days from `startIso` (inclusive) to `endIso`
// (exclusive). Negative when end < start. Mirrors the Python
// working_days_between() semantics exactly:
//   between(Mon, Mon)      == 0
//   between(Mon, Tue)      == 1   (Mon is the only WD in range)
//   between(Fri, next Mon) == 1   (Fri only; Sat/Sun skipped)
//   between(Mon, next Mon) == 5   (Mon..Fri)
// Defensive: parse failure / missing input -> 0.
export function workingDaysBetween(startIso, endIso) {
  if (!startIso || !endIso) return 0;
  try {
    const start = new Date(String(startIso).slice(0, 10) + "T00:00:00");
    const end = new Date(String(endIso).slice(0, 10) + "T00:00:00");
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
      return 0;
    }
    if (start > end) {
      return -workingDaysBetween(endIso, startIso);
    }
    let count = 0;
    const cur = new Date(start);
    while (cur < end) {
      if (isBusinessDay(cur)) count += 1;
      cur.setDate(cur.getDate() + 1);
    }
    return count;
  } catch {
    return 0;
  }
}

// CPM Part F (2026-05-15) -- slip between planned_finish and
// projected_finish_date.
//
// CPM Bug 3.B (2026-05-16) -- slip is now a WORKING-DAY count, not
// calendar. Pre-Bug-3.B this returned (projected - planned) in
// whole calendar days, so a 5-working-day delay whose Impacted
// Finish landed the following Friday showed Slip "+7" -- two
// phantom weekend days the super never entered. Now slip ==
// working_days_between(planned_finish, projected_finish), which
// equals the working-day impact the super logged.
//
// Returns 0 when either input is missing OR when the projection
// equals planned (no slip) OR when the projection is BEFORE planned
// (acceleration -- not a slip). Callers render "—" when this
// returns 0 and "+N" when > 0. Defensive: parse error -> 0.
export function slipDays(plannedIso, projectedIso) {
  if (!plannedIso || !projectedIso) return 0;
  const diff = workingDaysBetween(plannedIso, projectedIso);
  return diff > 0 ? diff : 0;
}
