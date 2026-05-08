// Bucket D Phase 3 -- inline chip rendering for linked_dr_activity_ids.
// Used by the Photos / Delays / RFIs / QC / Safety / Deliveries /
// Coordination gallery pages. Pulls the chip data via the shared
// useDrActivityChips hook (one round-trip for the whole page's union
// of ids).
import { Link } from "react-router-dom";
import { useDrActivityChips, chipLabel, chipTooltip } from "../../lib/drActivityChips";

export default function GalleryActivityChips({ projectId, activityIds, max = 4 }) {
  const ids = Array.isArray(activityIds) ? activityIds : [];
  const chips = useDrActivityChips(projectId, ids);
  if (!ids.length) return <span className="text-slate-300 text-xs">—</span>;
  const visible = ids.slice(0, max);
  const overflow = ids.length - visible.length;
  return (
    <span className="inline-flex items-center gap-1 flex-wrap">
      {visible.map((id) => {
        const c = chips[id];
        if (!c) {
          // Still loading or chip missing -- render placeholder.
          return (
            <span key={id} className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-400 font-mono">
              {id.slice(0, 6)}
            </span>
          );
        }
        return (
          <Link key={id} to={`/daily-reports?openId=${c.daily_report_id}`}
            title={chipTooltip(c)}
            className="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 border border-blue-200 text-blue-800 hover:bg-blue-100 inline-flex items-center gap-1">
            <span className="font-mono">{chipLabel(c)}</span>
            {c.dr_date && <span className="text-blue-500">[{c.dr_date.slice(5)}]</span>}
          </Link>
        );
      })}
      {overflow > 0 && (
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
          +{overflow}
        </span>
      )}
    </span>
  );
}
