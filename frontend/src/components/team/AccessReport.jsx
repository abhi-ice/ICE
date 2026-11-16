// Per-project access (2026-11-16) -- the "before you switch it on" report.
//
// Shown on the Project Access page to admins with access to all projects
// (the server answers 403 to everyone else and the section stays hidden).
// Lists who would lose access if the Settings switch "Per-project access"
// were turned on now, and lets the admin assign them with one click.
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Eye, ShieldCheck, UserPlus } from "lucide-react";
import api from "../../lib/api";
import { toast } from "../ui/sonner";

function errorText(ex, fallback) {
  const detail = ex?.response?.data?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

function when(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

export default function AccessReport({ onChanged, onSelectProject }) {
  const [report, setReport] = useState(null);
  const [hidden, setHidden] = useState(false);
  const [busyKey, setBusyKey] = useState(null);
  const [picks, setPicks] = useState({});   // user_id -> project id picked in the dropdown

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/admin/project-access/report");
      setReport(data);
    } catch (e) {
      // 403 = not an all-projects admin: nothing to show.
      setHidden(true);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const assign = async (userId, projectId, roleOnProject) => {
    if (!userId || !projectId) return;
    const key = `${userId}|${projectId}`;
    setBusyKey(key);
    try {
      await api.post(`/projects/${projectId}/members`, {
        project_id: projectId, user_id: userId, role_on_project: roleOnProject || "viewer",
      });
      toast.success("Assigned");
      await load();
      onChanged?.(projectId);
    } catch (e) {
      toast.error(errorText(e, "Could not assign. Try again."));
    } finally { setBusyKey(null); }
  };

  if (hidden || !report) return null;

  const noProjects = report.users_without_projects || [];
  const limitedAdmins = report.limited_admins_without_projects || [];
  const emptyProjects = report.projects_without_members || [];
  const drRows = report.daily_reports_by_unassigned || [];
  const watchAll = (report.watch || []).filter((w) => !w.now_assigned);
  // Rows that are only the phone's background download (nobody opened the
  // project) are summarised in one line, not listed as problems.
  const watch = watchAll.filter((w) => w.write || w.count > 0);
  const warmupOnly = watchAll.length - watch.length;
  const problems = noProjects.length + limitedAdmins.length + drRows.length + watch.length;
  const projects = report.projects || [];

  const AssignButton = ({ row, projectId, label }) => (
    <button type="button" data-testid={`par-assign-${row.user_id}-${projectId}`}
      disabled={busyKey === `${row.user_id}|${projectId}`}
      onClick={() => assign(row.user_id, projectId, row.suggested_role_on_project)}
      className="sbtn sbtn-sm sbtn-secondary shrink-0 max-md:min-h-[44px] disabled:opacity-50">
      <UserPlus className="h-3.5 w-3.5" /> {label || "Assign"}
    </button>
  );

  const who = (r) => r.full_name || r.email || r.user_id;

  return (
    <details data-testid="pa-access-report" open={problems > 0}
      className="mb-4 bg-white border border-slate-200 rounded">
      <summary className="p-3 cursor-pointer text-sm flex flex-wrap items-center gap-2 max-md:min-h-[44px]">
        <ShieldCheck className="h-4 w-4 text-slate-600" />
        <span className="font-medium">Per-project access check</span>
        {report.enforced ? (
          <span className="pill pill-green" data-testid="par-status">Switched on</span>
        ) : (
          <span className="pill pill-blue" data-testid="par-status">Watch only (switch is off)</span>
        )}
        {problems ? (
          <span className="text-xs text-amber-700 inline-flex items-center gap-1">
            <AlertTriangle className="h-3.5 w-3.5" /> {problems} item{problems === 1 ? "" : "s"} to review
          </span>
        ) : (
          <span className="text-xs text-emerald-700 inline-flex items-center gap-1">
            <CheckCircle2 className="h-3.5 w-3.5" /> Everyone is assigned
          </span>
        )}
      </summary>
      <div className="px-3 pb-3 space-y-4 text-sm">
        <p className="text-xs text-slate-500">
          When the &quot;Per-project access&quot; switch in Settings is on, people only see and change the projects they are assigned to.
          Admins with access to all projects see everything. Fix the items below before switching it on.
        </p>

        {!!noProjects.length && (
          <section data-testid="par-no-projects">
            <div className="font-medium mb-1">People with no projects ({noProjects.length})</div>
            <ul className="divide-y divide-slate-100 border border-slate-100 rounded">
              {noProjects.map((r) => (
                <li key={r.user_id} className="p-2 flex flex-wrap items-center gap-2">
                  <span className="grow min-w-[160px]">{who(r)} <span className="text-xs text-slate-500">({r.role})</span></span>
                  <select value={picks[r.user_id] || ""} data-testid={`par-pick-${r.user_id}`}
                    onChange={(e) => setPicks({ ...picks, [r.user_id]: e.target.value })}
                    className="h-8 max-md:min-h-[44px] px-2 rounded border border-slate-300 bg-white text-sm max-w-full">
                    <option value="">Pick a project…</option>
                    {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                  {picks[r.user_id] && <AssignButton row={r} projectId={picks[r.user_id]} />}
                </li>
              ))}
            </ul>
          </section>
        )}

        {!!limitedAdmins.length && (
          <section data-testid="par-limited-admins">
            <div className="font-medium mb-1">Admins limited to specific projects, with none assigned ({limitedAdmins.length})</div>
            <ul className="text-xs text-slate-600 list-disc pl-5">
              {limitedAdmins.map((r) => <li key={r.user_id}>{who(r)} (change on the Team page)</li>)}
            </ul>
          </section>
        )}

        {!!drRows.length && (
          <section data-testid="par-dr-unassigned">
            <div className="font-medium mb-1">Daily reports written in the last 30 days on projects the writer is not assigned to</div>
            <ul className="divide-y divide-slate-100 border border-slate-100 rounded">
              {drRows.map((r) => (
                <li key={`${r.user_id}|${r.project_id}`} className="p-2 flex flex-wrap items-center gap-2">
                  <span className="grow min-w-[160px]">
                    {who(r)} on <strong>{r.project_name || r.project_id}</strong>
                    <span className="text-xs text-slate-500"> ({r.report_count} report{r.report_count === 1 ? "" : "s"}, last {r.last_report_date})</span>
                  </span>
                  <AssignButton row={r} projectId={r.project_id} label="Assign to this project" />
                </li>
              ))}
            </ul>
          </section>
        )}

        {!!watch.length && (
          <section data-testid="par-watch">
            <div className="font-medium mb-1 flex items-center gap-1">
              <Eye className="h-4 w-4 text-slate-500" />
              {report.enforced ? "Refused because the person is not assigned" : "Would be refused once the switch is on"}
            </div>
            <ul className="divide-y divide-slate-100 border border-slate-100 rounded">
              {watch.map((w) => (
                <li key={`${w.user_id}|${w.project_id}|${w.route}|${w.method}`} className="p-2 flex flex-wrap items-center gap-2">
                  <span className="grow min-w-[160px]">
                    {who(w)} on <strong>{w.project_name || w.project_id}</strong>
                    <span className="block text-xs text-slate-500 break-all">
                      {w.write ? "Change" : "View"} · {w.method} {w.route} · {w.count ? `${w.count}×` : "background download only"}{w.count && w.prefetch_count ? ` (+${w.prefetch_count} background)` : ""} · last {when(w.last_at)}
                    </span>
                  </span>
                  {w.still_member === false
                    ? <span className="text-xs text-slate-500 shrink-0">No longer in the company</span>
                    : <AssignButton row={w} projectId={w.project_id} label="Assign to this project" />}
                </li>
              ))}
            </ul>
          </section>
        )}
        {warmupOnly > 0 && (
          <p className="text-xs text-slate-500" data-testid="par-warmup-only">
            Plus {warmupOnly} project{warmupOnly === 1 ? "" : "s"} only downloaded in the background by phones for offline use (nobody opened them). These are not counted as problems.
          </p>
        )}

        {!!emptyProjects.length && (
          <section data-testid="par-empty-projects">
            <div className="font-medium mb-1">Projects nobody is assigned to ({emptyProjects.length})</div>
            <div className="flex flex-wrap gap-2">
              {emptyProjects.map((p) => (
                <button key={p.project_id} type="button" onClick={() => onSelectProject?.(p.project_id)}
                  className="pill pill-slate hover:bg-slate-200 max-md:min-h-[44px] max-w-full whitespace-normal break-words text-left">
                  {p.project_name || p.project_id}
                </button>
              ))}
            </div>
            <div className="text-xs text-slate-500 mt-1">Only admins with access to all projects can open these until someone is assigned.</div>
          </section>
        )}
      </div>
    </details>
  );
}
