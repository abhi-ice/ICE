// v7 guided Daily Report flow (2026-07-05) — shared constants + the
// locked-DR reason card for the mobile guided flow (GuidedDrFlow in
// pages/DailyReports.jsx). Mobile-only surface: nothing here renders >=768px.
//
// COMMON_TRADES moved here from pages/DailyReports.jsx (pt.47/pt.62) so both
// the desktop Manpower Log and the flow's crew/unplanned sheets share one
// list without an import cycle (the page imports this module).
import { Lock } from "lucide-react";

// pt.47 (2026-05-28): trades a federal GC typically self-performs or subs
// out, "Other" pinned last. pt.62: alphabetical (localeCompare).
export const COMMON_TRADES = [
  ...[
    "Acoustical Ceilings", "Carpentry / Framing", "Concrete", "Demolition",
    "Doors / Hardware", "Drywall", "Earthwork / Excavation", "Electrical",
    "Fire Protection / Sprinkler", "Fireproofing", "Flooring", "General Labor",
    "Glazing", "HVAC / Mechanical", "Insulation", "Landscaping / Sitework",
    "Low-Voltage / Data", "Masonry", "Painting", "Plumbing", "Roofing",
    "Steel / Structural", "Tile",
  ].sort((a, b) => a.localeCompare(b)),
  "Other",
];

// Locked-DR reason threshold — mirrors ADD_REASON_MIN on the page (Bucket D
// Addition 5). In the flow, ONE reason (typed once on any activity screen)
// feeds BOTH edit_reason (grid PATCH) and add_reason (photo tags); the
// issue sheets keep their own per-event LockedAddReasonBlock.
export const FLOW_REASON_MIN = 10;
export const isFlowReasonValid = (v) => (v || "").trim().length >= FLOW_REASON_MIN;

// Spec B — compact locked-DR reason card at the top of each activity screen.
export function LockedReasonCard({ value, onChange }) {
  const ok = isFlowReasonValid(value);
  return (
    <div className={`dr-flow-reason${ok ? " ok" : ""}`} data-testid="dr-flow-locked-reason-card">
      <div className="t"><Lock className="h-3.5 w-3.5" /> Editing a submitted report — reason required</div>
      <textarea data-testid="dr-flow-locked-reason" value={value || ""} rows={2}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Why is this changing after submit?" />
      <div className="s">{ok ? "✓ Applied to every change on this report" : `${(value || "").trim().length} / ${FLOW_REASON_MIN} min chars`}</div>
    </div>
  );
}
