// Shared delay form — used by the daily report inline panel AND the sidebar
// Delays page. Multi-task picker with per-task projected impact (decimal days),
// inline float-vs-impact indicator, status, and conditional resolution fields.
//
// Props:
//   projectId    string — required, used to load project tasks for the picker
//   reportDate   string YYYY-MM-DD — used as default `date` when creating
//   dailyReportId string|null — set when invoked inside a daily report panel
//   initial      Delay | null — when present, edit mode (we PATCH); else create
//   onSaved      function(delay) — called after a successful POST/PATCH
//   onCancel     function — called when the user dismisses the form
import { useEffect, useMemo, useState } from "react";
import { Plus, X, AlertTriangle, ShieldCheck } from "lucide-react";
import api from "../../lib/api";
import { localTodayISO } from "../../lib/dateUtil";
import { toast } from "../ui/sonner";

const SEVERITY_OPTS = ["minor", "moderate", "major", "critical"];
const REASON_OPTS = ["weather", "rfi_pending", "submittal_pending", "design_change",
                     "site_condition", "manpower", "owner_caused", "sub_caused",
                     "concealed_condition", "other"];
const STATUS_OPTS = ["open", "in_progress", "resolved"];

export default function DelayForm({ projectId, reportDate, dailyReportId, initial, onSaved, onCancel }) {
  const [tasks, setTasks] = useState([]);  // [{id, activity_code, description, total_float_days, is_critical_path}]
  const [tasksLoaded, setTasksLoaded] = useState(false);
  const [picker, setPicker] = useState("");
  const [form, setForm] = useState(() => ({
    description: initial?.description || "",
    severity: initial?.severity || "moderate",
    reason_category: initial?.reason_category || "weather",
    status: initial?.status || "open",
    resolved_date: initial?.resolved_date || "",
    resolved_note: initial?.resolved_note || "",
    // impacts: array of {task_id, projected_impact_days, float_at_logging_days?, ...}
    impacts: (initial?.per_task_impacts || []).map((i) => ({ ...i })),
  }));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    api.get(`/projects/${projectId}/tasks`)
      .then(({ data }) => setTasks(Array.isArray(data) ? data : []))
      .catch(() => setTasks([]))
      .finally(() => setTasksLoaded(true));
  }, [projectId]);

  const taskById = useMemo(() => Object.fromEntries(tasks.map((t) => [t.id, t])), [tasks]);
  const linkedIds = form.impacts.map((i) => i.task_id);
  const availableTasks = tasks.filter((t) => !linkedIds.includes(t.id));

  const addLinkedTask = () => {
    if (!picker) return;
    setForm((f) => ({
      ...f,
      impacts: [...f.impacts, { task_id: picker, projected_impact_days: 0 }],
    }));
    setPicker("");
  };
  const removeLinkedTask = (tid) => {
    setForm((f) => ({ ...f, impacts: f.impacts.filter((i) => i.task_id !== tid) }));
  };
  const setImpact = (tid, days) => {
    const v = days === "" ? 0 : Number(days);
    setForm((f) => ({
      ...f,
      impacts: f.impacts.map((i) => i.task_id === tid ? { ...i, projected_impact_days: v } : i),
    }));
  };

  // Live preview of consumes_float_only / pushes_critical_path so the user
  // can see the indicator before submit. Backend recomputes authoritative
  // values on save (see _resolve_task_impacts in field_events_router).
  const previewFlag = (impact) => {
    const t = taskById[impact.task_id];
    if (!t) return { color: "slate", label: "task not loaded" };
    const f = t.total_float_days;
    const proj = Number(impact.projected_impact_days || 0);
    if (f === null || f === undefined) return { color: "slate", label: "no float data" };
    if (proj <= f) return { color: "emerald", label: `within float (${f}d)` };
    if (t.is_critical_path) return { color: "red", label: `pushes CP (float ${f}d)` };
    return { color: "amber", label: `exceeds float ${f}d (non-CP)` };
  };

  const submit = async () => {
    if (!form.description.trim()) { toast.error("What happened? is required"); return; }
    if (!form.impacts.length) { toast.error("Link at least one task"); return; }
    setBusy(true);
    try {
      const payload = {
        description: form.description,
        severity: form.severity,
        reason_category: form.reason_category,
        status: form.status,
        linked_task_ids: form.impacts.map((i) => i.task_id),
        per_task_impacts: form.impacts.map((i) => ({
          task_id: i.task_id,
          projected_impact_days: Number(i.projected_impact_days || 0),
        })),
      };
      if (form.status === "resolved") {
        payload.resolved_date = form.resolved_date || localTodayISO();
        payload.resolved_note = form.resolved_note || null;
      }
      let res;
      if (initial?.id) {
        res = await api.patch(`/delays/${initial.id}`, payload);
      } else {
        res = await api.post(`/delays`, {
          ...payload,
          project_id: projectId,
          daily_report_id: dailyReportId || undefined,
          date: reportDate || localTodayISO(),
        });
      }
      onSaved?.(res.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message || "Save failed");
    } finally { setBusy(false); }
  };

  return (
    <div data-testid="delay-form" className="space-y-3">
      <div>
        <label className="text-xs uppercase tracking-wide text-slate-500">What happened? *</label>
        <textarea
          data-testid="delay-form-description"
          rows={4}
          value={form.description}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
          placeholder="Describe what caused the delay, who was affected, and any photos/observations on site"
          className="mt-1 w-full p-2 rounded border border-slate-200 text-sm"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs uppercase tracking-wide text-slate-500">Severity</label>
          <select data-testid="delay-form-severity" value={form.severity}
            onChange={(e) => setForm({ ...form, severity: e.target.value })}
            className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm">
            {SEVERITY_OPTS.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs uppercase tracking-wide text-slate-500">Category</label>
          <select data-testid="delay-form-category" value={form.reason_category}
            onChange={(e) => setForm({ ...form, reason_category: e.target.value })}
            className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm">
            {REASON_OPTS.map((r) => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}
          </select>
        </div>
      </div>

      <div>
        <label className="text-xs uppercase tracking-wide text-slate-500">Linked tasks *</label>
        <div className="mt-1 space-y-2">
          {form.impacts.map((imp) => {
            const t = taskById[imp.task_id];
            const flag = previewFlag(imp);
            const colorClass = {
              emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
              amber: "bg-amber-50 text-amber-700 border-amber-200",
              red: "bg-red-50 text-red-700 border-red-200",
              slate: "bg-slate-50 text-slate-600 border-slate-200",
            }[flag.color];
            return (
              <div key={imp.task_id} data-testid={`delay-form-impact-${imp.task_id}`} className="flex flex-wrap items-center gap-2 p-2 rounded border border-slate-200 bg-white">
                <button type="button" onClick={() => removeLinkedTask(imp.task_id)}
                  className="h-6 w-6 grid place-items-center rounded hover:bg-red-50 text-red-500"
                  aria-label="remove linked task">
                  <X className="h-3.5 w-3.5" />
                </button>
                <div className="flex-1 min-w-[180px] text-sm">
                  <span className="font-mono text-xs text-blue-700">{t?.activity_code || imp.task_id.slice(0, 6)}</span>
                  <span className="ml-2 text-slate-700">{t?.description || "(task not in current project list)"}</span>
                </div>
                <label className="flex items-center gap-1 text-xs">
                  <span className="text-slate-500">Impact (days)</span>
                  <input
                    data-testid={`delay-form-impact-days-${imp.task_id}`}
                    type="number" step="0.5" min="0"
                    value={imp.projected_impact_days ?? 0}
                    onChange={(e) => setImpact(imp.task_id, e.target.value)}
                    className="h-7 w-20 px-1 border border-slate-200 rounded text-sm tabular-nums"
                  />
                </label>
                <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] ${colorClass}`}>
                  {flag.color === "red" ? <AlertTriangle className="h-3 w-3" /> : <ShieldCheck className="h-3 w-3" />}
                  {flag.label}
                </span>
              </div>
            );
          })}
          {!form.impacts.length && (
            <div className="text-xs text-slate-400 italic">No tasks linked yet — pick one below.</div>
          )}
          <div className="flex items-center gap-2 pt-1">
            <select data-testid="delay-form-add-task-select" value={picker}
              onChange={(e) => setPicker(e.target.value)}
              className="flex-1 h-8 px-2 rounded border border-slate-200 text-sm"
              disabled={!tasksLoaded}>
              <option value="">{tasksLoaded ? `— select a task (${availableTasks.length} available) —` : "Loading project tasks…"}</option>
              {availableTasks.map((t) => (
                <option key={t.id} value={t.id}>
                  [{t.activity_code}] {t.description}{t.is_critical_path ? " · CP" : ""}
                </option>
              ))}
            </select>
            <button data-testid="delay-form-add-task" type="button" onClick={addLinkedTask} disabled={!picker}
              className="h-8 px-3 rounded bg-blue-600 text-white text-sm disabled:opacity-50 hover:bg-blue-700 inline-flex items-center gap-1">
              <Plus className="h-3 w-3" /> Add task
            </button>
          </div>
        </div>
      </div>

      <div>
        <label className="text-xs uppercase tracking-wide text-slate-500">Status</label>
        <select data-testid="delay-form-status" value={form.status}
          onChange={(e) => setForm({ ...form, status: e.target.value })}
          className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm">
          {STATUS_OPTS.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
        </select>
      </div>

      {form.status === "resolved" && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-xs uppercase tracking-wide text-slate-500">Resolved date</label>
            <input data-testid="delay-form-resolved-date" type="date"
              value={form.resolved_date || ""}
              onChange={(e) => setForm({ ...form, resolved_date: e.target.value })}
              className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm" />
          </div>
          <div>
            <label className="text-xs uppercase tracking-wide text-slate-500">Resolution note</label>
            <input data-testid="delay-form-resolved-note" type="text"
              value={form.resolved_note || ""}
              onChange={(e) => setForm({ ...form, resolved_note: e.target.value })}
              placeholder="What was done to resolve"
              className="mt-1 w-full h-9 px-2 rounded border border-slate-200 text-sm" />
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 pt-2 border-t border-slate-100">
        <button data-testid="delay-form-save" onClick={submit} disabled={busy}
          className="h-9 px-4 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50">
          {initial?.id ? "Save changes" : "Add delay"}
        </button>
        {onCancel && (
          <button data-testid="delay-form-cancel" type="button" onClick={onCancel} className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
