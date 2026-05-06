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
