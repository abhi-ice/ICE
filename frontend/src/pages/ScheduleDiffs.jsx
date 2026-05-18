// Bucket C3c Phase 1 — Schedule update diffs list page. Auto-generated
// when a schedule_proposal flips to status='approved'; this page lets a
// PM browse the historical diff record across all approved updates.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { GitPullRequest, Plus, Minus, Pencil, AlertTriangle } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";

function StatPill({ icon: Icon, n, label, tone = "slate" }) {
  const tones = {
    slate: "bg-slate-100 text-slate-700",
    green: "bg-emerald-100 text-emerald-800",
    rose: "bg-rose-100 text-rose-800",
    amber: "bg-amber-100 text-amber-800",
    sky: "bg-sky-100 text-sky-800",
  };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${tones[tone]}`}>
      <Icon className="h-3 w-3" /> {n} <span className="opacity-70">{label}</span>
    </span>
  );
}

export default function ScheduleDiffs() {
  const { activeId } = useProjects();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!activeId) { setRows([]); return; }
    setLoading(true);
    api.get(`/projects/${activeId}/schedule-diffs`)
      .then((r) => setRows(r.data || []))
      .catch((e) => toast.error(e.response?.data?.detail || "Failed to load schedule diffs"))
      .finally(() => setLoading(false));
  }, [activeId]);

  if (!activeId) {
    return <div className="p-6 text-sm text-slate-500">Pick a project to see schedule diffs.</div>;
  }

  return (
    <div className="p-6 max-w-6xl mx-auto" data-testid="schedule-diffs-page">
      <header className="mb-4 flex items-center gap-2">
        <GitPullRequest className="h-5 w-5 text-slate-700" />
        <h1 className="text-xl font-semibold text-slate-800">Schedule updates</h1>
        <span className="text-xs text-slate-400 ml-2">
          One row per pair of approved schedule versions. Auto-generated on approval.
        </span>
      </header>

      {loading ? (
        <div className="text-sm text-slate-500">Loading…</div>
      ) : !rows.length ? (
        <div className="text-sm text-slate-500 border border-dashed border-slate-300 rounded p-8 text-center">
          No schedule diffs yet. Once you approve two or more schedule imports, diffs will appear here automatically.
        </div>
      ) : (
        <div className="space-y-2">
          {rows.map((d) => (
            <Link
              key={d.id}
              to={`/schedule-diffs/${d.id}`}
              data-testid={`schedule-diff-row-${d.id}`}
              className="block bg-white border border-slate-200 rounded p-3 hover:border-blue-400 hover:shadow-sm transition"
            >
              <div className="flex items-center justify-between mb-1">
                <div className="text-sm font-medium text-slate-800">
                  {new Date(d.computed_at).toLocaleString()}
                </div>
                <div className="text-xs text-slate-500">
                  {d.total_change_percentage?.toFixed?.(1) ?? "0.0"}% changed
                </div>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <StatPill icon={Plus} n={d.activities_added_count || 0} label="added" tone="green" />
                <StatPill icon={Minus} n={d.activities_removed_count || 0} label="removed" tone="rose" />
                <StatPill icon={Pencil} n={d.activities_modified_count || 0} label="modified" tone="amber" />
                <StatPill icon={AlertTriangle} n={d.cp_changes_count || 0} label="CP changes" tone="sky" />
              </div>
              <div className="text-xs text-slate-400 mt-1 font-mono">
                {(d.earlier_schedule_version_id || "").slice(0, 8)} → {(d.later_schedule_version_id || "").slice(0, 8)}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
