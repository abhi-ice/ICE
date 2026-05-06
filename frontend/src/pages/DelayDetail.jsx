// Sidebar Delay detail page.
//
// Shows the full delay record, the linked tasks with their CURRENT state
// (so you can see whether the projected impact has played out), the AI
// delay report (Phase B), and — Bucket C2 — three AI mitigation scenarios
// the PM can evaluate before picking one. The scenarios feature lives
// EXCLUSIVELY here: never shown on the daily report or anywhere else.
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronLeft, Sparkles, FileDown, Pencil, RefreshCw, Lightbulb, ChevronDown, ChevronUp } from "lucide-react";
import api from "../lib/api";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import DelayForm from "../components/forms/DelayForm";

export default function DelayDetail() {
  const { id } = useParams();
  const [delay, setDelay] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [editing, setEditing] = useState(false);
  const [generating, setGenerating] = useState(false);

  const refresh = async () => {
    try {
      const { data } = await api.get(`/delays/${id}`);
      setDelay(data);
      const ids = data.linked_task_ids || [];
      if (ids.length) {
        // Load each task individually — small N, no bulk endpoint exists
        const results = await Promise.all(ids.map((tid) =>
          api.get(`/tasks/${tid}`).then((r) => r.data).catch(() => null)
        ));
        setTasks(results.filter(Boolean));
      } else {
        setTasks([]);
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || "Load failed");
    }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [id]);

  if (!delay) return <div className="p-4 text-sm text-slate-500">Loading delay…</div>;

  const generateAIReport = async () => {
    setGenerating(true);
    try {
      const { data } = await api.post(`/delays/${id}/generate-ai-report`);
      setDelay(data);
      toast.success("AI report generated");
    } catch (e) {
      toast.error(e.response?.data?.detail || "AI report failed");
    } finally { setGenerating(false); }
  };

  const exportAIReportPDF = async () => {
    try {
      const res = await api.get(`/delays/${id}/ai-report/pdf`, { responseType: "blob" });
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = `delay_${id.slice(0, 8)}_ai_report.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e.response?.data?.detail || "PDF export failed");
    }
  };

  const sevColor = delay.severity === "critical" || delay.severity === "major" ? "pill-red" : "pill-slate";
  const statColor = delay.status === "resolved" ? "pill-green" : delay.status === "in_progress" ? "pill-amber" : "pill-slate";

  return (
    <div data-testid="delay-detail-page" className="space-y-4">
      <div className="flex items-center gap-2">
        <Link to="/delays" className="text-sm text-blue-600 hover:underline flex items-center gap-1">
          <ChevronLeft className="h-4 w-4" /> Back to delays
        </Link>
      </div>

      <div className="surface p-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="flex-1 min-w-[280px]">
            <div className="text-xs text-slate-500 uppercase tracking-wide">Delay logged {delay.date}</div>
            <div className="text-base mt-1 whitespace-pre-wrap">{delay.description}</div>
          </div>
          <div className="flex flex-col gap-1">
            <span className={`pill ${sevColor}`}>{delay.severity}</span>
            <span className={`pill ${statColor}`}>{(delay.status || "open").replace(/_/g, " ")}</span>
            {delay.reason_category && <span className="text-xs text-slate-500">{delay.reason_category}</span>}
          </div>
          <Button data-testid="delay-detail-edit" onClick={() => setEditing(true)} variant="outline" className="gap-1">
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Button>
        </div>

        <div className="mt-3 pt-3 border-t border-slate-100 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <Metric label="Logged date" value={delay.logged_date || delay.date} />
          <Metric label="Resolved date" value={delay.resolved_date || "—"} />
          <Metric label="Days open" value={delay.duration_days ?? "—"} />
          <Metric label="Linked tasks" value={(delay.linked_task_ids || []).length} />
        </div>
        {delay.resolved_note && (
          <div className="mt-2 text-xs text-slate-600">
            <span className="text-slate-400 uppercase tracking-wide mr-1">Resolution note:</span>
            {delay.resolved_note}
          </div>
        )}
      </div>

      <div className="surface p-4">
        <div className="font-medium text-sm mb-3">Linked tasks · current state</div>
        {!tasks.length && <div className="text-xs text-slate-400">No tasks linked to this delay.</div>}
        <div className="space-y-2">
          {tasks.map((t) => {
            const impact = (delay.per_task_impacts || []).find((i) => i.task_id === t.id) || {};
            // Bucket C1 Phase 5 — also pull the resolution-time outcome
            // (absorbed vs restored float) from cpm_impact_summary so a
            // resolved delay shows what actually happened, not just what
            // was projected at logging.
            const outcome = (delay.cpm_impact_summary || []).find((c) => c.task_id === t.id);
            const flagColor = impact.pushes_critical_path ? "pill-red"
              : impact.consumes_float_only ? "pill-green" : "pill-amber";
            return (
              <div key={t.id} className="border border-slate-200 rounded p-2 text-xs grid grid-cols-1 md:grid-cols-5 gap-2">
                <div className="md:col-span-2">
                  <span className="font-mono text-blue-700">{t.activity_code}</span>
                  <span className="ml-2">{t.description}</span>
                </div>
                <div>
                  <div className="text-slate-400 uppercase tracking-wide text-[10px]">Now</div>
                  <div>pct {t.pct_complete ?? 0}% · float {t.total_float_days ?? "—"}d{t.is_critical_path ? " · CP" : ""}</div>
                  {t.projected_finish_date && t.projected_finish_date > (t.planned_finish || "") && (
                    <div className="text-amber-700 mt-0.5">proj. finish {t.projected_finish_date}</div>
                  )}
                </div>
                <div>
                  <div className="text-slate-400 uppercase tracking-wide text-[10px]">At logging</div>
                  <div>impact {impact.projected_impact_days ?? 0}d · float {impact.float_at_logging_days ?? "—"}d</div>
                  <span className={`pill ${flagColor} mt-1 inline-block`}>
                    {impact.pushes_critical_path ? "pushes CP" : impact.consumes_float_only ? "within float" : "exceeds non-CP float"}
                  </span>
                </div>
                <div>
                  <div className="text-slate-400 uppercase tracking-wide text-[10px]">At resolution</div>
                  {outcome ? (
                    <div>
                      <div>actual {outcome.actual_days_open}d · absorbed {outcome.absorbed_days}d</div>
                      {outcome.restored_float_days > 0 ? (
                        <span className="pill pill-green mt-1 inline-block">+{outcome.restored_float_days}d float restored</span>
                      ) : (
                        <span className="pill pill-slate mt-1 inline-block">no float restored</span>
                      )}
                    </div>
                  ) : (
                    <div className="text-slate-400">—</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="surface p-4">
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="h-4 w-4 text-blue-600" />
          <div className="font-medium text-sm">AI delay report</div>
          {delay.ai_report_generated_at && (
            <span className="text-[10px] text-slate-400 ml-2">last generated {new Date(delay.ai_report_generated_at).toLocaleString()}</span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <Button data-testid="delay-ai-generate" onClick={generateAIReport} disabled={generating} className="gap-1 bg-blue-600 hover:bg-blue-700">
              <RefreshCw className={`h-3.5 w-3.5 ${generating ? "animate-spin" : ""}`} />
              {delay.ai_report ? "Regenerate" : "Generate AI delay report"}
            </Button>
            {delay.ai_report && (
              <Button data-testid="delay-ai-pdf" onClick={exportAIReportPDF} variant="outline" className="gap-1">
                <FileDown className="h-3.5 w-3.5" /> Export AI report as PDF
              </Button>
            )}
          </div>
        </div>
        {delay.ai_report ? (
          <div data-testid="delay-ai-report" className="text-sm whitespace-pre-wrap leading-relaxed text-slate-800 bg-slate-50 rounded p-3 border border-slate-200">
            {delay.ai_report}
          </div>
        ) : (
          <div className="text-xs text-slate-400">No AI report yet. Click Generate to produce a structured narrative covering linked tasks, float impact, and recommendations.</div>
        )}
      </div>

      <ScenariosSection delayId={id} />

      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Edit delay</DialogTitle></DialogHeader>
          <DelayForm
            projectId={delay.project_id}
            initial={delay}
            onSaved={() => { setEditing(false); refresh(); }}
            onCancel={() => setEditing(false)}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}

// Bucket C2 Phase 1 — AI mitigation scenarios block. Manages its own
// fetch/regenerate cycle so the parent doesn't need to wire scenario
// state through props. Phase 2 will add Choose / Change choice; Phase 3
// will add Record actuals.
function ScenariosSection({ delayId }) {
  const [data, setData] = useState(null);          // {scenarios_last_generated_at, active, history, chosen_mitigation_id}
  const [generating, setGenerating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [showHistory, setShowHistory] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const { data: d } = await api.get(`/delays/${delayId}/scenarios`);
      setData(d);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Could not load scenarios");
    } finally { setLoading(false); }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [delayId]);

  const generate = async () => {
    setGenerating(true);
    try {
      const { data: d } = await api.post(`/delays/${delayId}/generate-scenarios`);
      toast.success(`${d.scenarios.length} scenarios generated`);
      await refresh();
    } catch (e) {
      const msg = typeof e.response?.data?.detail === "string"
        ? e.response.data.detail
        : (e.response?.data?.detail?.[0]?.msg || e.message || "Generation failed");
      toast.error(msg);
    } finally { setGenerating(false); }
  };

  const active = data?.active || [];
  const hasActive = active.length > 0;
  const lastGen = data?.scenarios_last_generated_at;

  return (
    <div className="surface p-4" data-testid="delay-scenarios">
      <div className="flex items-center gap-2 mb-3">
        <Lightbulb className="h-4 w-4 text-amber-600" />
        <div className="font-medium text-sm">AI mitigation scenarios</div>
        {lastGen && (
          <span className="text-[10px] text-slate-400 ml-2">last generated {new Date(lastGen).toLocaleString()}</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <Button data-testid="scenarios-generate" onClick={generate} disabled={generating}
            className="gap-1 bg-amber-600 hover:bg-amber-700 text-white">
            <RefreshCw className={`h-3.5 w-3.5 ${generating ? "animate-spin" : ""}`} />
            {hasActive ? "Regenerate scenarios" : "Generate 3 mitigation scenarios"}
          </Button>
        </div>
      </div>

      {loading && <div className="text-xs text-slate-400">Loading scenarios…</div>}

      {!loading && !hasActive && (
        <div className="text-xs text-slate-500 bg-slate-50 border border-slate-200 rounded p-3">
          The AI will produce three ranked mitigation scenarios — wait/monitor, re-sequence, accelerate, and similar.
          Each one shows the schedule impact, cost impact, risk level, and the AI's confidence. You can pick one (Phase 2 — coming) and the schedule will reflow accordingly.
        </div>
      )}

      {!loading && hasActive && (
        <div data-testid="scenarios-active" className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {active.map((s) => <ScenarioCard key={s.id} scenario={s} />)}
        </div>
      )}

      {!loading && (data?.history?.length || 0) > 0 && (
        <div className="mt-4 pt-3 border-t border-slate-100">
          <button onClick={() => setShowHistory((v) => !v)}
            className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700">
            {showHistory ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            Past generations ({data.history.length} scenario{data.history.length === 1 ? "" : "s"} superseded)
          </button>
          {showHistory && (
            <div className="mt-2 space-y-1 text-xs">
              {data.history.map((h) => (
                <div key={h.id} className="flex items-center gap-2 text-slate-600 border-b border-slate-100 py-1">
                  <span className="text-[10px] font-mono text-slate-400">{h.generation_at?.slice(0, 16)?.replace("T", " ")}</span>
                  <span className="font-mono text-[10px] uppercase">{h.scenario_label}</span>
                  <span className="flex-1 truncate">{h.strategy_name}</span>
                  <span className="text-slate-400">{h.estimated_schedule_impact_days}d · ${h.estimated_cost_impact_usd}</span>
                  <span className="text-[10px] text-slate-400">{h.decision_status}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ScenarioCard({ scenario: s }) {
  const [showReasoning, setShowReasoning] = useState(false);
  const labelColor = s.scenario_label === "A" ? "bg-blue-600"
    : s.scenario_label === "B" ? "bg-emerald-600"
    : s.scenario_label === "C" ? "bg-purple-600"
    : "bg-slate-600";
  const riskColor = s.estimated_risk_level === "high" ? "pill-red"
    : s.estimated_risk_level === "medium" ? "pill-amber"
    : "pill-green";
  const confPct = Math.round((s.estimated_confidence ?? 0) * 100);
  const scheduleSign = s.estimated_schedule_impact_days > 0 ? "+" : "";
  return (
    <div data-testid={`scenario-card-${s.scenario_label}`}
      className="border border-slate-200 rounded-lg overflow-hidden bg-white flex flex-col">
      <div className={`${labelColor} text-white px-3 py-2 flex items-center gap-2`}>
        <div className="font-mono text-sm font-bold">{s.scenario_label}</div>
        <div className="text-sm flex-1 truncate">{s.strategy_name}</div>
      </div>
      <div className="p-3 text-xs space-y-2 flex-1">
        <div>
          <div className="text-slate-400 uppercase tracking-wide text-[10px]">Assumption</div>
          <div className="text-slate-700">{s.assumption}</div>
        </div>
        <div>
          <div className="text-slate-400 uppercase tracking-wide text-[10px]">Action</div>
          <div className="text-slate-700">{s.proposed_action_description}</div>
        </div>
        {s.prerequisites && s.prerequisites.length > 0 && (
          <div>
            <div className="text-slate-400 uppercase tracking-wide text-[10px]">Prerequisites</div>
            <ul className="list-disc list-inside text-slate-600">
              {s.prerequisites.map((p, i) => <li key={i}>{p}</li>)}
            </ul>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2 pt-2 border-t border-slate-100">
          <Metric label="Schedule" value={`${scheduleSign}${s.estimated_schedule_impact_days}d`} />
          <Metric label="Cost" value={`$${(s.estimated_cost_impact_usd || 0).toLocaleString()}`} />
          <div>
            <div className="text-[10px] text-slate-500 uppercase tracking-wide">Risk</div>
            <span className={`pill ${riskColor} mt-1 inline-block capitalize`}>{s.estimated_risk_level}</span>
          </div>
          <div>
            <div className="text-[10px] text-slate-500 uppercase tracking-wide">Confidence</div>
            <div className="text-sm tabular-nums">{confPct}%</div>
          </div>
        </div>
        <button onClick={() => setShowReasoning((v) => !v)}
          className="inline-flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-700 pt-2">
          {showReasoning ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          {showReasoning ? "Hide reasoning" : "Show reasoning"}
        </button>
        {showReasoning && (
          <div className="text-[11px] text-slate-600 bg-slate-50 rounded p-2 border border-slate-100 whitespace-pre-wrap">
            {s.ai_reasoning || "(no reasoning provided)"}
          </div>
        )}
      </div>
      {/* Phase 2 will add a "Choose this scenario" CTA here. */}
      <div className="px-3 py-2 border-t border-slate-100 bg-slate-50/50 text-[10px] text-slate-400 italic">
        Phase 1 preview — Choose / Change / Record-outcome controls land in Phase 2 + 3.
      </div>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div className="bg-slate-50 rounded px-2 py-1.5 border border-slate-100">
      <div className="text-[10px] text-slate-500 uppercase tracking-wide">{label}</div>
      <div className="text-sm tabular-nums">{value}</div>
    </div>
  );
}
