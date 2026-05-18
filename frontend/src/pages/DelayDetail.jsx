// Sidebar Delay detail page.
//
// Shows the full delay record, the linked tasks with their CURRENT state
// (so you can see whether the projected impact has played out), the AI
// delay report (Phase B), and — Bucket C2 — three AI mitigation scenarios
// the PM can evaluate before picking one. The scenarios feature lives
// EXCLUSIVELY here: never shown on the daily report or anywhere else.
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronLeft, Sparkles, FileDown, Pencil, RefreshCw, Lightbulb, ChevronDown, ChevronUp, CheckCircle2, RotateCcw } from "lucide-react";
import api from "../lib/api";
import { Button } from "../components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "../components/ui/sonner";
import DelayForm from "../components/forms/DelayForm";
// Bucket G Phase 4 (2026-05-11) -- Jigsaw pre-fill wrapper around the
// AI report block + an inline display for the chosen_mitigation_id
// suggestion produced by calibration_rag in Phase 2.
import JigsawField from "../components/forms/JigsawField";
import { getUser } from "../lib/auth";
// CPM Part G (2026-05-15) -- delay dates rendered MM/DD/YYYY via chokepoint.
import { formatDateDisplay } from "../lib/dateUtil";

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
            <div className="text-xs text-slate-500 uppercase tracking-wide">Delay logged {formatDateDisplay(delay.date)}</div>
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
          <Metric label="Logged date" value={formatDateDisplay(delay.logged_date || delay.date)} />
          <Metric label="Resolved date" value={formatDateDisplay(delay.resolved_date)} />
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
                  <div className="text-slate-400 uppercase tracking-wide text-xs">Now</div>
                  <div>pct {t.pct_complete ?? 0}% · float {t.total_float_days ?? "—"}d{t.is_critical_path ? " · CP" : ""}</div>
                  {t.projected_finish_date && t.projected_finish_date > (t.planned_finish || "") && (
                    <div className="text-amber-700 mt-0.5">proj. finish {t.projected_finish_date}</div>
                  )}
                </div>
                <div>
                  <div className="text-slate-400 uppercase tracking-wide text-xs">At logging</div>
                  <div>impact {impact.projected_impact_days ?? 0}d · float {impact.float_at_logging_days ?? "—"}d</div>
                  <span className={`pill ${flagColor} mt-1 inline-block`}>
                    {impact.pushes_critical_path ? "pushes CP" : impact.consumes_float_only ? "within float" : "exceeds non-CP float"}
                  </span>
                </div>
                <div>
                  <div className="text-slate-400 uppercase tracking-wide text-xs">At resolution</div>
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
            <span className="text-xs text-slate-400 ml-2">last generated {new Date(delay.ai_report_generated_at).toLocaleString()}</span>
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
        {/* Bucket G Phase 4 -- wrap the AI report block in JigsawField
            so when calibration_rag drops a fresh narrative suggestion,
            the PM can Accept/Reject it inline + the audit trail captures
            the human-confirmation step. Accept persists via Pp PATCH
            because the suggestion landed on delay.ai_report. */}
        <JigsawField
          entityType="delay" entityId={id}
          fieldPath="ai_report"
          userRole={(getUser()?.role || "").toLowerCase()}
          fieldLabel="ai_report"
          renderSuggestedValue={(v) => (
            <span className="whitespace-pre-wrap">
              {String(v || "").slice(0, 400)}
              {String(v || "").length > 400 && "…"}
            </span>
          )}
          onConfirm={async (v) => {
            // Persist the suggested narrative on the delay row when
            // the PM accepts. Best-effort; if the patch fails we leave
            // the audit confirmation intact and surface a toast.
            try {
              await api.patch(`/delays/${id}`, { ai_report: String(v || "") });
              refresh();
            } catch (e) {
              toast.error(
                e.response?.data?.detail || "Failed to apply AI narrative",
              );
            }
          }}
        >
          {delay.ai_report ? (
            <div data-testid="delay-ai-report" className="text-sm whitespace-pre-wrap leading-relaxed text-slate-800 bg-slate-50 rounded p-3 border border-slate-200">
              {delay.ai_report}
            </div>
          ) : (
            <div className="text-xs text-slate-400">No AI report yet. Click Generate to produce a structured narrative covering linked tasks, float impact, and recommendations.</div>
          )}
        </JigsawField>
      </div>

      {/* Bucket G Phase 4 -- chosen_mitigation_id suggestion display.
          calibration_rag dropped a Jigsaw pointer at the top-confidence
          scenario after mitigation generation; surface it here so the
          PM has the AI's pick visible alongside the 3 scenarios. */}
      <JigsawField
        entityType="delay" entityId={id}
        fieldPath="chosen_mitigation_id"
        userRole={(getUser()?.role || "").toLowerCase()}
        fieldLabel="chosen_mitigation_id"
        renderSuggestedValue={(v) => (
          <code className="text-xs">scenario {String(v || "").slice(0, 8)}</code>
        )}
        onConfirm={async (v) => {
          try {
            // Mirrors the existing /choose endpoint on the mitigations
            // router. The chosen scenario id IS the suggested value.
            // Body is required by MitigationChooseRequest; rationale
            // is optional (null = "accepted AI suggestion without
            // explicit rationale").
            await api.post(`/mitigations/${v}/choose`, {
              decision_rationale: null,
            });
            refresh();
            toast.success("AI-suggested mitigation chosen");
          } catch (e) {
            toast.error(
              e.response?.data?.detail || "Failed to choose mitigation",
            );
          }
        }}
      >
        {/* Pass-through anchor: the ScenariosSection below renders the
            actual UI; we only need the Jigsaw chrome above it. */}
        <div data-testid="delay-chosen-mitigation-anchor" />
      </JigsawField>

      <ScenariosSection delayId={id} delayStatus={delay.status} />

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

