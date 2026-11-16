// Progress lock (2026-11-16).
//
// Once a daily report is submitted (or approved), the progress it shows for
// an activity is locked: a later report can go further but not back, and a
// back-dated report can't claim more than a later submitted one. Going back
// means editing the report that recorded the higher value.
//
// The server stamps every grid row with the allowed range (GET grid and the
// report bundle): progress_floor / progress_floor_from (report date,
// YYYY-MM-DD) and progress_ceiling / progress_ceiling_from. A save outside
// it is refused with 422 and code "progress_out_of_range". These helpers
// let both screens (desktop and phone) stop the slider at the limits and
// say why in plain words.

export const PROGRESS_OUT_OF_RANGE_CODE = "progress_out_of_range";

/** "2026-11-16" -> "11/16". */
export function reportDay(iso) {
  const parts = String(iso || "").slice(0, 10).split("-");
  if (parts.length !== 3) return String(iso || "");
  return `${Number(parts[1])}/${Number(parts[2])}`;
}

/** The row's allowed range: {floor, floorFrom, ceiling, ceilingFrom}. */
export function progressLimitsOf(row) {
  const r = row || {};
  const floorFrom = r.progress_floor_from || null;
  const ceilingFrom = r.progress_ceiling_from || null;
  const floor = floorFrom && typeof r.progress_floor === "number" ? r.progress_floor : 0;
  const ceiling = ceilingFrom && typeof r.progress_ceiling === "number" ? r.progress_ceiling : 100;
  return { floor, floorFrom, ceiling, ceilingFrom };
}

/** `n` moved into the allowed range. */
export function clampToProgressLimits(row, n) {
  const { floor, ceiling } = progressLimitsOf(row);
  let v = Number(n);
  if (!Number.isFinite(v)) v = 0;
  if (v < floor) v = floor;
  if (v > ceiling) v = ceiling;
  return v;
}

/** Plain note for under the slider when it sits at a limit, else "". */
export function progressLimitNote(row, value) {
  const { floor, floorFrom, ceiling, ceilingFrom } = progressLimitsOf(row);
  const v = Number(value);
  if (floorFrom && floor > 0 && v <= floor) {
    return `Can't go below ${floor}% — set on the ${reportDay(floorFrom)} report. Edit that report to lower it.`;
  }
  if (ceilingFrom && ceiling < 100 && v >= ceiling) {
    return `Can't go above ${ceiling}% — set on the ${reportDay(ceilingFrom)} report. Edit that report first to raise it here.`;
  }
  return "";
}

/** True when a failed request was refused by the progress lock. */
export function isProgressOutOfRange(err) {
  const d = err?.response?.data;
  return err?.response?.status === 422 && d && d.code === PROGRESS_OUT_OF_RANGE_CODE;
}

/** The server's plain message for that refusal. */
export function progressOutOfRangeMessage(err) {
  const d = err?.response?.data || {};
  return (typeof d.detail === "string" && d.detail)
    || "That progress is outside what the submitted reports allow.";
}
