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
// columns in Tasks + Lookahead. For Part F we pass through the ISO
// YYYY-MM-DD form (matches the existing planned_start / planned_finish
// rendering). CPM Part G (next ship) will swap this helper's body to
// MM/DD/YY so the entire app changes format from one place -- every
// caller already routes through formatDateDisplay().
//
// Returns "—" (em-dash) on null/empty/undefined input so the Impacted
// Start + Impacted Finish columns don't render literal "null" or
// blank on tasks with no projected dates yet (fresh schedule import,
// no events logged).
export function formatDateDisplay(iso) {
  if (!iso) return "—";
  const s = String(iso).slice(0, 10);
  // Part F: pass-through. Part G will replace this with MM/DD/YY.
  return s;
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