// Bucket C2 Phase 1 + 2 — AI mitigation scenarios block. Manages its own
// fetch/regenerate cycle so the parent doesn't need to wire scenario
// state through props. Phase 2 wires Choose / Change choice; Phase 3
// will add Record actuals.
function ScenariosSection({ delayId, delayStatus }) {
  const [data, setData] = useState(null);          // {scenarios_last_generated_at, active, history, chosen_mitigation_id}
  const [generating, setGenerating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [showHistory, setShowHistory] = useState(false);
  // Phase 2 — confirmation dialog state. We open it with the scenario the
  // user clicked Choose on; closing without confirming aborts the action.
  const [chooseTarget, setChooseTarget] = useState(null);
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  // Phase 3 — outcome capture state. outcomeTarget = the chosen mitigation
  // we're recording against. autoOpened tracks whether we've already
  // pop'd the modal once for this transition (prevents re-popping every
  // refresh while the user has it open).
  const [outcomeTarget, setOutcomeTarget] = useState(null);
  const [outcome, setOutcome] = useState({ actual_cost_usd: "", actual_time_recovered_days: "", outcome_notes: "" });
  const [autoOpenedFor, setAutoOpenedFor] = useState(null);

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

  const confirmChoose = async () => {
    if (!chooseTarget) return;
    setBusy(true);
    try {
      await api.post(`/mitigations/${chooseTarget.id}/choose`, {
        decision_rationale: rationale || null,
      });
      toast.success(`Scenario ${chooseTarget.scenario_label} chosen — schedule reflowed`);
      setChooseTarget(null);
      setRationale("");
      await refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Choose failed");
    } finally { setBusy(false); }
  };

  const unchoose = async (m) => {
    if (!window.confirm("Revert the chosen scenario? The delay's per-task impacts and the projected finish dates will go back to what they were before you chose.")) return;
    setBusy(true);
    try {
      await api.post(`/mitigations/${m.id}/unchoose`);
      toast.success("Choice reverted — schedule restored");
      await refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Unchoose failed");
    } finally { setBusy(false); }
  };

  const active = data?.active || [];
  const hasActive = active.length > 0;
  const lastGen = data?.scenarios_last_generated_at;
  const chosenId = data?.chosen_mitigation_id;
  const chosen = active.find((s) => s.id === chosenId);

  // Phase 3 — auto-open outcome modal when the chosen mitigation flips
  // to 'executed' (i.e. the parent delay was just resolved) and we
  // haven't already prompted for this id. User can also click "Record
  // actuals" later if they Skip.
  useEffect(() => {
    if (!chosen) return;
    const needs = chosen.decision_status === "executed" && !chosen.outcome_recorded_at;
    if (needs && autoOpenedFor !== chosen.id) {
      setOutcomeTarget(chosen);
      setOutcome({ actual_cost_usd: "", actual_time_recovered_days: "", outcome_notes: "" });
      setAutoOpenedFor(chosen.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chosen?.id, chosen?.decision_status, chosen?.outcome_recorded_at]);

  const openOutcomeManually = (m) => {
    setOutcomeTarget(m);
    setOutcome({
      actual_cost_usd: m.actual_cost_usd != null ? String(m.actual_cost_usd) : "",
      actual_time_recovered_days: m.actual_time_recovered_days != null ? String(m.actual_time_recovered_days) : "",
      outcome_notes: m.outcome_notes || "",
    });
  };

  const submitOutcome = async () => {
    if (!outcomeTarget) return;
    const payload = {};
    if (outcome.actual_cost_usd !== "") payload.actual_cost_usd = Number(outcome.actual_cost_usd);
    if (outcome.actual_time_recovered_days !== "") payload.actual_time_recovered_days = Number(outcome.actual_time_recovered_days);
    if (outcome.outcome_notes) payload.outcome_notes = outcome.outcome_notes;
    if (!Object.keys(payload).length) {
      toast.error("Enter at least one of cost, days, or notes");
      return;
    }
    setBusy(true);
    try {
      await api.patch(`/mitigations/${outcomeTarget.id}/record-outcome`, payload);
      toast.success("Outcome recorded — feeds into AI calibration on next generation");
      setOutcomeTarget(null);
      await refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    } finally { setBusy(false); }
  };

  return (
    <div className="surface p-4" data-testid="delay-scenarios">
      <div className="flex items-center gap-2 mb-3">
        <Lightbulb className="h-4 w-4 text-amber-600" />
        <div className="font-medium text-sm">AI mitigation scenarios</div>
        {lastGen && (
          <span className="text-xs text-slate-400 ml-2">last generated {new Date(lastGen).toLocaleString()}</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {/* Block regenerate while a scenario is chosen — would orphan
              the chosen pointer and confuse the schedule reflow. User
              must Change choice first. */}
          <Button data-testid="scenarios-generate" onClick={generate} disabled={generating || !!chosenId}
            title={chosenId ? "Click 'Change choice' on the executing scenario before regenerating" : ""}
            className="gap-1 bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-50">
            <RefreshCw className={`h-3.5 w-3.5 ${generating ? "animate-spin" : ""}`} />
            {hasActive ? "Regenerate scenarios" : "Generate 3 mitigation scenarios"}
          </Button>
        </div>
      </div>

      {loading && <div className="text-xs text-slate-400">Loading scenarios…</div>}

      {!loading && !hasActive && (
        <div className="text-xs text-slate-500 bg-slate-50 border border-slate-200 rounded p-3">
          The AI will produce three ranked mitigation scenarios — wait/monitor, re-sequence, accelerate, and similar.
          Each one shows the schedule impact, cost impact, risk level, and the AI's confidence. Pick one and the
          delay's projected impact reflows across the linked tasks.
        </div>
      )}

      {chosen && (
        <div data-testid="scenarios-executing-banner"
          className="mb-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 flex items-center gap-2 text-sm">
          <CheckCircle2 className="h-4 w-4 text-emerald-700" />
          <div className="flex-1">
            <span className="font-mono text-emerald-700 mr-2">EXECUTING: Scenario {chosen.scenario_label}</span>
            <span className="text-emerald-900">{chosen.strategy_name}</span>
            {chosen.decision_rationale && (
              <div className="text-xs text-emerald-800/80 mt-0.5">Rationale: {chosen.decision_rationale}</div>
            )}
          </div>
          <button data-testid="scenarios-change-choice" onClick={() => unchoose(chosen)} disabled={busy}
            className="inline-flex items-center gap-1 text-xs text-emerald-700 hover:text-emerald-900 underline">
            <RotateCcw className="h-3 w-3" /> Change choice
          </button>
        </div>
      )}

      {!loading && hasActive && (
        <div data-testid="scenarios-active" className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {active.map((s) => (
            <ScenarioCard key={s.id} scenario={s}
              chosenId={chosenId}
              onChoose={() => { setChooseTarget(s); setRationale(""); }}
              onRecordActuals={() => openOutcomeManually(s)} />
          ))}
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
                  <span className="text-xs font-mono text-slate-400">{h.generation_at?.slice(0, 16)?.replace("T", " ")}</span>
                  <span className="font-mono text-xs uppercase">{h.scenario_label}</span>
                  <span className="flex-1 truncate">{h.strategy_name}</span>
                  <span className="text-slate-400">{h.estimated_schedule_impact_days}d · ${h.estimated_cost_impact_usd}</span>
                  <span className="text-xs text-slate-400">{h.decision_status}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Choose-confirmation dialog */}
      <Dialog open={!!chooseTarget} onOpenChange={(v) => !v && setChooseTarget(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Choose scenario {chooseTarget?.scenario_label}?</DialogTitle>
          </DialogHeader>
          {chooseTarget && (
            <div className="space-y-3 text-sm">
              <div className="text-slate-700">{chooseTarget.strategy_name}</div>
              <div className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded p-2">
                Choosing this scenario will set the delay's projected schedule impact to{" "}
                <b>{chooseTarget.estimated_schedule_impact_days}d</b> total, redistribute it
                across the {(chooseTarget.linked_task_ids || []).length} linked task(s) proportionally,
                and reflow downstream projected finish dates.
              </div>
              <div>
                <label className="text-xs text-slate-500 uppercase tracking-wide">Rationale (optional)</label>
                <textarea data-testid="scenario-choose-rationale"
                  value={rationale} onChange={(e) => setRationale(e.target.value)}
                  placeholder="Why is the team picking this one over the alternatives?"
                  className="mt-1 w-full p-2 rounded border border-slate-200 text-sm h-20" />
              </div>
              <div className="flex gap-2 justify-end">
                <button onClick={() => setChooseTarget(null)} disabled={busy}
                  className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
                  Cancel
                </button>
                <button data-testid="scenario-choose-confirm" onClick={confirmChoose} disabled={busy}
                  className="h-9 px-4 rounded bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-50">
                  {busy ? "Choosing…" : `Choose ${chooseTarget.scenario_label}`}
                </button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Phase 3 — outcome capture dialog. Auto-opens once when the chosen
          mitigation flips to 'executed' (delay just resolved) and re-opens
          on demand via the "Record actuals" button on the chosen card. */}
      <Dialog open={!!outcomeTarget} onOpenChange={(v) => !v && setOutcomeTarget(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Record actual outcome — Scenario {outcomeTarget?.scenario_label}</DialogTitle>
          </DialogHeader>
          {outcomeTarget && (
            <div className="space-y-3 text-sm">
              <div className="text-slate-700">{outcomeTarget.strategy_name}</div>
              <div className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded p-2">
                Estimated at choose time: <b>{outcomeTarget.estimated_schedule_impact_days}d</b> · ${(outcomeTarget.estimated_cost_impact_usd || 0).toLocaleString()} · confidence {Math.round((outcomeTarget.estimated_confidence ?? 0) * 100)}%.
                Record what actually happened — these numbers feed the AI's calibration on future scenario generations.
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="text-xs text-slate-500 uppercase tracking-wide">Actual cost (USD)</label>
                  <input data-testid="outcome-cost" type="number" step="0.01"
                    value={outcome.actual_cost_usd}
                    onChange={(e) => setOutcome({ ...outcome, actual_cost_usd: e.target.value })}
                    className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm tabular-nums" />
                </div>
                <div>
                  <label className="text-xs text-slate-500 uppercase tracking-wide">Time recovered (days)</label>
                  <input data-testid="outcome-days" type="number" step="0.5"
                    value={outcome.actual_time_recovered_days}
                    onChange={(e) => setOutcome({ ...outcome, actual_time_recovered_days: e.target.value })}
                    className="mt-1 w-full h-9 px-2 border border-slate-200 rounded text-sm tabular-nums" />
                </div>
              </div>
              <div>
                <label className="text-xs text-slate-500 uppercase tracking-wide">Notes</label>
                <textarea data-testid="outcome-notes"
                  value={outcome.outcome_notes}
                  onChange={(e) => setOutcome({ ...outcome, outcome_notes: e.target.value })}
                  placeholder="What was actually done; any side effects; lessons for future similar delays."
                  className="mt-1 w-full p-2 rounded border border-slate-200 text-sm h-20" />
              </div>
              <div className="flex gap-2 justify-end">
                <button onClick={() => setOutcomeTarget(null)} disabled={busy}
                  className="h-9 px-3 rounded border border-slate-200 bg-white text-sm hover:bg-slate-50">
                  Skip for now
                </button>
                <button data-testid="outcome-save" onClick={submitOutcome} disabled={busy}
                  className="h-9 px-4 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50">
                  {busy ? "Saving…" : "Save outcome"}
                </button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ScenarioCard({ scenario: s, chosenId, onChoose, onRecordActuals }) {
  const [showReasoning, setShowReasoning] = useState(false);
  const isChosen = chosenId === s.id;
  const isOtherChosen = chosenId && chosenId !== s.id;
  const isExecuted = s.decision_status === "executed";
  const hasOutcome = !!s.outcome_recorded_at;
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
      className={`border rounded-lg overflow-hidden bg-white flex flex-col transition-opacity ${
        isChosen ? "border-emerald-400 ring-2 ring-emerald-200"
        : isOtherChosen ? "border-slate-200 opacity-60"
        : "border-slate-200"
      }`}>
      <div className={`${labelColor} text-white px-3 py-2 flex items-center gap-2`}>
        <div className="font-mono text-sm font-bold">{s.scenario_label}</div>
        <div className="text-sm flex-1 truncate">{s.strategy_name}</div>
        {isChosen && <CheckCircle2 className="h-4 w-4" />}
      </div>
      <div className="p-3 text-xs space-y-2 flex-1">
        <div>
          <div className="text-slate-400 uppercase tracking-wide text-xs">Assumption</div>
          <div className="text-slate-700">{s.assumption}</div>
        </div>
        <div>
          <div className="text-slate-400 uppercase tracking-wide text-xs">Action</div>
          <div className="text-slate-700">{s.proposed_action_description}</div>
        </div>
        {s.prerequisites && s.prerequisites.length > 0 && (
          <div>
            <div className="text-slate-400 uppercase tracking-wide text-xs">Prerequisites</div>
            <ul className="list-disc list-inside text-slate-600">
              {s.prerequisites.map((p, i) => <li key={i}>{p}</li>)}
            </ul>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2 pt-2 border-t border-slate-100">
          <Metric label="Schedule" value={`${scheduleSign}${s.estimated_schedule_impact_days}d`} />
          <Metric label="Cost" value={`$${(s.estimated_cost_impact_usd || 0).toLocaleString()}`} />
          <div>
            <div className="text-xs text-slate-500 uppercase tracking-wide">Risk</div>
            <span className={`pill ${riskColor} mt-1 inline-block capitalize`}>{s.estimated_risk_level}</span>
          </div>
          <div>
            <div className="text-xs text-slate-500 uppercase tracking-wide">Confidence</div>
            <div className="text-sm tabular-nums">{confPct}%</div>
          </div>
        </div>
        <button onClick={() => setShowReasoning((v) => !v)}
          className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 pt-2">
          {showReasoning ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          {showReasoning ? "Hide reasoning" : "Show reasoning"}
        </button>
        {showReasoning && (
          <div className="text-xs text-slate-600 bg-slate-50 rounded p-2 border border-slate-100 whitespace-pre-wrap">
            {s.ai_reasoning || "(no reasoning provided)"}
          </div>
        )}
      </div>
      <div className="px-3 py-2 border-t border-slate-100 space-y-1">
        {isChosen && hasOutcome && (
          <div className="text-xs text-emerald-700 space-y-1">
            <div className="inline-flex items-center gap-1">
              <CheckCircle2 className="h-3 w-3" /> Outcome recorded
            </div>
            <div className="text-slate-600 grid grid-cols-2 gap-1">
              <div>Actual cost: ${(s.actual_cost_usd ?? 0).toLocaleString()}</div>
              <div>Days recovered: {s.actual_time_recovered_days ?? "—"}</div>
            </div>
            {s.outcome_notes && (
              <div className="text-slate-600 italic">{s.outcome_notes}</div>
            )}
            <button data-testid={`scenario-edit-outcome-${s.scenario_label}`} onClick={onRecordActuals}
              className="text-blue-600 hover:underline">Edit outcome</button>
          </div>
        )}
        {isChosen && !hasOutcome && isExecuted && (
          <button data-testid={`scenario-record-actuals-${s.scenario_label}`} onClick={onRecordActuals}
            className="w-full h-8 px-3 rounded bg-blue-600 text-white text-xs hover:bg-blue-700">
            Record actuals
          </button>
        )}
        {isChosen && !hasOutcome && !isExecuted && (
          <div className="text-xs text-emerald-700 inline-flex items-center gap-1">
            <CheckCircle2 className="h-3 w-3" /> Chosen — see banner above to change.
          </div>
        )}
        {!isChosen && isOtherChosen && (
          <div className="text-xs text-slate-400">Not chosen</div>
        )}
        {!isChosen && !isOtherChosen && (
          <button data-testid={`scenario-choose-${s.scenario_label}`} onClick={onChoose}
            className="w-full h-8 px-3 rounded bg-emerald-600 text-white text-xs hover:bg-emerald-700">
            Choose this scenario
          </button>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div className="bg-slate-50 rounded px-2 py-1.5 border border-slate-100">
      <div className="text-xs text-slate-500 uppercase tracking-wide">{label}</div>
      <div className="text-sm tabular-nums">{value}</div>
    </div>
  );
}
