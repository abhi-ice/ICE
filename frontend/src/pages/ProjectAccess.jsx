// Bucket H Phase 3 (2026-05-08) -- Project Access page.
//
// Admin / PM management of project_members rows. Two-pane layout:
// left = project list, right = member list for the selected project.
// Adds/removes/role-changes flow through the project_members router
// which audit-logs PROJECT_MEMBER_ADDED / REMOVED / ROLE_CHANGED.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Users, Plus, Trash2, ShieldCheck, AlertCircle, ChevronRight, Mail } from "lucide-react";
import api from "../lib/api";
import { toast } from "../components/ui/sonner";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import FieldLabel from "../components/forms/FieldLabel";
// CPM Part G (2026-05-15) -- added_at displayed MM/DD/YYYY.
import { formatDateDisplay } from "../lib/dateUtil";
import { confirmAction } from "../lib/confirmAction";

// Bucket J Phase 3 (2026-05-11) -- specialty roles join the dropdown.
// submittal_tech + scheduling_engineer carry write power on their
// specialty surfaces; everywhere else they're read-only.
const ROLES = [
  { value: "pm",                  label: "PM (full access + member management)" },
  { value: "super",               label: "Superintendent" },
  { value: "foreman",             label: "Foreman" },
  { value: "viewer",              label: "Viewer (read-only)" },
  { value: "submittal_tech",      label: "Submittal Tech (write submittals only)" },
  { value: "scheduling_engineer", label: "Scheduling Engineer (write schedule only)" },
];

const ROLE_PILL = {
  pm:                  "bg-blue-100 text-blue-900",
  super:               "bg-emerald-100 text-emerald-900",
  foreman:             "bg-amber-100 text-amber-900",
  viewer:              "bg-slate-100 text-slate-700",
  submittal_tech:      "bg-violet-100 text-violet-900",
  scheduling_engineer: "bg-sky-100 text-sky-900",
};

