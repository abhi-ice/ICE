import { useEffect, useState } from "react";
import {
  FolderKanban, AlertTriangle, BadgeCheck, Wrench, ShieldAlert, Activity,
} from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
// CPM Part G (2026-05-15) -- dates rendered MM/DD/YYYY app-wide via
// the lib/dateUtil chokepoint.
import { formatDateDisplay } from "../lib/dateUtil";

const Tile = ({ label, value, icon: Icon, tone = "slate", testid }) => (
  <div data-testid={testid} className="bg-card border border-border rounded-lg p-4">
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <Icon className="h-4 w-4" /> {label}
    </div>
    <div className={`mt-2 text-3xl font-semibold tracking-tight tabular-nums text-${tone}-700`}>{value}</div>
  </div>
);

export default function Dashboard() {
  const [summary, setSummary] = useState(null);
  const { projects, activeId } = useProjects();
  const [recentTasks, setRecentTasks] = useState([]);
  const [recentRfis, setRecentRfis] = useState([]);

  useEffect(() => {
    api.get("/dashboard/summary").then(({ data }) => setSummary(data)).catch(() => {});
  }, []);
  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setRecentTasks(data.slice(0, 8)));
    api.get(`/projects/${activeId}/rfis`).then(({ data }) => setRecentRfis(data.slice(0, 5)));
  }, [activeId]);

  return (
    <div data-testid="dashboard-page" className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Tenant-wide health for all federal construction operations.
        </p>
      </div>

      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <Tile testid="kpi-active-projects" label="Active projects" value={summary.active_projects} icon={FolderKanban} />
          <Tile testid="kpi-open-rfis" label="Open RFIs" value={summary.open_rfis} icon={AlertTriangle} tone="amber" />
          <Tile testid="kpi-pending-approvals" label="Pending approvals" value={summary.pending_approvals} icon={BadgeCheck} tone="amber" />
          <Tile testid="kpi-open-work-items" label="Field issues" value={summary.open_work_items} icon={Wrench} tone="red" />
          <Tile testid="kpi-safety" label="Open safety events" value={summary.open_safety_events} icon={ShieldAlert} tone="red" />
          <Tile testid="kpi-schedule-health" label="Schedule health" value={`${summary.schedule_health_pct}%`} icon={Activity} tone="emerald" />
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-card border border-border rounded-lg">
          <div className="px-4 py-3 border-b border-border font-medium text-sm">
            Active project: {projects.find((p) => p.id === activeId)?.name || "—"}
          </div>
          <table className="w-full dense-table">
            <thead>
              <tr><th>Activity</th><th>Description</th><th>Status</th><th>Plan finish</th><th>%</th></tr>
            </thead>
            <tbody>
              {recentTasks.map((t) => (
                <tr key={t.id} data-testid={`dash-task-row-${t.activity_code}`}>
                  <td className="font-mono text-xs">{t.activity_code}</td>
                  <td className="truncate max-w-[280px]">{t.description}</td>
                  <td>
                    <span className={`pill ${t.status === "complete" ? "pill-green" : t.status === "in_progress" ? "pill-blue" : "pill-slate"}`}>
                      {t.status}
                    </span>
                  </td>
                  <td className="font-mono text-xs">{formatDateDisplay(t.planned_finish)}</td>
                  <td className="tabular-nums">{t.pct_complete}%</td>
                </tr>
              ))}
              {!recentTasks.length && <tr><td colSpan="5" className="text-center text-sm text-muted-foreground py-6">No tasks loaded.</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="bg-card border border-border rounded-lg">
          <div className="px-4 py-3 border-b border-border font-medium text-sm">Recent RFIs</div>
          <div className="p-3 space-y-2">
            {recentRfis.map((r) => (
              <div key={r.id} className="border border-border rounded p-2 text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-muted-foreground">{r.rfi_number}</span>
                  <span className={`pill ${r.status === "answered" ? "pill-green" : r.status === "in_review" ? "pill-amber" : "pill-blue"}`}>
                    {r.status}
                  </span>
                </div>
                <div className="mt-1 text-sm">{r.subject}</div>
              </div>
            ))}
            {!recentRfis.length && <div className="text-sm text-muted-foreground p-2">No RFIs.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
