// Submittals page — list with auto-generated badge + manual create dialog +
// schedule activity linkage + auto-link button.
// Auto-generated rows come from spec_ingest._autogenerate_submittals().
import { useEffect, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Plus, Wand2, Link2, Sparkles } from "lucide-react";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";

const STATUS_OPTIONS = [
  "not_yet_submitted", "submitted", "in_review", "approved",
  "approved_as_noted", "rejected", "revise_resubmit", "closed",
];

const TYPE_OPTIONS = [
  "product_data", "shop_drawing", "sample", "calculation",
  "test_report", "warranty", "om_manual", "mock_up",
];

export default function Submittals() {
  const { activeId, projects } = useProjects();
  const [list, setList] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [open, setOpen] = useState(false);
  const [linkOpen, setLinkOpen] = useState(null); // submittal object
  const [linkActivity, setLinkActivity] = useState("");
  const [form, setForm] = useState({ title: "", spec_section_code: "", type: "product_data", sub_responsible: "" });
  const project = projects.find((p) => p.id === activeId);

  const load = () => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/submittals`).then(({ data }) => setList(data));
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data.filter((t) => t.status !== "deleted")));
  };
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  const create = async () => {
    try {
      await api.post("/submittals", { project_id: activeId, ...form });
      toast.success("Submittal created");
      setOpen(false);
      setForm({ title: "", spec_section_code: "", type: "product_data", sub_responsible: "" });
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail?.[0]?.msg || "Failed");
    }
  };

  const updateStatus = async (id, status) => {
    await api.patch(`/submittals/${id}`, { status });
    load();
  };

  const saveLink = async () => {
    try {
      await api.patch(`/submittals/${linkOpen.id}/links`, { schedule_activity_id: linkActivity || null });
      toast.success("Link saved");
      setLinkOpen(null); setLinkActivity("");
      load();
    } catch (e) { toast.error("Failed"); }
  };

  const autoLinkAll = async () => {
    try {
      const { data } = await api.post(`/projects/${activeId}/submittals/auto-link-activities`);
      toast.success(`Linked ${data.linked} of ${data.total_submittals_examined}`);
      load();
    } catch (e) { toast.error("Auto-link failed"); }
  };

  const auto = list.filter((s) => s.auto_generated).length;
  const linked = list.filter((s) => s.schedule_activity_id).length;
  const taskById = Object.fromEntries(tasks.map((t) => [t.id, t]));

  return (
    <div data-testid="submittals-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Submittals</h1>
          <p className="text-sm text-slate-500">{project?.name} · {list.length} total · {auto} auto-generated · {linked} linked to activity</p>
        </div>
        <div className="flex items-center gap-2">
          <Button data-testid="auto-link-all" onClick={autoLinkAll} variant="outline" className="gap-1">
            <Sparkles className="h-4 w-4" /> Auto-link to activities
          </Button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button data-testid="new-submittal" className="bg-blue-600 hover:bg-blue-700"><Plus className="h-4 w-4 mr-1" /> New submittal</Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader><DialogTitle>New submittal</DialogTitle></DialogHeader>
              <div className="space-y-3">
                <div>
                  <label className="field-label">Title *</label>
                  <input data-testid="sub-title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="field-label">Spec section code</label>
                    <input value={form.spec_section_code} onChange={(e) => setForm({ ...form, spec_section_code: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200 font-mono text-sm" placeholder="03 30 00" />
                  </div>
                  <div>
                    <label className="field-label">Type</label>
                    <select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                      {TYPE_OPTIONS.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                    </select>
                  </div>
                </div>
                <div>
                  <label className="field-label">Sub responsible</label>
                  <input value={form.sub_responsible} onChange={(e) => setForm({ ...form, sub_responsible: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <Button data-testid="sub-create" onClick={create} disabled={!form.title} className="bg-blue-600 hover:bg-blue-700">Create</Button>
              </div>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <div className="surface overflow-hidden">
        <table className="dense-table">
          <thead><tr><th>Number</th><th>Spec</th><th>Title</th><th>Type</th><th>Sub</th><th>Activity</th><th>Status</th></tr></thead>
          <tbody>
            {list.map((s) => {
              const linkedTask = s.schedule_activity_id ? taskById[s.schedule_activity_id] : null;
              return (
                <tr key={s.id} data-testid={`sub-row-${s.submittal_number}`}>
                  <td className="font-mono text-xs">
                    {s.gating_entity_id
                      ? <RouterLink to={`/gating-entities/${s.gating_entity_id}`} className="text-blue-700 hover:underline">{s.submittal_number}</RouterLink>
                      : s.submittal_number}
                  </td>
                  <td className="font-mono text-xs">{s.spec_section_code || "—"}</td>
                  <td className="truncate max-w-[320px]">
                    {s.title}
                    {s.auto_generated && (
                      <span className="ml-2 inline-flex items-center gap-1 pill pill-blue text-[10px]">
                        <Wand2 className="h-2.5 w-2.5" /> auto
                      </span>
                    )}
                  </td>
                  <td className="text-xs">{s.type.replace(/_/g, " ")}</td>
                  <td className="text-xs">{s.sub_responsible || "—"}</td>
                  <td className="text-xs">
                    <button
                      data-testid={`sub-link-${s.submittal_number}`}
                      onClick={() => { setLinkOpen(s); setLinkActivity(s.schedule_activity_id || ""); }}
                      className="inline-flex items-center gap-1 text-blue-700 hover:underline"
                    >
                      <Link2 className="h-3 w-3" />
                      {linkedTask ? `[${linkedTask.activity_code}]` : "Link…"}
                    </button>
                  </td>
                  <td>
                    <select
                      data-testid={`sub-status-${s.submittal_number}`}
                      value={s.status}
                      onChange={(e) => updateStatus(s.id, e.target.value)}
                      className="h-7 px-2 rounded border border-slate-200 text-xs bg-white"
                    >
                      {STATUS_OPTIONS.map((opt) => <option key={opt} value={opt}>{opt.replace(/_/g, " ")}</option>)}
                    </select>
                  </td>
                </tr>
              );
            })}
            {!list.length && <tr><td colSpan="7" className="text-center py-6 text-sm text-slate-500">No submittals yet. Upload a spec PDF — submittals will auto-generate from any "1.4 SUBMITTALS" paragraph.</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Link-to-activity dialog */}
      <Dialog open={!!linkOpen} onOpenChange={(o) => !o && setLinkOpen(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Link submittal to activity</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div className="text-sm text-slate-600">{linkOpen?.submittal_number} — {linkOpen?.title}</div>
            <select data-testid="sub-link-select" value={linkActivity} onChange={(e) => setLinkActivity(e.target.value)} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
              <option value="">— unlink —</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>[{t.activity_code}] {t.description.slice(0, 60)}</option>
              ))}
            </select>
            <Button data-testid="sub-link-save" onClick={saveLink} className="bg-blue-600 hover:bg-blue-700">Save</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
