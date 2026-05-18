// Feature F5 (2026-05-18) -- "is this task / activity already complete?"
//
// A completed task is hidden BY DEFAULT from every task-linking picker
// (RFIs, delays, QC issues, safety events, deliveries, photos). Each
// picker carries a "Show completed tasks" toggle (default OFF) that
// reveals them; these predicates are the single source of truth for
// what "completed" means so every picker agrees.
//
// Two shapes:
//   * master-schedule tasks  -> pct_complete + status   ("complete")
//   * DR activity-grid rows  -> pct_complete_end_of_day +
//                               status_at_end_of_day    ("complete")

// A master-schedule task (from /projects/{id}/tasks) is complete when
// its status is "complete" OR its percent-complete has reached 100.
export function isMasterTaskComplete(task) {
  if (!task) return false;
  if (task.status === "complete") return true;
  return Number(task.pct_complete ?? 0) >= 100;
}

// A DR activity-grid row (from /daily-reports/{id}/grid) is complete
// when its end-of-day status is "complete" OR its EOD% has reached 100.
export function isDrActivityComplete(row) {
  if (!row) return false;
  if (row.status_at_end_of_day === "complete") return true;
  return Number(row.pct_complete_end_of_day ?? 0) >= 100;
}
