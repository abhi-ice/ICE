// Bucket D Phase 3 + Corrective (2026-05-08) -- inline chip rendering
// for event row task linkage. Used by Photos / Delays / RFIs / QC /
// Safety / Deliveries / Coordination gallery pages.
//
// Backing data: useTaskChips fetches /projects/{pid}/tasks once per
// project and indexes by id. Chips link to /tasks/{id} (master
// schedule), since the corrective renamed the field from
// linked_dr_activity_ids (per-DR scope) to linked_task_ids (master
// schedule scope). The prop name (activityIds) stays for backward
// compat with the existing gallery callers; pass linked_task_ids in.
import { Link } from "react-router-dom";
import { useTaskChips, chipLabel, chipTooltip } from "../../lib/drActivityChips";

export default function GalleryActivityChips({ projectId, activityIds, max = 4 }) {
  const ids = Array.isArray(activityIds) ? activityIds : [];
  const chips = useTaskChips(projectId, ids);
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
            <span key={id} className="text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-400 font-mono">
              {id.slice(0, 6)}
            </span>
          );
        }
        return (
          <Link key={id} to={`/tasks/${id}`}
            title={chipTooltip(c)}
            className="text-xs px-1.5 py-0.5 rounded bg-blue-50 border border-blue-200 text-blue-800 hover:bg-blue-100 inline-flex items-center gap-1">
            <span className="font-mono">{chipLabel(c)}</span>
            {c.is_critical_path && (
              <span className="text-rose-600">CP</span>
            )}
          </Link>
        );
      })}
      {overflow > 0 && (
        <span className="text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
          +{overflow}
        </span>
      )}
    </span>
  );
}
