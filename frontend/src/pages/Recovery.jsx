import { useEffect, useState } from "react";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { toast } from "../components/ui/sonner";
import { Sparkles, Check, BarChart3 } from "lucide-react";

export default function Recovery() {
  const { activeId, projects } = useProjects();
  const [tasks, setTasks] = useState([]);
  const [history, setHistory] = useState([]);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [evalData, setEvalData] = useState(null);

  const project = projects.find((p) => p.id === activeId);

  const loadHistory = () => activeId && api.get(`/ai/recovery?project_id=${activeId}`).then(({ data }) => setHistory(data));
  const loadEval = () => activeId && api.get(`/ai/recovery/eval-harness?project_id=${activeId}`).then(({ data }) => setEvalData(data));
  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data.filter((t) => t.status !== "complete")));
    loadHistory(); loadEval();
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [activeId]);

  const generate = async () => {
    if (!target) return;
    setBusy(true);
    try {
      const { data } = await api.post("/ai/recovery", { project_id: activeId, target_activity_id: target, trigger: "manual" });
      toast.success(`Generated ${data.suggestions?.length || 0} suggestions`);
      loadHistory();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    } finally {
      setBusy(false);
    }
  };

  const apply = async (recoveryId, idx) => {
    try {
      const { data } = await api.post(`/ai/recovery/${recoveryId}/apply`, { suggestion_index: idx });
      toast.success(`Applied: ${(data.notes || []).join(" · ")}`);
      loadHistory(); loadEval();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Apply failed");
    }
  };

  return (
    <div data-testid="recovery-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <Sparkles className="h-5 w-5 text-primary" />
        <h1 className="text-2xl font-semibold">AI Recovery</h1>
      </div>
      <p className="text-sm text-muted-foreground">
        {project?.name} · pick a slipping activity and let Claude (Sonnet 4.5) propose 3-5 concrete recovery actions.
      </p>

      <div className="bg-card border border-border rounded-lg p-4 flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-[280px]">
          <label className="field-label">Target activity</label>
          <select data-testid="recovery-target" value={target} onChange={(e) => setTarget(e.target.value)} className="w-full h-9 px-2 rounded border border-input bg-background text-sm">
            <option value="">— select activity —</option>
            {tasks.map((t) => (
              <option key={t.id} value={t.id}>[{t.activity_code}] {t.description}</option>
            ))}
          </select>
        </div>
        <button data-testid="recovery-generate" onClick={generate} disabled={!target || busy} className="h-9 px-4 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50">
          {busy ? "Generating…" : "Generate suggestions"}
        </button>
      </div>

      {evalData && evalData.total_applied > 0 && (
        <div data-testid="recovery-eval" className="bg-card border border-border rounded-lg p-4">
          <div className="flex items-center gap-2 mb-2">
            <BarChart3 className="h-4 w-4 text-blue-600" />
            <div className="font-medium text-sm">Eval harness — past performance</div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
            <Stat label="Applied" value={evalData.total_applied} />
            <Stat label="Predicted (d)" value={evalData.total_predicted_days} />
            <Stat label="Realized (d)" value={evalData.total_realized_days} />
            <Stat label="Score" value={evalData.accuracy_score ?? "—"} />
          </div>
        </div>
      )}

      <div className="space-y-3">
        {history.map((h) => (
          <div key={h.id} data-testid={`recovery-card-${h.id}`} className="bg-card border border-border rounded-lg p-4">
            <div className="text-xs text-muted-foreground mb-2 flex items-center gap-2">
              Generated {new Date(h.generated_at).toLocaleString()} • trigger: {h.trigger}
              {h.status === "applied" && <span className="pill pill-green text-[10px]">applied (idx {h.chosen_suggestion_index})</span>}
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {(h.suggestions || []).map((s, i) => {
                const isApplied = h.status === "applied" && h.chosen_suggestion_index === i;
                return (
                  <div key={i} data-testid={`rec-sugg-${h.id}-${i}`} className={`border rounded p-3 ${isApplied ? "bg-emerald-50 border-emerald-300" : "border-border bg-muted/30"}`}>
                    <div className="flex items-center justify-between text-xs mb-1">
                      <span className="pill pill-blue capitalize">{s.category}</span>
                      <span className="font-mono">conf {(s.confidence ?? 0).toFixed(2)}</span>
                    </div>
                    <div className="font-medium text-sm">{s.title}</div>
                    <div className="text-xs text-muted-foreground mt-1">{s.rationale}</div>
                    <div className="mt-2 grid grid-cols-2 gap-1 text-xs">
                      <div>Recover: <span className="font-mono">{s.predicted_time_recovery_days}d</span></div>
                      <div>Cost: <span className="font-mono">${s.predicted_cost_usd?.toLocaleString()}</span></div>
                    </div>
                    {h.status !== "applied" && (
                      <button
                        data-testid={`rec-apply-${h.id}-${i}`}
                        onClick={() => apply(h.id, i)}
                        className="mt-3 h-7 px-3 rounded bg-blue-600 text-white text-xs inline-flex items-center gap-1"
                      >
                        <Check className="h-3 w-3" /> Apply
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
        {!history.length && <div className="text-sm text-muted-foreground">No suggestions yet.</div>}
      </div>
    </div>
  );
}

const Stat = ({ label, value }) => (
  <div className="border border-slate-200 rounded p-3">
    <div className="text-xs text-slate-500">{label}</div>
    <div className="text-xl font-semibold tabular-nums">{value}</div>
  </div>
);