export default function ProjectAccess() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [members, setMembers] = useState(null);   // {project_id, project_name, members:[]}
  const [eligible, setEligible] = useState([]);
  const [loadingProjects, setLoadingProjects] = useState(false);
  const [loadingMembers, setLoadingMembers] = useState(false);
  const [error, setError] = useState(null);
  const [addOpen, setAddOpen] = useState(false);

  const loadProjects = async () => {
    setLoadingProjects(true); setError(null);
    try {
      const r = await api.get(`/projects`);
      const list = Array.isArray(r.data) ? r.data : [];
      setProjects(list);
      if (list.length && !selectedId) setSelectedId(list[0].id);
    } catch (e) {
      setError(e.response?.data?.detail || "Couldn't load projects");
    } finally { setLoadingProjects(false); }
  };
  useEffect(() => { loadProjects(); }, []);

  const loadMembers = async (pid) => {
    if (!pid) { setMembers(null); return; }
    setLoadingMembers(true); setError(null);
    try {
      const [m, e] = await Promise.all([
        api.get(`/projects/${pid}/members`),
        api.get(`/projects/${pid}/eligible-members`).catch(() => ({ data: [] })),
      ]);
      setMembers(m.data);
      setEligible(Array.isArray(e.data) ? e.data : []);
    } catch (e) {
      setError(e.response?.data?.detail || "Couldn't load members");
      setMembers(null);
    } finally { setLoadingMembers(false); }
  };
  useEffect(() => { loadMembers(selectedId); /* eslint-disable-next-line */ }, [selectedId]);

  const onAddMember = async (userId, role) => {
    try {
      await api.post(`/projects/${selectedId}/members`, {
        project_id: selectedId, user_id: userId, role_on_project: role,
      });
      toast.success("Member added");
      setAddOpen(false);
      loadMembers(selectedId);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Add failed");
    }
  };

  const onRemove = async (member) => {
    // pt.37: themed modal (was window.confirm).
    const ok = await confirmAction({
      title: "Remove member?",
      message: `${member.user_full_name || member.user_email} will lose access to this project. They keep their organization membership.`,
      confirmText: "Remove member",
    });
    if (!ok) return;
    try {
      await api.delete(`/projects/${selectedId}/members/${member.user_id}`);
      toast.success("Member removed");
      loadMembers(selectedId);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Remove failed");
    }
  };

  const onChangeRole = async (member, newRole) => {
    if (newRole === member.role_on_project) return;
    try {
      await api.patch(`/projects/${selectedId}/members/${member.user_id}`, {
        role_on_project: newRole,
      });
      toast.success("Role updated");
      loadMembers(selectedId);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Role change failed");
    }
  };

  return (
    /* pt.36: dropped p-4 sm:p-6 -- AppLayout main already pads p-6.
       Earlier pt.35 sweep only caught `p-6`; the `p-4 sm:p-6` and
       similar variants escaped the grep. */
    <div data-testid="project-access-page">
      <header className="mb-4 flex items-center gap-2 max-md:flex-wrap">
        <ShieldCheck className="h-5 w-5 text-slate-700" />
        <h1 className="text-xl font-semibold text-slate-800">Project Access</h1>
        <span className="ml-2 text-xs text-slate-500 max-md:hidden">
          Manage who can see and act on each project. PMs and Admins only.
        </span>
        <span className="flex-1" />
        {/* pt.69: "Add member" only grants EXISTING org users access to a
            project. To bring a NEW person into the org, you invite them on the
            Team page — this shortcut routes there. */}
        <button data-testid="pa-send-invite" onClick={() => navigate("/team")}
          title="Invite a new teammate to your organization (Team page)"
          className="sbtn sbtn-sm sbtn-secondary shrink-0">
          <Mail className="h-3.5 w-3.5" /> Send invite
        </button>
      </header>

      {error && (
        <div data-testid="pa-error" className="mb-3 p-3 bg-rose-50 border border-rose-200 rounded text-sm flex items-center gap-2">
          <AlertCircle className="h-4 w-4 text-rose-600 shrink-0" />
          <div className="flex-1 text-rose-900">{error}</div>
          <button onClick={() => { loadProjects(); if (selectedId) loadMembers(selectedId); }}
            className="h-7 px-3 rounded bg-rose-600 text-white text-xs hover:bg-rose-700">
            Retry
          </button>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Projects list (left) */}
        <div className="md:col-span-1 bg-white border border-slate-200 rounded">
          <div className="p-2 border-b border-slate-100 font-medium text-sm flex items-center gap-1">
            <Users className="h-4 w-4 text-slate-500" /> Projects
          </div>
          {loadingProjects ? (
            <div className="p-3 text-xs text-slate-500">Loading…</div>
          ) : !projects.length ? (
            <div data-testid="pa-empty-projects" className="p-3 text-xs text-slate-500">
              No projects in this tenant yet.
            </div>
          ) : (
            <ul className="text-sm">
              {projects.map((p) => (
                <li key={p.id}>
                  <button data-testid={`pa-project-${p.id}`}
                    onClick={() => setSelectedId(p.id)}
                    className={`w-full text-left px-3 py-2 max-md:min-h-[44px] border-b border-slate-100 flex items-center justify-between hover:bg-slate-50 ${
                      selectedId === p.id ? "bg-blue-50 border-l-2 border-l-blue-600" : ""
                    }`}>
                    <span className="truncate">{p.name}</span>
                    <ChevronRight className="h-4 w-4 text-slate-400 shrink-0" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Members of selected project (right) */}
        <div className="md:col-span-2 bg-white border border-slate-200 rounded">
          {!selectedId ? (
            <div className="p-8 text-center text-sm text-slate-500">
              Select a project to manage its members.
            </div>
          ) : loadingMembers ? (
            <div className="p-3 text-xs text-slate-500">Loading members…</div>
          ) : !members ? null : (
            <>
              <div className="p-3 border-b border-slate-100 flex items-center justify-between">
                <div className="font-medium text-sm">{members.project_name}</div>
                <Button data-testid="pa-add-trigger" onClick={() => setAddOpen(true)}
                  className="bg-blue-600 hover:bg-blue-700 h-8 px-3 text-xs gap-1">
                  Add member
                </Button>
              </div>
              {!members.members.length ? (
                <div data-testid="pa-empty-members" className="p-6 text-center text-sm text-slate-500">
                  No members yet. Click <strong>Add member</strong> to grant access.
                </div>
              ) : (
                <div className="max-md:overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-slate-50 border-b border-slate-200 text-xs">
                    <tr>
                      <th className="p-2 text-left">User</th>
                      <th className="p-2 text-left">Role on project</th>
                      <th className="p-2 text-left">Added</th>
                      <th className="p-2"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.members.map((m) => (
                      <tr key={m.id} data-testid={`pa-member-${m.user_id}`}
                        className="border-b border-slate-100">
                        <td className="p-2">
                          <div className="font-medium text-slate-800">{m.user_full_name || m.user_email}</div>
                          <div className="text-xs text-slate-500">{m.user_email}</div>
                        </td>
                        <td className="p-2">
                          <select data-testid={`pa-role-${m.user_id}`}
                            value={m.role_on_project}
                            onChange={(e) => onChangeRole(m, e.target.value)}
                            className={`h-7 max-md:min-h-[44px] px-2 rounded text-xs border ${ROLE_PILL[m.role_on_project]}`}>
                            {ROLES.map((r) => <option key={r.value} value={r.value}>{r.value}</option>)}
                          </select>
                        </td>
                        <td className="p-2 font-mono text-xs text-slate-500">
                          {formatDateDisplay(m.added_at)}
                        </td>
                        <td className="p-2 text-right">
                          <button data-testid={`pa-remove-${m.user_id}`}
                            onClick={() => onRemove(m)}
                            className="h-7 w-7 max-md:min-h-[44px] max-md:min-w-[44px] grid place-items-center rounded hover:bg-rose-50 text-rose-500"
                            title="Remove member">
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Add member dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>Add member</DialogTitle></DialogHeader>
          <AddMemberForm eligible={eligible} onAdd={onAddMember}
            onCancel={() => setAddOpen(false)} />
        </DialogContent>
      </Dialog>
    </div>
  );
}

function AddMemberForm({ eligible, onAdd, onCancel }) {
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("viewer");
  return (
    <div className="space-y-3 text-sm">
      <div>
        <FieldLabel required className="field-label">Tenant user</FieldLabel>
        {!eligible.length ? (
          <div className="text-xs text-slate-500 italic">
            No eligible tenant users. All active users are already members.
          </div>
        ) : (
          <select data-testid="pa-add-user" value={userId}
            onChange={(e) => setUserId(e.target.value)}
            className="w-full h-9 px-2 rounded border border-slate-300 bg-white text-sm">
            <option value="">Select user…</option>
            {eligible.map((u) => (
              <option key={u.id} value={u.id}>
                {u.full_name || u.email} ({u.role})
              </option>
            ))}
          </select>
        )}
      </div>
      <div>
        <FieldLabel required className="field-label">Role on project</FieldLabel>
        <select data-testid="pa-add-role" value={role}
          onChange={(e) => setRole(e.target.value)}
          className="w-full h-9 px-2 rounded border border-slate-300 bg-white text-sm">
          {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
      </div>
      <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
        <button onClick={onCancel}
          className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
          Cancel
        </button>
        <Button data-testid="pa-add-submit" onClick={() => onAdd(userId, role)}
          disabled={!userId} className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50">
          Add
        </Button>
      </div>
    </div>
  );
}
