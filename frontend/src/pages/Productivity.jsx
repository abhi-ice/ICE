// Productivity rates dashboard — master prompt §16
// Trade × location → hours-per-pct-point rolling average, surfaced for AI agent.
import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";

export default function Productivity() {
  const { activeId, projects } = useProjects();
  const [days, setDays] = useState(90);
  const [data, setData] = useState(null);
  const project = projects.find((p) => p.id === activeId);

  useEffect(() => {
    if (!activeId) return;
    api.get(`/productivity-rates?project_id=${activeId}&days=${days}`).then(({ data }) => setData(data));
  }, [activeId, days]);

  return (
    <div data-testid="productivity-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">Productivity rates</h1>
          </div>
          <p className="text-sm text-slate-500">{project?.name} · trade × location, rolling {days} days</p>
        </div>
        <div className="flex items-center gap-1">
          {[30, 60, 90, 180].map((d) => (
            <button
              key={d}
              data-testid={`prod-window-${d}`}
              onClick={() => setDays(d)}
              className={`h-8 px-3 rounded text-sm border ${days === d ? "bg-blue-600 text-white border-blue-600" : "border-slate-200"}`}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      <div className="surface overflow-hidden">
        <table className="dense-table">
          <thead>
            <tr>
              <th>Trade</th>
              <th>Location</th>
              <th>Hours / % point</th>
              <th>Sample size</th>
              <th>Total hours</th>
            </tr>
          </thead>
          <tbody>
            {(data?.rates || []).map((r, i) => (
              <tr key={i} data-testid={`prod-row-${i}`}>
                <td className="font-mono text-xs">{r.trade || "—"}</td>
                <td className="text-xs text-slate-500">{r.location_id?.slice(0, 8) || "—"}</td>
                <td className="font-semibold tabular-nums">{r.hours_per_pct_point}</td>
                <td className="tabular-nums">{r.sample_size}</td>
                <td className="tabular-nums">{r.total_hours}</td>
              </tr>
            ))}
            {!data?.rates?.length && (
              <tr><td colSpan="5" className="text-center py-12 text-sm text-slate-500">
                No productivity observations yet. Update daily report rows with hours_worked + pct_complete to populate.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="text-xs text-slate-500">
        Productivity observations are written automatically when daily-report rows update <code>pct_complete_end_of_day</code>
        with non-zero <code>hours_worked</code>. The AI agent uses these rates to predict recovery durations.
      </div>
    </div>
  );
}
