import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Plus, Pencil, Trash2 } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "../components/ui/dialog";
import { Button } from "../components/ui/button";
import { toast } from "../components/ui/sonner";

const EMPTY = {
  activity_code: "", description: "", wbs: "", trade_code: "",
  planned_start: "", planned_finish: "", duration_days: "",
  predecessors: "", is_milestone: false,
};

export default function Tasks() {
  const { activeId, projects } = useProjects();
  const [tasks, setTasks] = useState([]);
  const [crews, setCrews] = useState([]);
  const [locs, setLocs] = useState([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const project = projects.find((p) => p.id === activeId);

  const load = () => activeId && api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data));
  useEffect(() => {
    if (!activeId) return;
    load();
    api.get("/crews").then(({ data }) => setCrews(data));
    api.get(`/projects/${activeId}/locations`).then(({ data }) => setLocs(data));
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [activeId]);

  const recompute = async () => {
    const { data } = await api.post(`/projects/${activeId}/critical-path/recompute`);
    toast.success(`CPM updated. ${data.critical_count} on critical path. Finish: ${data.project_finish}`);
    load();
  };

  const openNew = () => { setEditing(null); setForm(EMPTY); setOpen(true); };
  const openEdit = (t) => {
    setEditing(t);
    setForm({
      activity_code: t.activity_code,
      description: t.description,
      wbs: t.wbs || "",
      trade_code: t.trade_code || "",
      planned_start: t.planned_start || "",
      planned_finish: t.planned_finish || "",
      duration_days: t.duration_days ?? "",
      predecessors: (t.predecessors || []).join(", "),
      is_milestone: !!t.is_milestone,
      crew_id: t.crew_id || "",
      location_id: t.location_id || "",
    });
    setOpen(true);
  };

  const submit = async () => {
    const payload = {
      activity_code: form.activity_code.trim(),
      description: form.description.trim(),
      wbs: form.wbs || null,
      trade_code: form.trade_code || null,
      planned_start: form.planned_start || null,
      planned_finish: form.planned_finish || null,
      duration_days: form.duration_days === "" ? null : Number(form.duration_days),
      predecessors: form.predecessors ? form.predecessors.split(",").map((s) => s.trim()).filter(Boolean) : [],
      is_milestone: !!form.is_milestone,
      crew_id: form.crew_id || null,
      location_id: form.location_id || null,
    };
    Object.keys(payload).forEach((k) => { if (payload[k] === null) delete payload[k]; });
    try {
      if (editing) {
        // task update can't change activity_code; predecessors are now patchable
        const { activity_code, ...patchable } = payload;
        await api.patch(`/tasks/${editing.id}`, patchable);
        toast.success("Activity updated — CPM recomputing in background.");
      } else {
        await api.post("/tasks", { project_id: activeId, ...payload });
        toast.success("Activity created");
      }
      setOpen(false);
      load();
    } catch (e) {
      toast.error(JSON.stringify(e.response?.data?.detail || e.message));
    }
  };

  const remove = async (t) => {
    if (!window.confirm(`Delete activity ${t.activity_code}?`)) return;
    await api.delete(`/tasks/${t.id}`);
    toast.success("Deleted");
    load();
  };

  return (
    <div data-testid="tasks-page" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Schedule activities</h1>
          <p className="text-sm text-slate-500">{project?.name}</p>
        </div>
        <div className="flex gap-2">
          <button data-testid="cpm-recompute" onClick={recompute} className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
            Recompute CPM
          </button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button data-testid="new-activity" onClick={openNew} className="bg-blue-600 hover:bg-blue-700">
                <Plus className="h-4 w-4 mr-1" /> New activity
              </Button>
            </DialogTrigger>
            <DialogContent className="max-w-xl">
              <DialogHeader><DialogTitle>{editing ? `Edit ${editing.activity_code}` : "New activity"}</DialogTitle></DialogHeader>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="field-label">Activity code *</label>
                  <input data-testid="ac-code" value={form.activity_code} disabled={!!editing} onChange={(e) => setForm({ ...form, activity_code: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200 disabled:bg-slate-50" />
                </div>
                <div>
                  <label className="field-label">WBS</label>
                  <input value={form.wbs} onChange={(e) => setForm({ ...form, wbs: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div className="col-span-2">
                  <label className="field-label">Description *</label>
                  <input data-testid="ac-desc" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Planned start</label>
                  <input type="date" value={form.planned_start} onChange={(e) => setForm({ ...form, planned_start: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Planned finish</label>
                  <input type="date" value={form.planned_finish} onChange={(e) => setForm({ ...form, planned_finish: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Duration (days)</label>
                  <input type="number" value={form.duration_days} onChange={(e) => setForm({ ...form, duration_days: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Trade</label>
                  <input value={form.trade_code} onChange={(e) => setForm({ ...form, trade_code: e.target.value })} className="w-full h-9 px-3 rounded border border-slate-200" />
                </div>
                <div>
                  <label className="field-label">Crew</label>
                  <select value={form.crew_id || ""} onChange={(e) => setForm({ ...form, crew_id: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                    <option value="">—</option>
                    {crews.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.trade})</option>)}
                  </select>
                </div>
                <div>
                  <label className="field-label">Location</label>
                  <select value={form.location_id || ""} onChange={(e) => setForm({ ...form, location_id: e.target.value })} className="w-full h-9 px-2 rounded border border-slate-200 text-sm">
                    <option value="">—</option>
                    {locs.map((l) => <option key={l.id} value={l.id}>{l.code} {l.name}</option>)}
                  </select>
                </div>
                <div className="col-span-2">
                  <label className="field-label">Predecessors (comma-separated activity_codes)</label>
                  <input data-testid="task-predecessors" value={form.predecessors} onChange={(e) => setForm({ ...form, predecessors: e.target.value })} placeholder="e.g. 1.2, 1.5" className="w-full h-9 px-3 rounded border border-slate-200 font-mono text-sm" />
                  <div className="text-[11px] text-slate-400 mt-1">Comma-separated activity codes from this project. CPM is recomputed automatically.</div>
                </div>
                <label className="col-span-2 flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={!!form.is_milestone} onChange={(e) => setForm({ ...form, is_milestone: e.target.checked })} />
                  Milestone
                </label>
              </div>
              <Button data-testid="ac-submit" onClick={submit} disabled={!form.activity_code || !form.description} className="bg-blue-600 hover:bg-blue-700">
                {editing ? "Save changes" : "Create activity"}
              </Button>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <div className="surface overflow-hidden">
        <table className="dense-table">
          <thead>
            <tr>
              <th className="w-10">#</th>
              <th>Description</th>
              <th>Resource</th>
              <th>Predecessors</th>
              <th>Trade</th>
              <th>Plan start</th>
              <th>Plan finish</th>
              <th>Dur (d)</th>
              <th>%</th>
              <th>Status</th>
              <th title="Critical Path — these activities have zero float, any slip pushes the project finish.">CP</th>
              <th title="Float (a.k.a. slack): how many calendar days an activity can slip without delaying the project finish. High floats mean weak or missing predecessor links in the source file.">Float (d)</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t, idx) => (
              <tr key={t.id} data-testid={`task-row-${t.activity_code}`} className={t.is_critical_path ? "bg-red-50/40" : ""}>
                <td className="tabular-nums text-xs text-slate-500">{idx + 1}</td>
                <td className="truncate max-w-[280px]">
                  <div className="font-medium text-sm">{t.description}</div>
                  <div className="text-[10px] text-slate-400 font-mono">{t.activity_code}{t.is_milestone ? " · milestone" : ""}</div>
                </td>
                <td className="text-xs">
                  {t.resource_names && t.resource_names.length
                    ? t.resource_names.join(", ")
                    : <span className="text-slate-400">—</span>}
                </td>
                <td className="text-xs font-mono">
                  {t.predecessors && t.predecessors.length
                    ? t.predecessors.join(", ")
                    : <span className="text-slate-400">—</span>}
                </td>
                <td className="text-xs">{t.trade_code || "—"}</td>
                <td className="font-mono text-xs">{t.planned_start || "—"}</td>
                <td className="font-mono text-xs">{t.planned_finish || "—"}</td>
                <td className="tabular-nums text-xs">{t.duration_days}</td>
                <td className="tabular-nums">{t.pct_complete}%</td>
                <td>
                  <span className={`pill ${t.status === "complete" ? "pill-green" : t.status === "in_progress" ? "pill-blue" : "pill-slate"}`}>{t.status}</span>
                </td>
                <td>{t.is_critical_path ? <span className="pill pill-red" title="Critical Path">CP</span> : null}</td>
                <td className="tabular-nums text-xs" title="Days of slack before this task delays the project finish">
                  {t.total_float_days ?? "—"}
                </td>
                <td className="text-right">
                  <button data-testid={`edit-task-${t.activity_code}`} onClick={() => openEdit(t)} title="Edit" className="h-7 w-7 grid place-items-center rounded hover:bg-slate-100">
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button data-testid={`delete-task-${t.activity_code}`} onClick={() => remove(t)} title="Delete" className="h-7 w-7 grid place-items-center rounded hover:bg-red-50 text-red-600">
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            ))}
            {!tasks.length && <tr><td colSpan="13" className="text-center py-8 text-sm text-slate-500">No activities. Use the &ldquo;New activity&rdquo; button or import a schedule.</td></tr>}
          </tbody>
        </table>
      </div>

      {/* CP / Float legend */}
      {tasks.length > 0 && (
        <div className="text-xs text-slate-500 mt-2 flex flex-wrap gap-4">
          <span><span className="pill pill-red">CP</span> Critical Path — zero float, any slip delays the project finish.</span>
          <span><strong>Float</strong> (slack) — calendar days an activity can slip without delaying the project. High floats (e.g. 40+) usually mean the source schedule has no predecessor links between activities.</span>
        </div>
      )}
    </div>
  );
}
