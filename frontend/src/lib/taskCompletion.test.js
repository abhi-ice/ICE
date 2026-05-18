// Feature F5 (2026-05-18) -- completed-task predicates.
//
// These predicates decide which tasks a task-linking picker hides by
// default. Every picker (RFIs / delays / QC / safety / deliveries /
// photos) routes its completed-vs-not decision through here, so a bug
// here would change every picker at once -- worth pinning.

import { isMasterTaskComplete, isDrActivityComplete } from "./taskCompletion";

describe("isMasterTaskComplete (master-schedule task)", () => {
  test("status 'complete' -> complete (hidden by default)", () => {
    expect(isMasterTaskComplete({ status: "complete", pct_complete: 0 })).toBe(true);
  });

  test("pct_complete 100 -> complete even if status lags", () => {
    expect(isMasterTaskComplete({ status: "in_progress", pct_complete: 100 })).toBe(true);
  });

  test("in-progress task -> NOT complete (stays visible in the picker)", () => {
    expect(isMasterTaskComplete({ status: "in_progress", pct_complete: 60 })).toBe(false);
  });

  test("not-started task -> NOT complete", () => {
    expect(isMasterTaskComplete({ status: "not_started", pct_complete: 0 })).toBe(false);
  });

  test("blocked task -> NOT complete", () => {
    expect(isMasterTaskComplete({ status: "blocked", pct_complete: 40 })).toBe(false);
  });

  test("missing pct_complete -> NOT complete (no false positive)", () => {
    expect(isMasterTaskComplete({ status: "in_progress" })).toBe(false);
  });

  test("null / undefined -> NOT complete (never crashes)", () => {
    expect(isMasterTaskComplete(null)).toBe(false);
    expect(isMasterTaskComplete(undefined)).toBe(false);
  });
});

describe("isDrActivityComplete (DR activity-grid row)", () => {
  test("status_at_end_of_day 'complete' -> complete", () => {
    expect(isDrActivityComplete({
      status_at_end_of_day: "complete", pct_complete_end_of_day: 0,
    })).toBe(true);
  });

  test("EOD% 100 -> complete even if status lags", () => {
    expect(isDrActivityComplete({
      status_at_end_of_day: "in_progress", pct_complete_end_of_day: 100,
    })).toBe(true);
  });

  test("EOD% 40 -> NOT complete (stays visible in the picker)", () => {
    expect(isDrActivityComplete({
      status_at_end_of_day: "in_progress", pct_complete_end_of_day: 40,
    })).toBe(false);
  });

  test("zero-progress row -> NOT complete", () => {
    expect(isDrActivityComplete({
      status_at_end_of_day: "not_started", pct_complete_end_of_day: 0,
    })).toBe(false);
  });

  test("null / undefined -> NOT complete (never crashes)", () => {
    expect(isDrActivityComplete(null)).toBe(false);
    expect(isDrActivityComplete(undefined)).toBe(false);
  });
});
