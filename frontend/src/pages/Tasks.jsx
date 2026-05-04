import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";

export default function Tasks() {
  const { activeId, projects } = useProjects();
  const [tasks, setTasks] = useState([]);
  const project = projects.find((p) => p.id === activeId);

  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data));
  }, [activeId]);

  const recompute = async () => {
    const { data } = await api.post(`/projects/${activeId}/critical-path/recompute`);
    alert(`CPM: ${data.updated} updated, ${data.critical_count} on critical path. Project finish: ${data.project_finish}`);
    api.get(`/projects/${activeId}/tasks`).then(({ data: t }) => setTasks(t));
  };

  return (
    <div data-testid="tasks-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Schedule activities</h1>
          <p className="text-sm text-muted-foreground">{project?.name}</p>
        </div>
        <button data-testid="cpm-recompute" onClick={recompute} className="px-3 h-9 rounded bg-primary text-primary-foreground text-sm">
          Recompute critical path
        </button>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full dense-table">
          <thead>
            <tr>
              <th>Code</th><th>WBS</th><th>Description</th><th>Trade</th>
              <th>Plan start</th><th>Plan finish</th><th>Dur</th>
              <th>%</th><th>Status</th><th>CP</th><th>Float</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id} data-testid={`task-row-${t.activity_code}`} className={t.is_critical_path ? "bg-red-50/50" : ""}>
                <td className="font-mono text-xs">{t.activity_code}</td>
                <td className="font-mono text-xs text-muted-foreground">{t.wbs}</td>
                <td className="truncate max-w-[280px]">{t.description}</td>
                <td>{t.trade_code || "—"}</td>
                <td className="font-mono text-xs">{t.planned_start || "—"}</td>
                <td className="font-mono text-xs">{t.planned_finish || "—"}</td>
                <td className="tabular-nums">{t.duration_days}</td>
                <td className="tabular-nums">{t.pct_complete}%</td>
                <td>
                  <span className={`pill ${t.status === "complete" ? "pill-green" : t.status === "in_progress" ? "pill-blue" : "pill-slate"}`}>{t.status}</span>
                </td>
                <td>{t.is_critical_path ? <span className="pill pill-red">CP</span> : null}</td>
                <td className="tabular-nums text-xs">{t.total_float_days ?? "—"}</td>
              </tr>
            ))}
            {!tasks.length && <tr><td colSpan="11" className="text-center py-8 text-sm text-muted-foreground">No activities. Import a schedule from Imports → Schedule.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
