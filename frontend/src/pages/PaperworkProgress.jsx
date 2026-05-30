// Paperwork progress (pt.79) — lightweight progress + status entry for the
// submittal / approval / closeout PAPERWORK tasks that otherwise have no way to
// show progress and so falsely read "behind" at 0%. Setting "In progress" (or
// dragging progress) keeps the task on-track; "Blocked" flags it. This drives
// the SAME computed_status the Tasks + Look-ahead CPM already display — see the
// paperwork branch in backend/services/task_status.py:derive_status. A proper
// submittal-SLA model replaces this shortcut in v2.
import { useEffect, useState } from "react";
import { FileSignature, Loader2 } from "lucide-react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";

const PAPERWORK_CATS = ["preconstruction_paperwork", "construction_paperwork", "closeout_paperwork"];
const CAT_LABEL = {
  preconstruction_paperwork: "Preconstruction paperwork",
  construction_paperwork: "Construction paperwork",
  closeout_paperwork: "Closeout paperwork",
};
// dropdown values map straight onto the task.status field
const STATUS_OPTS = [
  { value: "not_started", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "blocked", label: "Blocked" },
];

function statusPill(cs) {
  const c = cs || "on_track";
  if (c === "complete") return { label: "complete", cls: "pill-green" };
  if (c === "blocked") return { label: "blocked", cls: "pill-red" };
  if (c === "behind") return { label: "behind", cls: "pill-amber" };
  return { label: "on track", cls: "pill-green" };
}

function Row({ t, saving, onPct, onStatus }) {
  const [pct, setPct] = useState(Math.round(t.pct_complete || 0));
  useEffect(() => { setPct(Math.round(t.pct_complete || 0)); }, [t.pct_complete]);
  const statusVal = ["not_started", "in_progress", "blocked"].includes(t.status) ? t.status : "not_started";
  const pill = statusPill(t.computed_status);
  return (
    <tr data-testid={`paperwork-row-${t.activity_code}`}>
      <td className="font-mono text-xs" style={{ color: "var(--v4-fg-muted)" }}>{t.activity_code}</td>
      <td><div className="text-sm whitespace-normal break-words" style={{ color: "var(--v4-fg-primary)" }}>{t.description}</div></td>
      <td style={{ minWidth: 200 }}>
        <div className="flex items-center gap-2">
          <input
            type="range" min="0" max="100" step="5" value={pct}
            data-testid={`paperwork-slider-${t.activity_code}`}
            className="eod-range flex-1"
            style={{ "--eod-fill": `${pct}%` }}
            onChange={(e) => setPct(Number(e.target.value))}
            onMouseUp={(e) => onPct(t.id, Number(e.target.value))}
            onTouchEnd={(e) => onPct(t.id, Number(e.target.value))}
            onKeyUp={(e) => onPct(t.id, Number(e.target.value))}
          />
          <span className="text-xs tabular-nums w-9 text-right" style={{ color: "var(--v4-fg-muted)" }}>{pct}%</span>
        </div>
      </td>
      <td>
        <select
          value={statusVal}
          data-testid={`paperwork-status-${t.activity_code}`}
          onChange={(e) => onStatus(t.id, e.target.value)}
          className="dr-mp-input text-xs"
          style={{ height: 30, minWidth: 120 }}
        >
          {STATUS_OPTS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </td>
      <td>
        <span className={`pill ${pill.cls}`}>{pill.label}</span>
        {saving && <Loader2 className="inline h-3 w-3 ml-2 animate-spin" style={{ color: "var(--v4-fg-faint)" }} />}
      </td>
    </tr>
  );
}

export default function PaperworkProgress() {
  const { activeId, projects } = useProjects();
  const project = projects.find((p) => p.id === activeId);
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(false);
  const [savingId, setSavingId] = useState(null);

  const load = (silent) => {
    if (!activeId) return Promise.resolve();
    if (!silent) setLoading(true);
    return api.get(`/projects/${activeId}/tasks`)
      .then(({ data }) => setTasks((data || []).filter((t) => PAPERWORK_CATS.includes(t.work_category))))
      .catch(() => toast.error("Failed to load paperwork tasks"))
      .finally(() => { if (!silent) setLoading(false); });
  };
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  // Optimistic patch + silent reload so the Schedule pill (computed_status,
  // recomputed read-time by GET /tasks) refreshes immediately.
  const commit = async (id, fields) => {
    setSavingId(id);
    setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, ...fields } : t)));
    try {
      await api.patch(`/tasks/${id}`, fields);
      await load(true);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed — try again");
      await load(true);
    } finally {
      setSavingId(null);
    }
  };
  const onPct = (id, v) => commit(id, { pct_complete: v });
  const onStatus = (id, v) => commit(id, { status: v });

  const groups = PAPERWORK_CATS
    .map((c) => ({ cat: c, items: tasks.filter((t) => t.work_category === c) }))
    .filter((g) => g.items.length);

  return (
    <div data-testid="paperwork-progress-page" className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Paperwork progress</h1>
        <p className="text-sm mt-1" style={{ color: "var(--v4-fg-muted)" }}>
          {project?.name ? `${project.name} · ` : ""}Submittal, approval &amp; closeout paperwork.
          Set <b>In progress</b> (or drag progress) to keep a task on-track in the schedule; mark <b>Blocked</b> if
          it&rsquo;s stuck past its finish date. This feeds the Tasks &amp; Look-ahead CPM directly.
        </p>
      </div>

      {!activeId ? (
        <div className="surface p-6 text-sm" style={{ color: "var(--v4-fg-muted)" }}>Select a project to see its paperwork.</div>
      ) : loading && !tasks.length ? (
        <div className="surface p-6 flex items-center gap-2 text-sm" style={{ color: "var(--v4-fg-muted)" }}>
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : !tasks.length ? (
        <div className="surface p-6 text-sm" style={{ color: "var(--v4-fg-muted)" }}>
          No paperwork tasks on this project yet. Tasks tagged preconstruction / construction / closeout paperwork appear here.
        </div>
      ) : (
        groups.map((g) => (
          <div key={g.cat} className="surface overflow-hidden">
            <div className="px-4 py-2 text-xs font-semibold flex items-center gap-2"
              style={{ background: "var(--v4-bg-surface-2)", borderBottom: "1px solid var(--v4-border)", color: "var(--v4-fg-secondary)" }}>
              <FileSignature className="h-3.5 w-3.5" /> {CAT_LABEL[g.cat]} · {g.items.length}
            </div>
            <div className="overflow-x-auto">
              <table className="dense-table">
                <thead>
                  <tr><th>Code</th><th>Task</th><th>Progress</th><th>Status</th><th>Schedule</th></tr>
                </thead>
                <tbody>
                  {g.items.map((t) => (
                    <Row key={t.id} t={t} saving={savingId === t.id} onPct={onPct} onStatus={onStatus} />
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </div>
  );
}
