// Bucket D Phase 3 -- shared helper for resolving daily_report_activity
// IDs (stored on event collections via linked_dr_activity_ids) into the
// chip-render data {id, dr_date, activity_code, activity_description}.
//
// Pattern: gallery pages collect the union of all visible
// linked_dr_activity_ids and call this hook with that array. One
// round-trip to GET /projects/{pid}/dr-activity-chips returns the
// denormalized chip data; pages render via the chips lookup map.
import { useEffect, useState } from "react";
import api from "./api";

export function useDrActivityChips(projectId, activityIds) {
  const [chips, setChips] = useState({});
  // Stable join key so the effect doesn't re-fire on identical arrays.
  const key = (activityIds || []).slice().sort().join(",");
  useEffect(() => {
    if (!projectId || !key) { setChips({}); return; }
    api.get(`/projects/${projectId}/dr-activity-chips`, {
      params: { ids: key },
    }).then(({ data }) => {
      const map = {};
      for (const c of (data || [])) map[c.id] = c;
      setChips(map);
    }).catch(() => setChips({}));
  }, [projectId, key]);
  return chips;
}

// Pure render helper for an activity chip badge.
export function chipLabel(c) {
  if (!c) return null;
  return `${c.activity_code || c.id.slice(0, 6)}`;
}

export function chipTooltip(c) {
  if (!c) return null;
  const date = c.dr_date ? `[${c.dr_date}] ` : "";
  return `${date}${c.activity_code || ""} ${c.activity_description || ""}`.trim();
}
